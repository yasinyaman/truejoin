"""``/settings``: the configuration an administrator changes from the console, served at once.

What the configuration file writes, and the bootstrap settings, are shown
locked. A change is validated as a whole, a dangerous one needs its path
typed back, and the server is rebuilt with it before the answer comes back.
The audit trail names the paths changed, never their values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from truejoin.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from truejoin.adapters.inbound.http.request_context import request_id_of
from truejoin.application.settings_service import SettingsError
from truejoin.domain.governance.audit import AuditEvent
from truejoin.ports.audit import AuditSink

if TYPE_CHECKING:
    from truejoin.adapters.inbound.http.app import LiveSettings

NOT_KEPT = {
    "detail": "Settings cannot be changed here: set settings_store.path to a file for them."
}


class SettingsChange(BaseModel):
    """Paths to set, paths to put back to the file's or the default value, and confirmations."""

    changes: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list, max_length=200)
    confirm: list[str] = Field(default_factory=list, max_length=200)


def create_settings_router(
    live: LiveSettings, auth_manager: AuthManager | None, audit: AuditSink, app_env: str
) -> APIRouter:
    """Read and change the settings; every route needs ``admin``."""
    enabled = auth_manager is not None and auth_manager.enabled
    deps = [Depends(auth_manager.require(Permission.ADMIN))] if enabled and auth_manager else []
    router = APIRouter(prefix="/settings", tags=["Settings"])

    @router.get("", dependencies=deps, summary="Every setting, as the server runs with it now")
    async def read_settings() -> dict[str, Any]:
        service = live.service
        if service is None:
            return {"available": False, **NOT_KEPT, "settings": []}
        return {
            "available": True,
            "may_change": enabled or app_env == "development",
            "settings": service.effective(live.current),
        }

    @router.put("", dependencies=deps, summary="Change settings; served before this answers")
    async def change_settings(http: Request, body: SettingsChange) -> dict[str, Any]:
        service = live.service
        if service is None:
            raise HTTPException(status_code=501, detail=NOT_KEPT["detail"])
        if not enabled and app_env != "development":
            raise HTTPException(
                status_code=409,
                detail="Authentication is off, so anyone could change settings: turn it on first",
            )
        if not body.changes and not body.reset:
            raise HTTPException(status_code=422, detail="nothing to change")
        caller = caller_of(http)
        try:
            _, stored = service.prepare(
                body.changes,
                body.confirm,
                by=caller.name if caller else None,
                app_env=app_env,
                reset=body.reset,
            )
        except SettingsError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        try:
            await live.apply(stored)
        except Exception as e:
            raise HTTPException(
                status_code=422, detail=f"the server could not start with these settings: {e}"
            ) from e
        paths = sorted({*body.changes, *body.reset})
        audit.record(
            AuditEvent(
                action="update",
                database="",
                table="",
                actor=caller.name if caller else None,
                tenant=caller.tenant if caller else None,
                roles=tuple(caller.roles) if caller else (),
                request_id=request_id_of(http),
                change=f"settings {','.join(paths)}",
                subject="access",
            )
        )
        current = live.service
        return {"changed": paths, "settings": current.effective(live.current) if current else []}

    return router
