"""Tests for the pure review transitions."""

import pytest

from truejoin.domain.catalog import catalog_review as rules
from truejoin.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
    TableReviewStatus,
)
from truejoin.domain.gateway.errors import ColumnNotFoundInCatalogError, TableNotFoundInCatalogError


def _catalog() -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name="db",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "Users"}),
                columns=[ColumnCatalogEntry(name="email", data_type="varchar")],
                review_status=TableReviewStatus.approved,
            ),
            "orders": TableCatalogEntry(table_name="orders"),
        },
        status=CatalogStatus.approved,
    )


def test_mark_as_draft_resets_everything():
    c = rules.mark_as_draft(_catalog())
    assert c.status == CatalogStatus.draft
    assert {t.review_status for t in c.tables.values()} == {TableReviewStatus.pending}


def test_approve_table_and_catalog():
    c = rules.mark_as_draft(_catalog())
    rules.approve_table(c, "users")
    assert c.tables["users"].review_status == TableReviewStatus.approved
    assert c.tables["orders"].review_status == TableReviewStatus.pending
    rules.approve_catalog(c)
    assert c.status == CatalogStatus.approved
    assert c.tables["orders"].review_status == TableReviewStatus.approved
    with pytest.raises(TableNotFoundInCatalogError):
        rules.approve_table(c, "nope")


def test_approving_an_approved_table_again_changes_nothing():
    c = _catalog()
    before = c.updated_at
    rules.approve_table(c, "users")
    assert c.updated_at == before
    assert c.tables["users"].review_status == TableReviewStatus.approved


def test_update_fields_record_overrides():
    c = rules.mark_as_draft(_catalog())
    table = rules.update_table_fields(c, "users", {"description": "People", "tags": ["core"]}, "en")
    assert table.description.get("en") == "People"
    assert table.user_overrides == {"description": {"en": "People"}, "tags": ["core"]}
    assert table.review_status == TableReviewStatus.modified

    col = rules.update_column_fields(c, "users", "email", {"semantic_type": "email"})
    assert col.user_overrides == {"semantic_type": "email"}
    with pytest.raises(ColumnNotFoundInCatalogError):
        rules.update_column_fields(c, "users", "nope", {"tags": []})


def test_overrides_round_trip():
    edited = rules.mark_as_draft(_catalog())
    rules.update_table_fields(edited, "users", {"human_name": {"en": "Members"}})
    rules.update_column_fields(edited, "users", "email", {"tags": ["pii"]})
    overrides = rules.extract_overrides(edited)
    assert overrides == {
        "users": {"human_name": {"en": "Members"}, "columns": {"email": {"tags": ["pii"]}}}
    }

    fresh = rules.mark_as_draft(_catalog())
    fresh.tables.pop("orders")  # a table that disappeared is skipped silently
    rules.apply_overrides(fresh, {**overrides, "orders": {"tags": ["gone"]}})
    assert fresh.tables["users"].human_name.get("en") == "Members"
    assert fresh.tables["users"].review_status == TableReviewStatus.modified
    assert fresh.tables["users"].columns[0].tags == ["pii"]
    assert rules.extract_overrides(_catalog()) == {}


def test_an_edited_relationship_keeps_the_datas_verdict():
    from truejoin.domain.catalog.catalog import RelationshipInfo

    def relationships(*verdicts):
        return [
            RelationshipInfo(
                source_column=column, target_table=target, target_column="id", verified=verdict
            )
            for (column, target), verdict in zip(
                [("depot_code", "depots"), ("carrier_no", "carriers")], verdicts, strict=True
            )
        ]

    shown = [
        {"source_column": r.source_column, "target_table": r.target_table, "target_column": "id"}
        for r in relationships(None, None)
    ]
    c = _catalog()
    c.tables["orders"].relationships = relationships(True, False)
    rules.update_table_fields(c, "orders", {"relationships": shown})
    assert [r.verified for r in c.tables["orders"].relationships] == [True, False]

    fresh = _catalog()
    fresh.tables["orders"].relationships = relationships(True, False)
    rules.apply_overrides(fresh, rules.extract_overrides(c))
    assert [r.verified for r in fresh.tables["orders"].relationships] == [True, False]


