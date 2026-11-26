"""Ossie and MetricFlow models read into drafts, and merged into a catalog as drafts."""

from __future__ import annotations

import pytest
import yaml

from truejoin.adapters.outbound.catalog_store.file_store import CatalogFileStore
from truejoin.adapters.outbound.export.metricflow_import import MetricFlowReader
from truejoin.adapters.outbound.export.ossie import OssieExporter
from truejoin.adapters.outbound.export.ossie_import import OssieReader
from truejoin.adapters.outbound.sqlglot_expressions import SqlglotExpressionMiner
from truejoin.application.catalog.catalog_review import CatalogReviewService
from truejoin.domain.catalog import catalog_review
from truejoin.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    RelationshipInfo,
    TableCatalogEntry,
)
from truejoin.domain.catalog.localized import LocalizedText
from truejoin.domain.catalog.semantic_draft import DraftMeasure, SemanticDraft
from truejoin.domain.catalog.terms import SqlAttribute, Term


def shop() -> DatabaseCatalog:
    """Orders and customers as an Oracle stores them: upper case."""
    return DatabaseCatalog(
        database_name="shop",
        tables={
            "ORDERS": TableCatalogEntry(
                table_name="ORDERS",
                columns=[
                    ColumnCatalogEntry(name="ID", data_type="number(9)", is_primary_key=True),
                    ColumnCatalogEntry(name="CUSTOMER_ID", data_type="number(9)"),
                    ColumnCatalogEntry(name="TOTAL", data_type="number(10,2)"),
                    ColumnCatalogEntry(
                        name="STATUS",
                        data_type="varchar2(8)",
                        description=LocalizedText(texts={"en": "Kept"}),
                    ),
                    ColumnCatalogEntry(name="PAID", data_type="number(1)"),
                ],
            ),
            "CUSTOMERS": TableCatalogEntry(
                table_name="CUSTOMERS",
                columns=[
                    ColumnCatalogEntry(name="ID", data_type="number(9)", is_primary_key=True),
                    ColumnCatalogEntry(name="SEGMENT", data_type="varchar2(8)"),
                ],
            ),
        },
    )


METRICFLOW = """
semantic_models:
  - name: orders
    description: One row per order
    model: ref('orders')
    entities:
      - {name: order, type: primary, expr: id}
      - {name: customer, type: foreign, expr: customer_id}
      - {name: order_code, type: natural, expr: code}
    dimensions:
      - {name: order_status, type: categorical, expr: status, description: Where it stands}
      - {name: placed_at, type: time, type_params: {time_granularity: day}}
      - {name: big, type: categorical, expr: "total > 100"}
    measures:
      - {name: revenue, agg: sum, expr: total, description: What was paid}
      - {name: basket, agg: average, expr: total}
      - {name: buyers, agg: count_distinct, expr: customer_id}
      - {name: paid_orders, agg: sum_boolean, expr: paid = 1}
      - {name: p90, agg: percentile, expr: total}
      - {name: balance, agg: sum, expr: total, non_additive_dimension: {name: placed_at}}
  - name: customers
    node_relation: {alias: customers, schema_name: public}
    entities:
      - {name: customer, type: primary, expr: id}
    dimensions:
      - {name: segment, type: categorical}
metrics:
  - {name: revenue, type: simple, type_params: {measure: revenue}}
  - {name: paid_revenue, type: simple, type_params: {measure: revenue}, filter: "{{ Dimension('order__paid') }} = 1"}
  - {name: margin, type: ratio, type_params: {numerator: revenue, denominator: revenue}}
"""


class TestMetricFlow:
    def test_models_entities_dimensions_and_measures(self):
        draft = MetricFlowReader().read(METRICFLOW)
        orders = next(t for t in draft.tables if t.name == "orders")
        assert orders.description == "One row per order"
        status = next(c for c in orders.columns if c.name == "status")
        assert status.synonyms == ["order_status"] and status.description == "Where it stands"
        assert [
            (r.table, r.column, r.target_table, r.target_column) for r in draft.relationships
        ] == [("orders", "customer_id", "customers", "id")]
        measures = {m.name: (m.aggregation, m.expression) for m in draft.measures}
        assert measures == {
            "revenue": ("sum", "total"),
            "basket": ("avg", "total"),
            "buyers": ("count_distinct", "customer_id"),
            "paid_orders": ("sum", "CASE WHEN paid = 1 THEN 1 ELSE 0 END"),
        }

    def test_what_has_no_place_is_skipped_with_its_reason(self):
        skipped = {s.item: s.reason for s in MetricFlowReader().read(METRICFLOW).skipped}
        assert "natural" in skipped["entity orders.order_code"]
        assert "expression" in skipped["dimension orders.big"]
        assert "granularity" in " ".join(skipped)
        assert "percentile" in skipped["measure orders.p90"]
        assert "non-additive" in skipped["measure orders.balance"]
        assert "filter" in skipped["metric paid_revenue"]
        assert "ratio" in skipped["metric margin"]
        assert "metric revenue" not in skipped

    def test_a_file_without_semantic_models_is_refused(self):
        with pytest.raises(ValueError, match="semantic_models"):
            MetricFlowReader().read("metrics: []")


