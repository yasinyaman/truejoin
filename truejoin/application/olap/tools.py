"""ToolService: the 16 LLM tools, dispatched by name.

Every method returns a JSON-serializable dict; failures become
``{"error": "..."}`` so MCP/REST/SDK callers get one uniform shape.
"""

from __future__ import annotations

import copy
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from truejoin.application.olap.lexicon import (
    STEM,
    LexiconPlanner,
    expand,
    fold,
    table_named,
    to_dsl,
)
from truejoin.application.olap.planner import FetchPlanner
from truejoin.application.olap.query import QueryService
from truejoin.application.olap.semantic import SemanticService
from truejoin.application.olap.sources import SourceService
from truejoin.application.olap.tool_schemas import TOOL_NAMES
from truejoin.application.olap.views import MaterializedViewService
from truejoin.domain.governance.access import UNRESTRICTED, AccessScope
from truejoin.domain.governance.audit import ToolCall, ToolOutcome
from truejoin.domain.governance.row_policy import PolicyError
from truejoin.domain.olap.catalog import SchemaCatalog
from truejoin.domain.olap.errors import BackupError, GuardrailViolation, QueryError, SchemaError
from truejoin.domain.olap.identifiers import (
    ALLOWED_AGG_FUNCS,
    MAX_RESULT_ROWS,
    is_valid_view_name,
    validate_identifier,
)
from truejoin.domain.olap.literal_repair import prove_substitution
from truejoin.domain.olap.models import MV_PREFIX, QueryResult, RowSet, TableRef
from truejoin.domain.olap.policy import TargetPlan
from truejoin.domain.olap.slices import Predicate, SliceSpec
from truejoin.ports.analytics_store import AnalyticsStore
from truejoin.ports.audit import AuditSink, NullAuditSink
from truejoin.ports.cache import QueryCache
from truejoin.ports.data_source import PushdownCapable
from truejoin.ports.sql_policy import SqlAnalyzer

_WHERE_HELP = (
    "load_table's 'where' must be an AND of simple conditions comparing a "
    "column to a literal (=, !=, <, <=, >, >=, LIKE, IN, IS NULL), for "
    "example \"status = 'paid' AND amount > 100\". OR, NOT, functions, "
    "subqueries and parameters cannot be sent to the source."
)

logger = logging.getLogger(__name__)


def _whole(value: Any, name: str) -> int:
    """A model's number argument, or a refusal it can read (not an internal error)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise QueryError(f"{name} must be a whole number, not {value!r}") from None


_UNREAD = "Nothing in this question is in the certified vocabulary (not read: {})."
#: A question that names tables no one request joins: the pilot and the console read this opening.
_APART = "This question names more than one reading can hold"
_WRITE_SQL = (
    "You write one read-only DuckDB SELECT that answers a business question over "
    "the tables below. Name every table as source.table, use only the columns "
    "listed, and reply with the SQL alone."
)


def _said(word: Mapping[str, Any]) -> dict[str, Any]:
    """A word as the asker typed it, with what it was recognised as."""
    return {"word": word["written"], "read_as": list(word["read_as"])}


def _apart(table: str, words: Sequence[Mapping[str, Any]]) -> str:
    """Why a question cannot be one request: what it names beyond ``table``'s reach."""
    named = ", ".join(
        f"'{word['written']}' ("
        + ", ".join(sorted({t for r in word["read_as"] if (t := table_named(r))}))
        + ")"
        for word in words
    )
    return (
        f"{_APART}: read from {table}, it would leave out {named}, which {table} "
        "does not reference, so the answer would be to another question."
    )


def _sql_in(reply: str) -> str:
    """The statement in a model's reply, without a code fence or a trailing semicolon."""
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", reply, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else reply).strip().rstrip(";").strip()


def _matched_nothing(result: QueryResult, request: Mapping[str, Any]) -> bool:
    """No rows, or the one row of an ungrouped aggregate holding only zeros and nulls.

    What a condition on a value that is not in the data leaves; a real zero
    reads the same, and costs only a look-up that finds the value.
    """
    if not any(f.get("op", "eq") == "eq" for f in request.get("filters") or []):
        return False
    if not result.rows:
        return True
    return not request.get("dimensions") and all(v in (None, 0) for v in result.rows[0])


