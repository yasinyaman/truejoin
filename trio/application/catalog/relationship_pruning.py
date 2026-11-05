"""Check each relationship of a catalog against the data before anyone joins on it.

A relationship the language model inferred from names is a guess. A declared
foreign key is not, so those pass unprobed; every other relationship gets one
bounded, read-only probe: do any of the source column's values exist in the
target column? Relationships are marked, never removed, so a reviewer still
sees what was proposed and why it does not count.
"""

from __future__ import annotations

import logging
from typing import Any

from trio.domain.catalog.catalog import DatabaseCatalog, RelationshipInfo
from trio.ports.database import DatabaseGateway

logger = logging.getLogger(__name__)

#: Distinct source values looked up in the target.
SAMPLE_VALUES = 200
#: Source rows read to find them.
SAMPLE_ROWS = 2000


async def verify_relationships(gateway: DatabaseGateway, catalog: DatabaseCatalog) -> int:
    """Set ``verified`` on every relationship of ``catalog``; return how many failed."""
    refuted = 0
    for table in catalog.tables.values():
        declared = {
            (fk.column, fk.references_table, fk.references_column) for fk in table.foreign_keys
        }
        for relationship in table.relationships:
            key = (
                relationship.source_column,
                relationship.target_table,
                relationship.target_column,
            )
            if key in declared:
                relationship.verified = True
                continue
            target = catalog.tables.get(relationship.target_table)
            if (
                target is None
                or target.get_column(relationship.target_column) is None
                or table.get_column(relationship.source_column) is None
            ):
                relationship.verified = False  # names a table or column that is not there
            else:
                relationship.verified = await _shares_values(
                    gateway, table.table_name, relationship
                )
            if relationship.verified is False:
                refuted += 1
                logger.warning(
                    "Relationship %s.%s -> %s.%s is not borne out by the data",
                    table.table_name,
                    *key,
                )
    return refuted


async def _shares_values(
    gateway: DatabaseGateway, table: str, relationship: RelationshipInfo
) -> bool | None:
    """True/False for overlap or none; None when it could not be told.

    An empty source column and a failed probe both leave the question open:
    neither is evidence that the columns are unrelated.
    """
    column = relationship.source_column
    values: dict[Any, None] = {}
    try:
        # Both reads are bounded by their limit and run to the end, so the
        # cursor is closed by the time the answer is known.
        async for batch in gateway.stream_select(table, columns=[column], limit=SAMPLE_ROWS):
            values.update((row[column], None) for row in batch if row.get(column) is not None)
        if not values:
            return None
        found = False
        async for batch in gateway.stream_select(
            relationship.target_table,
            columns=[relationship.target_column],
            filters=[(relationship.target_column, "in", list(values)[:SAMPLE_VALUES])],
            limit=1,
        ):
            found = found or bool(batch)
        return found
    except Exception as e:
        logger.warning(
            "Could not check relationship %s.%s -> %s.%s: %s",
            table,
            column,
            relationship.target_table,
            relationship.target_column,
            e,
        )
        return None
