"""Keys handed out from the console: made once, kept as digests, revoked for good."""

import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from truejoin.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from truejoin.adapters.inbound.http.routes.keys import create_keys_router
from truejoin.adapters.outbound.key_store.json_file import JsonFileKeyStore
from truejoin.application.access_keys import AccessKeyService
from truejoin.application.settings import ApiKeyConfig, AuthConfig
from truejoin.domain.governance.keys import AccessKeyError, Grantor, digest

CONFIGURED = [
    ApiKeyConfig(key="root-key", name="root", permissions=["all"]),
    ApiKeyConfig(key="admin-key", name="admin", permissions=["read", "admin"], roles=["ops"]),
    ApiKeyConfig(key="reader-key", name="reader", permissions=["read"]),
    ApiKeyConfig(
        key="tenant-admin-key",
        name="tenant_admin",
        permissions=["read", "admin"],
        tenant="a",
        row_filters={"orders": [{"column": "tenant", "value": "${tenant}"}]},
    ),
]


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    def record(self, event) -> None:
        self.events.append(event)


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 8, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


def build(tmp_path, *, store=True, enabled=True, clock=None, known=None, preview=None):
    keys = (
        AccessKeyService(
            JsonFileKeyStore(tmp_path / "keys.json"),
            reserved=[k.name for k in CONFIGURED],
            **({"clock": clock} if clock else {}),
        )
        if store
        else None
    )
    manager = AuthManager(AuthConfig(enabled=enabled, api_keys=CONFIGURED), keys=keys)
    sink = Sink()
    app = FastAPI()
    app.include_router(create_keys_router(keys, manager, sink, known=known, preview=preview))

    @app.get("/data", dependencies=[Depends(manager.require(Permission.READ))])
    async def data(request: Request) -> dict:
        caller = caller_of(request)
        return {
            "name": caller.name if caller else None,
            "filtered": caller.row_policy.conditions_for("orders") if caller else [],
        }

    return TestClient(app), sink, keys


def as_(key: str) -> dict:
    return {"X-API-Key": key}


def test_a_key_is_shown_once_and_kept_only_as_a_digest(tmp_path):
    client, _, _ = build(tmp_path)
    r = client.post(
        "/keys", json={"name": "etl-job", "permissions": ["read"]}, headers=as_("root-key")
    )
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    secret = r.json()["secret"]
    assert secret.startswith("truejoin_") and len(secret) > 40
    assert r.json()["key"]["hint"] == secret[-4:] and "digest" not in r.json()["key"]

    # It lets its holder in, as itself.
    assert client.get("/data", headers=as_(secret)).json()["name"] == "etl-job"

    # Listed without the secret or its digest, beside the configured keys.
    listed = client.get("/keys", headers=as_("root-key")).json()
    assert [k["name"] for k in listed["keys"]] == ["etl-job"] and listed["keys"][0]["active"]
    assert secret not in json.dumps(listed) and digest(secret) not in json.dumps(listed)
    assert {k["name"] for k in listed["configured"]} == {"root", "admin", "reader", "tenant_admin"}
    assert "root-key" not in json.dumps(listed)

    # On disk: the digest, never the secret, readable by the server's user only.
    path = tmp_path / "keys.json"
    text = path.read_text()
    assert digest(secret) in text and secret not in text
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_revoked_key_is_refused_from_the_next_request(tmp_path):
    client, _, _ = build(tmp_path)
    made = client.post(
        "/keys", json={"name": "bi", "permissions": ["read"]}, headers=as_("root-key")
    ).json()
    assert client.get("/data", headers=as_(made["secret"])).status_code == 200
    gone = client.delete(f"/keys/{made['key']['id']}", headers=as_("root-key"))
    assert gone.status_code == 200 and gone.json()["key"]["revoked_by"] == "root"
    assert client.get("/data", headers=as_(made["secret"])).status_code == 401
    again = client.delete(f"/keys/{made['key']['id']}", headers=as_("root-key"))
    assert again.status_code == 409
    assert client.delete("/keys/nope", headers=as_("root-key")).status_code == 404


def test_nobody_gives_more_than_they_hold(tmp_path):
    client, _, _ = build(tmp_path)

    def make(by: str, **body) -> tuple[int, str]:
        r = client.post("/keys", json={"name": "k", **body}, headers=as_(by))
        return r.status_code, str(r.json().get("detail", ""))

    assert make("admin-key", permissions=["query"]) == (
        422,
        "You cannot give what you do not hold: query",
    )
    assert make("admin-key", permissions=["all"])[0] == 422
    status, detail = make("admin-key", permissions=["read"], roles=["dpo"])
    assert status == 422 and "roles of whoever hands it out (yours: ops)" in detail
    # Not even "all" gives another role (dpo lifts masks), nor leaves one off.
    assert make("root-key", permissions=["read"], roles=["dpo"])[0] == 422
    assert make("admin-key", permissions=["read"], roles=[])[0] == 422
    # Held to row rules: a key without them would be a way out.
    status, detail = make("tenant-admin-key", permissions=["read"])
    assert status == 422 and "row rules" in detail
    assert make("admin-key", permissions=["read", "admin"], roles=["ops"])[0] == 201
    assert make("root-key", permissions=["frobnicate"])[0] == 422


