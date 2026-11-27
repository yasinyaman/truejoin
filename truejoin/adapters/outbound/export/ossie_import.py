"""Read an Apache Ossie semantic model (``core-spec/ossie-schema.json``, 0.2) into a draft.

The inverse of ``ossie.py``: a dataset is a table (the last part of its
``source``), a field whose SQL is a bare column is that column (its name, when
it differs, a word for it), a dataset metric that is one aggregate of one
expression is a measure, and a single-column relationship is a reference.
What a catalog has no place for is skipped with the reason:

- a field computed by an expression, a dataset whose source is a query;
- a metric of the whole model (it spans datasets), a ratio or any other
  calculation over aggregates, a filtered or windowed one, ``COUNT(*)``;
- a composite relationship;
- SQL in a dialect other than Ossie's own or ANSI;
- ``custom_extensions``, which are another tool's own data.

Metric SQL is read as ANSI and written as DuckDB, where measures run.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from truejoin.adapters.outbound.sqlglot_expressions import SqlglotExpressionMiner
from truejoin.domain.catalog.semantic_draft import (
    DraftColumn,
    DraftMeasure,
    DraftRelationship,
    DraftTable,
    SemanticDraft,
    Skipped,
)
from truejoin.domain.catalog.terms import MeasureError

#: Dialects whose SQL is read as written.
_READABLE = {"OSSIE_SQL_2026", "ANSI_SQL", "ANSI"}
_PLAIN_NAME = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_$#]*)"?$')


def load_document(text: str) -> Any:
    """YAML or JSON (JSON is YAML, but a JSON error reads better first)."""
    try:
        return json.loads(text)
    except ValueError:
        return yaml.safe_load(text)


def _text(value: Any) -> str:
    """A description: a string, or the first text of a mapping of languages."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return next((str(v).strip() for v in value.values() if v), "")
    return ""


def _synonyms(ai_context: Any) -> list[str]:
    if isinstance(ai_context, dict):
        words = ai_context.get("synonyms") or []
        return [str(w) for w in words if isinstance(w, str | int)]
    return []


def _sql(expression: Any) -> tuple[str | None, str]:
    """(SQL to read, or None) and why not."""
    if not isinstance(expression, dict):
        return None, "it has no expression"
    if isinstance(expression.get("sql"), str):
        return expression["sql"], ""
    for entry in expression.get("dialects") or []:
        if isinstance(entry, dict) and str(entry.get("dialect", "")).upper() in _READABLE:
            return str(entry.get("expression", "")), ""
    return None, "its SQL is only in a dialect other than Ossie's or ANSI"


def _table_of(source: Any) -> str | None:
    """The table a dataset reads: the last part of ``db.schema.table``; None for a query."""
    if not isinstance(source, str) or not source.strip():
        return None
    text = source.strip()
    if any(ch.isspace() for ch in text) or "(" in text:
        return None
    last = text.split(".")[-1].strip('"`[]')
    return last or None


class OssieReader:
    """An Ossie document as a draft for ``import_semantic_model``."""

    def __init__(self) -> None:
        self._split = SqlglotExpressionMiner().split_metric

    def read(self, text: str) -> SemanticDraft:  # noqa: C901, PLR0912
        document = load_document(text)
        if not isinstance(document, dict) or not isinstance(document.get("datasets"), list):
            raise ValueError("An Ossie model is an object with a 'datasets' list")
        draft = SemanticDraft(
            source="ossie",
            name=str(document.get("name") or ""),
            description=_text(document.get("description")),
        )
        tables: dict[str, str] = {}
        for dataset in document["datasets"]:
            if not isinstance(dataset, dict) or not dataset.get("name"):
                continue
            name = str(dataset["name"])
            table = _table_of(dataset.get("source") or name)
            if table is None:
                draft.skipped.append(
                    Skipped(item=f"dataset {name}", reason="its source is a query, not a table")
                )
                continue
            tables[name] = table
            drafted = DraftTable(
                name=table,
                description=_text(dataset.get("description")),
                synonyms=[w for w in [name, *_synonyms(dataset.get("ai_context"))] if w != table],
            )
            fields: dict[str, str] = {}
            for field in dataset.get("fields") or []:
                if not isinstance(field, dict) or not field.get("name"):
                    continue
                fname = str(field["name"])
                sql, why = _sql(field.get("expression") or {"sql": fname})
                column = _PLAIN_NAME.match(sql.strip()) if sql else None
                if column is None:
                    draft.skipped.append(
                        Skipped(
                            item=f"field {name}.{fname}",
                            reason=why or "it is computed by an expression, not a column",
                        )
                    )
                    continue
                fields[fname] = column.group(1)
                drafted.columns.append(
                    DraftColumn(
                        name=column.group(1),
                        description=_text(field.get("description")),
                        synonyms=[
                            w
                            for w in [fname, *_synonyms(field.get("ai_context"))]
                            if w.lower() != column.group(1).lower()
                        ],
                    )
                )
            draft.tables.append(drafted)
            for metric in dataset.get("metrics") or []:
                self._metric(draft, name, table, metric)

        for metric in document.get("metrics") or []:
            if isinstance(metric, dict):
                draft.skipped.append(
                    Skipped(
                        item=f"metric {metric.get('name', '?')}",
                        reason="a metric of the whole model spans datasets; a measure is over one table",
                    )
                )
        for relation in document.get("relationships") or []:
            self._relationship(draft, tables, relation)
        context = document.get("ai_context")
        if isinstance(context, dict) and isinstance(context.get("instructions"), str):
            draft.notes.append(context["instructions"].strip())
        elif isinstance(context, str) and context.strip():
            draft.notes.append(context.strip())
        if document.get("custom_extensions"):
            draft.skipped.append(
                Skipped(item="custom_extensions", reason="another tool's own data")
            )
        return draft

    def _metric(self, draft: SemanticDraft, dataset: str, table: str, metric: Any) -> None:
        if not isinstance(metric, dict) or not metric.get("name"):
            return
        label = f"metric {dataset}.{metric['name']}"
        sql, why = _sql(metric.get("expression"))
        if sql is None:
            draft.skipped.append(Skipped(item=label, reason=why))
            return
        try:
            aggregation, inner = self._split(sql)
        except MeasureError as e:
            draft.skipped.append(Skipped(item=label, reason=str(e)))
            return
        draft.measures.append(
            DraftMeasure(
                table=table,
                name=str(metric["name"]),
                expression=inner,
                aggregation=aggregation,  # type: ignore[arg-type]
                description=_text(metric.get("description")),
            )
        )

    @staticmethod
    def _relationship(draft: SemanticDraft, tables: dict[str, str], relation: Any) -> None:
        if not isinstance(relation, dict):
            return
        label = f"relationship {relation.get('name') or relation.get('from')}"
        from_columns = relation.get("from_columns") or []
        to_columns = relation.get("to_columns") or []
        if len(from_columns) != 1 or len(to_columns) != 1:
            draft.skipped.append(
                Skipped(item=label, reason="a composite key: one column each side only")
            )
            return
        source, target = tables.get(str(relation.get("from"))), tables.get(str(relation.get("to")))
        if source is None or target is None:
            draft.skipped.append(Skipped(item=label, reason="it names a dataset that was not read"))
            return
        draft.relationships.append(
            DraftRelationship(
                table=source,
                column=str(from_columns[0]),
                target_table=target,
                target_column=str(to_columns[0]),
            )
        )
