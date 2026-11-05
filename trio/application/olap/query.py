"""QueryService: the SQL execution pipeline.

validate -> cache lookup -> plan -> pushdown (if eligible) | lazy-load ->
execute on the analytics store -> cache.

A caller with row rules or masked columns takes a second path through the same
steps: every table the query reads is replaced by that caller's view of it
before anything runs, and nothing is pushed down. Either way one audit event
is recorded per query.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from trio.application.olap.planner import FetchPlanner
from trio.application.olap.sources import SourceService
from trio.domain.governance.access import UNRESTRICTED, AccessScope, TableView
from trio.domain.governance.audit import AuditEvent
from trio.domain.governance.row_policy import PolicyError
from trio.domain.olap.catalog import SchemaCatalog
from trio.domain.olap.errors import GuardrailViolation, QueryError
from trio.domain.olap.models import FetchPlan, QueryResult, TableRef
from trio.domain.olap.query_shape import UNKNOWN_SHAPE, QueryShape
from trio.domain.olap.slices import Predicate
from trio.ports.access import AccessRewriter
from trio.ports.analytics_store import AnalyticsStore
from trio.ports.audit import AuditSink, NullAuditSink
from trio.ports.cache import QueryCache
from trio.ports.data_source import PushdownCapable
from trio.ports.sql_policy import SqlAnalyzer, SqlValidator

logger = logging.getLogger(__name__)


class QueryService:
    def __init__(
        self,
        validator: SqlValidator,
        analyzer: SqlAnalyzer,
        planner: FetchPlanner,
        cache: QueryCache,
        store: AnalyticsStore,
        sources: SourceService,
        *,
        catalog: SchemaCatalog | None = None,
        access: AccessRewriter | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self._catalog = catalog
        self._access = access
        self._audit: AuditSink = audit or NullAuditSink()
        self._validator = validator
        self._analyzer = analyzer
        self._planner = planner
        self._cache = cache
        self._store = store
        self._sources = sources

    @property
    def planner(self) -> FetchPlanner:
        return self._planner

    @property
    def analyzer(self) -> SqlAnalyzer:
        return self._analyzer

    def sql(
        self,
        query: str,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        auto_load: bool = True,
        params: Sequence[Any] | None = None,
        local_only: bool = False,
        scope: AccessScope = UNRESTRICTED,
    ) -> QueryResult:
        """Run a read-only query as ``scope`` allows, and record that it ran.

        A restricted ``scope`` (row rules or masked columns) reads every table
        through that caller's view of it and never pushes down; see
        ``_sql_scoped``. The rest of this describes the unrestricted path.
        """
        applied: dict[TableRef, TableView] = {}
        try:
            self._validator.validate(query)
            if scope.is_restricted:
                result = self._sql_scoped(
                    query, use_cache, cache_ttl, auto_load, params, scope, applied
                )
            else:
                result = self._sql_open(query, use_cache, cache_ttl, auto_load, params, local_only)
        except Exception as e:
            status = 403 if isinstance(e, GuardrailViolation) else 400
            self.record_access(scope, self._tables_of(query), status=status, views=applied)
            raise
        self.record_access(scope, self._tables_of(query), row_count=result.row_count, views=applied)
        return result

    def _sql_open(
        self,
        query: str,
        use_cache: bool,
        cache_ttl: int | None,
        auto_load: bool,
        params: Sequence[Any] | None,
        local_only: bool,
    ) -> QueryResult:
        """The pipeline for a caller nothing is withheld from.

        ``params`` bind to ``?`` placeholders and force execution on the
        analytics store (pushdown is skipped because ``?`` binding is
        store-specific).

        ``local_only`` says this SQL was written for the analytics store and
        must not be shipped to a source, whatever the plan would allow. It is a
        correctness flag, not an optimization: the engines disagree on things
        that parse either way and answer differently — interval literals,
        ``NULLS FIRST``/``NULLS LAST`` defaults inside a window, integer
        division. Pushdown swallows failures and falls back, so a *syntax*
        mismatch is merely slow; a *semantic* one is silently wrong.
        """
        if use_cache:
            cached = self._cache.get(query, params)
            if cached is not None:
                logger.debug("Cache hit for query: %s", query[:80])
                return cached

        executed = query
        if auto_load and self._sources.has_sources:
            plan = self._planner.plan_query(query, estimator=self._sources.estimate_slice)

            if (
                params is None
                and not local_only
                and plan.fetch.pushdown_eligible
                and plan.fetch.source_name
            ):
                source = self._sources.pushdown_source(plan.fetch.source_name)
                if source is not None:
                    try:
                        result = self._execute_pushdown(query, plan.fetch, source)
                    except Exception as e:
                        logger.info("Pushdown failed, falling back to local execution: %s", e)
                    else:
                        if use_cache:
                            self._cache.put(query, result, ttl=cache_ttl)
                        return result

            if plan.is_refused:
                raise QueryError(plan.refusal)
            for table_name in plan.evictions:
                self._sources.evict_slice(table_name)
            loaded = self._sources.ensure_slices(plan.targets)
            mapping = {ref: name for ref, name in loaded.items() if name != ref.full_name}
            if mapping:
                # Only the table names change; the original WHERE and ON
                # clauses stay, so a slice can only narrow what is scanned.
                executed = self._analyzer.rewrite_tables(query, mapping)
                if executed != query:
                    logger.debug("Reading slices: %s", executed[:160])

        return self._execute_and_cache(
            executed, key=query, use_cache=use_cache, cache_ttl=cache_ttl, params=params
        )

    # -- restricted callers ---------------------------------------------------

    def _sql_scoped(
        self,
        query: str,
        use_cache: bool,
        cache_ttl: int | None,
        auto_load: bool,
        params: Sequence[Any] | None,
        scope: AccessScope,
        applied: dict[TableRef, TableView],
    ) -> QueryResult:
        """Run ``query`` so that it can read only what ``scope`` allows.

        The store holds whole rows and raw values, for every caller at once.
        So the query is not trusted to stay inside its tenant: each table it
        names is swapped for a subquery carrying the row rules and the masks,
        and a table that cannot be swapped refuses the query. ``applied``
        collects the views used, for the audit event.

        Never pushed down (the masks are written for the store), and cached
        under the rewritten SQL, so two tenants cannot share an entry.
        """
        if self._access is None or self._catalog is None:
            raise GuardrailViolation(
                "Guardrail: row rules or column masks apply to this caller, and this "
                "engine was built without an access rewriter"
            )
        resolved: dict[TableRef, TableRef] = {}
        rules: dict[TableRef, tuple[tuple[tuple[str, str, Any], ...], dict[str, str]]] = {}
        try:
            for written in self._analyzer.table_references(query):
                target = self._resolve(written)
                if target is None:
                    raise GuardrailViolation(
                        f"Guardrail: '{written}' is not a source table. Row rules or column "
                        f"masks apply to this caller, so materialized views and tables "
                        f"outside the connected sources are out of reach."
                    )
                resolved[written] = target
                rules[target] = (
                    scope.conditions_for(target.table),
                    scope.masks_for(target.source, target.table),
                )
        except PolicyError as e:
            raise GuardrailViolation(f"Guardrail: {e}") from e

        loaded: dict[TableRef, str] = {}
        if auto_load and self._sources.has_sources:
            shape = _scoped_shape(self._analyzer.analyze(query), resolved, rules)
            plan = self._planner.plan_query(
                query, estimator=self._sources.estimate_slice, shape=shape
            )
            if plan.is_refused:
                raise QueryError(plan.refusal)
            for table_name in plan.evictions:
                self._sources.evict_slice(table_name)
            loaded = self._sources.ensure_slices(plan.targets)

        views: dict[TableRef, TableView] = {}
        for written, target in resolved.items():
            name = loaded.get(target, target.full_name)
            conditions, masks = rules[target]
            # The stored table decides the columns: a slice may hold fewer.
            columns = tuple(column.name for column in self._store.describe(name))
            views[written] = applied[target] = TableView(name, columns, conditions, masks)
        secured = self._access.secure(query, views)
        logger.debug("Restricted query: %s", secured[:200])

        if use_cache:
            cached = self._cache.get(secured, params)
            if cached is not None:
                return cached
        return self._execute_and_cache(
            secured, key=secured, use_cache=use_cache, cache_ttl=cache_ttl, params=params
        )

    def _resolve(self, ref: TableRef) -> TableRef | None:
        """The source table ``ref`` names, by the planner's rule, or None."""
        if self._catalog is None or ref.is_view:
            return None
        if ref.source:
            return ref if self._catalog.has_table(ref) else None
        return next(
            (known for known in self._catalog.list_tables() if known.table == ref.table), None
        )

    def _tables_of(self, query: str) -> list[TableRef]:
        return [self._resolve(ref) or ref for ref in self._analyzer.table_references(query)]

    def record_access(
        self,
        scope: AccessScope,
        tables: Iterable[TableRef],
        *,
        row_count: int | None = None,
        status: int = 200,
        views: Mapping[TableRef, TableView] | None = None,
        row_filtered: bool = False,
        masked_columns: Iterable[str] = (),
    ) -> None:
        """Record one analytical read: who, which tables, and what was withheld.

        Table and column names only; never the SQL, whose literals are data.
        """
        refs = list(tables)
        views = views or {}
        masked = {*masked_columns} | {
            f"{ref.table}.{column}" for ref, view in views.items() for column in view.masked_columns
        }
        self._audit.record(
            AuditEvent(
                action="query",
                database=",".join(sorted({ref.source for ref in refs if ref.source})),
                table=",".join(ref.table for ref in refs),
                actor=scope.actor,
                tenant=scope.tenant,
                roles=scope.roles,
                request_id=scope.request_id,
                status=status,
                row_count=row_count,
                row_filtered=row_filtered or any(view.conditions for view in views.values()),
                masked_columns=tuple(sorted(masked)),
            )
        )

    def materialize_for(self, shape: QueryShape) -> dict[TableRef, str]:
        """Plan and load what a pre-analyzed query needs.

        The caller derived ``shape`` itself instead of handing over SQL to be
        parsed, which is what lets a windowed query be sliced at all.

        Returns:
            Where each table now lives in the store — its own name, or a slice.

        Raises:
            QueryError: With the planner's refusal, which already names the
                table, its size, the limit it exceeded and the ways forward.
        """
        plan = self._planner.plan_shape(shape, estimator=self._sources.estimate_slice)
        if plan.is_refused:
            raise QueryError(plan.refusal)
        for table_name in plan.evictions:
            self._sources.evict_slice(table_name)
        return self._sources.ensure_slices(plan.targets)

    def run_local(
        self, sql: str, *, use_cache: bool = True, cache_ttl: int | None = None
    ) -> QueryResult:
        """Validate and run SQL this process generated, against tables it already loaded.

        No planning, no table rewriting and no pushdown: the caller has already
        materialized what the query reads and written the store's own table
        names into it. Guardrails still apply — generated SQL is validated
        exactly like SQL from a caller.
        """
        self._validator.validate(sql)
        if use_cache:
            cached = self._cache.get(sql, None)
            if cached is not None:
                logger.debug("Cache hit for generated query: %s", sql[:80])
                return cached
        return self._execute_and_cache(
            sql, key=sql, use_cache=use_cache, cache_ttl=cache_ttl, params=None
        )

    def _execute_and_cache(
        self,
        executed: str,
        *,
        key: str,
        use_cache: bool,
        cache_ttl: int | None,
        params: Sequence[Any] | None,
    ) -> QueryResult:
        """Run ``executed`` on the store and cache the result under ``key``."""
        started = time.perf_counter()
        rows = self._store.execute(executed, params)
        elapsed_ms = (time.perf_counter() - started) * 1000
        result = QueryResult.from_rowset(rows, sql=key, execution_time_ms=elapsed_ms)

        if use_cache:
            self._cache.put(key, result, ttl=cache_ttl, params=params)
        logger.debug("Query executed in %.1fms (%d rows)", elapsed_ms, result.row_count)
        return result

    def _execute_pushdown(
        self, query: str, plan: FetchPlan, source: PushdownCapable
    ) -> QueryResult:
        assert plan.source_name is not None
        rewritten = self._analyzer.strip_source_prefix(query, plan.source_name)
        logger.info("Pushdown query to %s: %s", plan.source_name, rewritten[:120])
        started = time.perf_counter()
        rows = source.execute_query(rewritten)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return QueryResult.from_rowset(rows, sql=query, execution_time_ms=elapsed_ms)


