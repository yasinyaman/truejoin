#!/usr/bin/env python3
"""The release smoke: is what was built the product that was promised?

Run against the stack in docker/smoke (see `make smoke`). Standard library
only, so it needs nothing installed. Every expected number is computed from
docker/smoke/init.sql: tenant a has customers Ayse (Istanbul) and Mehmet
(Ankara) and transactions of 100, 50 and 200; tenant b has Zeynep and 999.

It checks, on the built image in production mode:

- the same question over REST, over MCP and as metrics returns a tenant's own
  rows, with the national id masked, and never another tenant's from the cache;
- a role that is exempt sees the raw value, and nobody gets in without a key;
- metrics join along the keys the database declares, two tables away;
- a join written on the wrong column is repaired;
- a question in words is answered from the certified terms, and a follow-up
  ("only the successful ones", "and the cancelled ones?") changes only what
  its words name;
- saved analyses: only certified ones are offered, and one that quotes a value
  never to a caller with row rules or masks;
- a key an admin hands out is shown once, kept as a digest, held to the row
  rules it was given, refused once revoked, and nobody else can hand one out;
- every analytical read left exactly one audit event, with no SQL in it, and
  handing out and revoking a key are recorded by name only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_URL", "http://127.0.0.1:18000")
KEY_A = os.environ.get("SMOKE_KEY_A", "smoke-tenant-a-4f1c9d27b6e0")
KEY_B = os.environ.get("SMOKE_KEY_B", "smoke-tenant-b-8a3e5c61d9f2")
KEY_DPO = os.environ.get("SMOKE_KEY_DPO", "smoke-dpo-2b7d0e94c5a8")
KEY_ADMIN = os.environ.get("SMOKE_KEY_ADMIN", "smoke-admin-6c2f8b15e7d3")
COMPOSE = [
    "docker",
    "compose",
    "-f",
    os.environ.get("SMOKE_COMPOSE_FILE", "docker/smoke/docker-compose.yml"),
]

PEOPLE = "SELECT ad, tc_kimlik FROM smoke.musteriler ORDER BY musteri_id"
WRONG_JOIN = (
    "SELECT m.ad, SUM(i.tutar) AS toplam FROM smoke.islemler i "
    "JOIN smoke.musteriler m ON i.islem_id = m.musteri_id GROUP BY m.ad ORDER BY m.ad"
)
PER_CITY = {"table": "smoke.islemler", "metrics": ["tutar:sum"], "dimensions": ["subeler.sehir"]}

checks: list[str] = []
reads = {"tenant_a": 0, "tenant_b": 0, "dpo": 0, "bi_a": 0}


def call(
    path: str, body: dict | None = None, key: str | None = None, method: str | None = None
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


def tool(name: str, arguments: dict, key: str, who: str, reads_data: bool = True) -> dict:
    """One MCP tool call, as an agent makes it."""
    reads[who] += reads_data
    status, body = call(
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        key,
    )
    assert status == 200, (name, status, body)
    return json.loads(body["result"]["content"][0]["text"])


def query(sql: str, key: str, who: str) -> dict:
    reads[who] += 1
    status, body = call("/olap/query", {"sql": sql}, key)
    assert status == 200, (status, body)
    return body


def check(what: str, condition: bool, seen: object = "") -> None:
    if not condition:
        raise SystemExit(f"FAILED: {what}\n  saw: {seen}")
    checks.append(what)
    print(f"  ok  {what}")


def main() -> int:
    status, health = call("/health")
    check("the service is healthy", status == 200 and health.get("status") == "healthy", health)

    status, _ = call("/olap/query", {"sql": PEOPLE})
    check("nobody gets in without a key", status in (401, 403), status)

    # -- row rules, masks, cache isolation: REST ------------------------------
    a = query(PEOPLE, KEY_A, "tenant_a")["rows"]
    check(
        "REST: tenant a reads its own two customers, national id masked",
        [r["ad"] for r in a] == ["Ayse", "Mehmet"]
        and all(
            r["tc_kimlik"].endswith(raw[-4:]) and r["tc_kimlik"] != raw
            for r, raw in zip(a, ["10000000146", "20000000292"], strict=True)
        ),
        a,
    )
    b = query(PEOPLE, KEY_B, "tenant_b")["rows"]
    check(
        "REST: the same SQL gives tenant b its own row, not a's cached answer",
        [r["ad"] for r in b] == ["Zeynep"],
        b,
    )
    dpo = query(PEOPLE, KEY_DPO, "dpo")["rows"]
    check(
        "REST: the exempt role sees everyone, unmasked",
        [r["tc_kimlik"] for r in dpo] == ["10000000146", "20000000292", "30000000338"],
        dpo,
    )

    # -- the same over MCP ---------------------------------------------------------
    mcp = tool("query_data", {"sql": PEOPLE}, KEY_A, "tenant_a")["rows"]
    check("MCP: query_data serves tenant a exactly what REST did", mcp == a, mcp)

    # -- metrics, joined along the declared keys ---------------------------------
    cities = tool("query_metrics", PER_CITY, KEY_A, "tenant_a")
    got = {r["subeler_sehir"]: float(r["tutar_sum"]) for r in cities.get("rows", [])}
    check(
        "metrics: tenant a's amounts per city, two keys away",
        got == {"Istanbul": 150.0, "Ankara": 200.0},
        cities,
    )
    got_b = tool("query_metrics", PER_CITY, KEY_B, "tenant_b")
    check(
        "metrics: tenant b's are its own",
        {r["subeler_sehir"]: float(r["tutar_sum"]) for r in got_b.get("rows", [])}
        == {"Istanbul": 999.0},
        got_b,
    )

    # -- grounding ---------------------------------------------------------------
    repaired = query(WRONG_JOIN, KEY_A, "tenant_a")
    joins = (repaired.get("grounding") or {}).get("joins") or [{}]
    check(
        "grounding: a join on the wrong column is repaired, and says so",
        joins[0].get("repaired") is True
        and {r["ad"]: float(r["toplam"]) for r in repaired["rows"]}
        == {"Ayse": 150.0, "Mehmet": 200.0},
        repaired,
    )

    # -- a question in words ---------------------------------------------------------
    answer = tool(
        "answer_question",
        {"question": "Şehir bazında toplam işlem tutarı nedir?"},
        KEY_A,
        "tenant_a",
    )
    check(
        "terms: a question in words is answered from the certified vocabulary",
        {r["subeler_sehir"]: float(r["tutar_sum"]) for r in answer.get("rows", [])}
        == {"Istanbul": 150.0, "Ankara": 200.0}
        and answer.get("request", {}).get("dimensions") == ["subeler.sehir"],
        answer,
    )

    # -- a follow-up, carried by the caller --------------------------------------------
    previous = answer.get("request", {})
    paid = tool(
        "answer_question",
        {"question": "sadece başarılı olanlar", "previous": previous},
        KEY_A,
        "tenant_a",
    )
    cancelled = tool(
        "answer_question",
        {"question": "peki iptal olanlar?", "previous": paid.get("request", {})},
        KEY_A,
        "tenant_a",
    )

    def per_city(result: dict) -> dict:
        return {r["subeler_sehir"]: float(r["tutar_sum"]) for r in result.get("rows", [])}

    check(
        "terms: a follow-up keeps the breakdown and changes the condition, on the caller's rows",
        per_city(paid) == {"Istanbul": 100.0, "Ankara": 200.0}
        and per_city(cancelled) == {"Istanbul": 50.0}
        and [f["value"] for f in cancelled.get("request", {}).get("filters", [])] == ["iptal"],
        (paid, cancelled),
    )

    # -- saved analyses --------------------------------------------------------------
    def offered(key: str, who: str) -> set[str]:
        found = tool(
            "find_examples", {"question": "Şehir bazında tutar"}, key, who, reads_data=False
        )
        return {e["name"] for e in found.get("examples", [])}

    check(
        "examples: a restricted caller is offered only the certified one without a value",
        offered(KEY_A, "tenant_a") == {"per_city"},
        offered(KEY_A, "tenant_a"),
    )
    check(
        "examples: an unrestricted caller also gets the one that quotes a value, never a draft",
        offered(KEY_DPO, "dpo") == {"per_city", "successful_per_city"},
        offered(KEY_DPO, "dpo"),
    )
    example_sql = tool(
        "find_examples", {"question": "Şehir bazında tutar"}, KEY_A, "tenant_a", reads_data=False
    )["examples"][0]["sql"]
    rows = query(example_sql, KEY_A, "tenant_a")["rows"]
    check(
        "examples: the offered SQL runs, held to the caller's rows",
        {r["sehir"]: float(r["toplam"]) for r in rows} == {"Istanbul": 150.0, "Ankara": 200.0},
        rows,
    )

    # -- keys handed out at run time ---------------------------------------------
    rule = [{"column": "kiraci", "value": "${tenant}"}]
    status, made = call(
        "/api/v1/keys",
        {
            "name": "bi_a",
            "permissions": ["read", "query"],
            "tenant": "a",
            "row_filters": {"musteriler": rule, "islemler": rule},
        },
        KEY_ADMIN,
    )
    secret = made.get("secret", "")
    check(
        "keys: an admin hands out a key, shown once",
        status == 201 and secret.startswith("truejoin_"),
        made,
    )
    status, refused = call("/api/v1/keys", {"name": "x", "permissions": ["read"]}, KEY_A)
    check("keys: a caller without admin cannot hand one out", status == 403, refused)
    handed = query(PEOPLE, secret, "bi_a")["rows"]
    check(
        "keys: the handed-out key is held to its row rules, the national id masked",
        [r["ad"] for r in handed] == ["Ayse", "Mehmet"] and handed[0]["tc_kimlik"] != "10000000146",
        handed,
    )
    stored = subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "api",
            "sh",
            "-c",
            "stat -c %a /app/data/keys.json; cat /app/data/keys.json",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    check(
        "keys: the store keeps a digest, never the key, readable by the server only",
        stored.startswith("600") and secret not in stored and '"digest"' in stored,
        stored[:80],
    )
    status, _ = call(f"/api/v1/keys/{made['key']['id']}", key=KEY_ADMIN, method="DELETE")
    after, _ = call("/olap/query", {"sql": PEOPLE}, secret)
    check("keys: a revoked key is refused at once", status == 200 and after == 401, (status, after))

    # -- the audit trail -------------------------------------------------------------
    log = subprocess.run(
        [*COMPOSE, "exec", "-T", "api", "cat", "/app/logs/audit.jsonl"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    events = [json.loads(line) for line in log.splitlines() if line.strip()]
    queries = [e for e in events if e.get("action") == "query"]
    seen = {who: sum(1 for e in queries if e.get("actor") == who) for who in reads}
    check("audit: one event per analytical read, per caller", seen == reads, (seen, reads))
    check(
        "audit: no event carries SQL or a value",
        "SELECT" not in log and "10000000146" not in log,
        "",
    )
    check(
        "audit: tenant reads are marked as row-filtered",
        all(e.get("row_filtered") for e in queries if e.get("actor") in ("tenant_a", "bi_a")),
        queries[:2],
    )
    access = [
        (e.get("actor"), e.get("change")) for e in events if e.get("event") == "access_change"
    ]
    check(
        "audit: handing out and revoking a key are recorded by name, never the key",
        access == [("admin", "create_key bi_a"), ("admin", "revoke_key bi_a")]
        and secret not in log,
        access,
    )

    # -- the container itself --------------------------------------------------------
    who = subprocess.run(
        [*COMPOSE, "exec", "-T", "api", "sh", "-c", "id -u; touch /app/x 2>&1 || true"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    check(
        "the container runs as a non-root user on a read-only filesystem",
        who.startswith("1000") and "Read-only" in who,
        who,
    )

    print(f"\n{len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
