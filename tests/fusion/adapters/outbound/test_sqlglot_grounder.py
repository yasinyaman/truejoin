"""Join verdicts and repairs, one decision per row.

The cases are the decision table of NVIDIA Auto Ontology's join-path check
(verified, wrong_column, missing_bridge, unverified, each with and without a
value overlap that vouches for the join), plus the ones where this grounder
decides differently on purpose: siblings of one parent may be joined directly,
and a repair is made only where it is mechanical.
"""

import duckdb
import pytest

from trio.adapters.outbound.sqlglot_grounder import SqlglotGrounder
from trio.domain.catalog.join_graph import JoinEdge, JoinGraph
from trio.domain.olap.grounding import (
    INCOMPLETE_KEY,
    MISSING_BRIDGE,
    UNVERIFIED,
    WRONG_COLUMN,
    Grounding,
    JoinTable,
    JoinVerdict,
)
from trio.domain.olap.models import TableRef

COLUMNS = {
    "customers": {"id", "name", "region"},
    "orders": {"id", "customer_id", "approved_by", "placed_by", "total"},
    "items": {"id", "order_id", "qty"},
    "invoices": {"id", "customer_id", "amount"},
    "staff": {"id", "name"},
    "visits": {"id", "customer_ref"},
}
TABLES = {
    TableRef("db", name): JoinTable(TableRef("db", name), frozenset(columns))
    for name, columns in COLUMNS.items()
}
GRAPH = JoinGraph(
    [
        JoinEdge("db", "orders", "customer_id", "customers", "id"),
        JoinEdge("db", "items", "order_id", "orders", "id"),
        JoinEdge("db", "invoices", "customer_id", "customers", "id"),
        JoinEdge("db", "orders", "approved_by", "staff", "id"),
        JoinEdge("db", "orders", "placed_by", "staff", "id"),
    ],
    keys=[(TableRef("db", name), "id") for name in COLUMNS],
)


def ground(sql: str, overlap: float | None = 0.0, repair: bool = False, tables=TABLES) -> Grounding:
    return SqlglotGrounder().ground(sql, tables, GRAPH, lambda *_: overlap, repair)


def kinds(grounding: Grounding) -> list[str]:
    return [v.verdict for v in grounding.verdicts]


ACCEPTED = [
    ("the declared key", "FROM db.orders o JOIN db.customers c ON o.customer_id = c.id"),
    (
        "the declared key, parent first",
        "FROM db.orders o JOIN db.customers c ON c.id = o.customer_id",
    ),
    (
        "two children of one parent",
        "FROM db.orders o JOIN db.invoices v ON o.customer_id = v.customer_id",
    ),
    ("no key on either side", "FROM db.customers c JOIN db.staff s ON c.name = s.name"),
    ("a self-join", "FROM db.orders a JOIN db.orders b ON a.id = b.customer_id"),
    (
        "a column against a value",
        "FROM db.orders o JOIN db.customers c ON o.customer_id = c.id AND c.id = 7",
    ),
    (
        "unqualified columns owned by one table",
        "FROM db.items JOIN db.orders ON order_id = orders.id",
    ),
    ("an ambiguous unqualified column", "FROM db.orders JOIN db.customers ON id = customer_id"),
    ("a derived table", "FROM (SELECT 1 AS id) x JOIN db.orders o ON x.id = o.id"),
    (
        "USING the key two children share",
        "FROM db.orders o JOIN db.invoices v USING (customer_id)",
    ),
    ("USING after a derived table", "FROM (SELECT 1 AS id) x JOIN db.orders o USING (id)"),
]

