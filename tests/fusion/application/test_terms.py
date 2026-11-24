"""The term layer on the engine: models from the catalog, joins along keys, questions in words.

Everything runs on real DuckDB over a fake source, and every answer is
checked against SQL written by hand.
"""

import pytest

from tests.fusion.fakes import FakeSourceFactory, ManualScheduler
from truejoin.application.olap.lexicon import _clean
from truejoin.application.olap.settings import Settings
from truejoin.bootstrap import build_app
from truejoin.domain.catalog import catalog_review
from truejoin.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from truejoin.domain.catalog.terms import ColumnAttribute, SqlAttribute, Term
from truejoin.domain.governance.access import AccessScope
from truejoin.domain.governance.masking import CatalogMasking, MaskingPolicy
from truejoin.domain.governance.row_policy import EMPTY_POLICY, build_policy
from truejoin.domain.olap.errors import GuardrailViolation, QueryError

TABLES = {
    "branches": [
        {"id": 1, "city": "Izmir", "region": "Ege"},
        {"id": 2, "city": "Ankara", "region": "Ic Anadolu"},
    ],
    "customers": [
        {"id": 1, "branch_id": 1, "segment": "gold", "tenant": "a", "email": "ann@x.com"},
        {"id": 2, "branch_id": 2, "segment": "basic", "tenant": "b", "email": "bo@y.org"},
        {"id": 3, "branch_id": 1, "segment": "basic", "tenant": "a", "email": "cy@x.com"},
    ],
    "orders": [
        {"id": 1, "customer_id": 1, "status": "paid", "total": 10.0, "placed_on": "2024-01-05"},
        {"id": 2, "customer_id": 1, "status": "void", "total": 4.0, "placed_on": "2024-01-20"},
        {"id": 3, "customer_id": 2, "status": "paid", "total": 7.0, "placed_on": "2024-02-02"},
        {"id": 4, "customer_id": 3, "status": "paid", "total": 1.0, "placed_on": "2024-02-09"},
        # No customer: an inner join leaves it out, as a hand-written one would.
        {"id": 5, "customer_id": None, "status": "paid", "total": 100.0, "placed_on": "2024-02-10"},
    ],
    # Two keys to one parent: nothing can tell which a question means.
    "transfers": [{"id": 1, "sender_id": 1, "receiver_id": 2, "amount": 5.0}],
}
FOREIGN_KEYS = {
    "customers": [("branch_id", "branches", "id")],
    "orders": [("customer_id", "customers", "id")],
    "transfers": [("sender_id", "customers", "id"), ("receiver_id", "customers", "id")],
}

PAID_TOTAL = "CASE WHEN status = 'paid' THEN total END"


def entry(name: str, **columns: tuple[str, list]) -> TableCatalogEntry:
    return TableCatalogEntry(
        table_name=name,
        description=LocalizedText(texts={"en": f"All the {name}"}),
        columns=[
            ColumnCatalogEntry(
                name=column,
                data_type=kind,
                description=LocalizedText(texts={"en": f"The {column} of {name}"}),
                sample_values=samples,
            )
            for column, (kind, samples) in columns.items()
        ],
    )


def attribute(table: str, column: str, *synonyms: str, **values: list[str]) -> ColumnAttribute:
    return ColumnAttribute(table=table, column=column, synonyms=list(synonyms), values=values)


def catalog(paid: str = "paid", certified: bool = True) -> DatabaseCatalog:
    """A reviewed catalog for TABLES. ``paid`` is how the vocabulary spells that status."""
    found = DatabaseCatalog(
        database_name="db",
        tables={
            "branches": entry(
                "branches", id=("integer", []), city=("varchar", []), region=("varchar", [])
            ),
            "customers": entry(
                "customers",
                id=("integer", []),
                branch_id=("integer", []),
                segment=("varchar", []),
                tenant=("varchar", []),
                email=("varchar", []),
            ),
            "orders": entry(
                "orders",
                id=("integer", []),
                customer_id=("integer", []),
                status=("varchar", []),
                total=("double", []),
                placed_on=("varchar", []),
            ),
        },
        terms={
            "Branch": Term(
                name="Branch",
                synonyms=["şube"],
                tables=["branches"],
                column_attributes=[
                    attribute("branches", "city", "şehir", Izmir=[], Ankara=[]),
                    attribute("branches", "region", "bölge"),
                ],
            ),
            "Customer": Term(
                name="Customer",
                synonyms=["müşteri"],
                tables=["customers"],
                column_attributes=[
                    attribute("customers", "segment", gold=["altın"], basic=["temel"]),
                    attribute("customers", "email", **{"ann@x.com": [], "bo@y.org": []}),
                ],
            ),
            "Order": Term(
                name="Order",
                synonyms=["sipariş"],
                tables=["orders"],
                column_attributes=[
                    attribute("orders", "total", "tutar", "amount"),
                    attribute("orders", "status", "durum", **{paid: ["ödenen"], "void": ["iptal"]}),
                ],
                sql_attributes=[
                    SqlAttribute(
                        name="paid_total", table="orders", expression=PAID_TOTAL, certified=True
                    ),
                    SqlAttribute(name="draft_total", table="orders", expression="total * 2"),
                ],
            ),
        },
    )
    catalog_review.approve_catalog(found)
    if certified:
        catalog_review.certify_terms(found)
    return found


class Audit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def audit():
    return Audit()


@pytest.fixture
def bare(tmp_path, audit):
    """The engine with keys declared and no catalog."""
    settings = Settings(
        threads=1, memory_limit="256MB", backup_path=str(tmp_path / "b"), grounding="repair"
    )
    app = build_app(
        settings, scheduler=ManualScheduler(), source_factory=FakeSourceFactory(), audit=audit
    )
    app.sources.connect("db", {"type": "fake", "tables": TABLES, "foreign_keys": FOREIGN_KEYS})
    yield app
    app.close()


@pytest.fixture
def app(bare):
    bare.semantic.use_catalog("db", catalog())
    return bare


def rows(result: dict) -> list[tuple]:
    assert "error" not in result, result
    return [tuple(row[c] for c in result["columns"]) for row in result["rows"]]


def sql(app, query: str) -> list[tuple]:
    return [tuple(r) for r in app.query.sql(query).rows]


def metrics(app, table: str, *names: str, **kwargs) -> dict:
    return app.tools.execute("query_metrics", {"table": table, "metrics": list(names), **kwargs})


def tenant(name: str) -> AccessScope:
    rule = [{"column": "tenant", "value": "${tenant}"}]
    return AccessScope(
        actor=f"key-{name}",
        tenant=name,
        policy=build_policy({"customers": rule}, caller={"tenant": name}),
    )


def masked(column: str) -> AccessScope:
    masking = CatalogMasking(
        policy=MaskingPolicy(rules={"secret": "null"}),
        semantic_types={"customers": {column: "secret"}},
    )
    return AccessScope(actor="x", policy=EMPTY_POLICY, masking={"db": masking}, masked=True)


