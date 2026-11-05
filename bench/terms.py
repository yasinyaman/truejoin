"""The fixture's catalog, and the terms a model writes for it.

The seventh arm answers from the catalog's certified terms, so the benchmark
needs a catalog: the fixture's structure, the same descriptions the catalog
arm's prompt carries, and a few stored values per column, as catalog analysis
would have kept them. ``python -m bench.terms <model>`` runs the product's own
term extraction over it and writes the result to ``bench/catalogs/``, where it
is checked in for review like the generated lexicon before it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from bench.fixture import (
    ISLEMLER,
    ISLEMLER_COLUMNS,
    MUSTERILER,
    MUSTERILER_COLUMNS,
    SUBELER,
    SUBELER_COLUMNS,
)
from bench.fusion_source import FOREIGN_KEYS, PRIMARY_KEYS, SCHEMAS
from bench.models import OllamaModel
from trio.application.catalog.term_extraction import extract_terms
from trio.domain.catalog import catalog_review
from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)

CATALOG_PATH = "bench/catalogs/banka.json"

ROWS = {
    "subeler": (SUBELER, SUBELER_COLUMNS),
    "musteriler": (MUSTERILER, MUSTERILER_COLUMNS),
    "islemler": (ISLEMLER, ISLEMLER_COLUMNS),
}

#: The words of the catalog arm's context (``run_duckdb.CATALOG_CONTEXT``), so
#: the two arms are told the same things about the schema.
DESCRIPTIONS: dict[str, tuple[str, dict[str, str]]] = {
    "subeler": (
        "bank branches, one row per branch",
        {
            "sube_kodu": "branch code, e.g. IST01",
            "sehir": "city the branch is in",
            "bolge": "region the branch belongs to",
        },
    ),
    "musteriler": (
        "bank customers, one row per customer",
        {
            "musteri_id": "customer id",
            "ad_soyad": "customer full name",
            "eposta": "email address, NULL when not provided",
            "sube_kodu": "branch code, e.g. IST01",
            "segment": "customer segment",
            "acilis_tarihi": "account opening date",
        },
    ),
    "islemler": (
        "transactions, one row per transaction",
        {
            "islem_id": "transaction id",
            "musteri_id": "the customer who made the transaction",
            "tutar": "transaction amount (the measure to sum/average)",
            "para_birimi": "currency",
            "islem_turu": "transaction type",
            "durum": "status",
            "islem_tarihi": "transaction date",
            "kanal": "channel",
        },
    ),
}

#: Columns catalog analysis keeps no samples of, because they look personal.
PERSONAL = frozenset({"ad_soyad", "eposta"})

#: As many distinct values per column as the catalog keeps.
SAMPLES = 5


def fixture_catalog() -> DatabaseCatalog:
    """The catalog analysis would produce for the fixture, before any term."""
    tables = {}
    for name, (rows, columns) in ROWS.items():
        description, column_descriptions = DESCRIPTIONS[name]
        keys = {key.column: key for key in FOREIGN_KEYS.get(name, ())}
        entries = []
        for index, (column, kind) in enumerate(SCHEMAS[name]):
            assert column == columns[index]
            seen = dict.fromkeys(row[index] for row in rows if row[index] is not None)
            key = keys.get(column)
            entries.append(
                ColumnCatalogEntry(
                    name=column,
                    data_type=kind.lower(),
                    description=LocalizedText(texts={"en": column_descriptions[column]}),
                    is_primary_key=PRIMARY_KEYS[name] == column,
                    is_foreign_key=key is not None,
                    references=f"{key.references_table}.{key.references_column}" if key else None,
                    sample_values=[] if column in PERSONAL else list(seen)[:SAMPLES],
                )
            )
        tables[name] = TableCatalogEntry(
            table_name=name,
            description=LocalizedText(texts={"en": description}),
            columns=entries,
            primary_key=PRIMARY_KEYS[name],
            row_count=len(rows),
        )
    return DatabaseCatalog(database_name="banka", database_type="duckdb", tables=tables)


class OllamaText:
    """A local model behind the product's ``TextGenerator`` port."""

    def __init__(self, model: str) -> None:
        self._model = OllamaModel(model=model, json_only=True, max_tokens=1200)

    async def generate(self, prompt: str, system_prompt: str | None = None, **_: Any) -> str:
        return self._model.complete(f"{system_prompt}\n\n{prompt}" if system_prompt else prompt)

    async def generate_json(self, prompt: str, system_prompt: str | None = None) -> str:
        return await self.generate(prompt, system_prompt)

    async def close(self) -> None:
        return None


def generate(model: str, path: str | Path = CATALOG_PATH) -> Path:
    """Write the fixture's catalog with the terms ``model`` gave it, uncertified."""
    catalog = fixture_catalog()
    catalog.terms = asyncio.run(extract_terms(catalog, OllamaText(model), ("tr", "en")))
    # The product keeps a table's bare names when the model fails on it. Here
    # that would be a vocabulary nobody wrote, checked in as if someone had.
    silent = [
        term.name
        for term in catalog.terms.values()
        if not term.synonyms and not any(a.synonyms for a in term.column_attributes)
    ]
    if silent:
        raise SystemExit(f"{model} wrote no words for: {', '.join(silent)}. Is it running?")
    catalog.llm_provider, catalog.llm_model = "ollama", model
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        catalog.model_dump_json(indent=2, exclude={"generated_at", "updated_at"}) + "\n", "utf-8"
    )
    return target


def load_certified(path: str | Path = CATALOG_PATH) -> DatabaseCatalog:
    """The checked-in catalog, approved and with its terms certified.

    Checking the file in is the review; this is the reviewer's signature.
    """
    catalog = DatabaseCatalog.model_validate_json(Path(path).read_text("utf-8"))
    catalog_review.approve_catalog(catalog)
    catalog_review.certify_terms(catalog)
    return catalog


if __name__ == "__main__":
    print(generate(*sys.argv[1:2] or ["qwen2.5-coder:7b"]))