# Both columns are keys already, of different things.
MISTAKEN = [
    (
        "the tables are related by another column",
        "FROM db.orders o JOIN db.customers c ON o.id = c.id",
        WRONG_COLUMN,
    ),
    (
        "a correlated subquery",
        "FROM db.customers c WHERE EXISTS (SELECT 1 FROM db.orders o WHERE o.id = c.id)",
        WRONG_COLUMN,
    ),
    (
        "related only through a table in between",
        "FROM db.items i JOIN db.customers c ON i.order_id = c.id",
        MISSING_BRIDGE,
    ),
    (
        "two children joined on something else",
        "FROM db.orders o JOIN db.invoices v ON o.id = v.id",
        MISSING_BRIDGE,
    ),
    ("nothing relates them", "FROM db.visits v JOIN db.customers c ON v.id = c.id", UNVERIFIED),
    ("USING the wrong column", "FROM db.orders o JOIN db.customers c USING (id)", WRONG_COLUMN),
    ("NATURAL on the ids they share", "FROM db.items NATURAL JOIN db.customers", MISSING_BRIDGE),
]

# One column is a key, the other is nothing the graph knows.
UNKNOWN = [
    (
        "an old-style join in WHERE",
        "FROM db.orders o, db.customers c WHERE o.total = c.id",
        WRONG_COLUMN,
    ),
    (
        "a key against a name",
        "FROM db.orders o JOIN db.staff s ON o.approved_by = s.name",
        WRONG_COLUMN,
    ),
    (
        "a quantity against a key two tables away",
        "FROM db.items i JOIN db.customers c ON i.qty = c.id",
        MISSING_BRIDGE,
    ),
    (
        "a reference nobody declared",
        "FROM db.visits v JOIN db.customers c ON v.customer_ref = c.id",
        UNVERIFIED,
    ),
]


