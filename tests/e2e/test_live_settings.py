"""Settings changed from the console while the server runs.

The real stack in one process over fake gateways: a change is served before
its answer comes back (a new generation is built and swapped in), one that
does not build changes nothing, and what the file writes stays locked.
"""

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tests.e2e.test_one_process import GatewayFactory, _approve, _settings
from truejoin.adapters.inbound.http import app as http_app
from truejoin.adapters.inbound.http.app import create_app
from truejoin.application.settings import RuntimeEnv
from truejoin.bootstrap import build_container

ADMIN = {"X-API-Key": "admin-key"}
READER = {"X-API-Key": "reader-key"}
SQL = "SELECT id, email FROM shop.users ORDER BY id"


class Server:
    def __init__(self, tmp_path, monkeypatch, file_paths=frozenset(), **core):
        monkeypatch.setenv("TRUEJOIN_SECRET_KEY", Fernet.generate_key().decode())
        self.factory = GatewayFactory()
        self.trail = tmp_path / "audit.jsonl"
        core = {
            "settings_store": {"path": str(tmp_path / "settings.json")},
            "auth": {
                "enabled": True,
                "api_keys": [
                    {"key": "admin-key", "name": "admin", "permissions": ["all"]},
                    {"key": "reader-key", "name": "reader", "permissions": ["read", "query"]},
                ],
            },
            "audit": {"enabled": True, "file": str(self.trail)},
            **core,
        }
        self.settings = _settings(tmp_path, settings=core).with_file_paths(frozenset(file_paths))

    def client(self) -> TestClient:
        app = create_app(
            container_factory=lambda: build_container(self.settings, gateway_factory=self.factory),
            env=RuntimeEnv(),
        )
        app.state.retire_after = 0
        return TestClient(app)


def _change(client, changes=None, confirm=(), reset=()):
    return client.put(
        "/api/v1/settings",
        json={"changes": changes or {}, "confirm": list(confirm), "reset": list(reset)},
        headers=ADMIN,
    )


def _tools(client) -> list[str]:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream", **ADMIN},
    )
    return sorted(t["name"] for t in response.json()["result"]["tools"])


def _setting(client, path):
    rows = client.get("/api/v1/settings", headers=ADMIN).json()["settings"]
    return next(r for r in rows if r["path"] == path)


def test_a_change_is_served_before_its_answer(tmp_path, monkeypatch):
    with Server(tmp_path, monkeypatch).client() as client:
        assert client.get("/api/v1/shop/users?limit=3", headers=ADMIN).status_code == 200
        changed = _change(client, {"settings.pagination.max_limit": 2})
        assert changed.status_code == 200, changed.text
        assert client.get("/api/v1/shop/users?limit=3", headers=ADMIN).status_code == 422
        assert client.get("/api/v1/shop/users?limit=2", headers=ADMIN).status_code == 200
        row = _setting(client, "settings.pagination.max_limit")
        assert (row["value"], row["source"], row["changed_by"]) == (2, "console", "admin")
        # The admin routes still answer first, and the OLAP side was rebuilt too.
        assert client.post("/olap/query", json={"sql": SQL}, headers=ADMIN).status_code == 200


def test_a_dangerous_one_needs_its_name(tmp_path, monkeypatch):
    with Server(tmp_path, monkeypatch).client() as client:
        before = _tools(client)
        refused = _change(client, {"mcp.tools": ["list_sources"]})
        assert refused.status_code == 422 and "confirm" in refused.text
        assert (
            _change(client, {"mcp.tools": ["list_sources"]}, confirm=["mcp.tools"]).status_code
            == 200
        )
        assert _tools(client) == ["list_sources"] != before


def test_masking_turned_on_from_the_console(tmp_path, monkeypatch):
    _approve(tmp_path, {"users": {"email": "email"}})
    with Server(tmp_path, monkeypatch).client() as client:
        plain = client.post("/olap/query", json={"sql": SQL}, headers=READER).json()["rows"]
        assert plain[0]["email"] == "a@example.com"
        masking = {"settings.masking.enabled": True, "settings.masking.rules": {"email": "partial"}}
        assert _change(client, masking, confirm=list(masking)).status_code == 200
        rows = client.post("/olap/query", json={"sql": SQL}, headers=READER).json()["rows"]
        assert rows[0]["email"] == "a***@example.com"
        assert client.get("/api/v1/shop/users", headers=READER).json()["items"][0]["email"] != (
            "a@example.com"
        )