class TestModelFromCatalog:
    def test_without_a_catalog_a_key_is_a_number_to_sum(self, bare):
        model = bare.semantic.model_for("db.orders")
        assert model.source == "inferred"
        assert model.measure("customer_id").default_agg == "SUM"

    def test_the_catalog_knows_a_key_is_counted_and_what_a_column_is(self, app):
        model = app.semantic.model_for("db.orders")
        assert model.source == "catalog"
        assert model.measure("customer_id").default_agg == "COUNT_DISTINCT"
        assert "numeric" in metrics(app, "db.orders", "customer_id:sum")["error"]
        assert model.measure("total").description == "The total of orders"
        assert rows(metrics(app, "db.orders", "customer_id:count_distinct")) == [(3,)]

    @pytest.mark.parametrize(
        "unreviewed",
        [
            catalog(certified=False),
            catalog().model_copy(update={"status": "draft"}),
            None,
        ],
    )
    def test_a_draft_or_an_uncertified_term_changes_nothing(self, bare, unreviewed):
        bare.semantic.use_catalog("db", unreviewed)
        assert bare.semantic.model_for("db.orders").source == "inferred"
        assert bare.semantic.term_tables() == []

    def test_a_configured_model_still_comes_first(self, app):
        from truejoin.domain.olap.measures import Measure

        app.semantic.define("db.orders", [Measure("revenue", "total")], [])
        assert app.semantic.model_for("db.orders").source == "explicit"

    def test_taking_the_catalog_away_goes_back_to_inferring(self, app):
        app.semantic.use_catalog("db", None)
        assert app.semantic.model_for("db.orders").source == "inferred"


class TestCertifiedExpressions:
    def test_a_certified_expression_is_a_measure(self, app):
        assert rows(metrics(app, "db.orders", "paid_total:sum", "total:sum")) == sql(
            app, f"SELECT SUM({PAID_TOTAL}), SUM(total) FROM db.orders"
        )
        described = app.tools.list_metrics("db.orders")
        paid = next(m for m in described["measures"] if m["name"] == "paid_total")
        assert paid["expression"] == PAID_TOTAL

    def test_it_groups_filters_and_transforms_like_any_other(self, app):
        result = metrics(
            app,
            "db.orders",
            "cumsum(paid_total:sum)",
            dimensions=["placed_on"],
            filters=[{"column": "customer_id", "op": "is_null", "value": False}],
        )
        assert [r[1] for r in rows(result)] == [10.0, 10.0, 17.0, 18.0]

    def test_one_nobody_certified_is_not_offered(self, app):
        assert (
            "Unknown measure 'draft_total'" in metrics(app, "db.orders", "draft_total:sum")["error"]
        )

    @pytest.mark.parametrize(
        ("expression", "why"),
        [
            ("(SELECT MAX(total) FROM db.orders)", "no subquery, aggregate or window"),
            ("SUM(total)", "no subquery, aggregate or window"),
            ("total + email", "'email' is not a column"),
            ("o.total", "is not a column of the table"),
            ("total +", "does not parse"),
        ],
    )
    def test_an_expression_that_is_not_a_scalar_over_its_table_is_refused(
        self, bare, expression, why
    ):
        reviewed = catalog()
        reviewed.terms["Order"].sql_attributes[0].expression = expression
        bare.semantic.use_catalog("db", reviewed)
        assert why in metrics(bare, "db.orders", "paid_total:sum")["error"]

    def test_one_that_names_no_column_does_not_take_the_model_with_it(self, bare):
        reviewed = catalog()
        reviewed.terms["Order"].sql_attributes[0].expression = "1 + 1"
        bare.semantic.use_catalog("db", reviewed)
        assert rows(metrics(bare, "db.orders", "total:sum")) == [(122.0,)]
        assert "Unknown measure" in metrics(bare, "db.orders", "paid_total:sum")["error"]


JOINED = (
    "FROM db.orders o JOIN db.customers c ON o.customer_id = c.id "
    "JOIN db.branches b ON c.branch_id = b.id"
)


class TestJoinsAlongKeys:
    """Needs no catalog: the keys the database declares are enough."""

    def test_a_dimension_of_a_referenced_table(self, bare):
        result = metrics(bare, "db.orders", "total:sum", dimensions=["customers.segment"])
        assert result["columns"] == ["customers_segment", "total_sum"]
        assert rows(result) == sql(
            bare, f"SELECT c.segment, SUM(o.total) {JOINED} GROUP BY 1 ORDER BY 1"
        )

    def test_two_steps_away_through_a_table_the_request_never_names(self, bare):
        result = metrics(
            bare,
            "orders",
            "*:count",
            "total:avg",
            dimensions=["branches.city", "status"],
            filters=[{"column": "customers.segment", "value": "basic"}],
            order_by="-total_avg",
        )
        assert rows(result) == sql(
            bare,
            f"SELECT b.city, o.status, COUNT(*), AVG(o.total) {JOINED} "
            f"WHERE c.segment = 'basic' GROUP BY 1, 2 ORDER BY 4 DESC",
        )

    def test_a_row_with_no_parent_is_left_out_as_in_a_written_join(self, bare):
        assert rows(metrics(bare, "db.orders", "total:sum")) == [(122.0,)]
        joined = metrics(
            bare,
            "db.orders",
            "total:sum",
            filters=[{"column": "branches.region", "op": "ne", "value": "x"}],
        )
        assert rows(joined) == [(22.0,)]

    def test_the_tables_own_columns_may_be_written_with_its_name(self, bare):
        assert rows(metrics(bare, "db.orders", "total:sum", dimensions=["orders.status"])) == rows(
            metrics(bare, "db.orders", "total:sum", dimensions=["status"])
        )

    def test_a_table_with_many_rows_per_row_is_refused_with_the_way_round(self, bare):
        error = metrics(bare, "db.customers", "*:count", dimensions=["orders.status"])["error"]
        assert "db.customers reaches db.branches" in error
        assert "table='db.orders'" in error

    def test_two_keys_to_one_table_are_not_guessed_between(self, bare):
        error = metrics(bare, "db.transfers", "amount:sum", dimensions=["customers.segment"])[
            "error"
        ]
        assert "reaches no other table" in error and "query_data" in error

    def test_a_column_the_other_table_does_not_have(self, bare):
        error = metrics(bare, "db.orders", "total:sum", dimensions=["customers.nope"])["error"]
        assert "Unknown dimension 'nope' for db.customers" in error
        error = metrics(
            bare, "db.orders", "total:sum", filters=[{"column": "customers.nope", "value": 1}]
        )["error"]
        assert "Unknown dimension 'nope' for db.customers" in error
        assert (
            "Unknown table" in metrics(bare, "db.orders", "*:count", dimensions=["nope.x"])["error"]
        )

    def test_a_ranking_by_a_column_of_another_table(self, bare):
        result = metrics(bare, "db.orders", "rank(total:sum)", dimensions=["customers.segment"])
        assert result["columns"] == ["customers_segment", "rank_total_sum"]
        assert rows(result) == [("basic", 2), ("gold", 1)]
        assert rows(result) == sql(
            bare,
            f"SELECT c.segment, RANK() OVER (ORDER BY SUM(o.total) DESC) {JOINED} "
            "GROUP BY 1 ORDER BY 1",
        )

    def test_a_running_total_along_a_column_two_steps_away(self, bare):
        result = metrics(bare, "db.orders", "cumsum(total:sum)", dimensions=["branches.city"])
        assert rows(result) == [("Ankara", 7.0), ("Izmir", 22.0)]

    def test_a_change_from_row_to_row_with_a_condition_on_another_table(self, bare):
        result = metrics(
            bare,
            "db.orders",
            "change(total:sum)",
            dimensions=["placed_on"],
            filters=[{"column": "branches.region", "value": "Ege"}],
        )
        # Ann's and Cy's orders, both in Izmir: 10, 4, then 1.
        assert [r[1] for r in rows(result)] == [None, -6.0, -3.0]

    def test_explain_shows_the_join_it_would_run(self, bare):
        explained = bare.semantic.explain("db.orders", ["total:sum"], ["branches.city"])
        assert explained["query"]["dimensions"] == ["branches.city"]
        assert (
            'JOIN "db"."customers"' in explained["sql"]
            and 'JOIN "db"."branches"' in explained["sql"]
        )


