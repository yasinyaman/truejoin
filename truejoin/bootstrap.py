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

from truejoin.adapters.outbound.audit import JsonlAuditReader, LoggingAuditSink
from truejoin.adapters.outbound.catalog_store.file_store import CatalogFileStore
from truejoin.adapters.outbound.database_store.encrypted_json import (
    DatabaseStoreKeyError,
    EncryptedJsonDatabaseStore,
)
from truejoin.adapters.outbound.db.comment_reader import CommentReader
from truejoin.adapters.outbound.db.dialect import dialect_or_generic
from truejoin.adapters.outbound.db.factory import DatabaseFactory
from truejoin.adapters.outbound.db.history import QueryHistoryReader
from truejoin.adapters.outbound.db.sample_reader import SampleReader
from truejoin.adapters.outbound.export.registry import default_exporters
from truejoin.adapters.outbound.key_store.json_file import JsonFileKeyStore
from truejoin.adapters.outbound.llm.providers import LLMClient
from truejoin.adapters.outbound.mcp_gateway.json_server_repository import JsonServerRepository
from truejoin.adapters.outbound.mcp_gateway.parsers.manual import ManualParser
from truejoin.adapters.outbound.mcp_gateway.parsers.openapi import OpenAPIParser
from truejoin.adapters.outbound.mcp_gateway.parsers.postman import PostmanParser
from truejoin.adapters.outbound.mcp_gateway.spec_loader import default_spec_loader
from truejoin.adapters.outbound.settings_store.encrypted_json import EncryptedJsonSettingsStore
from truejoin.adapters.outbound.sqlglot_expressions import SqlglotExpressionMiner
from truejoin.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator
from truejoin.adapters.outbound.user_store.json_file import JsonFileUserStore
from truejoin.application.access_keys import AccessKeyService
from truejoin.application.catalog.catalog_analysis import CatalogAnalysisService
from truejoin.application.catalog.catalog_export import CatalogExportService
from truejoin.application.catalog.catalog_review import CatalogReviewService
from truejoin.application.catalog.cross_reference import CrossReferenceService
from truejoin.application.catalog.history_mining import HistoryMiningService
from truejoin.application.container import AnalysisFactory, Container, MiningFactory
from truejoin.application.databases import DatabaseService
from truejoin.application.local_users import LocalUsers
from truejoin.application.mcpgen.convert import ConversionService, ParserRegistry
from truejoin.application.olap.app import FusionApp
from truejoin.application.olap.backup import BackupService
from truejoin.application.olap.planner import FetchPlanner
from truejoin.application.olap.query import QueryService
from truejoin.application.olap.semantic import SemanticService
from truejoin.application.olap.settings import Settings
from truejoin.application.olap.sources import SourceService
from truejoin.application.olap.tools import ToolService
from truejoin.application.olap.views import MaterializedViewService
from truejoin.application.settings import DatabaseConfig, legacy_env_overlay
from truejoin.application.settings import Settings as AppSettings
from truejoin.application.settings_service import SettingsError, SettingsService
from truejoin.domain.gateway.errors import ConfigurationError
from truejoin.domain.governance.access import PSEUDONYM_FUNCTION
from truejoin.domain.governance.masking import mask_value
from truejoin.domain.mcp.api import HTTPMethod, UnifiedAPISchema
from truejoin.domain.mcp.mcp import MCPConversionResult
from truejoin.domain.olap.catalog import SchemaCatalog
from truejoin.ports.analytics_store import AnalyticsStore
from truejoin.ports.audit import AuditSink, NullAuditSink
from truejoin.ports.cache import QueryCache
from truejoin.ports.catalog_repository import CatalogRepository
from truejoin.ports.data_source import DatabaseDiscovery, SourceFactory
from truejoin.ports.database import DatabaseGateway, DatabaseGatewayFactory
from truejoin.ports.scheduler import Scheduler
from truejoin.ports.semantic_compiler import SemanticCompiler
from truejoin.ports.server_repository import ServerDefinitionRepository
from truejoin.ports.spec_loader import SpecSource
from truejoin.ports.spec_parser import SpecParser
from truejoin.ports.sql_policy import SqlAnalyzer, SqlValidator
from truejoin.ports.text_generation import TextGenerator

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
    mining_factory: MiningFactory | None = None,
) -> Container:
    """Wire the default adapters; any of them can be replaced (tests, embedding)."""
    gateways = gateway_factory or DatabaseFactory()
    settings_service = _settings_service(settings)
    if settings_service is not None:
        try:
            settings = settings_service.merged()
        except SettingsError as e:
            # The server still starts, on the file's settings; the console shows the rest.
            logger.error("The settings changed from the console do not apply: %s", e)
    databases = _databases(settings, gateways)
    managed: frozenset[str] = frozenset()
    if databases is not None:
        stored = [DatabaseConfig(**db.connection()) for db in databases.all()]
        managed = frozenset(db.name for db in stored)
        # A copy: the caller's settings keep only what its file says.
        settings = settings.model_copy(update={"databases": [*settings.databases, *stored]})
    repo = repository if repository is not None else build_repository(settings)
    text_generators = text_generator_factory or (lambda: LLMClient.from_config(settings))
    # A second client only when the slot names a model of its own, and never
    # behind a factory the caller supplied (a test's fake stands for both).
    light = (
        (lambda: LLMClient.from_config(settings, "non_reasoning"))
        if text_generator_factory is None and settings.settings.llm.non_reasoning is not None
        else None
    )

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

    def default_mining(
        gateway: DatabaseGateway, db_config: DatabaseConfig, text_generator: TextGenerator
    ) -> HistoryMiningService:
        dialect = dialect_or_generic(db_config.type)
        return HistoryMiningService(
            history=QueryHistoryReader(
                gateway,
                db_type=dialect,
                schema=dialect.default_schema(db_config.database, db_config.options),
                database=db_config.database,
            ),
            miner=SqlglotExpressionMiner(),
            text_generator=text_generator,
            review=CatalogReviewService(repo),
            db_type=dialect.name,
            database_name=db_config.name,
            language=settings.settings.i18n.default_language,
        )

    audit = _audit_sink(settings)
    return Container(
        settings=settings,
        repository=repo,
        review=CatalogReviewService(repo),
        export=build_export_service(),
        gateway_factory=gateways,
        text_generator_factory=text_generators,
        light_generator_factory=light,
        analysis_factory=analysis_factory or default_analysis,
        mining_factory=mining_factory or default_mining,
        audit=audit,
        expressions=SqlglotExpressionMiner(),
        sql_validator=SqlglotValidator(),
        sql_analyzer=SqlglotAnalyzer(),
        audit_reader=(
            JsonlAuditReader(settings.settings.audit.file)
            if settings.settings.audit.enabled and settings.settings.audit.file
            else None
        ),
        olap_factory=(
            partial(build_olap, settings, gateways, audit=audit, managed=managed)
            if settings.olap.enabled
            else None
        ),
        access_keys=_access_keys(settings),
        local_users=build_local_users(settings),
        databases=databases,
        settings_service=settings_service,
    )


