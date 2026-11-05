"""Grounding in the query pipeline: off, advise and repair, on real DuckDB.

The rows are chosen so a join on the wrong column returns rows too, just the
wrong ones. That is the failure this exists for: nothing errors.
"""

import pytest

from tests.fusion.fakes import FakeSourceFactory, ManualScheduler
from trio.application.olap.settings import Settings
from trio.application.olap.sources import OVERLAP_MATCH_ROWS
from trio.bootstrap import build_app
from trio.domain.catalog.join_graph import JoinEdge
from trio.domain.governance.access import AccessScope
from trio.domain.governance.masking import CatalogMasking, MaskingPolicy
from trio.domain.governance.row_policy import EMPTY_POLICY, build_policy
from trio.domain.olap.errors import QueryError
from trio.domain.olap.grounding import UNVERIFIED, WRONG_COLUMN
from trio.domain.olap.models import TableRef

TABLES = {
    "customers": [
        {"id": 1, "name": "Ann", "tenant": "a", "email": "ann@x.com"},
        {"id": 2, "name": "Bo", "tenant": "b", "email": "bo@y.org"},
        {"id": 3, "name": "Cy", "tenant": "a", "email": "cy@x.com"},
    ],
    "orders": [
        {"id": 1, "customer_id": 2, "tenant": "b", "total": 5.0},
        {"id": 2, "customer_id": 2, "tenant": "b", "total": 7.0},
        {"id": 3, "customer_id": 3, "tenant": "a", "total": 1.0},
        {"id": 4, "customer_id": 1, "tenant": "a", "total": 2.0},
    ],
    "items": [
        {"id": 1, "order_id": 1, "qty": 2},
        {"id": 2, "order_id": 1, "qty": 3},
        {"id": 3, "order_id": 3, "qty": 1},
        {"id": 4, "order_id": 4, "qty": 9},
    ],
    # Related to customers by value, with no key declared anywhere.
    "visits": [{"id": 90, "customer_ref": 1}, {"id": 91, "customer_ref": 3}],
    # Shares nothing with anybody.
    "codes": [{"id": 70, "code": 500}, {"id": 71, "code": 501}],
}
FOREIGN_KEYS = {
    "orders": [("customer_id", "customers", "id")],
    "items": [("order_id", "orders", "id")],
}

WRONG = "SELECT c.name, SUM(o.total) AS total FROM db.orders o JOIN db.customers c ON o.id = c.id GROUP BY c.name ORDER BY c.name"
RIGHT = WRONG.replace("o.id = c.id", "o.customer_id = c.id")
BRIDGED = "SELECT c.name, SUM(i.qty) AS qty FROM db.items i JOIN db.customers c ON i.order_id = c.id GROUP BY c.name ORDER BY c.name"
UNKNOWN = "SELECT COUNT(*) AS n FROM db.codes k JOIN db.customers c ON k.code = c.id"
UNDECLARED = "SELECT COUNT(*) AS n FROM db.visits v JOIN db.customers c ON v.customer_ref = c.id"


class Audit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def factory():
    return FakeSourceFactory()


@pytest.fixture
def audit():
    return Audit()


def build(tmp_path, factory, audit, grounding: str, foreign_keys=FOREIGN_KEYS):
    settings = Settings(
        threads=1, memory_limit="256MB", backup_path=str(tmp_path / "b"), grounding=grounding
    )
    app = build_app(settings, scheduler=ManualScheduler(), source_factory=factory, audit=audit)
    app.sources.connect("db", {"type": "fake", "tables": TABLES, "foreign_keys": foreign_keys})
    return app


@pytest.fixture
def advise(tmp_path, factory, audit):
    app = build(tmp_path, factory, audit, "advise")
    yield app
    app.close()


@pytest.fixture
def repair(tmp_path, factory, audit):
    app = build(tmp_path, factory, audit, "repair")
    yield app
    app.close()


def probes(factory) -> list:
    """The bounded reads the value-overlap probe made (a table load has no row limit)."""
    return [call for call in factory.sources["db"].calls_named("fetch_slice") if call[2].limit]


def records(app, sql: str, **kwargs) -> list[dict]:
    return app.query.sql(sql, **kwargs).to_records()


