"""Composition root: wire adapters into the application services.

This is the only module that imports both the application layer and the
concrete adapters. Every dependency can be overridden (tests inject fakes).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from trio.application.olap.app import FusionApp
from trio.application.olap.backup import BackupService
from trio.application.olap.planner import FetchPlanner
from trio.application.olap.query import QueryService
from trio.application.olap.semantic import SemanticService
from trio.application.olap.settings import Settings
from trio.application.olap.sources import SourceService
from trio.application.olap.tools import ToolService
from trio.application.olap.views import MaterializedViewService
from trio.domain.olap.catalog import SchemaCatalog
from trio.ports.analytics_store import AnalyticsStore
from trio.ports.cache import QueryCache
from trio.ports.data_source import DatabaseDiscovery, SourceFactory
from trio.ports.scheduler import Scheduler
from trio.ports.semantic_compiler import SemanticCompiler
from trio.ports.sql_policy import SqlAnalyzer, SqlValidator


def build_app(
    settings: Settings | None = None,
    *,
    store: AnalyticsStore | None = None,
    cache: QueryCache | None = None,
    scheduler: Scheduler | None = None,
    source_factory: SourceFactory | None = None,
    validator: SqlValidator | None = None,
    analyzer: SqlAnalyzer | None = None,
    semantic_compiler: SemanticCompiler | None = None,
    clock: Callable[[], float] = time.time,
) -> FusionApp:
    """Build a ready-to-use FusionApp from ``settings`` (defaults: DuckDB in-memory,
    in-process cache, threading scheduler, Warp source registry)."""
    settings = settings or Settings()

    if store is None:
        from trio.adapters.outbound.duckdb_store import DuckDBStore

        store = DuckDBStore(
            database=settings.database,
            threads=settings.threads,
            memory_limit=settings.memory_limit,
            external_access=settings.external_access,
            max_temp_directory_size=settings.max_temp_directory_size or None,
        )
    if cache is None:
        from trio.adapters.outbound.memory_cache import MemoryQueryCache

        cache = MemoryQueryCache(
            max_entries=settings.cache_max_entries, default_ttl=settings.cache_ttl
        )
    if scheduler is None:
        from trio.adapters.outbound.threading_scheduler import ThreadingScheduler

        scheduler = ThreadingScheduler()
    if validator is None or analyzer is None:
        from trio.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator

        validator = validator or SqlglotValidator()
        analyzer = analyzer or SqlglotAnalyzer()
    if semantic_compiler is None:
        from trio.adapters.outbound.sqlglot_semantic import SqlglotSemanticCompiler

        semantic_compiler = SqlglotSemanticCompiler()
    if source_factory is None:
        from trio.adapters.outbound.registry import default_registry

        source_factory = default_registry(settings.warp_http_defaults()).create

    catalog = SchemaCatalog()
    policy = settings.policy()
    planner = FetchPlanner(catalog, analyzer, policy, clock)
    sources = SourceService(
        catalog,
        store,
        source_factory,
        max_ingest_rows=settings.max_ingest_rows,
        scheduler=scheduler,
        policy=policy,
        clock=clock,
        refresh_config=settings.refresh_specs(),
        # Refreshed data and rebuilt views change what a query returns, so
        # anything cached from before must go.
        on_data_changed=cache.clear,
    )
    query = QueryService(validator, analyzer, planner, cache, store, sources)
    views = MaterializedViewService(
        store, validator, planner, sources, scheduler, clock, on_data_changed=cache.clear
    )
    backup = BackupService(
        store,
        settings.backup_path,
        interval=settings.backup_interval,
        retention_days=settings.backup_retention_days,
        enabled=settings.backup_enabled,
        scheduler=scheduler,
        clock=clock,
    )
    semantic = SemanticService(catalog, query, semantic_compiler, settings.semantic_models())
    tools = ToolService(
        query, sources, views, store, catalog, cache, planner, analyzer, semantic=semantic
    )

    return FusionApp(
        settings=settings,
        catalog=catalog,
        store=store,
        cache=cache,
        scheduler=scheduler,
        sources=sources,
        query=query,
        views=views,
        backup=backup,
        semantic=semantic,
        tools=tools,
    )


def default_discovery(settings: Settings | None = None) -> DatabaseDiscovery:
    """The Warp database-discovery adapter (used by the CLIs for --auto-discover).

    With ``settings`` the Warp API key and header are applied to the probe.
    """
    from trio.adapters.outbound.warp_http.discovery import WarpDiscovery

    if settings is None:
        return WarpDiscovery()
    return WarpDiscovery(
        api_key=settings.warp_api_key or None, api_key_header=settings.warp_api_key_header
    )
