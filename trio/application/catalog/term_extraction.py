"""Writing the business terms of a catalog: one per table, by a model, once.

A language model is good at saying what people call things and bad at being
asked on every question. So it is asked here, per table, at analysis time; the
answer is stored in the catalog, reviewed and certified, and nothing asks
again. After NVIDIA Auto Ontology's term extraction, with one difference that
matters: nothing reads a term until a reviewer has certified it.

The model can only attach words to names that exist. Whatever else it
returns (a column the table does not have, a value that was never stored) is
dropped, and a column it skipped is still listed, with no words.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)
from trio.domain.catalog.terms import ColumnAttribute, Term, clean_words
from trio.ports.text_generation import TextGenerator

logger = logging.getLogger(__name__)

_TEXT_TYPES = ("char", "text", "string", "clob")

SYSTEM_PROMPT = (
    "You name the business concept a database table stores and list the words "
    "people use for it, its columns and the values they hold. "
    "Answer with one JSON object and nothing else."
)


def stored_values(column: ColumnCatalogEntry) -> list[str]:
    """The values of a text column the catalog kept, as the words a filter compares.

    Only text: a number or a date in a question is a quantity, not a name for
    something. The catalog keeps no samples of a column that looks personal,
    so those have no values here either.
    """
    if not any(hint in column.data_type.lower() for hint in _TEXT_TYPES):
        return []
    # shortcut: the catalog keeps a handful of samples per column, so a category
    # with more values than that is only partly known; read the distinct values
    # at analysis time when a reviewer finds one missing.
    return list(dict.fromkeys(str(v) for v in column.sample_values if v is not None))


def build_prompt(table: TableCatalogEntry, languages: Sequence[str], lang: str) -> str:
    """The question put to the model for one table."""
    lines = [
        f"Table: {table.table_name}",
        f"Known as: {table.human_name.get(lang) or '-'}",
        f"Description: {table.description.get(lang) or '-'}",
        "Columns:",
    ]
    for column in table.columns:
        line = f"- {column.name} ({column.data_type})"
        description = column.description.get(lang)
        if description:
            line += f": {description}"
        values = stored_values(column)
        if values:
            line += f" Stored values: {', '.join(values)}"
        lines.append(line)
    lines += [
        "",
        "Answer with (WORDS is an object with one list per language, "
        f"{json.dumps({language: ['...'] for language in languages}, ensure_ascii=False)}):",
        '{"name": "<the business concept, singular>", "description": "<one sentence>",',
        ' "synonyms": WORDS,',
        ' "columns": {"<column>": {"synonyms": WORDS, "values": {"<stored value>": WORDS}}}}',
        "",
        "Rules:",
        "- One entry under columns for every column listed, and no other.",
        "- A synonym is a word a person would type in a question, not the name again.",
        f"- Fill every language of WORDS ({', '.join(languages)}): a question may come in any.",
        "- Under values, only the stored values listed for that column.",
        "- An empty list is a valid answer.",
    ]
    return "\n".join(lines)


def _words(said: Any) -> Any:
    """The words of an answer, whether given as one list or as a list per language.

    Asked for per language, because a model asked for "both languages" in one
    list tends to write one of them.
    """
    if isinstance(said, dict):
        return [word for words in said.values() if isinstance(words, list) for word in words]
    return said


def build_term(table: TableCatalogEntry, raw: Any, lang: str) -> Term:
    """A term for ``table`` from whatever the model returned.

    ``raw`` that is not an object gives the bare term: the table's own name,
    every column and stored value, and no words for any of them.
    """
    answer = raw if isinstance(raw, dict) else {}
    name = answer.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else ""
    name = name or table.human_name.get(lang) or table.table_name
    description = answer.get("description")
    said = answer.get("columns")
    said = said if isinstance(said, dict) else {}

    attributes = []
    for column in table.columns:
        entry = said.get(column.name)
        entry = entry if isinstance(entry, dict) else {}
        words = entry.get("values")
        words = words if isinstance(words, dict) else {}
        attributes.append(
            ColumnAttribute(
                table=table.table_name,
                column=column.name,
                synonyms=clean_words(_words(entry.get("synonyms")), column.name),
                values={
                    value: clean_words(_words(words.get(value)), value)
                    for value in stored_values(column)
                },
            )
        )
    return Term(
        name=name,
        description=LocalizedText(texts={lang: description.strip()})
        if isinstance(description, str) and description.strip()
        else table.description.model_copy(deep=True),
        synonyms=clean_words(_words(answer.get("synonyms")), name, table.table_name),
        tables=[table.table_name],
        column_attributes=attributes,
    )


async def extract_terms(
    catalog: DatabaseCatalog,
    text_generator: TextGenerator,
    languages: Sequence[str] = ("en",),
) -> dict[str, Term]:
    """One uncertified term per table of ``catalog``, keyed by its name.

    A table the model fails on still gets its bare term, so the stored values
    stay known; two tables given the same name keep their table names instead.
    """
    lang = languages[0] if languages else "en"
    terms: dict[str, Term] = {}
    for table in catalog.tables.values():
        raw: Any = None
        try:
            reply = await text_generator.generate_json(
                prompt=build_prompt(table, languages or (lang,), lang),
                system_prompt=SYSTEM_PROMPT,
            )
            raw = json.loads(reply)
        except Exception as e:
            logger.warning(
                "No term written for %s, keeping its bare names: %s", table.table_name, e
            )
        term = build_term(table, raw, lang)
        if term.name in terms:
            term.name = table.table_name
        terms[term.name] = term
    return terms
