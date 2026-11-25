"""Whether the sample setup is up and behaves: `make demo-check`, and CI.

Run against a started `scripts/demo/docker-compose.yml` (or the same setup run
from source). Standard library only, as the machine that runs it has nothing
installed. The keys are read from `config.yaml`, so they are written once.

    python3 scripts/demo/check.py            # DEMO_URL says where, else :8000
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("DEMO_URL", "http://127.0.0.1:8000")
CONFIG = (Path(__file__).parent / "config.yaml").read_text(encoding="utf-8")
KEYS = {name: key for key, name in re.findall(r"- key: (\S+)\n\s+name: (\S+)", CONFIG)}

#: The query both films run: three tables, the national id among its columns.
SQL = (
    "select m.ad, m.tc_kimlik, s.sehir, sum(i.tutar) as toplam "
    "from smoke.islemler i "
    "join smoke.musteriler m on m.musteri_id = i.musteri_id "
    "join smoke.subeler s on s.sube_kodu = m.sube_kodu "
    "group by 1, 2, 3 order by 4 desc"
)


def call(path: str, key: str | None = None, body: dict | None = None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def rows(key: str) -> list[dict]:
    status, text = call("/olap/query", key, {"sql": SQL})
    assert status == 200, f"/olap/query answered {status}: {text[:300]}"
    return json.loads(text)["rows"]


def main() -> int:
    checks = []

    def check(what: str, ok: bool, detail: str = "") -> None:
        checks.append(ok)
        print(f"{'ok  ' if ok else 'FAIL'} {what}{'' if ok else ': ' + detail}")

    status, text = call("/health")
    check(
        "the server is healthy and reaches its database",
        status == 200 and '"healthy"' in text,
        text[:200],
    )

    status, text = call("/ui")
    check(
        "the console is served, with its guide",
        status == 200 and "/ui/guide.js" in text,
        str(status),
    )

    status, text = call("/ui/access", KEYS["admin"])
    access = json.loads(text) if status == 200 else {}
    check(
        "the administrator's key opens every page",
        all(access.get(p) for p in ("read", "query", "update", "create", "audit", "admin")),
        text[:200],
    )

    everyone, tenant = rows(KEYS["admin"]), rows(KEYS["tenant_a"])
    check("an administrator reads every tenant's rows", len(everyone) == 3, str(len(everyone)))
    check("a tenant's key reads its own rows only", len(tenant) == 2, str(len(tenant)))
    masked = [r["tc_kimlik"] for r in everyone + tenant]
    check(
        "the national id comes back masked", all(v.startswith("***") for v in masked), str(masked)
    )

    status, text = call(
        "/olap/tools/answer_question",
        KEYS["tenant_a"],
        {"question": "total transaction amount by city"},
    )
    check(
        "a question in words is answered from the certified terms",
        status == 200 and "tutar_sum" in text,
        text[:200],
    )

    print(f"{sum(checks)}/{len(checks)} checks passed against {BASE}")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
