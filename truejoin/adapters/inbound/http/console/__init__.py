"""The console at ``/ui``: static pages over the ``/olap`` and catalog routes.

The page holds no data and needs no key to load. Every call it makes carries
the key the user types and goes through the same routes any client uses, so
row rules, masks and permissions apply as they do there.
"""

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from truejoin.adapters.inbound.http.auth import AuthManager, Permission
from truejoin.adapters.inbound.http.ldap import (
    DirectoryUnreachable,
    SignInRefused,
    TooManyFailures,
)
from truejoin.adapters.inbound.http.oidc import ProviderUnreachable, TokenError

_DIR = Path(__file__).parent
_ASSETS = {
    "theme.js": "text/javascript",
    "tr.js": "text/javascript",
    "i18n.js": "text/javascript",
    "carbon.js": "text/javascript",
    "console.js": "text/javascript",
    "results.js": "text/javascript",
    "catalog.js": "text/javascript",
    "ontology.js": "text/javascript",
    "guide.js": "text/javascript",
    "docs.js": "text/javascript",
    "audit.js": "text/javascript",
    "keys.js": "text/javascript",
    "users.js": "text/javascript",
    "databases.js": "text/javascript",
    "settings.js": "text/javascript",
    "console.css": "text/css",
}

#: The Carbon components and IBM Plex, served from here (see vendor/README.md).
_VENDOR = _DIR / "vendor"
_VENDOR_TYPES = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}
#: Carbon's chunk names carry a content hash; a day is enough for the rest.
VENDOR_HEADERS = {"Cache-Control": "public, max-age=86400"}

# Same-origin scripts, styles and fetches only: nothing from a CDN, which an
# air-gapped install could not reach anyway.
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "font-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class SignInGate:
    """Calls to the identity provider for the console, on threads of their own.

    The sign-in routes need no key and each waits up to ten seconds on the
    provider. On the shared thread pool, a burst of them (junk refresh
    tokens while the provider is slow) would queue every bearer token check
    behind it; here they have ``workers`` threads, and past ``waiting`` calls
    in flight the next is turned away at once. A per-address limit would not
    do: behind a proxy every user shares one address, and refreshes are
    spread over the day.
    """

    def __init__(self, workers: int = 4, waiting: int = 16) -> None:
        """Size the pool and the queue in front of it."""
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sign-in")
        self._waiting = waiting
        self._in_flight = 0

    async def run(self, fn: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
        """``fn(*args)`` on the gate's threads, or 503 when too many are waiting."""
        if self._in_flight >= self._waiting:
            raise HTTPException(
                status_code=503,
                detail="Too many sign-ins at once; try again in a moment",
                headers={"Retry-After": "5"},
            )
        self._in_flight += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)
        finally:
            self._in_flight -= 1


class CodeExchange(BaseModel):
    """What the console brings back from the identity provider."""

    code: str = Field(..., min_length=1, max_length=4096)
    # RFC 7636: 43 to 128 characters.
    code_verifier: str = Field(..., min_length=43, max_length=128)


class PasswordSignIn(BaseModel):
    """A directory username and password, typed into the console."""

    username: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=1024, repr=False)


class TokenRefresh(BaseModel):
    """The refresh token the console's sign-in gave it."""

    refresh_token: str = Field(..., min_length=1, max_length=8192)


