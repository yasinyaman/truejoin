"""Row rules and masks on analytical queries: what a restricted caller can and cannot read.

Real DuckDB, the real rewriter and planner, a fake source. Every test asks the
same question from a different angle: is there a query that gets a row or a
raw value past the caller's scope?
"""

import pytest

from tests.fusion.fakes import FakeSourceFactory, ManualScheduler
from truejoin.application.olap.settings import Settings
from truejoin.bootstrap import build_app
from truejoin.domain.governance.access import PSEUDONYM_FUNCTION, UNRESTRICTED, AccessScope
from truejoin.domain.governance.masking import CatalogMasking, MaskingPolicy, mask_value
from truejoin.domain.governance.row_policy import EMPTY_POLICY, build_policy
from truejoin.domain.olap.errors import GuardrailViolation, QueryError

CUSTOMERS = [
    {"id": 1, "tenant": "a", "email": "alice@x.com", "tc_no": "12345678901", "amount": 10.0},
    {"id": 2, "tenant": "b", "email": "bob@y.org", "tc_no": "98765432109", "amount": 20.0},
    {"id": 3, "tenant": "a", "email": "cara@x.com", "tc_no": "55555555555", "amount": 5.0},
    {"id": 4, "tenant": "b", "email": "dan@y.org", "tc_no": "44444444444", "amount": 7.0},
]
NOTES = [{"id": 1, "customer_id": 1, "tenant": "a"}, {"id": 2, "customer_id": 2, "tenant": "b"}]
TABLES = {"customers": CUSTOMERS, "notes": NOTES}
RAW = {row[column] for row in CUSTOMERS for column in ("email", "tc_no")}

MASKING = CatalogMasking(
    policy=MaskingPolicy(
        rules={"email": "partial", "national_id": "last4"},
        by_role={"auditor": {"email": "hash"}},
        exempt_roles=("dpo",),
        hash_key=b"secret",
    ),
    semantic_types={"customers": {"email": "email", "tc_no": "national_id"}},
)


def tenant(name: str, masked: bool = False, roles: tuple[str, ...] = ()) -> AccessScope:
    rule = [{"column": "tenant", "value": "${tenant}"}]
    return AccessScope(
        actor=f"key-{name}",
        tenant=name,
        roles=roles,
        policy=build_policy({"customers": rule, "notes": rule}, caller={"tenant": name}),
        masking={"db": MASKING},
        masked=masked,
        request_id="req-1",
    )


MASKED = AccessScope(actor="analyst", policy=EMPTY_POLICY, masking={"db": MASKING}, masked=True)


class Audit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def audit():
    return Audit()


@pytest.fixture
def factory():
    return FakeSourceFactory()


@pytest.fixture
def app(tmp_path, factory, audit):
    settings = Settings(threads=1, memory_limit="256MB", backup_path=str(tmp_path / "b"))
    a = build_app(settings, scheduler=ManualScheduler(), source_factory=factory, audit=audit)
    a.sources.connect("db", {"type": "fake", "tables": TABLES})
    yield a
    a.close()


def rows(app, sql: str, scope: AccessScope, **kwargs) -> list[dict]:
    return app.query.sql(sql, scope=scope, **kwargs).to_records()


def everything_in(records: list[dict]) -> set:
    return {value for record in records for value in record.values()}


