"""The console at ``/ui``: static pages over the ``/olap`` and catalog routes.

The page holds no data and needs no key to load. Every call it makes carries
the key the user types and goes through the same routes any client uses, so
row rules, masks and permissions apply as they do there.
"""

from collections.abc import Callable, Mapping
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from trio.adapters.inbound.http.auth import AuthManager, Permission

_DIR = Path(__file__).parent
_ASSETS = {
    "console.js": "text/javascript",
    "catalog.js": "text/javascript",
    "ontology.js": "text/javascript",
    "docs.js": "text/javascript",
    "console.css": "text/css",
}

# Same-origin scripts, styles and fetches only: nothing from a CDN, which an
# air-gapped install could not reach anyway.
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


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
    assets = {name: (_DIR / name).read_text(encoding="utf-8") for name in _ASSETS}

    @app.get("/ui/access", include_in_schema=False)
    async def console_access(request: Request) -> dict[str, Any]:
        """What the caller's key may do, so the page offers only that.

        A hint, not a guard: every route still checks the key itself.
        """
        manager = auth()
        if manager is None or not manager.enabled:
            return {
                "actor": None,
                "read": True,
                "query": True,
                "update": True,
                "create": True,
            }
        user = await manager.get_current_user(request)  # 401 on a missing or unknown key
        if user is None:  # /ui listed as a public path: nobody is known
            return {
                "actor": None,
                "read": False,
                "query": False,
                "update": False,
                "create": False,
            }
        return {
            "actor": user.name,
            "read": user.has_permission(Permission.READ),
            # The console queries through /olap, which rewrites row rules in.
            "query": user.grants(Permission.QUERY),
            "update": user.has_permission(Permission.UPDATE),
            "create": user.has_permission(Permission.CREATE),
        }

    @app.get("/ui", include_in_schema=False)
    def console_page() -> HTMLResponse:
        html = page
        for name, value in values().items():
            html = html.replace("{{" + name + "}}", escape(value))
        return HTMLResponse(html, headers=HEADERS)

    @app.get("/ui/{name}", include_in_schema=False)
    def console_asset(name: str) -> Response:
        if name not in assets:
            raise HTTPException(status_code=404, detail="Not found")
        return Response(assets[name], media_type=_ASSETS[name], headers=HEADERS)
