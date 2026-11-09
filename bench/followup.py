"""Follow-up questions, answered with no model: ``python -m bench.followup``.

Each question in ``banka-followup`` follows up on another (``after``): the
opening one is answered first and its request passed back as ``previous``, as
a caller carrying a conversation would. Only the follow-up is graded. Written
before the planner was run on them; like the held-out set, never tuned on.
"""

from __future__ import annotations

import sys

from bench.arms import FollowUpArm
from bench.dataset import QuestionSet, load_question_set
from bench.heldout import NAMES
from bench.run_duckdb import term_arms
from bench.runner import ArmResult, run_arm

FOLLOW_UPS = "bench/questions/banka-followup.json"


def run(questions: QuestionSet) -> ArmResult:
    """The follow-ups answered over the certified vocabulary, graded against their DuckDB gold."""
    import duckdb

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
    from bench.models import DuckDBExecutor

    executor = DuckDBExecutor(load_duckdb(duckdb.connect()))
    gold = {q.id: executor.run(q.gold_for("duckdb")) for q in questions}
    tables = {
        "subeler": as_records(SUBELER, SUBELER_COLUMNS),
        "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
        "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
    }
    direct, *_ = term_arms(None, tables, NAMES)
    arm = FollowUpArm("terms, follow-up", direct._fusion)
    return run_arm(arm, tuple(questions), gold, "duckdb", questions.identifiers)


def main() -> int:
    questions = load_question_set(FOLLOW_UPS)
    result = run(questions)
    print(f"{questions.name:<15} {result.name} {result.accuracy(list(result.grades))}")
    for question in questions:
        grade = result.grades[question.id]
        if not grade.correct:
            print(
                f"    missed {question.id}: {question.after!r} -> {question.text!r} {grade.error}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