class TestRowRules:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM db.customers",
            "SELECT id FROM customers",
            "SELECT c.id FROM db.customers AS c WHERE c.tenant = 'b' OR 1 = 1",
            "SELECT id FROM (SELECT * FROM db.customers) AS s",
            "WITH x AS (SELECT * FROM db.customers) SELECT id FROM x",
            "SELECT id FROM db.customers UNION SELECT id FROM db.customers",
            "SELECT id FROM db.customers WHERE id IN (SELECT id FROM db.customers)",
            "SELECT o.id FROM db.customers AS c RIGHT JOIN db.customers AS o ON c.id = o.id",
            "SELECT id FROM db.customers AS c WHERE EXISTS "
            "(SELECT 1 FROM db.customers AS o WHERE o.id = c.id)",
        ],
    )
    def test_a_tenant_reads_only_its_rows_however_the_query_is_written(self, app, sql):
        assert sorted(r["id"] for r in rows(app, sql, tenant("a"))) == [1, 3]
        assert sorted(r["id"] for r in rows(app, sql, tenant("b"))) == [2, 4]

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM db.customers AS t, (SELECT 0 AS n) AS t",
            "SELECT id FROM (SELECT 0 AS n) AS customers, db.customers",
            "SELECT id FROM db.customers AS t, UNNEST([7]) AS t(z)",
            "SELECT id FROM (VALUES (1)) AS customers(v), db.customers",
            "WITH t AS (SELECT 7 AS z) SELECT id FROM t JOIN db.customers AS t ON TRUE",
            "WITH customers AS (SELECT 7 AS z) SELECT id FROM db.customers, customers",
        ],
    )
    def test_a_derived_table_sharing_the_name_does_not_hide_the_table(self, app, sql):
        try:
            got = rows(app, sql, tenant("a", masked=True))
        except (GuardrailViolation, QueryError):
            return
        assert got and {r["id"] for r in got} <= {1, 3}

    def test_a_rule_written_in_another_case_still_applies(self, app):
        rule = [{"column": "tenant", "value": "${tenant}"}]
        upper = AccessScope(policy=build_policy({"CUSTOMERS": rule}, caller={"tenant": "a"}))
        assert sorted(r["id"] for r in rows(app, "SELECT id FROM db.customers", upper)) == [1, 3]

    def test_an_empty_allow_list_reads_no_row_rather_than_failing(self, app):
        rule = [{"column": "tenant", "operator": "in", "value": []}]
        nobody = AccessScope(policy=build_policy({"customers": rule}, caller={}))
        assert rows(app, "SELECT id FROM db.customers", nobody) == []

    def test_asking_for_another_tenant_finds_nothing(self, app):
        assert rows(app, "SELECT id FROM db.customers WHERE tenant = 'b'", tenant("a")) == []

    def test_aggregates_count_the_tenants_rows(self, app):
        sql = "SELECT COUNT(*) AS n, SUM(amount) AS total FROM db.customers"
        assert rows(app, sql, tenant("a")) == [{"n": 2, "total": 15.0}]

    def test_a_join_restricts_both_sides(self, app):
        sql = "SELECT n.id FROM db.notes AS n LEFT JOIN db.customers AS c ON c.id = n.customer_id"
        assert rows(app, sql, tenant("b")) == [{"id": 2}]

    def test_the_rule_reaches_the_source_so_a_big_table_is_only_sliced(self, tmp_path, audit):
        factory = FakeSourceFactory()
        settings = Settings(threads=1, memory_limit="256MB", full_load_max_rows=10)
        app = build_app(settings, scheduler=ManualScheduler(), source_factory=factory, audit=audit)
        try:
            app.sources.connect(
                "db", {"type": "fake", "tables": TABLES, "row_estimates": {"customers": 1000}}
            )
            assert len(rows(app, "SELECT id FROM db.customers", tenant("a"))) == 2
            fetched = [call for call in factory.sources["db"].calls if call[0] == "fetch_slice"]
            assert [p.as_dict() for p in fetched[-1][2].predicates] == [
                {"column": "tenant", "op": "eq", "value": "a"}
            ]
            # Nothing to narrow it by for a caller without the rule: refused, not loaded.
            with pytest.raises(QueryError, match="customers"):
                rows(app, "SELECT id FROM db.customers", UNRESTRICTED)
        finally:
            app.close()

    def test_a_rule_that_needs_a_missing_tenant_refuses(self, app):
        nobody = AccessScope(policy=tenant("a").policy.__class__(tenant("a").policy.rules, {}))
        with pytest.raises(GuardrailViolation, match="tenant"):
            rows(app, "SELECT id FROM db.customers", nobody)


