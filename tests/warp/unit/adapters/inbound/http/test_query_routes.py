"""Tests for the raw SQL query validator and endpoint."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from truejoin.adapters.inbound.http.routes.query import QueryValidator, create_query_router


class TestQueryValidator:
    def test_allows_simple_select(self):
        v = QueryValidator(["SELECT"])
        assert v.validate("SELECT * FROM users WHERE id = :id") is True

    def test_allows_single_trailing_semicolon(self):
        v = QueryValidator(["SELECT"])
        assert v.validate("SELECT 1;") is True
        assert v.validate("SELECT 1 ;  ") is True

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM users; SELECT * FROM secrets",
            "SELECT 1; DROP TABLE users",
            "SELECT 1;DELETE FROM users",
            "SELECT 1; SELECT 2;",
        ],
    )
    def test_rejects_multiple_statements(self, query):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError, match="Multiple SQL statements"):
            v.validate(query)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * FROM users -- comment",
            "SELECT /* x */ 1",
            "DROP TABLE users",
            "DELETE FROM users",
        ],
    )
    def test_rejects_dangerous_or_non_whitelisted(self, query):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError):
            v.validate(query)

    def test_enforces_whitelist_start(self):
        v = QueryValidator(["SELECT"])
        with pytest.raises(ValueError, match="must start with"):
            v.validate("UPDATE users SET x = 1")

    def test_whitelist_is_configurable(self):
        v = QueryValidator(["SELECT", "INSERT"])
        assert v.validate("INSERT INTO t (a) VALUES (:a)") is True


class _DB:
    """Minimal adapter for the query endpoint."""

    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error

    async def execute_query(self, query, params=None):
        if self._error:
            raise self._error
        return self._rows


def _client(db, enabled=True, whitelist=None):
    app = FastAPI()
    app.include_router(
        create_query_router(db=db, whitelist=whitelist or ["SELECT"], enabled=enabled)
    )
    return TestClient(app)


class TestQueryEndpoint:
    def test_execute_select(self):
        client = _client(_DB(rows=[{"id": 1}]))
        r = client.post("/query/execute", json={"query": "SELECT * FROM users"})
        assert r.status_code == 200
        assert r.json()["row_count"] == 1

    def test_disabled_returns_403(self):
        client = _client(_DB(), enabled=False)
        r = client.post("/query/execute", json={"query": "SELECT 1"})
        assert r.status_code == 403

    def test_multi_statement_rejected(self):
        client = _client(_DB())
        r = client.post("/query/execute", json={"query": "SELECT 1; DROP TABLE users"})
        assert r.status_code == 400

    def test_non_whitelisted_rejected(self):
        client = _client(_DB())
        r = client.post("/query/execute", json={"query": "DELETE FROM users"})
        assert r.status_code == 400

    def test_db_error_does_not_leak_details(self):
        client = _client(_DB(error=RuntimeError("relation secret_table does not exist")))
        r = client.post("/query/execute", json={"query": "SELECT * FROM x"})
        assert r.status_code == 500
        assert "secret_table" not in r.text
        assert r.json()["detail"] == "Query execution failed"

    def test_allowed_commands_endpoint(self):
        client = _client(_DB(), whitelist=["SELECT", "INSERT"])
        r = client.get("/query/allowed-commands")
        assert r.status_code == 200
        assert "SELECT" in r.json()["allowed_commands"]


class TestParameterBindingErrors:
    def test_missing_named_param_is_400(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from truejoin.adapters.inbound.http.routes.query import create_query_router

        class _RaisingDB:
            async def execute_query(self, query, params=None):
                # What the real adapters raise when a :name has no value.
                raise ValueError("Missing value for query parameter :status")

        app = FastAPI()
        app.include_router(create_query_router(db=_RaisingDB(), enabled=True))
        r = TestClient(app).post(
            "/query/execute", json={"query": "SELECT * FROM users WHERE status = :status"}
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "Missing value for query parameter :status"


class TestMaskedCallers:
    """SQL sent straight to the database cannot be masked, so a masked caller is refused."""

    def _client(self, exempt_roles=()):
        from truejoin.domain.governance.masking import CatalogMasking, MaskingPolicy

        masking = CatalogMasking(
            policy=MaskingPolicy(rules={"email": "redact"}, exempt_roles=exempt_roles),
            semantic_types={"users": {"email": "email"}},
        )
        app = FastAPI()
        app.include_router(create_query_router(db=_DB(rows=[{"id": 1}]), masking=masking))
        return TestClient(app)

    def test_a_caller_the_table_routes_would_mask_is_sent_to_olap(self):
        r = self._client().post("/query/execute", json={"query": "SELECT email FROM users"})
        assert r.status_code == 403
        assert "/olap/query" in r.json()["detail"]

    def test_nothing_to_mask_means_nothing_to_refuse(self):
        from truejoin.domain.governance.masking import NO_MASKING

        app = FastAPI()
        app.include_router(create_query_router(db=_DB(rows=[{"id": 1}]), masking=NO_MASKING))
        r = TestClient(app).post("/query/execute", json={"query": "SELECT 1"})
        assert r.status_code == 200


class TestAudit:
    """One event per request: who and which tables, never the SQL or its values."""

    def _run(self, query, db=None, params=None, whitelist=("SELECT",), fingerprint=None):
        from truejoin.adapters.outbound.sqlglot_policy import SqlglotAnalyzer

        events = []

        class _Sink:
            def record(self, event):
                events.append(event)

        app = FastAPI()
        app.include_router(
            create_query_router(
                db=db or _DB(rows=[{"id": 1}, {"id": 2}]),
                whitelist=list(whitelist),
                audit=_Sink(),
                db_name="shop",
                analyzer=SqlglotAnalyzer(),
                fingerprint=fingerprint,
            )
        )
        response = TestClient(app).post("/query/execute", json={"query": query, "params": params})
        return response, events

    def test_the_fingerprint_rides_on_the_event_when_it_is_on(self):
        _, [plain] = self._run("SELECT * FROM users")
        assert plain.sql_fingerprint == ""
        _, [printed] = self._run("SELECT * FROM users", fingerprint=lambda sql: "d" * 32)
        assert printed.as_dict()["sql_fingerprint"] == "d" * 32

    def test_a_read_names_its_tables_and_row_count(self):
        response, [event] = self._run(
            "SELECT * FROM users u JOIN orders o ON o.user_id = u.id WHERE u.status = :s",
            params={"s": "secret-value"},
        )
        assert response.status_code == 200
        assert (event.action, event.database, event.table) == ("query", "shop", "users,orders")
        assert (event.status, event.row_count) == (200, 2)
        dumped = str(event.as_dict())
        assert "secret-value" not in dumped and "SELECT" not in dumped

    def test_refusals_and_failures_are_recorded_with_their_status(self):
        assert self._run("DELETE FROM users")[1][0].status == 400
        assert self._run("SELECT 1", db=_DB(error=RuntimeError("boom")))[1][0].status == 500

    def test_a_whitelisted_write_is_recorded_as_one(self):
        _, [event] = self._run("UPDATE users SET a = :a", whitelist=("SELECT", "UPDATE"))
        assert (event.action, event.table) == ("update", "users")
