"""The single app end to end: gateway routes, /olap and MCP in one process.

A fake gateway stands in for the database; everything else is the real stack:
Warp's lifespan and auth, the GatewayLoop thread, InProcessSource, DuckDB, the
OLAP router and the MCP streamable-HTTP endpoint.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from tests.fusion.fakes import FakeGateway
from trio.adapters.inbound.http.app import create_app, runtime_of
from trio.adapters.inbound.http.auth import AuthenticatedUser, Permission
from trio.application.settings import ApiKeyConfig, RuntimeEnv, Settings
from trio.bootstrap import build_container, build_repository
from trio.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    TableCatalogEntry,
)

TABLES = {
    "users": [
        {"id": 1, "name": "Alice", "segment": "premium", "email": "a@example.com"},
        {"id": 2, "name": "Bob", "segment": "basic", "email": "b@example.com"},
        {"id": 3, "name": "Cara", "segment": "premium", "email": "c@example.com"},
    ],
    "orders": [
        {"id": 1, "user_id": 1, "amount": 5.0},
        {"id": 2, "user_id": 2, "amount": 7.5},
        {"id": 3, "user_id": 1, "amount": 2.5},
    ],
}

KEYS = [
    {"key": "analyst-key", "name": "analyst", "permissions": ["read", "query"]},
    {"key": "reader-key", "name": "reader", "permissions": ["read"]},
    {
        "key": "tenant-key",
        "name": "tenant",
        "permissions": ["all"],
        "row_filters": {"users": [{"column": "segment", "value": "basic"}]},
    },
    {"key": "dpo-key", "name": "dpo", "permissions": ["all"], "roles": ["dpo"]},
]


class GatewayFactory:
    """A fresh gateway per ``create``, as the real factory gives the routes and the OLAP side."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.created: list[FakeGateway] = []

    def create(self, config):
        name = config["name"] if isinstance(config, dict) else config.name
        self.created.append(FakeGateway(TABLES, name=name, delay=self.delay))
        return self.created[-1]

    def get_supported_types(self):
        return ["postgresql"]


def _settings(tmp_path, **blocks) -> Settings:
    database = {"name": "shop", "type": "postgresql", "database": "shop", "username": "u"}
    core = {"catalog": {"storage_path": str(tmp_path / "catalogs")}, **blocks.pop("settings", {})}
    olap = {"threads": 1, "memory_limit": "256MB", **blocks.pop("olap", {})}
    return Settings(databases=[database], settings=core, olap=olap, **blocks)


def _approve(tmp_path, labels: dict[str, dict[str, str]], **blocks) -> None:
    """Store an approved catalog for ``shop`` that labels these columns."""
    tables = {
        table: TableCatalogEntry(
            table_name=table,
            columns=[
                ColumnCatalogEntry(name=column, data_type="text", semantic_type=semantic)
                for column, semantic in columns.items()
            ],
        )
        for table, columns in labels.items()
    }
    catalog = DatabaseCatalog(database_name="shop", tables=tables, status=CatalogStatus.approved)
    build_repository(_settings(tmp_path, **blocks)).save(catalog)


class Running:
    """The started app, its client and what the lifespan built."""

    def __init__(self, tmp_path, delay: float = 0.0, **blocks) -> None:
        self.factory = GatewayFactory(delay)
        settings = _settings(tmp_path, **blocks)
        self.app = create_app(
            container_factory=lambda: build_container(settings, gateway_factory=self.factory),
            env=RuntimeEnv(),
        )

    def __enter__(self) -> TestClient:
        self._client = TestClient(self.app)
        return self._client.__enter__()

    def __exit__(self, *exc) -> None:
        self._client.__exit__(*exc)


@pytest.fixture
def running(tmp_path):
    return Running(tmp_path)


@pytest.fixture
def client(running):
    with running as c:
        yield c


