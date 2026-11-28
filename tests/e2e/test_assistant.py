"""The built-in assistant through the whole server: auth, scope, the engine's loop, the trail.

Everything is the real stack over a fake gateway, as in ``test_one_process``;
only the model is a script. What is asked here is what a deployment asks: is a
run the caller's own, in what it reads and in what the trail says of it?
"""

import json

import pytest
from fastapi.testclient import TestClient

from tests.e2e.test_one_process import KEYS, GatewayFactory, Running, _approve, _settings
from tests.fusion.fakes import ScriptedChatModel, ask, say
from truejoin.adapters.inbound.http.app import create_app
from truejoin.application.settings import RuntimeEnv
from truejoin.bootstrap import build_container

SQL = "SELECT id, email FROM shop.users ORDER BY id"
LOCAL = {"default": {"provider": "ollama", "model": "local-model"}}
CLOUD = {"default": {"provider": "openai", "model": "cloud-model", "api_key": "sk-test"}}
RAW = {"a@example.com", "b@example.com", "c@example.com"}


class Assisted:
    """The started server, with a scripted model where the assistant's would be."""

    def __init__(self, tmp_path, *turns, llm=LOCAL, share: bool = False) -> None:
        self.models: list[ScriptedChatModel] = []
        self.trail = tmp_path / "audit.jsonl"
        _approve(tmp_path, {"users": {"email": "email"}})
        core = {
            "auth": {"enabled": True, "api_keys": KEYS},
            "masking": {"enabled": True, "rules": {"email": "partial"}, "exempt_roles": ["dpo"]},
            "audit": {"enabled": True, "file": str(self.trail)},
            "analysis": {"share_samples_with_cloud_llm": share},
            "llm": llm,
        }
        settings = _settings(tmp_path, settings=core, olap={"assistant": {"enabled": True}})
        factory = GatewayFactory()

        def model(settings, slot):
            self.models.append(ScriptedChatModel(*turns))
            return self.models[-1]

        self.app = create_app(
            container_factory=lambda: build_container(
                settings, gateway_factory=factory, chat_model_factory=model
            ),
            env=RuntimeEnv(),
        )

    def __enter__(self) -> TestClient:
        self._client = TestClient(self.app)
        return self._client.__enter__()

    def __exit__(self, *exc) -> None:
        self._client.__exit__(*exc)

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.trail.read_text().splitlines()]

    def told(self) -> str:
        """Everything the model was sent, in every turn."""
        calls = self.models[-1].script.calls
        return json.dumps([m.text for call in calls for m in call["messages"]])


def _ask(client, key: str | None, question: str = "Who are my users?", **more):
    headers = {"X-API-Key": key} if key else {}
    return client.post("/olap/assistant", json={"question": question, **more}, headers=headers)


class TestOffByDefault:
    def test_a_server_that_did_not_turn_it_on_has_none(self, tmp_path):
        with Running(tmp_path) as client:
            assert client.get("/olap/assistant").json()["available"] is False
            refused = client.post("/olap/assistant", json={"question": "How many users?"})
            assert refused.status_code == 409


