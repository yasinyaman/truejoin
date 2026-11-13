"""FastAPI application factory and lifespan (inbound HTTP adapter).

`create_app()` takes a *container factory* so that this module never builds
outbound adapters itself; `truejoin.main` supplies the factory from the
composition root.
"""

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.routing import BaseRoute
from starlette.types import Lifespan

from truejoin import __version__
from truejoin.adapters.inbound.http.arrow_export import arrow_available
from truejoin.adapters.inbound.http.auth import AuthManager, Permission
from truejoin.adapters.inbound.http.capabilities import capabilities_of
from truejoin.adapters.inbound.http.console import add_console
from truejoin.adapters.inbound.http.governance import Governance
from truejoin.adapters.inbound.http.olap import load_catalog, mount_olap
from truejoin.adapters.inbound.http.request_context import add_request_id_middleware
from truejoin.adapters.inbound.http.routes.audit import GapFinder, create_audit_router
from truejoin.adapters.inbound.http.routes.catalog import (
    _refresh_openapi_enrichment,
    create_catalog_router,
)
from truejoin.adapters.inbound.http.routes.crud import RouterFactory
from truejoin.adapters.inbound.http.routes.databases import create_databases_router
from truejoin.adapters.inbound.http.routes.export import create_export_router
from truejoin.adapters.inbound.http.routes.keys import create_keys_router
from truejoin.adapters.inbound.http.routes.query import create_query_router
from truejoin.adapters.inbound.http.routes.schema import create_schema_router
from truejoin.adapters.inbound.rest.routes import install_error_handlers
from truejoin.application.container import Container
from truejoin.application.gateway.schema_discovery import SchemaAnalyzer
from truejoin.application.olap.app import FusionApp
from truejoin.application.settings import (
    AuthConfig,
    DatabaseConfig,
    RuntimeEnv,
    Settings,
    validate_production_config,
)
from truejoin.domain.catalog.catalog import CatalogStatus
from truejoin.domain.gateway.errors import ConfigurationError, DatabaseConnectionError, WarpError
from truejoin.domain.gateway.managed import ManagedDatabaseError, scrub
from truejoin.domain.gateway.schema import DatabaseSchema
from truejoin.domain.gateway.sql_types import type_kind
from truejoin.domain.governance.masking import (
    NO_MASKING,
    CatalogMasking,
    MaskingPolicy,
    semantic_types_of,
    unsafe_rules,
    validate_strategy,
)
from truejoin.ports.audit import AuditSink, NullAuditSink
from truejoin.ports.database import DatabaseGateway

logger = logging.getLogger(__name__)

ContainerFactory = Callable[[], Container]

#: How often a running server looks for catalog changes it did not make itself
#: (`truejoin catalog` on the command line, another worker).
CATALOG_POLL_SECONDS = 30.0


@dataclass
class RuntimeContext:
    """What the lifespan built; stored on `app.state.runtime`."""

    env: RuntimeEnv
    container: Container | None = None
    settings: Settings | None = None
    auth_manager: AuthManager | None = None
    audit: AuditSink = field(default_factory=NullAuditSink)
    gateways: dict[str, DatabaseGateway] = field(default_factory=dict)
    readonly_gateways: dict[str, DatabaseGateway] = field(default_factory=dict)
    schemas: dict[str, DatabaseSchema] = field(default_factory=dict)
    fusion: FusionApp | None = None
    #: Each database's masking, one object shared by its table routes and the
    #: OLAP side, so labels changed in place reach both.
    maskings: dict[str, CatalogMasking] = field(default_factory=dict)
    #: The catalog index entries this process has acted on, to notice changes.
    seen_catalogs: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    is_ready: bool = False
    #: The routes each database's mount added, so a removed database takes them along.
    routes_of: dict[str, list[BaseRoute]] = field(default_factory=dict)
    #: The catalog, audit, keys and databases routes, kept ahead of every table's.
    admin_routes: list[BaseRoute] = field(default_factory=list)
    #: The database also served at the bare API prefix (the only one at startup), if any.
    alias_db: str | None = None
    #: Databases added from the console that are kept but not connected, and why.
    unconnected: dict[str, str] = field(default_factory=dict)


def runtime_of(app: FastAPI) -> RuntimeContext:
    """The app's runtime context."""
    return app.state.runtime  # type: ignore[no-any-return]