def certified_catalog() -> DatabaseCatalog:
    found = shop()
    found.terms["Order"] = Term(
        name="Order",
        tables=["ORDERS"],
        sql_attributes=[
            SqlAttribute(name="revenue", table="ORDERS", expression="TOTAL", certified=True),
            SqlAttribute(name="basket", table="ORDERS", expression="TOTAL", aggregation="avg", certified=True),
            SqlAttribute(
                name="buyers", table="ORDERS", expression="CUSTOMER_ID", aggregation="count_distinct",
                certified=True,
            ),
        ],
    )  # fmt: skip
    found.tables["ORDERS"].relationships.append(
        RelationshipInfo(
            source_column="CUSTOMER_ID", target_table="CUSTOMERS", target_column="ID", verified=True
        )
    )
    catalog_review.approve_catalog(found)
    catalog_review.certify_terms(found)
    return found


class TestOssie:
    def test_a_truejoin_export_reads_back_as_the_same_measures(self):
        exported = OssieExporter().export_string(certified_catalog(), "en")
        draft = OssieReader().read(exported)
        measures = {m.name: (m.table, m.aggregation, m.expression) for m in draft.measures}
        assert measures == {
            "revenue": ("ORDERS", "sum", "TOTAL"),
            "basket": ("ORDERS", "avg", "TOTAL"),
            "buyers": ("ORDERS", "count_distinct", "CUSTOMER_ID"),
        }
        assert [(r.table, r.column, r.target_table) for r in draft.relationships] == [
            ("ORDERS", "CUSTOMER_ID", "CUSTOMERS")
        ]

    def test_a_dialect_it_cannot_read_and_a_composite_key_are_skipped(self):
        document = {
            "version": "0.2.0.dev0",
            "name": "x",
            "datasets": [
                {
                    "name": "orders",
                    "source": "db.orders",
                    "fields": [{"name": "id", "expression": {"sql": "id"}}],
                    "metrics": [
                        {
                            "name": "snow",
                            "expression": {
                                "dialects": [{"dialect": "SNOWFLAKE", "expression": "SUM(x)"}]
                            },
                        }
                    ],
                },
                {"name": "q", "source": "SELECT 1 AS a"},
            ],
            "relationships": [
                {
                    "name": "pair",
                    "from": "orders",
                    "to": "orders",
                    "from_columns": ["a", "b"],
                    "to_columns": ["c", "d"],
                }
            ],
            "custom_extensions": [{"vendor": "x"}],
        }
        skipped = {s.item: s.reason for s in OssieReader().read(yaml.safe_dump(document)).skipped}
        assert "dialect" in skipped["metric orders.snow"]
        assert "query" in skipped["dataset q"]
        assert "composite" in skipped["relationship pair"]
        assert "custom_extensions" in skipped

    def test_a_document_without_datasets_is_refused(self):
        with pytest.raises(ValueError, match="datasets"):
            OssieReader().read("name: x")


class TestMerge:
    @pytest.fixture
    def review(self, tmp_path):
        return CatalogReviewService(CatalogFileStore(tmp_path))

    def test_names_meet_the_database_whatever_their_case_and_land_as_drafts(self, review):
        catalog = shop()
        catalog.status = CatalogStatus.approved
        report = review.import_semantic_model(
            catalog, MetricFlowReader().read(METRICFLOW), SqlglotExpressionMiner()
        )
        assert sorted(report.measures) == ["basket", "buyers", "paid_orders", "revenue"]
        stored = review.load("shop")
        assert stored.status == CatalogStatus.draft
        assert {stored.tables[t].review_status for t in report.tables} == {"pending"}
        term = next(t for t in stored.terms.values() if "ORDERS" in t.tables)
        assert term.certified is False
        by_name = {a.name: a for a in term.sql_attributes}
        assert by_name["basket"].aggregation == "avg" and by_name["basket"].expression == "TOTAL"
        assert not any(a.certified for a in term.sql_attributes)
        # A description fills an empty one only.
        columns = {c.name: c for c in stored.tables["ORDERS"].columns}
        assert columns["STATUS"].description.get("en") == "Kept"
        assert stored.tables["ORDERS"].description.get("en") == "One row per order"
        [joined] = stored.tables["ORDERS"].relationships
        assert (joined.source_column, joined.target_table, joined.verified) == (
            "CUSTOMER_ID",
            "CUSTOMERS",
            None,
        )

    def test_a_measure_the_rules_refuse_is_skipped_with_the_reason(self, review):
        draft = SemanticDraft(
            source="ossie",
            measures=[
                DraftMeasure(
                    table="orders", name="avg_status", expression="status", aggregation="avg"
                ),
                DraftMeasure(table="orders", name="ID", expression="total"),
                DraftMeasure(table="nowhere", name="x", expression="total"),
            ],
        )
        report = review.import_semantic_model(shop(), draft, SqlglotExpressionMiner())
        reasons = {s.item: s.reason for s in report.skipped}
        assert report.measures == []
        assert "cannot be averaged" in reasons["measure orders.avg_status"]
        assert "taken" in reasons["measure orders.ID"]
        assert "not here" in reasons["measure nowhere.x"]


def test_an_imported_catalogs_relationships_are_checked_again_here():
    catalog = certified_catalog()
    catalog_review.as_unreviewed(catalog)
    assert [r.verified for r in catalog.tables["ORDERS"].relationships] == [None]
