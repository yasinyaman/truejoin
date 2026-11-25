"""A report joined from two databases, as one caller may read it.

The sample setup serves a PostgreSQL (`smoke`: branches, customers,
transactions) and a MySQL (`crm`: branch targets, customer segments). This
writes a Markdown report from both: what each branch was set as a target beside
what its customers transacted, and the amounts per customer segment.

    python3 scripts/demo/report.py --as admin
    python3 scripts/demo/report.py --as tenant_a --out demo-out/report-a.md

It holds no SQL. The two analyses are saved in the `crm` catalog, certified by
a reviewer; the script asks the server for them and runs each as the key it was
given, so the numbers are that key's: a tenant's report counts the tenant's own
transactions, an administrator's everyone's. Each run is in the audit trail.

Standard library only. `--as` names a sample key of `config.yaml`; `--key` and
`--url` point it at another server.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG = (Path(__file__).parent / "config.yaml").read_text(encoding="utf-8")
SAMPLE_KEYS = {name: key for key, name in re.findall(r"- key: (\S+)\n\s+name: (\S+)", CONFIG)}
#: The saved analyses the report is made of, in the order it shows them.
ANALYSES = ("target_vs_actual_per_branch", "amount_per_segment")


class Server:
    def __init__(self, url: str, key: str) -> None:
        self.url, self.key = url.rstrip("/"), key

    def call(self, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-API-Key": self.key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            raise SystemExit(f"{path} answered {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise SystemExit(
                f"{self.url} is not reachable ({e.reason}): is the setup up?"
            ) from None


def cell(value: object) -> str:
    if isinstance(value, float):
        return f"{value:,.3f}".rstrip("0").rstrip(".") if value != int(value) else f"{int(value):,}"
    return "" if value is None else str(value)


def table(columns: list[str], rows: list[dict]) -> list[str]:
    numeric = [bool(rows) and all(isinstance(r[c], int | float) for r in rows) for c in columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("---:" if n else "---" for n in numeric) + "|",
    ]
    lines += ["| " + " | ".join(cell(row[c]) for c in columns) + " |" for row in rows]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as", dest="who", default="admin", choices=sorted(SAMPLE_KEYS))
    parser.add_argument("--key", help="an API key, in place of a sample one")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--api-prefix", default="/api/v1")
    parser.add_argument("--catalog", default="crm", help="the catalog that holds the analyses")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--out", type=Path, help="write the report here (default: print it)")
    args = parser.parse_args()
    server = Server(args.url, args.key or SAMPLE_KEYS[args.who])

    caller = server.call("/ui/access")["actor"]
    saved = server.call(f"{args.api_prefix}/catalog/{args.catalog}/ontology")["examples"]
    databases = sorted({t.split(".")[0] for name in ANALYSES for t in saved[name]["tables"]})
    lines = [
        "# Branch targets and actuals",
        "",
        f"For **{caller}**, {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}, "
        f"joined from {len(databases)} databases: {', '.join(f'`{d}`' for d in databases)}.",
        "",
    ]
    for name in ANALYSES:
        analysis = saved.get(name)
        if not analysis or not analysis.get("certified"):
            raise SystemExit(f"'{name}' is not a certified analysis of the {args.catalog} catalog")
        texts = analysis["question"]["texts"]
        answer = server.call("/olap/query", {"sql": analysis["sql"]})
        lines += [f"## {texts.get(args.lang) or next(iter(texts.values()))}", ""]
        lines += table(answer["columns"], answer["rows"])
        lines += ["", f"From {', '.join(f'`{t}`' for t in analysis['tables'])}.", ""]
    lines += [
        "---",
        "",
        f"The numbers are what the key **{caller}** may read: its row rules and masks applied in "
        "both databases, before the join. Another key's report can differ. Each query is one "
        "event in the audit trail, under this key's name.",
        "",
    ]
    report = "\n".join(lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"{args.out}: {len(ANALYSES)} analyses, as {caller}")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
