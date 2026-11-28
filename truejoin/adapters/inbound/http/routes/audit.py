"""Audit trail endpoint: the recorded events, for the people who review them."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from truejoin.adapters.inbound.http.auth import AuthManager, Permission
from truejoin.application.usage import summarize
from truejoin.domain.olap.errors import FusionError
from truejoin.ports.audit import AuditReader

#: Works out, as the caller of a request, which words of the kept questions
#: the vocabulary does not know (``ToolService.vocabulary_gaps``).
GapFinder = Callable[[Request, Mapping[str, Sequence[int]]], dict[str, Any]]


def create_audit_router(
    reader: AuditReader | None,
    auth_manager: AuthManager | None = None,
    gaps: GapFinder | None = None,
) -> APIRouter:
    """``GET /audit``: the newest events, filtered by field.

    Needs the ``audit`` permission (or ``all``). Events carry names, never row
    values, but who read which table is itself worth guarding.
    """
    deps = (
        [Depends(auth_manager.require(Permission.AUDIT))]
        if auth_manager is not None and auth_manager.enabled
        else []
    )
    router = APIRouter(prefix="/audit", tags=["Audit"])

    @router.get("", dependencies=deps, summary="Read the audit trail, newest first")
    async def read_audit(  # noqa: PLR0913
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        event: Literal[
            "data_access",
            "catalog_change",
            "access_change",
            "tool_call",
            "sign_in",
            "assistant_run",
        ]
        | None = None,
        actor: str | None = None,
        tool: str | None = None,
        outcome: Literal[
            "answered", "empty", "refused", "failed", "error", "signed_in", "paused", "exhausted"
        ]
        | None = None,
        database: str | None = None,
        table: str | None = None,
        action: str | None = None,
        restricted: bool | None = None,
        sql_fingerprint: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
        request_id: Annotated[str | None, Query(max_length=64)] = None,
        via: Literal["assistant"] | None = None,
    ) -> dict[str, Any]:
        """Recorded events, newest first; every filter must match exactly (any case).

        ``request_id`` reads one request whole: an assistant's run and the tool
        calls and reads it made as the caller share theirs.
        """
        if reader is None:
            return {
                "available": False,
                "detail": "Nothing to read: set audit.enabled and audit.file to keep a trail.",
                "events": [],
                "complete": True,
            }
        match = {
            key: str(value)
            for key, value in {
                "event": event,
                "actor": actor,
                "tool": tool,
                "outcome": outcome,
                "database": database,
                "table": table,
                "action": action,
                "restricted": restricted,
                "sql_fingerprint": sql_fingerprint,
                "request_id": request_id,
                "via": via,
            }.items()
            if value not in (None, "")
        }
        # Off the event loop: it reads up to 8 MB of the file.
        events, complete = await asyncio.to_thread(reader.recent, limit, match)
        return {"available": True, "events": events, "complete": complete}

    @router.get("/usage", dependencies=deps, summary="What the tool calls add up to")
    async def read_usage(
        request: Request,
        days: Annotated[int | None, Query(ge=1, le=3650)] = None,
    ) -> dict[str, Any]:
        """Every tool call in the trail (or the last ``days``), counted.

        Outcomes, the most asked-for tools, tables, measures, breakdowns,
        filtered columns and abbreviations, and the newest calls that were
        not answered: names only, as the events carry them. When the words of
        questions are kept, ``vocabulary`` says which of them the certified
        vocabulary does not know, the ones in unanswered questions first.
        """
        if reader is None:
            return {
                "available": False,
                "detail": "Nothing to read: set audit.enabled and audit.file to keep a trail.",
            }
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days else None
        summary = await asyncio.to_thread(lambda: summarize(reader.tool_calls(since)))
        asked = summary.pop("asked")
        if gaps is not None and asked:
            try:
                summary["vocabulary"] = await asyncio.to_thread(gaps, request, asked)
            except FusionError as e:
                summary["vocabulary"] = {"available": False, "detail": str(e)}
        return {"available": True, "days": days, **summary}

    return router
