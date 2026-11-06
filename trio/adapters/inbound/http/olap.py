"""The OLAP side of the one app: ``/olap`` and the MCP endpoint behind the gateway's auth.

Both surfaces run SQL, so neither can apply a caller's row rules and masks one
table at a time the way the table routes do. Instead each request is turned
into an ``AccessScope`` here, and the engine rewrites the query under it.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from trio.adapters.inbound.http.auth import AuthenticatedUser, AuthManager, Permission, caller_of
from trio.adapters.inbound.http.request_context import request_id_of
from trio.adapters.inbound.mcp.server import CALLER, create_mcp_server, request_scope
from trio.adapters.inbound.rest.routes import olap_router
from trio.application.olap.app import FusionApp
from trio.application.settings import MaskingConfig, Settings
from trio.domain.catalog.catalog import relationship_edges
from trio.domain.governance.access import AccessScope
from trio.domain.governance.masking import CatalogMasking, MaskingPolicy
from trio.domain.governance.row_policy import EMPTY_POLICY
from trio.ports.catalog_repository import CatalogRepository

logger = logging.getLogger(__name__)

OLAP_PREFIX = "/olap"

Guard = Callable[[Request], Awaitable[None]]


def masked_types(masking: MaskingConfig, roles: Sequence[str]) -> dict[str, str]:
    """The semantic types masked for a caller with these roles, with their strategies."""
    policy = MaskingPolicy(
        rules=dict(masking.rules),
        by_role={role: dict(rules) for role, rules in masking.by_role.items()},
        exempt_roles=tuple(masking.exempt_roles),
        enabled=masking.enabled,
    )
    return policy.for_roles(roles)


def masked_caller(masking: MaskingConfig, roles: Sequence[str]) -> bool:
    """Whether a caller with these roles has columns masked on the table routes."""
    return bool(masked_types(masking, roles))


def scope_for(
    user: AuthenticatedUser | None,
    masking: MaskingConfig,
    maskings: Mapping[str, CatalogMasking],
    request_id: str = "",
) -> AccessScope:
    """What ``user`` may see in analytical queries.

    Nobody identified (auth off, or a public path) has no row rules and no
    roles, so the default masks apply to them, as they do on the table routes.
    """
    roles = tuple(user.roles) if user is not None else ()
    return AccessScope(
        actor=user.name if user is not None else None,
        tenant=user.tenant if user is not None else None,
        roles=roles,
        policy=user.row_policy if user is not None else EMPTY_POLICY,
        masking=maskings,
        masked=masked_caller(masking, roles),
        request_id=request_id,
    )


def olap_guard(auth_manager: AuthManager) -> Guard:
    """Who may use ``/olap`` and MCP: a key that lists the ``query`` permission.

    Row rules do not disqualify a key here as they do for raw SQL on the
    gateway, because these queries are rewritten under them.
    """

    async def guard(request: Request) -> None:
        try:
            user = await auth_manager.get_current_user(request)
            if user is not None and not user.grants(Permission.QUERY):
                raise HTTPException(status_code=403, detail="Permission denied: 'query' required")
        except HTTPException as e:
            # The access log is off in production; a refused caller is still worth a line.
            logger.warning(
                "Refused %s %s: %s %s (client %s, request %s)",
                request.method,
                request.url.path,
                e.status_code,
                e.detail,
                request.client.host if request.client else "-",
                request_id_of(request),
            )
            raise
        request.state.user = user

    return guard


class GuardedApp:
    """Runs ``guard``, then the mounted ASGI app as the caller ``scope_of`` names."""

    def __init__(
        self, app: ASGIApp, guard: Guard, scope_of: Callable[[Request], AccessScope]
    ) -> None:
        self._app = app
        self._guard = guard
        self._scope_of = scope_of

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            await self._guard(request)
        except HTTPException as e:
            response = JSONResponse(
                {"detail": e.detail}, status_code=e.status_code, headers=e.headers
            )
            await response(scope, receive, send)
            return
        token = CALLER.set(self._scope_of(request))
        try:
            await self._app(scope, receive, send)
        finally:
            CALLER.reset(token)


def catalogs_approved(fusion: FusionApp, repository: CatalogRepository) -> dict[str, Any]:
    """A readiness check: masks and descriptions come from approved catalogs only."""
    pending = []
    for name in fusion.sources.source_names():
        try:
            status = getattr(repository.load(name), "status", None)
        except Exception:
            status = None
        if status != "approved":
            pending.append(name)
    return {
        "check": "catalogs_approved",
        "ok": not pending,
        "detail": f"no approved catalog for: {', '.join(pending)}"
        if pending
        else "every source has an approved catalog",
    }


def load_catalogs(fusion: FusionApp, repository: CatalogRepository, terms: bool = False) -> None:
    """Give the engine what each source's reviewed catalog knows.

    Always the approved, verified relationships, for the join graph. With
    ``terms`` (``olap.terms``), also the certified business terms, which name
    the measures and are the vocabulary questions are matched against. Read
    once, like the masking labels: a catalog approved later counts from the
    next start.
    """
    for name in fusion.sources.source_names():
        try:
            catalog = repository.load(name)
        except Exception as e:
            logger.warning("Could not load the catalog of %s: %s", name, e)
            continue
        fusion.catalog.set_relationships(name, relationship_edges(name, catalog))
        if terms:
            fusion.semantic.use_catalog(name, catalog)


async def mount_olap(
    app: FastAPI,
    fusion: FusionApp,
    settings: Settings,
    auth_manager: AuthManager,
    stack: AsyncExitStack,
    host: str,
    maskings: Mapping[str, CatalogMasking],
    repository: CatalogRepository,
) -> None:
    """Add the OLAP routes and the MCP endpoint to ``app``; ``stack`` ends the MCP sessions.

    ``maskings`` are each database's masking labels, as its table routes use them.
    """
    guard = olap_guard(auth_manager)
    fusion.tools.readiness_checks.append(lambda: catalogs_approved(fusion, repository))
    load_catalogs(fusion, repository, terms=settings.olap.terms == "on")

    def scope_of(request: Request) -> AccessScope:
        return scope_for(
            caller_of(request), settings.settings.masking, maskings, request_id_of(request)
        )

    app.include_router(
        olap_router(fusion, settings.olap.rate_limit, auth_manager.header_name, scope_of),
        prefix=OLAP_PREFIX,
        tags=["OLAP"],
        dependencies=[Depends(guard)],
    )

    server = create_mcp_server(
        fusion, name="trio", allowed=settings.offered_tools(), scope=request_scope
    )
    path = settings.mcp.http_path
    # Stateless JSON: every tool call is one request and one response. `host`
    # decides DNS-rebinding protection: on for a loopback bind, off otherwise.
    mcp_app = server.streamable_http_app(
        streamable_http_path=path, json_response=True, stateless_http=True, host=host
    )
    # A route at the exact path, not a mount: a mount answers /mcp with a redirect.
    app.router.routes.append(Route(path, endpoint=GuardedApp(mcp_app, guard, scope_of)))
    await stack.enter_async_context(server.session_manager.run())

    if fusion.settings.backup_enabled:
        if not fusion.settings.external_access:
            logger.warning(
                "olap.backup_enabled is on but olap.external_access is off: in-memory "
                "EXPORT DATABASE backups will fail"
            )
        fusion.backup.start()
    logger.info(
        "OLAP mounted at %s (sources: %s), MCP at %s",
        OLAP_PREFIX,
        ", ".join(fusion.sources.source_names()) or "none",
        path,
    )
