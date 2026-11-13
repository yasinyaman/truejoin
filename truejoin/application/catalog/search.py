"""Searching the catalogs: tables, columns, business terms, measures and abbreviations.

What a reviewer or an analyst types to find where something lives: a table's
name or what people call it, a column's description, a term's synonym, a
measure's name. Every word of the query has to be there (in any language the
catalog holds), compared on the folded form questions are matched on, so
"musteri" finds "Müşteri". Stored values are not searched: what may be shown
of them depends on the caller, and a name is what is being looked for.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from truejoin.application.olap.lexicon import fold
from truejoin.domain.catalog.catalog import DatabaseCatalog
from truejoin.domain.catalog.localized import LocalizedText

#: How much each kind of field says about a hit: a name the query spells out
#: beats a description that mentions it in passing.
_NAME, _WORDS, _TEXT = 3, 2, 1


@dataclass
class Hit:
    """One thing the query found, and where to open it."""

    kind: str  # table | column | term | measure | abbreviation
    database: str
    name: str
    table: str = ""
    label: str = ""
    matched: str = ""
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "kind": self.kind,
            "database": self.database,
            "name": self.name,
            "table": self.table,
            "label": self.label,
            "matched": self.matched,
        }
        return {**out, **self.extra}


def _texts(value: LocalizedText | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, LocalizedText):
        return [text for text in value.texts.values() if text]
    return [value] if value else []


def _score(words: list[str], fields: Iterable[tuple[int, str]]) -> tuple[float, str]:
    """The best field holding every word, weighted; an exact or leading name ranks first."""
    best, matched = 0.0, ""
    for weight, text in fields:
        folded = fold(text)
        if not text or not all(word in folded for word in words):
            continue
        score = float(weight)
        joined = " ".join(words)
        if folded == joined:
            score += 2
        elif folded.startswith(joined):
            score += 1
        if score > best:
            best, matched = score, text
    return best, matched


def search_catalogs(
    catalogs: Iterable[DatabaseCatalog], query: str, lang: str = "en", limit: int = 50
) -> list[dict[str, Any]]:
    """What ``query`` names across ``catalogs``, best first, at most ``limit``.

    Each hit says its kind, database, table (for all but an abbreviation),
    a label in ``lang`` when there is one, and the text that matched.
    """
    words = fold(query).split()
    if not words:
        return []
    hits: list[Hit] = []

    def consider(hit: Hit, fields: list[tuple[int, str]]) -> None:
        hit.score, hit.matched = _score(words, fields)
        if hit.score:
            hits.append(hit)

    for catalog in catalogs:
        db = catalog.database_name
        for tname, table in catalog.tables.items():
            owner = catalog.table_owners.get(tname, "")
            consider(
                Hit(
                    "table",
                    db,
                    tname,
                    tname,
                    table.human_name.get(lang) or "",
                    extra={"owner": owner} if owner else {},
                ),
                [(_NAME, tname), (_WORDS, owner)]
                + [(_WORDS, t) for t in _texts(table.human_name)]
                + [(_WORDS, tag) for tag in table.tags]
                + [(_TEXT, t) for t in _texts(table.description)],
            )
            for column in table.columns:
                consider(
                    Hit(
                        "column",
                        db,
                        column.name,
                        tname,
                        column.description.get(lang) or "",
                        extra={"semantic_type": column.semantic_type},
                    ),
                    [(_NAME, column.name)]
                    + [(_WORDS, column.semantic_type or "")]
                    + [(_WORDS, tag) for tag in column.tags]
                    + [(_TEXT, t) for t in _texts(column.description)],
                )
        for term in catalog.terms.values():
            first = term.tables[0] if term.tables else ""
            owner = catalog.term_owners.get(term.name, "")
            consider(
                Hit(
                    "term",
                    db,
                    term.name,
                    first,
                    term.description.get(lang) or "",
                    extra={"certified": term.certified, **({"owner": owner} if owner else {})},
                ),
                [(_NAME, term.name), (_WORDS, owner)]
                + [(_WORDS, s) for s in term.synonyms]
                + [(_WORDS, s) for attribute in term.column_attributes for s in attribute.synonyms]
                + [(_TEXT, t) for t in _texts(term.description)],
            )
            for measure in term.sql_attributes:
                consider(
                    Hit(
                        "measure",
                        db,
                        measure.name,
                        measure.table,
                        measure.description.get(lang) or "",
                        extra={"certified": measure.certified},
                    ),
                    [(_NAME, measure.name)] + [(_TEXT, t) for t in _texts(measure.description)],
                )
        for short, long in catalog.glossary.items():
            consider(
                Hit("abbreviation", db, short, label=long),
                [(_NAME, short), (_WORDS, long)],
            )
    order = {"table": 0, "term": 1, "measure": 2, "column": 3, "abbreviation": 4}
    hits.sort(key=lambda h: (-h.score, order[h.kind], h.database, h.table, h.name))
    return [hit.as_dict() for hit in hits[:limit]]