def _scoped_shape(
    shape: QueryShape,
    resolved: Mapping[TableRef, TableRef],
    rules: Mapping[TableRef, tuple[tuple[tuple[str, str, Any], ...], dict[str, str]]],
) -> QueryShape:
    """What to fetch for a query that will run through per-caller views.

    The row rules are added to each table's predicates, which is what keeps a
    tenant's query on a large table to that tenant's slice. A condition or a
    join key on a masked column is taken out: the query will compare the
    masked value, so fetching by the raw one would let a caller test guesses
    against it by counting rows.
    """
    if not shape.is_simple_select:
        return shape
    uses = []
    masked: dict[str, dict[str, str]] = {}
    dropped = False
    for use in shape.tables:
        target = resolved.get(use.ref)
        if target is None:
            return UNKNOWN_SHAPE
        conditions, masks = rules[target]
        kept = tuple(p for p in use.predicates if p.column not in masks)
        dropped = dropped or len(kept) != len(use.predicates)
        policy = tuple(
            Predicate(column, op, tuple(value) if op == "in" else value)  # type: ignore[arg-type]
            for column, op, value in conditions
        )
        columns = (
            None if use.columns is None else use.columns | {column for column, _, _ in conditions}
        )
        uses.append(replace(use, ref=target, columns=columns, predicates=kept + policy))
        masked[use.alias] = masks
    joins = tuple(
        join
        for join in shape.joins
        if join.left_column not in masked.get(join.left_alias, {})
        and join.right_column not in masked.get(join.right_alias, {})
    )
    # A LIMIT read at the source would now cut rows before a dropped condition ran.
    return replace(shape, tables=tuple(uses), joins=joins, limit=None if dropped else shape.limit)