def add_console(
    app: FastAPI,
    values: Callable[[], Mapping[str, str]],
    auth: Callable[[], AuthManager | None],
) -> None:
    """Serve the console.

    ``values`` fills the page's ``{{NAME}}`` placeholders when asked; ``auth``
    is the running app's auth manager (none before startup).
    """
    page = (_DIR / "index.html").read_text(encoding="utf-8")
    gate = SignInGate()
    assets = {name: (_DIR / name).read_text(encoding="utf-8") for name in _ASSETS}
    vendored = {
        str(path.relative_to(_VENDOR)): (path.read_bytes(), _VENDOR_TYPES[path.suffix])
        for path in _VENDOR.rglob("*")
        if path.suffix in _VENDOR_TYPES
    }

    # Before "/ui/{name}": only the files found at start are served, by exact path.
    @app.get("/ui/vendor/{path:path}", include_in_schema=False)
    def console_vendor(path: str) -> Response:
        found = vendored.get(path)
        if found is None:
            raise HTTPException(status_code=404, detail="Not found")
        body, media_type = found
        return Response(body, media_type=media_type, headers={**HEADERS, **VENDOR_HEADERS})

    @app.get("/ui/access", include_in_schema=False)
    async def console_access(request: Request) -> dict[str, Any]:
        """What the caller's key may do, so the page offers only that.

        A hint, not a guard: every route still checks the key itself.
        """
        manager = auth()
        if manager is None or not manager.enabled:
            return {
                "auth_enabled": False,
                "actor": None,
                "read": True,
                "query": True,
                "update": True,
                "create": True,
                "audit": True,
                "admin": True,
            }
        user = await manager.get_current_user(request)  # 401 on a missing or unknown key
        if user is None:  # /ui listed as a public path: nobody is known
            return {
                "auth_enabled": True,
                "actor": None,
                "read": False,
                "query": False,
                "update": False,
                "create": False,
                "audit": False,
                "admin": False,
            }
        return {
            "auth_enabled": True,
            "actor": user.name,
            "read": user.has_permission(Permission.READ),
            # The console queries through /olap, which rewrites row rules in.
            "query": user.grants(Permission.QUERY),
            "update": user.has_permission(Permission.UPDATE),
            "create": user.has_permission(Permission.CREATE),
            "audit": user.has_permission(Permission.AUDIT),
            "admin": user.has_permission(Permission.ADMIN),
        }

    @app.get("/ui", include_in_schema=False)
    def console_page() -> HTMLResponse:
        html = page
        for name, value in values().items():
            html = html.replace("{{" + name + "}}", escape(value))
        return HTMLResponse(html, headers=HEADERS)

    def redirect_uri(request: Request, manager: AuthManager) -> str:
        """Where the provider sends the user back: this server's console, nowhere else."""
        assert manager.oidc is not None
        base = manager.oidc.config.resource_url or str(request.base_url)
        return f"{base.rstrip('/')}/ui"

    # Before "/ui/{name}", which would take "sign-in" for an asset.
    @app.get("/ui/sign-in", include_in_schema=False)
    async def sign_in(request: Request) -> dict[str, Any]:
        """Whether the console can send its user to the identity provider, and how."""
        manager = auth()
        # "password" appears only when a directory is configured, so a server
        # without one answers exactly as before.
        extra = (
            {"password": True} if manager is not None and manager.password_sign_in_available else {}
        )
        if manager is None or manager.oidc is None:
            return {"enabled": False, **extra}
        answer = await gate.run(manager.oidc.console_sign_in, redirect_uri(request, manager))
        return {**answer, **extra}

    @app.post("/ui/sign-in/password", include_in_schema=False)
    async def sign_in_password(request: Request, body: PasswordSignIn) -> dict[str, Any]:
        """A username and password (a local user's or the directory's) for a token this server accepts.

        The console's form posts here; so can a script
        (``curl -d '{"username": ..., "password": ...}'``).
        """
        manager = auth()
        if manager is None or not manager.password_sign_in_available:
            raise HTTPException(status_code=404, detail="No password sign-in is configured")
        client = request.client.host if request.client else ""
        try:
            return await gate.run(manager.password_sign_in, body.username, body.password, client)
        except TooManyFailures as e:
            raise HTTPException(
                status_code=429, detail=str(e), headers={"Retry-After": str(e.retry_after)}
            ) from e
        except SignInRefused as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except DirectoryUnreachable as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.post("/ui/sign-in/token", include_in_schema=False)
    async def sign_in_token(request: Request, body: CodeExchange) -> dict[str, Any]:
        """The code the provider sent the user back with, for a token this server accepts."""
        manager = auth()
        if manager is None or manager.oidc is None:
            raise HTTPException(status_code=404, detail="No identity provider is configured")
        try:
            return await gate.run(
                manager.oidc.exchange, body.code, body.code_verifier, redirect_uri(request, manager)
            )
        except ProviderUnreachable as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        except TokenError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    @app.post("/ui/sign-in/refresh", include_in_schema=False)
    async def sign_in_refresh(body: TokenRefresh) -> dict[str, Any]:
        """A fresh access token before the one the console holds expires."""
        manager = auth()
        if manager is None or manager.oidc is None:
            raise HTTPException(status_code=404, detail="No identity provider is configured")
        try:
            return await gate.run(manager.oidc.refresh, body.refresh_token)
        except ProviderUnreachable as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        except TokenError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e

    @app.get("/ui/{name}", include_in_schema=False)
    def console_asset(name: str) -> Response:
        if name not in assets:
            raise HTTPException(status_code=404, detail="Not found")
        return Response(assets[name], media_type=_ASSETS[name], headers=HEADERS)