def _settings_service(settings: AppSettings) -> SettingsService | None:
    """The settings changed from the console, when ``settings_store.path`` names a file.

    Raises:
        ConfigurationError: The store is set but its key is not in the environment.
    """
    config = settings.settings.settings_store
    if not config.path:
        return None
    try:
        store = EncryptedJsonSettingsStore(config.path, os.environ.get(config.key_env, ""))
    except DatabaseStoreKeyError as e:
        raise ConfigurationError(
            f"settings_store.path is set but {config.key_env} holds no usable key ({e})"
        ) from e
    return SettingsService(store, settings)


def _databases(settings: AppSettings, gateways: DatabaseGatewayFactory) -> DatabaseService | None:
    """The databases added from the console, when ``database_store.path`` names a file.

    Raises:
        ConfigurationError: The store is set but its key is not in the environment.
    """
    config = settings.settings.database_store
    if not config.path:
        return None
    key = os.environ.get(config.key_env, "")
    try:
        store = EncryptedJsonDatabaseStore(config.path, key)
    except DatabaseStoreKeyError as e:
        raise ConfigurationError(
            f"database_store.path is set but {config.key_env} holds no usable key ({e}); "
            "generate one with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'`"
        ) from e
    return DatabaseService(
        store,
        configured=[db.name for db in settings.databases],
        supported_types=gateways.get_supported_types(),
    )


def _access_keys(settings: AppSettings) -> AccessKeyService | None:
    """The handed-out keys, when ``auth.key_store`` names a file for them."""
    auth = settings.settings.auth
    if not auth.key_store:
        return None
    return AccessKeyService(
        JsonFileKeyStore(auth.key_store), reserved=[key.name for key in auth.api_keys]
    )


def build_local_users(settings: AppSettings) -> LocalUsers | None:
    """The users made from the console, when ``auth.users`` names a file for them."""
    auth = settings.settings.auth
    if auth.users is None:
        return None
    return LocalUsers(
        JsonFileUserStore(auth.users.store),
        auth.users,
        reserved=[key.name for key in auth.api_keys],
    )


