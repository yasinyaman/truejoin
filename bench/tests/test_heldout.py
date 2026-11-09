"""The held-out set: separate from the tuned one, answerable, and not slipping back."""

import duckdb

from bench.dataset import load_question_set
from bench.fixture import load_duckdb
from bench.heldout import HELD_OUT, TUNED, run


def test_it_shares_no_question_with_the_tuned_set():
    tuned, held_out = load_question_set(TUNED), load_question_set(HELD_OUT)
    assert len(held_out) == 16
    assert not {q.id for q in tuned} & {q.id for q in held_out}
    assert not {q.text.lower() for q in tuned} & {q.text.lower() for q in held_out}
    assert held_out.identifiers == tuned.identifiers


def test_every_gold_query_runs_and_answers_something():
    connection = load_duckdb(duckdb.connect())
    for question in load_question_set(HELD_OUT):
        for dialect in ("oracle", "mssql", "duckdb"):
            assert question.gold_for(dialect), (question.id, dialect)
        assert connection.execute(question.gold_for("duckdb")).fetchall(), question.id


def test_the_planner_does_not_slip_back():
    # A floor, not a target: a held-out miss fixed on purpose stops being held out.
    results = {r.name: r for r in run(load_question_set(HELD_OUT))}
    right = {name: sum(g.correct for g in r.grades.values()) for name, r in results.items()}
    assert right["terms"] >= 12
    assert right["lexicon"] >= 9
