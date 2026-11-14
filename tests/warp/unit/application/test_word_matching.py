"""A model proposes where an unread word belongs; only what was offered comes back."""

import json

import pytest

from truejoin.application.catalog.word_matching import (
    SYSTEM_PROMPT,
    build_prompt,
    clean,
    offer,
    read_reply,
    suggest_places,
)
from truejoin.domain.catalog.catalog import DatabaseCatalog, LocalizedText
from truejoin.domain.catalog.terms import ColumnAttribute, Term


def catalog(name: str = "shop") -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name=name,
        database_type="postgresql",
        terms={
            "Order": Term(
                name="Order",
                description=LocalizedText(texts={"en": "A purchase a customer made."}),
                synonyms=["purchase"],
                tables=["orders"],
                column_attributes=[
                    ColumnAttribute(
                        table="orders",
                        column="status",
                        synonyms=["state"],
                        values={"SECRETVALUE": ["ödendi"]},
                    ),
                    ColumnAttribute(table="orders", column="total"),
                ],
                certified=True,
            ),
            "User": Term(name="User", synonyms=["customer"], tables=["users"]),
        },
    )


class Model:
    """A text generator that replays one reply and keeps what it was asked."""

    def __init__(self, reply) -> None:
        self.reply = reply
        self.asked: list[tuple[str, str | None]] = []

    async def generate_json(self, prompt: str, system_prompt: str | None = None) -> str:
        self.asked.append((prompt, system_prompt))
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


class TestOffer:
    def test_every_term_and_column_gets_a_key(self):
        places, listing = offer([catalog()])
        assert list(places) == ["T1", "T1.C1", "T1.C2", "T2"]
        assert (places["T1"].term, places["T1"].column, places["T1"].certified) == (
            "Order",
            None,
            True,
        )
        assert (places["T1.C1"].column, places["T2"].certified) == ("status", False)
        assert "T1 [shop] Order (also: purchase): A purchase a customer made." in listing
        assert "  T1.C1 status (also: state)" in listing and "  T1.C2 total" in listing

    def test_stored_values_and_their_words_stay_out(self):
        _, listing = offer([catalog()])
        assert "SECRETVALUE" not in listing and "ödendi" not in listing

    def test_the_prompt_names_the_words_and_the_language_of_the_reasons(self):
        prompt = build_prompt(["sipariş", "müşteri"], "T1 [shop] Order", "tr")
        assert "- sipariş\n- müşteri" in prompt and "T1 [shop] Order" in prompt
        assert "Write each reason in Türkçe" in prompt and "do not guess" in prompt


class TestReadReply:
    def places(self):
        return offer([catalog()])[0]

    def test_a_word_goes_where_the_model_said(self):
        reply = json.dumps(
            {
                "sipariş": {"place": "T1", "why": "Order is a purchase."},
                "durum": {"place": "T1.C1", "why": "The state of an order."},
            }
        )
        found = read_reply(reply, self.places(), ["sipariş", "durum"])
        assert found["unplaced"] == []
        assert found["suggestions"] == [
            {
                "word": "sipariş",
                "database": "shop",
                "term": "Order",
                "column": None,
                "certified": True,
                "why": "Order is a purchase.",
            },
            {
                "word": "durum",
                "database": "shop",
                "term": "Order",
                "column": "status",
                "certified": True,
                "why": "The state of an order.",
            },
        ]

    def test_a_place_that_was_not_offered_is_no_place(self):
        reply = json.dumps({"sipariş": {"place": "T9"}, "veren": {"place": None}, "x": "T1"})
        found = read_reply(reply, self.places(), ["sipariş", "veren", "x"])
        assert found == {"suggestions": [], "unplaced": ["sipariş", "veren", "x"]}

    def test_a_word_nobody_asked_about_is_dropped(self):
        reply = json.dumps({"hacked": {"place": "T1"}, "Sipariş ": {"place": "T2", "why": 7}})
        found = read_reply(reply, self.places(), ["sipariş"])
        assert [(s["word"], s["term"], s["why"]) for s in found["suggestions"]] == [
            ("sipariş", "User", "")
        ]

    @pytest.mark.parametrize("reply", ["not json", "[1, 2]", ""])
    def test_a_reply_that_is_no_object_is_an_error(self, reply):
        with pytest.raises(ValueError, match="reply"):
            read_reply(reply, self.places(), ["sipariş"])


class TestSuggestPlaces:
    @pytest.mark.asyncio
    async def test_one_call_with_the_words_and_no_values(self):
        model = Model({"sipariş": {"place": "T1", "why": "ok"}})
        found = await suggest_places(["sipariş", " Sipariş", "veren"], [catalog()], model, "tr")
        assert [s["term"] for s in found["suggestions"]] == ["Order"]
        assert found["unplaced"] == ["veren"]
        ((prompt, system),) = model.asked
        assert system == SYSTEM_PROMPT and prompt.count("- sipariş") == 1
        assert "SECRETVALUE" not in prompt

    @pytest.mark.asyncio
    async def test_nothing_to_place_on_asks_nobody(self):
        model = Model({})
        empty = DatabaseCatalog(database_name="none", database_type="postgresql")
        assert await suggest_places(["sipariş"], [empty], model) == {
            "suggestions": [],
            "unplaced": ["sipariş"],
        }
        assert model.asked == []

    def test_words_are_asked_once_in_their_order(self):
        assert clean(["b", " a ", "B", "", "a"]) == ["b", "a"]