class ToolService:
    def __init__(
        self,
        query: QueryService,
        sources: SourceService,
        views: MaterializedViewService,
        store: AnalyticsStore,
        catalog: SchemaCatalog,
        cache: QueryCache,
        planner: FetchPlanner | None = None,
        analyzer: SqlAnalyzer | None = None,
        semantic: SemanticService | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._query = query
        self._sources = sources
        self._views = views
        self._store = store
        self._catalog = catalog
        self._cache = cache
        self._planner = planner or query.planner
        self._analyzer = analyzer or query.analyzer
        self._semantic = semantic
        self._scope = UNRESTRICTED
        self._audit: AuditSink = audit or NullAuditSink()
        #: Whether a usage record keeps the words of a question; off, as they
        #: may name a person (``audit.question_text``).
        self.keep_questions = False
        #: Writes SQL for a question the vocabulary reads none of, as
        #: ``(prompt, system prompt) -> reply``; None (``olap.unread_questions:
        #: refuse``) refuses such a question instead.
        self.write_sql: Callable[[str, str], str] | None = None
        #: Whether that model is shown the stored values; off for a cloud
        #: model unless ``analysis.share_samples_with_cloud_llm``.
        self.values_to_model = True
        #: More checks for ``check_readiness``, added by whoever hosts the engine
        #: and knows things it does not (the catalog's review status, say).
        self.readiness_checks: list[Callable[[], dict[str, Any]]] = []
        self._handlers: dict[str, Callable[..., dict[str, Any]]] = {
            name: getattr(self, name) for name in TOOL_NAMES
        }

    def for_scope(self, scope: AccessScope) -> ToolService:
        """These tools, run as one caller: row rules and masks apply to every read."""
        bound = copy.copy(self)
        bound._scope = scope
        bound._handlers = {name: getattr(bound, name) for name in TOOL_NAMES}
        return bound

    def _require_unrestricted(self, what: str) -> None:
        """Refuse a tool that would work on rows before any row rule or mask applied."""
        if self._scope.is_restricted:
            raise GuardrailViolation(
                f"Guardrail: {what} is not available to a caller with row rules or "
                f"masked columns; queries load what they need by themselves"
            )

    # -- dispatch -----------------------------------------------------------

    def execute(self, tool_name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        args = dict(arguments or {})
        started = time.perf_counter()
        outcome: ToolOutcome = "answered"
        try:
            result = handler(**args)
        except GuardrailViolation as e:
            result, outcome = {"error": str(e)}, "refused"
        except (QueryError, SchemaError, BackupError) as e:
            result, outcome = {"error": str(e)}, "failed"
        except TypeError as e:
            result, outcome = {"error": f"Invalid arguments for {tool_name}: {e}"}, "failed"
        except Exception as e:
            logger.exception("Tool execution error: %s", tool_name)
            result, outcome = {"error": f"Internal error: {e}"}, "error"
        if outcome == "answered":
            if "error" in result:
                outcome = "failed"
            elif result.get("row_count", result.get("count")) == 0:
                outcome = "empty"  # no rows, or a search (terms, examples) that found nothing
        self._record_call(tool_name, args, result, outcome, started)
        return result

    def _record_call(
        self,
        tool: str,
        args: Mapping[str, Any],
        result: Mapping[str, Any],
        outcome: ToolOutcome,
        started: float,
    ) -> None:
        """One usage record: the shape of what was asked, never a value or the SQL."""
        try:
            # How answer_question read the question, or what query_metrics was asked.
            reading = result.get("request") if tool == "answer_question" else args
            if not isinstance(reading, Mapping):
                reading = {}
            table = reading.get("table") or args.get("table") or ""
            measures = [str(m) for m in reading.get("metrics") or [] if isinstance(m, str)]
            dimensions = [
                d if isinstance(d, str) else str(d.get("column", ""))
                for d in reading.get("dimensions") or []
                if isinstance(d, str | Mapping)
            ]
            filtered = [
                str(f.get("column")) for f in reading.get("filters") or [] if isinstance(f, Mapping)
            ]
            if tool == "search_data" and args.get("filter_column"):
                filtered.append(str(args["filter_column"]))
            if tool == "aggregate_data":
                dimensions += [str(args["group_by"])] if args.get("group_by") else []
                if args.get("agg_column"):
                    measures.append(f"{args['agg_column']}:{str(args.get('agg_func', '')).lower()}")
            glossary = [str(e).split(" -> ", 1)[0] for e in result.get("glossary") or []]
            words = args.get("question") if tool in ("answer_question", "check_question") else None
            if tool == "search_terms":
                words = args.get("query")
            row_count = result.get("row_count")
            self._audit.record(
                ToolCall(
                    tool=tool,
                    outcome=outcome,
                    actor=self._scope.actor,
                    tenant=self._scope.tenant,
                    roles=self._scope.roles,
                    request_id=self._scope.request_id,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    row_count=row_count if isinstance(row_count, int) else None,
                    table=str(table),
                    measures=tuple(measures),
                    dimensions=tuple(dimensions),
                    filtered_columns=tuple(dict.fromkeys(filtered)),
                    glossary=tuple(glossary),
                    question=str(words) if self.keep_questions and words else None,
                )
            )
        except Exception:  # usage is a record, never a reason a call fails
            logger.exception("Could not record the call to %s", tool)

    @property
    def names(self) -> tuple[str, ...]:
        return TOOL_NAMES

    # -- tools --------------------------------------------------------------

    def list_sources(self) -> dict[str, Any]:
        sources = []
        for source_name, entry in self._catalog.get_all_sources().items():
            tables = []
            for table_name, schema in entry.tables.items():
                ref = TableRef(source_name, table_name)
                loaded = self._catalog.is_loaded(ref)
                row_count = schema.row_count
                if loaded:
                    try:
                        row_count = self._store.count(ref.full_name)
                    except QueryError:
                        pass
                # A slice is described by its conditions, and those are other
                # callers' literals: not for someone who is restricted to see.
                slices = (
                    []
                    if self._scope.is_restricted
                    else [s for s in self._catalog.slices_of(ref) if not s.is_full]
                )
                tables.append(
                    {
                        "name": ref.full_name,
                        "row_count": row_count,
                        # What the source says the table holds, which is what
                        # decides whether it can be loaded whole at all.
                        "row_estimate": schema.row_estimate,
                        "columns": len(schema.columns),
                        "loaded": loaded,
                        "slices": [
                            {
                                "table": s.table_name,
                                "rows": s.row_count,
                                "where": s.spec.describe(),
                            }
                            for s in slices
                        ],
                    }
                )
            described: dict[str, Any] = {
                "source": source_name,
                "type": entry.type,
                "tables": tables,
            }
            notes = self._catalog.notes_of(source_name)
            if notes:
                described["notes"] = list(notes)
            sources.append(described)
        return {"sources": sources}

    def describe_table(self, table: str) -> dict[str, Any]:
        validate_identifier(table, "table name")
        if table.startswith("mv_"):
            try:
                return self._views.describe(table)
            except QueryError as e:
                return {"error": f"Materialized view '{table}' not found: {e}"}
        schema = self._catalog.get_table(table)
        described: dict[str, Any] = {
            "table": table,
            "columns": [c.as_dict() for c in schema.columns],
            "row_count": schema.row_count,
            # What to join on: the keys the database itself declares.
            "primary_key": list(schema.primary_key),
            "foreign_keys": [fk.as_dict() for fk in schema.foreign_keys],
        }
        entry = self._catalog_entry(table)
        if entry is not None:
            # What a reviewer signed off about the table, beside what the
            # database says about it.
            described["description"] = str(entry.description)
            texts = {c.name: str(c.description) for c in entry.columns}
            for column in described["columns"]:
                if texts.get(column["name"]):
                    column["description"] = texts[column["name"]]
        return described

    def _catalog_entry(self, table: str) -> Any:
        if self._semantic is None:
            return None
        try:
            return self._semantic.catalog_entry(TableRef.parse(table))
        except SchemaError:
            return None

    # -- the term layer -----------------------------------------------------

    def search_terms(self, query: str) -> dict[str, Any]:
        """The certified business terms whose words match ``query``."""
        # By stem, as questions are matched: "siparişler" has to find "sipariş".
        expanded, _ = expand(str(query), self._catalog.glossary)
        words = [word[:STEM] for word in expanded.split() if len(word) > 2]
        found = []
        for source, term in self._require_semantic().terms():
            matched = sorted(
                {
                    label
                    for label, text in _term_words(term, self._withholds(source))
                    if any(word in fold(text) for word in words)
                }
            )
            if matched or not words:
                found.append(
                    {
                        "term": term.name,
                        "tables": [f"{source}.{table}" for table in term.tables],
                        "description": str(term.description),
                        "matched": matched,
                    }
                )
        return {"terms": found, "count": len(found)}

    def _withholds(self, source: str) -> Callable[[str, str], bool]:
        """Whether a term's stored values of ``(table, column)`` are kept from this caller.

        Masked columns' values are; so are all of a row-ruled table's, which
        may come from rows outside the caller's rules.
        """
        scope = self._scope

        def withheld(table: str, column: str) -> bool:
            if not scope.is_restricted:
                return False
            if scope.conditions_for(table):
                return True
            try:
                return column in scope.masks_for(source, table)
            except PolicyError:
                return True

        return withheld

    def find_examples(self, question: str = "", limit: int = 5) -> dict[str, Any]:
        """Certified examples close to ``question``: questions and the SQL that answered them.

        Matched by word stem, as questions are, after the glossary has written
        abbreviations out; an empty question lists them. A caller with row
        rules or masks is not shown an example whose SQL quotes a value.
        """
        limit = min(max(1, _whole(limit, "limit")), 20)
        expanded, _ = expand(str(question), self._catalog.glossary)
        words = [word[:STEM] for word in expanded.split() if len(word) > 2]
        scored = []
        for example in self._catalog.examples():
            if self._scope.is_restricted and example.has_literals:
                continue
            text = fold(
                " ".join([*example.question.texts.values(), *example.description.texts.values()])
            )
            score = sum(word in text for word in words)
            if score or not words:
                scored.append((score, example))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        found = [
            {
                "name": example.name,
                "question": str(example.question),
                "sql": example.sql,
                "description": str(example.description),
                "tables": list(example.tables),
            }
            for _, example in scored[:limit]
        ]
        return {"examples": found, "count": len(found)}

    def get_term(self, name: str) -> dict[str, Any]:
        """One certified term: what it is called, where it lives, what can be asked of it."""
        semantic = self._require_semantic()
        wanted = fold(str(name))
        for source, term in semantic.terms():
            if wanted not in {fold(term.name), *(fold(table) for table in term.tables)}:
                continue
            tables = [TableRef(source, table) for table in term.tables]
            return {
                "term": term.name,
                "description": str(term.description),
                "synonyms": list(term.synonyms),
                "tables": [ref.full_name for ref in tables],
                "columns": [
                    {
                        "table": f"{source}.{a.table}",
                        "column": a.column,
                        "synonyms": list(a.synonyms),
                        "values": {}
                        if self._withholds(source)(a.table, a.column)
                        else dict(a.values),
                    }
                    for a in term.column_attributes
                ],
                "measures": [
                    measure.as_dict()
                    for ref in tables
                    if self._catalog.has_table(ref)
                    for measure in semantic.model_for(ref).measures
                ],
                # Whose columns a question about this term may also use.
                "reaches": sorted(
                    {
                        parent.full_name
                        for ref in tables
                        for parent in {
                            **self._catalog.joins.parents(ref),
                            **self._catalog.joins.across(ref),
                        }
                    }
                ),
            }
        known = ", ".join(term.name for _, term in semantic.terms()) or "none"
        raise QueryError(f"No certified term called '{name}'. Certified terms: {known}.")

    def check_question(self, question: str) -> dict[str, Any]:
        """How much of ``question`` the certified vocabulary covers; nothing runs.

        Word by word, what each was recognised as (a table, a column, a stored
        value, an aggregation, a grain) and which were recognised as nothing.
        The reading ``answer_question`` would take is given only when the
        question named something the vocabulary has: otherwise it would be a
        guess. Values are matched as ``answer_question`` matches them: a
        column masked for the caller has none.
        """
        semantic = self._require_semantic()
        if not isinstance(question, str) or not question.strip():
            raise QueryError("Ask a question in words, e.g. 'total amount per channel'.")
        read, expanded = expand(question, self._catalog.glossary)
        planner = semantic.planner(self._scope)
        words, unknown, covered = self._coverage(planner, question)
        named = covered != "none"
        answer: dict[str, Any] = {
            "question": question,
            "covered": covered,
            "coverage": round((len(words) - len(unknown)) / len(words), 2) if words else 0.0,
            "words": words,
            "unknown": unknown,
            "count": len(words) - len(unknown),
        }
        apart: list[dict[str, Any]] = []
        if named:
            planned = planner.plan(read, written=question)
            answer["reading"] = to_dsl(planned)
            # Recognised is not read: what the reading leaves out, and what it could never hold.
            left, apart = planner.unused(planned, words), planner.beyond(planned, words)
            if left:
                answer["unused"] = [_said(word) for word in left]
            if apart:
                answer["apart"] = [_said(word) for word in apart]
        if expanded:
            answer["glossary"] = expanded
        if apart:
            answer["advice"] = (
                f"{_apart(str(answer['reading']['table']), apart)} answer_question refuses "
                "it: ask with query_metrics or query_data."
            )
            return answer
        answer["advice"] = {
            "all": "Every word is in the vocabulary: answer_question will read it as shown.",
            "part": (
                f"Not in the vocabulary: {', '.join(unknown)}. answer_question would answer "
                "without them; check the reading, or ask with query_metrics or query_data."
            ),
            "none": (
                "Nothing in this question names a table, column or value the vocabulary "
                "has: ask with query_data, or have a steward add the words (get_term)."
            ),
        }[covered]
        if answer.get("unused"):
            answer["advice"] += (
                " Recognised but not used by the reading: "
                f"{', '.join(w['word'] for w in answer['unused'])}; no word added to the "
                "vocabulary changes that."
            )
        return answer

    def _coverage(
        self, planner: LexiconPlanner, question: str
    ) -> tuple[list[dict[str, Any]], list[str], str]:
        """Each word's reading, the words read as nothing, and all / part / none."""
        read, _ = expand(question, self._catalog.glossary)
        words = planner.coverage(read, written=question)
        unknown = [w["word"] for w in words if not w["read_as"]]
        named = any(
            reading.split(" ", 1)[0] in ("table", "column", "value")
            for w in words
            for reading in w["read_as"]
        )
        return words, unknown, "none" if not named else "part" if unknown else "all"

    def vocabulary_gaps(self, asked: Mapping[str, Sequence[int]], top: int = 20) -> dict[str, Any]:
        """The words people asked with that the vocabulary does not know, most missed first.

        ``asked`` maps a question text to ``(calls, calls not answered)``, as
        the usage summary counts them. Each question is read as
        ``check_question`` reads it, as this caller: a word the caller's masks
        hide a value behind is a gap here too.
        """
        semantic = self._require_semantic()
        if not any(True for _ in semantic.terms()):
            raise QueryError(
                "No certified vocabulary yet: approve a catalog with terms to measure questions against."
            )
        planner = semantic.planner(self._scope)
        covered: Counter[str] = Counter()
        gaps: dict[str, dict[str, Any]] = {}
        for question, (calls, missed) in sorted(asked.items(), key=lambda q: -q[1][0]):
            _, unknown, verdict = self._coverage(planner, question)
            covered[verdict] += calls
            for word in dict.fromkeys(unknown):
                gap = gaps.setdefault(
                    word,
                    {
                        "word": word,
                        "questions": 0,
                        "asked": 0,
                        "unanswered": 0,
                        "example": question,
                    },
                )
                gap["questions"] += 1
                gap["asked"] += calls
                gap["unanswered"] += missed
        ranked = sorted(gaps.values(), key=lambda g: (-g["unanswered"], -g["asked"], g["word"]))
        return {
            "questions": len(asked),
            "covered": {name: covered[name] for name in ("all", "part", "none")},
            "gaps": ranked[:top],
        }

    def answer_question(
        self, question: str, previous: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Answer a question asked in words, and show how it was read.

        The question is matched against the certified vocabulary, with no
        model involved; the result carries the request that was understood
        and the SQL it compiled to, so the reading can be checked. With
        ``previous``, the ``request`` an earlier answer returned, the question
        is a follow-up ("and by branch?"): that request, changed only where
        the new words say. Nothing is kept here between calls; the caller
        carries the conversation.
        """
        semantic = self._require_semantic()
        if not isinstance(question, str):
            question = ""  # ask() says what a question has to be
        read, expanded = expand(question, self._catalog.glossary) if question else ("", [])
        unknown: list[str] = []
        typed: list[str] = []
        words: list[dict[str, Any]] = []
        planner = semantic.planner(self._scope) if question.strip() else None
        if planner is not None:
            words, unknown, covered = self._coverage(planner, question)
            typed = [w["written"] for w in words if not w["read_as"]]
            if previous is None and covered == "none":
                # A guess here is a confident wrong answer: say so, or hand it on.
                unread = _UNREAD.format(", ".join(unknown) or "any word")
                if self.write_sql is not None:
                    return self._answer_by_model(question, unread, unknown, typed, planner)
                raise QueryError(
                    f"{unread} Ask with the "
                    "words the terms use (search_terms finds them), or use "
                    "query_metrics or query_data; a steward can add the words."
                )
        if previous is None:
            planned = semantic.ask(read, self._scope, written=question)
            # ask() refuses a question with no words, so here the planner is there.
            if planner is not None and (apart := planner.beyond(planned, words)):
                # Every word may be known and the reading still be of another
                # question: one table's rows, with the rest of what was named
                # left out. That is the same confident wrong answer.
                why = _apart(str(planned["table"]), apart)
                if self.write_sql is not None:
                    return self._answer_by_model(question, why, unknown, typed, planner)
                raise QueryError(
                    f"{why} Ask about one of them at a time, or write the request "
                    "with query_metrics or query_data."
                )
        elif not isinstance(previous, Mapping) or not question.strip():
            raise QueryError(
                "A follow-up needs the question in words and, as previous, the "
                "request an earlier answer returned."
            )
        else:
            try:
                planned = semantic.planner(self._scope).follow_up(previous, read, written=question)
            except ValueError as e:
                raise QueryError(f"{e}: pass the request an earlier answer returned.") from e
        request = to_dsl(planned)
        result = semantic.run(**request, scope=self._scope)
        repaired: list[dict[str, str]] = []
        if _matched_nothing(result, request):
            # Only now: a value that is in the data needs no look-up, and each is a read.
            fixed, repaired = self._repair_literals(request)
            if repaired:
                request = fixed
                result = semantic.run(**request, scope=self._scope)
        answer = self._format_result(result)
        answer["request"] = request
        answer["sql"] = semantic.compiled_sql(semantic.build(**request))
        if expanded:
            answer["glossary"] = expanded
        if repaired:
            answer["literal_repairs"] = repaired
        if unknown:
            # Answered without these: the reading may have dropped what they asked for.
            answer["unread"] = unknown
            answer["unread_written"] = typed  # the same words, as they were typed
        left = planner.unused(request, words) if planner is not None else []
        if left:
            # Known words the reading made no use of: adding a word will not bring them in.
            answer["unused"] = [_said(word) for word in left]
        return answer

    def _answer_by_model(
        self,
        question: str,
        unread: str,
        unknown: list[str],
        typed: list[str],
        planner: LexiconPlanner,
    ) -> dict[str, Any]:
        """A question the vocabulary cannot read, answered with SQL a model wrote.

        ``unread`` says why it could not: none of its words are known, or
        they name tables no one request joins.

        The model sees what this caller may: the tables, their columns and
        keys, the stored values not masked for them (none for a cloud model,
        unless samples may be shared with one), the glossary and the notes. Its SQL runs as ``query_data`` would, through the guardrails
        and the caller's row rules and masks; the answer says it was written
        by a model and carries the SQL, so the reading can be checked.
        """
        assert self.write_sql is not None
        try:
            reply = self.write_sql(
                f"{self._schema_for_model(planner)}\n\nQuestion: {question}\nSQL:", _WRITE_SQL
            )
        except Exception as e:
            raise QueryError(
                f"{unread} The model that writes SQL for such questions did not answer: {e}"
            ) from e
        sql = _sql_in(reply)
        try:
            result = self._query.sql(sql, scope=self._scope)
        except (GuardrailViolation, QueryError, SchemaError) as e:
            raise QueryError(f"{unread} The SQL a model wrote for it did not run: {e}") from e
        answer = self._format_result(result)
        answer.update(
            sql=sql,
            read_by="model",
            unread=unknown,
            unread_written=typed,
            note="Written by a model from the schema, not read from the certified vocabulary: check the SQL.",
        )
        return answer

    def _schema_for_model(self, planner: LexiconPlanner) -> str:
        """The tables as this caller may see them, for a model writing SQL."""
        lines = []
        for ref in self._catalog.list_tables():
            schema = self._catalog.get_table(ref)
            values = planner.values(ref.full_name) if self.values_to_model else {}
            columns = ", ".join(
                f"{c.name} {c.type}"
                + (
                    f" ({' | '.join(str(v) for v in values[c.name][:12])})"
                    if values.get(c.name)
                    else ""
                )
                for c in schema.columns
            )
            lines.append(f"{ref.full_name}: {columns}")
            lines += [
                f"  {ref.full_name}.{fk.column} -> {ref.source}.{fk.references_table}.{fk.references_column}"
                for fk in schema.foreign_keys
            ]
        glossary = self._catalog.glossary
        if glossary:
            lines.append("Abbreviations: " + "; ".join(f"{k} = {v}" for k, v in glossary.items()))
        for source in sorted({ref.source for ref in self._catalog.list_tables()}):
            lines += [f"Note ({source}): {note}" for note in self._catalog.notes_of(source)]
        return "\n".join(lines)

    def _repair_literals(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Swap a condition's value for the stored spelling when it matches no row.

        The vocabulary is as old as the catalog; the data is live. A value
        that matches nothing is looked up among the column's present values,
        and the corrected request is used only if its SQL is proven to differ
        from the original by that one literal.
        """
        semantic = self._require_semantic()
        fact = str(request["table"])
        repaired: list[dict[str, str]] = []
        for index, condition in enumerate(request["filters"]):
            value = condition.get("value")
            if condition.get("op", "eq") != "eq" or not isinstance(value, str):
                continue
            owner, _, column = str(condition["column"]).rpartition(".")
            table = f"{fact.partition('.')[0]}.{owner}" if owner else fact
            try:
                if self.search_data(table, column, value, limit=1).get("row_count"):
                    continue
                stored = self.aggregate_data(table, column, column, "COUNT").get("rows", [])
            except (GuardrailViolation, QueryError, SchemaError) as e:
                # The check is a courtesy: a table too big to look into, say,
                # must not cost the caller the answer itself.
                logger.info("Could not check '%s' against %s.%s: %s", value, table, column, e)
                continue
            present = next(
                (
                    row[column]
                    for row in stored
                    if isinstance(row[column], str) and fold(row[column]) == fold(value)
                ),
                None,
            )
            if present is None:
                continue
            filters = list(request["filters"])
            filters[index] = {**condition, "value": present}
            candidate = {**request, "filters": filters}
            if prove_substitution(
                semantic.compiled_sql(semantic.build(**request)),
                semantic.compiled_sql(semantic.build(**candidate)),
                value,
                present,
                self._query.validate,
            ):
                request = candidate
                repaired.append({"column": condition["column"], "from": value, "to": present})
        return request, repaired

    def query_data(self, sql: str) -> dict[str, Any]:
        return self._format_result(self._query.sql(sql, scope=self._scope))

    def search_data(
        self, table: str, filter_column: str, filter_value: str, limit: int = 20
    ) -> dict[str, Any]:
        validate_identifier(table, "table name")
        validate_identifier(filter_column, "column name")
        self._validate_columns_in_catalog(table, [filter_column])
        limit = min(max(1, _whole(limit, "limit")), MAX_RESULT_ROWS)
        filter_value = str(filter_value)

        # The two shortcuts below read the source directly, past any row rule
        # or mask, so a restricted caller goes straight to the query pipeline.
        restricted = self._scope.is_restricted
        if not restricted:
            pushed = self._try_search_pushdown(table, filter_column, filter_value, limit)
            if pushed is not None:
                return pushed

        # A table too big to load whole can still be searched: fetch just the
        # matching rows and read them from the slice.
        scanned = (
            table if restricted else self._search_slice(table, filter_column, filter_value) or table
        )
        operator = "LIKE" if "%" in filter_value else "="
        sql = (
            f"SELECT * FROM {scanned} WHERE CAST({filter_column} AS VARCHAR) {operator} ? "
            f"LIMIT {limit}"
        )
        return self._format_result(self._query.sql(sql, params=[filter_value], scope=self._scope))

    def _search_slice(self, table: str, column: str, value: str) -> str | None:
        """Load the rows matching a search, when the table is not loaded yet.

        Only for text columns: the value arrives as a string, and a source
        that compares it literally would silently miss rows of a numeric or
        date column. Those fall back to loading the table.
        """
        if "." not in table or table.startswith(MV_PREFIX):
            return None
        ref = TableRef.parse(table)
        if self._catalog.is_loaded(ref) or not self._catalog.has_table(ref):
            return None
        schema = self._catalog.get_table(ref)
        if self._planner.policy.allows_full_load(schema.known_estimate):
            # Small enough to take whole, which serves every later query too.
            return None
        kinds = {c.name: c.type.lower() for c in schema.columns}
        if not any(kinds.get(column, "").startswith(text) for text in ("varchar", "text", "char")):
            return None
        source = self._sources.source(ref.source)
        if source is None or not source.capabilities.slices:
            return None
        spec = SliceSpec(predicates=(Predicate(column, "like" if "%" in value else "eq", value),))
        covering = self._catalog.find_covering_slice(ref, spec)
        if covering is not None:
            return covering.table_name
        try:
            return self._sources.ensure_slices([TargetPlan(ref, spec, "load_slice")]).get(ref)
        except QueryError as e:
            logger.info("Search slice for %s unavailable (%s); loading the table", table, e)
            return None

    def aggregate_data(
        self, table: str, group_by: str, agg_column: str, agg_func: str
    ) -> dict[str, Any]:
        validate_identifier(table, "table name")
        validate_identifier(group_by, "group_by column")
        validate_identifier(agg_column, "agg_column")
        self._validate_columns_in_catalog(table, [group_by, agg_column])

        func = str(agg_func).upper()
        if func not in ALLOWED_AGG_FUNCS:
            return {
                "error": f"Invalid aggregation function: {agg_func}. "
                f"Allowed: {sorted(ALLOWED_AGG_FUNCS)}"
            }
        alias = f"{func.lower()}_{agg_column}"

        if not self._scope.is_restricted:
            pushed = self._try_aggregate_pushdown(table, group_by, agg_column, func, alias)
            if pushed is not None:
                return pushed

        sql = (
            f"SELECT {group_by}, {func}({agg_column}) AS {alias} FROM {table} "
            f"GROUP BY {group_by} ORDER BY {alias} DESC LIMIT {MAX_RESULT_ROWS}"
        )
        return self._format_result(self._query.sql(sql, scope=self._scope))

    def create_view(self, name: str, sql: str, refresh: str = "manual") -> dict[str, Any]:
        self._require_unrestricted("create_view")
        if not is_valid_view_name(name):
            return {"error": f"Invalid view name: '{name}'. Use alphanumeric and underscore only."}
        spec = self._views.create(name, sql, refresh=refresh)
        out: dict[str, Any] = {
            "status": "created",
            "name": spec.name,
            "table_name": spec.table_name,
            "refresh": spec.refresh,
        }
        if spec.grounding is not None:
            out["grounding"] = spec.grounding.as_dict()
        return out

    def list_views(self) -> dict[str, Any]:
        return {"views": self._views.list_views()}

    def refresh_view(self, name: str) -> dict[str, Any]:
        self._require_unrestricted("refresh_view")
        self._views.refresh(name)
        return {"status": "refreshed", "name": name}

    def load_table(
        self,
        table: str,
        where: str | None = None,
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load a table, or just the slice ``where``/``columns`` describe.

        Without ``where`` and ``columns`` this is the whole table, and a
        table too big for the configured limit is refused with advice. With
        them, only the matching rows and columns are fetched, which is how a
        table far larger than memory is still made queryable.
        """
        self._require_unrestricted("load_table")
        validate_identifier(table, "table name")
        try:
            ref = TableRef.parse(table)
        except SchemaError:
            return {"error": "Use 'source.table' format (e.g. 'mydb.orders')"}
        if not self._catalog.has_table(ref):
            return {"error": str(_missing_table_error(self._catalog, ref))}
        if where is None and not columns:
            return self._load_whole_table(ref, table)
        return self._load_slice(ref, table, where, columns)

    def _load_whole_table(self, ref: TableRef, table: str) -> dict[str, Any]:
        if self._catalog.is_loaded(ref):
            return {"status": "already_loaded", "table": table}
        plan = self._planner.plan_query(f"SELECT * FROM {table}")
        if plan.is_refused:
            return {"error": plan.refusal}
        newly = self._sources.ensure_loaded([ref])
        if ref in newly:
            return {"status": "loaded", "table": table, "row_count": self._row_count(ref)}
        return {"error": f"Failed to load table '{table}'"}

    def _load_slice(
        self, ref: TableRef, table: str, where: str | None, columns: list[str] | None
    ) -> dict[str, Any]:
        """Turn ``where``/``columns`` into a slice by parsing them as a SELECT."""
        if columns:
            for column in columns:
                validate_identifier(column, "column name")
            self._validate_columns_in_catalog(table, list(columns))
        projection = ", ".join(columns) if columns else "*"
        sql = f"SELECT {projection} FROM {table}"
        if where:
            sql += f" WHERE {where}"
        shape = self._analyzer.analyze(sql)
        use = shape.use_for(ref) if shape.is_simple_select else None
        if use is None:
            return {"error": _WHERE_HELP}
        if where and not use.predicates:
            return {"error": _WHERE_HELP}
        spec = use.slice_spec()
        target = TargetPlan(ref, spec, "load_slice")
        try:
            table_name = self._sources.ensure_slices([target])[ref]
        except KeyError:
            return {"error": f"No connected source for '{table}'"}
        loaded = self._catalog.slices.get(table_name)
        return {
            "status": "loaded",
            "table": table,
            "slice": spec.describe(),
            "slice_table": table_name,
            "row_count": loaded.row_count if loaded else 0,
            "complete": loaded.complete if loaded else False,
        }

    def _row_count(self, ref: TableRef) -> int:
        try:
            return self._store.count(ref.full_name)
        except QueryError:
            return -1

    def list_metrics(self, table: str | None = None) -> dict[str, Any]:
        """The semantic model for a table, or a summary of every table's."""
        return self._require_semantic().describe(table)

    def query_metrics(
        self,
        table: str,
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        order_by: str = "",
        limit: int = MAX_RESULT_ROWS,
    ) -> dict[str, Any]:
        """Aggregate and transform without the caller writing any SQL."""
        result = self._require_semantic().run(
            table,
            metrics,
            dimensions or (),
            filters or (),
            order_by=order_by,
            limit=limit,
            scope=self._scope,
        )
        return self._format_result(result)

    def _require_semantic(self) -> SemanticService:
        if self._semantic is None:
            raise QueryError(
                "The semantic layer is not available in this build. "
                "Use query_data or aggregate_data instead."
            )
        return self._semantic

    def cache_stats(self) -> dict[str, Any]:
        return self._cache.stats()

    def check_readiness(self) -> dict[str, Any]:
        """Whether questions can be answered here, and what is missing if not."""
        sources = self._catalog.get_all_sources()
        tables = sum(len(entry.tables) for entry in sources.values())
        checks: list[dict[str, Any]] = [
            {
                "check": "sources_connected",
                "ok": bool(sources),
                "detail": ", ".join(sources) or "no data source is connected",
            },
            {"check": "tables_discovered", "ok": tables > 0, "detail": f"{tables} tables"},
            {
                "check": "semantic_layer",
                "ok": self._semantic is not None,
                "detail": "query_metrics is available"
                if self._semantic is not None
                else "not in this build; use query_data",
            },
            {
                # Not a reason to be "not ready": everything but
                # answer_question works without a vocabulary.
                "check": "certified_terms",
                "ok": True,
                # 0 = answer_question cannot read any question yet.
                "tables": len(self._semantic.term_tables()) if self._semantic else 0,
                "detail": f"{len(covered)} tables have a certified term; answer_question "
                f"reads questions about them"
                if (covered := self._semantic.term_tables() if self._semantic else [])
                else "no certified terms; answer_question is unavailable until "
                "`truejoin catalog analyze` and `truejoin catalog review` have produced some",
            },
            *(check() for check in self.readiness_checks),
        ]
        if self._query.grounding != "off":
            keyed = {edge.source for edge in self._catalog.joins.edges}
            bare = [name for name in sources if name not in keyed]
            checks.append(
                {
                    "check": "join_graph",
                    "ok": not bare,
                    "detail": f"no foreign key or verified relationship known for: "
                    f"{', '.join(bare)}; joins there are not checked"
                    if bare
                    else f"{len(self._catalog.joins)} join keys known",
                }
            )
        return {"ready": all(check["ok"] for check in checks), "checks": checks}

    # -- helpers ------------------------------------------------------------

    def _validate_columns_in_catalog(self, table: str, columns: list[str]) -> None:
        """Defense-in-depth over the identifier regex: columns must exist.

        Materialized views live only in the store, so they are skipped.
        """
        if table.startswith("mv_"):
            return
        schema = self._catalog.get_table(table)  # SchemaError if unknown
        known = schema.column_names
        if not known:
            return  # schema could not be inferred; let the store reject bad columns
        for col in columns:
            if col not in known:
                raise QueryError(
                    f"Unknown column '{col}' in table '{table}'. "
                    f"Available columns: {', '.join(sorted(known))}"
                )

    def _pushdown_target(self, table: str) -> tuple[PushdownCapable | None, str]:
        """(source, bare table name) when pushdown applies, else (None, '')."""
        try:
            ref = TableRef.parse(table)
        except SchemaError:
            return None, ""
        if self._catalog.is_loaded(ref):
            return None, ""  # local is faster once loaded
        source = self._sources.pushdown_source(ref.source)
        if source is None:
            return None, ""
        return source, ref.table

    def _try_search_pushdown(
        self, table: str, column: str, value: str, limit: int
    ) -> dict[str, Any] | None:
        source, bare = self._pushdown_target(table)
        if source is None:
            return None
        try:
            started = time.perf_counter()
            if "%" in value:
                escaped = value.replace("'", "''")
                sql = (
                    f"SELECT * FROM {bare} WHERE CAST({column} AS VARCHAR) LIKE '{escaped}' "
                    f"LIMIT {limit}"
                )
                rows = source.execute_query(sql)
            else:
                rows = source.fetch_filtered(bare, {column: value}, limit=limit)
            self._query.record_access(self._scope, [TableRef.parse(table)], row_count=len(rows))
            return self._format_rowset(rows, (time.perf_counter() - started) * 1000)
        except Exception as e:
            logger.info("Search pushdown failed for %s, falling back: %s", table, e)
            return None

    def _try_aggregate_pushdown(
        self, table: str, group_by: str, agg_column: str, func: str, alias: str
    ) -> dict[str, Any] | None:
        source, bare = self._pushdown_target(table)
        if source is None:
            return None
        try:
            started = time.perf_counter()
            sql = (
                f"SELECT {group_by}, {func}({agg_column}) AS {alias} FROM {bare} "
                f"GROUP BY {group_by} ORDER BY {alias} DESC LIMIT {MAX_RESULT_ROWS}"
            )
            rows = source.execute_query(sql)
            self._query.record_access(self._scope, [TableRef.parse(table)], row_count=len(rows))
            return self._format_rowset(rows, (time.perf_counter() - started) * 1000)
        except Exception as e:
            logger.info("Aggregate pushdown failed for %s, falling back: %s", table, e)
            return None

    @staticmethod
    def _format_result(result: QueryResult) -> dict[str, Any]:
        records = result.to_records()
        truncated = len(records) > MAX_RESULT_ROWS
        out = {
            "columns": list(result.columns),
            "rows": records[:MAX_RESULT_ROWS],
            "row_count": result.row_count,
            "truncated": truncated,
            "execution_time_ms": round(result.execution_time_ms, 1),
            "from_cache": result.from_cache,
        }
        if result.grounding is not None:
            # Only when a join matched no known key: what was found, and the
            # query that ran instead when one was repaired.
            out["grounding"] = result.grounding.as_dict()
        return out

    @staticmethod
    def _format_rowset(rows: RowSet, elapsed_ms: float = 0.0) -> dict[str, Any]:
        records = rows.to_records()
        truncated = len(records) > MAX_RESULT_ROWS
        return {
            "columns": list(rows.columns),
            "rows": records[:MAX_RESULT_ROWS],
            "row_count": len(rows),
            "truncated": truncated,
            "execution_time_ms": round(elapsed_ms, 1),
            "from_cache": False,
        }


def _term_words(term: Any, withheld: Callable[[str, str], bool]) -> list[tuple[str, str]]:
    """Everything a term can be found by, as ``(what it is, the words)``."""
    words = [(term.name, " ".join([term.name, *term.synonyms, *term.tables]))]
    for attribute in term.column_attributes:
        values = (
            []
            if withheld(attribute.table, attribute.column)
            else [word for value, known in attribute.values.items() for word in (value, *known)]
        )
        words.append((attribute.column, " ".join([attribute.column, *attribute.synonyms, *values])))
    words += [(a.name, f"{a.name} {a.description}") for a in term.sql_attributes if a.certified]
    return words


def _missing_table_error(catalog: SchemaCatalog, ref: TableRef) -> SchemaError:
    try:
        catalog.get_table(ref)
    except SchemaError as e:
        return e
    return SchemaError(f"Table '{ref}' not found")
