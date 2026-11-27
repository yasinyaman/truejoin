"""Read dbt MetricFlow semantic models (``semantic_models:`` and ``metrics:``) into a draft.

- A semantic model is a table: ``model: ref('orders')`` or ``node_relation``.
- An entity of type ``foreign`` references the model whose ``primary`` entity
  has its name: a single-column relationship. ``unique`` and ``natural``
  entities have no place and are skipped.
- A dimension whose ``expr`` is a bare column is that column; its name, when
  it differs, a word for it. A time dimension's granularity is not kept.
- A measure is one aggregation of one expression: ``sum``, ``count``,
  ``average``, ``min``, ``max``, ``count_distinct``, ``median``, and
  ``sum_boolean`` as the sum of a 1/0 flag. ``percentile``, and a measure with
  a ``non_additive_dimension``, are skipped.
- A ``simple`` metric without a filter is its measure, already read; every
  other metric (``ratio``, ``derived``, ``cumulative``, ``conversion``, or one
  with a filter) is a calculation the catalog has no place for, and is skipped.

Expressions are read as ANSI SQL and checked as DuckDB, where measures run.
"""

from __future__ import annotations

import re
from typing import Any

from truejoin.adapters.outbound.export.ossie_import import load_document
from truejoin.domain.catalog.semantic_draft import (
    DraftColumn,
    DraftMeasure,
    DraftRelationship,
    DraftTable,
    SemanticDraft,
    Skipped,
)

_REF = re.compile(r"""ref\(\s*['"]([^'"]+)['"]\s*\)""")
_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")

#: MetricFlow's aggregations, as a catalog expression's.
_AGGREGATIONS = {
    "sum": "sum",
    "count": "count",
    "average": "avg",
    "avg": "avg",
    "min": "min",
    "max": "max",
    "count_distinct": "count_distinct",
    "median": "median",
}


def _table_of(model: dict[str, Any]) -> str | None:
    relation = model.get("node_relation")
    if isinstance(relation, dict) and relation.get("alias"):
        return str(relation["alias"])
    ref = _REF.search(str(model.get("model") or ""))
    if ref:
        return ref.group(1)
    return None


def _column(item: dict[str, Any]) -> str | None:
    """The bare column an entity or a dimension is, or None when it is an expression."""
    expr = str(item.get("expr") or item.get("name") or "").strip()
    return expr if _PLAIN_NAME.match(expr) else None


class MetricFlowReader:
    """MetricFlow YAML (one file, or several concatenated) as a draft."""

    def read(self, text: str) -> SemanticDraft:  # noqa: C901, PLR0912, PLR0915
        document = load_document(text)
        if not isinstance(document, dict) or not isinstance(document.get("semantic_models"), list):
            raise ValueError("A MetricFlow file has a 'semantic_models' list")
        draft = SemanticDraft(source="metricflow")
        primaries: dict[str, tuple[str, str]] = {}  # entity name -> (table, column)
        foreigns: list[tuple[str, str, str, str]] = []  # (model, table, column, entity)
        measures: set[str] = set()

        for model in document["semantic_models"]:
            if not isinstance(model, dict) or not model.get("name"):
                continue
            name = str(model["name"])
            table = _table_of(model)
            if table is None:
                draft.skipped.append(
                    Skipped(
                        item=f"semantic model {name}",
                        reason="it names no table (model or node_relation)",
                    )
                )
                continue
            drafted = DraftTable(
                name=table,
                description=str(model.get("description") or "").strip(),
                synonyms=[name] if name != table else [],
            )
            for entity in model.get("entities") or []:
                if not isinstance(entity, dict):
                    continue
                kind = str(entity.get("type", "")).lower()
                column = _column(entity)
                label = f"entity {name}.{entity.get('name')}"
                if column is None:
                    draft.skipped.append(
                        Skipped(item=label, reason="its key is an expression, not a column")
                    )
                elif kind == "primary":
                    primaries[str(entity.get("name"))] = (table, column)
                elif kind == "foreign":
                    foreigns.append((name, table, column, str(entity.get("name"))))
                else:
                    draft.skipped.append(
                        Skipped(
                            item=label,
                            reason=f"a {kind or 'untyped'} entity has no place in a catalog",
                        )
                    )
            for dimension in model.get("dimensions") or []:
                if not isinstance(dimension, dict) or not dimension.get("name"):
                    continue
                column = _column(dimension)
                dname = str(dimension["name"])
                if column is None:
                    draft.skipped.append(
                        Skipped(
                            item=f"dimension {name}.{dname}",
                            reason="it is computed by an expression, not a column",
                        )
                    )
                    continue
                if (
                    dimension.get("type_params", {})
                    and str(dimension.get("type", "")).lower() == "time"
                ):
                    draft.skipped.append(
                        Skipped(
                            item=f"dimension {name}.{dname} granularity",
                            reason="a time grain is asked per question here",
                        )
                    )
                drafted.columns.append(
                    DraftColumn(
                        name=column,
                        description=str(dimension.get("description") or "").strip(),
                        synonyms=[dname] if dname.lower() != column.lower() else [],
                    )
                )
            draft.tables.append(drafted)
            for measure in model.get("measures") or []:
                if isinstance(measure, dict) and measure.get("name"):
                    self._measure(draft, name, table, measure)
                    measures.add(str(measure["name"]))

        for model, table, column, entity in foreigns:
            target = primaries.get(entity)
            if target is None:
                draft.skipped.append(
                    Skipped(
                        item=f"entity {model}.{entity}",
                        reason="no semantic model has it as its primary entity",
                    )
                )
                continue
            draft.relationships.append(
                DraftRelationship(
                    table=table, column=column, target_table=target[0], target_column=target[1]
                )
            )

        for metric in document.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            label = f"metric {metric.get('name', '?')}"
            kind = str(metric.get("type", "simple")).lower()
            if kind != "simple":
                draft.skipped.append(
                    Skipped(
                        item=label,
                        reason=f"a {kind} metric is a calculation over measures; a measure here is one aggregate",
                    )
                )
            elif metric.get("filter"):
                draft.skipped.append(
                    Skipped(
                        item=label,
                        reason="a metric with a filter: certify a measure of the condition instead",
                    )
                )
            else:
                params = metric.get("type_params") or {}
                measure = params.get("measure")
                measure_name = measure.get("name") if isinstance(measure, dict) else measure
                if measure_name not in measures:
                    draft.skipped.append(Skipped(item=label, reason="its measure was not read"))
        return draft

    @staticmethod
    def _measure(draft: SemanticDraft, model: str, table: str, measure: dict[str, Any]) -> None:
        name = str(measure["name"])
        label = f"measure {model}.{name}"
        agg = str(measure.get("agg", "")).lower()
        if measure.get("non_additive_dimension"):
            draft.skipped.append(
                Skipped(
                    item=label,
                    reason="a non-additive measure (a balance at a date) is not one aggregate",
                )
            )
            return
        expression = str(measure.get("expr") or name).strip()
        if agg == "sum_boolean":
            agg, expression = "sum", f"CASE WHEN {expression} THEN 1 ELSE 0 END"
        aggregation = _AGGREGATIONS.get(agg)
        if aggregation is None:
            draft.skipped.append(
                Skipped(item=label, reason=f"'{agg or 'no'}' aggregation has no place here")
            )
            return
        draft.measures.append(
            DraftMeasure(
                table=table,
                name=name,
                expression=expression,
                aggregation=aggregation,  # type: ignore[arg-type]
                description=str(measure.get("description") or "").strip(),
            )
        )