class TestMasks:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM db.customers",
            "SELECT email AS e, tc_no AS t FROM db.customers",
            "SELECT c.* FROM db.customers AS c",
            "SELECT MAX(email) AS m, MIN(tc_no) AS n FROM db.customers",
            "SELECT STRING_AGG(email, ',') AS all_of_them FROM db.customers",
            "SELECT email || '' AS e, SUBSTR(tc_no, 1, 11) AS t FROM db.customers",
            "SELECT FIRST_VALUE(email) OVER (ORDER BY id) AS f FROM db.customers",
            "WITH x AS (SELECT email AS a FROM db.customers) SELECT a FROM x",
            "SELECT (SELECT MAX(tc_no) FROM db.customers) AS t",
            "SELECT email FROM db.customers GROUP BY email",
        ],
    )
    def test_no_query_brings_a_raw_value_out(self, app, sql):
        seen = everything_in(rows(app, sql, MASKED))
        leaked = {raw for raw in RAW for value in seen if isinstance(value, str) and raw in value}
        assert not leaked

    @pytest.mark.parametrize(
        ("sql", "masked"),
        [
            ("SELECT * FROM db.customers", ("email", "tc_no")),
            ("SELECT email AS e, amount FROM customers", ("e",)),
            ("WITH c AS (SELECT tc_no AS t FROM db.customers) SELECT upper(t) AS u FROM c", ("u",)),
            ("SELECT COUNT(email) AS n, SUM(amount) AS s FROM db.customers", ()),
            (
                "SELECT n.id, c.email FROM db.notes n JOIN db.customers c ON c.id = n.customer_id",
                ("email",),
            ),
        ],
    )
    def test_the_result_says_which_columns_show_a_mask(self, app, sql, masked):
        assert app.query.sql(sql, scope=MASKED).masked == masked
        # Said again when the answer comes from the cache.
        assert app.query.sql(sql, scope=MASKED).masked == masked

    def test_nothing_is_said_masked_for_a_caller_who_sees_the_values(self, app):
        assert app.query.sql("SELECT * FROM db.customers", scope=UNRESTRICTED).masked == ()
        dpo = tenant("a", masked=True, roles=("dpo",))
        assert app.query.sql("SELECT email FROM db.customers", scope=dpo).masked == ()
        answer = app.tools.for_scope(MASKED).execute(
            "query_data", {"sql": "SELECT email FROM db.customers"}
        )
        assert answer["masked"] == ["email"]
        assert "masked" not in app.tools.execute(
            "query_data", {"sql": "SELECT email FROM db.customers"}
        )

    def test_the_masks_are_the_ones_the_table_routes_apply(self, app):
        got = rows(app, "SELECT id, email, tc_no FROM db.customers ORDER BY id", MASKED)
        assert got == [
            {
                "id": row["id"],
                "email": mask_value(row["email"], "partial"),
                "tc_no": mask_value(row["tc_no"], "last4"),
            }
            for row in CUSTOMERS
        ]

    def test_a_guess_at_a_masked_value_cannot_be_checked(self, app):
        hit = "SELECT COUNT(*) AS n FROM db.customers WHERE tc_no = '12345678901'"
        miss = "SELECT COUNT(*) AS n FROM db.customers WHERE tc_no <> '12345678901'"
        assert rows(app, hit, MASKED) == [{"n": 0}]
        assert rows(app, miss, MASKED) == [{"n": 4}]

    def test_a_condition_on_a_masked_column_never_narrows_the_fetch(self, tmp_path, audit):
        factory = FakeSourceFactory()
        settings = Settings(threads=1, memory_limit="256MB", full_load_max_rows=1)
        app = build_app(settings, scheduler=ManualScheduler(), source_factory=factory, audit=audit)
        try:
            app.sources.connect("db", {"type": "fake", "tables": TABLES})
            sql = "SELECT id FROM db.customers WHERE tc_no <> '12345678901' AND amount > 1"
            assert len(rows(app, sql, MASKED)) == 4
            fetched = [call for call in factory.sources["db"].calls if call[0] == "fetch_slice"]
            assert [p.column for p in fetched[-1][2].predicates] == ["amount"]
        finally:
            app.close()

    def test_an_exempt_role_reads_raw_values(self, app):
        dpo = AccessScope(roles=("dpo",), masking={"db": MASKING}, masked=False)
        assert rows(app, "SELECT email FROM db.customers WHERE id = 1", dpo) == [
            {"email": "alice@x.com"}
        ]

    def test_the_hash_mask_is_the_keyed_pseudonym(self, app):
        app.store.define_function(
            PSEUDONYM_FUNCTION, lambda text: mask_value(text, "hash", b"secret")
        )
        auditor = AccessScope(roles=("auditor",), masking={"db": MASKING}, masked=True)
        got = rows(app, "SELECT email FROM db.customers WHERE id = 1", auditor)
        assert got == [{"email": mask_value("alice@x.com", "hash", b"secret")}]

    def test_the_pseudonym_function_cannot_be_called_to_check_guesses(self, app):
        app.store.define_function(
            PSEUDONYM_FUNCTION, lambda text: mask_value(text, "hash", b"secret")
        )
        for sql in (
            f"SELECT {PSEUDONYM_FUNCTION}('alice@x.com') AS h",
            f"SELECT id FROM db.customers WHERE email = \"{PSEUDONYM_FUNCTION.upper()}\"('x')",
        ):
            with pytest.raises(GuardrailViolation, match="reserved for masking"):
                rows(app, sql, MASKED)

    def test_a_source_without_labels_is_refused_not_served_raw(self, app):
        unlabelled = AccessScope(masking={}, masked=True)
        with pytest.raises(GuardrailViolation, match="no masking labels"):
            rows(app, "SELECT email FROM db.customers", unlabelled)
        # A policy waiting for its catalog's approval is no labels either.
        waiting = AccessScope(
            masking={"db": CatalogMasking(policy=MASKING.policy, semantic_types={})}, masked=True
        )
        with pytest.raises(GuardrailViolation, match="no masking labels"):
            rows(app, "SELECT email FROM db.customers", waiting)

    def test_rows_and_masks_together(self, app):
        got = rows(app, "SELECT email FROM db.customers ORDER BY id", tenant("a", masked=True))
        assert got == [{"email": "a***@x.com"}, {"email": "c***@x.com"}]


