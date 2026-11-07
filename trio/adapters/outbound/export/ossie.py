"""Apache Ossie semantic model export (``format=ossie``).

Ossie (formerly Open Semantic Interchange) is the open YAML standard BI and AI
tools exchange semantic models in: https://ossie.apache.org/. This writes the
core document of spec version ``0.2.0.dev0`` (``core-spec/ossie-schema.json``,
which the tests validate against):

- a **dataset** per table, with its primary key and description;
- a **field** per column: the column itself, its logical type, its
  description, a time dimension for dates, and the term's words for it;
- a **metric** per certified measure, as ``SUM`` of its expression;
- a **relationship** per declared foreign key and per relationship the
  approved catalog verified against the data;
- the catalog's notes and glossary as the model's ``ai_context``.

Only what was reviewed speaks for the data: terms, measures, verified
relationships, notes and the glossary come from an approved catalog; a draft
exports its structure and descriptions alone. A dataset's ``source`` is the
table's name, unqualified, because the catalog does not record the schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from trio.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    guidance_of,
    relationship_edges,
)
from trio.domain.gateway.sql_types import type_kind
from trio.ports.exporter import CatalogExporter

OSSIE_VERSION = "0.2.0.dev0"

_DATATYPES = {
    "int": "Integer",
    "bool": "Boolean",
    "date": "Date",
    "time": "Time",
    "str": "String",
    "uuid": "String",
}


def _datatype(column: ColumnCatalogEntry) -> str | None:
    """The Ossie logical type, or None (omitted) where it cannot be told."""
    kind = type_kind(column.data_type, None, column.full_type)
    written = f"{column.data_type} {column.full_type or ''}".lower()
    if kind == "float":
        return "Decimal" if "numeric" in written or "decimal" in written else "Float"
    if kind == "datetime":
        zoned = "time zone" in written and "without" not in written
        return "DateTimeTz" if zoned or "timestamptz" in written else "DateTime"
    if kind in ("json", "bytes", "list"):
        return "Opaque"
    return _DATATYPES.get(kind)


def _text(value: Any, lang: str) -> str:
    return (value.get(lang) or "") if value is not None else ""


def to_ossie(catalog: DatabaseCatalog, lang: str = "en") -> dict[str, Any]:
    """The catalog as an Ossie semantic model document."""
    approved = catalog.status == CatalogStatus.approved
    glossary, notes = guidance_of(catalog)
    model: dict[str, Any] = {"version": OSSIE_VERSION, "name": catalog.database_name}
    if description := _text(catalog.description, lang):
        model["description"] = description
    instructions = list(notes)
    if glossary:
        instructions.append(
            "Abbreviations: " + "; ".join(f"{s} = {long}" for s, long in glossary.items())
        )
    if instructions:
        model["ai_context"] = {"instructions": "\n".join(instructions)}

    datasets = []
    for tname, table in catalog.tables.items():
        term = catalog.term_for(tname) if approved else None
        dataset: dict[str, Any] = {"name": tname, "source": tname}
        keys = [c.name for c in table.columns if c.is_primary_key] or (
            [table.primary_key] if isinstance(table.primary_key, str) and table.primary_key else []
        )
        if keys:
            dataset["primary_key"] = keys
        if description := _text(table.description, lang):
            dataset["description"] = description
        synonyms = list(
            dict.fromkeys(
                [*table.human_name.texts.values(), *([term.name, *term.synonyms] if term else [])]
            )
        )
        if synonyms:
            dataset["ai_context"] = {"synonyms": synonyms}
        fields = []
        for column in table.columns:
            field: dict[str, Any] = {"name": column.name, "expression": {"sql": column.name}}
            datatype = _datatype(column)
            if datatype:
                field["datatype"] = datatype
            if datatype in ("Date", "Time", "DateTime", "DateTimeTz"):
                field["dimension"] = {"is_time": True}
            if description := _text(column.description, lang):
                field["description"] = description
            words = term.attribute(tname, column.name) if term else None
            if words and words.synonyms:
                field["ai_context"] = {"synonyms": list(words.synonyms)}
            fields.append(field)
        dataset["fields"] = fields
        metrics = [
            {
                "name": measure.name,
                "expression": {"sql": f"SUM({measure.expression})"},
                **({"description": text} if (text := _text(measure.description, lang)) else {}),
            }
            for measure in (term.certified_sql(tname) if term else [])
        ]
        if metrics:
            dataset["metrics"] = metrics
        datasets.append(dataset)
    model["datasets"] = datasets

    relationships: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    declared = [
        (tname, column.name, *column.references.split(".", 1))
        for tname, table in catalog.tables.items()
        for column in table.columns
        if column.is_foreign_key and column.references and "." in column.references
    ]
    verified = [
        (e.table, e.column, e.ref_table, e.ref_column)
        for e in relationship_edges(catalog.database_name, catalog)
    ]
    for many, key, one, ref in [*declared, *verified]:
        if many in catalog.tables and one in catalog.tables:
            relationships.setdefault(
                (many, key, one, ref),
                {
                    "name": f"{many}_{key}_to_{one}",
                    "from": many,
                    "to": one,
                    "from_columns": [key],
                    "to_columns": [ref],
                },
            )
    if relationships:
        model["relationships"] = list(relationships.values())
    return model


class OssieExporter(CatalogExporter):
    """Writes the catalog as an Apache Ossie semantic model (YAML)."""

    def export_string(self, catalog: DatabaseCatalog, lang: str | None = None) -> str:
        """Render the Ossie document."""
        return yaml.safe_dump(
            to_ossie(catalog, lang or (catalog.languages[0] if catalog.languages else "en")),
            allow_unicode=True,
            sort_keys=False,
        )

    def export(
        self, catalog: DatabaseCatalog, output_path: str | Path, lang: str | None = None
    ) -> Path:
        """Write the Ossie document to ``output_path`` and return the path."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.export_string(catalog, lang), encoding="utf-8")
        return path