class TestJoinsUnderAccessRules:
    def test_a_tenant_sees_its_own_rows_of_the_joined_table(self, bare, audit):
        result = bare.tools.for_scope(tenant("a")).execute(
            "query_metrics",
            {"table": "db.orders", "metrics": ["total:sum"], "dimensions": ["customers.segment"]},
        )
        # Bo (tenant b, the only other basic customer) is not in the basic sum.
        assert rows(result) == [("basic", 1.0), ("gold", 14.0)]
        *_, event, call = audit.events  # the read, then the call that asked for it
        assert event.actor == "key-a" and event.row_filtered
        assert set(event.table.split(",")) == {"orders", "customers"}
        assert (call.tool, call.outcome, call.actor, call.table) == (
            "query_metrics",
            "answered",
            "key-a",
            "db.orders",
        )
        assert call.measures == ("total:sum",) and call.dimensions == ("customers.segment",)

    def test_a_ranking_over_a_join_sees_only_the_tenants_rows(self, bare, audit):
        result = bare.tools.for_scope(tenant("a")).execute(
            "query_metrics",
            {
                "table": "db.orders",
                "metrics": ["rank(total:sum)"],
                "dimensions": ["customers.segment"],
            },
        )
        # Without the rule Bo's 7 would be in basic; ranks are the same here, the sum is not.
        assert rows(result) == [("basic", 2), ("gold", 1)]
        assert audit.events[-2].row_filtered
        total = bare.tools.for_scope(tenant("a")).execute(
            "query_metrics",
            {
                "table": "db.orders",
                "metrics": ["cumsum(total:sum)"],
                "dimensions": ["customers.segment"],
            },
        )
        assert rows(total) == [("basic", 1.0), ("gold", 15.0)]

    @pytest.mark.parametrize(
        ("column", "request_"),
        [
            ("segment", {"dimensions": ["customers.segment"]}),
            ("segment", {"filters": [{"column": "customers.segment", "value": "gold"}]}),
            # The key the join runs on: masked, it would match nothing.
            ("id", {"dimensions": ["customers.tenant"]}),
        ],
    )
    def test_a_masked_column_is_refused_not_grouped_by(self, bare, column, request_):
        tools = bare.tools.for_scope(masked(column))
        error = tools.execute(
            "query_metrics", {"table": "db.orders", "metrics": ["*:count"], **request_}
        )["error"]
        assert f"{column} in db.customers is masked" in error

    def test_the_refusal_is_a_guardrail(self, bare):
        with pytest.raises(GuardrailViolation):
            bare.semantic.run(
                "db.orders", ["*:count"], ["customers.segment"], scope=masked("segment")
            )


class TestQuestionsInWords:
    def ask(self, app, question: str, scope=None) -> dict:
        asked = app.semantic.ask(question, scope) if scope else app.semantic.ask(question)
        return {k: v for k, v in asked.items() if v}

    def test_the_words_of_the_catalog_choose_the_measure(self, app):
        assert self.ask(app, "Toplam sipariş tutarı nedir?") == {
            "table": "db.orders",
            "metrics": [{"measure": "total", "aggregation": "sum"}],
        }

    def test_a_stored_value_becomes_a_condition(self, app):
        assert self.ask(app, "iptal edilen sipariş sayısı")["filters"] == [
            {"column": "status", "op": "eq", "value": "void"}
        ]

    def test_a_breakdown_from_a_table_two_keys_away(self, app):
        assert self.ask(app, "Şehir bazında toplam sipariş tutarı nedir?") == {
            "table": "db.orders",
            "metrics": [{"measure": "total", "aggregation": "sum"}],
            "dimensions": [{"column": "branches.city"}],
        }

    def test_a_sum_is_of_the_table_that_holds_the_number(self, app):
        asked = self.ask(app, "Altın müşterilerin ödenen sipariş tutarı toplamı nedir?")
        assert asked["table"] == "db.orders"
        assert asked["filters"] == [
            {"column": "status", "op": "eq", "value": "paid"},
            {"column": "customers.segment", "op": "eq", "value": "gold"},
        ]

    def test_a_count_is_of_the_noun_it_is_attached_to(self, app):
        assert self.ask(app, "Izmir'deki müşteri sayısı nedir?") == {
            "table": "db.customers",
            "metrics": [{"measure": "*", "aggregation": "count"}],
            "filters": [{"column": "branches.city", "op": "eq", "value": "Izmir"}],
        }

    def test_a_number_named_without_a_word_for_how_takes_its_own_aggregation(self, app):
        # No "toplam": the amount is still summed, not the rows counted by it.
        assert self.ask(app, "Şehir bazında sipariş tutarı") == {
            "table": "db.orders",
            "metrics": [{"measure": "total", "aggregation": "sum"}],
            "dimensions": [{"column": "branches.city"}],
        }
        # A count said in words stays a count.
        assert self.ask(app, "sipariş tutarı sayısı")["metrics"] == [
            {"measure": "*", "aggregation": "count"}
        ]

    def test_a_name_of_several_words_needs_all_of_them(self, bare):
        # "customer orders" names orders; "customers" alone does not. Scored on
        # its first word, it won: orders also has customer_id to add to it.
        reviewed = catalog()
        reviewed.terms["Order"].synonyms = ["sipariş", "customer orders"]
        bare.semantic.use_catalog("db", reviewed)
        assert self.ask(bare, "How many customers are there?")["table"] == "db.customers"
        assert self.ask(bare, "How many customer orders are there?")["table"] == "db.orders"

    def test_without_certified_terms_it_says_how_to_get_them(self, bare):
        with pytest.raises(QueryError, match="no connected table has a certified term"):
            bare.semantic.ask("toplam tutar")
        assert (
            "truejoin catalog review"
            in bare.tools.execute("answer_question", {"question": "toplam tutar"})["error"]
        )

    def test_a_masked_columns_values_are_not_matched_for_that_caller(self, app):
        question = "ann@x.com için sipariş sayısı"
        assert self.ask(app, question)["filters"] == [
            {"column": "customers.email", "op": "eq", "value": "ann@x.com"}
        ]
        # Whether the address exists must not show in how the question was read.
        assert "filters" not in self.ask(app, question, masked("email"))

    @pytest.mark.parametrize("nothing", ["", "   ", None, 7])
    def test_a_question_has_to_be_words(self, app, nothing):
        assert "in words" in app.tools.execute("answer_question", {"question": nothing})["error"]


