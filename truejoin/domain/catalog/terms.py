"""Business terms: what people call a table, its columns and the values they hold.

A term is written by a model once per table and then reviewed, like the rest
of the catalog. Until someone certifies it, nothing reads it: the semantic
model, the question planner and the published context all take certified
terms of an approved catalog and nothing else.
"""

from collections.abc import Iterable, Mapping
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator

from truejoin.domain.catalog.localized import LocalizedText


class MeasureError(ValueError):
    """A measure a reviewer wrote that cannot be one, with the reason why."""


#: How a certified expression is aggregated. Each is one aggregate of one
#: expression: a weighted average needs a second column and a ratio two
#: aggregates, so neither is one of these. A 1/0 flag is counted by its sum.
Aggregation = Literal["sum", "avg", "min", "max", "count", "count_distinct", "median"]
AGGREGATIONS: tuple[str, ...] = get_args(Aggregation)


class ColumnAttribute(BaseModel):
    """The words for one column, and for the values it stores."""

    table: str
    column: str
    synonyms: list[str] = Field(default_factory=list)
    values: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Stored value -> the words people use for it",
    )


class SqlAttribute(BaseModel):
    """A measure that is an expression over one table's columns.

    Certified separately from its term: an expression decides what a number
    means, which is a bigger claim than a synonym.
    """

    name: str = Field(..., description="The measure's name: an identifier")
    table: str
    expression: str = Field(..., description="A scalar SQL expression over the table's columns")
    description: LocalizedText = Field(default_factory=LocalizedText)
    source_sql_ids: list[str] = Field(
        default_factory=list, description="The statements it was mined from, when it was"
    )
    #: The one aggregation it is certified as. A catalog written before this
    #: field reads as sum, which is what every expression was then.
    aggregation: Aggregation = "sum"
    certified: bool = False

    @field_validator("aggregation", mode="before")
    @classmethod
    def _lower_case(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class Term(BaseModel):
    """One business concept: a name, the words for it, and where it lives."""

    name: str
    description: LocalizedText = Field(default_factory=LocalizedText)
    synonyms: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    column_attributes: list[ColumnAttribute] = Field(default_factory=list)
    sql_attributes: list[SqlAttribute] = Field(default_factory=list)
    certified: bool = False

    def attribute(self, table: str, column: str) -> ColumnAttribute | None:
        """The words for ``table.column``, if the term has any."""
        return next(
            (a for a in self.column_attributes if a.table == table and a.column == column), None
        )

    def certified_sql(self, table: str) -> list[SqlAttribute]:
        """The expressions on ``table`` a reviewer signed off."""
        return [a for a in self.sql_attributes if a.certified and a.table == table]


def clean_words(words: Any, *avoid: str) -> list[str]:
    """Words as a model or a person wrote them, made usable.

    Not a list becomes none; blanks, repeats and the name itself are dropped.
    """
    if not isinstance(words, list | tuple):
        return []
    seen = {word.strip().lower() for word in avoid}
    kept = []
    for word in words:
        text = str(word).strip() if isinstance(word, str | int | float) else ""
        if text and text.lower() not in seen:
            seen.add(text.lower())
            kept.append(text)
    return kept


def carry_over(
    fresh: Mapping[str, Term], previous: Iterable[Term], columns: Mapping[str, Iterable[str]]
) -> dict[str, Term]:
    """``fresh`` terms, with the certified ones of an earlier catalog kept as they were.

    A certified term is a reviewer's work, and re-analysing the database must
    not replace it with a new guess. It is kept when every table it covers is
    still there, minus the columns that are not. ``columns`` maps each table
    of the new catalog to its column names.
    """
    kept = dict(fresh)
    for term in previous:
        if not term.certified or not term.tables or any(t not in columns for t in term.tables):
            continue
        known = {table: set(columns[table]) for table in term.tables}
        survivor = term.model_copy(
            update={
                "column_attributes": [
                    a for a in term.column_attributes if a.column in known.get(a.table, ())
                ]
            }
        )
        for key in [k for k, t in kept.items() if set(t.tables) & set(term.tables)]:
            del kept[key]
        kept[term.name] = survivor
    return kept
