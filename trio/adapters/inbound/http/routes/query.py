"""Raw SQL query endpoint."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from trio.adapters.inbound.http.auth import AuthManager, Permission, caller_of, roles_of
from trio.adapters.inbound.http.request_context import request_id_of
from trio.domain.gateway.raw_query import QueryValidator
from trio.domain.governance.audit import AuditAction, AuditEvent
from trio.domain.governance.masking import NO_MASKING, CatalogMasking
from trio.ports.audit import AuditSink, NullAuditSink
from trio.ports.database import DatabaseGateway
from trio.ports.sql_policy import SqlAnalyzer

logger = logging.getLogger(__name__)

#: A whitelist may let a write through; it is recorded as one, not as a read.
_WRITES: dict[str, AuditAction] = {"INSERT": "create", "UPDATE": "update", "DELETE": "delete"}


class QueryRequest(BaseModel):
    """Request model for raw SQL query execution."""

    query: str = Field(..., description="SQL query to execute")
    params: dict[str, Any] | None = Field(
        default=None, description="Named parameters for the query (e.g., {'name': 'John'})"
    )


class QueryResponse(BaseModel):
    """Response model for query results."""

    success: bool = True
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    message: str | None = None


def create_query_router(
    db: DatabaseGateway,
    whitelist: list[str] | None = None,
    enabled: bool = True,
    auth_manager: AuthManager | None = None,
    masking: CatalogMasking = NO_MASKING,
    audit: AuditSink | None = None,
    db_name: str = "default",
    analyzer: SqlAnalyzer | None = None,
) -> APIRouter:
    """Create a router for raw SQL query execution.

    Security: this endpoint is disabled by default and, when enabled, should be
    pointed at a database account with a read-only role so that even a bypass of
    the command whitelist cannot mutate data. It is also refused at startup when
    enabled together with APP_ENV=production.

    Args:
        db: Database adapter instance.
        whitelist: List of allowed SQL commands.
        enabled: Whether raw queries are enabled.
        auth_manager: Optional auth manager for permission control.
        masking: This database's masking. A caller it would mask is refused:
            SQL sent straight to the database cannot be masked, and /olap/query
            runs the same question under the caller's masks.
        audit: Where one event per request goes: who, which tables (when
            ``analyzer`` can name them), the status and the row count. Never
            the SQL or its parameters, which are data.
        db_name: The database the events name.
        analyzer: Names the tables a query reads, for the event.

    Returns:
        FastAPI router with query endpoint.
    """
    router = APIRouter(prefix="/query", tags=["Raw Query"])
    validator = QueryValidator(whitelist)
    sink = audit or NullAuditSink()

    def record(http: Request, query: str, status: int, row_count: int | None = None) -> None:
        caller = caller_of(http)
        try:
            tables = analyzer.table_references(query) if analyzer else []
        except Exception:  # naming tables is best effort; the event is not
            tables = []
        first = query.split(maxsplit=1)[:1]
        sink.record(
            AuditEvent(
                # shortcut: judged by the first word, so WITH ... INSERT reads as a query; parse it if a whitelist allows that.
                action=_WRITES.get(first[0].upper(), "query") if first else "query",
                database=db_name,
                table=",".join(ref.table for ref in tables),
                actor=caller.name if caller else None,
                tenant=caller.tenant if caller else None,
                roles=tuple(caller.roles) if caller else (),
                request_id=request_id_of(http),
                status=status,
                row_count=row_count,
            )
        )

    # Auth dependencies
    def get_auth_deps(permission: Permission) -> list[Any]:
        if auth_manager and auth_manager.enabled:
            return [Depends(auth_manager.require(permission))]
        return []

    if not enabled:

        @router.post("/execute", response_model=QueryResponse)
        async def execute_query_disabled(request: QueryRequest) -> QueryResponse:
            """Raw query execution is disabled."""
            raise HTTPException(status_code=403, detail="Raw SQL query execution is disabled")

        return router

    @router.post(
        "/execute",
        response_model=QueryResponse,
        summary="Execute Raw SQL Query",
        description="""
Execute a raw SQL query against the database.

**Security Notes:**
- Only whitelisted SQL commands are allowed
- Use parameterized queries to prevent SQL injection
- Query results are limited for safety

**Example:**
```json
{
    "query": "SELECT * FROM users WHERE status = :status LIMIT 10",
    "params": {"status": "active"}
}
```
        """,
        dependencies=get_auth_deps(Permission.QUERY),
    )
    async def execute_query(request: QueryRequest, http: Request) -> QueryResponse:
        """Execute a raw SQL query."""
        if not masking.is_empty and masking.policy.for_roles(roles_of(http)):
            record(http, request.query, 403)
            raise HTTPException(
                status_code=403,
                detail="Column masking applies to this caller, and raw SQL is not masked; "
                "use /olap/query",
            )
        try:
            # Validate query
            validator.validate(request.query)

            # Execute query
            rows = await db.execute_query(query=request.query, params=request.params)

            record(http, request.query, 200, len(rows))
            return QueryResponse(success=True, rows=rows, row_count=len(rows))

        except ValueError as e:
            # Validation errors are our own safe messages — fine to return.
            record(http, request.query, 400)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            # Never leak DB/driver errors (schema, SQL text) to the client;
            # log the detail server-side instead.
            logger.exception(f"Raw query execution failed: {e}")
            record(http, request.query, 500)
            raise HTTPException(status_code=500, detail="Query execution failed") from e

    @router.get(
        "/allowed-commands",
        summary="Get Allowed SQL Commands",
        description="Returns the list of SQL commands that are allowed for raw queries.",
    )
    async def get_allowed_commands() -> dict[str, Any]:
        """Get list of allowed SQL commands."""
        return {"allowed_commands": validator.whitelist, "enabled": enabled}

    return router
