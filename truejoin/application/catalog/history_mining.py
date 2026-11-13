"""Suggesting measures from what a database was asked before.

The flow, each step separately testable:

    read statements and view definitions -> mine expressions -> drop what is
    not a definition -> rank by use -> have a model name them -> store drafts

The model names and describes; it never writes or changes an expression. The
drafts land in the catalog as uncertified ``SqlAttribute``s, which nothing
reads until a reviewer certifies them (``truejoin catalog certify``).
"""

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from truejoin.application.catalog.catalog_review import CatalogReviewService
from truejoin.domain.catalog.catalog import DatabaseCatalog, LocalizedText, TableCatalogEntry
from truejoin.domain.catalog.history import (
    AGGREGATE,
    CONDITION,
    Candidate,
    HistoryEntry,
    HistoryUnavailableError,
)
from truejoin.domain.catalog.terms import SqlAttribute
from truejoin.ports.query_history import ExpressionMiner, QueryHistorySource
from truejoin.ports.text_generation import TextGenerator

logger = logging.getLogger(__name__)

_TEMPORAL = ("date", "time")
_NAME = re.compile(r"[^a-z0-9_]+")

SYSTEM_PROMPT = (
    "You review SQL expressions found in a database's query history and decide which "
    "are business definitions worth a name. Answer with one JSON object and nothing else."
)


@dataclass
class MiningReport:
    """What one run of ``suggest`` found and did."""

    statements: int = 0
    views: int = 0
    #: Why a source could not be read, in the database's own words.
    unavailable: list[str] = field(default_factory=list)
    #: The drafts added to the catalog.
    added: list[SqlAttribute] = field(default_factory=list)
    #: Tables with a usable expression and no term to attach it to.
    without_term: list[str] = field(default_factory=list)


def _is_definition(candidate: Candidate, table: TableCatalogEntry) -> bool:
    """Whether a mined condition says what something *is* rather than which rows were wanted.

    ``id = 7`` and ``created_at >= '2024-01-01'`` are one query's choice of
    rows: a key or a moment, not a definition. What sits inside an aggregate
    is kept whatever it reads; someone already chose to compute it. Arithmetic
    on a key or a date is held to the same rule as a condition.
    """
    if candidate.kind == AGGREGATE:
        return True
    for name in candidate.columns:
        column = table.get_column(name)
        if column is None or column.is_primary_key or column.is_foreign_key:
            return False
        if any(hint in column.data_type.lower() for hint in _TEMPORAL):
            return False
    return True


def rank_candidates(
    entries: Iterable[HistoryEntry],
    miner: ExpressionMiner,
    dialect: str,
    catalog: DatabaseCatalog,
) -> list[Candidate]:
    """Every distinct expression worth showing, the most used first.

    An expression a view defines comes before one that only ran: somebody
    wrote it down on purpose. Among the rest, the number of runs decides.
    """
    columns = {name: table.get_column_names() for name, table in catalog.tables.items()}
    found: dict[tuple[str, str], Candidate] = {}
    for entry in entries:
        for mined in miner.mine(entry.sql, dialect, columns):
            candidate = found.setdefault(
                (mined.table, mined.expression),
                Candidate(mined.table, mined.expression, mined.kind, mined.columns),
            )
            candidate.calls += entry.calls
            candidate.in_view = candidate.in_view or entry.origin == "view"
            if entry.id not in candidate.sources:
                candidate.sources.append(entry.id)
    kept = [c for c in found.values() if _is_definition(c, catalog.tables[c.table])]
    return sorted(kept, key=lambda c: (c.rank, c.expression), reverse=True)


def build_prompt(candidates: Sequence[Candidate], catalog: DatabaseCatalog, lang: str) -> str:
    """The question put to the model: which of these to keep, and what to call them."""
    lines = ["Expressions found in this database's queries and views:", ""]
    for number, candidate in enumerate(candidates, start=1):
        table = catalog.tables[candidate.table]
        about = table.description.get(lang) or str(table.description) or "-"
        where = "defined in a view" if candidate.in_view else f"ran {candidate.calls} times"
        lines += [
            f"{number}. table {candidate.table} ({about}); {where}",
            f"   {candidate.expression}",
        ]
    lines += [
        "",
        "Keep the ones a business would give a name to (a status that means paid, a net",
        "amount). Drop the ones that only say which rows one query happened to want.",
        "Answer with:",
        '{"keep": [{"id": <number>, "name": "<snake_case name>", "description": "<one sentence>"}]}',
        "Do not rewrite an expression: only its number, a name and a description.",
    ]
    return "\n".join(lines)


