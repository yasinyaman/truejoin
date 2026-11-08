"""SemanticService: answering a metric question without anyone writing SQL.

The flow is deliberately a chain of small, separately testable steps:

    parse metrics -> resolve the model -> build and validate the query
      -> derive the shape -> materialize what it needs -> compile -> run

Nothing here imports sqlglot; the compiler behind the ``SemanticCompiler`` port
does that. What this service owns is the order of the steps and the decision to
execute locally.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

from trio.application.olap.lexicon import LexiconPlanner, Vocabulary
from trio.application.olap.query import QueryService
from trio.application.olap.semantic_from_catalog import semantic_model_from_catalog
from trio.domain.catalog.catalog import DatabaseCatalog, TableCatalogEntry
from trio.domain.catalog.join_graph import JoinEdge
from trio.domain.catalog.terms import Term
from trio.domain.governance.access import UNRESTRICTED, AccessScope
from trio.domain.governance.row_policy import PolicyError
from trio.domain.olap.catalog import SchemaCatalog
from trio.domain.olap.errors import GuardrailViolation, QueryError, SchemaError
from trio.domain.olap.identifiers import MAX_RESULT_ROWS, validate_identifier
from trio.domain.olap.measures import SemanticModel, infer_model
from trio.domain.olap.metric_dsl import parse_metric
from trio.domain.olap.models import QueryResult, TableRef, coerce_ref
from trio.domain.olap.semantic_query import DimensionRef, SemanticQuery, parse_dimension
from trio.domain.olap.slices import PREDICATE_OPS, Predicate, PredicateOp
from trio.domain.olap.transforms import TRANSFORMS
from trio.ports.semantic_compiler import SemanticCompiler

logger = logging.getLogger(__name__)


class SemanticService:
    """Resolves metric questions against per-table semantic models."""

    def __init__(
        self,
        catalog: SchemaCatalog,
        query: QueryService,
        compiler: SemanticCompiler,
        models: Mapping[str, SemanticModel] | None = None,
        cross_check: bool = True,
    ) -> None:
        self._catalog = catalog
        self._query = query
        self._compiler = compiler
        #: Explicitly configured models, by full table name. Anything not in
        #: here is inferred from the catalog on first use.
        self._configured: dict[str, SemanticModel] = dict(models or {})
        self._inferred: dict[str, SemanticModel] = {}
        self._cross_check = cross_check
        #: Approved catalogs with certified terms, by source. A table one of
        #: them covers takes its model from it; see ``use_catalog``.
        self._catalogs: dict[str, DatabaseCatalog] = {}

    # -- models -------------------------------------------------------------

    def model_for(self, table: str | TableRef) -> SemanticModel:
        """The model for a table: configured, else from its reviewed catalog, else inferred.

        Raises:
            QueryError: When the table is not in the catalog, naming what is.
        """
        ref = self._resolve(table)
        name = ref.full_name
        configured = self._configured.get(name)
        if configured is not None:
            return configured
        cached = self._inferred.get(name)
        if cached is not None:
            return cached
        model = self._from_catalog(ref) or infer_model(ref, self._catalog.get_table(ref))
        self._inferred[name] = model
        return model

    # -- the term layer -----------------------------------------------------

    def use_catalog(self, source: str, catalog: DatabaseCatalog | None) -> None:
        """Let a source's reviewed catalog say what its tables mean.

        Only an approved catalog with certified terms counts; anything else
        (None, a draft, terms nobody certified) takes the source back to
        inferred models. Whoever hosts the engine calls this when the term
        layer is on.
        """
        if catalog is not None and catalog.certified_terms:
            self._catalogs[source] = catalog
        else:
            self._catalogs.pop(source, None)
        self._inferred.clear()

    def _from_catalog(self, ref: TableRef) -> SemanticModel | None:
        catalog = self._catalogs.get(ref.source)
        if catalog is None:
            return None
        entry, term = catalog.get_table(ref.table), catalog.term_for(ref.table)
        if entry is None or term is None:
            return None
        return semantic_model_from_catalog(ref, self._catalog.get_table(ref), entry, term)

    def term_tables(self) -> list[TableRef]:
        """The connected tables a certified term covers."""
        return [
            ref
            for ref in self._catalog.list_tables()
            if ref.source in self._catalogs
            and self._catalogs[ref.source].term_for(ref.table) is not None
        ]

    def terms(self) -> list[tuple[str, Term]]:
        """Every certified term in use, with the source it belongs to."""
        return [
            (source, term)
            for source, catalog in self._catalogs.items()
            for term in catalog.certified_terms
        ]

    def catalog_entry(self, ref: TableRef) -> TableCatalogEntry | None:
        """What the reviewed catalog says about a table, when it has a certified term."""
        catalog = self._catalogs.get(ref.source)
        if catalog is None or catalog.term_for(ref.table) is None:
            return None
        return catalog.get_table(ref.table)

    def ask(self, question: str, scope: AccessScope = UNRESTRICTED) -> dict[str, Any]:
        """The request ``question`` asks for, matched against the certified vocabulary.

        No model is involved: the same question gives the same request. The
        stored values of a column masked for ``scope`` are not matched.

        Raises:
            QueryError: When no connected table has a certified term, saying
                how to get one.
        """
        if not isinstance(question, str) or not question.strip():
            raise QueryError("Ask a question in words, e.g. 'total amount per channel'.")
        tables = self.term_tables()
        if not tables:
            raise QueryError(
                "Answering a question in words needs a reviewed vocabulary, and no "
                "connected table has a certified term. With olap.terms on, run "
                "`trio catalog analyze` and `trio catalog review` (or approve it in "
                "the console): an approval is picked up within 30 seconds. Until "
                "then use query_metrics or query_data."
            )
        vocabulary = Vocabulary()
        try:
            for source, catalog in self._catalogs.items():
                vocabulary.add(
                    source,
                    catalog.certified_terms,
                    hidden=lambda table, source=source: (  # type: ignore[misc]
                        scope.masks_for(source, table) if scope.is_restricted else ()
                    ),
                )
        except PolicyError as e:
            raise GuardrailViolation(f"Guardrail: {e}") from e
        known = {(ref.source.lower(), ref.table.lower()): ref.full_name for ref in tables}
        parents = {
            ref.full_name: [
                known[key]
                for parent in {
                    **self._catalog.joins.parents(ref),
                    **self._catalog.joins.across(ref),
                }
                if (key := (parent.source.lower(), parent.table.lower())) in known
            ]
            for ref in tables
        }
        planner = LexiconPlanner(
            self,
            [ref.full_name for ref in tables],
            vocabulary.synonyms,
            vocabulary.value_synonyms,
            vocabulary.values,
            parents,
        )
        return planner.plan(question)

    def define(
        self, table: str | TableRef, measures: Sequence[Any], dimensions: Sequence[Any]
    ) -> SemanticModel:
        """Replace the model for a table (SDK only; not exposed as a tool).

        Deciding what "revenue" means is an operator's call, not a model's: a
        caller that defines its own measures and then queries them is checking
        its own homework, and nothing about the answer would be auditable.
        """
        ref = self._resolve(table)
        model = SemanticModel(
            ref=ref, measures=tuple(measures), dimensions=tuple(dimensions), source="explicit"
        )
        self._configured[ref.full_name] = model
        self._inferred.pop(ref.full_name, None)
        return model

    def describe(self, table: str | TableRef | None = None) -> dict[str, Any]:
        """The model for one table, or a summary of every table that has one."""
        if table is None:
            return {
                "tables": [
                    {
                        "table": ref.full_name,
                        "measures": list(self.model_for(ref).measure_names),
                        "dimensions": list(self.model_for(ref).dimension_names),
                    }
                    for ref in self._catalog.list_tables()
                ],
                "transforms": [spec.as_dict() for spec in TRANSFORMS.values()],
            }
        model = self.model_for(table)
        described = model.as_dict()
        described["transforms"] = [spec.as_dict() for spec in TRANSFORMS.values()]
        described["examples"] = self._examples(model)
        return described

    @staticmethod
    def _examples(model: SemanticModel) -> list[str]:
        """Metric expressions that will work against this model, ready to paste.

        Keys are skipped when there is anything else numeric: ``id:sum`` is a
        valid expression and a useless suggestion, and an example that reads as
        nonsense teaches the wrong thing about the whole language.
        """
        examples = ["*:count"]
        candidates = [m for m in model.measures if m.numeric and not m.is_row_count]
        numeric = next(
            (m for m in candidates if not _looks_like_a_key(m.name)),
            next(iter(candidates), None),
        )
        if numeric is not None:
            examples += [
                f"{numeric.name}:sum",
                f"{numeric.name}:avg",
                f"cumsum({numeric.name}:sum)",
                f"change_pct({numeric.name}:sum)",
            ]
            if any(d.temporal for d in model.dimensions):
                examples.append(f"time_shift({numeric.name}:sum)")
        return examples

    # -- running ------------------------------------------------------------

    def build(
        self,
        table: str | TableRef,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Sequence[Mapping[str, Any]] = (),
        order_by: str = "",
        limit: int = MAX_RESULT_ROWS,
    ) -> SemanticQuery:
        """Parse and validate a metric question against the table's model."""
        # Arguments arrive from a language model, so their *types* are as
        # untrusted as their values: a list where a string belongs used to
        # surface as an AttributeError from deep inside the domain.
        metrics = _string_list(metrics, "metrics", "metrics=['revenue:sum']")
        dimensions = _string_list(dimensions, "dimensions", "dimensions=['status']")
        if not isinstance(order_by, str):
            raise QueryError(
                f"order_by must be a single column name, got {order_by!r}. "
                f"Prefix it with '-' to sort descending, e.g. order_by='-revenue_sum'."
            )
        if not isinstance(filters, Sequence) or isinstance(filters, str):
            raise QueryError(
                f"filters must be a list of conditions, got {filters!r}. "
                f"For example: filters=[{{'column': 'status', 'value': 'paid'}}]."
            )
        model = self.model_for(table)
        # A name written `other.column` belongs to a table this one
        # references. How to get there is read from the keys, never from the
        # request, so a request cannot ask for a join that does not exist.
        paths: dict[TableRef, tuple[JoinEdge, ...]] = {}
        chosen: list[DimensionRef] = []
        for text in dimensions:
            other, name = self._split(text, model.ref)
            if other is None:
                chosen.append(parse_dimension(name, model))
                continue
            paths[other] = self._path(model.ref, other)
            chosen.append(replace(parse_dimension(name, self.model_for(other)), table=other))
        own: list[Predicate] = []
        joined: list[tuple[TableRef, Predicate]] = []
        for raw in filters:
            column = raw.get("column") if isinstance(raw, Mapping) else None
            other, name = self._split(column, model.ref) if isinstance(column, str) else (None, "")
            if other is None:
                own.append(_predicate({**raw, "column": name} if name else raw))
                continue
            paths[other] = self._path(model.ref, other)
            predicate = _predicate({**raw, "column": name})
            self.model_for(other).dimension(predicate.column)
            joined.append((other, predicate))
        crossings = {path[:2] for path in paths.values() if _crosses(path)}
        if len(crossings) > 1:
            raise QueryError(
                "A question can reach across one link table at a time; these columns are "
                f"reached across {len(crossings)}. Ask one at a time, or write it with query_data."
            )
        query = SemanticQuery(
            model=model,
            metrics=tuple(parse_metric(metric) for metric in metrics),
            dimensions=tuple(chosen),
            filters=tuple(own),
            order_by=order_by,
            limit=min(int(limit or MAX_RESULT_ROWS), MAX_RESULT_ROWS),
            joins=tuple(dict.fromkeys(edge for path in paths.values() for edge in path)),
            joined_filters=tuple(joined),
        )
        query.validate(self._catalog.get_table(model.ref))
        return query

    def _split(self, text: str, fact: TableRef) -> tuple[TableRef | None, str]:
        """``customers.segment:month`` -> the table it names and ``segment:month``.

        The table is None for a column of ``fact`` itself, however it was
        written.
        """
        head, colon, grain = text.partition(":")
        table, dot, column = head.rpartition(".")
        if not dot:
            return None, text
        other = self._resolve(table if "." in table else f"{fact.source}.{table}")
        return (None if other == fact else other), column + colon + grain

    def _path(self, fact: TableRef, other: TableRef) -> tuple[JoinEdge, ...]:
        """The key steps from ``fact`` up to ``other``, or across a link table to it.

        Raises:
            QueryError: When ``fact`` does not reach ``other``, naming what
                it does reach and the two ways forward.
        """
        graph = self._catalog.joins
        reachable = {**graph.parents(fact), **graph.across(fact)}
        wanted = (other.source.lower(), other.table.lower())
        for parent, path in reachable.items():
            if (parent.source.lower(), parent.table.lower()) == wanted:
                return path
        offered = ", ".join(parent.full_name for parent in reachable) or "no other table"
        raise QueryError(
            f"{other.full_name} cannot be read alongside {fact.full_name}: no one chain of "
            f"keys leads from {fact.full_name} to it ({fact.full_name} reaches {offered}). "
            f"If {other.table} holds several rows per row of {fact.table}, ask with "
            f"table='{other.full_name}' instead; otherwise write the join with query_data."
        )

    def compiled_sql(self, query: SemanticQuery) -> str:
        """The SQL ``query`` compiles to, reading every table by its own name."""
        if query.joins:
            return self._compiler.compile_joined(query)
        return str(self._compiler.compile(query).sql)

    def run(
        self,
        table: str | TableRef,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Sequence[Mapping[str, Any]] = (),
        order_by: str = "",
        limit: int = MAX_RESULT_ROWS,
        scope: AccessScope = UNRESTRICTED,
    ) -> QueryResult:
        """Answer a metric question, as ``scope`` allows.

        Raises:
            QueryError: For a malformed metric, an unknown measure or column,
                or a table too large to answer for — the planner's refusal
                names the size, the limit and the ways forward.
        """
        query = self.build(table, metrics, dimensions, filters, order_by, limit)
        if query.joins:
            return self._run_joined(query, scope)
        ref = query.model.ref
        row_filtered = False
        try:
            if scope.is_restricted:
                query, row_filtered = self._restricted(query, scope)
            compiled, _ = self._materialize_and_compile(query)
            logger.info("Semantic query on %s: %s", ref.full_name, compiled.sql[:200])
            result = self._query.run_local(compiled.sql)
        except Exception as e:
            status = 403 if isinstance(e, GuardrailViolation) else 400
            self._query.record_access(scope, [ref], status=status, row_filtered=row_filtered)
            raise
        self._query.record_access(
            scope, [ref], row_count=result.row_count, row_filtered=row_filtered
        )
        return result

    def _run_joined(self, query: SemanticQuery, scope: AccessScope) -> QueryResult:
        """Answer a query that reads referenced tables, through the query pipeline.

        The generated SELECT is handed over like a caller's own: the pipeline
        loads what it reads, wraps every table in the caller's row rules,
        checks the joins when grounding is on, and records one access. It is
        never pushed down, because it was generated for the store.
        """
        if scope.is_restricted:
            self._refuse_masked(query, scope)
        sql = self._compiler.compile_joined(query)
        logger.info("Semantic query on %s with joins: %s", query.model.ref.full_name, sql[:200])
        return self._query.sql(sql, scope=scope, local_only=True)

    @staticmethod
    def _refuse_masked(query: SemanticQuery, scope: AccessScope) -> None:
        """Refuse a joined query that reads, or joins on, a column masked for the caller.

        The single-table path refuses the same thing. A masked key is refused
        too: masked on one side only, it would match nothing and the answer
        would be an empty result that looks like a fact.
        """
        read: dict[TableRef, set[str]] = {query.model.ref: set(query.required_columns())}
        for joined, names in query.joined_columns().items():
            read.setdefault(joined, set()).update(names)
        for edge in query.joins:
            read.setdefault(edge.child, set()).add(edge.column)
            read.setdefault(edge.parent, set()).add(edge.ref_column)
        try:
            for table, columns in read.items():
                hidden = sorted(set(scope.masks_for(table.source, table.table)) & columns)
                if hidden:
                    raise GuardrailViolation(
                        f"Guardrail: {', '.join(hidden)} in {table.full_name} is masked for "
                        f"this caller and cannot be measured, grouped by, filtered or joined on."
                    )
        except PolicyError as e:
            raise GuardrailViolation(f"Guardrail: {e}") from e

    @staticmethod
    def _restricted(query: SemanticQuery, scope: AccessScope) -> tuple[SemanticQuery, bool]:
        """``query`` with the caller's row rules as filters, and whether there were any.

        Row rules use the vocabulary filters do, so they become filters: they
        reach the slice, the generated WHERE and the cache key by the paths a
        caller's own filter already takes. A masked column has no such
        translation, since an aggregate over a masked value means nothing, so
        a question that reads one is refused.
        """
        ref = query.model.ref
        try:
            conditions = scope.conditions_for(ref.table)
            masks = scope.masks_for(ref.source, ref.table)
        except PolicyError as e:
            raise GuardrailViolation(f"Guardrail: {e}") from e
        hidden = sorted(set(masks) & query.required_columns())
        if hidden:
            raise GuardrailViolation(
                f"Guardrail: {', '.join(hidden)} in {ref.full_name} is masked for this "
                f"caller and cannot be measured, grouped by or filtered on."
            )
        rules = tuple(
            Predicate(column, op, tuple(value) if op == "in" else value)  # type: ignore[arg-type]
            for column, op, value in conditions
        )
        return replace(query, filters=query.filters + rules), bool(rules)

    def explain(
        self,
        table: str | TableRef,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        filters: Sequence[Mapping[str, Any]] = (),
        order_by: str = "",
        limit: int = MAX_RESULT_ROWS,
    ) -> dict[str, Any]:
        """What ``run`` would do, without running it."""
        query = self.build(table, metrics, dimensions, filters, order_by, limit)
        if query.joins:
            # Planned when it runs, by the query pipeline: nothing is loaded here.
            return {"query": query.as_dict(), "sql": self.compiled_sql(query)}
        compiled, tables = self._materialize_and_compile(query)
        return {
            "query": query.as_dict(),
            "sql": compiled.sql,
            "base_sql": compiled.base_sql,
            "columns": list(compiled.columns),
            "shape": query.query_shape().as_dict(),
            "reading": {ref.full_name: name for ref, name in tables.items()},
        }

    def _materialize_and_compile(self, query: SemanticQuery) -> tuple[Any, dict[TableRef, str]]:
        """Load the slices the query needs, then generate SQL against them."""
        shape = query.query_shape()
        tables = self._query.materialize_for(shape)
        if query.model.ref not in tables:
            raise QueryError(
                f"No connected source holds {query.model.ref.full_name}. "
                f"Call list_sources to see what is available."
            )
        compiled = self._compiler.compile(query, tables)
        if self._cross_check:
            self._verify_shape(query, compiled, shape)
        return compiled, tables

    def _verify_shape(self, query: SemanticQuery, compiled: Any, shape: Any) -> None:
        """Check the shape we planned from against the SQL we actually generated.

        Both come from the same query, so they agree by construction — but a
        disagreement would silently drop rows, which is the one failure this
        layer must not have. Reading the generated scan back with the analyzer
        costs one parse and turns that into a log line.
        """
        try:
            observed = self._query.analyzer.analyze(compiled.base_sql)
        except Exception as e:  # pragma: no cover - the analyzer swallows its own errors
            logger.warning("Could not cross-check the generated scan: %s", e)
            return
        merged = shape.merge_conservative(observed)
        if merged != shape:
            logger.warning(
                "Generated scan disagrees with the planned shape for %s; planned %s, read back %s",
                query.model.ref.full_name,
                shape.as_dict(),
                observed.as_dict(),
            )

    # -- helpers ------------------------------------------------------------

    def _resolve(self, table: str | TableRef) -> TableRef:
        """Resolve a table name against the catalog.

        An unqualified name takes the first source that has a table by that
        name, which is the rule the SQL planner already uses — so
        ``query_metrics('orders', ...)`` and ``FROM orders`` agree.
        """
        known = self._catalog.list_tables()
        if isinstance(table, str):
            validate_identifier(table, "table name")
            if "." not in table:
                match = next((ref for ref in known if ref.table == table), None)
                if match is not None:
                    return match
                raise QueryError(self._unknown_table(table, known))
        try:
            ref = coerce_ref(table)
        except SchemaError as e:
            raise QueryError(str(e)) from e
        if self._catalog.has_table(ref):
            return ref
        raise QueryError(self._unknown_table(str(table), known))

    @staticmethod
    def _unknown_table(table: str, known: Sequence[TableRef]) -> str:
        available = ", ".join(ref.full_name for ref in known) or "none"
        return f"Unknown table '{table}'. Connected tables: {available}."


