"""The arms that need no model, on the tuned set and the held-out set side by side.

``python -m bench.heldout``. The planner's rules were written from the misses
on ``banka-tr-en``, so its score there says how well it was fitted, not how
well it reads. ``banka-heldout`` was written afterwards and never tuned on;
the gap between the two is the honest number. Takes seconds and no model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

from bench.arms import LexiconArm
from bench.dataset import QuestionSet, load_question_set
from bench.fixture import (
    ISLEMLER,
    ISLEMLER_COLUMNS,
    MUSTERILER,
    MUSTERILER_COLUMNS,
    SUBELER,
    SUBELER_COLUMNS,
    as_records,
    load_duckdb,
)
from bench.fusion_source import fixture_factory
from bench.lexicon_gen import load as load_lexicon
from bench.models import DuckDBExecutor
from bench.run_duckdb import term_arms
from bench.runner import ArmResult, run_arm

TUNED = "bench/questions/banka-tr-en.json"
HELD_OUT = "bench/questions/banka-heldout.json"
LEXICON_PATH = "bench/lexicons/banka-7b.json"
NAMES = ["banka.islemler", "banka.musteriler", "banka.subeler"]


def run(questions: QuestionSet) -> list[ArmResult]:
    """The lexicon and terms arms over ``questions``, graded against their DuckDB gold."""
    from trio import Settings, build_app

    executor = DuckDBExecutor(load_duckdb(duckdb.connect()))
    gold = {q.id: executor.run(q.gold_for("duckdb")) for q in questions}
    tables = {
        "subeler": as_records(SUBELER, SUBELER_COLUMNS),
        "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
        "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
    }
    fusion = build_app(Settings(), source_factory=fixture_factory(tables))
    fusion.sources.connect("banka", {"type": "fixture"})
    lexicon = load_lexicon(LEXICON_PATH) if Path(LEXICON_PATH).exists() else None
    # term_arms' first arm is answer_question on its own; the second needs a model.
    arms = [
        LexiconArm("lexicon", fusion, NAMES, lexicon=lexicon),
        *term_arms(None, tables, NAMES)[:1],
    ]
    try:
        return [
            run_arm(arm, tuple(questions), gold, "duckdb", questions.identifiers) for arm in arms
        ]
    finally:
        fusion.close()


def main() -> int:
    for path in (TUNED, HELD_OUT):
        questions = load_question_set(path)
        for result in run(questions):
            print(f"{questions.name:<14} {result.name:<8} {result.accuracy(list(result.grades))}")
            if path == HELD_OUT:
                for question in questions:
                    if not result.grades[question.id].correct:
                        print(f"    missed {question.id}: {question.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