class TestOff:
    def test_the_wrong_join_runs_and_nobody_is_told(self, tmp_path, factory, audit):
        app = build(tmp_path, factory, audit, "off")
        try:
            result = app.query.sql(WRONG)
            assert result.grounding is None
            assert result.to_records() != records(app, RIGHT)
            assert "grounding" not in app.tools.query_data(WRONG)
            assert "grounding" not in app.tools.query_data(UNKNOWN)
            assert probes(factory) == []
        finally:
            app.close()

    def test_an_unknown_mode_is_refused_at_startup(self, tmp_path, factory, audit):
        with pytest.raises(ValueError, match="off, advise, repair"):
            build(tmp_path, factory, audit, "strict")


class TestAdvise:
    def test_the_query_runs_as_written_and_the_verdict_comes_with_it(self, advise):
        result = advise.tools.query_data(WRONG)
        assert result["rows"] == [
            {"name": "Ann", "total": 5.0},
            {"name": "Bo", "total": 7.0},
            {"name": "Cy", "total": 1.0},
        ]
        (join,) = result["grounding"]["joins"]
        assert join["verdict"] == WRONG_COLUMN
        assert join["known"] == ["orders.customer_id = customers.id"]
        assert join["repaired"] is False
        assert "sql" not in result["grounding"]

    def test_a_join_on_the_key_says_nothing(self, advise):
        assert "grounding" not in advise.tools.query_data(RIGHT)

    def test_a_cached_answer_still_carries_the_verdict(self, advise):
        advise.query.sql(WRONG)
        again = advise.query.sql(WRONG)
        assert again.from_cache
        assert [v.verdict for v in again.grounding.verdicts] == [WRONG_COLUMN]

    def test_a_relationship_nobody_declared_is_recognised_by_its_values(self, advise, factory):
        assert advise.query.sql(UNDECLARED).grounding is None
        measured = len(probes(factory))
        assert measured > 0
        advise.query.sql(UNDECLARED, use_cache=False)
        # Measured once per pair of columns, not once per query.
        assert len(probes(factory)) == measured

    def test_columns_that_share_nothing_are_flagged(self, advise):
        (verdict,) = advise.query.sql(UNKNOWN).grounding.verdicts
        assert (verdict.verdict, verdict.overlap) == (UNVERIFIED, 0.0)

    def test_readiness_reports_the_join_graph(self, advise):
        check = advise.tools.check_readiness()["checks"][-1]
        assert check == {"check": "join_graph", "ok": True, "detail": "2 join keys known"}

    def test_readiness_says_when_a_source_declares_no_key(self, tmp_path, factory, audit):
        app = build(tmp_path, factory, audit, "advise", foreign_keys={})
        try:
            result = app.tools.check_readiness()
            assert not result["ready"]
            assert "joins there are not checked" in result["checks"][-1]["detail"]
            assert app.query.sql(WRONG).grounding is None
        finally:
            app.close()

    def test_describe_table_lists_the_keys(self, advise):
        described = advise.tools.describe_table("db.orders")
        assert described["primary_key"] == ["id"]
        assert described["foreign_keys"] == [
            {"column": "customer_id", "references_table": "customers", "references_column": "id"}
        ]

    def test_a_catalog_relationship_makes_a_join_known(self, advise, factory):
        advise.catalog.set_relationships(
            "db", [JoinEdge("db", "visits", "customer_ref", "customers", "id", "catalog")]
        )
        assert advise.query.sql(UNDECLARED).grounding is None
        assert probes(factory) == []


class TestRepair:
    def test_a_wrong_column_gives_the_right_answer(self, repair):
        result = repair.tools.query_data(WRONG)
        assert result["rows"] == [
            {"name": "Ann", "total": 2.0},
            {"name": "Bo", "total": 12.0},
            {"name": "Cy", "total": 1.0},
        ]
        assert result["grounding"]["joins"][0]["repaired"] is True
        assert '"customer_id"' in result["grounding"]["sql"]

    def test_the_table_in_between_is_loaded_and_joined(self, repair):
        assert records(repair, BRIDGED) == [
            {"name": "Ann", "qty": 9},
            {"name": "Bo", "qty": 5},
            {"name": "Cy", "qty": 1},
        ]
        assert repair.catalog.is_loaded("db.orders")

    def test_a_join_nothing_vouches_for_is_refused_and_nothing_is_loaded(self, repair, audit):
        with pytest.raises(
            QueryError, match="Grounding: codes.code = customers.id matches no known"
        ):
            repair.query.sql(UNKNOWN)
        assert not repair.catalog.is_loaded("db.customers")
        assert audit.events[-1].status == 400

    def test_the_tool_turns_the_refusal_into_an_error_the_model_can_act_on(self, repair):
        error = repair.tools.execute("query_data", {"sql": UNKNOWN})["error"]
        assert "describe_table lists them" in error

    def test_what_could_not_be_repaired_refuses_the_whole_query(self, repair):
        sql = "SELECT 1 FROM db.items i, db.customers c WHERE i.order_id = c.id"
        with pytest.raises(QueryError, match="related only through items.order_id = orders.id"):
            repair.query.sql(sql)

    def test_an_undeclared_but_real_relationship_runs(self, repair):
        assert records(repair, UNDECLARED) == [{"n": 2}]


