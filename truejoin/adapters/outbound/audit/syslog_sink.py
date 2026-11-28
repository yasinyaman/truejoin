"""The audit trail to a SIEM over syslog: RFC 5424, as JSON or as CEF.

Not ``logging.handlers.SysLogHandler``: it connects while it is built (a SIEM
that is down would stop the server starting), blocks on every send (a slow one
would stall requests), never reconnects after a failure, writes no RFC 5424
header and has no TLS. Here a bounded queue takes the event at once and a
daemon thread sends it:

- UDP, TCP or TLS (``ssl``, the CA from ``ca_file`` or the system's); TCP and
  TLS frames are octet-counted (RFC 6587), so a message may hold a newline.
- A receiver that is down or goes away is tried again, waiting a little longer
  each time (up to 30 seconds); meanwhile events wait in the queue.
- A full queue drops the new event and counts it; the count is logged when
  sending resumes. Recording never blocks and never raises.
- ``close`` sends what is queued, for a few seconds at most.

The payload is the event's own ``as_dict``, the same fields the audit file
holds: names and counts, never SQL or values.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import socket
import ssl
import threading
import time
from datetime import UTC, datetime
from typing import Any

from truejoin.adapters.outbound.audit.log_sink import without_filter_columns
from truejoin.domain.governance.audit import AuditRecord

logger = logging.getLogger(__name__)

#: RFC 5424 facility codes, by name.
FACILITIES = {
    "kern": 0, "user": 1, "daemon": 3, "auth": 4, "authpriv": 10, "local0": 16,
    "local1": 17, "local2": 18, "local3": 19, "local4": 20, "local5": 21,
    "local6": 22, "local7": 23,
}  # fmt: skip

_STOP = object()
_MAX_BACKOFF = 30.0


def severity(payload: dict[str, Any]) -> tuple[int, int]:
    """(syslog severity, CEF severity) of one event.

    A refusal or a failed sign-in is a warning; a change to who may get in, to a
    catalog or to data a notice; everything else information.
    """
    status = payload.get("status")
    refused = payload.get("outcome") in {"refused", "paused", "error", "failed"} or (
        isinstance(status, int) and status >= 400
    )
    if refused:
        return 4, 7
    if payload.get("event") in {"access_change", "catalog_change"} or payload.get("is_write"):
        return 5, 5
    return 6, 3


def _signature(payload: dict[str, Any]) -> str:
    event = str(payload.get("event", "event"))
    detail = payload.get("tool") or payload.get("method") or payload.get("action") or ""
    return f"{event}.{detail}" if detail else event


def _cef_header(value: Any) -> str:
    return (
        str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    )


def _cef_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    text = str(value)
    return text.replace("\\", "\\\\").replace("=", "\\=").replace("\n", "\\n").replace("\r", "\\r")


#: Event field -> CEF extension key, with a label for the custom ones.
_CEF_FIELDS: tuple[tuple[str, str, str | None], ...] = (
    ("actor", "suser", None),
    ("action", "act", None),
    ("event", "cat", None),
    ("outcome", "outcome", None),
    ("request_id", "externalId", None),
    ("change", "msg", None),
    ("client", "src", None),
    ("row_count", "cn1", "rows"),
    ("status", "cn2", "status"),
    ("duration_ms", "cn3", "duration_ms"),
    ("database", "cs1", "database"),
    ("table", "cs2", "table"),
    ("tenant", "cs3", "tenant"),
    ("roles", "cs4", "roles"),
    ("masked_columns", "cs5", "masked_columns"),
    ("sql_fingerprint", "cs6", "sql_fingerprint"),
    ("tool", "flexString1", "tool"),
    ("method", "flexString2", "method"),
    ("steps", "flexNumber1", "steps"),
    ("tool_calls", "flexNumber2", "tool_calls"),
    ("via", "requestClientApplication", None),
)


def to_cef(payload: dict[str, Any], version: str, host: str) -> str:
    """One event as a CEF line (ArcSight Common Event Format, version 0)."""
    _, level = severity(payload)
    parts = [f"rt={_epoch_ms(payload.get('at'))}", f"dvchost={_cef_value(host)}"]
    for name, key, label in _CEF_FIELDS:
        value = payload.get(name)
        if value is None or value == "" or value == []:
            continue
        parts.append(f"{key}={_cef_value(value)}")
        if label:
            parts.append(f"{key}Label={label}")
    header = "|".join(
        _cef_header(v)
        for v in ("Truejoin", "Truejoin", version, _signature(payload), _name(payload), level)
    )
    return f"CEF:0|{header}|{' '.join(parts)}"


def _name(payload: dict[str, Any]) -> str:
    who = payload.get("actor") or "anonymous"
    what = payload.get("change") or payload.get("tool") or payload.get("action") or "event"
    where = ".".join(str(p) for p in (payload.get("database"), payload.get("table")) if p)
    return " ".join(str(p) for p in (who, what, where) if p)


def _epoch_ms(at: Any) -> int:
    try:
        return int(datetime.fromisoformat(str(at)).timestamp() * 1000)
    except ValueError:
        return int(time.time() * 1000)


def rfc5424(payload: dict[str, Any], message: str, *, facility: int, host: str, app: str) -> bytes:
    """``<PRI>1 TIMESTAMP HOST APP PROCID MSGID - MSG``, encoded as UTF-8."""
    level, _ = severity(payload)
    stamp = str(payload.get("at") or datetime.now(UTC).isoformat())
    msgid = str(payload.get("event", "-"))[:32] or "-"
    head = f"<{facility * 8 + level}>1 {stamp} {host or '-'} {app or '-'} - {msgid} -"
    return f"{head} {message}".encode()


class SyslogAuditSink:
    """Sends each event to a syslog receiver from a background thread."""

    def __init__(  # noqa: PLR0913
        self,
        host: str,
        port: int,
        protocol: str = "udp",
        fmt: str = "json",
        facility: str = "local0",
        app_name: str = "truejoin",
        ca_file: str = "",
        queue_size: int = 10_000,
        version: str = "",
        filter_columns: bool = True,
        timeout: float = 5.0,
    ) -> None:
        if protocol not in {"udp", "tcp", "tls"}:
            raise ValueError(f"syslog protocol '{protocol}' is not udp, tcp or tls")
        if fmt not in {"json", "cef"}:
            raise ValueError(f"syslog format '{fmt}' is not json or cef")
        if facility not in FACILITIES:
            raise ValueError(f"syslog facility '{facility}' is not one of {sorted(FACILITIES)}")
        self._address = (host, port)
        self._protocol = protocol
        self._format = fmt
        self._facility = FACILITIES[facility]
        self._app = app_name
        self._ca_file = ca_file
        self._version = version
        self._filter_columns = filter_columns
        self._timeout = timeout
        self._host = socket.gethostname()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, queue_size))
        self._socket: socket.socket | None = None
        self._dropped = 0
        self._sent = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="audit-syslog", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    # -- recording (any thread) ------------------------------------------------

    def record(self, event: AuditRecord) -> None:
        """Queue the event; a full queue drops it and counts it. Never blocks."""
        if self._closed:
            return
        try:
            payload = without_filter_columns(event, self._filter_columns).as_dict()
            self._queue.put_nowait(self.encode(payload))
        except queue.Full:
            self._dropped += 1
        except Exception as e:  # pragma: no cover - an event that cannot be encoded
            logger.error(f"Could not queue an audit event for syslog: {e}")

    def encode(self, payload: dict[str, Any]) -> bytes:
        """The datagram or frame payload for one event."""
        if self._format == "cef":
            message = to_cef(payload, self._version, self._host)
        else:
            message = json.dumps(payload, default=str, separators=(",", ":"))
        return rfc5424(payload, message, facility=self._facility, host=self._host, app=self._app)

    @property
    def dropped(self) -> int:
        """Events dropped because the queue was full."""
        return self._dropped

    @property
    def sent(self) -> int:
        """Events handed to the network."""
        return self._sent

    # -- sending (the thread) ----------------------------------------------------

    def _run(self) -> None:
        backoff = 0.5
        pending: bytes | None = None
        while True:
            if pending is None:
                item = self._queue.get()
                if item is _STOP:
                    return
                pending = item
            try:
                self._send(pending)
            except OSError as e:
                self._disconnect()
                if self._closed:
                    return
                logger.warning(f"Audit syslog {self._address[0]}:{self._address[1]} failed: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            pending = None
            backoff = 0.5
            self._sent += 1
            if self._dropped:
                logger.warning(f"Audit syslog dropped {self._dropped} event(s): the queue was full")
                self._dropped = 0

    def _send(self, message: bytes) -> None:
        if self._protocol == "udp":
            if self._socket is None:
                self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.sendto(message, self._address)
            return
        if self._socket is None:
            self._socket = self._connect()
        self._socket.sendall(f"{len(message)} ".encode() + message)

    def _connect(self) -> socket.socket:
        raw = socket.create_connection(self._address, timeout=self._timeout)
        if self._protocol != "tls":
            return raw
        context = ssl.create_default_context(cafile=self._ca_file or None)
        try:
            return context.wrap_socket(raw, server_hostname=self._address[0])
        except Exception:
            raw.close()
            raise

    def _disconnect(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def close(self, timeout: float = 3.0) -> None:
        """Send what is queued (for ``timeout`` seconds at most), then stop."""
        if self._closed:
            return
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            pass
        self._thread.join(timeout)
        self._closed = True
        self._disconnect()
        atexit.unregister(self.close)
