"""The Apache Ossie export: valid against the spec's own schema, and only reviewed words."""

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from trio.adapters.outbound.export.ossie import OSSIE_VERSION, OssieExporter
from trio.domain.catalog import catalog_review
from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)
from trio.domain.catalog.terms import ColumnAttribute, SqlAttribute, Term

SCHEMA = json.loads(
    (Path(__file__).parent / "ossie-schema-0.2.0.dev0.json").read_text(encoding="utf-8")
)


def catalog() -> DatabaseCatalog:
    found = DatabaseCatalog(
        database_name="shop",
        description=LocalizedText(texts={"en": "A shop", "tr": "Bir dükkan"}),
        languages=["en", "tr"],
        tables={
            "customers": TableCatalogEntry(
                table_name="customers",
                human_name=LocalizedText(texts={"en": "Customers", "tr": "Müşteriler"}),
                columns=[
                    ColumnCatalogEntry(name="id", data_type="integer", is_primary_key=True),
                    ColumnCatalogEntry(name="segment", data_type="varchar"),
                ],
                relationships=[
                    RelationshipInfo(
                        source_column="id",
                        target_table="orders",
                        target_column="customer_id",
                        relationship_type="one-to-many",
                        verified=True,
                    )
                ],
            ),
            "orders": TableCatalogEntry(
                table_name="orders",
                description=LocalizedText(texts={"en": "Orders placed"}),
                columns=[
                    ColumnCatalogEntry(name="id", data_type="bigint", is_primary_key=True),
                    ColumnCatalogEntry(name="customer_id", data_type="integer"),
                    ColumnCatalogEntry(
                        name="total",
                        data_type="numeric",
                        full_type="numeric(10,2)",
                        description=LocalizedText(texts={"en": "Order total"}),
                    ),
                    ColumnCatalogEntry(name="placed_at", data_type="timestamp with time zone"),
                    ColumnCatalogEntry(name="paid", data_type="boolean"),
                    ColumnCatalogEntry(name="extra", data_type="jsonb"),
                ],
            ),
        },
        terms={
            "Order": Term(
                name="Order",
                synonyms=["sipariş"],
                tables=["orders"],
                column_attributes=[
                    ColumnAttribute(table="orders", column="total", synonyms=["tutar"])
                ],
                sql_attributes=[
                    SqlAttribute(
                        name="paid_total",
                        table="orders",
                        expression="CASE WHEN paid THEN total ELSE 0 END",
                        description=LocalizedText(texts={"en": "What was paid"}),
                        certified=True,
                    ),
                    SqlAttribute(name="draft_one", table="orders", expression="total * 2"),
                ],
            )
        },
        glossary={"SP": "sipariş"},
        notes=["Totals are in TRY."],
    )
    return found


def export(found: DatabaseCatalog, lang: str | None = "en") -> dict:
    document = yaml.safe_load(OssieExporter().export_string(found, lang))
    jsonschema.validate(document, SCHEMA)
    return document


def test_an_approved_catalog_exports_its_reviewed_model():
    found = catalog()
    catalog_review.approve_catalog(found)
    catalog_review.certify_terms(found)
    model = export(found)
    assert model["version"] == OSSIE_VERSION == SCHEMA["properties"]["version"]["const"]
    assert model["name"] == "shop" and model["description"] == "A shop"
    assert model["ai_context"]["instructions"] == "Totals are in TRY.\nAbbreviations: SP = sipariş"
    orders = next(d for d in model["datasets"] if d["name"] == "orders")
    assert orders["primary_key"] == ["id"] and orders["ai_context"]["synonyms"] == [
        "Order",
        "sipariş",
    ]
    fields = {f["name"]: f for f in orders["fields"]}
    assert fields["total"]["datatype"] == "Decimal"
    assert fields["total"]["ai_context"] == {"synonyms": ["tutar"]}
    assert fields["placed_at"]["datatype"] == "DateTimeTz"
    assert fields["placed_at"]["dimension"] == {"is_time": True}
    assert fields["paid"]["datatype"] == "Boolean" and fields["extra"]["datatype"] == "Opaque"
    # Certified measures only, summed.
    assert orders["metrics"] == [
        {
            "name": "paid_total",
            "expression": {"sql": "SUM(CASE WHEN paid THEN total ELSE 0 END)"},
            "description": "What was paid",
        }
    ]
    # The verified one-to-many, written from the many side.
    assert model["relationships"] == [
        {
            "name": "orders_customer_id_to_customers",
            "from": "orders",
            "to": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["id"],
        }
    ]


def test_a_draft_exports_its_structure_alone():
    model = export(catalog())
    assert "ai_context" not in model and "relationships" not in model
    orders = next(d for d in model["datasets"] if d["name"] == "orders")
    assert "metrics" not in orders and "ai_context" not in orders
    customers = next(d for d in model["datasets"] if d["name"] == "customers")
    # Its own names are words a reviewer did not have to certify.
    assert customers["ai_context"] == {"synonyms": ["Customers", "Müşteriler"]}


@pytest.mark.parametrize("lang", ["tr", None])
def test_descriptions_in_the_language_asked_or_the_catalogs_first(lang):
    assert export(catalog(), lang)["description"] == ("Bir dükkan" if lang else "A shop")


def test_a_declared_foreign_key_is_a_relationship_without_review():
    found = catalog()
    found.tables["customers"].relationships = []
    orders = found.tables["orders"]
    key = orders.get_column("customer_id")
    key.is_foreign_key, key.references = True, "customers.id"
    assert export(found)["relationships"][0]["from_columns"] == ["customer_id"]
