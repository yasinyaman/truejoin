"""Whether the built-in assistant, with a real model behind it, behaves on the sample setup.

Not part of `make demo-check`: the sample setup has no model. Run it against
the setup served with the assistant turned on (the comment at the top of
`config.yaml` says what to add), with whatever model that is:

    python3 scripts/demo/assistant_check.py      # DEMO_URL says where, else :8000

It asks a handful of questions as the tenant's key and as the administrator's
and checks what a scripted model cannot show: that a real one calls the tools,
that its figures are the ones the same key gets from SQL, that a national id
never comes back whole, and that the audit trail holds the runs and nothing of
what was read. A model words its answers as it likes, so the figures are looked
for in the text and in the rows; read the answers it prints as well.

Standard library only, as `check.py` is. It spends model tokens: about a dozen
turns in all.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get("DEMO_URL", "http://127.0.0.1:8000")
CONFIG = (Path(__file__).parent / "config.yaml").read_text(encoding="utf-8")
KEYS = {name: key for key, name in re.findall(r"- key: (\S+)\n\s+name: (\S+)", CONFIG)}
#: A question may take several turns of a model that thinks before it answers.
TIMEOUT = 300

BY_CITY = (
    "select s.sehir, sum(i.tutar) as toplam from smoke.islemler i "
    "join smoke.musteriler m on m.musteri_id = i.musteri_id "
    "join smoke.subeler s on s.sube_kodu = m.sube_kodu group by 1 order by 1"
)
#: A national id as the database holds it: eleven digits. Masked, four are left.
WHOLE_ID = re.compile(r"(?<!\d)\d{11}(?!\d)")


def call(path: str, key: str, body: dict | None = None, request_id: str = "") -> tuple[int, str]:
    headers = {"Content-Type": "application/json", "X-API-Key": key}
    if request_id:
        headers["X-Request-ID"] = request_id
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def ask(key: str, question: str, request_id: str, **more: object) -> dict:
    """One question as one JSON document; a failure comes back as ``{"failed": ...}``."""
    status, text = call("/olap/assistant", key, {"question": question, **more}, request_id)
    if status != 200:
        return {"failed": f"{status}: {text[:300]}", "text": "", "steps": [], "sql": []}
    return json.loads(text)


def ask_streamed(key: str, question: str, request_id: str) -> list[dict]:
    """One question as the events of its run, a line each."""
    status, text = call("/olap/assistant?stream=true", key, {"question": question}, request_id)
    if status != 200:
        return [{"type": "error", "code": str(status), "message": text[:300]}]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def said(answer: dict) -> str:
    """Everything an answer shows its reader: its words and the rows beside them."""
    return answer.get("text", "") + " " + json.dumps(answer.get("result") or {}, default=str)


def number_in(text: str, value: float) -> bool:
    """Whether a figure is in a text, however the model wrote it (200, 200.0, 200,00)."""
    whole = f"{value:g}"
    return re.search(rf"(?<![\d.,]){re.escape(whole)}(?:[.,]0+)?(?!\d)", text) is not None


def main() -> int:  # noqa: C901, PLR0915 - one list of checks, read top to bottom
    failures: list[str] = []
    tokens = [0, 0]

    def check(what: str, ok: bool, detail: str = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {what}" + (f" ({detail})" if detail and not ok else ""))
        if not ok:
            failures.append(what)

    def show(who: str, question: str, answer: dict) -> None:
        usage = answer.get("usage") or {}
        tokens[0] += usage.get("input_tokens") or 0
        tokens[1] += usage.get("output_tokens") or 0
        tools = ", ".join(f"{s['tool']}:{s['outcome']}" for s in answer.get("steps", []))
        print(f"\n[{who}] {question}\n  tools: {tools or 'none'}")
        for line in (answer.get("failed") or answer.get("text", "")).splitlines():
            print(f"  | {line}")

    tenant, admin = KEYS["tenant_a"], KEYS["admin"]
    run = uuid.uuid4().hex[:8]
    ids = {name: f"live-{run}-{name}" for name in ("count", "city", "who", "admin", "weather")}

    status, text = call("/olap/assistant", tenant)
    state = json.loads(text) if status == 200 else {}
    check("the assistant answers here", state.get("available") is True, text[:200])
    if not state.get("available"):
        return 1
    print(f"     model: {state['provider']}/{state['model']}, tools: {len(state['tools'])}")
    outside = state["masks_everyone"]
    print(f"     the model is {'outside' if outside else 'on'} this server")

    # 1. A plain question, as the tenant: its own rows only.
    status, text = call(
        "/olap/query", tenant, {"sql": "select count(*) as n from smoke.musteriler"}
    )
    own = json.loads(text)["rows"][0]["n"]
    status, text = call("/olap/query", admin, {"sql": "select count(*) as n from smoke.musteriler"})
    everyone = json.loads(text)["rows"][0]["n"]
    question = "How many customers do I have?"
    answer = ask(tenant, question, ids["count"])
    show("tenant_a", question, answer)
    check(
        "a real model answers by calling a tool",
        bool(answer.get("steps")),
        str(answer.get("failed")),
    )
    check(
        f"the tenant is told its own {own} customers, not all {everyone}",
        number_in(answer.get("text", ""), own) and own != everyone,
    )

    # 2. Figures, streamed: the ones the same key gets from SQL.
    status, text = call("/olap/query", tenant, {"sql": BY_CITY})
    by_city = {row["sehir"]: row["toplam"] for row in json.loads(text)["rows"]}
    question = "What is the total transaction amount by city?"
    events = ask_streamed(tenant, question, ids["city"])
    final = events[-1] if events else {}
    show(
        "tenant_a", question, {**final, "steps": [e for e in events if e.get("phase") == "result"]}
    )
    check("the stream ends in an answer", final.get("type") == "answer", json.dumps(final)[:200])
    kinds = [(e["type"], e.get("phase")) for e in events]
    check(
        "each step comes as a call, then its result",
        ("step", "call") in kinds and ("step", "result") in kinds,
    )
    check(
        f"its totals are the tenant's own by SQL ({by_city})",
        all(
            city in said(final) and number_in(said(final), total) for city, total in by_city.items()
        ),
    )

    # 3. A follow-up that asks for what is masked.
    history = [{"question": question, "answer": final.get("text", "")}]
    question = "Who are the customers behind those totals? Give their national id numbers too."
    answer = ask(tenant, question, ids["who"], history=history, conversation=f"live-{run}")
    show("tenant_a", question, answer)
    check(
        "the follow-up is answered",
        answer.get("outcome") in ("answered", "exhausted"),
        str(answer.get("failed")),
    )
    check("no national id comes back whole to the tenant", not WHOLE_ID.search(said(answer)))
    check("the masked ids are in what it read", "***" in said(answer), "no masked value was shown")

    # 4. The same as the administrator: masked too when the model is outside.
    answer = ask(admin, "List the customers with their national id numbers.", ids["admin"])
    show("admin", "List the customers with their national id numbers.", answer)
    check(
        "the administrator is answered",
        answer.get("outcome") in ("answered", "exhausted"),
        str(answer.get("failed")),
    )
    check("no national id comes back whole to the administrator", not WHOLE_ID.search(said(answer)))

    # 5. Not a question about the data.
    question = "What is the weather in Ankara today?"
    answer = ask(tenant, question, ids["weather"])
    show("tenant_a", question, answer)
    check(
        "a question the data cannot answer still ends in words",
        bool(answer.get("text")),
        str(answer.get("failed")),
    )

    # The trail: the runs, their calls as the caller's, and nothing that was read.
    status, text = call(f"/api/v1/audit?request_id={ids['who']}", admin)
    events = json.loads(text).get("events", []) if status == 200 else []
    runs = [e for e in events if e.get("event") == "assistant_run"]
    calls = [e for e in events if e.get("event") == "tool_call"]
    check("the follow-up left one run in the audit trail", len(runs) == 1, text[:200])
    if runs:
        check(
            "the run names the model and the conversation",
            runs[0]["provider"] == state["provider"] and runs[0]["conversation"] == f"live-{run}",
        )
    check(
        "its tool calls are the tenant's, marked as the assistant's",
        bool(calls)
        and all(c["actor"] == "tenant_a" and c.get("via") == "assistant" for c in calls),
    )
    seen = []
    for request_id in ids.values():
        status, text = call(f"/api/v1/audit?request_id={request_id}&limit=500", admin)
        seen.append(text)
    trail = "\n".join(seen)
    check("the trail holds no national id", not WHOLE_ID.search(trail) and "***" not in trail)
    check("the trail holds no SQL", "select " not in trail.lower())

    print(f"\nmodel tokens, where the provider reports them: {tokens[0]} in, {tokens[1]} out")
    total = (
        "all checks passed" if not failures else f"{len(failures)} failed: {'; '.join(failures)}"
    )
    print(f"{total} against {BASE}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
