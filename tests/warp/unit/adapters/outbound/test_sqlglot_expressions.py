"""The expression miner: what is taken from a statement, and what is left."""

import pytest

from trio.adapters.outbound.sqlglot_expressions import SqlglotExpressionMiner
from trio.adapters.outbound.sqlglot_semantic import _expression_sql
from trio.domain.catalog.history import AGGREGATE, CONDITION, PROJECTION
from trio.domain.olap.measures import Measure

COLUMNS = {
    "orders": ["id", "customer_id", "status", "total", "qty", "placed_on"],
    "customers": ["id", "segment", "active"],
}
JOINED = "FROM orders o JOIN customers c ON c.id = o.customer_id"


def mine(sql: str, dialect: str = "postgresql") -> list[tuple[str, str, str]]:
    found = SqlglotExpressionMiner().mine(sql, dialect, COLUMNS)
    return [(m.table, m.kind, m.expression) for m in found]


def flag(condition: str) -> str:
    return f"CASE WHEN {condition} THEN 1 ELSE 0 END"


class TestWhatIsTaken:
    def test_what_sits_inside_an_aggregate(self):
        sql = f"SELECT SUM(CASE WHEN o.status = 'paid' THEN o.total END), AVG(o.total * o.qty) {JOINED}"
        assert mine(sql) == [
            ("orders", AGGREGATE, "CASE WHEN status = 'paid' THEN total END"),
            ("orders", AGGREGATE, "total * qty"),
        ]

    def test_a_bare_column_a_star_or_a_distinct_is_nothing_new(self):
        assert mine("SELECT SUM(total), COUNT(*), COUNT(DISTINCT status), MAX(1) FROM orders") == []

    def test_arithmetic_a_statement_selects(self):
        sql = "SELECT id, (total - qty) AS net, total * 1.2, UPPER(status), status FROM orders"
        assert mine(sql) == [
            ("orders", PROJECTION, "total - qty"),
            ("orders", PROJECTION, "total * 1.2"),
        ]

    def test_a_selected_case_or_function_is_not_assumed_to_be_a_number(self):
        sql = "SELECT CASE WHEN total > 100 THEN 'big' ELSE 'small' END, COALESCE(status, '-') FROM orders"
        assert mine(sql) == []

    def test_each_condition_of_a_where_becomes_a_flag(self):
        sql = "SELECT COUNT(*) FROM orders WHERE status IN ('paid', 'sent') AND total > 100"
        assert mine(sql) == [
            ("orders", CONDITION, flag("status IN ('paid', 'sent')")),
            ("orders", CONDITION, flag("total > 100")),
        ]

    @pytest.mark.parametrize(
        ("condition", "kept"),
        [
            ("NOT (status = 'void')", "NOT (status = 'void')"),
            ("total IS NOT NULL", "total IS NOT NULL"),
            ("qty BETWEEN 1 AND 5", "qty BETWEEN 1 AND 5"),
            ("status LIKE 'p%'", "status LIKE 'p%'"),
        ],
    )
    def test_the_comparisons_it_knows(self, condition, kept):
        assert mine(f"SELECT 1 FROM orders WHERE {condition}") == [
            ("orders", CONDITION, flag(kept))
        ]

    def test_an_or_is_not_split_into_halves_that_each_mean_something_else(self):
        assert mine("SELECT 1 FROM orders WHERE status = 'paid' OR total > 100") == []

    def test_a_column_is_placed_by_the_table_that_has_it(self):
        assert mine(f"SELECT SUM(total * qty) {JOINED} WHERE segment = 'gold'") == [
            ("orders", AGGREGATE, "total * qty"),
            ("customers", CONDITION, flag("segment = 'gold'")),
        ]

    def test_every_select_of_a_statement_is_read_once(self):
        sql = (
            "SELECT SUM(total) FROM (SELECT total FROM orders WHERE status = 'paid') x "
            "UNION ALL SELECT SUM(total) FROM orders WHERE status = 'paid'"
        )
        assert mine(sql) == [("orders", CONDITION, flag("status = 'paid'"))]

    def test_the_stored_spelling_of_a_column_is_what_comes_out(self):
        assert mine("SELECT SUM(NVL(TOTAL, 0) * QTY) FROM ORDERS", "oracle") == [
            ("orders", AGGREGATE, "COALESCE(total, 0) * qty")
        ]


class TestWhatIsLeft:
    @pytest.mark.parametrize(
        "sql",
        [
            # One query's parameter is not a definition.
            "SELECT SUM(total) FROM orders WHERE status = $1",
            # Which table's id?
            "SELECT SUM(id + total) FROM orders, customers",
            # Two tables in one expression cannot be one table's measure.
            f"SELECT SUM(o.total + c.id) {JOINED}",
            # A subquery could read a table nobody planned or restricted.
            "SELECT 1 FROM orders WHERE total > (SELECT AVG(total) FROM orders)",
            "SELECT SUM(total - (SELECT MIN(total) FROM orders)) FROM orders",
            # A window is not a scalar over a row.
            "SELECT SUM(total - LAG(total) OVER (ORDER BY id)) FROM orders",
            # No column of the catalog.
            "SELECT SUM(price * 2) FROM products",
            "SELECT 1 FROM orders WHERE note = 'x'",
            "this is not sql (((",
            "",
        ],
    )
    def test_nothing_is_mined(self, sql):
        assert mine(sql) == []


class TestDialects:
    def test_sql_server_functions_arrive_as_the_stores_own(self):
        sql = "SELECT SUM(ISNULL(total, 0) * qty) FROM dbo.orders WHERE status = @p1"
        assert mine(sql, "mssql") == [("orders", AGGREGATE, "COALESCE(total, 0) * qty")]

    def test_a_view_definition_is_read_through_its_create(self):
        sql = "CREATE VIEW paid AS SELECT TOP 5 id FROM orders WHERE status = 'paid'"
        assert mine(sql, "mssql") == [("orders", CONDITION, flag("status = 'paid'"))]

    def test_oracles_positional_bind_is_a_parameter_not_a_parse_error(self):
        sql = "SELECT SUM(NVL(TOTAL, 0)) FROM ORDERS WHERE STATUS = :1 AND QTY > 5"
        assert mine(sql, "oracle") == [
            ("orders", AGGREGATE, "COALESCE(total, 0)"),
            ("orders", CONDITION, flag("qty > 5")),
        ]

    def test_a_time_in_a_literal_is_not_taken_for_a_bind(self):
        sql = "SELECT 1 FROM ORDERS WHERE STATUS = '12:30'"
        assert mine(sql, "oracle") == [("orders", CONDITION, flag("status = '12:30'"))]


def test_whatever_is_mined_is_something_the_compiler_accepts_as_a_measure():
    """The two ends agree on what an expression may be."""
    sql = (
        f"SELECT SUM(CASE WHEN o.status = 'paid' THEN o.total END), AVG(o.total * o.qty) {JOINED} "
        "WHERE o.status <> 'void' AND o.qty BETWEEN 1 AND 5 AND c.segment IN ('gold')"
    )
    found = SqlglotExpressionMiner().mine(sql, "postgresql", COLUMNS)
    assert len(found) == 5
    for mined in found:
        measure = Measure("m", mined.columns[0], expression=mined.expression, reads=mined.columns)
        assert _expression_sql(measure).startswith("(")
