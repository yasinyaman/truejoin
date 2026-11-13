"""Port: checking the joins a query writes against the keys that really exist."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from truejoin.domain.catalog.join_graph import JoinGraph
from truejoin.domain.olap.grounding import Grounding, JoinTable
from truejoin.domain.olap.models import TableRef

Probe = Callable[[TableRef, str, TableRef, str], float | None]
"""``(table, column, table, column)`` -> the share of sampled values the two
columns have in common, or None when it cannot be measured."""


class SqlGrounder(Protocol):
    def ground(
        self,
        sql: str,
        tables: Mapping[TableRef, JoinTable],
        graph: JoinGraph,
        probe: Probe,
        repair: bool = False,
    ) -> Grounding:
        """Verdicts on the column-to-column joins of ``sql``.

        ``tables`` is keyed by the reference as the query writes it (an
        unqualified name has an empty ``source``). Only joins where a column
        is a known key are judged; a join on anything else is business logic
        the graph was never meant to model. A column that cannot be attributed
        to exactly one table is left alone: missing information never flags.

        With ``repair``, a verdict that has exactly one safe fix is applied
        and marked ``repaired``, and the returned SQL is checked again. What
        is left in ``unresolved`` could not be fixed; the caller decides
        whether such a query may run.
        """
        ...
