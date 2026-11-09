"""The unreadable set: what it holds, and where the routed arm sends it."""

import re

import duckdb

from bench.arms import ScriptedModel
from bench.dataset import load_question_set
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
from bench.heldout import HELD_OUT, NAMES, TUNED
from bench.run_duckdb import term_arms
from bench.unreadable import UNREADABLE, run

TABLES = {
    "subeler": as_records(SUBELER, SUBELER_COLUMNS),
    "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
    "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
}


def verdicts(path: str) -> dict[str, str]:
    direct, *_ = term_arms(None, TABLES, NAMES)
    tools = direct._fusion.tools
    return {
        q.id: tools.execute("check_question", {"question": q.text})["covered"]
        for q in load_question_set(path)
    }


def test_the_vocabulary_reads_none_of_them_and_each_has_an_answer():
    connection = load_duckdb(duckdb.connect())
    questions = load_question_set(UNREADABLE)
    assert len(questions) == 16
    for question in questions:
        assert connection.execute(question.gold_for("duckdb")).fetchall(), question.id
    assert set(verdicts(UNREADABLE).values()) == {"none"}


def test_on_the_other_sets_the_routed_arm_never_calls_the_model():
    assert "none" not in {*verdicts(TUNED).values(), *verdicts(HELD_OUT).values()}


def test_every_unreadable_question_goes_to_the_model():
    questions = load_question_set(UNREADABLE)
    model = ScriptedModel({q.text: q.gold_for("duckdb") for q in questions})
    terms, catalog, routed, product = run(questions, model)
    assert sum(g.correct for g in catalog.grades.values()) == len(questions)
    assert {i: g.correct for i, g in routed.grades.items()} == {
        i: g.correct for i, g in catalog.grades.items()
    }
    assert sum(g.correct for g in terms.grades.values()) < len(questions)
    # The product's path asks with its own prompt, which holds the question
    # text too, and wants tables named as source.table.
    qualified = ScriptedModel(
        {
            q.text: re.sub(r"\b(islemler|musteriler|subeler)\b", r"banka.\1", q.gold_for("duckdb"))
            for q in questions
        }
    )
    *_, product = run(questions, qualified)
    assert sum(g.correct for g in product.grades.values()) == len(questions)
