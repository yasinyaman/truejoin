"""The join graph: which columns hold the same key, and how tables are related."""

from trio.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    RelationshipInfo,
    TableCatalogEntry,
    relationship_edges,
)
from trio.domain.catalog.join_graph import JoinEdge, JoinGraph
from trio.domain.olap.catalog import SchemaCatalog
from trio.domain.olap.models import ForeignKey, TableRef, TableSchema, keys_of


def ref(table: str) -> TableRef:
    return TableRef("db", table)


def edge(table: str, column: str, ref_table: str, ref_column: str = "id") -> JoinEdge:
    return JoinEdge("db", table, column, ref_table, ref_column)


GRAPH = JoinGraph(
    [
        edge("orders", "customer_id", "customers"),
        edge("invoices", "customer_id", "customers"),
        edge("items", "order_id", "orders"),
        edge("enrollments", "student_id", "students"),
        edge("enrollments", "course_id", "courses"),
        edge("employees", "manager_id", "employees"),
    ]
)


class TestSameKey:
    def test_the_two_ends_of_an_edge(self):
        assert GRAPH.same_key(ref("orders"), "customer_id", ref("customers"), "id")
        assert GRAPH.same_key(ref("customers"), "id", ref("orders"), "customer_id")

    def test_two_foreign_keys_to_one_parent(self):
        assert GRAPH.same_key(ref("orders"), "customer_id", ref("invoices"), "customer_id")

    def test_two_keys_of_different_things(self):
        assert not GRAPH.same_key(ref("orders"), "id", ref("customers"), "id")
        assert not GRAPH.same_key(ref("items"), "order_id", ref("customers"), "id")

    def test_a_column_no_edge_touches_is_no_key(self):
        assert not GRAPH.knows(ref("orders"), "total")
        assert not GRAPH.same_key(ref("orders"), "total", ref("customers"), "id")

    def test_a_primary_key_is_a_key_even_when_nothing_references_it(self):
        graph = JoinGraph(
            [edge("orders", "customer_id", "customers")], keys=[(ref("orders"), "id")]
        )
        assert graph.knows(ref("orders"), "ID")
        assert not graph.same_key(ref("orders"), "id", ref("customers"), "id")
        assert len(graph) == 1

    def test_names_compare_without_case(self):
        assert GRAPH.knows(TableRef("DB", "ORDERS"), "CUSTOMER_ID")
        assert GRAPH.same_key(ref("ORDERS"), "Customer_Id", ref("customers"), "ID")


class TestRoutes:
    def test_the_direct_edges_between_two_tables(self):
        both_ways = GRAPH.edges_between(ref("customers"), ref("orders"))
        assert [str(e) for e in both_ways] == ["orders.customer_id = customers.id"]
        assert GRAPH.edges_between(ref("items"), ref("customers")) == []

    def test_a_path_through_one_table(self):
        path = GRAPH.shortest_path(ref("items"), ref("customers"))
        assert [str(e) for e in path] == [
            "items.order_id = orders.id",
            "orders.customer_id = customers.id",
        ]

    def test_a_link_table_reaches_both_parents(self):
        path = GRAPH.shortest_path(ref("students"), ref("courses"))
        assert [e.table for e in path] == ["enrollments", "enrollments"]

    def test_nothing_connects_them(self):
        assert GRAPH.shortest_path(ref("students"), ref("customers")) == []
        assert GRAPH.shortest_path(ref("orders"), ref("orders")) == []

    def test_a_self_reference_leads_nowhere_else(self):
        assert GRAPH.edges_between(ref("employees"), ref("employees")) == []
        assert GRAPH.knows(ref("employees"), "manager_id")

    def test_an_edge_knows_its_column_on_each_side(self):
        e = edge("orders", "customer_id", "customers")
        assert e.column_of(ref("ORDERS")) == "customer_id"
        assert e.column_of(ref("customers")) == "id"
        assert e.column_of(ref("items")) is None
        assert e.other_end(ref("orders")) == ref("customers")


