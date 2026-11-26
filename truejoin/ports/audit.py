"""Port: where audit events go."""

from collections.abc import Iterator, Mapping
from typing import Any, Protocol

from truejoin.domain.governance.audit import AuditRecord


class AuditSink(Protocol):
    """Records data-access events.

    Implementations must never raise: an audit sink that can fail a request
    turns a logging problem into an outage. They must also never be given
    row values — see :mod:`truejoin.domain.governance.audit`.
    """

    def record(self, event: AuditRecord) -> None:
        """Record one event."""
        ...


class AuditReader(Protocol):
    """Reads recorded events back, for the people who review them."""

    def recent(self, limit: int, match: Mapping[str, str]) -> tuple[list[dict[str, Any]], bool]:
        """The newest events whose fields equal ``match`` (case-insensitive), newest first.

        Returns the events and whether the whole trail was searched.
        """
        ...

    def tool_calls(self, since: str | None = None) -> Iterator[dict[str, Any]]:
        """Every ``tool_call`` event in the trail, oldest first; from ``since`` (ISO) on."""
        ...


class NullAuditSink:
    """Records nothing (auditing disabled).

    Lives beside the port rather than with the real sinks: it performs no I/O,
    and the inbound adapters need a default without importing an outbound one.
    """

    def record(self, event: AuditRecord) -> None:
        """Discard the event."""