class TestAnswerQuestion:
    def test_the_rows_come_with_how_the_question_was_read(self, app):
        answer = app.tools.execute(
            "answer_question", {"question": "Şehir bazında toplam sipariş tutarı nedir?"}
        )
        assert rows(answer) == sql(
            app, f"SELECT b.city, SUM(o.total) {JOINED} GROUP BY 1 ORDER BY 1"
        )
        assert answer["request"]["dimensions"] == ["branches.city"]
        assert answer["request"]["metrics"] == ["total:sum"]
        assert 'JOIN "db"."branches"' in answer["sql"]

    def test_each_column_of_the_answer_says_what_it_is(self, app):
        answer = app.tools.execute(
            "answer_question", {"question": "Şehir bazında toplam sipariş tutarı nedir?"}
        )
        labels = answer["labels"]
        assert set(labels) == set(answer["columns"])
        measure = next(v for v in labels.values() if v["kind"] == "measure")
        assert (measure["name"], measure["agg"], measure["transforms"]) == ("total", "sum", [])
        city = next(v for v in labels.values() if v["kind"] == "dimension")
        assert (city["name"], city["table"], city["grain"]) == ("city", "branches", None)
        count = app.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert [v["agg"] for v in count["labels"].values()] == ["count"]

    def test_a_question_about_one_table(self, app):
        answer = app.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert rows(answer) == [(4,)]
        assert answer["sql"].count("JOIN") == 0 and "literal_repairs" not in answer

    def test_a_restricted_caller_gets_its_own_rows(self, app):
        answer = app.tools.for_scope(tenant("a")).execute(
            "answer_question", {"question": "Şehir bazında toplam sipariş tutarı"}
        )
        assert rows(answer) == [("Izmir", 15.0)]

    def test_a_value_the_data_spells_differently_is_swapped_and_said(self, bare):
        # The catalog was written when the status read "Paid"; the data says "paid".
        bare.semantic.use_catalog("db", catalog(paid="Paid"))
        answer = bare.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert answer["literal_repairs"] == [{"column": "status", "from": "Paid", "to": "paid"}]
        assert rows(answer) == [(4,)]
        assert "'paid'" in answer["sql"] and "'Paid'" not in answer["sql"]

    def test_a_question_it_reads_none_of_is_refused_not_guessed(self, app, audit):
        before = len(audit.events)
        answer = app.tools.execute("answer_question", {"question": "Kargo ücreti ne kadar?"})
        assert "Nothing in this question" in answer["error"]
        assert "kargo" in answer["error"] and "rows" not in answer
        # Refused before anything ran: one usage record, no read.
        assert [type(e).__name__ for e in audit.events[before:]] == ["ToolCall"]

    def test_a_question_read_in_part_says_which_words_it_left_out(self, app):
        answer = app.tools.execute(
            "answer_question", {"question": "kargo ile ödenen sipariş sayısı"}
        )
        assert rows(answer) == [(4,)] and answer["unread"] == ["kargo"]
        assert answer["unread_written"] == ["kargo"]
        whole = app.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert "unread" not in whole

    def test_a_value_the_data_holds_is_not_looked_up(self, app, monkeypatch):
        looked: list[str] = []
        real = type(app.tools).search_data

        def search(self, table, column, value, limit=20):
            looked.append(value)
            return real(self, table, column, value, limit)

        monkeypatch.setattr(type(app.tools), "search_data", search)
        answer = app.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert rows(answer) == [(4,)] and looked == []

    def test_a_check_that_cannot_be_made_does_not_cost_the_answer(self, bare, monkeypatch):
        bare.semantic.use_catalog("db", catalog(paid="Paid"))

        def too_big(*args, **kwargs):
            raise QueryError("db.orders is too large to search")

        monkeypatch.setattr(type(bare.tools), "search_data", too_big)
        answer = bare.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert rows(answer) == [(0,)] and "literal_repairs" not in answer

    def test_a_value_the_data_does_not_hold_at_all_is_left_as_asked(self, bare):
        bare.semantic.use_catalog("db", catalog(paid="settled"))
        answer = bare.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert rows(answer) == [(0,)] and "literal_repairs" not in answer


class TestTermTools:
    def test_search_finds_a_term_by_any_of_its_words(self, app):
        found = app.tools.execute("search_terms", {"query": "iptal siparişler"})
        assert [(t["term"], t["tables"], t["matched"]) for t in found["terms"]] == [
            ("Order", ["db.orders"], ["Order", "status"])
        ]
        assert app.tools.execute("search_terms", {"query": "zzz"}) == {"terms": [], "count": 0}

    def test_an_empty_search_lists_them_all(self, app):
        assert app.tools.execute("search_terms", {"query": ""})["count"] == 3

    def test_a_term_says_what_can_be_asked_of_it(self, app):
        term = app.tools.execute("get_term", {"name": "orders"})
        assert term["term"] == "Order" and term["synonyms"] == ["sipariş"]
        assert term["reaches"] == ["db.branches", "db.customers"]
        assert {"paid_total", "total", "*"} <= {m["name"] for m in term["measures"]}
        status = next(c for c in term["columns"] if c["column"] == "status")
        assert status["values"] == {"paid": ["ödenen"], "void": ["iptal"]}
        assert app.tools.execute("get_term", {"name": "Sipariş"}) != term

    def test_a_masked_columns_values_are_kept_from_that_caller(self, app):
        tools = app.tools.for_scope(masked("email"))
        term = tools.execute("get_term", {"name": "Customer"})
        values = {c["column"]: c["values"] for c in term["columns"]}
        assert values["email"] == {} and values["segment"] != {}
        assert tools.execute("search_terms", {"query": "ann@x.com"})["count"] == 0
        assert app.tools.execute("search_terms", {"query": "ann@x.com"})["count"] == 1

    def test_a_row_ruled_tables_values_are_kept_from_that_caller(self, app):
        tools = app.tools.for_scope(tenant("a"))
        term = tools.execute("get_term", {"name": "Customer"})
        assert all(c["values"] == {} for c in term["columns"])
        assert tools.execute("search_terms", {"query": "gold"})["count"] == 0

    def test_an_unknown_term_names_the_ones_there_are(self, app):
        error = app.tools.execute("get_term", {"name": "invoice"})["error"]
        assert "Certified terms: Branch, Customer, Order" in error

    def test_describe_table_adds_what_the_reviewer_signed_off(self, app, bare):
        described = app.tools.describe_table("db.orders")
        assert described["description"] == "All the orders"
        total = next(c for c in described["columns"] if c["name"] == "total")
        assert total["description"] == "The total of orders"
        assert "description" not in app.tools.describe_table("db.transfers")

    def test_readiness_says_whether_questions_in_words_can_be_asked(self, app, bare):
        check = next(
            c for c in app.tools.check_readiness()["checks"] if c["check"] == "certified_terms"
        )
        assert check["ok"] and "3 tables have a certified term" in check["detail"]
        app.semantic.use_catalog("db", None)
        check = next(
            c for c in app.tools.check_readiness()["checks"] if c["check"] == "certified_terms"
        )
        assert check["ok"] and "answer_question is unavailable" in check["detail"]


