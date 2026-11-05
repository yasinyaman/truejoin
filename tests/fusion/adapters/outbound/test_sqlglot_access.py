"""The rewriter alone: what it wraps, and everything it refuses rather than passes through."""

import pytest

from trio.adapters.outbound.sqlglot_access import SqlglotAccessRewriter
from trio.domain.governance.access import TableView
from trio.domain.olap.errors import GuardrailViolation
from trio.domain.olap.models import TableRef

ORDERS = TableRef("db", "orders")
VIEWS = {ORDERS: TableView("db.orders__s_1", ("id", "email"), (("tenant", "eq", "a"),))}


@pytest.fixture
def rewriter():
    return SqlglotAccessRewriter()


def test_a_table_becomes_a_subquery_over_where_it_is_stored(rewriter):
    sql = rewriter.secure("SELECT o.id FROM db.orders AS o WHERE o.id = ?", VIEWS)
    assert sql == (
        'SELECT o.id FROM (SELECT "id", "email" FROM "db"."orders__s_1" '
        "WHERE \"tenant\" = 'a') AS o WHERE o.id = ?"
    )


def test_an_unaliased_table_keeps_its_name_as_the_qualifier(rewriter):
    assert rewriter.secure("SELECT orders.id FROM db.orders", VIEWS).endswith("AS orders")


def test_a_value_is_a_literal_however_it_is_spelled(rewriter):
    views = {ORDERS: TableView("db.orders", ("id",), (("tenant", "eq", "o'; DROP TABLE x; --"),))}
    assert "'o''; DROP TABLE x; --'" in rewriter.secure("SELECT id FROM db.orders", views)


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("in", [1, 2], '"c" IN (1, 2)'),
        ("is_null", True, '"c" IS NULL'),
        ("is_null", False, 'NOT "c" IS NULL'),
        ("like", "a%", "\"c\" LIKE 'a%'"),
        ("gte", 3, '"c" >= 3'),
    ],
)
def test_every_row_rule_operator_has_its_sql(rewriter, operator, value, expected):
    views = {ORDERS: TableView("db.orders", ("id",), (("c", operator, value),))}
    assert expected in rewriter.secure("SELECT id FROM db.orders", views)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM db.other",
        "SELECT * FROM cat.db.orders",
        "SELECT * FROM range(3)",
        "SELECT * FROM query_table('db.orders')",
        "SELECT * FROM db.orders, LATERAL query_table('db.orders')",
        "SELECT * FROM db.orders TABLESAMPLE 10%",
        "SELECT * FROM 'db.orders'",
        "PIVOT db.orders ON tenant USING SUM(id)",
        "DESCRIBE db.orders",
        "SELECT * FROM (",
        "SELECT * FROM orders WHERE id IN (WITH orders AS (SELECT 1 AS id) SELECT id FROM orders)",
    ],
)
def test_what_cannot_be_wrapped_is_refused(rewriter, sql):
    with pytest.raises(GuardrailViolation, match="Guardrail"):
        rewriter.secure(sql, VIEWS)


def test_unknown_columns_strategies_and_operators_are_refused(rewriter):
    for view in (
        TableView("db.orders", ()),
        TableView("db.orders", ("id",), masks={"id": "rot13"}),
        TableView("db.orders", ("id",), (("id", "between", 1),)),
    ):
        with pytest.raises(GuardrailViolation):
            rewriter.secure("SELECT * FROM db.orders", {ORDERS: view})


def test_a_cte_of_the_query_is_left_alone(rewriter):
    sql = rewriter.secure("WITH x AS (SELECT id FROM db.orders) SELECT id FROM x", VIEWS)
    assert sql.endswith("SELECT id FROM x")
    assert sql.count("orders__s_1") == 1
