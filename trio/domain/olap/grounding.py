"""What checking a query's joins against the join graph found.

A join on the wrong column is valid SQL: both columns exist, the types match,
rows come back. Nothing downstream ever fails on it; the answer is simply
wrong. So each column-to-column join is compared with the keys the database
declares, and one that matches none of them gets a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trio.domain.olap.models import TableRef

#: The two tables are related, by other columns than the ones written.
WRONG_COLUMN = "wrong_column"
#: The two tables are related only through one or more tables in between.
MISSING_BRIDGE = "missing_bridge"
#: Nothing known relates them, and their values do not bear the join out.
UNVERIFIED = "unverified"
#: The tables are related by a key of several columns, and only some are joined.
INCOMPLETE_KEY = "incomplete_key"

#: Share of sampled values two columns must have in common for a join the
#: graph does not know to be taken as real. Generous on purpose: the graph can
#: be incomplete, and flagging a legitimate join costs more than passing one.
OVERLAP_KEEP = 0.2


@dataclass(frozen=True, slots=True)
class JoinTable:
    """A source table a query reads, and the columns it has."""

    ref: TableRef
    columns: frozenset[str]


@dataclass(frozen=True, slots=True)
class JoinVerdict:
    """One join that matches no known key, and what is known instead."""

    verdict: str
    left: str
    right: str
    #: The known join conditions between the two tables, in the order to apply them.
    route: tuple[str, ...] = ()
    #: Share of sampled values the columns have in common. None when it was
    #: not measured: it could not be, or both columns are keys already and
    #: shared values would prove nothing.
    overlap: float | None = None
    #: The query was rewritten to join by ``route``.
    repaired: bool = False

    def message(self) -> str:
        written = f"{self.left} = {self.right}"
        if self.verdict == WRONG_COLUMN:
            return (
                f"{written} is not how these tables are related; "
                f"the known key is {' or '.join(self.route)}"
            )
        if self.verdict == INCOMPLETE_KEY:
            return (
                f"{written} is one column of a key of several; "
                f"the whole key is {' and '.join(self.route)}"
            )
        if self.verdict == MISSING_BRIDGE:
            return (
                f"{written} joins two tables that are related only through "
                f"{' and '.join(self.route)}"
            )
        measured = (
            "nothing in the data vouches for it"
            if self.overlap is None
            else f"only {self.overlap:.0%} of sampled values match"
        )
        return f"{written} matches no known relationship, and {measured}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "join": f"{self.left} = {self.right}",
            "known": list(self.route),
            "overlap": self.overlap,
            "repaired": self.repaired,
            "message": self.message(),
        }


@dataclass(frozen=True, slots=True)
class Grounding:
    """The query to run (repaired or as written) and every verdict on its joins."""

    sql: str
    verdicts: tuple[JoinVerdict, ...] = ()

    @property
    def unresolved(self) -> tuple[JoinVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.repaired)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"joins": [v.as_dict() for v in self.verdicts]}
        if any(v.repaired for v in self.verdicts):
            out["sql"] = self.sql
        return out
