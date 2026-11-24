"""The query console at /ui: a static page, served with a strict CSP."""

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from truejoin.adapters.inbound.http.app import create_app, runtime_of
from truejoin.application.settings import RuntimeEnv, Settings


def _client(header_name: str | None = None) -> TestClient:
    app = create_app(env=RuntimeEnv())
    if header_name is not None:
        runtime_of(app).auth_manager = SimpleNamespace(header_name=header_name)
    return TestClient(app)


def test_the_page_names_the_default_key_header_and_loads_only_its_own_assets():
    r = _client().get("/ui")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert '<meta name="truejoin-key-header" content="X-API-Key">' in r.text
    assert '<meta name="truejoin-api-prefix" content="/api/v1">' in r.text
    assert '<meta name="truejoin-mcp-path" content="/mcp">' in r.text
    assert "{{" not in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_the_configured_key_header_is_escaped_into_the_page():
    r = _client('X-"><script>').get("/ui")
    assert '<script>"' not in r.text
    assert 'content="X-&quot;&gt;&lt;script&gt;"' in r.text


def test_the_script_and_style_are_served_with_their_types():
    client = _client()
    js = client.get("/ui/console.js")
    css = client.get("/ui/console.css")
    assert js.status_code == css.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    assert css.headers["content-type"].startswith("text/css")
    assert js.headers["x-content-type-options"] == "nosniff"
    catalog = client.get("/ui/catalog.js")
    docs = client.get("/ui/docs.js")
    ontology = client.get("/ui/ontology.js")
    audit = client.get("/ui/audit.js")
    results = client.get("/ui/results.js")
    keys = client.get("/ui/keys.js")
    databases = client.get("/ui/databases.js")
    settings_js = client.get("/ui/settings.js")
    for name in ("tr.js", "i18n.js", "carbon.js", "theme.js"):
        assert client.get(f"/ui/{name}").status_code == 200, name
    assert catalog.status_code == docs.status_code == ontology.status_code == 200
    assert audit.status_code == results.status_code == keys.status_code == 200
    assert databases.status_code == settings_js.status_code == 200
    # Server values reach the page as text nodes only.
    scripts = js.text + catalog.text + docs.text + ontology.text + audit.text + results.text
    scripts += keys.text + databases.text + settings_js.text
    assert "innerHTML" not in scripts
    assert client.get("/ui/index.html").status_code == 404
    assert client.get("/ui/__init__.py").status_code == 404


def test_every_way_to_sign_in_has_a_button_outside_the_header():
    """The header has no room for the sign-in buttons on a phone or a laptop, so each
    has a twin in the bar under it, and the rule that hides one shows the other."""
    client = _client()
    page = client.get("/ui").text
    header, rest = page.split("</cds-header>", 1)
    bar = rest.split('id="sign-in-bar"', 1)[1].split("</div>", 1)[0]
    in_header = set(re.findall(r'data-sign-in="(\w+)"', header))
    assert in_header == {"provider", "password"}
    assert set(re.findall(r'data-sign-in="(\w+)"', bar)) == in_header
    assert 'id="refused"' in bar  # a refused key is said there too, at any width
    css = client.get("/ui/console.css").text
    # The header's buttons are hidden in one place only: where the bar shows.
    assert css.count("[data-sign-in] { display: none; }") == 1
    assert "#sign-in-password" not in css and "#sign-in," not in css
    narrow = css.split("@media (max-width: 1599px)", 1)[1].split("\n}", 1)[0]
    assert ".header-auth [data-sign-in] { display: none; }" in narrow
    assert ".sign-in-bar:has([data-sign-in]:not([hidden])) { display: flex; }" in narrow
    # Both buttons of a kind get the same handler.
    assert 'querySelectorAll(`[data-sign-in="${how}"]`)' in client.get("/ui/console.js").text


def test_the_console_is_not_in_the_openapi_spec():
    assert not any(p.startswith("/ui") for p in _client().get("/openapi.json").json()["paths"])


