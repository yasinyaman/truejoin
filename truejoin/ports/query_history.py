"""Ports: reading what a database was asked before, and mining it for expressions."""

from collections.abc import Collection, Mapping
from typing import Protocol

from truejoin.domain.catalog.history import HistoryEntry, MinedExpression


class QueryHistorySource(Protocol):
    """A database's own record of the statements it ran and the views it holds."""

    async def recent_statements(self, limit: int = 500) -> list[HistoryEntry]:
        """The most-run SELECT statements, most frequent first.

        Raises:
            HistoryUnavailableError: The engine keeps no such record, or this
                user may not read it.
        """
        ...

    async def view_definitions(self) -> list[HistoryEntry]:
        """The definition of every view in the configured schema.

        Raises:
            HistoryUnavailableError: As above.
        """
        ...


class ExpressionChecker(Protocol):
    """Holds an expression a reviewer wrote to what a measure may be."""

    def check(self, expression: str, columns: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
        """``expression`` as the engine will run it, and the columns it reads.

        ``columns`` maps the table's column names to their kinds
        (``type_kind``: int, float, bool, str, date, ...). The expression is
        what goes inside an aggregate, over those columns only: no subquery,
        aggregate, window or parameter, and something that can be added up.

        Raises:
            MeasureError: Saying what is wrong with it.
        """
        ...


class ExpressionMiner(Protocol):
    """Finds the reusable scalar expressions in a statement."""

    def mine(
        self, sql: str, dialect: str, columns: Mapping[str, Collection[str]]
    ) -> list[MinedExpression]:
        """Expressions in ``sql`` (written in ``dialect``) over one known table each.

        ``columns`` maps each table of the catalog to its column names; an
        expression is returned only when every column it reads can be placed
        in exactly one of them, and when it holds no parameter, subquery,
        aggregate or window. A statement that does not parse yields nothing.
        """
        ...