def test_a_key_left_without_roles_carries_its_grantors(tmp_path):
    client, _, _ = build(tmp_path)
    made = client.post(
        "/keys", json={"name": "ops-bot", "permissions": ["read"]}, headers=as_("admin-key")
    ).json()
    assert made["key"]["roles"] == ["ops"]


def test_only_an_admin_manages_keys(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.get("/keys", headers=as_("reader-key")).status_code == 403
    no = client.post(
        "/keys", json={"name": "x", "permissions": ["read"]}, headers=as_("reader-key")
    )
    assert no.status_code == 403
    assert client.get("/keys").status_code == 401


def test_names_are_never_reused(tmp_path):
    client, _, _ = build(tmp_path)

    def make(name: str) -> int:
        body = {"name": name, "permissions": ["read"]}
        return client.post("/keys", json=body, headers=as_("root-key")).status_code

    assert make("reader") == 422  # a configured key's
    assert make("Reader") == 422
    assert make("bi") == 201 and make("bi") == 422
    made = client.get("/keys", headers=as_("root-key")).json()["keys"][0]
    client.delete(f"/keys/{made['id']}", headers=as_("root-key"))
    assert make("bi") == 422  # revoked, but the audit trail still names it
    assert make("has space") == 422 and make("-dash") == 422


def test_a_key_expires(tmp_path):
    clock = Clock()
    client, _, _ = build(tmp_path, clock=clock)
    secret = client.post(
        "/keys",
        json={"name": "trial", "permissions": ["read"], "expires_in_days": 7},
        headers=as_("root-key"),
    ).json()["secret"]
    clock.at += timedelta(days=6, hours=23)
    assert client.get("/data", headers=as_(secret)).status_code == 200
    clock.at += timedelta(hours=1)
    assert client.get("/data", headers=as_(secret)).status_code == 401
    assert client.get("/keys", headers=as_("root-key")).json()["keys"][0]["active"] is False
    too_long = client.post(
        "/keys",
        json={"name": "forever", "permissions": ["read"], "expires_in_days": 99999},
        headers=as_("root-key"),
    )
    assert too_long.status_code == 422


def test_row_rules_are_checked_when_the_key_is_made_and_applied_to_its_holder(tmp_path):
    client, _, _ = build(tmp_path)
    rules = {"orders": [{"column": "tenant", "value": "${tenant}"}]}
    no_tenant = client.post(
        "/keys",
        json={"name": "t", "permissions": ["read"], "row_filters": rules},
        headers=as_("root-key"),
    )
    assert no_tenant.status_code == 422 and "Row rules" in no_tenant.json()["detail"]
    made = client.post(
        "/keys",
        json={"name": "t", "permissions": ["read"], "tenant": "b", "row_filters": rules},
        headers=as_("root-key"),
    ).json()
    seen = client.get("/data", headers=as_(made["secret"])).json()
    assert seen["filtered"] == [["tenant", "eq", "b"]]


def test_a_rule_on_a_table_or_column_nothing_serves_is_refused(tmp_path):
    client, _, keys = build(tmp_path, known=lambda: {"orders": {"id", "tenant"}})
    for rules, said in (
        ({"ordrs": [{"column": "tenant", "value": "a"}]}, "Did you mean 'orders'?"),
        ({"orders": [{"column": "tenent", "value": "a"}]}, "has no column 'tenent'"),
    ):
        refused = client.post(
            "/keys",
            json={"name": "t", "permissions": ["read"], "row_filters": rules},
            headers=as_("root-key"),
        )
        assert refused.status_code == 422 and said in refused.json()["detail"], refused.text
    assert keys.keys() == []  # nothing was made
    made = client.post(
        "/keys",
        json={
            "name": "t",
            "permissions": ["read"],
            "row_filters": {"Orders": [{"column": "TENANT", "value": "a"}]},
        },
        headers=as_("root-key"),
    )
    assert made.status_code == 201


def test_rules_are_previewed_before_a_key_is_made_with_them(tmp_path):
    asked = []

    def preview(request, body, rules):
        asked.append((body.name, body.tenant, rules))
        return [{"table": "db.orders", "visible": 2, "total": 5, "error": ""}]

    client, sink, keys = build(tmp_path, known=lambda: {"orders": {"tenant"}}, preview=preview)
    rules = {"orders": [{"column": "tenant", "value": "${tenant}"}]}
    body = {"name": "bi", "tenant": "a", "row_filters": rules}
    answer = client.post("/keys/preview", json=body, headers=as_("root-key"))
    assert answer.json() == {
        "problems": [],
        "tables": [{"table": "db.orders", "visible": 2, "total": 5, "error": ""}],
    }
    assert asked[0][:2] == ("bi", "a") and asked[0][2]["orders"][0]["column"] == "tenant"
    assert keys.keys() == []  # a preview makes no key
    assert [(e.change, e.action, e.actor) for e in sink.events] == [
        ("preview_row_rules bi", "read", "root")
    ]
    # A template the caller cannot fill is refused as when making the key; a bad name is said.
    assert (
        client.post(
            "/keys/preview", json={**body, "tenant": None}, headers=as_("root-key")
        ).status_code
        == 422
    )
    said = client.post(
        "/keys/preview",
        json={"row_filters": {"order": [{"column": "x", "value": 1}]}},
        headers=as_("root-key"),
    ).json()
    assert said["problems"][0].startswith("no connected table is called 'order'")
    assert said["tables"] == [] and len(asked) == 1  # a table with a problem is not counted
    assert client.post("/keys/preview", json=body, headers=as_("reader-key")).status_code == 403


def test_without_the_analytics_engine_there_is_no_preview(tmp_path):
    client, _, _ = build(tmp_path)
    refused = client.post("/keys/preview", json={"row_filters": {}}, headers=as_("root-key"))
    assert refused.status_code == 501 and "analytics engine" in refused.json()["detail"]


def test_another_process_sees_a_key_the_moment_it_is_written(tmp_path):
    client, _, _ = build(tmp_path)
    other = AccessKeyService(JsonFileKeyStore(tmp_path / "keys.json"))
    assert other.keys() == []
    secret = client.post(
        "/keys", json={"name": "bi", "permissions": ["read"]}, headers=as_("root-key")
    ).json()["secret"]
    caller = other.caller_for(secret)
    assert caller is not None and caller.name == "bi"
    assert other.caller_for(secret + "x") is None


def test_handing_out_and_revoking_are_audited_by_name_only(tmp_path):
    client, sink, _ = build(tmp_path)
    made = client.post(
        "/keys", json={"name": "bi", "permissions": ["read"]}, headers=as_("root-key")
    ).json()
    client.delete(f"/keys/{made['key']['id']}", headers=as_("root-key"))
    assert [(e.change, e.subject, e.actor) for e in sink.events] == [
        ("create_key bi", "access", "root"),
        ("revoke_key bi", "access", "root"),
    ]
    assert sink.events[0].as_dict()["event"] == "access_change"
    assert made["secret"] not in json.dumps([e.as_dict() for e in sink.events])


def test_without_a_store_or_with_auth_off(tmp_path):
    client, _, _ = build(tmp_path, store=False)
    listed = client.get("/keys", headers=as_("root-key")).json()
    assert listed["available"] is False and "auth.key_store" in listed["detail"]
    body = {"name": "x", "permissions": ["read"]}
    assert client.post("/keys", json=body, headers=as_("root-key")).status_code == 501
    off, _, _ = build(tmp_path, enabled=False)
    r = off.post("/keys", json=body)
    assert r.status_code == 409 and "Authentication is off" in r.json()["detail"]


def test_a_damaged_store_keeps_the_last_good_read(tmp_path):
    keys = AccessKeyService(JsonFileKeyStore(tmp_path / "keys.json"))
    grantor = Grantor(name="root", permissions=("all",), roles=(), has_row_rules=False)
    _, secret = keys.create(grantor, name="bi", permissions=["read"])
    (tmp_path / "keys.json").write_text("{not json")
    assert keys.caller_for(secret) is not None
    with pytest.raises(AccessKeyError, match="at least one permission"):
        keys.create(grantor, name="none", permissions=[])


def test_a_revocation_is_not_undone_by_another_process_writing_at_the_same_time(tmp_path):
    """Two services on one file, as two workers: one revokes while the other hands out.

    The revoking one is held between its read and its write; the other must
    wait for it and then build on what it wrote, not on what it read before.
    """
    import threading
    import time

    root = Grantor(name="root", permissions=("all",), roles=(), has_row_rules=False)
    path = tmp_path / "keys.json"
    first = AccessKeyService(JsonFileKeyStore(path))
    victim, victim_secret = first.create(root, name="victim", permissions=["read"])

    class Slow(JsonFileKeyStore):
        def save(self, keys):
            time.sleep(0.3)  # the other worker's request arrives now
            super().save(keys)

    revoking = AccessKeyService(Slow(path))
    other = AccessKeyService(JsonFileKeyStore(path))
    assert other.caller_for(victim_secret) is not None  # it has read the keys

    revoker = threading.Thread(target=revoking.revoke, args=(victim.id, "root"))
    revoker.start()
    time.sleep(0.1)  # the revocation holds the lock, its write still to come
    _, new_secret = other.create(root, name="newcomer", permissions=["read"])
    revoker.join()

    reader = AccessKeyService(JsonFileKeyStore(path))
    assert reader.caller_for(victim_secret) is None  # still revoked
    assert reader.caller_for(new_secret) is not None  # and the new key kept
    assert other.caller_for(victim_secret) is None  # the writer sees it too
