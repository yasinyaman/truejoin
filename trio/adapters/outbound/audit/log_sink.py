"""Audit sinks that write to a logger or a file."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from trio.domain.governance.audit import AuditEvent, ToolCall

logger = logging.getLogger(__name__)

#: A dedicated logger so audit output can be routed (and retained) separately
#: from application logs without filtering on message text.
AUDIT_LOGGER = "warp.audit"


class LoggingAuditSink:
    """Writes one JSON object per event.

    Events go to the ``warp.audit`` logger, and additionally to ``path`` when
    one is configured — appended, never rewritten, so the file is an append-only
    record even if the process restarts.
    """

    def __init__(self, path: str | None = None, filter_columns: bool = True) -> None:
        """Open the audit file, if one is configured.

        ``filter_columns`` False (``audit.log_filter_columns``) drops the names
        of the columns a caller filtered on from every event before it is
        written anywhere.
        """
        self._filter_columns = filter_columns
        self._logger = logging.getLogger(AUDIT_LOGGER)
        self._path = Path(path) if path else None
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.error(f"Cannot create the audit directory {self._path.parent}: {e}")
                self._path = None

    def record(self, event: AuditEvent | ToolCall) -> None:
        """Record one event, and never let a logging failure fail the request."""
        if not self._filter_columns and event.filtered_columns:
            event = replace(event, filtered_columns=())
        payload = event.as_dict()
        try:
            self._logger.info(event.summary(), extra={"extra_fields": payload})
        except Exception as e:  # pragma: no cover - a broken logging config
            logger.error(f"Audit logging failed: {e}")
        if self._path is None:
            return
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
        except OSError as e:
            logger.error(f"Could not append to the audit file {self._path}: {e}")


class JsonlAuditReader:
    """Reads the audit file that ``LoggingAuditSink`` appends to, newest first."""

    # shortcut: only the file's last 8 MB (tens of thousands of events) are
    # searched; read rotated or older trails with jq until paging is needed.
    SCAN_BYTES = 8 * 1024 * 1024

    def __init__(self, path: str, scan_bytes: int = SCAN_BYTES) -> None:
        """Remember the file; it is opened on every read, so rotation is fine."""
        self._path = Path(path)
        self._scan_bytes = scan_bytes

    def recent(self, limit: int, match: Mapping[str, str]) -> tuple[list[dict[str, Any]], bool]:
        """The newest ``limit`` events whose fields equal ``match``, and whether all were read."""
        try:
            with self._path.open("rb") as handle:
                size = handle.seek(0, 2)
                start = max(0, size - self._scan_bytes)
                handle.seek(start)
                data = handle.read()
        except FileNotFoundError:
            return [], True
        lines = data.split(b"\n")
        if start:
            lines = lines[1:]  # cut mid-line
        wanted = {key: value.lower() for key, value in match.items()}
        found: list[dict[str, Any]] = []
        for raw in reversed(lines):
            try:
                event = json.loads(raw)
            except ValueError:
                continue  # blank, or a line cut short by a crash
            if not isinstance(event, dict):
                continue
            if all(str(event.get(k)).lower() == v for k, v in wanted.items()):
                found.append(event)
                if len(found) >= limit:
                    break
        return found, start == 0

    def tool_calls(self, since: str | None = None) -> Iterator[dict[str, Any]]:
        """Every tool call in the file, oldest first, one line at a time.

        shortcut: the whole current file on every call (a cheap test on each
        line before it is parsed); a rotated file is not read. Keep a running
        total when trails grow past a few hundred MB.
        """
        try:
            handle = self._path.open("rb")
        except FileNotFoundError:
            return
        with handle:
            for raw in handle:
                if b'"tool_call"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("event") != "tool_call":
                    continue
                if since is not None and str(event.get("at", "")) < since:
                    continue
                yield event
