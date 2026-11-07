"""Audit trail endpoint: the recorded events, for the people who review them."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from trio.adapters.inbound.http.auth import AuthManager, Permission
from trio.ports.audit import AuditReader


def create_audit_router(
    reader: AuditReader | None, auth_manager: AuthManager | None = None
) -> APIRouter:
    """``GET /audit``: the newest events, filtered by field.

    Needs the ``audit`` permission (or ``all``). Events carry names, never row
    values, but who read which table is itself worth guarding.
    """
    deps = (
        [Depends(auth_manager.require(Permission.AUDIT))]
        if auth_manager is not None and auth_manager.enabled
        else []
    )
    router = APIRouter(prefix="/audit", tags=["Audit"])

    @router.get("", dependencies=deps, summary="Read the audit trail, newest first")
    async def read_audit(  # noqa: PLR0913
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        event: Literal["data_access", "catalog_change", "tool_call"] | None = None,
        actor: str | None = None,
        tool: str | None = None,
        outcome: Literal["answered", "empty", "refused", "failed", "error"] | None = None,
        database: str | None = None,
        table: str | None = None,
        action: str | None = None,
        restricted: bool | None = None,
    ) -> dict[str, Any]:
        """Recorded events, newest first; every filter must match exactly (any case)."""
        if reader is None:
            return {
                "available": False,
                "detail": "Nothing to read: set audit.enabled and audit.file to keep a trail.",
                "events": [],
                "complete": True,
            }
        match = {
            key: str(value)
            for key, value in {
                "event": event,
                "actor": actor,
                "tool": tool,
                "outcome": outcome,
                "database": database,
                "table": table,
                "action": action,
                "restricted": restricted,
            }.items()
            if value not in (None, "")
        }
        events, complete = reader.recent(limit, match)
        return {"available": True, "events": events, "complete": complete}

    return router