class TestOutOfReach:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM mv_all",
            "SELECT * FROM db.nothing",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM duckdb_tables()",
            "SELECT * FROM range(3)",
            "SELECT * FROM db.customers TABLESAMPLE 50%",
            "SELECT * FROM customers WHERE id IN "
            "(WITH customers AS (SELECT 1 AS id) SELECT id FROM customers)",
            "WITH RECURSIVE mv_all AS (SELECT * FROM mv_all UNION ALL "
            "SELECT * FROM mv_all WHERE 1 = 0) SELECT * FROM mv_all",
        ],
    )
    def test_anything_but_a_plain_source_table_is_refused(self, app, sql):
        app.views.create("all", "SELECT * FROM db.customers")
        with pytest.raises(GuardrailViolation, match="Guardrail"):
            rows(app, sql, tenant("a"))

    def test_a_refusal_says_what_was_wrong_with_the_name(self, app):
        app.views.create("all", "SELECT * FROM db.customers")
        with pytest.raises(GuardrailViolation, match="Did you mean 'db.customers'"):
            rows(app, "SELECT * FROM db.custmers", tenant("a"))
        with pytest.raises(GuardrailViolation, match="Did you mean 'db.customers'"):
            rows(app, "SELECT * FROM custmers", tenant("a"))
        with pytest.raises(GuardrailViolation, match="no connected source has a table 'db.zzz'.$"):
            rows(app, "SELECT * FROM db.zzz", tenant("a"))
        with pytest.raises(GuardrailViolation, match="materialized view.*source tables"):
            rows(app, "SELECT * FROM mv_all", tenant("a"))

    def test_a_slice_cannot_be_read_by_its_own_name(self, app):
        app.tools.load_table("db.customers", where="tenant = 'b'")
        name = app.catalog.slices_of("db.customers")[0].table_name
        assert rows(app, f"SELECT COUNT(*) AS n FROM {name}", UNRESTRICTED) == [{"n": 2}]
        with pytest.raises(GuardrailViolation):
            rows(app, f"SELECT * FROM {name}", tenant("a"))

    def test_an_engine_without_a_rewriter_refuses_rather_than_serving(self, app):
        app.query._access = None
        with pytest.raises(GuardrailViolation, match="without an access rewriter"):
            rows(app, "SELECT 1", tenant("a"))


