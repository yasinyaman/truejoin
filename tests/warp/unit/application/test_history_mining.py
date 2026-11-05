"""Mining a database's past queries for measures worth naming."""

import json

import pytest

from trio.adapters.outbound.sqlglot_expressions import SqlglotExpressionMiner
from trio.application.catalog.catalog_review import CatalogReviewService
from trio.application.catalog.history_mining import (
    HistoryMiningService,
    build_prompt,
    name_candidates,
    rank_candidates,
)
from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from trio.domain.catalog.history import HistoryEntry, HistoryUnavailableError
from trio.domain.catalog.terms import SqlAttribute, Term
from trio.domain.gateway.errors import CatalogNotFoundError

PAID = "CASE WHEN status = 'paid' THEN total END"
NET = "total - discount"


def catalog(terms: bool = True) -> DatabaseCatalog:
    def column(name: str, kind: str = "varchar", **flags) -> ColumnCatalogEntry:
        return ColumnCatalogEntry(name=name, data_type=kind, **flags)

    return DatabaseCatalog(
        database_name="shop",
        tables={
            "orders": TableCatalogEntry(
                table_name="orders",
                description=LocalizedText(texts={"en": "Customer orders"}),
                columns=[
                    column("id", "integer", is_primary_key=True),
                    column("customer_id", "integer", is_foreign_key=True),
                    column("status"),
                    column("total", "numeric"),
                    column("discount", "numeric"),
                    column("placed_on", "timestamp without time zone"),
                ],
            ),
            "customers": TableCatalogEntry(
                table_name="customers", columns=[column("id", "integer"), column("segment")]
            ),
        },
        terms={"Order": Term(name="Order", tables=["orders"])} if terms else {},
    )


HISTORY = [
    HistoryEntry(f"SELECT SUM({PAID}) FROM orders WHERE status <> 'void'", calls=40),
    HistoryEntry(f"SELECT status, SUM({PAID}) FROM orders GROUP BY 1", calls=15),
    HistoryEntry("SELECT * FROM orders WHERE id = 7 AND placed_on >= '2024-01-01'", calls=900),
    HistoryEntry("SELECT * FROM orders WHERE customer_id = 3", calls=500),
    HistoryEntry("SELECT COUNT(*) FROM customers WHERE segment = 'gold'", calls=5),
]
VIEWS = [HistoryEntry(f"SELECT id, {NET} AS net FROM orders", origin="view", name="v_net")]


def ranked(entries) -> list[tuple[str, str, int]]:
    found = rank_candidates(entries, SqlglotExpressionMiner(), "postgresql", catalog())
    return [(c.table, c.expression, c.calls) for c in found]


class TestRanking:
    def test_an_expression_is_counted_across_every_statement_that_holds_it(self):
        top = rank_candidates(HISTORY, SqlglotExpressionMiner(), "postgresql", catalog())[0]
        assert (top.expression, top.calls) == (PAID, 55)
        assert len(top.sources) == 2 and all(s.startswith("sql:") for s in top.sources)

    def test_the_most_run_comes_first(self):
        assert [calls for _, _, calls in ranked(HISTORY)] == [55, 40, 5]

    def test_a_key_or_a_moment_is_one_querys_choice_of_rows_however_often_it_ran(self):
        expressions = [expression for _, expression, _ in ranked(HISTORY)]
        assert not any("id = 7" in e or "placed_on" in e or "customer_id" in e for e in expressions)

    def test_inside_an_aggregate_a_key_or_a_date_is_kept(self):
        entries = [
            HistoryEntry("SELECT SUM(CASE WHEN customer_id IS NULL THEN total END) FROM orders")
        ]
        assert ranked(entries) == [("orders", "CASE WHEN customer_id IS NULL THEN total END", 1)]

    def test_a_column_a_view_derives_is_a_definition(self):
        assert ranked(VIEWS) == [("orders", NET, 1)]

    def test_arithmetic_on_a_key_or_a_date_is_not(self):
        entries = [HistoryEntry("SELECT id + 1, customer_id * 2, total / 2 FROM orders")]
        assert ranked(entries) == [("orders", "total / 2", 1)]

    def test_what_a_view_defines_comes_before_what_only_ran(self):
        first = rank_candidates(
            [*HISTORY, *VIEWS], SqlglotExpressionMiner(), "postgresql", catalog()
        )[0]
        assert (first.expression, first.in_view, first.sources) == (NET, True, ["view:v_net"])


class Scripted:
    def __init__(self, reply) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def generate_json(self, prompt: str, system_prompt: str | None = None) -> str:
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)

    async def close(self) -> None:
        return None


def candidates():
    return rank_candidates(HISTORY, SqlglotExpressionMiner(), "postgresql", catalog())


