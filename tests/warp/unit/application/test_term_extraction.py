"""Term extraction: the model attaches words to names that exist, and to nothing else."""

import json

import pytest

from trio.application.catalog.catalog_analysis import CatalogAnalysisService
from trio.application.catalog.catalog_review import CatalogReviewService
from trio.application.catalog.term_extraction import (
    build_prompt,
    build_term,
    extract_terms,
    stored_values,
)
from trio.application.settings import Settings
from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)


def table(name: str = "orders") -> TableCatalogEntry:
    return TableCatalogEntry(
        table_name=name,
        human_name=LocalizedText(texts={"en": "Orders"}),
        description=LocalizedText(texts={"en": "Customer orders"}),
        columns=[
            ColumnCatalogEntry(name="id", data_type="integer", sample_values=[1, 2]),
            ColumnCatalogEntry(
                name="status",
                data_type="character varying",
                description=LocalizedText(texts={"en": "Order status"}),
                sample_values=["paid", "void", "paid", None],
            ),
        ],
    )


ANSWER = {
    "name": "Order",
    "description": "A purchase a customer made.",
    "synonyms": ["sipariş", "purchase", "orders"],
    "columns": {
        "status": {
            "synonyms": ["durum", "status"],
            "values": {"paid": ["ödendi", "paid"], "shipped": ["kargoda"]},
        },
        "ghost": {"synonyms": ["nothing"]},
    },
}