def test_what_the_file_writes_and_bootstrap_are_locked(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch, file_paths={"settings.pagination.default_limit"})
    with server.client() as client:
        assert _setting(client, "settings.pagination.default_limit")["locked"] == "file"
        assert _setting(client, "settings.audit.enabled")["locked"] == "bootstrap"
        for path in ("settings.pagination.default_limit", "settings.audit.enabled", "olap.threads"):
            assert _change(client, {path: 1}).status_code == 422
        assert _change(client, {"settings.not_a_setting": 1}).status_code == 422


def test_an_invalid_value_or_a_failed_build_changes_nothing(tmp_path, monkeypatch):
    with Server(tmp_path, monkeypatch).client() as client:
        bad = _change(client, {"olap.rate_limit": "lots"})
        assert bad.status_code == 422
        real_build = http_app._build

        async def failing(*args, **kwargs):
            raise RuntimeError("cannot build")

        monkeypatch.setattr(http_app, "_build", failing)
        failed = _change(client, {"settings.pagination.max_limit": 2})
        assert failed.status_code == 422 and "cannot build" in failed.text
        monkeypatch.setattr(http_app, "_build", real_build)
        assert client.get("/api/v1/shop/users?limit=3", headers=ADMIN).status_code == 200
        assert _setting(client, "settings.pagination.max_limit")["source"] == "default"


def test_kept_across_a_restart_and_reset(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    with server.client() as client:
        assert _change(client, {"settings.pagination.max_limit": 2}).status_code == 200
    with server.client() as client:
        assert client.get("/api/v1/shop/users?limit=3", headers=ADMIN).status_code == 422
        assert _change(client, reset=["settings.pagination.max_limit"]).status_code == 200
        assert client.get("/api/v1/shop/users?limit=3", headers=ADMIN).status_code == 200


def test_secrets_are_kept_encrypted_and_never_shown(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    with server.client() as client:
        path = "settings.llm.default.api_key"
        assert _change(client, {path: "sk-very-secret"}).status_code == 200
        row = _setting(client, path)
        assert row["value"] is None and row["has_value"] is True and row["secret"]
        assert _change(client, {path: ""}).status_code == 200  # blank keeps it
        assert _setting(client, path)["has_value"] is True
        listed = client.get("/api/v1/settings", headers=ADMIN).text
        assert "sk-very-secret" not in listed
    assert "sk-very-secret" not in (tmp_path / "settings.json").read_text()
    assert "sk-very-secret" not in server.trail.read_text()


def test_the_old_generation_is_closed(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    with server.client() as client:
        first = list(server.factory.created)
        assert _change(client, {"settings.pagination.max_limit": 5}).status_code == 200
        client.get("/health")  # lets the retire task run
        assert all(not g.is_connected for g in first)
        assert client.get("/health").json()["databases"] == {"shop": "connected"}


def test_who_may_change_them_and_the_trail(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    with server.client() as client:
        assert client.get("/api/v1/settings", headers=READER).status_code == 403
        assert client.put("/api/v1/settings", json={}, headers=READER).status_code == 403
        _change(client, {"settings.pagination.max_limit": 4321})
    events = [json.loads(line) for line in server.trail.read_text().splitlines()]
    change = next(e for e in events if e.get("change", "").startswith("settings "))
    assert change["change"] == "settings settings.pagination.max_limit"
    assert "4321" not in json.dumps(change)


def test_without_a_store_nothing_changes(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(
        container_factory=lambda: build_container(settings, gateway_factory=GatewayFactory()),
        env=RuntimeEnv(),
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/settings").json()["available"] is False
        assert _change(client, {"settings.pagination.max_limit": 2}).status_code == 501


@pytest.fixture(autouse=True)
def _no_polling(monkeypatch):
    monkeypatch.setattr("truejoin.adapters.inbound.http.app.CATALOG_POLL_SECONDS", 3600)
