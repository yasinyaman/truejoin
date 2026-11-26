"""The OLAP REST surface: the 16 tools plus readiness, backup and debug endpoints.

``olap_router`` builds an ``APIRouter`` around an assembled ``FusionApp``; the
host application decides where it is mounted and who may call it. Handlers are
plain ``def`` on purpose: they run in the threadpool, so the synchronous engine
never blocks the server's event loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from truejoin import __version__
from truejoin.adapters.inbound.rest.rate_limit import rate_limit_key
from truejoin.adapters.inbound.rest.schemas import (
    AggregateRequest,
    CreateViewRequest,
    LoadTableRequest,
    SearchRequest,
    SQLRequest,
)
from truejoin.application.olap.app import FusionApp
from truejoin.application.olap.tool_schemas import TOOL_DEFINITIONS
from truejoin.application.olap.tools import ToolService
from truejoin.domain.governance.access import UNRESTRICTED, AccessScope
from truejoin.domain.olap.errors import BackupError, GuardrailViolation, QueryError, SchemaError

logger = logging.getLogger(__name__)

_FORBIDDEN_MARKERS = ("Guardrail", "Only SELECT", "Blocked", "Multi-statement")

_ERROR_STATUS: dict[type[Exception], int] = {
    GuardrailViolation: 403,
    QueryError: 400,
    SchemaError: 400,
    BackupError: 409,
    RateLimitExceeded: 429,
}


def install_error_handlers(app: FastAPI) -> None:
    """Map the engine's errors (and a spent rate limit) to HTTP on the host app."""

    async def handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, RateLimitExceeded):
            return JSONResponse(
                status_code=429, content={"error": f"Rate limit exceeded: {exc.detail}"}
            )
        return JSONResponse(status_code=_ERROR_STATUS[type(exc)], content={"error": str(exc)})

    for error in _ERROR_STATUS:
        app.add_exception_handler(error, handler)


def handle_result(result: dict[str, Any]) -> dict[str, Any]:
    """Turn a tool ``{"error": ...}`` dict into the matching HTTP error."""
    if "error" in result:
        message = str(result["error"])
        if any(marker in message for marker in _FORBIDDEN_MARKERS):
            raise HTTPException(status_code=403, detail=message)
        if "Unknown tool" in message:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)
    return result


