"""``/databases``: connections an administrator adds, changes and removes from the console.

The configured databases (the YAML file) are listed read-only. An added one
is connected before it is kept, and served at once: its table routes, the
OLAP source, MCP and the catalog. No answer ever holds a password.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from truejoin.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from truejoin.adapters.inbound.http.request_context import request_id_of
from truejoin.application.settings import DatabaseConfig
from truejoin.domain.gateway.managed import ManagedDatabase, ManagedDatabaseError, explain_failure
from truejoin.domain.governance.audit import AuditAction, AuditEvent
from truejoin.ports.audit import AuditSink

if TYPE_CHECKING:
    from truejoin.adapters.inbound.http.app import LiveDatabases

NOT_KEPT = {"detail": "Databases cannot be added here: set database_store.path to a file for them."}


class DatabaseRequest(BaseModel):
    """A connection; on a change, a field left out (or a blank password) keeps its value."""

    name: str | None = Field(None, max_length=63)
    type: str | None = Field(None, max_length=32)
    host: str | None = Field(None, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    database: str | None = Field(None, max_length=255)
    username: str | None = Field(None, max_length=255)
    password: str | None = Field(None, max_length=1024)
    readonly_username: str | None = Field(None, max_length=255)
    readonly_password: str | None = Field(None, max_length=1024)
    options: dict[str, Any] | None = None

    def fields(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


def create_databases_router(
    live: LiveDatabases, auth_manager: AuthManager | None, audit: AuditSink, app_env: str
) -> APIRouter:
    """List, test, add, change and remove databases; every route needs ``admin``."""
    enabled = auth_manager is not None and auth_manager.enabled
    deps = [Depends(auth_manager.require(Permission.ADMIN))] if enabled and auth_manager else []
    router = APIRouter(prefix="/databases", tags=["Databases"])

    def service() -> Any:
        found = live.service
        if found is None:
            raise HTTPException(status_code=501, detail=NOT_KEPT["detail"])
        return found

    def may_change() -> None:
        # Unauthenticated, anyone who reaches the server could point it at any
        # host it can reach; outside development that is refused.
        if not enabled and app_env != "development":
            raise HTTPException(
                status_code=409,
                detail="Authentication is off, so anyone could add a database: turn it on first",
            )

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

    def by(http: Request) -> str | None:
        caller = caller_of(http)
        return caller.name if caller else None

    def shown(database: ManagedDatabase) -> dict[str, Any]:
        state, why = live.state(database.name)
        return {**database.public(), "state": state, "error": why}

    def unprocessable(e: Exception) -> HTTPException:
        return HTTPException(status_code=422, detail=str(e))

    def refused(e: ManagedDatabaseError) -> JSONResponse:
        """422 with the reason beside the words, when connecting is what failed."""
        body: dict[str, Any] = {"detail": str(e)}
        if e.reason:
            body |= {"reason": e.reason, "raw": e.raw}
        return JSONResponse(status_code=422, content=body)

    def failed(e: Exception, database: ManagedDatabase) -> JSONResponse:
        return refused(
            explain_failure(
                e,
                "could not serve it",
                host=database.host,
                port=database.port,
                database=database.database,
                secrets=[database.password, database.readonly_password],
            )
        )

    @router.get("", dependencies=deps, summary="The configured databases and the added ones")
    async def list_databases() -> dict[str, Any]:
        configured = [
            {
                "name": db.name,
                "type": db.type,
                "host": db.host,
                "port": db.port,
                "database": db.database,
                "state": live.state(db.name)[0],
            }
            for db in live.configured()
        ]
        found = live.service
        if found is None:
            return {"available": False, **NOT_KEPT, "databases": [], "configured": configured}
        return {
            "available": True,
            "may_change": enabled or app_env == "development",
            "types": found.supported_types,
            "databases": [shown(db) for db in found.all()],
            "configured": configured,
        }

    @router.post("/test", dependencies=deps, summary="Try a connection without keeping it")
    async def test_database(
        http: Request, body: DatabaseRequest
    ) -> Any:  # a dict, or the 422 that says why it could not connect
        """A new connection, or an added database with ``name`` and only what changes."""
        databases = service()
        may_change()
        fields = body.fields()
        try:
            name = fields.get("name", "")
            existing = name and any(d.name == name for d in databases.all())
            database = (
                databases.change(name, fields, by(http))
                if existing
                else databases.prepare(fields, by(http))
            )
            await live.test(DatabaseConfig(**database.connection()))
        except ManagedDatabaseError as e:
            return refused(e)
        except LookupError as e:
            raise unprocessable(e) from e
        return {"ok": True}

    @router.post("", dependencies=deps, status_code=201, summary="Add a database")
    async def add_database(
        http: Request, body: DatabaseRequest
    ) -> Any:  # a dict, or the 422 that says why it could not connect
        """Connected and served first, kept second: what cannot connect is not kept."""
        databases = service()
        may_change()
        try:
            database = databases.prepare(body.fields(), by(http))
            live.check_name(database.name)
            await live.test(DatabaseConfig(**database.connection()))
            await live.attach(DatabaseConfig(**database.connection()))
        except ManagedDatabaseError as e:
            return refused(e)
        except Exception as e:
            return failed(e, database)
        try:
            databases.add(database)
        except ManagedDatabaseError as e:  # taken meanwhile by another process
            await live.detach(database.name)
            raise unprocessable(e) from e
        record(http, "create", f"add_database {database.name}")
        return {"database": shown(database)}

    @router.put("/{name}", dependencies=deps, summary="Change an added database")
    async def change_database(
        http: Request, name: str, body: DatabaseRequest
    ) -> Any:  # a dict, or the 422 that says why it could not connect
        """The new connection is tried first; if it fails, the old one keeps serving."""
        databases = service()
        may_change()
        try:
            old = databases.get(name)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        try:
            new = databases.change(name, body.fields(), by(http))
            await live.test(DatabaseConfig(**new.connection()))
            await live.swap(DatabaseConfig(**old.connection()), DatabaseConfig(**new.connection()))
        except ManagedDatabaseError as e:
            return refused(e)
        except Exception as e:
            return failed(e, new)
        databases.replace(new)
        record(http, "update", f"update_database {name}")
        return {"database": shown(new)}

    @router.delete("/{name}", dependencies=deps, summary="Remove an added database")
    async def remove_database(http: Request, name: str) -> dict[str, Any]:
        """Its routes, its OLAP source and its connections go at once."""
        databases = service()
        may_change()
        if live.service is not None and databases.is_configured(name):
            raise HTTPException(
                status_code=409, detail=f"{name} is in the configuration file: remove it there"
            )
        try:
            databases.get(name)
        except LookupError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        await live.detach(name)
        removed = databases.remove(name)
        record(http, "delete", f"remove_database {name}")
        return {"database": removed.public()}

    return router
