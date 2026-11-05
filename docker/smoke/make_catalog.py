"""Writes the release smoke's reviewed catalog into docker/smoke/catalogs/.

The smoke needs what a reviewer leaves behind: an approved catalog that labels
the personal column (so it is masked) and certified terms (so a question in
words can be answered). Producing that through the product would need a model
in the loop, so it is built here from the product's own models and the result
is checked in. Run again after changing the catalog format:

    uv run python docker/smoke/make_catalog.py
"""

from pathlib import Path

from trio.adapters.outbound.catalog_store.file_store import CatalogFileStore
from trio.domain.catalog import catalog_review
from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from trio.domain.catalog.terms import ColumnAttribute, Term

HERE = Path(__file__).parent


def column(name: str, kind: str, about: str, **flags) -> ColumnCatalogEntry:
    return ColumnCatalogEntry(
        name=name, data_type=kind, description=LocalizedText(texts={"en": about}), **flags
    )


def table(name: str, about: str, *columns: ColumnCatalogEntry) -> TableCatalogEntry:
    return TableCatalogEntry(
        table_name=name, description=LocalizedText(texts={"en": about}), columns=list(columns)
    )


def build() -> DatabaseCatalog:
    catalog = DatabaseCatalog(
        database_name="smoke",
        database_type="postgresql",
        tables={
            "subeler": table(
                "subeler",
                "Bank branches",
                column("sube_kodu", "character varying", "Branch code", is_primary_key=True),
                column("sehir", "character varying", "City of the branch"),
            ),
            "musteriler": table(
                "musteriler",
                "Bank customers",
                column("musteri_id", "integer", "Customer id", is_primary_key=True),
                column("ad", "character varying", "First name"),
                # The label the masking rule is keyed on.
                column("tc_kimlik", "character varying", "National id", semantic_type="tc_no"),
                column("kiraci", "character varying", "Tenant the row belongs to"),
                column("sube_kodu", "character varying", "Branch", is_foreign_key=True),
            ),
            "islemler": table(
                "islemler",
                "Transactions",
                column("islem_id", "integer", "Transaction id", is_primary_key=True),
                column("musteri_id", "integer", "Customer", is_foreign_key=True),
                column("kiraci", "character varying", "Tenant the row belongs to"),
                column("tutar", "numeric", "Amount"),
                column("durum", "character varying", "Status"),
            ),
        },
        terms={
            "Şube": Term(
                name="Şube",
                synonyms=["branch"],
                tables=["subeler"],
                column_attributes=[
                    ColumnAttribute(
                        table="subeler",
                        column="sehir",
                        synonyms=["şehir", "city"],
                        values={"Istanbul": [], "Ankara": []},
                    )
                ],
            ),
            "Müşteri": Term(name="Müşteri", synonyms=["customer"], tables=["musteriler"]),
            "İşlem": Term(
                name="İşlem",
                synonyms=["transaction"],
                tables=["islemler"],
                column_attributes=[
                    ColumnAttribute(table="islemler", column="tutar", synonyms=["amount"]),
                    ColumnAttribute(
                        table="islemler",
                        column="durum",
                        synonyms=["status"],
                        values={"basarili": ["successful"], "iptal": ["cancelled"]},
                    ),
                ],
            ),
        },
    )
    catalog_review.approve_catalog(catalog)
    catalog_review.certify_terms(catalog)
    return catalog


if __name__ == "__main__":
    print(CatalogFileStore(HERE / "catalogs").save(build()))
