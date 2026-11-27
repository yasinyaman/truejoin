"""A semantic model from elsewhere (Ossie, MetricFlow), read into what a catalog can hold.

A reader turns a file into a :class:`SemanticDraft`: tables, the words and
descriptions for them and their columns, single-column relationships, and
measures as one aggregation of one expression. What has no place in a catalog
(a ratio of two aggregates, a time spine, a composite key) is listed in
``skipped`` with the reason, never dropped in silence.

Nothing in it is trusted: ``import_semantic_model`` matches every name against
the database's own tables and columns, holds every expression to the rules a
measure follows, and stores the result as drafts a reviewer certifies.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from truejoin.domain.catalog.terms import Aggregation


class DraftColumn(BaseModel):
    name: str
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)


class DraftTable(BaseModel):
    name: str
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    columns: list[DraftColumn] = Field(default_factory=list)


class DraftRelationship(BaseModel):
    """``table.column`` references ``target_table.target_column`` (many to one)."""

    table: str
    column: str
    target_table: str
    target_column: str


class DraftMeasure(BaseModel):
    """One aggregation of one expression over one table's columns."""

    table: str
    name: str
    expression: str
    aggregation: Aggregation = "sum"
    description: str = ""


class Skipped(BaseModel):
    """Something in the model a catalog has no place for, and why."""

    item: str
    reason: str


class SemanticDraft(BaseModel):
    source: Literal["ossie", "metricflow"]
    name: str = ""
    description: str = ""
    tables: list[DraftTable] = Field(default_factory=list)
    relationships: list[DraftRelationship] = Field(default_factory=list)
    measures: list[DraftMeasure] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)


class ImportReport(BaseModel):
    """What an import put in the catalog, as drafts, and what it left out."""

    tables: list[str] = Field(default_factory=list)
    descriptions: int = 0
    words: int = 0
    relationships: int = 0
    measures: list[str] = Field(default_factory=list)
    notes: int = 0
    skipped: list[Skipped] = Field(default_factory=list)
