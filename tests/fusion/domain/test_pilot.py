"""Judging a pilot's answers and writing the report that leaves the company."""

from decimal import Decimal

import pytest

from truejoin.domain.olap.pilot import (
    PilotOutcome,
    PilotQuestion,
    judge,
    parse_questions,
    render_comparison,
    render_report,
    rows_equal,
)

Q = PilotQuestion(id="q1", text="ann@x.com için sipariş sayısı", expected_rows=((1,),))


def answer(*rows, **extra):
    return {"columns": ["n"], "rows": [{"n": r} for r in rows], **extra}


class TestParse:
    def test_sql_rows_and_scalars(self):
        questions = parse_questions(
            {
                "questions": [
                    {"id": "a", "question": "x?", "expected_sql": "SELECT 1"},
                    {"question": "y?", "expected": 42},
                    {"question": "z?", "expected": [["a", 1], ["b", 2]], "ordered": True},
                ]
            }
        )
        assert [q.id for q in questions] == ["a", "2", "3"]
        assert questions[1].expected_rows == ((42,),)
        assert questions[2].expected_rows == (("a", 1), ("b", 2)) and questions[2].ordered

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({}, "non-empty 'questions'"),
            ({"questions": [{"question": " "}]}, "in words"),
            ({"questions": [{"question": "x"}]}, "either"),
            ({"questions": [{"question": "x", "expected": 1, "expected_sql": "S"}]}, "either"),
            (
                {"questions": [{"id": 1, "question": "x", "expected": 1}] * 2},
                "used twice",
            ),
        ],
    )
    def test_a_bad_file_names_the_question(self, data, message):
        with pytest.raises(ValueError, match=message):
            parse_questions(data)


class TestJudge:
    def test_rows_compare_across_drivers_and_order(self):
        assert rows_equal([(Decimal("1.5"), "a ")], [(1.5, "a")])
        assert rows_equal([(1,), (2,)], [(2,), (1,)])
        assert not rows_equal([(1,), (2,)], [(2,), (1,)], ordered=True)
        assert not rows_equal([(True,)], [(1,)])

    def test_verdicts(self):
        assert judge(Q, answer(1), [(1,)]).verdict == "correct"
        wrong = judge(Q, answer(2), [(1,)])
        assert (wrong.verdict, wrong.reason) == ("wrong", "different values")
        assert judge(Q, answer(1, 2), [(1,)]).reason == "a different number of rows"

    def test_a_refusal_is_not_a_wrong_answer(self):
        refused = judge(
            Q, {"error": "Nothing in this question is in the vocabulary: kargo."}, [(1,)]
        )
        assert refused.verdict == "refused"
        assert refused.reason == "none of its words are in the vocabulary"
        apart = judge(
            Q, {"error": "This question names more than one reading can hold: ..."}, [(1,)]
        )
        assert (apart.verdict, apart.reason) == (
            "refused",
            "it names tables one reading cannot join",
        )

    def test_what_cannot_be_checked_is_not_guessed(self):
        assert judge(Q, answer(1), None, "no such table").verdict == "unchecked"
        assert judge(Q, answer(1, truncated=True), [(1,)]).verdict == "unchecked"

    def test_a_model_written_and_partly_read_answer_says_so(self):
        outcome = judge(Q, answer(1, read_by="model", unread=["kargo"]), [(1,)])
        assert (outcome.read_by, outcome.unread_words) == ("model", 1)


class TestReport:
    OUTCOMES = [
        PilotOutcome("q1", "correct", rows=1, expected_rows=1, read_by="vocabulary"),
        PilotOutcome("q2", "refused", "none of its words are in the vocabulary", detail="kargo"),
        PilotOutcome("q3", "wrong", "different values", rows=1, expected_rows=1),
    ]
    QUESTIONS = [Q, PilotQuestion("q2", "Kargo ücreti?", expected_rows=((1,),))]

    def test_wrong_first_and_acceptance(self):
        report = render_report(self.QUESTIONS, self.OUTCOMES, [("As", "pilot")], max_wrong=0)
        assert (
            report.index("## Wrong answers")
            < report.index("## Refused")
            < report.index("## Correct")
        )
        assert "**Acceptance (at most 0 wrong):** not met (1 wrong)" in report
        assert "| 1 | 1 | 1 | 0 | 3 |" in report

    def test_the_shareable_report_never_quotes_a_question(self):
        report = render_report(self.QUESTIONS, self.OUTCOMES)
        assert "ann@x.com" not in report and "kargo" not in report.lower()
        inside = render_report(self.QUESTIONS, self.OUTCOMES, with_questions=True)
        assert "ann@x.com" in inside and "| kargo |" in inside
        assert "keep it inside" in inside

    def test_comparison(self):
        text = render_comparison(
            "q?",
            [("a", "no row rules", answer(1, 2, 3)), ("b", "row rules apply", {"error": "no"})],
            limit=2,
        )
        assert "## As `a`" in text and "3 row(s), 1 not shown." in text
        assert "Refused: no" in text