def olap_router(  # noqa: C901, PLR0915 - one flat list of routes
    fusion: FusionApp,
    rate_limit: str = "100/minute",
    key_header: str = "X-API-Key",
    scope_of: Callable[[Request], AccessScope] = lambda request: UNRESTRICTED,
    is_admin: Callable[[Request], bool] = lambda request: True,
) -> APIRouter:
    """Routes over ``fusion``; every one is rate limited per API key (else per client).

    ``scope_of`` says who a request is; every tool then runs under that
    caller's row rules and masks. ``is_admin`` says whether the caller may do
    what an administrator does here (read a table again from its source).
    """
    router = APIRouter()
    limiter = Limiter(key_func=partial(rate_limit_key, header=key_header))
    limited = limiter.limit(rate_limit)
    settings = fusion.settings

    def tools_for(request: Request) -> ToolService:
        return fusion.tools.for_scope(scope_of(request))

    def unrestricted(request: Request) -> None:
        """Backups hold every row unmasked; only a caller who may see that runs them."""
        if scope_of(request).is_restricted:
            raise HTTPException(
                status_code=403,
                detail="Backups are not available to a caller with row rules or masked columns",
            )

    # -- health -------------------------------------------------------------

    @router.get("/health")
    @limited
    def health(request: Request) -> dict[str, Any]:
        """Liveness of the OLAP side: 200 whenever it serves requests."""
        return {"status": "healthy", "version": __version__, "environment": settings.env}

    @router.get("/readiness")
    @limited
    def readiness(request: Request) -> Any:
        """Readiness: at least one data source must be connected."""
        try:
            sources = tools_for(request).list_sources()
        except Exception as e:  # pragma: no cover - defensive
            logger.error("Readiness check failed: %s", e)
            return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(e)})
        if not sources.get("sources"):
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "No data sources connected"},
            )
        return {"status": "ready", "version": __version__, "sources": len(sources["sources"])}

    # -- tools --------------------------------------------------------------

    @router.get("/tools")
    @limited
    def list_tools(request: Request) -> dict[str, Any]:
        return {"tools": TOOL_DEFINITIONS, "count": len(TOOL_DEFINITIONS)}

    @router.post("/tools/{tool_name}")
    @limited
    def execute_tool(
        request: Request, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Generic dispatch: the body is the tool's arguments dict."""
        return handle_result(tools_for(request).execute(tool_name, arguments or {}))

    @router.get("/sources")
    @limited
    def get_sources(request: Request) -> dict[str, Any]:
        return tools_for(request).list_sources()

    @router.get("/tables/{table:path}/schema")
    @limited
    def get_table_schema(request: Request, table: str) -> dict[str, Any]:
        return handle_result(tools_for(request).describe_table(table))

    @router.post("/query")
    @limited
    def run_query(request: Request, body: SQLRequest) -> dict[str, Any]:
        return handle_result(tools_for(request).query_data(body.sql))

    @router.post("/search")
    @limited
    def search(request: Request, body: SearchRequest) -> dict[str, Any]:
        return handle_result(
            tools_for(request).search_data(
                body.table, body.filter_column, body.filter_value, body.limit
            )
        )

    @router.post("/aggregate")
    @limited
    def aggregate(request: Request, body: AggregateRequest) -> dict[str, Any]:
        return handle_result(
            tools_for(request).aggregate_data(
                body.table, body.group_by, body.agg_column, body.agg_func
            )
        )

    @router.get("/views")
    @limited
    def get_views(request: Request) -> dict[str, Any]:
        return tools_for(request).list_views()

    @router.post("/views")
    @limited
    def create_view(request: Request, body: CreateViewRequest) -> dict[str, Any]:
        return handle_result(tools_for(request).create_view(body.name, body.sql, body.refresh))

    @router.post("/views/{name}/refresh")
    @limited
    def refresh_view(request: Request, name: str) -> dict[str, Any]:
        return handle_result(tools_for(request).refresh_view(name))

    @router.post("/tables/{table:path}/load")
    @limited
    def load_table(
        request: Request, table: str, body: LoadTableRequest | None = None
    ) -> dict[str, Any]:
        """Load a whole table, or the slice described by the optional body."""
        spec = body or LoadTableRequest()
        return handle_result(
            tools_for(request).load_table(table, where=spec.where, columns=spec.columns)
        )

    @router.post("/tables/{table:path}/refresh")
    @limiter.limit("10/minute")
    def refresh_table(request: Request, table: str) -> dict[str, Any]:
        """Read a table again from its source now: topped up, or fetched again whole.

        An administrator's, with no row rules or masks: what is read is every row.
        """
        if not is_admin(request):
            raise HTTPException(status_code=403, detail="Permission denied: 'admin' required")
        return handle_result(tools_for(request).refresh_table(table))

    @router.get("/cache/stats")
    @limited
    def cache_stats(request: Request) -> dict[str, Any]:
        return tools_for(request).cache_stats()

    # -- backup -------------------------------------------------------------

    @router.get("/backup/list")
    @limiter.limit("10/minute")
    def list_backups(request: Request) -> dict[str, Any]:
        unrestricted(request)
        backups = [b.as_dict() for b in fusion.backup.list_backups()]
        return {"backups": backups, "count": len(backups)}

    @router.post("/backup/create")
    @limiter.limit("5/minute")
    def create_backup(request: Request) -> dict[str, Any]:
        unrestricted(request)
        info = fusion.backup.create_backup()
        return {"message": "Backup created successfully", "backup": info.as_dict()}

    @router.get("/backup/stats")
    @limited
    def backup_stats(request: Request) -> dict[str, Any]:
        unrestricted(request)
        return fusion.backup.get_stats()

    # -- debug --------------------------------------------------------------

    @router.get("/debug/config")
    @limited
    def debug_config(request: Request) -> dict[str, Any]:
        """Effective engine configuration (hidden in production unless enabled)."""
        if not settings.debug_endpoints_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        return {
            "environment": settings.env,
            "memory_limit": settings.memory_limit,
            "threads": settings.threads,
            "cache_ttl": settings.cache_ttl,
            "cache_max_entries": settings.cache_max_entries,
            "rate_limit": rate_limit,
            "backup_enabled": settings.backup_enabled,
        }

    return router
