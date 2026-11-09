"""Questions the vocabulary reads none of: ``python -m bench.unreadable [model]``.

``answer_question`` refuses them (it used to answer a guess). This runs three
arms over ``banka-unreadable``: ``terms`` (that refusal), ``catalog`` (the
model writing SQL from the annotated schema) and ``terms+model-on-none``
(``answer_question``, except where ``check_question`` says it read nothing,
which goes to the model). On the tuned and held-out sets no question reads as
``none``, so there that arm is ``terms``. Needs a model (``OLLAMA_HOST``).
"""

from __future__ import annotations

import sys

from bench.arms import GapsOnlyArm, TextToSqlArm
from bench.dataset import QuestionSet, load_question_set
from bench.heldout import NAMES
from bench.runner import ArmResult, run_arm

UNREADABLE = "bench/questions/banka-unreadable.json"


def run(questions: QuestionSet, model: object) -> list[ArmResult]:
    """The three arms over ``questions``, graded against their DuckDB gold."""
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
    from bench.run_duckdb import CATALOG_CONTEXT, term_arms

    executor = DuckDBExecutor(load_duckdb(duckdb.connect()))
    gold = {q.id: executor.run(q.gold_for("duckdb")) for q in questions}
    tables = {
        "subeler": as_records(SUBELER, SUBELER_COLUMNS),
        "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
        "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
    }
    direct, *_ = term_arms(None, tables, NAMES)
    catalog = TextToSqlArm("catalog", model, executor, CATALOG_CONTEXT, "DuckDB")
    routed = GapsOnlyArm("terms+model-on-none", direct._fusion, direct, catalog, model_on=("none",))
    # The product's own way (olap.unread_questions: sql): answer_question
    # hands the model the schema it builds itself, on an engine of its own.
    product, *_ = term_arms(None, tables, NAMES)
    product._fusion.tools.write_sql = lambda prompt, system: model.complete(  # type: ignore[attr-defined]
        f"{system}\n\n{prompt}"
    )
    product.name = "terms+sql-on-unread"
    return [
        run_arm(arm, tuple(questions), gold, "duckdb", questions.identifiers)
        for arm in (direct, catalog, routed, product)
    ]


def main(model_name: str = "qwen2.5-coder:7b") -> int:
    from bench.models import OllamaModel

    questions = load_question_set(UNREADABLE)
    for result in run(questions, OllamaModel(model=model_name)):
        print(f"{questions.name:<17} {result.name:<20} {result.accuracy(list(result.grades))}")
        for question in questions:
            grade = result.grades[question.id]
            if not grade.correct:
                print(f"    missed {question.id}: {question.text} {grade.error[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