class TestGlossaryAndNotes:
    """A reviewer's abbreviations are written out before a question is matched; notes travel with the source."""

    @pytest.fixture
    def guided(self, bare):
        from truejoin.adapters.inbound.http.olap import load_catalog

        reviewed = catalog()
        reviewed.glossary = {"SP": "sipariş", "KDV dahil": "tutar"}
        reviewed.notes = ["Tutarlar TL cinsindendir."]
        load_catalog(bare, "db", reviewed, terms=True)
        return bare

    def test_an_abbreviation_is_written_out_and_said(self, guided):
        answer = guided.tools.execute("answer_question", {"question": "ödenen SP sayısı nedir?"})
        assert rows(answer) == [(4,)]
        assert answer["glossary"] == ["SP -> sipariş"]

    def test_the_longest_abbreviation_wins_and_only_whole_words(self):
        from truejoin.application.olap.lexicon import expand

        glossary = {"KDV": "vergi", "KDV dahil": "brut tutar", "SP": "siparis"}
        text, used = expand("KDV dahil SPOR toplamı", glossary)
        assert text == "brut tutar spor toplami"
        assert used == ["KDV dahil -> brut tutar"]

    def test_search_terms_reads_the_abbreviation_too(self, guided):
        found = guided.tools.execute("search_terms", {"query": "SP"})["terms"]
        assert [t["term"] for t in found] == ["Order"]

    def test_the_notes_come_with_the_source(self, guided):
        (source,) = guided.tools.execute("list_sources", {})["sources"]
        assert source["notes"] == ["Tutarlar TL cinsindendir."]

    def test_a_draft_catalog_gives_neither(self, bare):
        from truejoin.adapters.inbound.http.olap import load_catalog

        draft = catalog()
        draft.glossary, draft.notes = {"SP": "sipariş"}, ["x"]
        catalog_review.mark_as_draft(draft)
        load_catalog(bare, "db", draft, terms=True)
        assert bare.catalog.glossary == {} and bare.catalog.notes_of("db") == ()
        (source,) = bare.tools.execute("list_sources", {})["sources"]
        assert "notes" not in source


class TestUsageRecords:
    """Each tool call leaves the shape of what was asked; never a value, never the SQL."""

    def calls(self, audit) -> list:
        return [e for e in audit.events if type(e).__name__ == "ToolCall"]

    def test_a_question_is_recorded_by_its_reading_not_its_words(self, app, audit):
        app.catalog.set_guidance("db", {"SP": "sipariş"}, [])
        app.tools.execute("answer_question", {"question": "ödenen SP sayısı nedir?"})
        (call,) = self.calls(audit)
        record = call.as_dict()
        assert (record["event"], record["tool"], record["outcome"]) == (
            "tool_call",
            "answer_question",
            "answered",
        )
        assert record["table"] == "db.orders" and record["database"] == "db"
        assert record["measures"] == ["*:count"] and record["filtered_columns"] == ["status"]
        assert record["glossary"] == ["SP"] and record["row_count"] == 1
        assert "question" not in record
        assert "paid" not in repr(record) and "ödenen" not in repr(record)  # no value, no words

    def test_the_words_are_kept_only_when_asked(self, app, audit):
        app.tools.keep_questions = True
        app.tools.execute("answer_question", {"question": "ödenen sipariş sayısı nedir?"})
        assert self.calls(audit)[0].question == "ödenen sipariş sayısı nedir?"

    def test_sql_is_never_kept_and_outcomes_are_told_apart(self, app, audit):
        app.tools.execute("query_data", {"sql": "SELECT * FROM db.orders WHERE status = 'void'"})
        app.tools.execute("query_data", {"sql": "SELECT * FROM db.orders WHERE status = 'nope'"})
        app.tools.for_scope(tenant("a")).execute("load_table", {"table": "db.orders"})
        app.tools.execute("query_data", {"sql": "SELECT nope FROM db.orders"})
        outcomes = [(c.tool, c.outcome) for c in self.calls(audit)]
        assert outcomes == [
            ("query_data", "answered"),
            ("query_data", "empty"),
            ("load_table", "refused"),
            ("query_data", "failed"),
        ]
        assert not any("void" in repr(c.as_dict()) for c in self.calls(audit))

    def test_a_search_that_finds_nothing_is_empty_and_a_bad_limit_is_failed(self, app, audit):
        app.tools.execute("search_terms", {"query": "zzzz"})
        app.tools.execute("find_examples", {"question": "kargo"})
        result = app.tools.execute("find_examples", {"question": "x", "limit": "many"})
        assert "limit must be a whole number" in result["error"]
        outcomes = [(c.tool, c.outcome) for c in self.calls(audit)]
        assert outcomes == [
            ("search_terms", "empty"),
            ("find_examples", "empty"),
            ("find_examples", "failed"),
        ]

    def test_a_search_and_an_aggregation_keep_their_column_names(self, app, audit):
        app.tools.execute(
            "search_data",
            {"table": "db.customers", "filter_column": "segment", "filter_value": "gold"},
        )
        app.tools.execute(
            "aggregate_data",
            {"table": "db.orders", "group_by": "status", "agg_column": "total", "agg_func": "SUM"},
        )
        search, aggregate = self.calls(audit)
        assert search.filtered_columns == ("segment",) and "gold" not in repr(search.as_dict())
        assert aggregate.dimensions == ("status",) and aggregate.measures == ("total:sum",)


class TestFindExamples:
    """Certified examples close to a question, and none a restricted caller should not see."""

    @pytest.fixture
    def examples(self, bare):
        from truejoin.adapters.inbound.http.olap import load_catalog
        from truejoin.domain.catalog.examples import QueryExample

        def example(name, question, sql, certified=True):
            return QueryExample(
                name=name,
                question=LocalizedText(texts={"tr": question}),
                sql=sql,
                tables=["db.orders"],
                certified=certified,
            )

        reviewed = catalog()
        reviewed.glossary = {"SP": "sipariş"}
        reviewed.examples = {
            "paid": example(
                "paid",
                "Ödenen sipariş tutarı",
                "SELECT sum(total) FROM db.orders WHERE status = 'paid'",
            ),
            "per_customer": example(
                "per_customer",
                "Müşteri başına sipariş sayısı",
                "SELECT customer_id, count(*) FROM db.orders GROUP BY 1",
            ),
            "draft": example(
                "draft", "Sipariş taslağı", "SELECT 1 FROM db.orders", certified=False
            ),
        }
        load_catalog(bare, "db", reviewed, terms=True)
        return bare

    def names(self, app, question="", scope=None):
        tools = app.tools.for_scope(scope) if scope else app.tools
        return [
            e["name"] for e in tools.execute("find_examples", {"question": question})["examples"]
        ]

    def test_the_closest_first_and_drafts_never(self, examples):
        assert self.names(examples, "ödenen sipariş tutarı nedir") == ["paid", "per_customer"]
        assert self.names(examples, "müşteri başına") == ["per_customer"]
        assert self.names(examples, "SP sayısı") == ["per_customer", "paid"]  # glossary first
        assert self.names(examples) == ["paid", "per_customer"]  # empty lists them
        assert self.names(examples, "kargo") == []

    def test_a_restricted_caller_never_sees_a_quoted_value(self, examples):
        assert self.names(examples, scope=tenant("a")) == ["per_customer"]

    def test_the_sql_comes_with_it(self, examples):
        (found,) = examples.tools.execute("find_examples", {"question": "müşteri başına"})[
            "examples"
        ]
        assert found["sql"].startswith("SELECT customer_id") and found["tables"] == ["db.orders"]