class TestNaming:
    def test_the_prompt_shows_the_expression_and_how_much_it_was_used(self):
        prompt = build_prompt(candidates(), catalog(), "en")
        assert f"1. table orders (Customer orders); ran 55 times\n   {PAID}" in prompt
        assert "Do not rewrite an expression" in prompt

    async def test_the_model_names_and_drops_and_cannot_touch_the_expression(self):
        model = Scripted(
            {
                "keep": [
                    {
                        "id": 1,
                        "name": "Paid Total!",
                        "description": "Total of paid orders.",
                        "expression": "DROP TABLE orders",
                    },
                    {"id": 99, "name": "ghost"},
                    "nonsense",
                ]
            }
        )
        (draft,) = await name_candidates(candidates(), catalog(), model)
        assert (draft.name, draft.table, draft.expression) == ("paid_total", "orders", PAID)
        assert draft.description.get("en") == "Total of paid orders."
        assert not draft.certified and len(draft.source_sql_ids) == 2

    @pytest.mark.parametrize("reply", [RuntimeError("no model"), "not json", {"kept": []}, []])
    async def test_a_model_that_fails_loses_its_vote_not_the_drafts(self, reply):
        drafts = await name_candidates(candidates(), catalog(), Scripted(reply))
        assert [d.name for d in drafts] == ["total_value", "status_flag", "segment_flag"]

    async def test_a_name_never_collides_with_a_column_or_another_expression(self):
        taken = catalog()
        taken.terms["Order"].sql_attributes.append(
            SqlAttribute(name="paid", table="orders", expression="1")
        )
        model = Scripted(
            {
                "keep": [
                    {"id": 1, "name": "status"},
                    {"id": 2, "name": "paid"},
                    {"id": 3, "name": "9x"},
                ]
            }
        )
        drafts = await name_candidates(candidates(), taken, model)
        assert [d.name for d in drafts] == ["status_2", "paid_2", "m_9x"]

    async def test_no_candidates_asks_nobody(self):
        model = Scripted({"keep": []})
        assert await name_candidates([], catalog(), model) == [] and model.prompts == []


class Repository:
    def __init__(self, stored: DatabaseCatalog | None) -> None:
        self.stored = stored

    def load(self, name):
        return self.stored.model_copy(deep=True) if self.stored else None

    def load_or_raise(self, name):
        if self.stored is None:
            raise CatalogNotFoundError(name)
        return self.stored.model_copy(deep=True)

    def save(self, found, format=None):
        self.stored = found.model_copy(deep=True)


class History:
    def __init__(self, statements=HISTORY, views=VIEWS) -> None:
        self.statements, self.views = statements, views
        self.asked: list[int] = []

    async def recent_statements(self, limit: int = 500):
        self.asked.append(limit)
        if isinstance(self.statements, Exception):
            raise self.statements
        return list(self.statements)

    async def view_definitions(self):
        if isinstance(self.views, Exception):
            raise self.views
        return list(self.views)


def service(repository, history=None, reply=None) -> HistoryMiningService:
    return HistoryMiningService(
        history=history or History(),
        miner=SqlglotExpressionMiner(),
        text_generator=Scripted(reply if reply is not None else RuntimeError("no model")),
        review=CatalogReviewService(repository),
        db_type="postgresql",
        database_name="shop",
    )


class TestSuggest:
    async def test_drafts_land_in_the_term_of_their_table_uncertified(self):
        repository = Repository(catalog())
        report = await service(repository).suggest()
        assert (report.statements, report.views, report.unavailable) == (5, 1, [])
        assert [(d.table, d.expression) for d in report.added] == [
            ("orders", NET),
            ("orders", PAID),
            ("orders", "CASE WHEN status <> 'void' THEN 1 ELSE 0 END"),
        ]
        stored = repository.stored.terms["Order"].sql_attributes
        assert [a.name for a in stored] == ["total_value", "total_value_2", "status_flag"]
        assert not any(a.certified for a in stored)

    async def test_a_table_without_a_term_is_named_not_dropped_silently(self):
        report = await service(Repository(catalog())).suggest()
        assert report.without_term == ["customers"]

    async def test_running_it_again_suggests_nothing_twice(self):
        repository = Repository(catalog())
        await service(repository).suggest()
        again = await service(repository).suggest()
        assert again.added == []
        assert len(repository.stored.terms["Order"].sql_attributes) == 3

    async def test_the_limit_keeps_the_most_used(self):
        report = await service(Repository(catalog())).suggest(limit=2, statements=50)
        assert [d.expression for d in report.added] == [NET, PAID]

    async def test_a_source_that_cannot_be_read_is_reported_and_the_other_still_used(self):
        refused = HistoryUnavailableError("needs GRANT SELECT ON V_$SQL")
        history = History(statements=refused)
        report = await service(Repository(catalog()), history).suggest()
        assert report.unavailable == ["needs GRANT SELECT ON V_$SQL"]
        assert [d.expression for d in report.added] == [NET]
        assert report.added[0].source_sql_ids == ["view:v_net"]

    async def test_a_source_that_was_not_asked_for_is_not_read(self):
        history = History(views=RuntimeError("must not be called"))
        report = await service(Repository(catalog()), history).suggest(use_views=False)
        assert report.views == 0 and len(report.added) == 2
        history = History()
        await service(Repository(catalog()), history).suggest(use_history=False)
        assert history.asked == []

    async def test_no_catalog_is_an_error(self):
        with pytest.raises(CatalogNotFoundError):
            await service(Repository(None)).suggest()
