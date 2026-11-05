"""What a database has been asked before: the raw material for suggested measures.

People write the same definitions into query after query and into views: what
counts as a paid order, how a net amount is computed. Those are read here as
they were run, mined for the expressions they repeat, and offered to a
reviewer as draft ``SqlAttribute``s. Nothing mined is read by anything until
it is certified. After NVIDIA Auto Ontology's query-history mining; reading
view definitions as well is this project's addition.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


class HistoryUnavailableError(Exception):
    """A database's statement history or view definitions cannot be read.

    The message says why and what would make it readable, because the usual
    reason is a grant only a DBA can give.
    """


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One statement that ran, or one view's definition."""

    sql: str
    #: How many times it ran; 1 for a view, whose use nobody counted.
    calls: int = 1
    last_seen: datetime | None = None
    #: "history" for a statement that ran, "view" for a definition.
    origin: str = "history"
    #: The view's name, when it is one.
    name: str = ""

    @property
    def id(self) -> str:
        """A short, stable name for where an expression came from."""
        if self.origin == "view":
            return f"view:{self.name}"
        return (
            "sql:" + hashlib.sha1(self.sql.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
        )


#: An expression inside an aggregate, e.g. the CASE in ``SUM(CASE ...)``.
AGGREGATE = "aggregate"
#: A WHERE condition, offered as a 1/0 flag to be summed.
CONDITION = "condition"
#: Arithmetic a statement selects, e.g. ``total - discount AS net``: what a
#: view usually exists to define.
PROJECTION = "projection"


@dataclass(frozen=True, slots=True)
class MinedExpression:
    """A scalar expression over one table's columns, found in a statement."""

    table: str
    #: DuckDB SQL naming the table's columns unqualified.
    expression: str
    kind: str
    columns: tuple[str, ...]


@dataclass(slots=True)
class Candidate:
    """One expression with everything that speaks for it."""

    table: str
    expression: str
    kind: str
    columns: tuple[str, ...]
    calls: int = 0
    in_view: bool = False
    sources: list[str] = field(default_factory=list)

    @property
    def rank(self) -> tuple[bool, int]:
        """A definition someone put in a view first, then by how often it ran."""
        return self.in_view, self.calls
