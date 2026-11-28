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
    narrow = css.split("@media (max-width: 1279px)", 1)[1].split("\n}", 1)[0]
    assert ".header-auth [data-sign-in] { display: none; }" in narrow
    assert ".sign-in-bar:has([data-sign-in]:not([hidden])) { display: flex; }" in narrow
    # Both buttons of a kind get the same handler.
    assert 'querySelectorAll(`[data-sign-in="${how}"]`)' in client.get("/ui/console.js").text


def test_the_menu_offers_only_what_the_caller_may_open():
    """Every page says which permission it needs; the account menu holds the way out."""
    client = _client()
    page = client.get("/ui").text
    account = page.split('id="account-menu"', 1)[1].split("</details>", 1)[0]
    assert 'id="disconnect"' in account and 'id="change-password"' in account
    assert 'id="view-gate"' in page and "users-self" not in page
    needs = {}
    for name in ("console", "catalog", "docs", "audit", "keys", "users", "databases", "settings"):
        script = client.get(f"/ui/{name}.js").text
        for found in re.finditer(r'page\("(\w+)", \{\s*(?:needs: "(\w+)")?', script):
            needs[found.group(1)] = found.group(2)
    assert needs == {
        "query": "query",
        "catalog": "read",
        "audit": "audit",
        "keys": "admin",
        "users": "admin",
        "databases": "admin",
        "settings": "admin",
        "docs": None,
    }
    # The page asks who is calling once (whoAmI) and every screen reads that answer.
    asked = [
        n
        for n in ("console", "catalog", "audit")
        if '"/ui/access"' in client.get(f"/ui/{n}.js").text
    ]
    assert asked == ["console"]


ADMIN_PAGES = ("databases", "keys", "users", "audit", "settings")


def test_every_screen_has_a_head_and_no_form_in_its_side_panel():
    """A screen says what it is and offers its first action in its head; a side
    panel holds what to pick from, never the form that makes something."""
    client = _client()
    page = client.get("/ui").text
    views = dict(re.findall(r'<div class="view" id="view-(\w+)"(.*?)\n</div>', page, flags=re.S))
    assert set(views) == {"query", "catalog", "admin", "docs"}
    panes = dict(re.findall(r'<section id="pane-(\w+)"(.*?)</section>', views["admin"], flags=re.S))
    assert tuple(panes) == ADMIN_PAGES
    for name, body in {**views, **panes}.items():
        if name != "admin":
            assert re.search(r"<h1[^>]*data-t>", body), name
    assert views["admin"].count("<nav") == 1 and "<form" not in views["admin"]
    for name, body in (
        ("catalog", views["catalog"]),
        *((n, panes[n]) for n in ("keys", "users", "databases")),
    ):
        assert f'id="{name}-actions"' in body, name
    assert "keys-preview" not in page  # the rules' preview is inside the form's panel
    # The create forms open in a panel, which says their answers itself.
    for name, panel in (("keys", "keyPanel"), ("users", "userPanel"), ("databases", "dbPanel")):
        script = client.get(f"/ui/{name}.js").text
        assert f"if ({panel}.isOpen) return {panel}.say(text, error);" in script, name
    assert "function sidePanel(" in client.get("/ui/carbon.js").text


def test_the_catalog_side_list_shows_what_waits_and_narrows_as_the_reader_types():
    """Under the open catalog a table is a row of the side list, not a small link
    borrowed from the Query screen's column list; the review's state sits at the
    row's end, a filter keeps the tables still waiting, and the search box narrows
    the list as well as searching."""
    client = _client()
    script = client.get("/ui/catalog.js").text
    nav = script[
        script.index("// -- the side list") : script.index("// -- where a reviewed text came from")
    ]
    assert '"cols"' not in nav and '"link current"' not in nav
    assert 'class: current ? "item sub current" : "item sub"' in nav
    assert nav.count('"aria-pressed": String(') == 2  # All, Waiting: buttons that say which is on
    assert '"aria-expanded": String(open)' in nav
    assert (
        'role: "img", "aria-label": tr("Approved")' in nav
    )  # the tick is named for a screen reader
    # The server's name for a table is shown as it is, never translated.
    assert "tr(t.human_name" not in nav and "tr(t.table_name" not in nav
    # Typing narrows the list at once, and a place opened by its address ends the search.
    assert "if (review.list) renderNav(); // the names narrow at once" in script
    assert "const searched = endSearch();" in script
    style = client.get("/ui/console.css").text
    assert "#catalog-q { position: sticky; top: 0;" in style
    assert (
        "overflow-wrap: anywhere" in re.search(r"#catalogs a\.item \.name \{[^}]*\}", style).group()
    )


