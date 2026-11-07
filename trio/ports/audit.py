"""Port: where audit events go."""

from collections.abc import Mapping
from typing import Any, Protocol

from trio.domain.governance.audit import AuditEvent, ToolCall


class AuditSink(Protocol):
    """Records data-access events.

    Implementations must never raise: an audit sink that can fail a request
    turns a logging problem into an outage. They must also never be given
    row values — see :mod:`trio.domain.governance.audit`.
    """

    def record(self, event: AuditEvent | ToolCall) -> None:
        """Record one event."""
        ...


class AuditReader(Protocol):
    """Reads recorded events back, for the people who review them."""

    def recent(self, limit: int, match: Mapping[str, str]) -> tuple[list[dict[str, Any]], bool]:
        """The newest events whose fields equal ``match`` (case-insensitive), newest first.

        Returns the events and whether the whole trail was searched.
        """
        ...


class NullAuditSink:
    """Records nothing (auditing disabled).

    Lives beside the port rather than with the real sinks: it performs no I/O,
    and the inbound adapters need a default without importing an outbound one.
    """

    def record(self, event: AuditEvent | ToolCall) -> None:
        """Discard the event."""
