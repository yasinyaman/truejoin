"""Tests for the OLAP REST routes (auth, CORS and request ids belong to the host app)."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.fusion.rest_app import create_app
from truejoin import __version__
from truejoin.adapters.inbound.rest.rate_limit import rate_limit_key
from truejoin.adapters.inbound.rest.routes import handle_result
from truejoin.application.olap.settings import Settings


@pytest.fixture
def client(app_with_data):
    return TestClient(create_app(app_with_data))


def _client_with(app, **overrides):
    return TestClient(create_app(app, replace(app.settings, **overrides)))


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "healthy",
            "version": __version__,
            "environment": "development",
        }

    def test_readiness_ready(self, client):
        resp = client.get("/readiness")
        assert resp.status_code == 200
        assert resp.json()["sources"] == 1

    def test_readiness_without_sources(self, app):
        resp = TestClient(create_app(app)).get("/readiness")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"


class TestTools:
    def test_list_tools(self, client):
        data = client.get("/tools").json()
        assert data["count"] == 18
        assert {t["name"] for t in data["tools"]} >= {"query_data", "list_sources", "cache_stats"}

    def test_dispatch(self, client):
        assert "sources" in client.post("/tools/list_sources", json={}).json()
        resp = client.post("/tools/describe_table", json={"table": "test_db.users"})
        assert resp.json()["table"] == "test_db.users"
        assert client.post("/tools/list_sources").status_code == 200  # no body

    def test_dispatch_errors(self, client):
        assert client.post("/tools/nonexistent_tool", json={}).status_code == 404
        assert client.post("/tools/query_data", json={"sql": "DROP TABLE x"}).status_code == 403
        assert client.post("/tools/describe_table", json={}).status_code == 400
        resp = client.post("/tools/query_data", json={"sql": "SELECT 1; SELECT 2"})
        assert resp.status_code == 403

    def test_handle_result_passthrough(self):
        assert handle_result({"rows": []}) == {"rows": []}


class TestConvenienceRoutes:
    def test_sources_and_schema(self, client):
        assert client.get("/sources").json()["sources"][0]["source"] == "test_db"
        data = client.get("/tables/test_db.users/schema").json()
        assert data["table"] == "test_db.users"
        assert len(data["columns"]) == 3
        assert client.get("/tables/test_db.nope/schema").status_code == 400

    def test_query(self, client):
        resp = client.post("/query", json={"sql": "SELECT COUNT(*) AS cnt FROM test_db.users"})
        assert resp.status_code == 200
        assert resp.json()["rows"][0]["cnt"] == 5

    def test_query_guardrail_violation(self, client):
        resp = client.post("/query", json={"sql": "DROP TABLE test_db.users"})
        assert resp.status_code == 403
        assert "error" in resp.json()

    def test_query_union(self, client):
        resp = client.post(
            "/query",
            json={"sql": "SELECT 1 AS x UNION SELECT 2 AS x ORDER BY x"},
        )
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 2

    def test_query_bad_sql_is_400(self, client):
        assert client.post("/query", json={"sql": "SELECT * FROM nope"}).status_code == 400

    def test_search_and_aggregate(self, client):
        resp = client.post(
            "/search",
            json={"table": "test_db.users", "filter_column": "name", "filter_value": "Bob"},
        )
        assert resp.json()["row_count"] == 1
        resp = client.post(
            "/aggregate",
            json={
                "table": "test_db.orders",
                "group_by": "product",
                "agg_column": "amount",
                "agg_func": "SUM",
            },
        )
        assert resp.json()["rows"][0]["product"] == "A"

    def test_views(self, client):
        assert client.get("/views").json() == {"views": []}
        resp = client.post(
            "/views", json={"name": "v", "sql": "SELECT COUNT(*) AS cnt FROM test_db.users"}
        )
        assert resp.json()["status"] == "created"
        assert client.post("/views/v/refresh").json()["status"] == "refreshed"
        assert client.post("/views/nope/refresh").status_code == 400
        assert len(client.get("/views").json()["views"]) == 1

    def test_load_table_and_cache(self, app_lazy):
        c = TestClient(create_app(app_lazy))
        assert c.post("/tables/warp_main.users/load").json()["status"] == "loaded"
        assert c.post("/tables/warp_main.users/load").json()["status"] == "already_loaded"
        assert isinstance(c.get("/cache/stats").json(), dict)


class TestBackupRoutes:
    def test_disabled_backup_is_409(self, client):
        assert client.post("/backup/create").status_code == 409
        assert client.get("/backup/list").json() == {"backups": [], "count": 0}
        assert client.get("/backup/stats").json()["enabled"] is False

    def test_enabled_backup_flow(self, tmp_path, scheduler):
        from truejoin.bootstrap import build_app

        settings = Settings(
            threads=1,
            memory_limit="256MB",
            external_access=True,
            backup_enabled=True,
            backup_path=str(tmp_path / "b"),
        )
        app = build_app(settings, scheduler=scheduler)
        try:
            c = TestClient(create_app(app))
            resp = c.post("/backup/create")
            assert resp.status_code == 200
            assert resp.json()["backup"]["kind"] == "export"
            assert c.get("/backup/list").json()["count"] == 1
        finally:
            app.close()


class TestDebugEndpoint:
    def test_available_outside_production(self, client):
        resp = client.get("/debug/config")
        assert resp.status_code == 200
        assert resp.json()["environment"] == "development"
        assert resp.json()["rate_limit"] == "100/minute"

    def test_hidden_in_production(self, app_with_data):
        c = _client_with(app_with_data, env="production")
        assert c.get("/debug/config").status_code == 404

    def test_force_enabled_in_production(self, app_with_data):
        c = _client_with(app_with_data, env="production", debug_endpoints="true")
        assert c.get("/debug/config").status_code == 200


class TestRateLimit:
    def _req(self, api_key=None, host="9.9.9.9"):
        req = MagicMock()
        req.headers = {"X-API-Key": api_key} if api_key else {}
        req.client.host = host
        return req

    def test_uses_api_key_when_present(self):
        key = rate_limit_key(self._req(api_key="secret-abc"))
        assert key.startswith("key:")
        assert rate_limit_key(self._req(api_key="secret-abc")) == key
        assert "secret-abc" not in key

    def test_distinguishes_api_keys(self):
        assert rate_limit_key(self._req(api_key="aaa")) != rate_limit_key(self._req(api_key="bbb"))

    def test_falls_back_to_client_ip(self):
        assert rate_limit_key(self._req(host="1.2.3.4")) == "1.2.3.4"

    def test_reads_the_configured_header(self):
        req = MagicMock()
        req.headers = {"Authorization-Key": "abc"}
        assert rate_limit_key(req, header="Authorization-Key").startswith("key:")

    def test_limits_are_per_route_and_per_key(self, app_with_data):
        c = _client_with(app_with_data, rate_limit="1/minute")
        assert c.get("/cache/stats").status_code == 200
        assert c.get("/cache/stats").status_code == 429
        assert c.get("/sources").status_code == 200
        assert c.get("/cache/stats", headers={"X-API-Key": "other"}).status_code == 200

    def test_default_limit_enforced(self, app_with_data):
        c = _client_with(app_with_data, rate_limit="3/minute")
        statuses = [c.get("/cache/stats").status_code for _ in range(4)]
        assert statuses[:3] == [200, 200, 200]
        assert statuses[3] == 429


class TestRefreshTable:
    def test_a_held_table_is_read_again(self, app_lazy):
        c = TestClient(create_app(app_lazy))
        assert c.post("/tables/warp_main.users/refresh").json()["how"] == "not_loaded"
        c.post("/tables/warp_main.users/load")
        body = c.post("/tables/warp_main.users/refresh").json()
        assert body["status"] == "refreshed" and body["how"] == "reloaded"
        assert body["row_count"] == 5

    def test_a_table_that_is_not_there_is_a_client_error(self, client):
        assert client.post("/tables/test_db.nope/refresh").status_code == 400
        assert client.post("/tables/nodot/refresh").status_code == 400

    def test_only_an_administrator(self, app_with_data):
        from fastapi import FastAPI

        from truejoin.adapters.inbound.rest import install_error_handlers, olap_router

        app = FastAPI()
        install_error_handlers(app)
        app.include_router(olap_router(app_with_data, is_admin=lambda request: False))
        response = TestClient(app).post("/tables/test_db.users/refresh")
        assert response.status_code == 403 and "'admin' required" in response.text


class TestLoadTableBody:
    def test_load_without_a_body_loads_the_whole_table(self, client):
        response = client.post("/tables/test_db.users/load")
        assert response.status_code == 200
        assert response.json()["status"] in ("loaded", "already_loaded")

    def test_load_with_a_where_body_loads_a_slice(self, client):
        response = client.post(
            "/tables/test_db.orders/load",
            json={"where": "product = 'A'", "columns": ["id", "product"]},
        )
        body = response.json()
        assert response.status_code == 200
        assert body["status"] == "loaded"
        assert body["slice"] == "columns=id,product where product = 'A'"
        assert body["row_count"] == 3

    def test_an_unpushable_where_is_a_client_error(self, client):
        response = client.post(
            "/tables/test_db.orders/load", json={"where": "product = 'A' OR id = 1"}
        )
        assert response.status_code == 400
        assert "AND of simple conditions" in response.json()["detail"]


class TestAssistant:
    COUNT = "SELECT COUNT(*) AS n FROM test_db.users"

    def client(self, app, *turns, **options):
        from tests.fusion.fakes import ScriptedChat
        from truejoin.application.olap.assistant import AssistantService
        from truejoin.application.olap.tool_schemas import assistant_tools
        from truejoin.domain.olap.assistant import AssistantLimits

        chat = ScriptedChat(*turns)
        router = {k: options.pop(k) for k in ("assistant_rate_limit", "scope_of") if k in options}
        service = AssistantService(
            app.tools,
            chat,
            assistant_tools(),
            provider="scripted",
            model="m1",
            limits=AssistantLimits(**options),
        )
        return TestClient(create_app(replace(app, assistant=service), **router)), chat

    def test_off_it_says_so_and_how_to_turn_it_on(self, client):
        assert client.get("/assistant").json() == {
            "available": False,
            "detail": "The built-in assistant is off (olap.assistant.enabled).",
        }
        refused = client.post("/assistant", json={"question": "How many users?"})
        assert refused.status_code == 409
        assert "olap.assistant.enabled" in refused.json()["detail"]

    def test_on_it_says_which_model_and_tools(self, app_with_data):
        from tests.fusion.fakes import say

        client, _ = self.client(app_with_data, say("ok"))
        status = client.get("/assistant").json()
        assert (status["available"], status["provider"], status["model"]) == (
            True,
            "scripted",
            "m1",
        )
        assert "query_data" in status["tools"] and "load_table" not in status["tools"]
        assert status["masks_everyone"] is False and status["max_steps"] == 8

    def test_an_answer_is_one_document_with_its_steps(self, app_with_data):
        from tests.fusion.fakes import ask, say

        client, _ = self.client(app_with_data, ask("query_data", sql=self.COUNT), say("Two.", 9, 1))
        response = client.post("/assistant", json={"question": " How many users? "})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["outcome"], body["text"], body["turns"]) == ("answered", "Two.", 2)
        assert body["sql"] == [self.COUNT]
        assert body["result"]["columns"] == ["n"] and body["result"]["row_count"] == 1
        (step,) = body["steps"]
        assert (step["tool"], step["outcome"], step["row_count"]) == ("query_data", "answered", 1)
        assert body["usage"] == {"input_tokens": 9, "output_tokens": 1}

    def test_the_stream_gives_each_step_as_it_happens(self, app_with_data):
        import json

        from tests.fusion.fakes import ask, say

        client, _ = self.client(app_with_data, ask("query_data", sql=self.COUNT), say("Two."))
        with client.stream("POST", "/assistant?stream=true", json={"question": "How many?"}) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("application/x-ndjson")
            events = [json.loads(line) for line in r.iter_lines() if line]
        assert [(e["type"], e.get("phase")) for e in events] == [
            ("step", "call"),
            ("step", "result"),
            ("answer", None),
        ]
        assert events[-1]["text"] == "Two." and events[-1]["result"]["columns"] == ["n"]

    def test_the_conversation_reaches_the_model_and_its_name_the_trail(self, app_with_data):
        from tests.fusion.fakes import say

        client, chat = self.client(app_with_data, say("By city: ..."))
        body = {
            "question": "and by city?",
            "history": [{"question": "How many users?", "answer": "Two."}],
            "conversation": "conv_1-a",
        }
        assert client.post("/assistant", json=body).status_code == 200
        assert [(m.role, m.text) for m in chat.sent[1:]] == [
            ("user", "How many users?"),
            ("assistant", "Two."),
            ("user", "and by city?"),
        ]

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"question": "   "},
            {"question": "x" * 4001},
            {"question": "q", "conversation": "has spaces"},
            {"question": "q", "conversation": "x" * 65},
            {"question": "q", "history": [{"question": "only"}]},
            {"question": "q", "history": [{"question": "a", "answer": "b"}] * 51},
        ],
    )
    def test_a_body_that_is_not_a_question_is_refused(self, app_with_data, body):
        from tests.fusion.fakes import say

        client, chat = self.client(app_with_data, say("ok"))
        assert client.post("/assistant", json=body).status_code == 422
        assert chat.calls == []

    def test_a_model_that_fails_or_is_late_is_a_gateway_error(self, app_with_data):
        from truejoin.domain.gateway.errors import LLMGenerationError

        failed, _ = self.client(app_with_data, LLMGenerationError("OpenAI: quota exceeded"))
        response = failed.post("/assistant", json={"question": "q"})
        assert (response.status_code, response.json()["detail"]) == (502, "OpenAI: quota exceeded")
        late, _ = self.client(app_with_data, TimeoutError())
        assert late.post("/assistant", json={"question": "q"}).status_code == 504

    def test_a_failure_in_a_stream_is_its_last_line(self, app_with_data):
        import json

        failed, _ = self.client(app_with_data, RuntimeError("gone"))
        with failed.stream("POST", "/assistant?stream=true", json={"question": "q"}) as r:
            assert r.status_code == 200
            (event,) = [json.loads(line) for line in r.iter_lines() if line]
        assert (event["type"], event["code"]) == ("error", "model")

    def test_it_has_a_limit_of_its_own(self, app_with_data):
        from tests.fusion.fakes import say

        client, _ = self.client(app_with_data, say("ok"), assistant_rate_limit="2/minute")
        codes = [client.post("/assistant", json={"question": "q"}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert client.get("/health").status_code == 200  # the other routes keep theirs

    def test_the_run_is_the_callers(self, app_with_data):
        from tests.fusion.fakes import ask, say
        from truejoin.domain.governance.access import AccessScope
        from truejoin.domain.governance.row_policy import build_policy

        rule = [{"column": "id", "value": 1}]
        scope = AccessScope(actor="one", policy=build_policy({"users": rule}, caller={}))
        client, _ = self.client(
            app_with_data,
            ask("query_data", sql=self.COUNT),
            say("ok"),
            scope_of=lambda request: scope,
        )
        body = client.post("/assistant", json={"question": "How many?"}).json()
        assert body["result"]["rows"] == [{"n": 1}]