def _unique(wanted: str, taken: set[str]) -> str:
    """A snake_case identifier based on ``wanted`` that nothing else in the table has."""
    base = _NAME.sub("_", wanted.strip().lower()).strip("_") or "expression"
    if base[0].isdigit():
        base = f"m_{base}"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}_{number}", number + 1
    taken.add(name)
    return name


async def name_candidates(
    candidates: Sequence[Candidate],
    catalog: DatabaseCatalog,
    text_generator: TextGenerator,
    lang: str = "en",
) -> list[SqlAttribute]:
    """Draft attributes for the candidates the model kept, named as it named them.

    A model that fails, or answers nonsense, loses its vote: every candidate
    is then kept under a name made from what it reads. A reviewer decides
    either way, and a draft with a plain name is better than none.
    """
    kept: dict[int, dict[str, Any]] | None = None
    if candidates:
        try:
            reply = json.loads(
                await text_generator.generate_json(
                    prompt=build_prompt(candidates, catalog, lang), system_prompt=SYSTEM_PROMPT
                )
            )
            kept = {
                int(item["id"]): item
                for item in reply["keep"]
                if isinstance(item, dict) and str(item.get("id", "")).isdigit()
            }
        except Exception as e:
            logger.warning("No model verdict on %d mined expressions: %s", len(candidates), e)

    taken: dict[str, set[str]] = {}
    drafts = []
    for number, candidate in enumerate(candidates, start=1):
        verdict = {} if kept is None else kept.get(number)
        if verdict is None:
            continue
        names = taken.setdefault(
            candidate.table,
            {name.lower() for name in catalog.tables[candidate.table].get_column_names()}
            | {
                attribute.name
                for term in catalog.terms.values()
                for attribute in term.sql_attributes
                if attribute.table == candidate.table
            },
        )
        fallback = f"{candidate.columns[0]}_{'flag' if candidate.kind == CONDITION else 'value'}"
        described = verdict.get("description")
        drafts.append(
            SqlAttribute(
                name=_unique(str(verdict.get("name") or fallback), names),
                table=candidate.table,
                expression=candidate.expression,
                description=LocalizedText(
                    texts={lang: described.strip()} if isinstance(described, str) else {}
                ),
                source_sql_ids=candidate.sources[:10],
            )
        )
    return drafts


class HistoryMiningService:
    """Use case: suggest draft measures for a database from its own past queries."""

    def __init__(
        self,
        *,
        history: QueryHistorySource,
        miner: ExpressionMiner,
        text_generator: TextGenerator,
        review: CatalogReviewService,
        db_type: str,
        database_name: str,
        language: str = "en",
    ) -> None:
        """Store the collaborators; the composition root wires the adapters."""
        self.history = history
        self.miner = miner
        self.text_generator = text_generator
        self.review = review
        self.db_type = db_type
        self.database_name = database_name
        self.language = language

    async def suggest(
        self,
        limit: int = 20,
        statements: int = 500,
        use_history: bool = True,
        use_views: bool = True,
    ) -> MiningReport:
        """Mine, name and store up to ``limit`` drafts.

        Raises:
            CatalogNotFoundError: There is no catalog to attach drafts to.
        """
        catalog = self.review.load_or_raise(self.database_name)
        report = MiningReport()
        entries: list[HistoryEntry] = []
        if use_history:
            try:
                ran = await self.history.recent_statements(statements)
                report.statements = len(ran)
                entries += ran
            except HistoryUnavailableError as e:
                report.unavailable.append(str(e))
        if use_views:
            try:
                views = await self.history.view_definitions()
                report.views = len(views)
                entries += views
            except HistoryUnavailableError as e:
                report.unavailable.append(str(e))

        known = {
            (attribute.table, attribute.expression)
            for term in catalog.terms.values()
            for attribute in term.sql_attributes
        }
        covered = {table for term in catalog.terms.values() for table in term.tables}
        candidates = []
        for candidate in rank_candidates(entries, self.miner, self.db_type, catalog):
            if candidate.table not in covered:
                if candidate.table not in report.without_term:
                    report.without_term.append(candidate.table)
            elif (candidate.table, candidate.expression) not in known:
                candidates.append(candidate)
        drafts = await name_candidates(
            candidates[:limit], catalog, self.text_generator, self.language
        )
        if drafts:
            report.added = self.review.add_sql_attributes(self.database_name, drafts)
        return report