class TestCheckQuestion:
    """How much of a question the vocabulary covers, word by word, before anything runs."""

    def check(self, app, question, scope=None):
        tools = app.tools.for_scope(scope) if scope else app.tools
        return tools.execute("check_question", {"question": question})

    def test_every_word_recognised_and_the_reading_it_gives(self, app):
        result = self.check(app, "ödenen sipariş sayısı nedir?")
        assert result["covered"] == "all" and result["unknown"] == []
        assert {w["word"]: w["read_as"] for w in result["words"]} == {
            "odenen": ["value db.orders.status = paid"],
            "siparis": ["table db.orders"],
            "sayisi": ["aggregation count"],
        }
        assert result["reading"]["filters"] == [{"column": "status", "op": "eq", "value": "paid"}]

    def test_a_column_of_a_referenced_table_and_the_how_words(self, app):
        words = self.check(app, "şehir bazında toplam sipariş tutarı")["words"]
        assert [w["read_as"] for w in words] == [
            ["column db.branches.city"],
            ["breakdown"],
            ["aggregation sum"],
            ["table db.orders"],
            ["column db.orders.total"],
        ]

    def test_the_words_the_vocabulary_lacks_and_no_guessed_reading(self, app):
        result = self.check(app, "kargo firması bazında iade oranı")
        assert result["covered"] == "none"
        assert result["unknown"] == ["kargo", "firmasi", "iade", "orani"]
        # Each word also as it was typed: what a person recognises, and adds.
        assert [w["written"] for w in result["words"] if not w["read_as"]] == [
            "kargo",
            "firması",
            "iade",
            "oranı",
        ]
        assert "reading" not in result and "query_data" in result["advice"]

    def test_a_word_is_given_as_typed_without_its_suffix_or_its_case_lost(self, app):
        words = self.check(app, "İstanbul'daki ŞUBE Müdürünün siparişi")["words"]
        typed = {w["word"]: w["written"] for w in words}
        assert typed["istanbul"] == "İstanbul" and typed["mudurunun"] == "Müdürünün"
        assert typed["sube"] == "ŞUBE" and "daki" not in typed

    def test_part_of_it(self, app):
        result = self.check(app, "kargo ile gelen sipariş sayısı")
        assert result["covered"] == "part" and result["unknown"] == ["kargo", "gelen"]
        assert "kargo, gelen" in result["advice"]

    def test_inflection_suffixes_and_fillers_are_not_unknown_words(self, app):
        result = self.check(app, "Izmir'deki siparişlerin toplamını göster")
        assert result["covered"] == "all"
        assert [(w["word"], w["read_as"]) for w in result["words"]] == [
            ("izmir", ["value db.branches.city = Izmir"]),
            ("siparislerin", ["table db.orders"]),
            ("toplamini", ["aggregation sum"]),
        ]

    def test_the_glossary_is_written_out_first(self, app):
        app.catalog.set_guidance("db", {"SP": "sipariş"}, [])
        result = self.check(app, "SP sayısı")
        assert result["covered"] == "all" and result["glossary"] == ["SP -> sipariş"]

    def test_values_are_matched_as_answer_question_matches_them(self, app):
        # Row rules: the reading is the same one answer_question would show.
        word = self.check(app, "altın müşteri sayısı", scope=tenant("a"))["words"][0]
        assert word == {
            "word": "altin",
            "written": "altın",
            "read_as": ["value db.customers.segment = gold"],
        }
        # A masked column has no values to match, for either.
        word = self.check(app, "altın müşteri sayısı", scope=masked("segment"))["words"][0]
        assert word == {"word": "altin", "written": "altın", "read_as": []}

    def test_nothing_runs_and_nothing_is_read(self, app, audit):
        before = len(audit.events)
        self.check(app, "ödenen sipariş sayısı")
        calls = audit.events[before:]
        assert [type(e).__name__ for e in calls] == ["ToolCall"]
        assert "error" in self.check(app, "  ")


class TestPlannerReadsMore:
    """What the planner learned from check_question's misses on the bench, each read and run."""

    def reading(self, app, question):
        return app.tools.execute("check_question", {"question": question})["reading"]

    def answer(self, app, question):
        return app.tools.execute("answer_question", {"question": question})

    def test_two_breakdowns_in_the_order_they_are_named(self, app):
        assert self.reading(app, "segment ve durum kırılımında sipariş sayısı")["dimensions"] == [
            "customers.segment",
            "status",
        ]
        result = self.answer(app, "order count by status and segment")
        assert result["request"]["dimensions"] == ["status", "customers.segment"]
        assert sorted(rows(result)) == sorted(
            sql(app, f"SELECT o.status, c.segment, COUNT(*) {JOINED} GROUP BY 1, 2")
        )

    def test_a_ranking_is_read_in_rank_order(self, app):
        result = self.answer(app, "durum bazında toplam tutar sıralaması")
        assert result["request"]["metrics"] == ["rank(total:sum)"]
        assert rows(result) == [("paid", 1), ("void", 2)]

    def test_absence_is_a_condition_that_the_column_is_empty(self, app):
        result = self.answer(app, "How many orders without a customer?")
        assert result["request"]["filters"] == [
            {"column": "customer_id", "op": "is_null", "value": True}
        ]
        assert rows(result) == [(1,)]
        assert self.reading(app, "tutarı olmayan sipariş sayısı")["filters"] == [
            {"column": "total", "op": "is_null", "value": True}
        ]

    def test_a_short_code_counts_only_written_in_capitals(self, bare):
        from truejoin.application.olap.lexicon import LexiconPlanner

        planner = LexiconPlanner(
            bare.semantic, ["db.orders"], values={"db.orders": {"status": ["TRY", "USD"]}}
        )
        assert planner.plan("total in USD")["filters"] == [
            {"column": "status", "op": "eq", "value": "USD"}
        ]
        assert planner.plan("try the total")["filters"] == []
        # Folded on the way (the glossary does that), the question as written still decides.
        assert planner.plan("total in usd", written="total in USD")["filters"][0]["value"] == "USD"

    def test_what_distinct_counts_is_the_noun_after_it(self, app):
        reading = self.reading(app, "How many distinct customers placed an order?")
        assert reading["metrics"] == ["customer_id:count_distinct"]

    def test_the_top_n_of_a_thing_is_one_row_per_thing(self, app):
        result = self.answer(app, "sipariş sayısı en yüksek 2 müşteri")
        assert result["request"]["dimensions"] == ["customer_id"]
        assert result["request"]["limit"] == 2
        assert rows(result)[0] == (1, 2)  # customer 1 placed two
        own = self.reading(app, "en yüksek tutarlı 2 sipariş")
        assert own["dimensions"] == ["id"] and own["limit"] == 2