class TestCache:
    def test_two_tenants_never_share_an_entry(self, app):
        sql = "SELECT id FROM db.customers ORDER BY id"
        assert [r["id"] for r in rows(app, sql, tenant("a"))] == [1, 3]
        assert [r["id"] for r in rows(app, sql, tenant("b"))] == [2, 4]
        assert [r["id"] for r in rows(app, sql, UNRESTRICTED)] == [1, 2, 3, 4]
        assert app.query.sql(sql, scope=tenant("a")).from_cache
        assert [r["id"] for r in rows(app, sql, tenant("a"))] == [1, 3]

    def test_a_masked_caller_does_not_get_an_unmasked_entry(self, app):
        sql = "SELECT email FROM db.customers WHERE id = 1"
        assert rows(app, sql, UNRESTRICTED) == [{"email": "alice@x.com"}]
        assert rows(app, sql, MASKED) == [{"email": "a***@x.com"}]


class TestTools:
    def test_every_data_tool_runs_under_the_scope(self, app):
        tools = app.tools.for_scope(tenant("a", masked=True))
        assert tools.query_data("SELECT COUNT(*) AS n FROM db.customers")["rows"] == [{"n": 2}]
        found = tools.search_data("db.customers", "tenant", "%")
        assert sorted(r["id"] for r in found["rows"]) == [1, 3]
        assert not RAW & everything_in(found["rows"])
        grouped = tools.aggregate_data("db.customers", "tenant", "amount", "SUM")
        assert grouped["rows"] == [{"tenant": "a", "sum_amount": 15.0}]

    def test_the_source_shortcuts_are_not_taken(self, tmp_path, audit):
        factory = FakeSourceFactory()
        app = build_app(
            Settings(threads=1, memory_limit="256MB"),
            scheduler=ManualScheduler(),
            source_factory=factory,
            audit=audit,
        )
        try:
            app.sources.connect("db", {"type": "fake", "tables": TABLES, "pushdown": True})
            tools = app.tools.for_scope(tenant("a"))
            assert len(tools.search_data("db.customers", "tenant", "%")["rows"]) == 2
            assert len(tools.query_data("SELECT * FROM db.customers")["rows"]) == 2
            kinds = {call[0] for call in factory.sources["db"].calls}
            assert kinds.isdisjoint({"execute_query", "fetch_filtered"})
        finally:
            app.close()

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("create_view", {"name": "v", "sql": "SELECT * FROM db.customers"}),
            ("refresh_view", {"name": "v"}),
            ("load_table", {"table": "db.customers"}),
        ],
    )
    def test_tools_that_work_on_unrestricted_rows_are_refused(self, app, tool, arguments):
        result = app.tools.for_scope(tenant("a")).execute(tool, arguments)
        assert "Guardrail" in result["error"]

    def test_reading_a_table_again_is_refused(self, app):
        app.tools.load_table("db.customers")
        with pytest.raises(GuardrailViolation, match="refresh_table is not available"):
            app.tools.for_scope(tenant("a")).refresh_table("db.customers")
        assert app.tools.refresh_table("db.customers")["how"] == "reloaded"

    def test_other_callers_slices_are_not_listed(self, app):
        app.tools.load_table("db.customers", where="tc_no = '12345678901'")
        listed = str(app.tools.for_scope(tenant("a")).list_sources())
        assert "12345678901" not in listed
        assert "12345678901" in str(app.tools.list_sources())

    def test_a_row_ruled_table_does_not_say_how_many_rows_it_holds(self, app):
        """The table's size counts other tenants' rows: a caller held to row rules
        on it is told "not known", by the listing and by the description alike."""
        app.tools.query_data("SELECT * FROM db.customers")  # loaded, so its rows are counted

        def sizes(tools):
            return {
                t["name"]: (t["row_count"], t["row_estimate"])
                for s in tools.list_sources()["sources"]
                for t in s["tables"]
            }

        everyone = sizes(app.tools)
        assert everyone["db.customers"][0] == 4
        assert app.tools.describe_table("db.customers")["row_count"] != -1
        held = app.tools.for_scope(tenant("a"))
        assert sizes(held)["db.customers"] == (-1, -1)
        assert held.describe_table("db.customers")["row_count"] == -1
        # A table the caller's rules do not touch keeps its size.
        rule = [{"column": "tenant", "value": "a"}]
        customers_only = AccessScope(actor="c", policy=build_policy({"customers": rule}, caller={}))
        partly = sizes(app.tools.for_scope(customers_only))
        assert partly["db.customers"] == (-1, -1) and partly["db.notes"] == everyone["db.notes"]
        # A caller who is only masked sees every row, so the size tells them nothing new.
        assert sizes(app.tools.for_scope(MASKED))["db.customers"] == everyone["db.customers"]

    def test_a_size_that_was_not_told_is_not_audited_as_rows_read(self, app, audit):
        app.tools.for_scope(tenant("a")).execute("describe_table", {"table": "db.customers"})
        call = next(e for e in reversed(audit.events) if getattr(e, "tool", "") == "describe_table")
        assert call.row_count is None

    def test_binding_a_scope_leaves_the_shared_tools_alone(self, app):
        app.tools.for_scope(tenant("a"))
        assert len(app.tools.query_data("SELECT id FROM db.customers")["rows"]) == 4

    def test_readiness_names_what_is_missing(self, app):
        assert app.tools.check_readiness()["ready"]
        app.tools.readiness_checks.append(
            lambda: {"check": "catalogs_approved", "ok": False, "detail": "no approved catalog"}
        )
        result = app.tools.execute("check_readiness")
        assert not result["ready"]
        assert result["checks"][-1]["detail"] == "no approved catalog"


