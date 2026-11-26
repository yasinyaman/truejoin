"""GET /audit: the trail for people with the audit permission."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from truejoin.adapters.inbound.http.auth import AuthManager
from truejoin.adapters.inbound.http.routes.audit import create_audit_router
from truejoin.application.settings import ApiKeyConfig, AuthConfig, Settings
from truejoin.bootstrap import build_container


class Reader:
    def __init__(self):
        self.calls = []

    def recent(self, limit, match):
        self.calls.append((limit, match))
        return [{"request_id": "r1"}], False


def _client(reader, auth=True, gaps=None):
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
    app.include_router(create_audit_router(reader, manager, gaps))
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


def test_usage_counts_every_call_not_the_newest_500(tmp_path):
    import json
    from datetime import UTC, datetime, timedelta

    from truejoin.adapters.outbound.audit import JsonlAuditReader

    now = datetime.now(UTC)
    path = tmp_path / "audit.jsonl"
    lines = []
    for i in range(600):
        lines.append(
            {
                "event": "tool_call",
                # The first 100 are two months old.
                "at": (now - timedelta(days=60 if i < 100 else 1, seconds=-i)).isoformat(),
                "tool": "answer_question" if i % 3 else "query_metrics",
                "outcome": "empty" if i % 10 == 0 else "answered",
                "table": "db.orders",
                "measures": ["tutar"],
                "dimensions": [],
                "filtered_columns": ["durum"] if i % 2 else [],
                "glossary": [],
                "request_id": f"r{i}",
            }
        )
        lines.append({"event": "data_access", "at": now.isoformat(), "action": "read"})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
    client = _client(JsonlAuditReader(str(path)))

    assert client.get("/audit/usage", headers={"X-API-Key": "reader-key"}).status_code == 403
    usage = client.get("/audit/usage", headers={"X-API-Key": "dpo-key"}).json()
    assert usage["calls"] == 600
    assert usage["outcomes"] == {
        "answered": 540,
        "empty": 60,
        "refused": 0,
        "failed": 0,
        "error": 0,
    }
    assert usage["tools"] == [["answer_question", 400], ["query_metrics", 200]]
    assert usage["tables"] == [["db.orders", 600]] and usage["filtered_columns"] == [["durum", 300]]
    # The newest misses first, and never more than a page of them.
    assert usage["unanswered"][0]["request_id"] == "r590" and len(usage["unanswered"]) == 60
    assert usage["questions_kept"] is False
    assert "asked" not in usage and "vocabulary" not in usage

    recent = client.get("/audit/usage?days=30", headers={"X-API-Key": "dpo-key"}).json()
    assert recent["calls"] == 500 and recent["days"] == 30


def test_usage_without_a_trail():
    client = _client(None)
    body = client.get("/audit/usage", headers={"X-API-Key": "dpo-key"}).json()
    assert body["available"] is False and "audit.file" in body["detail"]


def _trail(tmp_path, calls):
    import json

    from truejoin.adapters.outbound.audit import JsonlAuditReader

    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(json.dumps({"event": "tool_call", **c}) for c in calls) + "\n")
    return JsonlAuditReader(str(path))


def test_usage_says_which_words_the_vocabulary_does_not_know(tmp_path):
    reader = _trail(
        tmp_path,
        [
            {"tool": "answer_question", "outcome": "empty", "question": "kargo sayısı"},
            {"tool": "answer_question", "outcome": "answered", "question": "kargo sayısı"},
            {"tool": "check_question", "outcome": "answered", "question": "tutar"},
            {"tool": "query_metrics", "outcome": "answered"},
        ],
    )
    seen = []

    def gaps(request, asked):
        seen.append((request.headers["X-API-Key"], asked))
        return {"questions": len(asked), "gaps": []}

    usage = _client(reader, gaps=gaps).get("/audit/usage", headers={"X-API-Key": "dpo-key"}).json()
    # Read as the caller, over every kept question with its calls and misses.
    assert seen == [("dpo-key", {"kargo sayısı": [2, 1], "tutar": [1, 0]})]
    assert usage["vocabulary"] == {"questions": 2, "gaps": []}
    assert usage["questions_kept"] is True and "asked" not in usage


def test_usage_says_why_there_are_no_gaps(tmp_path):
    from truejoin.domain.olap.errors import QueryError

    reader = _trail(tmp_path, [{"tool": "answer_question", "outcome": "empty", "question": "x"}])

    def gaps(request, asked):
        raise QueryError("No certified vocabulary yet")

    usage = _client(reader, auth=False, gaps=gaps).get("/audit/usage").json()
    assert usage["vocabulary"] == {"available": False, "detail": "No certified vocabulary yet"}
    # Without the words of questions there is nothing to look at.
    quiet = _trail(tmp_path, [{"tool": "answer_question", "outcome": "empty"}])
    assert "vocabulary" not in _client(quiet, auth=False, gaps=gaps).get("/audit/usage").json()


def test_sign_ins_and_one_query_fingerprint_can_be_asked_for():
    reader = Reader()
    client = _client(reader, auth=False)
    fingerprint = "0123456789abcdef" * 2
    assert client.get("/audit?event=sign_in&outcome=refused").status_code == 200
    assert client.get(f"/audit?sql_fingerprint={fingerprint}").status_code == 200
    assert reader.calls == [
        (100, {"event": "sign_in", "outcome": "refused"}),
        (100, {"sql_fingerprint": fingerprint}),
    ]
    # A fingerprint is 32 hex characters, nothing else.
    assert client.get("/audit?sql_fingerprint=SELECT%201").status_code == 422