def _audit_sink(settings: AppSettings) -> AuditSink:
    """The audit sink for this configuration (a no-op when auditing is off)."""
    config = settings.settings.audit
    if not config.enabled:
        return NullAuditSink()
    return LoggingAuditSink(config.file, filter_columns=config.log_filter_columns)


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
        from truejoin.adapters.outbound.duckdb_store import DuckDBStore

        store = DuckDBStore(
            database=settings.database,
            threads=settings.threads,
            memory_limit=settings.memory_limit,
            external_access=settings.external_access,
            max_temp_directory_size=settings.max_temp_directory_size or None,
            temp_directory=settings.temp_directory or None,
        )
    if cache is None:
        from truejoin.adapters.outbound.memory_cache import MemoryQueryCache

        cache = MemoryQueryCache(
            max_entries=settings.cache_max_entries, default_ttl=settings.cache_ttl
        )
    if scheduler is None:
        from truejoin.adapters.outbound.threading_scheduler import ThreadingScheduler

        scheduler = ThreadingScheduler()
    if validator is None or analyzer is None:
        from truejoin.adapters.outbound.sqlglot_policy import SqlglotAnalyzer, SqlglotValidator

        validator = validator or SqlglotValidator()
        analyzer = analyzer or SqlglotAnalyzer()
    if semantic_compiler is None:
        from truejoin.adapters.outbound.sqlglot_semantic import SqlglotSemanticCompiler

        semantic_compiler = SqlglotSemanticCompiler()
    if source_factory is None:
        from truejoin.adapters.outbound.registry import default_registry

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
    from truejoin.adapters.outbound.sqlglot_access import SqlglotAccessRewriter
    from truejoin.adapters.outbound.sqlglot_grounder import SqlglotGrounder

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
        grounder=SqlglotGrounder(),
        grounding=settings.grounding,
    )
    views = MaterializedViewService(
        store,
        validator,
        planner,
        sources,
        scheduler,
        clock,
        on_data_changed=cache.clear,
        ground=query.ground,
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
        query,
        sources,
        views,
        store,
        catalog,
        cache,
        planner,
        analyzer,
        semantic=semantic,
        audit=audit,
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
    from truejoin.adapters.outbound.warp_http.discovery import WarpDiscovery

    if settings is None:
        return WarpDiscovery()
    return WarpDiscovery(
        api_key=settings.warp_api_key or None, api_key_header=settings.warp_api_key_header
    )


def load_settings(
    config_path: str | None = None, environ: Mapping[str, str] | None = None
) -> AppSettings:
    """The YAML config with the pre-1.0 ``FUSION_*``/``WARP_*`` variables applied on top."""
    from truejoin.adapters.outbound.yaml_config import load_config

    return legacy_env_overlay(load_config(config_path), os.environ if environ is None else environ)


def build_olap(
    settings: AppSettings,
    gateway_factory: DatabaseGatewayFactory,
    env: str = "development",
    audit: AuditSink | None = None,
    managed: frozenset[str] = frozenset(),
) -> FusionApp:
    """The OLAP side of the one process, connected to every configured database.

    Starts the loop thread that owns the OLAP gateways (their pools are
    separate from the HTTP routes' own, which live on the server's loop) and
    connects one in-process source per database; ``FusionApp.close`` stops it.
    A database in ``managed`` (added from the console) that cannot be reached
    is left out rather than stopping the start.
    """
    from truejoin.adapters.outbound.warp_inproc import GatewayLoop
    from truejoin.domain.olap.identifiers import IDENTIFIER_RE

    fusion_settings = settings.olap.to_fusion_settings(env)
    loop = GatewayLoop().start()
    fusion = build_app(fusion_settings, audit=audit)
    fusion.tools.keep_questions = settings.settings.audit.question_text
    fusion.on_close.append(loop.stop)
    if settings.olap.unread_questions == "sql":
        from truejoin.adapters.outbound.llm.providers import LLMClient

        slot = "non_reasoning" if settings.settings.llm.non_reasoning else "reasoning"
        writer = LLMClient.from_config(settings, slot)

        def write_sql(prompt: str, system: str) -> str:
            return loop.submit(writer.generate(prompt, system_prompt=system), timeout=120)

        fusion.tools.write_sql = write_sql
        # The stored values are samples of the data: the same opt-in as analysis.
        fusion.tools.values_to_model = (
            not settings.settings.llm.resolve(slot).is_cloud_provider
            or settings.settings.analysis.share_samples_with_cloud_llm
        )
        fusion.on_close.insert(0, lambda: loop.submit(writer.close(), timeout=10))
    cfg = settings.settings
    if cfg.masking.hash_secret:
        # The `hash` mask in analytical SQL: the same keyed pseudonym the table
        # routes produce, so a masked join key still joins across both.
        key = cfg.masking.hash_secret.encode("utf-8")
        fusion.store.define_function(PSEUDONYM_FUNCTION, lambda text: mask_value(text, "hash", key))

    def attach(db: DatabaseConfig) -> None:
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

    # Called later from the server's loop for a database added from the console.
    fusion.attach_database = attach
    try:
        for db in settings.databases:
            if not IDENTIFIER_RE.match(db.name):
                logger.warning(
                    "Database '%s' is not offered to OLAP: its name cannot be a SQL schema "
                    "(letters, digits, underscore)",
                    db.name,
                )
                continue
            try:
                attach(db)
            except Exception as e:
                if db.name not in managed:
                    raise
                # One added from the console: the console shows it as not connected.
                logger.error("Could not connect the added database %s to OLAP: %s", db.name, e)
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
