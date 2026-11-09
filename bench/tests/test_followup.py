"""The follow-up set: answerable, telling, and not slipping back."""

import duckdb

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
from bench.followup import FOLLOW_UPS, run
from bench.heldout import HELD_OUT, NAMES, TUNED
from bench.run_duckdb import term_arms
from bench.scoring import rows_equal


def test_every_follow_up_has_an_opening_and_a_gold_that_answers_something():
    connection = load_duckdb(duckdb.connect())
    follow_ups = load_question_set(FOLLOW_UPS)
    assert len(follow_ups) == 16
    for question in follow_ups:
        assert question.after and question.after != question.text, question.id
        assert connection.execute(question.gold_for("duckdb")).fetchall(), question.id
    others = {q.text.lower() for path in (TUNED, HELD_OUT) for q in load_question_set(path)}
    assert not {q.text.lower() for q in follow_ups} & others


def test_an_answer_that_ignored_the_follow_up_would_be_wrong():
    """The opening question's own answer never matches the follow-up's gold.

    Otherwise a follow-up read as nothing would pass, as f08 once did: premium
    and standard customers made the same number of transactions.
    """
    connection = load_duckdb(duckdb.connect())
    tables = {
        "subeler": as_records(SUBELER, SUBELER_COLUMNS),
        "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
        "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
    }
    direct, *_ = term_arms(None, tables, NAMES)
    tools = direct._fusion.tools
    for question in load_question_set(FOLLOW_UPS):
        opening = tools.execute("answer_question", {"question": question.after})
        rows = [tuple(row[c] for c in opening["columns"]) for row in opening["rows"]]
        gold = connection.execute(question.gold_for("duckdb")).fetchall()
        assert not rows_equal(rows, gold, question.ordered), question.id


def test_the_follow_ups_do_not_slip_back():
    # A floor, not a target: the planner is not tuned on these.
    result = run(load_question_set(FOLLOW_UPS))
    assert sum(g.correct for g in result.grades.values()) >= 15
