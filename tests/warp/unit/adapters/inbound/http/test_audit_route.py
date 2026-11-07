"""GET /audit: the trail for people with the audit permission."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trio.adapters.inbound.http.auth import AuthManager
from trio.adapters.inbound.http.routes.audit import create_audit_router
from trio.application.settings import ApiKeyConfig, AuthConfig, Settings
from trio.bootstrap import build_container


class Reader:
    def __init__(self):
        self.calls = []

    def recent(self, limit, match):
        self.calls.append((limit, match))
        return [{"request_id": "r1"}], False


def _client(reader, auth=True):
    manager = AuthManager(
        AuthConfig(
            enabled=auth,
            api_keys=[
                ApiKeyConfig(key="reader-key", name="reader", permissions=["read", "update"]),
                ApiKeyConfig(key="dpo-key", name="dpo", permissions=["audit"]),
                ApiKeyConfig(key="admin-key", name="admin", permissions=["all"]),
            ],
        )
    )
    app = FastAPI()
    app.include_router(create_audit_router(reader, manager))
    return TestClient(app)


def test_it_needs_the_audit_permission():
    client = _client(Reader())
    assert client.get("/audit").status_code == 401
    assert client.get("/audit", headers={"X-API-Key": "reader-key"}).status_code == 403
    assert client.get("/audit", headers={"X-API-Key": "dpo-key"}).status_code == 200
    assert client.get("/audit", headers={"X-API-Key": "admin-key"}).status_code == 200


def test_filters_reach_the_reader_and_empty_ones_do_not():
    reader = Reader()
    body = (
        _client(reader, auth=False)
        .get("/audit?limit=5&actor=dpo&table=&restricted=true&event=catalog_change")
        .json()
    )
    assert body == {"available": True, "events": [{"request_id": "r1"}], "complete": False}
    assert reader.calls == [(5, {"actor": "dpo", "restricted": "True", "event": "catalog_change"})]


def test_bad_filters_are_refused():
    client = _client(Reader(), auth=False)
    assert client.get("/audit?limit=0").status_code == 422
    assert client.get("/audit?event=other").status_code == 422


def test_without_a_trail_it_says_how_to_keep_one():
    body = _client(None, auth=False).get("/audit").json()
    assert body["available"] is False and "audit.file" in body["detail"]


def test_the_reader_is_wired_only_when_there_is_a_file(tmp_path):
    assert build_container(Settings()).audit_reader is None
    on = Settings(settings={"audit": {"enabled": True, "file": str(tmp_path / "a.jsonl")}})
    assert build_container(on).audit_reader is not None


def test_usage_records_are_filtered_by_tool_and_outcome():
    reader = Reader()
    client = _client(reader, auth=False)
    params = {"event": "tool_call", "tool": "answer_question", "outcome": "empty"}
    assert client.get("/audit", params=params).status_code == 200
    assert reader.calls[-1][1] == params
    assert client.get("/audit", params={"outcome": "maybe"}).status_code == 422
