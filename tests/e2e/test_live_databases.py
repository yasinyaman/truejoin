"""Databases added, changed and removed from the console while the server runs.

The real stack in one process (lifespan, routes, the OLAP loop thread, DuckDB,
MCP) over fake gateways: a database added here must be served everywhere at
once, gone everywhere once removed, and back after a restart.
"""

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tests.e2e.test_one_process import FOREIGN_KEYS, TABLES, _settings
from tests.fusion.fakes import FakeGateway
from truejoin.adapters.inbound.http.app import create_app
from truejoin.application.settings import RuntimeEnv
from truejoin.bootstrap import build_container

SALES_TABLES = {"deals": [{"id": 1, "amount": 10.0}, {"id": 2, "amount": 32.5}]}
SALES = {
    "name": "sales",
    "type": "postgresql",
    "database": "sales",
    "username": "app",
    "password": "s3cret-pass",
}
ADMIN = {"X-API-Key": "admin-key"}


class RefusingGateway(FakeGateway):
    async def connect(self) -> None:
        raise ConnectionError("password authentication failed for PWD=wrong-pass")


class Factory:
    """``shop`` has the shop tables, anything else the sales ones; password ``wrong-pass`` fails."""

    def __init__(self) -> None:
        self.down: set[str] = set()

    def create(self, config):
        cfg = config if isinstance(config, dict) else config.model_dump()
        name = cfg["name"].removesuffix("__readonly")
        tables, keys = (TABLES, FOREIGN_KEYS) if name == "shop" else (SALES_TABLES, {})
        kind = (
            RefusingGateway
            if cfg.get("password") == "wrong-pass" or name in self.down
            else FakeGateway
        )
        return kind(tables, name=cfg["name"], foreign_keys=keys)

    def get_supported_types(self):
        return ["postgresql", "mysql"]


class Server:
    def __init__(self, tmp_path, monkeypatch, auth: bool = True, app_env: str = "development"):
        monkeypatch.setenv("TRUEJOIN_SECRET_KEY", Fernet.generate_key().decode())
        self.factory = Factory()
        core = {"database_store": {"path": str(tmp_path / "databases.json")}}
        if auth:
            core["auth"] = {
                "enabled": True,
                "api_keys": [
                    {"key": "admin-key", "name": "admin", "permissions": ["all"]},
                    {"key": "reader-key", "name": "reader", "permissions": ["read", "query"]},
                ],
            }
            core["audit"] = {"enabled": True}
        self.settings = _settings(tmp_path, settings=core)
        self.env = RuntimeEnv(app_env=app_env)

    def client(self) -> TestClient:
        app = create_app(
            container_factory=lambda: build_container(self.settings, gateway_factory=self.factory),
            env=self.env,
        )
        return TestClient(app)


def _query(client, sql):
    return client.post("/olap/query", json={"sql": sql}, headers=ADMIN)


def _sources(client):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_sources", "arguments": {}},
        },
        headers={"Accept": "application/json, text/event-stream", **ADMIN},
    )
    return json.loads(response.json()["result"]["content"][0]["text"])