class TestMetrics:
    def test_a_metric_is_measured_over_the_tenants_rows(self, app):
        tools = app.tools.for_scope(tenant("a"))
        result = tools.query_metrics("db.customers", ["amount:sum"])
        assert result["rows"][0]["amount_sum"] == 15.0
        other = app.tools.for_scope(tenant("b")).query_metrics("db.customers", ["amount:sum"])
        assert other["rows"][0]["amount_sum"] == 27.0

    def test_a_masked_column_cannot_be_measured_or_grouped_by(self, app):
        tools = app.tools.for_scope(MASKED)
        refused = tools.execute(
            "query_metrics",
            {"table": "db.customers", "metrics": ["amount:sum"], "dimensions": ["email"]},
        )
        assert "email" in refused["error"] and "masked" in refused["error"]
        allowed = tools.query_metrics("db.customers", ["amount:sum"], ["tenant"])
        assert len(allowed["rows"]) == 2


class TestAudit:
    def test_one_event_per_query_says_who_and_what_was_withheld(self, app, audit):
        rows(app, "SELECT email FROM db.customers", tenant("a", masked=True))
        (event,) = audit.events
        assert (event.action, event.database, event.table) == ("query", "db", "customers")
        assert (event.actor, event.tenant, event.request_id) == ("key-a", "a", "req-1")
        assert (event.row_count, event.status) == (2, 200)
        assert event.row_filtered
        assert event.masked_columns == ("customers.email", "customers.tc_no")

    def test_an_unrestricted_read_is_recorded_as_one(self, app, audit):
        rows(app, "SELECT id FROM db.customers JOIN db.notes USING (id)", UNRESTRICTED)
        (event,) = audit.events
        assert event.table == "customers,notes"
        assert not event.restricted

    def test_a_refusal_is_recorded(self, app, audit):
        with pytest.raises(GuardrailViolation):
            rows(app, "SELECT * FROM mv_secret", tenant("a"))
        assert [e.status for e in audit.events] == [403]

    def test_a_cache_hit_is_still_an_access(self, app, audit):
        for _ in range(2):
            rows(app, "SELECT id FROM db.customers", tenant("a"))
        assert len(audit.events) == 2

    def test_metrics_and_source_shortcuts_are_recorded_too(self, app, audit):
        app.tools.for_scope(tenant("a")).query_metrics("db.customers", ["amount:sum"])
        assert audit.events[-1].row_filtered
        assert audit.events[-1].table == "customers"

    def test_the_event_carries_no_sql_and_no_values(self, app, audit):
        rows(app, "SELECT id FROM db.customers WHERE email = 'alice@x.com'", tenant("a"))
        assert "alice" not in str(audit.events[0].as_dict())