class Scripted:
    """A text generator that replays replies and records what it was asked."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def generate_json(self, prompt: str, system_prompt: str | None = None) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    async def generate(self, prompt: str, **_) -> str:
        return await self.generate_json(prompt)

    async def close(self) -> None:
        return None


class TestStoredValues:
    def test_only_text_columns_have_them_once_each(self):
        orders = table()
        assert stored_values(orders.get_column("status")) == ["paid", "void"]
        assert stored_values(orders.get_column("id")) == []


class TestPrompt:
    def test_it_lists_every_column_and_the_values_the_model_may_name(self):
        prompt = build_prompt(table(), ["tr", "en"], "en")
        assert "Table: orders" in prompt and "Known as: Orders" in prompt
        assert "- status (character varying): Order status Stored values: paid, void" in prompt
        assert "- id (integer)" in prompt
        assert "each of these languages: tr, en" in prompt


class TestBuildTerm:
    def test_words_land_on_the_names_that_exist(self):
        term = build_term(table(), ANSWER, "en")
        assert term.name == "Order" and term.tables == ["orders"] and not term.certified
        assert term.description.get("en") == "A purchase a customer made."
        # "orders" is the table's own name, so it is not a synonym for it.
        assert term.synonyms == ["sipariş", "purchase"]
        status = term.attribute("orders", "status")
        assert status.synonyms == ["durum"]
        # A value the column never stored is dropped; one the model skipped stays, wordless.
        assert status.values == {"paid": ["ödendi"], "void": []}

    def test_a_column_the_model_invented_is_dropped_and_a_skipped_one_kept(self):
        term = build_term(table(), ANSWER, "en")
        assert [a.column for a in term.column_attributes] == ["id", "status"]
        assert term.attribute("orders", "id").synonyms == []

    @pytest.mark.parametrize("raw", [None, [], "Order", {"columns": "all of them"}])
    def test_nonsense_gives_the_bare_term(self, raw):
        term = build_term(table(), raw, "en")
        assert term.name == "Orders" and term.synonyms == []
        assert term.description.get("en") == "Customer orders"
        assert term.attribute("orders", "status").values == {"paid": [], "void": []}


class TestExtractTerms:
    @pytest.mark.asyncio
    async def test_one_term_per_table_and_a_failure_costs_only_its_words(self):
        catalog = DatabaseCatalog(
            database_name="db", tables={"orders": table(), "refunds": table("refunds")}
        )
        model = Scripted(ANSWER, RuntimeError("model went away"))
        terms = await extract_terms(catalog, model, ["en"])
        assert list(terms) == ["Order", "Orders"]
        assert terms["Orders"].tables == ["refunds"] and terms["Orders"].synonyms == []
        assert len(model.prompts) == 2

    @pytest.mark.asyncio
    async def test_two_tables_given_one_name_keep_their_own(self):
        catalog = DatabaseCatalog(
            database_name="db", tables={"orders": table(), "refunds": table("refunds")}
        )
        terms = await extract_terms(catalog, Scripted(ANSWER, ANSWER), ["en"])
        assert list(terms) == ["Order", "refunds"]


class Repository:
    def __init__(self) -> None:
        self.saved: DatabaseCatalog | None = None

    def load(self, name: str) -> DatabaseCatalog | None:
        return self.saved.model_copy(deep=True) if self.saved else None

    def load_or_raise(self, name: str) -> DatabaseCatalog:
        assert self.saved is not None
        return self.saved.model_copy(deep=True)

    def save(self, catalog: DatabaseCatalog, format: str | None = None):
        self.saved = catalog.model_copy(deep=True)


class Gateway:
    async def get_tables(self):
        return ["orders"]

    async def get_table_schema(self, name):
        return {
            "columns": [
                {"name": "id", "type": "integer", "nullable": False},
                {"name": "status", "type": "varchar", "nullable": False},
            ],
            "foreign_keys": [],
            "indexes": [],
            "primary_key": "id",
        }


def analysis(terms: str, model, repository) -> CatalogAnalysisService:
    config = Settings(
        settings={
            "analysis": {"sample_limit": 0},
            "catalog": {"auto_cross_reference": False},
        },
        olap={"terms": terms},
    )
    return CatalogAnalysisService(
        gateway=Gateway(),
        config=config,
        text_generator=model,
        review=CatalogReviewService(repository),
        database_name="db",
    )


TABLE_ANALYSIS = {"table_description": {"en": "Orders"}, "columns": {}, "relationships": []}


class TestInAnalysis:
    @pytest.mark.asyncio
    async def test_no_term_is_written_while_the_layer_is_off(self):
        model = Scripted(TABLE_ANALYSIS)
        catalog = await analysis("off", model, Repository()).analyze()
        assert catalog.terms == {} and len(model.prompts) == 1

    @pytest.mark.asyncio
    async def test_on_it_costs_one_more_call_per_table(self):
        model = Scripted(TABLE_ANALYSIS, ANSWER)
        repository = Repository()
        catalog = await analysis("on", model, repository).analyze()
        assert list(catalog.terms) == ["Order"] and not catalog.terms["Order"].certified
        assert list(repository.saved.terms) == ["Order"]

    @pytest.mark.asyncio
    async def test_analysing_again_keeps_what_a_reviewer_certified(self):
        repository = Repository()
        await analysis("on", Scripted(TABLE_ANALYSIS, ANSWER), repository).analyze()
        review = CatalogReviewService(repository)
        review.update_term("db", "Order", {"synonyms": ["deal"]})
        review.approve_table("db", "orders")

        again = {**ANSWER, "name": "Purchase", "synonyms": ["something else"]}
        catalog = await analysis("on", Scripted(TABLE_ANALYSIS, again), repository).analyze()
        assert list(catalog.terms) == ["Order"]
        assert catalog.terms["Order"].synonyms == ["deal"] and catalog.terms["Order"].certified
        assert review.certify_terms("db") == 0

    @pytest.mark.asyncio
    async def test_translation_goes_to_the_lighter_model_when_there_is_one(self):
        heavy, light = Scripted(TABLE_ANALYSIS), Scripted("Siparişler")
        service = analysis("off", heavy, Repository())
        service.light_generator = light
        assert await service._translate_text("Orders", "en", "tr") == "Siparişler"
        assert len(light.prompts) == 1 and heavy.prompts == []
