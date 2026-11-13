"""Run a pilot: every question through ``answer_question``, as one caller.

The expected answer runs as the same caller, so a masked column is compared
masked to masked and a row rule narrows both sides alike. Both reads go
through the ordinary pipeline, so each is audited like any other.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from truejoin.application.olap.query import QueryService
from truejoin.application.olap.tools import ToolService
from truejoin.domain.governance.access import UNRESTRICTED, AccessScope
from truejoin.domain.olap.pilot import PilotOutcome, PilotQuestion, judge


def expected_rows(
    query: QueryService, question: PilotQuestion, scope: AccessScope
) -> tuple[list[tuple[Any, ...]] | None, str]:
    """The expected rows, or ``None`` and why the expected query did not run."""
    if question.expected_rows is not None:
        return [tuple(row) for row in question.expected_rows], ""
    try:
        return [tuple(row) for row in query.sql(question.expected_sql, scope=scope).rows], ""
    except Exception as e:  # the customer's own query: any failure leaves it unchecked
        return None, str(e)


def run_pilot(
    tools: ToolService,
    query: QueryService,
    questions: Sequence[PilotQuestion],
    scope: AccessScope = UNRESTRICTED,
) -> list[PilotOutcome]:
    bound = tools.for_scope(scope)
    outcomes = []
    for question in questions:
        expected, why = expected_rows(query, question, scope)
        answer = bound.execute("answer_question", {"question": question.text})
        outcomes.append(judge(question, answer, expected, why))
    return outcomes