async def connect_with_retry(
    gateway: DatabaseGateway, max_retries: int = 5, retry_delay: float = 2.0
) -> None:
    """Connect to database with exponential backoff retry.

    Args:
        gateway: Database gateway to connect.
        max_retries: Maximum number of retry attempts.
        retry_delay: Initial delay between retries (doubles each attempt).
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Connection attempt {attempt}/{max_retries} to {gateway.name}")
            await gateway.connect()
            logger.info(f"Successfully connected to {gateway.name}")
            return
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                wait_time = retry_delay * (2 ** (attempt - 1))
                logger.warning(
                    f"Failed to connect to {gateway.name}: {e}. Retrying in {wait_time:.1f}s..."
                )
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"All connection attempts to {gateway.name} failed")

    raise DatabaseConnectionError(
        f"Failed to connect to {gateway.name} after {max_retries} attempts",
        details={"last_error": str(last_error)},
    )


def _mount_prefixes(api_prefix: str, db_name: str, multi_db: bool) -> list[str]:
    """Where one database's routers are mounted: the documented prefix first.

    Every database answers at ``{api_prefix}/{db_name}``, so a client can
    address it by name however many databases are configured; that is the
    documented (OpenAPI-visible) location. With a single database the bare
    ``{api_prefix}`` is kept as a hidden alias for compatibility.

    The scoped prefix must be registered **first**: routers carry
    ``/{table}`` paths, so an alias route would otherwise swallow
    ``{api_prefix}/{db_name}/…`` with ``table = db_name``.
    """
    scoped = f"{api_prefix}/{db_name}"
    return [scoped] if multi_db else [scoped, api_prefix]


def _include_at_prefixes(
    app: FastAPI,
    routers: list[APIRouter],
    prefixes: list[str],
    tags: list[str | Enum] | None = None,
) -> None:
    """Include ``routers`` under each prefix; only the first prefix is in the schema."""
    for position, prefix in enumerate(prefixes):
        for router in routers:
            app.include_router(router, prefix=prefix, tags=tags, include_in_schema=position == 0)


def olap_maskings(container: Container) -> dict[str, CatalogMasking]:
    """Each database's masking: its policy and the labels its approved catalog gives.

    A database without labels (no approved catalog, or one that failed to
    load) refuses a masked caller in ``AccessScope.masks_for`` rather than
    serving it raw.
    """
    return {
        db.name: _masking_for(container, container.settings.settings, db.name, None)
        for db in container.settings.databases
    }


def go_live(runtime: RuntimeContext, db_name: str) -> bool:
    """Put a database's approved catalog to work in this process, without a restart.

    Its masking labels (in place, so every route holding the masking sees
    them), its verified relationships and, with ``olap.terms`` on, its
    certified terms. A draft or a missing catalog changes nothing: what is
    live stays live until a catalog is approved again.
    """
    container, settings = runtime.container, runtime.settings
    if container is None or settings is None:
        return False
    try:
        catalog = container.repository.load(db_name)
    except Exception as e:
        logger.warning(f"Could not load the catalog of {db_name}: {e}")
        return False
    if catalog is None or catalog.status != CatalogStatus.approved:
        return False
    masking = runtime.maskings.get(db_name)
    if masking is not None and isinstance(masking.semantic_types, dict):
        labels, live = semantic_types_of(catalog), masking.semantic_types
        live.update(labels)  # table by table, so no table is ever briefly unlabelled
        for table in set(live) - set(labels):
            del live[table]
        if db_name in runtime.schemas:
            _warn_about_unsafe_masks(masking, runtime.schemas[db_name], db_name)
    fusion = runtime.fusion
    if fusion is not None and db_name in fusion.sources.source_names():
        load_catalog(fusion, db_name, catalog, terms=settings.olap.terms == "on")
    logger.info(f"The approved catalog of {db_name} is live")
    return True


def catalog_versions(container: Container) -> dict[str, tuple[Any, ...]]:
    """What identifies each stored catalog's current version, from the index."""
    return {
        name: (entry.status, entry.generated_at, entry.updated_at)
        for name, entry in container.repository.get_index().catalogs.items()
    }


def follow_changes(runtime: RuntimeContext) -> list[str]:
    """Put every catalog that changed since this process last looked to work.

    Returns the names that changed; approved ones went live.
    """
    if runtime.container is None:
        return []
    versions = catalog_versions(runtime.container)
    changed = [n for n, v in versions.items() if runtime.seen_catalogs.get(n) != v]
    changed += [n for n in runtime.seen_catalogs if n not in versions]  # deleted
    runtime.seen_catalogs = versions
    for name in changed:
        go_live(runtime, name)
    return changed


def follow_catalogs(app: FastAPI) -> bool:
    """:func:`follow_changes`, then the enriched OpenAPI spec rebuilt if any changed."""
    runtime = runtime_of(app)
    changed = follow_changes(runtime)
    if changed and runtime.container is not None and runtime.gateways:
        _refresh_openapi_enrichment(app, runtime.container, runtime.gateways, runtime.schemas)
    return bool(changed)


