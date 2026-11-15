"""``/auth/users``: local users an administrator makes, changes and removes from the console.

Every route but one needs the ``admin`` permission. The exception is a user
changing their own password, which needs only to be signed in and to know the
current password. No answer carries a password or its hash.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from truejoin.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from truejoin.adapters.inbound.http.request_context import request_id_of
from truejoin.application.local_users import LocalUsers
from truejoin.domain.governance.audit import AuditAction, AuditEvent
from truejoin.domain.governance.keys import PERMISSIONS, Grantor
from truejoin.domain.governance.signin import SignInRefused, TooManyFailures
from truejoin.domain.governance.users import UserError
from truejoin.ports.audit import AuditSink

NOT_KEPT = {"detail": "Users cannot be made here: set auth.users.store to a file for them."}
NO_STORE = {"Cache-Control": "no-store"}


class UserRequest(BaseModel):
    """A user to make: who they are and what they may do, and a first password."""

    name: str = Field(..., min_length=1, max_length=64, description="The sign-in name")
    password: str = Field(..., min_length=1, max_length=1024, repr=False)
    permissions: list[str] = Field(..., min_length=1, max_length=8)
    display: str | None = Field(None, max_length=128, description="The name shown beside theirs")
    tenant: str | None = Field(None, max_length=128, description="What ${tenant} resolves to")


class UserChange(BaseModel):
    """What may change about a user; what is left out stays as it is."""

    permissions: list[str] | None = Field(None, min_length=1, max_length=8)
    display: str | None = Field(None, max_length=128)
    tenant: str | None = Field(None, max_length=128)
    clear_tenant: bool = False
    disabled: bool | None = None


class NewPassword(BaseModel):
    """The password an administrator sets for someone else."""

    password: str = Field(..., min_length=1, max_length=1024, repr=False)


class PasswordChange(BaseModel):
    """A user changing their own password."""

    current_password: str = Field(..., min_length=1, max_length=1024, repr=False)
    new_password: str = Field(..., min_length=1, max_length=1024, repr=False)


def create_users_router(
    users: LocalUsers | None, auth_manager: AuthManager | None, audit: AuditSink
) -> APIRouter:
    """List, make, change and remove local users; the ``admin`` permission is needed."""
    enabled = auth_manager is not None and auth_manager.enabled
    deps = [Depends(auth_manager.require(Permission.ADMIN))] if enabled and auth_manager else []
    anyone = (
        [Depends(auth_manager.require_any(list(Permission)))] if enabled and auth_manager else []
    )
    # Not "/users": a database's own table of that name is served at /{api_prefix}/users, and
    # this must not answer in its place (or the other way about).
    router = APIRouter(prefix="/auth/users", tags=["Users"])

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

    def service() -> LocalUsers:
        if users is None:
            raise HTTPException(status_code=501, detail=NOT_KEPT["detail"])
        return users

    def grantor_of(http: Request) -> Grantor:
        caller = caller_of(http)
        if not enabled or caller is None:
            raise HTTPException(
                status_code=409,
                detail="Authentication is off, so a user would not be checked: turn it on first",
            )
        return Grantor(
            name=caller.name,
            permissions=tuple(caller.permissions),
            roles=tuple(caller.roles),
            has_row_rules=caller.has_row_rules,
        )

    def refuse(error: Exception) -> HTTPException:
        if isinstance(error, LookupError):
            return HTTPException(status_code=404, detail=str(error))
        return HTTPException(status_code=422, detail=str(error))

    @router.get("", dependencies=deps, summary="The local users")
    async def list_users() -> dict[str, Any]:
        """Every local user, disabled ones too, never a password or its hash."""
        if users is None:
            return {"available": False, **NOT_KEPT, "users": [], "permissions": list(PERMISSIONS)}
        return {
            "available": True,
            "auth_enabled": enabled,
            "users": [user.as_public() for user in users.users()],
            "permissions": list(PERMISSIONS),
            "min_password_length": users.config.min_password_length,
        }

    @router.post("", dependencies=deps, status_code=201, summary="Make a user")
    async def create_user(http: Request, body: UserRequest) -> JSONResponse:
        """A new user, who can sign in with this password at once."""
        made = service()
        grantor = grantor_of(http)
        try:
            user = made.create(
                grantor,
                name=body.name,
                password=body.password,
                permissions=body.permissions,
                display=body.display,
                tenant=body.tenant,
            )
        except UserError as e:
            raise refuse(e) from e
        record(http, "create", f"create_user {user.name}")
        return JSONResponse({"user": user.as_public()}, status_code=201, headers=NO_STORE)

    # Before "/{name}": "me" is a route, not a user (the name is reserved).
    @router.put("/me/password", dependencies=anyone, summary="Change your own password")
    async def change_own_password(http: Request, body: PasswordChange) -> JSONResponse:
        """The signed-in user's password, once they have proved they know the current one."""
        made = service()
        caller = caller_of(http)
        if not enabled or caller is None:
            raise HTTPException(status_code=409, detail="Nobody is signed in")
        try:
            made.change_password(caller.name, body.current_password, body.new_password)
        except TooManyFailures as e:
            raise HTTPException(
                status_code=429, detail=str(e), headers={"Retry-After": str(e.retry_after)}
            ) from e
        except SignInRefused as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except (UserError, LookupError) as e:
            raise refuse(e) from e
        record(http, "update", f"change_password {caller.name}")
        return JSONResponse(
            {
                "changed": True,
                "detail": "Every sign-in with the old password has ended, this one too: sign in again.",
            },
            headers=NO_STORE,
        )

    @router.patch("/{name}", dependencies=deps, summary="Change a user")
    async def change_user(http: Request, name: str, body: UserChange) -> JSONResponse:
        """What a user may do, how they are shown, or whether they may sign in."""
        made = service()
        grantor = grantor_of(http)
        try:
            user = made.update(
                grantor,
                name,
                permissions=body.permissions,
                display=body.display,
                tenant=body.tenant,
                clear_tenant=body.clear_tenant,
                disabled=body.disabled,
            )
        except (UserError, LookupError) as e:
            raise refuse(e) from e
        what = [
            field
            for field in ("permissions", "display", "tenant", "disabled", "clear_tenant")
            if field in body.model_fields_set
        ]
        record(http, "update", f"update_user {user.name} {','.join(what)}")
        return JSONResponse({"user": user.as_public()}, headers=NO_STORE)

    @router.post("/{name}/password", dependencies=deps, summary="Set a user's password")
    async def set_password(http: Request, name: str, body: NewPassword) -> JSONResponse:
        """Replace a user's password. Whatever they were signed in with ends."""
        made = service()
        grantor = grantor_of(http)
        try:
            user = made.set_password(grantor, name, body.password)
        except (UserError, LookupError) as e:
            raise refuse(e) from e
        record(http, "update", f"reset_password {user.name}")
        return JSONResponse({"user": user.as_public()}, headers=NO_STORE)

    @router.delete("/{name}", dependencies=deps, summary="Remove a user")
    async def delete_user(http: Request, name: str) -> JSONResponse:
        """Remove a user: they are refused from the next request on."""
        made = service()
        grantor = grantor_of(http)
        try:
            user = made.delete(grantor, name)
        except (UserError, LookupError) as e:
            raise refuse(e) from e
        record(http, "delete", f"delete_user {user.name}")
        return JSONResponse({"user": user.as_public()}, headers=NO_STORE)

    return router