def test_added_served_everywhere_changed_removed_and_back_after_restart(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    with server.client() as client:
        added = client.post("/api/v1/databases", json=SALES, headers=ADMIN)
        assert added.status_code == 201, added.text
        assert "s3cret-pass" not in added.text and added.json()["database"]["has_password"]

        assert client.get("/api/v1/sales/deals", headers=ADMIN).json()["total"] == 2
        assert _query(client, "SELECT SUM(amount) AS s FROM sales.deals").json()["rows"] == [
            {"s": 42.5}
        ]
        assert "sales" in json.dumps(_sources(client))
        assert "/api/v1/sales/deals" in client.get("/openapi.json", headers=ADMIN).json()["paths"]
        assert client.get("/api/v1/shop/users", headers=ADMIN).json()["total"] == 3
        # The admin routes still come first: no table route swallows them.
        listed = client.get("/api/v1/databases", headers=ADMIN).json()
        assert [d["state"] for d in listed["databases"]] == ["connected"]
        assert "s3cret-pass" not in json.dumps(listed)

        # A change that cannot connect leaves the old one serving.
        refused = client.put(
            "/api/v1/databases/sales", json={"password": "wrong-pass"}, headers=ADMIN
        )
        assert refused.status_code == 422 and "wrong-pass" not in refused.text
        assert client.get("/api/v1/sales/deals", headers=ADMIN).status_code == 200

        changed = client.put("/api/v1/databases/sales", json={"host": "db2"}, headers=ADMIN)
        assert changed.status_code == 200 and changed.json()["database"]["host"] == "db2"
        assert client.get("/api/v1/sales/deals", headers=ADMIN).status_code == 200

    with server.client() as client:  # a restart: kept, connected again
        assert client.get("/api/v1/sales/deals", headers=ADMIN).json()["total"] == 2
        removed = client.delete("/api/v1/databases/sales", headers=ADMIN)
        assert removed.status_code == 200
        assert client.get("/api/v1/sales/deals", headers=ADMIN).status_code == 404
        assert "error" in _query(client, "SELECT 1 FROM sales.deals").json()
        assert "sales" not in client.get("/health").json()["databases"]

    assert "s3cret-pass" not in (tmp_path / "databases.json").read_text()


def test_one_that_cannot_connect_is_not_kept(tmp_path, monkeypatch):
    with Server(tmp_path, monkeypatch).client() as client:
        refused = client.post(
            "/api/v1/databases", json={**SALES, "password": "wrong-pass"}, headers=ADMIN
        )
        assert refused.status_code == 422 and "wrong-pass" not in refused.text
        # Why, in words, with the driver's own after: what the console reads.
        assert refused.json()["reason"] == "credentials"
        assert "refused the user name or password" in refused.json()["detail"]
        assert "PWD=***" in refused.json()["raw"]
        assert client.get("/api/v1/databases", headers=ADMIN).json()["databases"] == []
        assert client.get("/api/v1/sales/deals", headers=ADMIN).status_code == 404
        tested = client.post("/api/v1/databases/test", json=SALES, headers=ADMIN)
        assert tested.json() == {"ok": True}


def test_an_added_one_that_is_down_does_not_stop_the_start(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    with server.client() as client:
        assert client.post("/api/v1/databases", json=SALES, headers=ADMIN).status_code == 201
    server.factory.down.add("sales")
    monkeypatch.setattr("truejoin.adapters.inbound.http.app.asyncio.sleep", _no_wait)
    with server.client() as client:
        listed = client.get("/api/v1/databases", headers=ADMIN).json()["databases"]
        assert listed[0]["state"] == "not connected" and "wrong-pass" not in listed[0]["error"]
        assert listed[0]["error"].startswith("the database refused the user name or password")
        assert client.get("/api/v1/shop/users", headers=ADMIN).status_code == 200
        server.factory.down.clear()
        assert client.put("/api/v1/databases/sales", json={}, headers=ADMIN).status_code == 200
        assert client.get("/api/v1/sales/deals", headers=ADMIN).status_code == 200


async def _no_wait(_seconds):
    return None


def test_who_may_change_them(tmp_path, monkeypatch):
    with Server(tmp_path, monkeypatch).client() as client:
        reader = {"X-API-Key": "reader-key"}
        assert client.get("/api/v1/databases", headers=reader).status_code == 403
        assert client.post("/api/v1/databases", json=SALES, headers=reader).status_code == 403
        configured = client.delete("/api/v1/databases/shop", headers=ADMIN)
        assert configured.status_code == 409
        reserved = client.post("/api/v1/databases", json={**SALES, "name": "keys"}, headers=ADMIN)
        assert reserved.status_code == 422


def test_without_auth_only_development_may_change_them(tmp_path, monkeypatch):
    with Server(tmp_path, monkeypatch, auth=False, app_env="staging").client() as client:
        assert client.get("/api/v1/databases").json()["may_change"] is False
        assert client.post("/api/v1/databases", json=SALES).status_code == 409


def test_without_a_store_nothing_is_added(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(
        container_factory=lambda: build_container(settings, gateway_factory=Factory()),
        env=RuntimeEnv(),
    )
    with TestClient(app) as client:
        listed = client.get("/api/v1/databases").json()
        assert listed["available"] is False and listed["configured"][0]["name"] == "shop"
        assert client.post("/api/v1/databases", json=SALES).status_code == 501


def test_a_store_without_its_key_stops_the_start(tmp_path, monkeypatch):
    monkeypatch.delenv("TRUEJOIN_SECRET_KEY", raising=False)
    settings = _settings(tmp_path, settings={"database_store": {"path": str(tmp_path / "d.json")}})
    with pytest.raises(Exception, match="TRUEJOIN_SECRET_KEY"):
        build_container(settings, gateway_factory=Factory())


def test_the_audit_trail_names_the_change_not_the_secrets(tmp_path, monkeypatch):
    server = Server(tmp_path, monkeypatch)
    trail = tmp_path / "audit.jsonl"
    server.settings.settings.audit.file = str(trail)
    with server.client() as client:
        client.post("/api/v1/databases", json=SALES, headers=ADMIN)
        client.delete("/api/v1/databases/sales", headers=ADMIN)
    text = trail.read_text()
    assert "add_database sales" in text and "remove_database sales" in text
    assert "s3cret-pass" not in text and '"app"' not in text