def watch_catalogs(runtime: RuntimeContext, stop: threading.Event) -> None:
    """:func:`follow_changes` every few seconds until ``stop``; for a thread."""
    while not stop.wait(CATALOG_POLL_SECONDS):
        try:
            follow_changes(runtime)
        except Exception as e:  # a bad read must not end the loop
            logger.warning(f"Could not check the catalogs for changes: {e}")


async def _poll_catalogs(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(CATALOG_POLL_SECONDS)
        try:
            await asyncio.to_thread(follow_catalogs, app)
        except Exception as e:  # a bad read must not end the loop
            logger.warning(f"Could not check the catalogs for changes: {e}")


def _masking_for(
    container: Container, settings: Any, db_name: str, schema: DatabaseSchema | None
) -> CatalogMasking:
    """Build this database's masking from config plus its approved catalog.

    Masks key on semantic types, so without an approved catalog there is
    nothing to key them on and nothing is masked — which is why a rule that
    cannot be applied is logged loudly rather than passed over in silence.
    """
    config = settings.masking
    if not config.enabled or not (config.rules or config.by_role):
        return NO_MASKING

    # Checked before the catalog is loaded, because this is wrong whether or
    # not this database has one. Refused in every environment rather than only
    # production: a `hash` rule with no key cannot run, and both alternatives —
    # skipping the rule, or falling back to an unkeyed digest — would return
    # readable PII while reporting that it was masked.
    if "hash" in config.strategies and not config.hash_secret:
        raise ValueError(
            "Masking is configured with the 'hash' strategy but masking.hash_secret "
            "is not set. An unkeyed digest of enumerable data (an email, a national "
            "id) is reversible, so there is no safe default. Set masking.hash_secret "
            "or choose redact/partial/last4/null."
        )

    for semantic, strategy in {**config.rules, **_flattened(config.by_role)}.items():
        validate_strategy(strategy, semantic)
    policy = MaskingPolicy(
        rules=dict(config.rules),
        by_role={role: dict(rules) for role, rules in config.by_role.items()},
        exempt_roles=tuple(config.exempt_roles),
        enabled=True,
        hash_key=config.hash_secret.encode("utf-8") if config.hash_secret else None,
    )

    # Its own labels object even when empty: `go_live` fills it in place
    # once a catalog is approved.
    try:
        catalog = container.repository.load(db_name)
    except Exception as e:
        logger.warning(f"Could not load the catalog for masking in {db_name}: {e}")
        catalog = None

    labels = semantic_types_of(catalog)
    if not labels:
        logger.warning(
            f"Masking is enabled but {db_name} has no approved catalog, so no "
            f"column is labelled and nothing will be masked until one is approved."
        )
    masking = CatalogMasking(policy=policy, semantic_types=labels)
    if schema is not None and labels:
        _warn_about_unsafe_masks(masking, schema, db_name)
    return masking


def _flattened(by_role: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    return {
        semantic: strategy for rules in by_role.values() for semantic, strategy in rules.items()
    }


def _warn_about_unsafe_masks(masking: CatalogMasking, schema: DatabaseSchema, db_name: str) -> None:
    """Report masks whose output does not fit the column's documented type."""
    for table_name, table in schema.tables.items():
        column_types = {
            column.name: (
                type_kind(column.type, column.udt_name, column.full_type),
                column.nullable,
            )
            for column in table.columns
        }
        problems = unsafe_rules(masking.masks_for(table_name), column_types)
        for problem in problems:
            logger.warning(f"Masking in {db_name}.{table_name}: {problem}")


def _admin_routes_first(app: FastAPI, start: int, schemas: dict[str, Any]) -> None:
    """Put the catalog, audit and keys routes ahead of every table's.

    With one database its tables are also mounted at the bare API prefix, so a
    table called ``audit`` or ``keys`` would otherwise answer
    ``{api_prefix}/audit`` with its rows, or take ``POST {api_prefix}/keys``
    as an insert. The table stays reachable at ``{api_prefix}/{db}/{table}``.
    """
    routes = app.router.routes
    admin = routes[start:]
    del routes[start:]
    routes[:0] = admin
    taken = {"catalog", "audit", "keys"}
    for db_name, schema in schemas.items():
        for table in sorted(taken & set(schema.get_table_names())):
            logger.warning(
                f"Table {db_name}.{table} shares its name with a truejoin route; "
                f"it is served at /{db_name}/{table} under the API prefix only"
            )


async def _mount_database(  # noqa: PLR0913
    app: FastAPI,
    runtime: RuntimeContext,
    container: Container,
    db_config: Any,
    auth_manager: AuthManager,
    multi_db: bool,
    retries: int = 5,
) -> None:
    """Connect one configured database and mount its CRUD + raw-query routers.

    The routes it adds are kept in ``runtime.routes_of``, even when it fails
    part way, so a database can be taken down again.
    """
    db_name = db_config.name
    start = len(app.router.routes)
    try:
        await _connect_and_mount(
            app, runtime, container, db_config, auth_manager, multi_db, retries
        )
    finally:
        runtime.routes_of[db_name] = app.router.routes[start:]


async def _connect_and_mount(  # noqa: PLR0913
    app: FastAPI,
    runtime: RuntimeContext,
    container: Container,
    db_config: Any,
    auth_manager: AuthManager,
    multi_db: bool,
    retries: int,
) -> None:
    settings = container.settings.settings
    db_name = db_config.name
    logger.info(f"Connecting to database: {db_name} ({db_config.type})")

    gateway = container.gateway_factory.create(db_config)
    await connect_with_retry(gateway, max_retries=retries)
    runtime.gateways[db_name] = gateway

    if not settings.auto_discover_tables:
        return

    analyzer = SchemaAnalyzer(gateway, excluded_tables=settings.excluded_tables)
    schema = await analyzer.analyze()
    runtime.schemas[db_name] = schema
    masking = runtime.maskings[db_name]
    _warn_about_unsafe_masks(masking, schema, db_name)
    governance = Governance(masking=masking, audit=runtime.audit)

    table_names = schema.get_table_names()
    logger.info(
        f"Discovered {len(schema.tables)} tables in {db_name}: "
        f"{', '.join(table_names[:10])}{'...' if len(table_names) > 10 else ''}"
    )

    prefixes = _mount_prefixes(settings.api_prefix, db_name, multi_db)
    # Schema and export routes first: GET /{table}/schema and /{table}/export
    # must win over CRUD's GET /{table}/{id}.
    _include_at_prefixes(
        app, [create_schema_router(db_name, schema, gateway, auth_manager)], prefixes
    )
    export_routers = [
        create_export_router(
            table,
            gateway,
            settings.export,
            auth_manager,
            db_name if multi_db else None,
            governance,
        )
        for table in schema.tables.values()
    ]
    _include_at_prefixes(app, export_routers, prefixes)
    router_factory = RouterFactory(
        db=gateway,
        schema_analyzer=analyzer,
        governance=governance,
        default_limit=settings.pagination.default_limit,
        max_limit=settings.pagination.max_limit,
        db_name=db_name if multi_db else None,
        auth_manager=auth_manager,
        readonly_columns=settings.readonly_columns,
    )
    _include_at_prefixes(app, router_factory.create_routers_for_all_tables(schema.tables), prefixes)

    # Raw query endpoint: use a separate read-only connection when configured,
    # so a whitelist bypass still cannot mutate data.
    query_gateway = gateway
    readonly_config = db_config.readonly_config()
    if readonly_config and settings.enable_raw_query:
        readonly_gateway = container.gateway_factory.create(readonly_config)
        await connect_with_retry(readonly_gateway, max_retries=retries)
        runtime.readonly_gateways[db_name] = readonly_gateway
        query_gateway = readonly_gateway
        logger.info(f"Raw query endpoint for {db_name} uses a read-only connection")

    query_router = create_query_router(
        db=query_gateway,
        whitelist=settings.raw_query_whitelist,
        enabled=settings.enable_raw_query,
        auth_manager=auth_manager,
        masking=governance.masking,
        audit=governance.audit,
        db_name=db_name,
        analyzer=container.sql_analyzer,
    )
    _include_at_prefixes(
        app,
        [query_router],
        prefixes,
        tags=[f"{db_name} - Raw Query"] if multi_db else ["Raw Query"],
    )


async def _take_down(
    app: FastAPI,
    runtime: RuntimeContext,
    container: Container,
    db_name: str,
    keep_config: bool = False,
) -> None:
    """Undo what mounting ``db_name`` did: its routes, connections and OLAP source.

    ``keep_config`` leaves it in ``settings.databases`` (and its masking), for
    an added database that stays listed while it is not connected.
    """
    gone = {id(route) for route in runtime.routes_of.pop(db_name, [])}
    app.router.routes[:] = [r for r in app.router.routes if id(r) not in gone]
    for gateways in (runtime.gateways, runtime.readonly_gateways):
        gateway = gateways.pop(db_name, None)
        if gateway is not None:
            try:
                await gateway.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting {db_name}: {e}")
    runtime.schemas.pop(db_name, None)
    fusion = runtime.fusion
    if fusion is not None and db_name in fusion.sources.source_names():
        await asyncio.to_thread(fusion.sources.disconnect, db_name)
    if not keep_config:
        runtime.maskings.pop(db_name, None)
        databases = container.settings.databases
        databases[:] = [db for db in databases if db.name != db_name]
    runtime.unconnected.pop(db_name, None)
    app.openapi_schema = None


class LiveDatabases:
    """Connects, changes and takes down the databases added from the console, at once.

    A change connects the new configuration before anything already serving
    is touched, and is kept only once it serves; the routes, the OLAP source,
    MCP and the catalog all see the same list. One change at a time.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._lock = asyncio.Lock()

    @property
    def _runtime(self) -> RuntimeContext:
        return runtime_of(self._app)

    @property
    def _container(self) -> Container:
        container = self._runtime.container
        if container is None:  # pragma: no cover - only before startup
            raise RuntimeError("the server has not started")
        return container

    @property
    def service(self) -> Any:
        """The ``DatabaseService``, or None when there is no store."""
        return self._container.databases

    def configured(self) -> list[DatabaseConfig]:
        """The databases from the configuration file, in its order."""
        service = self.service
        return [
            db
            for db in self._container.settings.databases
            if service is None or service.is_configured(db.name)
        ]

    def state(self, name: str) -> tuple[str, str]:
        """``("connected", "")``, or ``("not connected", why)``."""
        runtime = self._runtime
        if name in runtime.gateways:
            return "connected", ""
        return "not connected", runtime.unconnected.get(name, "")

    async def test(self, config: DatabaseConfig) -> None:
        """Connect and list its tables, then let go; nothing is kept.

        Raises:
            ManagedDatabaseError: Why it could not connect, without its passwords.
        """
        configs = [config.model_dump()]
        readonly = config.readonly_config()
        if readonly:
            configs.append(readonly)
        for cfg in configs:
            gateway = self._container.gateway_factory.create(cfg)
            try:
                await gateway.connect()
                await gateway.get_tables()
            except Exception as e:
                raise ManagedDatabaseError(
                    f"could not connect: {scrub(str(e), [config.password, config.readonly_password])}"
                ) from e
            finally:
                try:
                    await gateway.disconnect()
                except Exception:  # noqa: S110 - a test connection that never opened
                    pass

    async def attach(self, config: DatabaseConfig) -> None:
        """Connect and serve a database everywhere; on failure nothing is left behind."""
        async with self._lock:
            await self._attach(config)

    async def detach(self, name: str) -> None:
        async with self._lock:
            await _take_down(self._app, self._runtime, self._container, name)
            self._refresh()

    async def swap(self, old: DatabaseConfig, new: DatabaseConfig) -> None:
        """Serve ``new`` in place of ``old``; if ``new`` will not serve, ``old`` is put back."""
        async with self._lock:
            await _take_down(self._app, self._runtime, self._container, old.name)
            try:
                await self._attach(new)
            except Exception:
                try:
                    await self._attach(old)
                except Exception as e:
                    logger.error(f"Could not put {old.name} back after a failed change: {e}")
                    self._container.settings.databases.append(old)
                    self._runtime.unconnected[old.name] = "a change failed and it did not reconnect"
                raise

    def check_name(self, name: str) -> None:
        """Refuse a name the bare-prefix database already serves a table under."""
        runtime = self._runtime
        alias = runtime.alias_db
        if alias and alias in runtime.schemas:
            if name in runtime.schemas[alias].get_table_names():
                raise ManagedDatabaseError(
                    f"'{name}' is a table of {alias}, which is also served at the bare API "
                    "prefix: the two would share a URL"
                )

    async def _attach(self, config: DatabaseConfig) -> None:
        app, runtime, container = self._app, self._runtime, self._container
        name = config.name
        container.settings.databases[:] = [
            db for db in container.settings.databases if db.name != name
        ] + [config]
        runtime.maskings[name] = _masking_for(container, container.settings.settings, name, None)
        try:
            await _mount_database(
                app, runtime, container, config, _auth_of(runtime), multi_db=True, retries=1
            )
            fusion = runtime.fusion
            if fusion is not None and fusion.attach_database is not None:
                await asyncio.to_thread(fusion.attach_database, config)
        except Exception:
            await _take_down(app, runtime, container, name)
            raise
        runtime.unconnected.pop(name, None)
        await asyncio.to_thread(go_live, runtime, name)
        self._refresh()

    def _refresh(self) -> None:
        """The admin routes ahead of every table's again, and the spec rebuilt."""
        app, runtime = self._app, self._runtime
        admin = {id(r) for r in runtime.admin_routes}
        routes = app.router.routes
        routes[:] = runtime.admin_routes + [r for r in routes if id(r) not in admin]
        app.openapi_schema = None
        container = runtime.container
        if container is not None and runtime.gateways:
            try:
                _refresh_openapi_enrichment(app, container, runtime.gateways, runtime.schemas)
            except Exception as e:
                logger.warning(f"Could not refresh the OpenAPI spec: {e}")


def _auth_of(runtime: RuntimeContext) -> AuthManager:
    if runtime.auth_manager is None:  # pragma: no cover - only before startup
        raise RuntimeError("the server has not started")
    return runtime.auth_manager


def _make_lifespan(  # noqa: C901
    container_factory: ContainerFactory | None,
) -> Lifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: C901, PLR0912, PLR0915
        runtime = runtime_of(app)
        env = runtime.env
        logger.info(f"Starting Warp Engine v{__version__} (env={env.app_env})...")

        if container_factory is None:
            raise ConfigurationError(
                "create_app() was called without a container_factory; "
                "use truejoin.main:app or pass one explicitly"
            )
        container = container_factory()
        runtime.container = container
        runtime.settings = container.settings
        logger.info(f"Loaded configuration with {len(container.settings.databases)} database(s)")

        # Fail-safe: refuse to start in production with unsafe configuration.
        violations = validate_production_config(
            container.settings, env.app_env, list(env.cors_origins)
        )
        if violations:
            for violation in violations:
                logger.error(f"Unsafe production configuration: {violation}")
            raise ConfigurationError(
                "Refusing to start in production with unsafe configuration",
                details={"violations": violations},
            )

        auth_manager = AuthManager(container.settings.settings.auth, keys=container.access_keys)
        runtime.auth_manager = auth_manager
        audit_config = container.settings.settings.audit
        runtime.audit = container.audit
        if audit_config.enabled:
            logger.info(
                "Audit trail enabled"
                + (f", appending to {audit_config.file}" if audit_config.file else "")
            )
        if auth_manager.enabled:
            handed_out = (
                f", and keys handed out kept in {container.settings.settings.auth.key_store}"
                if container.access_keys is not None
                else ""
            )
            logger.info(
                f"Authentication enabled with {auth_manager.api_key_count} API key(s){handed_out}"
            )
        else:
            logger.info("Authentication disabled - all endpoints are public")

        runtime.maskings = olap_maskings(container)
        runtime.seen_catalogs = catalog_versions(container)
        multi_db = len(container.settings.databases) > 1
        if len(container.settings.databases) == 1:
            runtime.alias_db = container.settings.databases[0].name
        added = container.databases
        for db_config in list(container.settings.databases):
            try:
                await _mount_database(app, runtime, container, db_config, auth_manager, multi_db)
            except Exception as e:
                if added is None or added.is_configured(db_config.name):
                    if not isinstance(e, DatabaseConnectionError):
                        logger.error(f"Failed to setup database {db_config.name}: {e}")
                    raise
                # Added from the console: kept, shown as not connected, fixed from there.
                await _take_down(app, runtime, container, db_config.name, keep_config=True)
                runtime.unconnected[db_config.name] = scrub(
                    str(e.details.get("last_error", e)) if isinstance(e, WarpError) else str(e),
                    [db_config.password, db_config.readonly_password],
                )
                logger.error(f"The added database {db_config.name} is not connected: {e}")

        # Everything the OLAP side opens (MCP sessions, DuckDB, the loop thread
        # and its gateways) is closed by this stack, before the gateways below.
        async with AsyncExitStack() as olap:
            gaps: GapFinder | None = None
            if container.olap_factory is not None:
                # In a thread: connecting waits on the gateway loop, not on this one.
                fusion = await asyncio.to_thread(container.olap_factory, env.app_env)
                olap.push_async_callback(asyncio.to_thread, fusion.close)
                runtime.fusion = fusion
                for name in runtime.unconnected:  # served by neither side, not by one
                    if name in fusion.sources.source_names():
                        await asyncio.to_thread(fusion.sources.disconnect, name)
                scope_of = await mount_olap(
                    app,
                    fusion,
                    container.settings,
                    auth_manager,
                    olap,
                    env.api_host,
                    runtime.maskings,
                    container.repository,
                )
                tools = fusion.tools

                def find_gaps(
                    request: Request, asked: Mapping[str, Sequence[int]]
                ) -> dict[str, Any]:
                    return tools.for_scope(scope_of(request)).vocabulary_gaps(asked)

                gaps = find_gaps

            # Taken before the admin routers are added, to move them first below.
            before_admin = len(app.router.routes)
            try:
                app.include_router(
                    create_catalog_router(
                        container=container,
                        gateways=runtime.gateways,
                        app=app,
                        auth_manager=auth_manager,
                        on_change=lambda name: go_live(runtime, name),
                    ),
                    prefix=container.settings.settings.api_prefix,
                )
                logger.info("Catalog API router registered")
            except Exception as e:
                logger.warning(f"Failed to initialize catalog router: {e}")
            app.include_router(
                create_audit_router(container.audit_reader, auth_manager, gaps),
                prefix=container.settings.settings.api_prefix,
            )
            app.include_router(
                create_keys_router(container.access_keys, auth_manager, container.audit),
                prefix=container.settings.settings.api_prefix,
            )
            app.include_router(
                create_databases_router(
                    LiveDatabases(app), auth_manager, container.audit, env.app_env
                ),
                prefix=container.settings.settings.api_prefix,
            )
            runtime.admin_routes = app.router.routes[before_admin:]
            _admin_routes_first(app, before_admin, runtime.schemas)

            if runtime.gateways:
                try:
                    _refresh_openapi_enrichment(app, container, runtime.gateways, runtime.schemas)
                except Exception as e:
                    logger.warning(f"Failed to setup OpenAPI auto-enrichment: {e}")

            poller = asyncio.create_task(_poll_catalogs(app))
            olap.callback(poller.cancel)
            runtime.is_ready = True
            logger.info("Warp Engine started successfully!")
            logger.info(f"API documentation available at: {container.settings.settings.docs_url}")

            yield

            logger.info("Shutting down Warp Engine...")
            runtime.is_ready = False

        for label, gateways in (("", runtime.gateways), ("read-only ", runtime.readonly_gateways)):
            for db_name, gateway in gateways.items():
                try:
                    await gateway.disconnect()
                    logger.info(f"Disconnected {label}connection for {db_name}")
                except Exception as e:
                    logger.error(f"Error disconnecting {label}{db_name}: {e}")

    return lifespan


def create_app(  # noqa: C901, PLR0915
    container_factory: ContainerFactory | None = None,
    env: RuntimeEnv | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        container_factory: Builds the `Container` when the app starts (lifespan).
            Without it the app can be created (routes, docs, probes) but not
            started.
        env: Process environment; defaults to `RuntimeEnv.from_environ()`.

    Returns:
        Configured FastAPI application instance.
    """
    env = env or RuntimeEnv.from_environ()
    docs_url = "/docs" if not env.is_production else None

    app = FastAPI(
        title="Warp Engine",
        description="""
# Warp - Auto-discovery REST CRUD API Generator

Automatically generates REST API endpoints from database schema.

## Features

- **Auto-discovery**: Automatically discovers database tables and generates endpoints
- **CRUD Operations**: Full Create, Read, Update, Delete support
- **Filtering**: Filter results using query parameters
- **Sorting**: Sort results by any column
- **Pagination**: Built-in pagination support
- **Raw Queries**: Execute raw SQL queries (when enabled)
- **Multi-DB Support**: PostgreSQL and MySQL support

## Filtering Examples

```
GET /api/v1/users?filter[status]=active
GET /api/v1/products?filter[price][gte]=100&filter[price][lte]=500
GET /api/v1/orders?filter[status][in]=pending,processing
```

## Sorting Examples

```
GET /api/v1/users?sort=name:asc
GET /api/v1/products?sort=price:desc,name:asc
GET /api/v1/orders?sort=-created_at
```

## Pagination

```
GET /api/v1/users?limit=20&offset=40
```
        """,
        version=__version__,
        # Disable default docs — we serve custom /docs with cache-busting
        docs_url=None,
        redoc_url=None,
        # The spec route is registered explicitly below so it can be guarded
        # by the auth manager (it carries catalog descriptions/x-llm-context).
        openapi_url=None,
        lifespan=_make_lifespan(container_factory),
    )
    app.state.runtime = RuntimeContext(env=env)

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_json(request: Request) -> JSONResponse:
        """Serve the (possibly catalog-enriched) OpenAPI spec.

        Public only when "/openapi.json" is listed in `auth.public_paths`
        (the development default); otherwise a key with `read` permission is
        required, like any other endpoint.
        """
        manager = runtime_of(app).auth_manager
        if manager and manager.enabled:
            user = await manager.get_current_user(request)
            if user is not None and not user.has_permission(Permission.READ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Permission denied: 'read' required"},
                )
        return JSONResponse(app.openapi())

    # Custom /docs and /redoc with cache-busting for OpenAPI JSON
    if docs_url:
        from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

        @app.get("/docs", include_in_schema=False)
        async def custom_swagger_ui() -> HTMLResponse:
            return get_swagger_ui_html(
                openapi_url=f"/openapi.json?v={int(time.time())}",
                title=f"{app.title} - Swagger UI",
            )

        @app.get("/redoc", include_in_schema=False)
        async def custom_redoc() -> HTMLResponse:
            return get_redoc_html(
                openapi_url=f"/openapi.json?v={int(time.time())}",
                title=f"{app.title} - ReDoc",
            )

    def console_values() -> dict[str, str]:
        runtime = runtime_of(app)
        manager = runtime.auth_manager
        return {
            "KEY_HEADER": manager.header_name if manager else AuthConfig().header_name,
            "API_PREFIX": runtime.settings.settings.api_prefix if runtime.settings else "/api/v1",
            "MCP_PATH": runtime.settings.mcp.http_path if runtime.settings else "/mcp",
        }

    add_console(app, console_values, lambda: runtime_of(app).auth_manager)

    add_request_id_middleware(app)
    install_error_handlers(app)

    # CORS middleware.
    # Browsers reject a "*" allowlist combined with credentials, so credentials
    # are only enabled when an explicit origin allowlist is configured.
    cors_origins = list(env.cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials="*" not in cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(WarpError)
    async def warp_error_handler(request: Request, exc: WarpError) -> JSONResponse:
        logger.error(f"Application error: {exc.message}")
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"Unhandled error: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "InternalServerError",
                "message": "An unexpected error occurred",
                "details": {} if env.is_production else {"error": str(exc)},
            },
        )

    # Health check endpoint (Kubernetes/Docker compatible)
    # response_model=None: the handler returns dict | JSONResponse (a union that
    # FastAPI cannot build a response model from).
    @app.get("/health", tags=["Health"], response_model=None)
    async def health_check() -> dict[str, Any] | JSONResponse:
        """Health check endpoint for container orchestration.

        Returns:
            - 200: Service is healthy
            - 503: Service is unhealthy (database disconnected)
        """
        runtime = runtime_of(app)
        db_status = {}
        all_healthy = True

        for name, gateway in runtime.gateways.items():
            is_connected = gateway.is_connected
            db_status[name] = "connected" if is_connected else "disconnected"
            if not is_connected:
                all_healthy = False

        status = "healthy" if all_healthy and runtime.is_ready else "unhealthy"
        response = {
            "status": status,
            "ready": runtime.is_ready,
            "databases": db_status,
            "environment": env.app_env,
        }
        if not all_healthy:
            return JSONResponse(status_code=503, content=response)
        return response

    # Readiness probe (Kubernetes)
    @app.get("/ready", tags=["Health"], response_model=None)
    async def readiness_check() -> dict[str, Any] | JSONResponse:
        """Readiness probe for Kubernetes.

        Returns 200 only when the application is fully ready to serve traffic.
        """
        if not runtime_of(app).is_ready:
            return JSONResponse(
                status_code=503, content={"ready": False, "message": "Application not ready"}
            )
        return {"ready": True}

    # RFC 9728: which authorization server issues the tokens this server takes.
    # An MCP client reads it after a 401 to send its user to the provider.
    # Public, as discovery has to be; 404 when only API keys are configured.
    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    @app.get("/.well-known/oauth-protected-resource/{resource:path}", include_in_schema=False)
    async def protected_resource(request: Request) -> JSONResponse:
        manager = runtime_of(app).auth_manager
        if manager is None or manager.oidc is None:
            return JSONResponse(status_code=404, content={"detail": "No token issuer configured"})
        return JSONResponse(manager.oidc.resource_metadata(str(request.base_url).rstrip("/")))

    # Liveness probe (Kubernetes)
    @app.get("/live", tags=["Health"])
    async def liveness_check() -> dict[str, Any]:
        """Liveness probe for Kubernetes.

        Returns 200 if the application process is alive.
        """
        return {"alive": True}

    # Info endpoint
    @app.get("/info", tags=["Info"])
    async def api_info() -> dict[str, Any]:
        """Get API information and discovered tables."""
        runtime = runtime_of(app)
        tables_info = {
            db_name: {"tables": schema.get_table_names(), "table_count": len(schema.tables)}
            for db_name, schema in runtime.schemas.items()
        }

        catalog_info: dict[str, Any] = {"available_catalogs": [], "catalog_count": 0}
        settings = runtime.settings
        if runtime.container is not None and settings is not None:
            try:
                names = runtime.container.repository.list_catalogs()
                catalog_info = {
                    "available_catalogs": names,
                    "catalog_count": len(names),
                    "storage_path": settings.settings.catalog.storage_path,
                }
            except Exception:
                pass

        cfg = settings.settings if settings else None
        return {
            "name": "Warp Engine",
            "version": __version__,
            "environment": env.app_env,
            "databases": tables_info,
            "catalog": catalog_info,
            "settings": {
                "api_prefix": cfg.api_prefix if cfg else "/api/v1",
                "pagination": {
                    "default_limit": cfg.pagination.default_limit if cfg else 50,
                    "max_limit": cfg.pagination.max_limit if cfg else 1000,
                },
                "raw_query_enabled": cfg.enable_raw_query if cfg else False,
            },
            "capabilities": capabilities_of(cfg, arrow_available()),
        }

    return app