class TestVerdicts:
    @pytest.mark.parametrize(("why", "sql"), ACCEPTED, ids=[a[0] for a in ACCEPTED])
    def test_accepted(self, why, sql):
        assert ground(f"SELECT 1 {sql}").verdicts == ()

    @pytest.mark.parametrize(("why", "sql", "verdict"), MISTAKEN, ids=[m[0] for m in MISTAKEN])
    def test_two_keys_of_different_things_are_flagged_whatever_their_values(
        self, why, sql, verdict
    ):
        """Serial ids of unrelated tables overlap almost entirely; that proves nothing."""

        def never_asked(*_):
            raise AssertionError("two known keys are not compared by value")

        grounding = SqlglotGrounder().ground(f"SELECT 1 {sql}", TABLES, GRAPH, never_asked)
        assert kinds(grounding) == [verdict]
        assert grounding.verdicts[0].overlap is None

    @pytest.mark.parametrize(("why", "sql", "verdict"), UNKNOWN, ids=[u[0] for u in UNKNOWN])
    def test_an_unknown_column_is_flagged_unless_its_values_vouch_for_it(self, why, sql, verdict):
        """The graph can be incomplete: a relationship nobody declared is not a mistake."""
        assert kinds(ground(f"SELECT 1 {sql}", overlap=0.0)) == [verdict]
        assert kinds(ground(f"SELECT 1 {sql}", overlap=0.19)) == [verdict]
        assert ground(f"SELECT 1 {sql}", overlap=0.2).verdicts == ()
        # Not being able to look is not a reason to let it through.
        assert kinds(ground(f"SELECT 1 {sql}", overlap=None)) == [verdict]

    def test_using_and_natural_are_judged_but_never_rewritten(self):
        for sql in (
            "SELECT * FROM db.orders o JOIN db.customers c USING (id)",
            "SELECT * FROM db.orders NATURAL JOIN db.customers",
        ):
            grounding = ground(sql, repair=True)
            assert grounding.sql == sql  # as ON, SELECT * and the shared column would change
            assert [(v.verdict, v.left, v.repaired) for v in grounding.verdicts] == [
                (WRONG_COLUMN, "orders.id", False)
            ]

    def test_a_cte_is_not_a_table_to_judge(self):
        sql = "WITH x AS (SELECT 1 AS id) SELECT 1 FROM x JOIN db.orders o ON x.id = o.id"
        assert ground(sql).verdicts == ()

    def test_a_verdict_names_the_join_and_what_is_known(self):
        (verdict,) = ground(
            "SELECT 1 FROM db.items i JOIN db.customers c ON i.order_id = c.id"
        ).verdicts
        assert verdict == JoinVerdict(
            MISSING_BRIDGE,
            "items.order_id",
            "customers.id",
            ("items.order_id = orders.id", "orders.customer_id = customers.id"),
        )
        assert "related only through items.order_id = orders.id and" in verdict.message()
        assert verdict.as_dict()["join"] == "items.order_id = customers.id"

    def test_an_unverified_join_says_what_was_measured(self):
        sql = "SELECT 1 FROM db.visits v JOIN db.customers c ON v.customer_ref = c.id"
        assert "only 5% of sampled values match" in ground(sql, 0.05).verdicts[0].message()
        assert "nothing in the data vouches for it" in ground(sql, None).verdicts[0].message()

    def test_of_two_keys_between_two_tables_the_one_sharing_a_column_is_named(self):
        (verdict,) = ground(
            "SELECT 1 FROM db.orders o JOIN db.staff s ON o.approved_by = s.name"
        ).verdicts
        assert verdict.route == ("orders.approved_by = staff.id",)
        (both,) = ground("SELECT 1 FROM db.orders o JOIN db.staff s ON o.total = s.id").verdicts
        assert len(both.route) == 2

    def test_the_same_join_written_twice_is_one_verdict(self):
        sql = (
            "SELECT 1 FROM db.orders o JOIN db.customers c ON o.id = c.id "
            "WHERE EXISTS (SELECT 1 FROM db.orders o JOIN db.customers c ON o.id = c.id)"
        )
        assert kinds(ground(sql)) == [WRONG_COLUMN]

    def test_only_keyed_joins_are_probed(self):
        asked = []
        SqlglotGrounder().ground(
            "SELECT 1 FROM db.orders o JOIN db.customers c ON o.customer_id = c.id "
            "JOIN db.staff s ON c.name = s.name JOIN db.items i ON i.qty = o.id",
            TABLES,
            GRAPH,
            lambda *pair: asked.append(pair) or 0.5,
        )
        assert asked == [(TableRef("db", "items"), "qty", TableRef("db", "orders"), "id")]

    def test_tables_of_two_databases_are_not_compared(self):
        other = TableRef("crm", "customers")
        tables = {**TABLES, other: JoinTable(other, frozenset({"id"}))}
        sql = "SELECT 1 FROM db.orders o JOIN crm.customers c ON o.id = c.id"
        assert ground(sql, tables=tables).verdicts == ()

    def test_nothing_to_do_without_a_graph_or_for_sql_that_does_not_parse(self):
        sql = "SELECT 1 FROM db.orders o JOIN db.customers c ON o.id = c.id"
        assert SqlglotGrounder().ground(sql, TABLES, JoinGraph(), lambda *_: 0.0) == Grounding(sql)
        assert ground("SELECT FROM WHERE (((") == Grounding("SELECT FROM WHERE (((")


@pytest.fixture(scope="module")
def db():
    """Rows chosen so that a wrong join and the right one give different answers."""
    con = duckdb.connect()
    con.execute("CREATE SCHEMA db")
    con.execute(
        "CREATE TABLE db.customers AS SELECT * FROM (VALUES (1,'Ann','n'),(2,'Bo','s'),(3,'Cy','n')) t(id,name,region)"
    )
    con.execute(
        "CREATE TABLE db.orders AS SELECT * FROM "
        "(VALUES (1,2,10,10,5.0),(2,2,10,11,7.0),(3,3,11,10,1.0),(4,1,11,11,2.0)) t(id,customer_id,approved_by,placed_by,total)"
    )
    con.execute(
        "CREATE TABLE db.items AS SELECT * FROM (VALUES (1,1,2),(2,1,3),(3,3,1),(4,4,9)) t(id,order_id,qty)"
    )
    con.execute(
        "CREATE TABLE db.invoices AS SELECT * FROM (VALUES (1,2,50.0)) t(id,customer_id,amount)"
    )
    con.execute("CREATE TABLE db.staff AS SELECT * FROM (VALUES (10,'Dee'),(11,'Eli')) t(id,name)")
    yield con
    con.close()