def _predicate(raw: Mapping[str, Any]) -> Predicate:
    """Build a Predicate from a tool-supplied filter mapping.

    Raises:
        QueryError: On a missing column, an unknown operator, or a value the
            operator cannot use.
    """
    if not isinstance(raw, Mapping):
        raise QueryError(f"Each filter must be an object with 'column' and 'value', got {raw!r}.")
    column = raw.get("column")
    if not isinstance(column, str) or not column:
        raise QueryError(f"A filter needs a 'column', got {raw!r}.")
    validate_identifier(column, "column name")
    op = str(raw.get("op", "eq"))
    if op not in PREDICATE_OPS:
        raise QueryError(
            f"Unknown filter operator '{op}' on '{column}'. "
            f"Available: {', '.join(sorted(PREDICATE_OPS))}."
        )
    operator = cast(PredicateOp, op)
    if op != "is_null" and "value" not in raw:
        raise QueryError(f"Filter on '{column}' with op '{op}' needs a 'value'.")
    try:
        return Predicate(column=column, op=operator, value=raw.get("value"))
    except ValueError as e:
        raise QueryError(f"Invalid filter on '{column}': {e}") from e


def _looks_like_a_key(name: str) -> bool:
    """Whether a column name reads as an identifier rather than a quantity."""
    lowered = name.lower()
    return lowered == "id" or lowered.endswith(("_id", "_key", "_code"))


def _string_list(value: Any, label: str, example: str) -> tuple[str, ...]:
    """Coerce a tool argument that must be a list of names.

    ``None`` means "none given". A bare string is the mistake a model makes
    most often, and naming it is more useful than a type error from three
    frames deeper.

    Raises:
        QueryError: When the value is not a list of strings.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raise QueryError(f"{label} must be a list, e.g. {example}.")
    if not isinstance(value, Sequence):
        raise QueryError(f"{label} must be a list, got {value!r}. For example: {example}.")
    for item in value:
        if not isinstance(item, str):
            raise QueryError(
                f"Every entry in {label} must be a name, got {item!r}. For example: {example}."
            )
    return tuple(value)


def _crosses(path: tuple[JoinEdge, ...]) -> bool:
    """Whether ``path`` goes down to a link table first, as ``JoinGraph.across`` paths do.

    Going up, each step starts where the last one ended; across, the first two
    steps both start at the link table.
    """
    return len(path) >= 2 and path[1].child == path[0].child
