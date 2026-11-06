"""The query console at /ui: a static page, served with a strict CSP."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from trio.adapters.inbound.http.app import create_app, runtime_of
from trio.application.settings import RuntimeEnv, Settings


def _client(header_name: str | None = None) -> TestClient:
    app = create_app(env=RuntimeEnv())
    if header_name is not None:
        runtime_of(app).auth_manager = SimpleNamespace(header_name=header_name)
    return TestClient(app)


def test_the_page_names_the_default_key_header_and_loads_only_its_own_assets():
    r = _client().get("/ui")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert '<meta name="trio-key-header" content="X-API-Key">' in r.text
    assert '<meta name="trio-api-prefix" content="/api/v1">' in r.text
    assert '<meta name="trio-mcp-path" content="/mcp">' in r.text
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
    assert catalog.status_code == docs.status_code == ontology.status_code == 200
    # Server values reach the page as text nodes only.
    assert "innerHTML" not in js.text + catalog.text + docs.text + ontology.text
    assert client.get("/ui/index.html").status_code == 404
    assert client.get("/ui/__init__.py").status_code == 404


def test_the_console_is_not_in_the_openapi_spec():
    assert not any(p.startswith("/ui") for p in _client().get("/openapi.json").json()["paths"])


def _access(key: str | None, enabled: bool = True):
    from trio.adapters.inbound.http.auth import AuthManager
    from trio.application.settings import ApiKeyConfig, AuthConfig

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
        "actor": "reader",
        "read": True,
        "query": False,
        "update": False,
        "create": False,
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
    assert '<meta name="trio-mcp-path" content="/tools/mcp">' in TestClient(app).get("/ui").text
