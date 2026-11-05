"""Ports: reading what a database was asked before, and mining it for expressions."""

from collections.abc import Collection, Mapping
from typing import Protocol

from trio.domain.catalog.history import HistoryEntry, MinedExpression


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
