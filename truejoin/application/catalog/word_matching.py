"""Where a word the vocabulary does not know belongs: proposed by a model, placed by a person.

A question the certified vocabulary cannot read names the words it missed,
and a steward adds each to the term or the column it means. Across languages
("sipariş" for Order) that is a look-up a model does well, so it is asked
here, once per request. It sees the words and the names of things: terms,
columns, their synonyms and the terms' descriptions. It never sees a stored
value, a sample or the question the words came from.

What comes back is a proposal and nothing more: every place is offered under
a key, a key that was not offered is dropped, and so is a word that was not
asked about. Nothing is written to a catalog here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from truejoin.application.gateway.localization import LANGUAGE_NAMES
from truejoin.domain.catalog.catalog import DatabaseCatalog
from truejoin.ports.text_generation import TextGenerator

SYSTEM_PROMPT = (
    "You match words from a question to the business vocabulary of a database. "
    "Answer with one JSON object and nothing else."
)

# shortcut: a vocabulary larger than this is cut off in the prompt, so its last
# terms are never proposed; rank the terms by the words first when one grows past it.
MAX_TERMS = 150
MAX_COLUMNS = 40
_DESCRIPTION = 160
_WHY = 200


@dataclass(frozen=True)
class Place:
    """Somewhere a word can be added: a term, or one of its columns."""

    database: str
    term: str
    column: str | None
    certified: bool


def _also(words: Sequence[str]) -> str:
    return f" (also: {', '.join(words)})" if words else ""


def offer(catalogs: Sequence[DatabaseCatalog]) -> tuple[dict[str, Place], str]:
    """Every place of ``catalogs`` under a key, and the listing a model reads.

    Names, synonyms and term descriptions only: the values a column stores,
    and the words for them, stay out.
    """
    places: dict[str, Place] = {}
    lines: list[str] = []
    terms = [(c.database_name, term) for c in catalogs for term in c.terms.values()]
    for number, (database, term) in enumerate(terms[:MAX_TERMS], start=1):
        key = f"T{number}"
        places[key] = Place(database, term.name, None, term.certified)
        about = next((text.strip() for text in term.description.texts.values() if text), "")
        about = f": {about[:_DESCRIPTION]}" if about else ""
        lines.append(f"{key} [{database}] {term.name}{_also(term.synonyms)}{about}")
        for index, attribute in enumerate(term.column_attributes[:MAX_COLUMNS], start=1):
            column_key = f"{key}.C{index}"
            places[column_key] = Place(database, term.name, attribute.column, term.certified)
            lines.append(f"  {column_key} {attribute.column}{_also(attribute.synonyms)}")
    return places, "\n".join(lines)


def build_prompt(words: Sequence[str], listing: str, language: str = "en") -> str:
    """The question put to the model: these words, these places, one place a word or none."""
    spoken = LANGUAGE_NAMES.get(language, language)
    return "\n".join(
        [
            "Words a question used that the vocabulary does not know:",
            *(f"- {word}" for word in words),
            "",
            "The vocabulary. Each line is a place with its key: a term (T1), "
            "or under it one of its columns (T1.C1).",
            listing,
            "",
            "For each word, give the place it means: a term's key when the word names "
            "what the term is about, a column's key when it names that column. A word "
            "may be in any language and inflected. Use null when no place fits (a verb, "
            "a filler, a word about something else): do not guess.",
            f"Write each reason in {spoken}, in one short sentence.",
            "Answer with:",
            '{"<word>": {"place": "<key>" or null, "why": "<reason>"}}',
        ]
    )


def read_reply(reply: str, places: dict[str, Place], words: Sequence[str]) -> dict[str, Any]:
    """The proposals in ``reply`` that name a word asked about and a place offered.

    Raises:
        ValueError: ``reply`` is not a JSON object.
    """
    try:
        said = json.loads(reply)
    except (TypeError, ValueError) as e:
        raise ValueError("The model's reply was not JSON") from e
    if not isinstance(said, dict):
        raise ValueError("The model's reply was not a JSON object")
    answers = {str(word).strip().casefold(): answer for word, answer in said.items()}
    suggestions: list[dict[str, Any]] = []
    unplaced: list[str] = []
    for word in words:
        answer = answers.get(word.casefold())
        key = answer.get("place") if isinstance(answer, dict) else None
        place = places.get(key.strip()) if isinstance(key, str) else None
        if place is None:
            unplaced.append(word)
            continue
        why = answer.get("why") if isinstance(answer, dict) else None
        suggestions.append(
            {
                "word": word,
                "database": place.database,
                "term": place.term,
                "column": place.column,
                "certified": place.certified,
                "why": why.strip()[:_WHY] if isinstance(why, str) else "",
            }
        )
    return {"suggestions": suggestions, "unplaced": unplaced}


def clean(words: Sequence[str]) -> list[str]:
    """``words`` without blanks and repeats, in the order asked."""
    kept: dict[str, str] = {}
    for word in words:
        if word.strip():
            kept.setdefault(word.strip().casefold(), word.strip())
    return list(kept.values())


async def suggest_places(
    words: Sequence[str],
    catalogs: Sequence[DatabaseCatalog],
    generator: TextGenerator,
    language: str = "en",
) -> dict[str, Any]:
    """For each of ``words``, the term or column of ``catalogs`` a model says it means.

    One model call. ``{"suggestions": [...], "unplaced": [...]}``: a word the
    model placed nowhere, or somewhere that was not offered, is unplaced.

    Raises:
        ValueError: The model's reply could not be read.
    """
    asked = clean(words)
    places, listing = offer(catalogs)
    if not asked or not places:
        return {"suggestions": [], "unplaced": asked}
    reply = await generator.generate_json(
        prompt=build_prompt(asked, listing, language), system_prompt=SYSTEM_PROMPT
    )
    return read_reply(reply, places, asked)