class TestUnreadToModel:
    """``olap.unread_questions: sql``: what the vocabulary reads none of goes to a model."""

    QUESTION = "Kargo ücreti ne kadar?"  # nothing in it is in the vocabulary

    def writer(self, app, reply, scope=None):
        prompts: list[tuple[str, str]] = []

        def write_sql(prompt: str, system: str) -> str:
            prompts.append((prompt, system))
            if isinstance(reply, Exception):
                raise reply
            return reply

        app.tools.write_sql = write_sql
        tools = app.tools.for_scope(scope) if scope else app.tools
        return tools, prompts

    def test_the_answer_says_a_model_wrote_it_and_shows_the_sql(self, app):
        tools, prompts = self.writer(app, "```sql\nSELECT COUNT(*) AS n FROM db.customers;\n```")
        answer = tools.execute("answer_question", {"question": self.QUESTION})
        assert rows(answer) == sql(app, "SELECT COUNT(*) FROM db.customers")
        assert answer["read_by"] == "model" and answer["unread"] == ["kargo", "ucreti", "kadar"]
        assert answer["sql"] == "SELECT COUNT(*) AS n FROM db.customers"
        prompt, system = prompts[0]
        assert "db.orders:" in prompt and "Question: Kargo ücreti ne kadar?" in prompt
        assert "status varchar (paid | void)" in prompt and "segment varchar (gold" in prompt
        assert "SELECT" in system

    def test_the_callers_rules_hold_for_the_models_sql(self, app):
        tools, _ = self.writer(app, "SELECT COUNT(*) AS n FROM db.customers", scope=tenant("a"))
        answer = tools.execute("answer_question", {"question": self.QUESTION})
        assert rows(answer) == sql(app, "SELECT COUNT(*) FROM db.customers WHERE tenant = 'a'")

    def test_a_masked_columns_values_are_not_shown_to_the_model(self, app):
        tools, prompts = self.writer(app, "SELECT 1 AS one", scope=masked("segment"))
        tools.execute("answer_question", {"question": self.QUESTION})
        assert "segment varchar," in prompts[0][0] and "gold" not in prompts[0][0]

    def test_a_model_not_trusted_with_samples_gets_no_values(self, app):
        tools, prompts = self.writer(app, "SELECT 1 AS one")
        app.tools.values_to_model = tools.values_to_model = False
        tools.execute("answer_question", {"question": self.QUESTION})
        assert "status varchar," in prompts[0][0] and "paid" not in prompts[0][0]

    def test_sql_the_guardrails_refuse_is_not_run(self, app):
        tools, _ = self.writer(app, "DROP TABLE db.orders")
        error = tools.execute("answer_question", {"question": self.QUESTION})["error"]
        assert "did not run" in error and "not read: kargo" in error

    def test_a_model_that_does_not_answer_is_said(self, app):
        tools, _ = self.writer(app, TimeoutError("no answer in 120 s"))
        error = tools.execute("answer_question", {"question": self.QUESTION})["error"]
        assert "did not answer" in error and "120 s" in error

    def test_what_the_vocabulary_reads_never_goes_to_the_model(self, app):
        tools, prompts = self.writer(app, "SELECT 1 AS one")
        assert "read_by" not in tools.execute(
            "answer_question", {"question": "ödenen sipariş sayısı"}
        )
        part = tools.execute("answer_question", {"question": "kargo ile ödenen sipariş sayısı"})
        first = tools.execute("answer_question", {"question": "şehir bazında sipariş sayısı"})
        tools.execute("answer_question", {"question": "ya ortalama?", "previous": first["request"]})
        assert "read_by" not in part and prompts == []


class TestRecognisedIsNotRead:
    """A word the vocabulary knows can still be left out of the reading: said, or refused."""

    #: Customers are counted, and the condition is on their orders: customers
    #: reference branches, not orders, so no one request holds both. It used
    #: to answer 3, every customer, with nothing said: each word was "read".
    APART = "siparişi iptal olan müşteri sayısı"

    def answer(self, app, question):
        return app.tools.execute("answer_question", {"question": question})

    def test_tables_no_one_request_joins_are_refused_not_guessed(self, app, audit):
        before = len(audit.events)
        answer = self.answer(app, self.APART)
        assert answer["error"].startswith("This question names more than one reading can hold")
        assert "read from db.customers" in answer["error"]
        assert "'siparişi' (db.orders)" in answer["error"] and "'iptal'" in answer["error"]
        # Refused before anything ran: one usage record, no read.
        assert "rows" not in answer
        assert [type(e).__name__ for e in audit.events[before:]] == ["ToolCall"]

    def test_the_check_says_so_before_it_is_asked(self, app):
        check = app.tools.execute("check_question", {"question": self.APART})
        assert check["covered"] == "all" and check["reading"]["table"] == "db.customers"
        assert check["apart"] == [
            {"word": "siparişi", "read_as": ["table db.orders"]},
            {"word": "iptal", "read_as": ["value db.orders.status = void"]},
        ]
        assert "answer_question refuses it" in check["advice"]

    def test_with_a_model_allowed_it_goes_to_the_model(self, app):
        app.tools.write_sql = lambda prompt, system: "SELECT COUNT(*) AS n FROM db.customers"
        answer = self.answer(app, self.APART)
        assert answer["read_by"] == "model" and "unread" in answer

    def test_a_reading_that_uses_no_naming_word_is_refused_not_guessed(self, app, audit):
        # "bölge" names branches.region; the reading counted branches and used
        # nothing of it. Seen in the console as "takip listesinden olan ... hareket
        # listesi" answered "count 500": "liste" matched watchlist.listed_on.
        before = len(audit.events)
        answer = self.answer(app, "bölge listesi")
        assert answer["error"].startswith("Nothing in this question")
        assert "bölge (db.branches)" in answer["error"] and "not read: listesi" in answer["error"]
        assert "rows" not in answer
        assert [type(e).__name__ for e in audit.events[before:]] == ["ToolCall"]
        check = app.tools.execute("check_question", {"question": "bölge listesi"})
        assert "answer_question refuses it" in check["advice"]

    def test_with_a_model_allowed_an_idle_reading_goes_to_the_model(self, app):
        app.tools.write_sql = lambda prompt, system: "SELECT region FROM db.branches"
        answer = self.answer(app, "bölge listesi")
        assert answer["read_by"] == "model"

    def test_a_known_word_the_reading_does_not_use_is_listed(self, app):
        # The customers table is within reach, and nothing of it is in the request.
        answer = self.answer(app, "müşterilerin toplam sipariş tutarı")
        assert rows(answer) == [(122.0,)] and "unread" not in answer
        assert answer["unused"] == [{"word": "müşterilerin", "read_as": ["table db.customers"]}]

    def test_a_number_the_reading_does_not_take_is_listed(self, app):
        # A year, a window, a count of things: never a filler word.
        answer = self.answer(app, "2024 toplam sipariş tutarı")
        assert answer["unused"] == [{"word": "2024", "read_as": ["number"]}]
        check = app.tools.execute("check_question", {"question": "son 3 sipariş tutarı"})
        assert {"word": "3", "read_as": ["number"]} in check["unused"]
        assert "no word added" in check["advice"]

    def test_a_grain_with_no_date_to_take_it_is_listed(self, app):
        # ``placed_on`` is text here, so "monthly" changes nothing: the total of every month.
        answer = self.answer(app, "aylık toplam sipariş tutarı")
        assert rows(answer) == [(122.0,)]
        assert answer["unused"] == [{"word": "aylık", "read_as": ["grain month"]}]

    def test_a_reading_that_uses_every_word_lists_none(self, app):
        for question in (
            "ödenen sipariş sayısı nedir?",
            "Şehir bazında toplam sipariş tutarı nedir?",
            "Altın müşterilerin ödenen sipariş tutarı toplamı nedir?",
            "Izmir'deki müşteri sayısı nedir?",
            "sipariş sayısı en yüksek 2 müşteri",  # the key stands for the customer
            "durum bazında toplam tutar sıralaması",
            "How many orders without a customer?",
        ):
            answer = self.answer(app, question)
            assert "error" not in answer and "unused" not in answer, (question, answer)

    def test_a_word_on_a_column_is_not_a_word_for_its_namesakes(self, bare):
        # Every table has an ``id``: "üye" put on the customers' says nothing of the orders'.
        reviewed = catalog()
        reviewed.terms["Customer"].column_attributes.append(attribute("customers", "id", "üye"))
        bare.semantic.use_catalog("db", reviewed)
        (word,) = bare.tools.execute("check_question", {"question": "üye"})["words"]
        assert word["read_as"] == ["column db.customers.id"]