class TestRepairs:
    def repaired(self, sql: str) -> Grounding:
        grounding = ground(sql, repair=True)
        assert grounding.verdicts and not grounding.unresolved, grounding
        return grounding

    def test_a_wrong_column_is_swapped_for_the_key(self, db):
        fixed = self.repaired(
            "SELECT c.name, SUM(o.total) AS total FROM db.orders o "
            "JOIN db.customers c ON o.id = c.id GROUP BY c.name ORDER BY c.name"
        )
        right = "SELECT c.name, SUM(o.total) AS total FROM db.orders o JOIN db.customers c ON o.customer_id = c.id GROUP BY c.name ORDER BY c.name"
        assert db.execute(fixed.sql).fetchall() == db.execute(right).fetchall()
        assert fixed.verdicts[0].repaired

    def test_only_the_column_that_is_wrong_changes(self):
        fixed = self.repaired(
            "SELECT 1 FROM db.orders o JOIN db.customers c ON o.customer_id = c.name"
        )
        assert 'o.customer_id = c."id"' in fixed.sql

    def test_an_old_style_join_is_repaired_where_it_stands(self, db):
        fixed = self.repaired(
            "SELECT COUNT(*) FROM db.orders o, db.customers c WHERE o.id = c.id AND c.region = 'n'"
        )
        assert db.execute(fixed.sql).fetchall() == [(2,)]

    def test_the_table_in_between_is_joined_in(self, db):
        fixed = self.repaired(
            "SELECT c.name, SUM(i.qty) AS qty FROM db.items i "
            "JOIN db.customers c ON i.order_id = c.id GROUP BY c.name ORDER BY c.name"
        )
        assert db.execute(fixed.sql).fetchall() == [("Ann", 9), ("Bo", 5), ("Cy", 1)]

    def test_it_works_whichever_side_the_join_brings_in(self, db):
        fixed = self.repaired(
            "SELECT c.name, i.qty FROM db.customers c JOIN db.items i ON c.id = i.order_id ORDER BY i.id"
        )
        assert db.execute(fixed.sql).fetchall() == [("Bo", 2), ("Bo", 3), ("Cy", 1), ("Ann", 9)]

    def test_a_left_join_stays_a_left_join_and_keeps_its_other_conditions(self, db):
        fixed = self.repaired(
            "SELECT i.id, c.name FROM db.items i "
            "LEFT JOIN db.customers c ON i.order_id = c.id AND c.region = 'n' ORDER BY i.id"
        )
        assert db.execute(fixed.sql).fetchall() == [(1, None), (2, None), (3, "Cy"), (4, "Ann")]

    def test_the_repaired_query_is_checked_again(self):
        grounding = ground(
            "SELECT 1 FROM db.orders o JOIN db.customers c ON o.id = c.id "
            "JOIN db.visits v ON v.id = c.id",
            repair=True,
        )
        assert [(v.verdict, v.repaired) for v in grounding.verdicts] == [
            (WRONG_COLUMN, True),
            (UNVERIFIED, False),
        ]
        assert [v.verdict for v in grounding.unresolved] == [UNVERIFIED]

    NOT_MECHANICAL = [
        ("two keys could be meant", "FROM db.orders o JOIN db.staff s ON o.total = s.id"),
        (
            "the table in between is already in the query",
            "FROM db.orders o JOIN db.items i ON o.id = i.order_id JOIN db.customers c ON i.order_id = c.id",
        ),
        (
            "there is no JOIN to put it before",
            "FROM db.items i, db.customers c WHERE i.order_id = c.id",
        ),
        (
            "two children would be multiplied through their parent",
            "FROM db.orders o JOIN db.invoices v ON o.id = v.id",
        ),
        (
            "a full join cannot be split in two",
            "FROM db.items i FULL JOIN db.customers c ON i.order_id = c.id",
        ),
        (
            "the condition is about two tables joined earlier",
            "FROM db.items i CROSS JOIN db.customers c JOIN db.staff s ON i.order_id = c.id",
        ),
        (
            "nothing is known to repair it with",
            "FROM db.visits v JOIN db.customers c ON v.id = c.id",
        ),
    ]

    @pytest.mark.parametrize(("why", "sql"), NOT_MECHANICAL, ids=[n[0] for n in NOT_MECHANICAL])
    def test_left_unresolved_and_untouched(self, why, sql):
        grounding = ground(f"SELECT 1 {sql}", repair=True)
        assert grounding.sql == f"SELECT 1 {sql}"
        assert len(grounding.unresolved) == 1

    def test_a_query_with_nothing_wrong_is_returned_as_written(self):
        sql = "select  1 from db.orders o join db.customers c on o.customer_id = c.id"
        assert ground(sql, repair=True) == Grounding(sql)


