"""A pilot: a customer's own questions, each judged right, refused or wrong.

The customer writes the questions they would ask and, for each, the answer
they expect: a query they trust or the rows themselves. The product answers
every question as any caller would (``answer_question``, under one key's row
rules and masks) and each answer is judged by its rows, never by its SQL.

The report is written to be sent out of the company: it holds counts and
verdicts only. No row value, no SQL, and the question text and refusal
messages only when asked for (``with_questions``), because a question can
name a person.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

Verdict = Literal["correct", "refused", "wrong", "unchecked"]

#: The order a report lists them in: what went wrong first.
VERDICTS: tuple[Verdict, ...] = ("wrong", "refused", "unchecked", "correct")

#: Sums computed in a different order differ in the last bits; that is not a wrong answer.
FLOAT_TOLERANCE = 1e-6

#: How ``answer_question`` begins a refusal of a question it reads none of.
_UNREAD_PREFIX = "Nothing in this question"
#: And of one whose words name tables no one request joins.
_APART_PREFIX = "This question names more than one reading can hold"


@dataclass(frozen=True, slots=True)
class PilotQuestion:
    """One question and how to know its answer is right."""

    id: str
    text: str
    #: A query whose rows are the right answer, run as the same caller.
    expected_sql: str = ""
    #: Or the rows themselves.
    expected_rows: tuple[tuple[Any, ...], ...] | None = None
    #: True only when the question asks for an order ("top 5").
    ordered: bool = False


@dataclass(frozen=True, slots=True)
class PilotOutcome:
    """What one question got. ``detail`` may quote the question; reports show it only on request."""

    id: str
    verdict: Verdict
    #: Why, in words that never quote the question or the data.
    reason: str = ""
    read_by: str = ""
    rows: int | None = None
    expected_rows: int | None = None
    unread_words: int = 0
    detail: str = ""


def parse_questions(data: Any) -> tuple[PilotQuestion, ...]:
    """The questions of a pilot file (``{"questions": [...]}``), checked.

    Raises:
        ValueError: Naming the entry, for a missing id or text, a duplicate id,
            or an entry that gives no expected answer or two.
    """
    entries = data.get("questions") if isinstance(data, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("a pilot file needs a non-empty 'questions' list")
    seen: set[str] = set()
    questions = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise ValueError(f"question {index}: expected a mapping")
        qid = str(entry.get("id") or index)
        text = entry.get("question")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"question {qid}: 'question' must be the question in words")
        if qid in seen:
            raise ValueError(f"question {qid}: the id is used twice")
        seen.add(qid)
        sql = entry.get("expected_sql") or ""
        has_rows = "expected" in entry
        if bool(sql) == has_rows:
            raise ValueError(f"question {qid}: give either 'expected_sql' or 'expected', not both")
        if sql and not isinstance(sql, str):
            raise ValueError(f"question {qid}: 'expected_sql' must be a query")
        questions.append(
            PilotQuestion(
                id=qid,
                text=text.strip(),
                expected_sql=sql,
                expected_rows=_as_rows(entry["expected"]) if has_rows else None,
                ordered=bool(entry.get("ordered", False)),
            )
        )
    return tuple(questions)


def _as_rows(value: Any) -> tuple[tuple[Any, ...], ...]:
    """``42`` is one row of one value; ``[1, 2]`` one row; ``[[1], [2]]`` two rows."""
    if not isinstance(value, list):
        return ((value,),)
    if value and all(isinstance(row, list) for row in value):
        return tuple(tuple(row) for row in value)
    return (tuple(value),)


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def values_equal(left: Any, right: Any) -> bool:
    """Whether two values are the same answer across drivers (Decimal vs float, date vs text)."""
    left, right = _normalize(left), _normalize(right)
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(left, right, rel_tol=FLOAT_TOLERANCE, abs_tol=FLOAT_TOLERANCE)
    if isinstance(left, str) and isinstance(right, str):
        return left.strip() == right.strip()
    return bool(left == right)


def rows_equal(
    actual: Sequence[Sequence[Any]], expected: Sequence[Sequence[Any]], ordered: bool = False
) -> bool:
    """Whether two result sets answer alike, by position in the row, never by column name."""
    if len(actual) != len(expected):
        return False
    if len({len(row) for row in [*actual, *expected]}) > 1:
        return False
    if ordered:
        return all(
            all(values_equal(a, b) for a, b in zip(x, y, strict=True))
            for x, y in zip(actual, expected, strict=True)
        )
    remaining = list(expected)
    for row in actual:
        for index, candidate in enumerate(remaining):
            if all(values_equal(a, b) for a, b in zip(row, candidate, strict=True)):
                remaining.pop(index)
                break
        else:
            return False
    return True


def judge(
    question: PilotQuestion,
    answer: Mapping[str, Any],
    expected: Sequence[Sequence[Any]] | None,
    expected_error: str = "",
) -> PilotOutcome:
    """One verdict from what ``answer_question`` returned and the expected rows.

    A refusal is never a wrong answer: the product said it could not answer.
    An answer cut at the tool's row cap, or an expected answer that did not
    run, is left unchecked rather than guessed at.
    """
    if "error" in answer:
        error = str(answer["error"])
        if error.startswith(_UNREAD_PREFIX):
            reason = "none of its words are in the vocabulary"
        elif error.startswith(_APART_PREFIX):
            reason = "it names tables one reading cannot join"
        elif error.startswith("Guardrail"):
            reason = "a guardrail refused it"
        else:
            reason = "it could not be answered"
        return PilotOutcome(question.id, "refused", reason, detail=error)
    columns = list(answer.get("columns", ()))
    rows = [tuple(record[c] for c in columns) for record in answer.get("rows", ())]
    read_by = "model" if answer.get("read_by") == "model" else "vocabulary"
    unread = len(answer.get("unread", ()))

    def outcome(verdict: Verdict, reason: str = "", detail: str = "") -> PilotOutcome:
        return PilotOutcome(
            question.id,
            verdict,
            reason,
            read_by=read_by,
            rows=len(rows),
            expected_rows=None if expected is None else len(expected),
            unread_words=unread,
            detail=detail,
        )

    if expected is None:
        return outcome("unchecked", "the expected answer did not run", expected_error)
    if answer.get("truncated"):
        return outcome("unchecked", "the answer has more rows than a tool returns")
    if rows_equal(rows, expected, question.ordered):
        return outcome("correct")
    if len(rows) != len(expected):
        return outcome("wrong", "a different number of rows")
    return outcome("wrong", "different values")


def tally(outcomes: Iterable[PilotOutcome]) -> dict[Verdict, int]:
    counts: dict[Verdict, int] = dict.fromkeys(VERDICTS, 0)
    for outcome in outcomes:
        counts[outcome.verdict] += 1
    return counts


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def markdown_table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines


def render_report(
    questions: Sequence[PilotQuestion],
    outcomes: Sequence[PilotOutcome],
    about: Sequence[tuple[str, str]] = (),
    max_wrong: int = 2,
    with_questions: bool = False,
) -> str:
    """The pilot report in Markdown, wrong answers first."""
    counts = tally(outcomes)
    texts = {q.id: q.text for q in questions}
    met = counts["wrong"] <= max_wrong
    lines = ["# Pilot report", ""]
    lines += [f"- **{label}:** {value}" for label, value in about]
    lines += [
        "",
        "## Result",
        "",
        *markdown_table(
            ["Correct", "Refused", "Wrong", "Not checked", "Questions"],
            [
                [
                    counts["correct"],
                    counts["refused"],
                    counts["wrong"],
                    counts["unchecked"],
                    len(outcomes),
                ]
            ],
        ),
        "",
        f"**Acceptance (at most {max_wrong} wrong):** {'met' if met else 'not met'}"
        f" ({counts['wrong']} wrong).",
        "",
        "A refusal is the product saying it could not read or answer a question; it is "
        "not a wrong answer. A wrong answer is one whose rows differ from the expected ones.",
    ]
    sections: dict[Verdict, tuple[str, list[str]]] = {
        "wrong": (
            "Wrong answers",
            ["Rows returned", "Rows expected", "Why", "Read by", "Words not read"],
        ),
        "refused": ("Refused", ["Why"]),
        "unchecked": ("Not checked", ["Why"]),
        "correct": ("Correct", ["Rows", "Read by", "Words not read"]),
    }
    for verdict in VERDICTS:
        chosen = [o for o in outcomes if o.verdict == verdict]
        if not chosen:
            continue
        title, columns = sections[verdict]
        header = ["Id", *(["Question"] if with_questions else []), *columns]
        if with_questions and verdict in ("refused", "unchecked"):
            header.append("Message")
        body = []
        for o in chosen:
            cells: list[Any] = [o.id, *([texts.get(o.id, "")] if with_questions else [])]
            if verdict == "wrong":
                cells += [o.rows, o.expected_rows, o.reason, o.read_by, o.unread_words]
            elif verdict == "correct":
                cells += [o.rows, o.read_by, o.unread_words]
            else:
                cells.append(o.reason)
                if with_questions:
                    cells.append(o.detail)
            body.append(cells)
        lines += ["", f"## {title} ({len(chosen)})", "", *markdown_table(header, body)]
    lines += [
        "",
        "---",
        "",
        "This report holds counts and verdicts only: no row values and no SQL."
        + (
            " It **does** hold the question text and refusal messages: keep it inside the company."
            if with_questions
            else " The questions appear by id only."
        ),
        "",
    ]
    return "\n".join(lines)


def render_comparison(
    question: str, answers: Sequence[tuple[str, str, Mapping[str, Any]]], limit: int = 10
) -> str:
    """One question answered for several callers, side by side: ``(name, what applies, answer)``."""
    lines = [f"# One question, {len(answers)} callers", "", f"> {question}", ""]
    for name, applies, answer in answers:
        lines += [f"## As `{name}`", "", f"_{applies}_", ""]
        if "error" in answer:
            lines += [f"Refused: {answer['error']}", ""]
            continue
        columns = list(answer.get("columns", ()))
        records = list(answer.get("rows", ()))
        lines += markdown_table(columns, [[r[c] for c in columns] for r in records[:limit]])
        more = len(records) - limit
        lines += [
            "",
            f"{len(records)} row(s)" + (f", {more} not shown" if more > 0 else "") + ".",
            "",
        ]
    return "\n".join(lines)
