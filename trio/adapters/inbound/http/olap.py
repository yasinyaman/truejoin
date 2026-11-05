"""The OLAP side of the one app: ``/olap`` and the MCP endpoint behind the gateway's auth.

Both surfaces return rows as the database has them: SQL is not rewritten per
caller yet. Until it is, the guard here admits only callers for whom that is
already the right answer.
"""

import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from trio.adapters.inbound.http.auth import AuthManager, Permission
from trio.adapters.inbound.mcp.server import create_mcp_server
from trio.adapters.inbound.rest.routes import olap_router
from trio.application.olap.app import FusionApp
from trio.application.settings import MaskingConfig, Settings
from trio.domain.governance.masking import MaskingPolicy

logger = logging.getLogger(__name__)

OLAP_PREFIX = "/olap"

Guard = Callable[[Request], Awaitable[None]]


def olap_guard(auth_manager: AuthManager, masking: MaskingConfig) -> Guard:
    """Who may use ``/olap`` and MCP while their SQL is not rewritten per caller.

    The ``query`` permission, which a key with row rules never has, and nobody
    whose columns would be masked elsewhere: an unidentified caller counts as
    masked, as it does on the table routes.
    """
    require = auth_manager.require(Permission.QUERY)
    policy = MaskingPolicy(
        rules=dict(masking.rules),
        by_role={role: dict(rules) for role, rules in masking.by_role.items()},
        exempt_roles=tuple(masking.exempt_roles),
        enabled=masking.enabled,
    )

    async def guard(request: Request) -> None:
        user = await require(request, request.headers.get(auth_manager.header_name))
        if policy.for_roles(user.roles if user is not None else []):
            raise HTTPException(
                status_code=403,
                detail="Column masking applies to this caller, and analytical queries "
                "do not mask columns yet; use the table endpoints",
            )

    return guard


class GuardedApp:
    """Runs ``guard`` before handing an HTTP request to a mounted ASGI app."""

    def __init__(self, app: ASGIApp, guard: Guard) -> None:
        self._app = app
        self._guard = guard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            try:
                await self._guard(Request(scope, receive))
            except HTTPException as e:
                response = JSONResponse(
                    {"detail": e.detail}, status_code=e.status_code, headers=e.headers
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


async def mount_olap(
    app: FastAPI,
    fusion: FusionApp,
    settings: Settings,
    auth_manager: AuthManager,
    stack: AsyncExitStack,
    host: str,
) -> None:
    """Add the OLAP routes and the MCP endpoint to ``app``; ``stack`` ends the MCP sessions."""
    guard = olap_guard(auth_manager, settings.settings.masking)
    app.include_router(
        olap_router(fusion, settings.olap.rate_limit, auth_manager.header_name),
        prefix=OLAP_PREFIX,
        tags=["OLAP"],
        dependencies=[Depends(guard)],
    )

    server = create_mcp_server(fusion, name="trio", allowed=settings.mcp.allowed_tools())
    path = settings.mcp.http_path
    # Stateless JSON: every tool call is one request and one response. `host`
    # decides DNS-rebinding protection: on for a loopback bind, off otherwise.
    mcp_app = server.streamable_http_app(
        streamable_http_path=path, json_response=True, stateless_http=True, host=host
    )
    # A route at the exact path, not a mount: a mount answers /mcp with a redirect.
    app.router.routes.append(Route(path, endpoint=GuardedApp(mcp_app, guard)))
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
