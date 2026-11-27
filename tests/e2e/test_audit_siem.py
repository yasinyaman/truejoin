"""The trail reaches a SIEM: the whole app, a syslog receiver on this machine."""

import json
import socket

from tests.e2e import test_one_process as one


def _receiver() -> socket.socket:
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(5)
    return receiver


def _events(receiver: socket.socket, n: int) -> list[dict]:
    got = []
    while len(got) < n:
        raw, _ = receiver.recvfrom(65536)
        got.append(json.loads(raw.decode().split(" ", 7)[7]))
    return got


def test_a_query_and_a_refused_key_reach_the_receiver(tmp_path, monkeypatch):
    monkeypatch.setenv("TRUEJOIN_SECRET_KEY", "s" * 44)
    receiver = _receiver()
    audit = {
        "enabled": True,
        "file": str(tmp_path / "audit.jsonl"),
        "sql_fingerprint": True,
        "syslog": {"host": "127.0.0.1", "port": receiver.getsockname()[1]},
    }
    auth = {"enabled": True, "api_keys": one.KEYS}
    running = one.Running(tmp_path, settings={"auth": auth, "audit": audit})
    try:
        with running as client:
            sql = {"sql": "SELECT COUNT(*) AS n FROM shop.users WHERE id > 1"}
            ok = client.post("/olap/query", json=sql, headers={"X-API-Key": "analyst-key"})
            assert ok.status_code == 200, ok.text
            refused = client.get("/olap/sources", headers={"X-API-Key": "nope"})
            assert refused.status_code == 401
            events = _events(receiver, 2)
    finally:
        receiver.close()
    query = next(e for e in events if e["event"] == "data_access")
    sign_in = next(e for e in events if e["event"] == "sign_in")
    assert query["actor"] == "analyst" and len(query["sql_fingerprint"]) == 32
    assert (sign_in["method"], sign_in["outcome"]) == ("key", "refused")
    assert "nope" not in json.dumps(sign_in) and "SELECT" not in json.dumps(query)
    # The file has the same events, fingerprint included.
    trail = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(e.get("sql_fingerprint") == query["sql_fingerprint"] for e in trail)