class TestCatalogGraph:
    def tables(self) -> dict[str, TableSchema]:
        return {
            "customers": TableSchema(primary_key=("id",)),
            "orders": TableSchema(foreign_keys=(ForeignKey("customer_id", "customers", "id"),)),
            "visits": TableSchema(),
        }

    def test_declared_foreign_keys_are_the_graph(self):
        catalog = SchemaCatalog()
        catalog.register_source("db", "fake", self.tables())
        assert [str(e) for e in catalog.joins.edges] == ["orders.customer_id = customers.id"]
        assert catalog.joins.knows(ref("customers"), "id")
        assert not catalog.joins.knows(ref("orders"), "id")  # no primary key declared

    def test_catalog_relationships_are_added_and_survive_a_refresh(self):
        catalog = SchemaCatalog()
        catalog.register_source("db", "fake", self.tables())
        catalog.set_relationships(
            "db",
            [
                JoinEdge("db", "visits", "customer_ref", "customers", "id", "catalog"),
                JoinEdge("db", "visits", "shop_id", "shops", "id", "catalog"),  # no such table
            ],
        )
        assert len(catalog.joins) == 2
        assert catalog.joins.same_key(ref("visits"), "customer_ref", ref("orders"), "customer_id")
        catalog.register_source("db", "fake", self.tables())
        assert len(catalog.joins) == 2

    def test_a_disconnected_source_leaves_no_edge(self):
        catalog = SchemaCatalog()
        catalog.register_source("db", "fake", self.tables())
        assert len(catalog.joins) == 1
        catalog.unregister_source("db")
        assert len(catalog.joins) == 0

    def test_the_schema_context_names_the_keys(self):
        catalog = SchemaCatalog()
        catalog.register_source("db", "fake", self.tables())
        assert "`customer_id` references `db.customers.id`" in catalog.generate_context()


class TestRelationshipEdges:
    def catalog(self, status: CatalogStatus, verified: bool | None) -> DatabaseCatalog:
        relationship = RelationshipInfo(
            source_column="customer_ref",
            target_table="customers",
            target_column="id",
            verified=verified,
        )
        return DatabaseCatalog(
            database_name="db",
            status=status,
            tables={"visits": TableCatalogEntry(table_name="visits", relationships=[relationship])},
        )

    def test_an_approved_verified_relationship_is_an_edge(self):
        edges = relationship_edges("db", self.catalog(CatalogStatus.approved, True))
        assert edges == [JoinEdge("db", "visits", "customer_ref", "customers", "id", "catalog")]

    def test_a_one_to_many_on_the_parent_runs_from_the_child(self):
        relationship = RelationshipInfo(
            source_column="id",
            target_table="bookings",
            target_column="client_id",
            relationship_type="one-to-many",
            verified=True,
        )
        catalog = DatabaseCatalog(
            database_name="db",
            status=CatalogStatus.approved,
            tables={
                "clients": TableCatalogEntry(table_name="clients", relationships=[relationship])
            },
        )
        assert relationship_edges("db", catalog) == [
            JoinEdge("db", "bookings", "client_id", "clients", "id", "catalog")
        ]
        relationship.relationship_type = "many-to-many"
        assert relationship_edges("db", catalog) == []

    def test_the_keys_decide_which_way_round(self):
        # A one-to-many written on the child, as a model did on a real schema:
        # read as written it made transactions the parent of customers.
        relationship = RelationshipInfo(
            source_column="customer_id",
            target_table="customers",
            target_column="customer_id",
            relationship_type="one-to-many",
            verified=True,
        )
        customers = TableCatalogEntry(
            table_name="customers",
            columns=[ColumnCatalogEntry(name="customer_id", data_type="int", is_primary_key=True)],
        )
        transactions = TableCatalogEntry(
            table_name="transactions",
            columns=[
                ColumnCatalogEntry(name="id", data_type="int", is_primary_key=True),
                ColumnCatalogEntry(name="customer_id", data_type="int"),
            ],
            relationships=[relationship],
        )
        catalog = DatabaseCatalog(
            database_name="db",
            status=CatalogStatus.approved,
            tables={"customers": customers, "transactions": transactions},
        )
        assert relationship_edges("db", catalog) == [
            JoinEdge("db", "transactions", "customer_id", "customers", "customer_id", "catalog")
        ]
        # Neither end a key: no single row either way, no edge.
        customers.columns[0].is_primary_key = False
        customers.columns.append(
            ColumnCatalogEntry(name="id", data_type="int", is_primary_key=True)
        )
        assert relationship_edges("db", catalog) == []

    def test_a_draft_adds_nothing(self):
        assert relationship_edges("db", self.catalog(CatalogStatus.draft, True)) == []
        assert relationship_edges("db", None) == []

    def test_an_unchecked_or_contradicted_relationship_adds_nothing(self):
        assert relationship_edges("db", self.catalog(CatalogStatus.approved, None)) == []
        assert relationship_edges("db", self.catalog(CatalogStatus.approved, False)) == []

    def test_agents_are_not_told_about_a_contradicted_relationship(self):
        table = self.catalog(CatalogStatus.approved, False).tables["visits"]
        assert table.joinable_relationships == []
        assert (
            len(self.catalog(CatalogStatus.approved, None).tables["visits"].joinable_relationships)
            == 1
        )


