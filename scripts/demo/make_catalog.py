"""Writes the sample setup's reviewed catalogs into scripts/demo/catalogs/.

Two databases, so two catalogs: `smoke` (the PostgreSQL) is the release smoke's
own, built by docker/smoke/make_catalog.py and written here again so that one
folder seeds the setup; `crm` (the MySQL) is built below. Both are what a
reviewer leaves behind (approved, the personal columns labelled, terms
certified), made from the product's own models because making them through the
product would need a model in the loop. Run again after changing either:

    uv run python scripts/demo/make_catalog.py
"""

import runpy
from pathlib import Path

from truejoin.adapters.outbound.catalog_store.file_store import CatalogFileStore
from truejoin.domain.catalog import catalog_review
from truejoin.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from truejoin.domain.catalog.examples import QueryExample
from truejoin.domain.catalog.terms import ColumnAttribute, Term

HERE = Path(__file__).parent
SMOKE = HERE.parent.parent / "docker" / "smoke" / "make_catalog.py"

#: The report the setup is there to show: what each branch was set as a target
#: (the CRM's MySQL) beside what its customers transacted (the bank's PostgreSQL).
#: No constant in either statement, not even a rounding: a saved analysis that
#: quotes a value is never offered to a caller held to row rules or masks.
TARGET_VS_ACTUAL = (
    "SELECT s.sehir, h.hedef_tutar AS hedef, SUM(i.tutar) AS gerceklesen, "
    "SUM(i.tutar) / h.hedef_tutar AS gerceklesme_orani "
    "FROM smoke.islemler i "
    "JOIN smoke.musteriler m ON i.musteri_id = m.musteri_id "
    "JOIN smoke.subeler s ON m.sube_kodu = s.sube_kodu "
    "JOIN crm.sube_hedefleri h ON h.sube_kodu = s.sube_kodu "
    "GROUP BY s.sehir, h.hedef_tutar ORDER BY s.sehir"
)
#: The same join the other way round: a column of the MySQL breaks down a sum of the PostgreSQL.
AMOUNT_PER_SEGMENT = (
    "SELECT g.segment, COUNT(DISTINCT g.musteri_id) AS musteri, SUM(i.tutar) AS toplam "
    "FROM smoke.islemler i "
    "JOIN crm.musteri_segmentleri g ON g.musteri_id = i.musteri_id "
    "GROUP BY g.segment ORDER BY g.segment"
)


def column(name: str, kind: str, about: str, **flags) -> ColumnCatalogEntry:
    return ColumnCatalogEntry(
        name=name, data_type=kind, description=LocalizedText(texts={"en": about}), **flags
    )


def table(name: str, about: str, *columns: ColumnCatalogEntry) -> TableCatalogEntry:
    return TableCatalogEntry(
        table_name=name, description=LocalizedText(texts={"en": about}), columns=list(columns)
    )


def build_crm() -> DatabaseCatalog:
    catalog = DatabaseCatalog(
        database_name="crm",
        database_type="mysql",
        tables={
            "sube_hedefleri": table(
                "sube_hedefleri",
                "The amount each branch is set as a target for a year",
                column("hedef_id", "int", "Target id", is_primary_key=True),
                column("sube_kodu", "varchar", "Branch code, as the bank's own database writes it"),
                column("yil", "int", "The year the target is for"),
                column("hedef_tutar", "decimal", "Target amount"),
            ),
            "musteri_segmentleri": table(
                "musteri_segmentleri",
                "The segment the CRM puts each customer in, and how to reach them",
                column("kayit_id", "int", "Record id", is_primary_key=True),
                column("musteri_id", "int", "Customer id, as the bank's own database writes it"),
                column("kiraci", "varchar", "Tenant the row belongs to"),
                column("segment", "varchar", "Customer segment"),
                # The label the masking rule is keyed on.
                column("telefon", "varchar", "Phone number", semantic_type="phone"),
            ),
        },
        terms={
            "Hedef": Term(
                name="Hedef",
                synonyms=["target"],
                tables=["sube_hedefleri"],
                column_attributes=[
                    ColumnAttribute(
                        table="sube_hedefleri", column="hedef_tutar", synonyms=["target amount"]
                    ),
                    ColumnAttribute(table="sube_hedefleri", column="yil", synonyms=["year"]),
                ],
            ),
            "Segment": Term(
                name="Segment",
                synonyms=["customer segment"],
                tables=["musteri_segmentleri"],
                column_attributes=[
                    ColumnAttribute(
                        table="musteri_segmentleri",
                        column="segment",
                        synonyms=["segment"],
                        values={"bireysel": ["retail"], "kurumsal": ["corporate"]},
                    )
                ],
            ),
        },
    )
    catalog.examples = {
        "target_vs_actual_per_branch": QueryExample(
            name="target_vs_actual_per_branch",
            question=LocalizedText(
                texts={
                    "en": "Target and actual amount per branch city",
                    "tr": "Şube şehri bazında hedef ve gerçekleşen tutar",
                }
            ),
            sql=TARGET_VS_ACTUAL,
            tables=["smoke.islemler", "smoke.musteriler", "smoke.subeler", "crm.sube_hedefleri"],
            certified=True,
        ),
        "amount_per_segment": QueryExample(
            name="amount_per_segment",
            question=LocalizedText(
                texts={
                    "en": "Transaction amount per customer segment",
                    "tr": "Müşteri segmenti bazında işlem tutarı",
                }
            ),
            sql=AMOUNT_PER_SEGMENT,
            tables=["smoke.islemler", "crm.musteri_segmentleri"],
            certified=True,
        ),
    }
    catalog_review.approve_catalog(catalog)
    catalog_review.certify_terms(catalog)
    return catalog


if __name__ == "__main__":
    store = CatalogFileStore(HERE / "catalogs")
    print(store.save(runpy.run_path(str(SMOKE))["build"]()))
    print(store.save(build_crm()))