def test_administration_is_one_screen_and_the_old_addresses_still_open():
    client = _client()
    page = client.get("/ui").text
    header = page.split("</cds-header-nav>", 1)[0]
    assert re.findall(r'<cds-header-nav-item id="tab-(\w+)"', header) == [
        "query",
        "catalog",
        "admin",
        "docs",
    ]
    # Each of its pages has a link in its side panel and in the narrow window's menu.
    for name in ADMIN_PAGES:
        assert f'id="nav-{name}" href="#admin/{name}"' in page, name
        assert f'id="side-{name}" href="#admin/{name}"' in page, name
        script = client.get(f"/ui/{name}.js").text
        assert re.search(rf'page\("{name}", \{{\s*needs: "\w+",\s*area: "admin"', script), name
    console = client.get("/ui/console.js").text
    # #keys, #settings/masking and the rest, from before the area, are rewritten in place.
    assert 'const ADMIN = ["databases", "keys", "users", "audit", "settings"];' in console
    assert "} else if (ADMIN.includes(name)) {" in console
    assert 'history.replaceState(null, "", hashOf(name, ...args));' in console


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
        "assistant": False,
    }
    assert _access("steward-key").json()["update"] is True
    # Row rules keep a key from raw SQL, not from /olap, which rewrites them in.
    assert _access("tenant-key").json()["query"] is True


def test_the_assistant_is_offered_to_who_may_query_where_there_is_one():
    from truejoin.adapters.inbound.http.auth import AuthManager
    from truejoin.adapters.inbound.http.console import add_console
    from truejoin.application.settings import ApiKeyConfig, AuthConfig

    keys = [
        ApiKeyConfig(key="reader-key", name="reader", permissions=["read"]),
        ApiKeyConfig(key="analyst-key", name="analyst", permissions=["read", "query"]),
    ]

    def offered(key: str | None, answers: bool, enabled: bool = True, public=("/health",)) -> bool:
        from fastapi import FastAPI

        config = AuthConfig(enabled=enabled, public_paths=list(public), api_keys=keys)
        manager = AuthManager(config)
        app = FastAPI()
        add_console(app, lambda: {}, lambda: manager, lambda: answers)
        headers = {"X-API-Key": key} if key else {}
        return TestClient(app).get("/ui/access", headers=headers).json()["assistant"]

    assert offered("analyst-key", answers=True) is True
    assert offered("analyst-key", answers=False) is False  # off, or no model
    assert offered("reader-key", answers=True) is False  # may not query
    assert offered(None, answers=True, public=("/ui",)) is False  # nobody known
    assert offered(None, answers=True, enabled=False) is True  # auth off: everything is open


def test_the_assistant_mode_is_part_of_the_query_screen_and_hidden_until_offered():
    client = TestClient(create_app(env=RuntimeEnv()))
    page = client.get("/ui").text
    assert (
        '<label id="mode-assistant" hidden><input type="radio" name="mode" value="assistant">'
        in page
    )
    # Before console.js, which chooses a mode as it starts and asks this file what to show.
    assert (
        page.index("/ui/carbon.js") < page.index("/ui/assistant.js") < page.index("/ui/console.js")
    )
    script = client.get("/ui/assistant.js")
    assert script.status_code == 200 and "innerHTML" not in script.text
    # A model wrote the answer: it is shown as text, and nothing in it becomes a link.
    assert "rich(" not in script.text and "href" not in script.text
    console = client.get("/ui/console.js").text
    assert (
        "ACCESS.assistant === true" in console
        and 'if (!assisted && mode === "assistant")' in console
    )
    assert "stale: () => forgetConversation()" in console


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
