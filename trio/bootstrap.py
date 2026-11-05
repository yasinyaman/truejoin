"""Composition root: the only module that knows both the application and the adapters.

Three parts, one per origin, in one place:

- the gateway and catalog side (``build_container``; Warp),
- the OLAP side (``build_app`` for the SDK, ``build_olap`` for the server; Fusion),
- the generic OpenAPI -> MCP converter (``convert_to_mcp``, ``parse_spec``; Stargate).

Every dependency can be overridden (tests inject fakes).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from trio.adapters.outbound.audit import LoggingAuditSink
from trio.adapters.outbound.catalog_store.file_store import CatalogFileStore
from trio.adapters.outbound.db.comment_reader import CommentReader
from trio.adapters.outbound.db.dialect import dialect_or_generic
from trio.adapters.outbound.db.factory import DatabaseFactory
from trio.adapters.outbound.db.sample_reader import SampleReader
from trio.adapters.outbound.export.registry import default_exporters
from trio.adapters.outbound.llm.providers import LLMClient
from trio.adapters.outbound.mcp_gateway.json_server_repository import JsonServerRepository
from trio.adapters.outbound.mcp_gateway.parsers.manual import ManualParser
from trio.adapters.outbound.mcp_gateway.parsers.openapi import OpenAPIParser
from trio.adapters.outbound.mcp_gateway.parsers.postman import PostmanParser
from trio.adapters.outbound.mcp_gateway.spec_loader import default_spec_loader
from trio.application.catalog.catalog_analysis import CatalogAnalysisService
from trio.application.catalog.catalog_export import CatalogExportService
from trio.application.catalog.catalog_review import CatalogReviewService
from trio.application.catalog.cross_reference import CrossReferenceService
from trio.application.container import AnalysisFactory, Container
from trio.application.mcpgen.convert import ConversionService, ParserRegistry
from trio.application.olap.app import FusionApp
from trio.application.olap.backup import BackupService
from trio.application.olap.planner import FetchPlanner
from trio.application.olap.query import QueryService
from trio.application.olap.semantic import SemanticService
from trio.application.olap.settings import Settings
from trio.application.olap.sources import SourceService
from trio.application.olap.tools import ToolService
from trio.application.olap.views import MaterializedViewService
from trio.application.settings import DatabaseConfig, legacy_env_overlay
from trio.application.settings import Settings as AppSettings
from trio.domain.governance.access import PSEUDONYM_FUNCTION
from trio.domain.governance.masking import mask_value
from trio.domain.mcp.api import HTTPMethod, UnifiedAPISchema
from trio.domain.mcp.mcp import MCPConversionResult
from trio.domain.olap.catalog import SchemaCatalog
from trio.ports.analytics_store import AnalyticsStore
from trio.ports.audit import AuditSink, NullAuditSink
from trio.ports.cache import QueryCache
from trio.ports.catalog_repository import CatalogRepository
from trio.ports.data_source import DatabaseDiscovery, SourceFactory
from trio.ports.database import DatabaseGateway, DatabaseGatewayFactory
from trio.ports.scheduler import Scheduler
from trio.ports.semantic_compiler import SemanticCompiler
from trio.ports.server_repository import ServerDefinitionRepository
from trio.ports.spec_loader import SpecSource
from trio.ports.spec_parser import SpecParser
from trio.ports.sql_policy import SqlAnalyzer, SqlValidator
from trio.ports.text_generation import TextGenerator

logger = logging.getLogger(__name__)


# -- gateway and catalog (Warp) ------------------------------------------------


def build_repository(settings: AppSettings) -> CatalogFileStore:
    """File store configured from `settings.catalog`."""
    return CatalogFileStore(
        settings.settings.catalog.storage_path,
        default_format=settings.settings.catalog.default_format,
    )


def build_export_service() -> CatalogExportService:
    """Export service over the default exporter registry."""
    return CatalogExportService(default_exporters())


def build_analysis_service(
    *,
    settings: AppSettings,
    gateway: DatabaseGateway,
    db_config: DatabaseConfig,
    text_generator: TextGenerator,
    repository: CatalogRepository,
) -> CatalogAnalysisService:
    """Wire readers, review and cross-reference around a connected gateway."""
    dialect = dialect_or_generic(db_config.type)
    schema = dialect.default_schema(db_config.database, db_config.options)
    cross_reference = (
        CrossReferenceService(repository, exclude_db=db_config.name)
        if settings.settings.catalog.auto_cross_reference
        else None
    )
    return CatalogAnalysisService(
        gateway=gateway,
        config=settings,
        text_generator=text_generator,
        comments=CommentReader(
            gateway, db_type=dialect, schema=schema, database=db_config.database
        ),
        samples=SampleReader(gateway, db_type=dialect, schema=schema),
        review=CatalogReviewService(repository),
        cross_reference=cross_reference,
        db_type=db_config.type,
        database_name=db_config.name,
    )


def build_container(
    settings: AppSettings,
    *,
    repository: CatalogRepository | None = None,
    gateway_factory: DatabaseGatewayFactory | None = None,
    text_generator_factory: Callable[[], TextGenerator] | None = None,
    analysis_factory: AnalysisFactory | None = None,
) -> Container:
    """Wire the default adapters; any of them can be replaced (tests, embedding)."""
    repo = repository if repository is not None else build_repository(settings)
    text_generators = text_generator_factory or (lambda: LLMClient.from_config(settings))
    gateways = gateway_factory or DatabaseFactory()

    def default_analysis(
        gateway: DatabaseGateway, db_config: DatabaseConfig, text_generator: TextGenerator
    ) -> CatalogAnalysisService:
        return build_analysis_service(
            settings=settings,
            gateway=gateway,
            db_config=db_config,
            text_generator=text_generator,
            repository=repo,
        )

    audit = _audit_sink(settings)
    return Container(
        settings=settings,
        repository=repo,
        review=CatalogReviewService(repo),
        export=build_export_service(),
        gateway_factory=gateways,
        text_generator_factory=text_generators,
        analysis_factory=analysis_factory or default_analysis,
        audit=audit,
        olap_factory=(
            partial(build_olap, settings, gateways, audit=audit) if settings.olap.enabled else None
        ),
    )


def _audit_sink(settings: AppSettings) -> AuditSink:
    """The audit sink for this configuration (a no-op when auditing is off)."""
    config = settings.settings.audit
    if not config.enabled:
        return NullAuditSink()
    return LoggingAuditSink(config.file)


# -- OLAP (Fusion) -------------------------------------------------------------


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
    audit: AuditSink | None = None,
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
    from trio.adapters.outbound.sqlglot_access import SqlglotAccessRewriter

    query = QueryService(
        validator,
        analyzer,
        planner,
        cache,
        store,
        sources,
        catalog=catalog,
        access=SqlglotAccessRewriter(),
        audit=audit,
    )
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


def load_settings(
    config_path: str | None = None, environ: Mapping[str, str] | None = None
) -> AppSettings:
    """The YAML config with the pre-1.0 ``FUSION_*``/``WARP_*`` variables applied on top."""
    from trio.adapters.outbound.yaml_config import load_config

    return legacy_env_overlay(load_config(config_path), os.environ if environ is None else environ)


def build_olap(
    settings: AppSettings,
    gateway_factory: DatabaseGatewayFactory,
    env: str = "development",
    audit: AuditSink | None = None,
) -> FusionApp:
    """The OLAP side of the one process, connected to every configured database.

    Starts the loop thread that owns the OLAP gateways (their pools are
    separate from the HTTP routes' own, which live on the server's loop) and
    connects one in-process source per database; ``FusionApp.close`` stops it.
    """
    from trio.adapters.outbound.warp_inproc import GatewayLoop
    from trio.domain.olap.identifiers import IDENTIFIER_RE

    fusion_settings = settings.olap.to_fusion_settings(env)
    loop = GatewayLoop().start()
    fusion = build_app(fusion_settings, audit=audit)
    fusion.on_close.append(loop.stop)
    cfg = settings.settings
    if cfg.masking.hash_secret:
        # The `hash` mask in analytical SQL: the same keyed pseudonym the table
        # routes produce, so a masked join key still joins across both.
        key = cfg.masking.hash_secret.encode("utf-8")
        fusion.store.define_function(PSEUDONYM_FUNCTION, lambda text: mask_value(text, "hash", key))
    try:
        for db in settings.databases:
            if not IDENTIFIER_RE.match(db.name):
                logger.warning(
                    "Database '%s' is not offered to OLAP: its name cannot be a SQL schema "
                    "(letters, digits, underscore)",
                    db.name,
                )
                continue
            fusion.sources.connect(
                db.name,
                {
                    "type": "warp",
                    # This side only reads: the read-only credentials when there are any.
                    "gateway": gateway_factory.create(db.readonly_config() or db),
                    "loop": loop,
                    "raw_query": cfg.enable_raw_query,
                    "raw_query_whitelist": cfg.raw_query_whitelist,
                    "excluded_tables": cfg.excluded_tables,
                    "batch_size": cfg.export.batch_size,
                    "statement_timeout_ms": cfg.export.statement_timeout_ms,
                },
            )
        remote = settings.olap.remote
        if remote.url:
            names = remote.databases or default_discovery(fusion_settings).discover_databases(
                remote.url
            )
            for name in names:
                fusion.sources.connect(
                    name, {"type": "warp-remote", "base_url": remote.url, "database": name}
                )
    except BaseException:
        fusion.close()
        raise
    return fusion


# -- OpenAPI -> MCP converter (Stargate) ---------------------------------------

DEFAULT_PARSERS: tuple[type[SpecParser], ...] = (OpenAPIParser, PostmanParser, ManualParser)

# Mutable registration list (in priority order); appending a parser class here
# makes it available to the default service. Kept for stargate.converter compat.
PARSERS: list[type[SpecParser]] = list(DEFAULT_PARSERS)


def parser_registry() -> ParserRegistry:
    """Registry over PARSERS."""
    return ParserRegistry(PARSERS)


def conversion_service() -> ConversionService:
    """ConversionService with the default loader and parsers."""
    return ConversionService(loader=default_spec_loader(), parsers=parser_registry())


def server_repository() -> ServerDefinitionRepository:
    """Repository owning the server.json layout."""
    return JsonServerRepository()


def convert_to_mcp(
    source: SpecSource,
    base_url: str | None = None,
    format: str | None = None,
    include_deprecated: bool = False,
    filter_tags: list[str] | None = None,
    filter_methods: list[HTTPMethod] | None = None,
    allowed_domains: set[str] | None = None,
) -> MCPConversionResult:
    """Converts REST API definition to MCP Server.

    Args:
        source: API definition source (URL, file path or dict)
        base_url: API base URL (if not in specification)
        format: Force format ("openapi", "postman", "manual")
        include_deprecated: Include deprecated endpoints
        filter_tags: Only include endpoints with these tags
        filter_methods: Only turn these HTTP methods into tools
        allowed_domains: Allowed domains (SSRF protection)

    Returns:
        MCPConversionResult (server, metadata, warnings, errors)

    Raises:
        ValueError: Invalid source or format
        FileNotFoundError: File not found

    Example:
        >>> result = convert_to_mcp("https://petstore3.swagger.io/api/v3/openapi.json")
        >>> print(f"Tools: {len(result.server.tools)}")
        >>> result = convert_to_mcp("./api-spec.yaml")
        >>> result = convert_to_mcp({"openapi": "3.0.0", "info": {...}, "paths": {...}})
    """
    return conversion_service().convert(
        source,
        base_url=base_url,
        format=format,
        include_deprecated=include_deprecated,
        filter_tags=filter_tags,
        filter_methods=filter_methods,
        allowed_domains=allowed_domains,
    )


def parse_spec(
    source: SpecSource,
    format: str | None = None,
    allowed_domains: set[str] | None = None,
) -> UnifiedAPISchema:
    """Parses API specification (without MCP conversion).

    Args:
        source: API definition source (URL, file path or dict)
        format: Force format
        allowed_domains: Allowed domains

    Returns:
        UnifiedAPISchema
    """
    return conversion_service().parse(source, format=format, allowed_domains=allowed_domains)


def detect_format(spec: dict[str, Any]) -> SpecParser | None:
    """Detects specification format and returns an appropriate parser, or None."""
    return parser_registry().detect(spec)
