"""The term layer on the engine: models from the catalog, joins along keys, questions in words.

Everything runs on real DuckDB over a fake source, and every answer is
checked against SQL written by hand.
"""

import pytest

from tests.fusion.fakes import FakeSourceFactory, ManualScheduler
from trio.application.olap.settings import Settings
from trio.bootstrap import build_app
from trio.domain.catalog import catalog_review
from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from trio.domain.catalog.terms import ColumnAttribute, SqlAttribute, Term
from trio.domain.governance.access import AccessScope
from trio.domain.governance.masking import CatalogMasking, MaskingPolicy
from trio.domain.governance.row_policy import EMPTY_POLICY, build_policy
from trio.domain.olap.errors import GuardrailViolation, QueryError

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
        from trio.domain.olap.measures import Measure

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

    def test_a_transform_is_refused_rather_than_computed_over_a_join(self, bare):
        error = metrics(bare, "db.orders", "cumsum(total:sum)", dimensions=["customers.segment"])[
            "error"
        ]
        assert "cannot be combined with a column of another table" in error

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
        event = audit.events[-1]
        assert event.actor == "key-a" and event.row_filtered
        assert set(event.table.split(",")) == {"orders", "customers"}

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

    def test_without_certified_terms_it_says_how_to_get_them(self, bare):
        with pytest.raises(QueryError, match="no connected table has a certified term"):
            bare.semantic.ask("toplam tutar")
        assert (
            "trio catalog review"
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
