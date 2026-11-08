"""Saved analyses: a question and the SQL that answers it, reviewed like a measure.

A model writes better SQL when it sees how this data is asked about: which
table holds the amount, how a status is spelled, what "active" means here.
An example is a steward's answer to one such question, written in the
engine's SQL (``source.table`` names, as ``query_data`` takes them), and like
an expression it is offered to nobody until it is certified.

Its SQL is shown to the models that call ``find_examples``. A string literal
in it may be another tenant's value (``WHERE tenant = 'b'``), so a caller with
row rules or masks is never shown an example that has one.
"""

from pydantic import BaseModel, Field

from trio.domain.catalog.localized import LocalizedText


class ExampleError(ValueError):
    """An example that cannot be saved, with the reason why."""


class QueryExample(BaseModel):
    """One question and the SQL a reviewer says answers it."""

    name: str = Field(..., description="An identifier, unique in the catalog")
    question: LocalizedText = Field(default_factory=LocalizedText)
    sql: str = Field(..., description="A SELECT over this catalog's tables, as query_data runs it")
    description: LocalizedText = Field(default_factory=LocalizedText)
    #: The tables it reads, as ``source.table``; found when it was saved.
    tables: list[str] = Field(default_factory=list)
    certified: bool = False

    @property
    def has_literals(self) -> bool:
        """Whether a quoted value appears in it: one a restricted caller must not see.

        Conservative on purpose: a quote anywhere (even in a comment) counts.
        """
        return "'" in self.sql