def _access(key: str | None, enabled: bool = True):
    from truejoin.adapters.inbound.http.auth import AuthManager
    from truejoin.application.settings import ApiKeyConfig, AuthConfig

    app = create_app(env=RuntimeEnv())
    runtime_of(app).auth_manager = AuthManager(
        AuthConfig(
            enabled=enabled,
            public_paths=["/health"],
            api_keys=[
                ApiKeyConfig(key="reader-key", name="reader", permissions=["read"]),
                ApiKeyConfig(key="steward-key", name="steward", permissions=["read", "update"]),
                ApiKeyConfig(
                    key="tenant-key",
                    name="tenant",
                    permissions=["read", "query"],
                    row_filters={"users": [{"column": "segment", "value": "a"}]},
                ),
            ],
        )
    )
    return TestClient(app).get("/ui/access", headers={"X-API-Key": key} if key else {})


def test_access_tells_the_page_what_the_key_may_do():
    reader = _access("reader-key").json()
    assert reader == {
        "auth_enabled": True,
        "actor": "reader",
        "read": True,
        "query": False,
        "update": False,
        "create": False,
        "audit": False,
        "admin": False,
    }
    assert _access("steward-key").json()["update"] is True
    # Row rules keep a key from raw SQL, not from /olap, which rewrites them in.
    assert _access("tenant-key").json()["query"] is True


def test_access_needs_a_known_key_when_auth_is_on():
    assert _access(None).status_code == 401
    assert _access("nope").status_code == 401
    assert _access(None, enabled=False).json()["update"] is True


def test_the_page_names_the_configured_mcp_path():
    app = create_app(env=RuntimeEnv())
    settings = Settings()
    settings.mcp.http_path = "/tools/mcp"
    runtime_of(app).settings = settings
    assert '<meta name="truejoin-mcp-path" content="/tools/mcp">' in TestClient(app).get("/ui").text


def test_carbon_and_plex_are_served_from_here_with_every_chunk_they_import():
    import re
    from pathlib import Path

    client = _client()
    page = client.get("/ui").text
    assert "https://" not in page.split("<body>")[0]  # nothing from a CDN
    vendor = Path("truejoin/adapters/inbound/http/console/vendor")
    entries = re.findall(r'src="/ui/vendor/(carbon/[^"]+\.js)"', page)
    assert "carbon/ui-shell.min.js" in entries
    imports = re.compile(r"""(?:from|import)\s*\(?\s*["']\./([^"']+\.js)["']""")
    todo, seen = list(entries), set()
    while todo:  # the page's entries and everything they import, transitively
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        assert (vendor / name).is_file(), f"{name} is imported but not vendored"
        todo += [f"carbon/{m}" for m in imports.findall((vendor / name).read_text())]
    for path in ("carbon/ui-shell.min.js", "carbon/themes.css", "plex/plex.css"):
        response = client.get(f"/ui/vendor/{path}")
        assert response.status_code == 200, path
    font = client.get("/ui/vendor/plex/IBMPlexSans-Regular.woff2")
    assert font.headers["content-type"] == "font/woff2"
    assert "font-src 'self'" in font.headers["content-security-policy"]
    assert client.get("/ui/vendor/carbon/nope.js").status_code == 404
    assert client.get("/ui/vendor/README.md").status_code == 404
    assert client.get("/ui/vendor/../__init__.py").status_code == 404


def test_the_console_knows_a_refusal_for_unread_words_by_how_the_server_starts_it():
    # The page shows its own words for it; a reworded server message would bring the raw one back.
    from truejoin.application.olap.tools import _UNREAD

    script = Path("truejoin/adapters/inbound/http/console/console.js").read_text(encoding="utf-8")
    (prefix,) = re.findall(r'^const UNREAD = "([^"]+)";$', script, flags=re.MULTILINE)
    assert _UNREAD.startswith(prefix)


def test_the_console_and_the_pilot_know_a_question_no_one_reading_holds_the_same_way():
    from truejoin.application.olap.tools import _APART
    from truejoin.domain.olap.pilot import _APART_PREFIX

    script = Path("truejoin/adapters/inbound/http/console/console.js").read_text(encoding="utf-8")
    (prefix,) = re.findall(r'^const APART = "([^"]+)";$', script, flags=re.MULTILINE)
    assert _APART.startswith(prefix) and _APART.startswith(_APART_PREFIX)
