"""A pilot report on the bench's bank, as a customer would get one.

``python -m bench.pilot``. No model, seconds. Twenty questions nobody tuned the
planner on (the 16 held-out ones and four of the unreadable ones), each with
its DuckDB gold as the expected answer, run through ``truejoin.application.
olap.pilot`` as a masked caller, the way ``truejoin pilot run --as`` does on a
real database. Writes into ``bench/results-pilot/``:

- ``pilot-report.md``: the one that may leave the company (ids only);
- ``pilot-report-with-questions.md``: the same with the questions in words;
- ``compare.md``: one question as head office and as one branch.

These are a fixture's questions, not a customer's: the report shows the
format and how the product does on unseen questions, nothing about demand.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from bench.dataset import load_question_set
from bench.fixture import (
    ISLEMLER,
    ISLEMLER_COLUMNS,
    MUSTERILER,
    MUSTERILER_COLUMNS,
    SUBELER,
    SUBELER_COLUMNS,
    as_records,
)
from bench.fusion_source import fixture_factory
from bench.heldout import HELD_OUT
from bench.terms import CATALOG_PATH, load_certified

UNREADABLE = "bench/questions/banka-unreadable.json"
UNREADABLE_IDS = ("u01-tr", "u02-tr", "u05-tr", "u07-tr")
OUT = Path("bench/results-pilot")
COMPARE = "Şehir bazında başarılı işlem tutarı"

_TABLE = re.compile(r"\b(FROM|JOIN)\s+(islemler|musteriler|subeler)\b", re.IGNORECASE)


def qualified(sql: str) -> str:
    """The gold names bare tables; the engine knows them as ``banka.<table>``."""
    return _TABLE.sub(lambda m: f"{m.group(1)} banka.{m.group(2)}", sql)


def questions():
    from truejoin.domain.olap.pilot import PilotQuestion

    chosen = [*load_question_set(HELD_OUT)]
    chosen += [q for q in load_question_set(UNREADABLE) if q.id in UNREADABLE_IDS]
    return [
        PilotQuestion(q.id, q.text, expected_sql=qualified(q.gold_for("duckdb")), ordered=q.ordered)
        for q in chosen
    ]


def scopes():
    """A pilot key that sees names and addresses masked, and one branch's key besides."""
    from truejoin.domain.governance.access import AccessScope
    from truejoin.domain.governance.masking import CatalogMasking, MaskingPolicy
    from truejoin.domain.governance.row_policy import build_policy

    masking = CatalogMasking(
        policy=MaskingPolicy(rules={"email": "redact", "name": "partial"}),
        semantic_types={"musteriler": {"eposta": "email", "ad_soyad": "name"}},
    )
    pilot = AccessScope(actor="pilot", masking={"banka": masking}, masked=True)
    branch = AccessScope(
        actor="istanbul",
        masking={"banka": masking},
        masked=True,
        policy=build_policy({"musteriler": [{"column": "sube_kodu", "value": "IST01"}]}),
    )
    return pilot, branch


def main() -> int:
    from truejoin import Settings, build_app
    from truejoin.application.olap.pilot import run_pilot
    from truejoin.domain.olap.pilot import render_comparison, render_report, tally

    tables = {
        "subeler": as_records(SUBELER, SUBELER_COLUMNS),
        "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
        "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
    }
    fusion = build_app(Settings(grounding="advise"), source_factory=fixture_factory(tables))
    try:
        fusion.sources.connect("banka", {"type": "fixture"})
        fusion.semantic.use_catalog("banka", load_certified(CATALOG_PATH))
        pilot, branch = scopes()
        asked = questions()
        outcomes = run_pilot(fusion.tools, fusion.query, asked, pilot)
        about = [
            ("Data", "the bench's bank fixture (3 tables), not a customer's"),
            ("Questions", f"{len(asked)}: bench held-out (16) and unreadable (4), never tuned on"),
            ("As", "pilot: no row rules; e-mail and names masked"),
            ("Catalog", f"`{CATALOG_PATH}`, its terms certified"),
        ]
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "pilot-report.md").write_text(render_report(asked, outcomes, about), "utf-8")
        (OUT / "pilot-report-with-questions.md").write_text(
            render_report(asked, outcomes, about, with_questions=True), "utf-8"
        )
        answers = [
            (
                scope.actor or "",
                applies,
                fusion.tools.for_scope(scope).execute("answer_question", {"question": COMPARE}),
            )
            for scope, applies in (
                (pilot, "head office: no row rules; e-mail and names masked"),
                (branch, "Istanbul branch: customers of IST01 only; e-mail and names masked"),
            )
        ]
        (OUT / "compare.md").write_text(render_comparison(COMPARE, answers), "utf-8")
    finally:
        fusion.close()
    counts = tally(outcomes)
    print(
        f"{OUT}: {counts['correct']} correct, {counts['refused']} refused, "
        f"{counts['wrong']} wrong, {counts['unchecked']} not checked"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