class TestKeysOf:
    def test_a_gateway_description(self):
        primary, foreign = keys_of(
            {
                "primary_key": "id",
                "foreign_keys": [
                    {
                        "column": "customer_id",
                        "references_table": "customers",
                        "references_column": "id",
                    },
                    {"column": "broken", "references_table": "", "references_column": "id"},
                ],
            }
        )
        assert primary == ("id",)
        assert foreign == (ForeignKey("customer_id", "customers", "id"),)

    def test_a_composite_or_missing_key(self):
        assert keys_of({"primary_key": ["a", "b"]}) == (("a", "b"), ())
        assert keys_of({"primary_key": None}) == ((), ())


class TestParents:
    """The tables a table references: where a row meets at most one other row."""

    def test_nearest_first_with_the_whole_way_there(self):
        parents = GRAPH.parents(ref("items"))
        assert list(parents) == [ref("orders"), ref("customers")]
        assert [str(e) for e in parents[ref("customers")]] == [
            "items.order_id = orders.id",
            "orders.customer_id = customers.id",
        ]

    def test_a_child_is_not_a_parent(self):
        # customers has many orders: reading orders beside it would repeat its rows.
        assert GRAPH.parents(ref("customers")) == {}

    def test_a_parent_reached_by_two_keys_is_left_out_with_what_lies_beyond(self):
        graph = JoinGraph(
            [
                edge("transfers", "sender_id", "accounts"),
                edge("transfers", "receiver_id", "accounts"),
                edge("accounts", "branch_id", "branches"),
                edge("transfers", "channel_id", "channels"),
            ]
        )
        assert list(graph.parents(ref("transfers"))) == [ref("channels")]

    def test_a_parent_reached_by_two_chains_is_left_out(self):
        graph = JoinGraph(
            [
                edge("sales", "buyer_id", "buyers"),
                edge("sales", "shop_id", "shops"),
                edge("buyers", "country_id", "countries"),
                edge("shops", "country_id", "countries"),
                edge("countries", "region_id", "regions"),
            ]
        )
        assert list(graph.parents(ref("sales"))) == [ref("buyers"), ref("shops")]

    def test_a_skipped_parent_is_not_reached_by_a_longer_chain(self):
        graph = JoinGraph(
            [
                edge("wires", "from_acct", "accounts"),
                edge("wires", "to_acct", "accounts"),
                edge("wires", "desk_id", "desks"),
                edge("desks", "settlement_acct", "accounts"),
            ]
        )
        assert list(graph.parents(ref("wires"))) == [ref("desks")]

    def test_a_relationship_repeating_a_key_is_one_edge(self):
        declared = edge("bookings", "client_id", "clients")
        echoed = JoinEdge("db", "bookings", "client_id", "clients", "id", "catalog")
        graph = JoinGraph([declared, echoed])
        assert graph.edges == (declared,)
        assert list(graph.parents(ref("bookings"))) == [ref("clients")]

    def test_a_cycle_ends(self):
        graph = JoinGraph([edge("a", "b_id", "b"), edge("b", "a_id", "a")])
        assert list(graph.parents(ref("a"))) == [ref("b")]

    def test_names_compare_without_case(self):
        assert list(GRAPH.parents(TableRef("DB", "ITEMS"))) == [ref("orders"), ref("customers")]


class TestAcross:
    """Many-to-many: down to a link table, then up to its other end."""

    def link(self, name: str, a: str, b: str) -> list[JoinEdge]:
        return [JoinEdge("db", name, f"{a}_id", a, "id"), JoinEdge("db", name, f"{b}_id", b, "id")]

    def test_the_far_end_and_what_it_references(self):
        graph = JoinGraph(
            [
                *self.link("enrolled", "student", "course"),
                JoinEdge("db", "course", "dept_id", "dept", "id"),
            ],
            bridges=[TableRef("db", "enrolled")],
        )
        across = graph.across(TableRef("db", "student"))
        assert {ref.table for ref in across} == {"course", "dept"}
        assert [e.table for e in across[TableRef("db", "dept")]] == [
            "enrolled",
            "enrolled",
            "course",
        ]

    def test_only_a_named_link_is_crossed(self):
        graph = JoinGraph(self.link("enrolled", "student", "course"))
        assert graph.across(TableRef("db", "student")) == {}

    def test_two_links_to_one_table_are_not_guessed_between(self):
        graph = JoinGraph(
            [
                *self.link("enrolled", "student", "course"),
                *self.link("waitlist", "student", "course"),
            ],
            bridges=[TableRef("db", "enrolled"), TableRef("db", "waitlist")],
        )
        assert graph.across(TableRef("db", "student")) == {}

    def test_a_table_already_referenced_is_reached_the_direct_way(self):
        graph = JoinGraph(
            [
                *self.link("enrolled", "student", "course"),
                JoinEdge("db", "student", "course_id", "course", "id"),
            ],
            bridges=[TableRef("db", "enrolled")],
        )
        assert graph.across(TableRef("db", "student")) == {}
        assert TableRef("db", "course") in graph.parents(TableRef("db", "student"))