class TestValueWords:
    def test_a_value_keeps_a_word_that_only_shares_its_stem(self):
        """A value is matched as a whole word: "standard" is not ``standart\\w*``."""
        words = {"standart": ["standard", "Standart", "ortak"]}
        assert _clean(words, by_stem=False) == {"standart": ("standard", "ortak")}
        # A column is matched by stem, so there the same word adds nothing.
        assert _clean(words) == {"standart": ("ortak",)}


class TestFollowUp:
    """A question that follows up on an earlier answer: its request, changed where the words say."""

    def ask(self, app, question, previous=None, scope=None):
        tools = app.tools.for_scope(scope) if scope else app.tools
        args = {"question": question, **({"previous": previous} if previous else {})}
        return tools.execute("answer_question", args)

    def test_a_breakdown_keeps_the_measure_and_the_condition(self, app):
        first = self.ask(app, "ödenen siparişlerin toplam tutarı")
        then = self.ask(app, "peki şehir bazında?", first["request"])
        whole = self.ask(app, "şehir bazında ödenen siparişlerin toplam tutarı")
        assert then["request"] == whole["request"]
        assert rows(then) == rows(whole)

    def test_a_condition_on_the_same_column_replaces_the_old_one(self, app):
        first = self.ask(app, "ödenen sipariş sayısı")
        then = self.ask(app, "iptal edilenler?", first["request"])
        statuses = [f["value"] for f in then["request"]["filters"] if f["column"] == "status"]
        assert statuses == ["void"] and rows(then) == [(1,)]

    def test_an_aggregation_alone_is_of_the_measure_there_was(self, app):
        first = self.ask(app, "şehir bazında toplam sipariş tutarı")
        then = self.ask(app, "ya ortalama?", first["request"])
        assert then["request"]["metrics"] == ["total:avg"]
        assert then["request"]["dimensions"] == first["request"]["dimensions"]

    def test_and_adds_a_breakdown_beside_the_one_there_was(self, app):
        first = self.ask(app, "şehir bazında sipariş sayısı")
        then = self.ask(app, "ve durum bazında", first["request"])
        assert then["request"]["dimensions"] == ["branches.city", "status"]
        replaced = self.ask(app, "durum bazında", first["request"])
        assert replaced["request"]["dimensions"] == ["status"]

    def test_a_number_is_the_top_n_of_what_there_was(self, app):
        first = self.ask(app, "şehir bazında toplam sipariş tutarı")
        then = self.ask(app, "en yüksek 1", first["request"])
        assert then["request"]["metrics"] == ["total:sum"]
        assert then["request"]["limit"] == 1 and then["request"]["order_by"] == "-total_sum"
        assert len(rows(then)) == 1

    def test_the_callers_rules_hold_for_a_follow_up(self, app):
        first = self.ask(app, "toplam sipariş tutarı", scope=tenant("a"))
        then = self.ask(app, "şehir bazında", first["request"], scope=tenant("a"))
        assert rows(then) == [("Izmir", 15.0)]

    def test_what_cannot_be_followed_up(self, app):
        assert "earlier answer" in self.ask(app, "peki?", {"table": "db.nope"})["error"]
        assert "follow-up" in self.ask(app, "  ", {"table": "db.orders"})["error"]
        broken = {"table": "db.orders", "metrics": ["nope:"]}
        assert "error" in self.ask(app, "şehir bazında", broken)


class TestVocabularyGaps:
    """The words people asked with that the vocabulary does not know, from the usage counts."""

    ASKED = {
        "ödenen sipariş sayısı nedir?": [3, 0],
        "kargo firmasına göre sipariş sayısı": [2, 2],
        "kargo ücreti toplamı": [1, 1],
        "altın müşteri sayısı": [1, 0],
    }

    def test_the_most_missed_word_first_with_a_question_it_came_in(self, app):
        found = app.tools.vocabulary_gaps(self.ASKED)
        assert found["questions"] == 4
        assert found["covered"] == {"all": 4, "part": 2, "none": 1}
        first, *rest = found["gaps"]
        assert first == {
            "word": "kargo",
            "questions": 2,
            "asked": 3,
            "unanswered": 3,
            "example": "kargo firmasına göre sipariş sayısı",
        }
        assert [g["word"] for g in rest] == ["firmasina", "ucreti"]
        assert app.tools.vocabulary_gaps(self.ASKED, top=1)["gaps"] == [first]

    def test_read_as_the_caller_a_masked_value_is_a_gap(self, app):
        found = app.tools.for_scope(masked("segment")).vocabulary_gaps(self.ASKED)
        assert "altin" in [g["word"] for g in found["gaps"]]
        assert "altin" not in [g["word"] for g in app.tools.vocabulary_gaps(self.ASKED)["gaps"]]

    def test_a_word_written_out_in_the_glossary_is_no_longer_a_gap(self, app):
        app.catalog.set_guidance("db", {"kargo firmasına": "şube"}, [])
        found = app.tools.vocabulary_gaps(self.ASKED)
        assert [g["word"] for g in found["gaps"]] == ["kargo", "ucreti"]
        assert found["gaps"][0]["questions"] == 1

    def test_without_a_vocabulary_there_is_nothing_to_measure(self, bare):
        with pytest.raises(QueryError, match="No certified vocabulary"):
            bare.tools.vocabulary_gaps(self.ASKED)
