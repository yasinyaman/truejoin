"""``/keys``: API keys an administrator hands out and revokes, without editing the config."""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from truejoin.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from truejoin.adapters.inbound.http.request_context import request_id_of
from truejoin.application.access_keys import MAX_DAYS, AccessKeyService, checked_row_filters
from truejoin.domain.governance.audit import AuditAction, AuditEvent
from truejoin.domain.governance.keys import AccessKeyError, Grantor
from truejoin.domain.governance.row_policy import check_rule_names
from truejoin.ports.audit import AuditSink


class KeyRequest(BaseModel):
    """A key to hand out: what it may do is fixed now and cannot be edited later."""

    name: str = Field(..., min_length=1, max_length=64, description="Who or what holds it")
    permissions: list[str] = Field(..., min_length=1, max_length=8)
    tenant: str | None = Field(None, max_length=128, description="What ${tenant} resolves to")
    roles: list[str] | None = Field(
        None, max_length=32, description="Left out: yours, which are the only ones allowed"
    )
    row_filters: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict, description="table -> [{column, operator, value}], as in the config"
    )
    expires_in_days: int | None = Field(None, ge=1, le=MAX_DAYS)


class PreviewRequest(BaseModel):
    """Row rules to try before a key is made with them: nothing is kept."""

    name: str = Field("preview", min_length=1, max_length=64, description="What ${username} is")
    tenant: str | None = Field(None, max_length=128, description="What ${tenant} resolves to")
    roles: list[str] | None = Field(None, max_length=32, description="Left out: yours")
    row_filters: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


NOT_KEPT = {"detail": "Keys cannot be handed out here: set auth.key_store to a file for them."}

#: Each table's bare name and its columns, read when asked: databases come and go.
KnownTables = Callable[[], Mapping[str, Iterable[str]]]
#: The rows of each table that rules would leave: (request, rules checked) -> tables.
Previewer = Callable[
    [Request, PreviewRequest, dict[str, list[dict[str, Any]]]], list[dict[str, Any]]
]


def create_keys_router(
    keys: AccessKeyService | None,
    auth_manager: AuthManager | None,
    audit: AuditSink,
    known: KnownTables | None = None,
    preview: Previewer | None = None,
) -> APIRouter:
    """List, hand out and revoke keys; every route needs the ``admin`` permission.

    ``known`` refuses a key whose row rules name a table or column nothing
    serves; ``preview`` counts what rules would leave (it needs the analytics
    engine). Without them, keys are made as before and the preview is refused.
    """
    enabled = auth_manager is not None and auth_manager.enabled
    deps = [Depends(auth_manager.require(Permission.ADMIN))] if enabled and auth_manager else []
    router = APIRouter(prefix="/keys", tags=["Keys"])

    def record(http: Request, action: AuditAction, change: str) -> None:
        caller = caller_of(http)
        audit.record(
            AuditEvent(
                action=action,
                database="",
                table="",
                actor=caller.name if caller else None,
                tenant=caller.tenant if caller else None,
                roles=tuple(caller.roles) if caller else (),
                request_id=request_id_of(http),
                change=change,
                subject="access",
            )
        )

    @router.get("", dependencies=deps, summary="The keys handed out, and the configured ones")
    async def list_keys() -> dict[str, Any]:
        """Every handed-out key (revoked ones too), never a secret or a digest.

        The configured keys are listed by name and grant only: they are changed
        in the configuration, not here.
        """
        configured = [
            {
                "name": key.name,
                "permissions": key.permissions,
                "tenant": key.tenant,
                "roles": key.roles,
                "row_rules": key.has_row_rules,
            }
            for key in (auth_manager.config.api_keys if auth_manager else [])
            if key.key
        ]
        if keys is None:
            return {"available": False, **NOT_KEPT, "keys": [], "configured": configured}
        at = keys.now()
        return {
            "available": True,
            "auth_enabled": enabled,
            "keys": [key.as_public(at) for key in keys.keys()],
            "configured": configured,
        }

    @router.post("", dependencies=deps, status_code=201, summary="Hand out a key")
    async def create_key(http: Request, body: KeyRequest) -> JSONResponse:
        """A new key. Its secret is in this answer and nowhere else, ever."""
        if keys is None:
            raise HTTPException(status_code=501, detail=NOT_KEPT["detail"])
        caller = caller_of(http)
        if not enabled or caller is None:
            raise HTTPException(
                status_code=409,
                detail="Authentication is off, so a key would not be checked: turn it on first",
            )
        grantor = Grantor(
            name=caller.name,
            permissions=tuple(caller.permissions),
            roles=tuple(caller.roles),
            has_row_rules=caller.has_row_rules,
        )
        try:
            key, secret = keys.create(
                grantor,
                name=body.name,
                permissions=body.permissions,
                tenant=body.tenant,
                roles=body.roles,
                row_filters=body.row_filters,
                expires_in_days=body.expires_in_days,
                known=known() if known is not None else None,
            )
        except AccessKeyError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        record(http, "create", f"create_key {key.name}")
        return JSONResponse(
            {"key": key.as_public(keys.now()), "secret": secret},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/preview", dependencies=deps, summary="What a key with these row rules would see")
    def preview_key(http: Request, body: PreviewRequest) -> dict[str, Any]:
        """Per table the rules name: the rows they leave, out of the rows you see.

        Plain ``def``: it reads through the analytics engine, which is never
        called from the server's loop. Each count is audited as yours, and one
        more event names the key previewed; no value is returned or recorded.
        """
        if preview is None:
            raise HTTPException(
                status_code=501,
                detail="Previewing row rules needs the analytics engine (olap): it counts rows",
            )
        try:
            rules = checked_row_filters(body.row_filters, body.tenant, body.name)
        except AccessKeyError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        tables_known = known() if known is not None else None
        problems: list[str] = []
        countable: dict[str, list[dict[str, Any]]] = {}
        for table, table_rules in rules.items():
            found = check_rule_names({table: table_rules}, tables_known) if tables_known else []
            problems += found
            if not found:  # a rule naming nothing would only fail, or count everything
                countable[table] = table_rules
        tables = preview(http, body, countable) if countable else []
        record(http, "read", f"preview_row_rules {body.name}")
        return {"problems": problems, "tables": tables}

    @router.delete("/{key_id}", dependencies=deps, summary="Revoke a key")
    async def revoke_key(http: Request, key_id: str) -> dict[str, Any]:
        """Revoke a key: its holder is refused from the next request on."""
        if keys is None:
            raise HTTPException(status_code=501, detail=NOT_KEPT["detail"])
        caller = caller_of(http)
        try:
            key = keys.revoke(key_id, caller.name if caller else None)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except AccessKeyError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        record(http, "delete", f"revoke_key {key.name}")
        return {"key": key.as_public(keys.now())}

    return router
