"""The audit record: who read or changed what, and whether it was restricted.

One event per data-touching request. The point of recording *whether* a row
filter or a mask applied is that an audit log which cannot distinguish "saw
everything" from "saw their own tenant, with the email column masked" cannot
answer the only question anyone asks it afterwards.

What an event must never carry is the data itself: no row values, no filter
literals, no SQL parameters. An audit trail that quotes the values it is
auditing becomes a second copy of the data, in a file that usually has weaker
access controls and a longer retention than the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

AuditAction = Literal["read", "create", "update", "delete", "export", "query"]

#: Actions that change data, kept separately because retention rules and
#: alerting usually treat them differently from reads.
WRITE_ACTIONS: frozenset[str] = frozenset({"create", "update", "delete"})


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One recorded data access."""

    action: AuditAction
    database: str
    table: str
    #: The API key's name, or ``None`` when authentication is off. ``None`` is
    #: itself worth recording: it means the request could not be attributed.
    actor: str | None = None
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    request_id: str = ""
    status: int = 200
    row_count: int | None = None
    #: Whether a row policy narrowed this request, and which columns were
    #: masked — the difference between "read the table" and "read their slice
    #: of it, with two columns hidden".
    row_filtered: bool = False
    masked_columns: tuple[str, ...] = ()
    #: Column names the caller filtered on. Names only, never the values.
    filtered_columns: tuple[str, ...] = ()
    #: Set for a change to a catalog rather than to data: what was done, by
    #: name only (``approve_table``, ``edit_column email semantic_type``),
    #: never the text a reviewer wrote. ``table`` is empty for the whole catalog.
    change: str = ""
    #: What ``change`` changed: a catalog, or who may get in (``access``: a
    #: key handed out or revoked, by its name only).
    subject: Literal["catalog", "access"] = "catalog"
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_write(self) -> bool:
        """Whether this event changed data."""
        return self.action in WRITE_ACTIONS

    @property
    def restricted(self) -> bool:
        """Whether anything was withheld from the caller."""
        return self.row_filtered or bool(self.masked_columns)

    def as_dict(self) -> dict[str, Any]:
        """The event as a flat JSON-safe mapping."""
        return {
            "event": f"{self.subject}_change" if self.change else "data_access",
            "at": self.at.isoformat(),
            "action": self.action,
            "database": self.database,
            "table": self.table,
            "actor": self.actor,
            "tenant": self.tenant,
            "roles": list(self.roles),
            "request_id": self.request_id,
            "status": self.status,
            "row_count": self.row_count,
            "row_filtered": self.row_filtered,
            "masked_columns": list(self.masked_columns),
            "filtered_columns": list(self.filtered_columns),
            "change": self.change,
            "restricted": self.restricted,
            "is_write": self.is_write,
        }

    def summary(self) -> str:
        """A one-line human-readable form, for a text log."""
        who = self.actor or "anonymous"
        where = f"{self.database}.{self.table}" if self.table else self.database
        if self.change:
            return f"{who} {self.subject} {self.change} {where} status={self.status}"
        rows = "" if self.row_count is None else f" rows={self.row_count}"
        marks = []
        if self.row_filtered:
            marks.append("row-filtered")
        if self.masked_columns:
            marks.append(f"masked={','.join(self.masked_columns)}")
        suffix = f" [{'; '.join(marks)}]" if marks else ""
        return f"{who} {self.action} {where}{rows} status={self.status}{suffix}"


ToolOutcome = Literal["answered", "empty", "refused", "failed", "error"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One analytical tool call, as usage: what was asked for, and how it went.

    The structure of the request, never its values: the table, the measures
    and breakdowns by name, the columns a condition was on, the abbreviations
    the glossary wrote out. Never the SQL, whose literals are data. The words
    of a question only when the deployment says so (``audit.question_text``):
    "how much did Ayşe Yılmaz spend" is a question and a record of a person.
    """

    tool: str
    outcome: ToolOutcome
    actor: str | None = None
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    request_id: str = ""
    duration_ms: float = 0.0
    row_count: int | None = None
    table: str = ""
    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    filtered_columns: tuple[str, ...] = ()
    glossary: tuple[str, ...] = ()
    question: str | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        """The call as a flat JSON-safe mapping."""
        out: dict[str, Any] = {
            "event": "tool_call",
            "at": self.at.isoformat(),
            # As data_access and catalog_change name theirs, so one filter reads all three.
            "action": "tool",
            "tool": self.tool,
            "outcome": self.outcome,
            "actor": self.actor,
            "tenant": self.tenant,
            "roles": list(self.roles),
            "request_id": self.request_id,
            "duration_ms": round(self.duration_ms, 1),
            "row_count": self.row_count,
            "database": self.table.partition(".")[0] if "." in self.table else "",
            "table": self.table,
            "measures": list(self.measures),
            "dimensions": list(self.dimensions),
            "filtered_columns": list(self.filtered_columns),
            "glossary": list(self.glossary),
        }
        if self.question is not None:
            out["question"] = self.question
        return out

    def summary(self) -> str:
        """A one-line human-readable form, for a text log."""
        who = self.actor or "anonymous"
        on = f" {self.table}" if self.table else ""
        rows = "" if self.row_count is None else f" rows={self.row_count}"
        return f"{who} tool {self.tool}{on} {self.outcome}{rows} {self.duration_ms:.0f}ms"