class TestARunIsTheCallers:
    def test_a_tenants_run_reads_the_tenants_rows_masked(self, tmp_path):
        server = Assisted(tmp_path, ask("query_data", sql=SQL), say("One user."))
        with server as client:
            answer = _ask(client, "tenant-key")
            assert answer.status_code == 200, answer.text
            body = answer.json()
            assert body["result"]["rows"] == [{"id": 2, "email": "b***@example.com"}]
            assert body["result"]["masked"] == ["email"]
            assert "b***@example.com" in server.told()
            assert not any(value in server.told() for value in RAW)

    def test_the_trail_has_the_run_and_its_calls_as_the_callers_and_no_value(self, tmp_path):
        server = Assisted(tmp_path, ask("query_data", sql=SQL), say("One user: b***@example.com."))
        with server as client:
            assert _ask(client, "tenant-key", conversation="conv-1").status_code == 200
        events = [e for e in server.events() if e.get("event") != "sign_in"]
        by_kind = {e["event"]: e for e in events}
        assert set(by_kind) == {"data_access", "tool_call", "assistant_run"}
        assert {e["actor"] for e in events} == {"tenant"}
        assert len({e["request_id"] for e in events}) == 1
        run = by_kind["assistant_run"]
        assert (run["outcome"], run["provider"], run["model"]) == (
            "answered",
            "ollama",
            "local-model",
        )
        assert (run["steps"], run["tool_calls"], run["conversation"]) == (2, 1, "conv-1")
        assert by_kind["tool_call"]["via"] == "assistant"
        assert by_kind["data_access"]["row_filtered"] is True
        assert by_kind["data_access"]["masked_columns"] == ["users.email"]
        trail = server.trail.read_text()
        assert "example.com" not in trail and "SELECT" not in trail and "One user" not in trail

    def test_it_needs_the_query_permission_like_every_analytical_route(self, tmp_path):
        server = Assisted(tmp_path, say("ok"))
        with server as client:
            assert _ask(client, None).status_code == 401
            assert _ask(client, "reader-key").status_code == 403
            assert _ask(client, "analyst-key").status_code == 200
            assert len(server.models[-1].script.calls) == 1  # only the one who may ask reached it

    def test_the_stream_is_the_callers_too(self, tmp_path):
        server = Assisted(tmp_path, ask("query_data", sql=SQL), say("One user."))
        with server as client:
            with client.stream(
                "POST",
                "/olap/assistant?stream=true",
                json={"question": "Who?"},
                headers={"X-API-Key": "tenant-key"},
            ) as response:
                assert response.status_code == 200
                events = [json.loads(line) for line in response.iter_lines() if line]
        assert [e["type"] for e in events] == ["step", "step", "answer"]
        assert events[-1]["result"]["rows"] == [{"id": 2, "email": "b***@example.com"}]


class TestAModelOutsideTheServer:
    def test_an_exempt_role_reads_raw_itself_and_masked_through_the_assistant(self, tmp_path):
        server = Assisted(
            tmp_path, ask("query_data", sql=SQL), say("Three users."), llm=CLOUD, share=True
        )
        dpo = {"X-API-Key": "dpo-key"}
        with server as client:
            own = client.post("/olap/query", json={"sql": SQL}, headers=dpo).json()["rows"]
            assert own[0]["email"] == "a@example.com"
            assert client.get("/olap/assistant", headers=dpo).json()["masks_everyone"] is True
            body = _ask(client, "dpo-key").json()
            assert [row["email"] for row in body["result"]["rows"]] == [
                "a***@example.com",
                "b***@example.com",
                "c***@example.com",
            ]
            assert not any(value in server.told() for value in RAW)
        run = next(e for e in server.events() if e.get("event") == "assistant_run")
        assert (run["actor"], run["roles"], run["provider"]) == ("dpo", ["dpo"], "openai")

    def test_a_model_on_the_server_is_shown_what_the_caller_sees(self, tmp_path):
        server = Assisted(tmp_path, ask("query_data", sql=SQL), say("Three users."))
        with server as client:
            body = _ask(client, "dpo-key").json()
            assert body["result"]["rows"][0]["email"] == "a@example.com"
            assert client.get("/olap/assistant").status_code == 401


class TestTheModelsClient:
    def test_it_is_closed_when_the_server_stops(self, tmp_path):
        server = Assisted(tmp_path, say("ok"))
        with server as client:
            assert _ask(client, "analyst-key").status_code == 200
            (model,) = server.models
            assert model.closed is False
        assert model.closed is True

    @pytest.mark.parametrize("failure", [RuntimeError("the model is gone"), TimeoutError()])
    def test_a_model_that_fails_is_an_answer_about_the_model_not_a_crash(self, tmp_path, failure):
        server = Assisted(tmp_path, failure)
        with server as client:
            response = _ask(client, "analyst-key")
            assert response.status_code in (502, 504)
            assert client.post(
                "/olap/query", json={"sql": SQL}, headers={"X-API-Key": "analyst-key"}
            ).is_success
        run = next(e for e in server.events() if e.get("event") == "assistant_run")
        assert run["outcome"] == "failed" and run["reason"]