def _generated() -> DatabaseCatalog:
    catalog = _catalog()
    table = catalog.tables["users"]
    table.generated_description = LocalizedText(texts={"en": "Users"})
    column = table.columns[0]
    column.description = LocalizedText(texts={"en": "Their email"})
    column.generated_description = LocalizedText(texts={"en": "Their email"})
    column.semantic_type = column.generated_semantic_type = "email"
    return catalog


def test_an_edit_says_who_made_it_and_going_back_to_the_model_is_no_edit():
    c = _generated()
    table = rules.update_table_fields(c, "users", {"description": "People"}, by="ayse")
    assert table.user_overrides["description"] == {"en": "People"}
    assert table.edited["description"].by == "ayse" and table.edited["description"].at
    table = rules.update_table_fields(c, "users", {"description": "Users"}, by="mehmet")
    assert "description" not in table.user_overrides and "description" not in table.edited
    column = rules.update_column_fields(c, "users", "email", {"semantic_type": "name"}, by="ayse")
    assert column.edited["semantic_type"].by == "ayse"
    column = rules.update_column_fields(c, "users", "email", {"semantic_type": "email"})
    assert "semantic_type" not in column.user_overrides and "semantic_type" not in column.edited
    column = rules.update_column_fields(c, "users", "email", {"semantic_type": " "})
    assert column.semantic_type is None and column.user_overrides["semantic_type"] is None


def test_re_analysis_keeps_what_the_model_wrote_before_and_the_edit_marks():
    previous = _generated()
    rules.update_column_fields(previous, "users", "email", {"description": "Login"}, by="ayse")
    fresh = _generated()
    fresh.tables["users"].generated_description = LocalizedText(texts={"en": "Members"})
    fresh.tables["users"].columns[0].generated_semantic_type = "name"
    rules.record_model_changes(fresh, previous)
    table = fresh.tables["users"]
    assert table.previous_generated == {"description": {"en": "Users"}}
    column = table.columns[0]
    assert column.previous_generated == {"semantic_type": "email"}
    assert column.edited["description"].by == "ayse"
    kept = _generated()
    kept.tables["users"].generated_description = LocalizedText(texts={"en": "Other"})
    rules.record_model_changes(kept, previous, keep=["users"])
    assert kept.tables["users"].previous_generated == {}


def test_a_catalog_written_before_these_fields_still_loads():
    old = {
        "database_name": "db",
        "tables": {
            "users": {
                "table_name": "users",
                "description": {"texts": {"en": "Users"}},
                "generated_description": {"texts": {"en": "Users"}},
                "columns": [
                    {"name": "email", "data_type": "varchar", "user_overrides": {"tags": ["x"]}}
                ],
                "user_overrides": {"description": {"en": "Users"}},
            }
        },
    }
    table = DatabaseCatalog.model_validate(old).tables["users"]
    assert (
        table.edited == {} and table.previous_generated == {} and table.generated_human_name is None
    )
    assert table.columns[0].generated_semantic_type is None


def test_the_sum_and_the_average_of_one_expression_are_two_measures():
    from truejoin.domain.catalog.terms import MeasureError, SqlAttribute, Term

    c = _catalog()
    c.terms["Order"] = Term(name="Order", tables=["orders"])

    def measure(name: str, aggregation: str, certified: bool = False) -> SqlAttribute:
        return SqlAttribute(
            name=name,
            table="orders",
            expression="total",
            aggregation=aggregation,
            certified=certified,
        )

    rules.add_measure(c, "Order", measure("revenue", "sum"))
    added = rules.add_measure(c, "Order", measure("basket", "avg", certified=True))
    assert added.certified is False  # a draft, whatever the input said
    with pytest.raises(MeasureError, match="already is the avg of this expression"):
        rules.add_measure(c, "Order", measure("basket_again", "avg"))
    assert [a.name for a in rules.add_sql_attributes(c, [measure("median_total", "median")])] == [
        "median_total"
    ]
    assert rules.add_sql_attributes(c, [measure("again", "median")]) == []
