"""The semantic model a reviewed catalog implies for one table.

The types say which columns are numbers. The catalog says which of those are
keys (counted, never summed), what each column is, and which expressions a
reviewer certified as measures. Only certified terms of an approved catalog
reach this; see ``DatabaseCatalog.certified_terms``.
"""

from __future__ import annotations

import logging

from truejoin.domain.catalog.catalog import TableCatalogEntry
from truejoin.domain.catalog.terms import Term
from truejoin.domain.olap.identifiers import VIEW_NAME_RE
from truejoin.domain.olap.measures import (
    NUMERIC_AGGS,
    Measure,
    SemanticModel,
    columns_read,
    infer_model,
    is_numeric_type,
)
from truejoin.domain.olap.models import TableRef, TableSchema

logger = logging.getLogger(__name__)

_KEY_TYPES = ("id", "identifier", "key")


def semantic_model_from_catalog(
    ref: TableRef, schema: TableSchema, entry: TableCatalogEntry, term: Term, lang: str = "en"
) -> SemanticModel:
    """``ref``'s model, from its stored schema and its reviewed catalog entry."""
    keys = set(schema.primary_key) | {key.column for key in schema.foreign_keys}
    descriptions = {}
    for column in entry.columns:
        kind = (column.semantic_type or "").lower()
        if column.is_primary_key or column.is_foreign_key or kind in _KEY_TYPES:
            keys.add(column.name)
        text = column.description.get(lang) or str(column.description)
        if text:
            descriptions[column.name] = text

    expressions = []
    for attribute in term.certified_sql(ref.table):
        reads = columns_read(attribute.expression, [c.name for c in schema.columns])
        if not VIEW_NAME_RE.match(attribute.name) or not reads:
            # Left out rather than refused: one unusable expression must not
            # take the table's whole model with it.
            logger.warning(
                "Expression %s of term %s is not offered as a measure: %s",
                attribute.name,
                term.name,
                "its name is not an identifier" if reads else "it names no column of the table",
            )
            continue
        agg = attribute.aggregation.upper()
        types = {c.name: c.type for c in schema.columns}
        expressions.append(
            Measure(
                name=attribute.name,
                column=reads[0],
                default_agg=agg,
                # A sum is the old meaning, and a sum of a flag may be averaged
                # (a share): only another aggregation is held to itself.
                fixed_agg=agg != "SUM",
                numeric=agg in NUMERIC_AGGS
                or all(is_numeric_type(str(types.get(r, ""))) for r in reads),
                description=attribute.description.get(lang) or str(attribute.description),
                expression=attribute.expression,
                reads=reads,
            )
        )
    return infer_model(ref, schema, keys, descriptions, expressions, source="catalog")