def tenant(name: str) -> AccessScope:
    rule = [{"column": "tenant", "value": "${tenant}"}]
    return AccessScope(
        actor=f"key-{name}",
        tenant=name,
        policy=build_policy({"customers": rule, "orders": rule}, caller={"tenant": name}),
    )


class TestWithAccessScopes:
    def test_a_table_the_repair_adds_is_read_through_the_callers_view(self, repair, audit):
        """items has no tenant column: only the orders joined in keep tenant b out."""
        assert records(repair, BRIDGED, scope=tenant("a")) == [
            {"name": "Ann", "qty": 9},
            {"name": "Cy", "qty": 1},
        ]
        event = audit.events[-1]
        assert event.table == "items,orders,customers"
        assert event.row_filtered

    def test_the_wrong_join_cannot_reach_another_tenant_either(self, advise):
        rows = records(
            advise,
            "SELECT c.name FROM db.orders o JOIN db.customers c ON o.id = c.id",
            scope=tenant("a"),
        )
        assert {r["name"] for r in rows} <= {"Ann", "Cy"}

    def test_a_masked_columns_values_are_not_compared_for_a_caller(self, advise, factory):
        masking = CatalogMasking(
            policy=MaskingPolicy(rules={"reference": "null"}),
            semantic_types={"visits": {"customer_ref": "reference"}},
        )
        scope = AccessScope(actor="x", policy=EMPTY_POLICY, masking={"db": masking}, masked=True)
        result = advise.query.sql(UNDECLARED, scope=scope)
        # Unmasked, the values vouch for this join; here nobody looked.
        assert [(v.verdict, v.overlap) for v in result.grounding.verdicts] == [(UNVERIFIED, None)]
        assert probes(factory) == []
        assert result.to_records() == [{"n": 0}]


class TestValueOverlap:
    def overlap(self, app, left: str, right: str) -> float | None:
        (lt, lc), (rt, rc) = left.split("."), right.split(".")
        return app.sources.value_overlap(TableRef("db", lt), lc, TableRef("db", rt), rc)

    def test_the_larger_direction_counts(self, advise):
        # Every visit names a customer; only two of three customers were visited.
        assert self.overlap(advise, "customers.id", "visits.customer_ref") == 1.0

    def test_disjoint_columns(self, advise):
        assert self.overlap(advise, "codes.code", "customers.id") == 0.0

    def test_columns_of_different_types_are_not_compared(self, advise, factory):
        assert self.overlap(advise, "customers.name", "orders.id") is None
        assert probes(factory) == []

    def test_a_source_that_cannot_filter_is_not_asked(self, advise, factory):
        factory.sources["db"].slices = False
        assert self.overlap(advise, "codes.code", "customers.id") is None

    def test_an_unknown_table_or_a_failed_read_decides_nothing(self, advise, factory):
        assert self.overlap(advise, "nope.id", "customers.id") is None
        factory.sources["db"].connected = False
        assert self.overlap(advise, "items.qty", "customers.id") is None

    def test_a_result_cut_off_by_the_row_limit_decides_nothing(self, advise, factory):
        factory.sources["db"].set_table(
            "hot", [{"id": i, "k": 1} for i in range(OVERLAP_MATCH_ROWS)]
        )
        factory.sources["db"].set_table("keys", [{"id": 1, "k": 1}, {"id": 2, "k": 2}])
        advise.sources.refresh_all(force=True)
        assert self.overlap(advise, "keys.k", "hot.k") is None

    def test_measured_again_after_the_data_changed(self, advise, factory):
        assert self.overlap(advise, "codes.code", "customers.id") == 0.0
        factory.sources["db"].set_table("codes", [{"id": 70, "code": 1}])
        advise.sources.refresh_all(force=True)
        assert self.overlap(advise, "codes.code", "customers.id") == 1.0