def _mcp(client, method: str, params: dict | None = None, headers: dict | None = None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def _tool(client, name: str, arguments: dict, headers: dict | None = None) -> dict:
    response = _mcp(client, "tools/call", {"name": name, "arguments": arguments}, headers)
    assert response.status_code == 200, response.text
    return json.loads(response.json()["result"]["content"][0]["text"])


class TestOneProcess:
    def test_sql_through_olap_query(self, client, running):
        response = client.post(
            "/olap/query",
            json={
                "sql": "SELECT segment, COUNT(*) AS n FROM shop.users GROUP BY segment ORDER BY 1"
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [
            {"segment": "basic", "n": 1},
            {"segment": "premium", "n": 2},
        ]
        # Raw SQL is off by default, so the rows came through a slice, not pushdown.
        routes_gateway, olap_gateway = running.factory.created
        assert olap_gateway.queries == [] and olap_gateway.stream_calls
        assert routes_gateway.stream_calls == []

    def test_sql_through_mcp_streamable_http(self, client):
        result = _tool(
            client, "query_data", {"sql": "SELECT SUM(amount) AS total FROM shop.orders"}
        )
        assert result["rows"] == [{"total": 15.0}]

    def test_the_gateway_schema_route(self, client):
        response = client.get("/api/v1/shop/schema")
        assert response.status_code == 200
        tables = response.json()["tables"]
        assert set(tables) == {"users", "orders"}
        assert tables["users"]["row_estimate"] == 3

    def test_a_join_across_tables_the_gateway_could_not_run(self, client):
        sql = (
            "SELECT u.name, SUM(o.amount) AS total FROM shop.users u "
            "JOIN shop.orders o ON o.user_id = u.id GROUP BY u.name ORDER BY u.name"
        )
        rows = client.post("/olap/query", json={"sql": sql}).json()["rows"]
        assert rows == [{"name": "Alice", "total": 7.5}, {"name": "Bob", "total": 7.5}]

    def test_one_health_for_the_whole_process(self, client):
        assert client.get("/health").json()["databases"] == {"shop": "connected"}
        assert client.get("/olap/readiness").json()["sources"] == 1
        assert client.get("/olap/sources").json()["sources"][0]["source"] == "shop"

    def test_engine_errors_keep_their_status_codes(self, client):
        assert client.post("/olap/query", json={"sql": "DROP TABLE shop.users"}).status_code == 403
        assert client.post("/olap/query", json={"sql": "SELECT * FROM nope"}).status_code == 400
        assert client.post("/olap/backup/create").status_code == 409

    def test_requests_carry_the_gateway_request_id(self, client):
        response = client.get("/olap/sources", headers={"X-Request-ID": "abc-123"})
        assert response.headers["X-Request-ID"] == "abc-123"

    def test_shutdown_closes_both_sides_and_the_loop_thread(self, running):
        def loops() -> int:
            return sum(t.name == "trio-gateway-loop" for t in threading.enumerate())

        before = loops()
        with running:
            assert loops() == before + 1
            assert all(g.is_connected for g in running.factory.created)
        assert loops() == before
        assert len(running.factory.created) == 2
        assert not any(g.is_connected for g in running.factory.created)

    def test_raw_query_enables_pushdown_on_the_olap_gateway(self, tmp_path):
        running = Running(tmp_path, settings={"enable_raw_query": True})
        with running as c:
            sql = "SELECT COUNT(*) AS count FROM shop.users"
            assert c.post("/olap/query", json={"sql": sql}).json()["rows"] == [{"count": 3}]
        assert running.factory.created[1].queries == ["SELECT COUNT(*) AS count FROM users"]

    def test_the_olap_side_uses_the_read_only_credentials(self, tmp_path):
        running = Running(tmp_path)
        settings = _settings(tmp_path)
        settings.databases[0].readonly_username = "ro"
        container = build_container(settings, gateway_factory=running.factory)
        with TestClient(create_app(container_factory=lambda: container, env=RuntimeEnv())):
            assert [g.name for g in running.factory.created] == ["shop", "shop__readonly"]

    def test_backups_are_scheduled_by_the_lifespan(self, tmp_path):
        olap = {"backup_enabled": True, "external_access": True, "backup_path": str(tmp_path / "b")}
        running = Running(tmp_path, olap=olap)
        with running as c:
            fusion = runtime_of(running.app).fusion
            assert fusion.backup.running
            assert c.post("/olap/backup/create").status_code == 200
        assert not fusion.backup.running

    def test_olap_can_be_switched_off(self, tmp_path):
        running = Running(tmp_path, olap={"enabled": False})
        with running as c:
            assert c.post("/olap/query", json={"sql": "SELECT 1"}).status_code == 404
            assert _mcp(c, "tools/list").status_code == 404
            assert c.get("/api/v1/shop/schema").status_code == 200
        assert len(running.factory.created) == 1

    def test_a_database_name_that_is_no_schema_name_is_left_out(self, tmp_path, caplog):
        settings = _settings(tmp_path)
        settings.databases[0].name = "shop-eu"
        container = build_container(settings, gateway_factory=GatewayFactory())
        with TestClient(create_app(container_factory=lambda: container, env=RuntimeEnv())) as c:
            assert c.get("/api/v1/shop-eu/schema").status_code == 200
            assert c.get("/olap/readiness").status_code == 503
        assert "'shop-eu' is not offered to OLAP" in caplog.text


class TestConcurrency:
    def test_32_concurrent_olap_queries_against_a_slow_gateway(self, tmp_path):
        """Threadpool handlers all wait on the one loop thread; nothing may wait back."""
        with Running(tmp_path, delay=0.01) as c:

            def query(i: int) -> tuple[int, list]:
                table, column = ("users", "id") if i % 2 else ("orders", "user_id")
                sql = f"SELECT COUNT(*) AS n FROM shop.{table} WHERE {column} >= {i % 4}"
                response = c.post("/olap/query", json={"sql": sql})
                return response.status_code, response.json().get("rows")

            with ThreadPoolExecutor(max_workers=32) as pool:
                results = list(pool.map(query, range(32), timeout=60))
        assert [status for status, _ in results] == [200] * 32
        expected = {0: 3, 1: 3, 2: 1, 3: 1}  # orders.user_id >= 0|2, users.id >= 1|3
        assert [rows[0]["n"] for _, rows in results] == [expected[i % 4] for i in range(32)]


class TestOneAuth:
    """The gateway's AuthManager is the only authenticator, for /olap and MCP alike."""

    @pytest.fixture
    def client(self, tmp_path):
        auth = {"enabled": True, "api_keys": KEYS}
        with Running(tmp_path, settings={"auth": auth}) as c:
            yield c

    @pytest.mark.parametrize(
        ("key", "status"),
        [
            (None, 401),
            ("wrong", 401),
            ("reader-key", 403),
            ("tenant-key", 200),
            ("analyst-key", 200),
        ],
    )
    def test_olap_needs_the_query_permission(self, client, key, status):
        headers = {"X-API-Key": key} if key else {}
        assert client.get("/olap/sources", headers=headers).status_code == status
        assert _mcp(client, "tools/list", headers=headers).status_code == status

    def test_a_key_with_row_rules_still_cannot_run_raw_sql_on_the_gateway(self, client):
        assert not AuthenticatedUser(ApiKeyConfig(**KEYS[2])).has_permission(Permission.QUERY)

    def test_mcp_runs_a_query_for_an_allowed_key(self, client):
        result = _tool(
            client,
            "query_data",
            {"sql": "SELECT COUNT(*) AS n FROM shop.orders"},
            {"X-API-Key": "analyst-key"},
        )
        assert result["rows"] == [{"n": 3}]


class TestGovernance:
    """Row rules, masks and the audit trail on every analytical surface."""

    SQL = "SELECT id, email FROM shop.users ORDER BY id"

    @pytest.fixture
    def core(self, tmp_path):
        return {
            "auth": {"enabled": True, "api_keys": KEYS},
            "masking": {"enabled": True, "rules": {"email": "partial"}, "exempt_roles": ["dpo"]},
            "audit": {"enabled": True, "file": str(tmp_path / "audit.jsonl")},
        }

    @pytest.fixture
    def client(self, tmp_path, core):
        _approve(tmp_path, {"users": {"email": "email"}})
        with Running(tmp_path, settings=core) as c:
            yield c

    def _query(self, client, key: str, sql: str = SQL) -> list[dict]:
        response = client.post("/olap/query", json={"sql": sql}, headers={"X-API-Key": key})
        assert response.status_code == 200, response.text
        return response.json()["rows"]

    def test_rest_serves_the_tenants_rows_masked(self, client):
        assert self._query(client, "tenant-key") == [{"id": 2, "email": "b***@example.com"}]

    def test_mcp_serves_the_tenants_rows_masked(self, client):
        result = _tool(client, "query_data", {"sql": self.SQL}, {"X-API-Key": "tenant-key"})
        assert result["rows"] == [{"id": 2, "email": "b***@example.com"}]

    def test_metrics_are_measured_over_the_tenants_rows(self, client):
        arguments = {"table": "shop.users", "metrics": ["*:count"]}
        tenant = _tool(client, "query_metrics", arguments, {"X-API-Key": "tenant-key"})
        everyone = _tool(client, "query_metrics", arguments, {"X-API-Key": "dpo-key"})
        assert [list(r.values()) for r in tenant["rows"]] == [[1]]
        assert [list(r.values()) for r in everyone["rows"]] == [[3]]

    def test_one_callers_cached_answer_is_not_anothers(self, client):
        assert len(self._query(client, "dpo-key")) == 3
        assert len(self._query(client, "tenant-key")) == 1
        assert self._query(client, "analyst-key")[0]["email"] == "a***@example.com"
        assert self._query(client, "dpo-key")[0]["email"] == "a@example.com"

    def test_a_masked_value_does_not_come_out_through_an_aggregate(self, client):
        sql = "SELECT MAX(email) AS m, STRING_AGG(email, ',') AS s FROM shop.users"
        assert "a@example.com" not in str(self._query(client, "analyst-key", sql))

    def test_every_query_leaves_one_audit_event(self, client, tmp_path):
        self._query(client, "tenant-key")
        _tool(client, "query_data", {"sql": self.SQL}, {"X-API-Key": "dpo-key"})
        events = [
            json.loads(line)
            for line in (tmp_path / "audit.jsonl").read_text().splitlines()
            if json.loads(line)["action"] == "query"
        ]
        assert [(e["actor"], e["row_filtered"], e["masked_columns"]) for e in events] == [
            ("tenant", True, ["users.email"]),
            ("dpo", False, []),
        ]
        assert all(e["request_id"] and e["table"] == "users" for e in events)

    def test_a_restricted_caller_gets_no_backup_and_no_view(self, client):
        headers = {"X-API-Key": "tenant-key"}
        assert client.post("/olap/backup/create", headers=headers).status_code == 403
        view = {"name": "everything", "sql": "SELECT * FROM shop.users"}
        assert client.post("/olap/views", json=view, headers=headers).status_code == 403

    def test_without_auth_nobody_is_identified_so_everybody_is_masked(self, tmp_path):
        _approve(tmp_path, {"users": {"email": "email"}})
        masking = {"enabled": True, "rules": {"email": "redact"}}
        with Running(tmp_path, settings={"masking": masking}) as c:
            rows = c.post("/olap/query", json={"sql": self.SQL}).json()["rows"]
            assert {r["email"] for r in rows} == {"***"}

    def test_readiness_asks_for_an_approved_catalog(self, client, tmp_path):
        headers = {"X-API-Key": "dpo-key"}
        assert _tool(client, "check_readiness", {}, headers)["ready"]
        with Running(tmp_path / "other") as c:
            result = _tool(c, "check_readiness", {})
            assert not result["ready"]
            assert result["checks"][-1]["detail"] == "no approved catalog for: shop"


class TestMcpToolSurface:
    def _names(self, client) -> set[str]:
        return {tool["name"] for tool in _mcp(client, "tools/list").json()["result"]["tools"]}

    def test_the_analyst_profile_leaves_out_the_admin_tools(self, client):
        names = self._names(client)
        assert len(names) == 10
        assert names.isdisjoint({"create_view", "refresh_view", "cache_stats"})

    def test_the_admin_profile_has_all_thirteen(self, tmp_path):
        with Running(tmp_path, mcp={"profile": "admin"}) as c:
            assert len(self._names(c)) == 13

    def test_an_allow_list_narrows_it_further(self, tmp_path):
        with Running(tmp_path, mcp={"tools": ["query_data", "list_sources"]}) as c:
            assert self._names(c) == {"query_data", "list_sources"}
            refused = _mcp(c, "tools/call", {"name": "load_table", "arguments": {"table": "x"}})
            assert "load_table" in refused.text and "rows" not in refused.text

    def test_the_endpoint_moves_with_the_config(self, tmp_path):
        with Running(tmp_path, mcp={"http_path": "/tools/mcp"}) as c:
            assert _mcp(c, "tools/list").status_code == 404
            response = c.post(
                "/tools/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert response.status_code == 200

    @pytest.mark.parametrize(
        ("mcp", "message"),
        [
            ({"tools": ["drop_everything"]}, "unknown tools: drop_everything"),
            ({"tools": ["create_view"]}, "'analyst' profile does not offer"),
        ],
    )
    def test_a_tool_list_that_cannot_be_honoured_refuses_to_load(self, tmp_path, mcp, message):
        with pytest.raises(ValueError, match=message):
            _settings(tmp_path, mcp=mcp)