class TestKeysOfSeveralColumns:
    """A shipment names an order line by order and line number; one of the two is not enough."""

    LINES = TableRef("db", "lines")
    SHIPMENTS = TableRef("db", "shipments")
    TABLES = {
        LINES: JoinTable(LINES, frozenset({"order_no", "line_no", "qty"})),
        SHIPMENTS: JoinTable(SHIPMENTS, frozenset({"id", "order_no", "line_no", "shipped"})),
    }
    GRAPH = JoinGraph(
        [
            JoinEdge("db", "shipments", "order_no", "lines", "order_no"),
            JoinEdge("db", "shipments", "line_no", "lines", "line_no"),
        ],
        keys=[(SHIPMENTS, "id")],
        composite=[(LINES, ("order_no", "line_no"))],
    )
    FROM = "SELECT 1 FROM db.shipments s JOIN db.lines l ON "

    def ground(self, sql, repair=False):
        return SqlglotGrounder().ground(sql, self.TABLES, self.GRAPH, lambda *_: 0.0, repair)

    def test_the_whole_key_is_accepted(self):
        sql = self.FROM + "s.order_no = l.order_no AND l.line_no = s.line_no"
        assert self.ground(sql).verdicts == ()

    def test_one_column_of_it_is_flagged(self):
        (verdict,) = self.ground(self.FROM + "s.order_no = l.order_no").verdicts
        assert (verdict.verdict, verdict.left, verdict.right) == (
            INCOMPLETE_KEY,
            "shipments.order_no",
            "lines.order_no",
        )
        assert verdict.route == (
            "shipments.order_no = lines.order_no",
            "shipments.line_no = lines.line_no",
        )
        assert "one column of a key of several" in verdict.message()

    def test_repair_adds_the_rest_of_the_key(self):
        grounding = self.ground(self.FROM + "s.order_no = l.order_no", repair=True)
        assert [(v.verdict, v.repaired) for v in grounding.verdicts] == [(INCOMPLETE_KEY, True)]
        assert 's."line_no" = l."line_no"' in grounding.sql
        assert self.ground(grounding.sql).verdicts == ()

    def test_where_and_using_are_flagged_but_not_completed(self):
        for sql in (
            "SELECT 1 FROM db.shipments s, db.lines l WHERE s.order_no = l.order_no",
            "SELECT 1 FROM db.shipments s JOIN db.lines l USING (order_no)",
        ):
            grounding = self.ground(sql, repair=True)
            assert grounding.sql == sql
            assert [(v.verdict, v.repaired) for v in grounding.verdicts] == [
                (INCOMPLETE_KEY, False)
            ]

    def test_two_keys_to_one_column_are_not_one_key(self):
        """orders.approved_by and orders.placed_by both reference staff.id: two keys."""
        sql = "SELECT 1 FROM db.orders o JOIN db.staff s ON o.approved_by = s.id"
        assert ground(sql).verdicts == ()
