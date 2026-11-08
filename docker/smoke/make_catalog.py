"""Writes the release smoke's reviewed catalog into docker/smoke/catalogs/.

The smoke needs what a reviewer leaves behind: an approved catalog that labels
the personal column (so it is masked), certified terms (so a question in words
can be answered) and saved analyses: one certified, one certified that quotes a
value (never offered to a restricted caller) and one still a draft. Producing that through the product would need a model
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
from trio.domain.catalog.examples import QueryExample
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
    per_city = (
        "SELECT s.sehir, SUM(i.tutar) AS toplam FROM smoke.islemler i "
        "JOIN smoke.musteriler m ON i.musteri_id = m.musteri_id "
        "JOIN smoke.subeler s ON m.sube_kodu = s.sube_kodu"
    )
    tables = ["smoke.islemler", "smoke.musteriler", "smoke.subeler"]

    def example(name: str, question: str, sql: str, certified: bool) -> QueryExample:
        return QueryExample(
            name=name,
            question=LocalizedText(texts={"tr": question}),
            sql=sql,
            tables=tables,
            certified=certified,
        )

    catalog.examples = {
        e.name: e
        for e in [
            example("per_city", "Şehir bazında toplam tutar", f"{per_city} GROUP BY s.sehir", True),
            example(
                "successful_per_city",
                "Şehir bazında başarılı işlem tutarı",
                f"{per_city} WHERE i.durum = 'basarili' GROUP BY s.sehir",
                True,
            ),
            example(
                "average_per_city",
                "Şehir bazında ortalama tutar",
                f"{per_city.replace('SUM', 'AVG')} GROUP BY s.sehir",
                False,
            ),
        ]
    }
    catalog_review.approve_catalog(catalog)
    catalog_review.certify_terms(catalog)
    return catalog


if __name__ == "__main__":
    print(CatalogFileStore(HERE / "catalogs").save(build()))
