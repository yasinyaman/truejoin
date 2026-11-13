"""The proof gate: a repaired statement differs from the original by one literal.

When a condition compares a column with a value it does not hold ('Premium'
where the data says 'premium'), the fix is to swap that one string. Whoever
proposes the fix, a rewriter here or a model elsewhere, is not trusted to have
changed only that: the candidate runs only if this proves it. After the
literal-repair gate in NVIDIA Auto Ontology.

Parser-free, like the rest of ``sql_text``: the check must not depend on a
candidate being something a particular parser round-trips.
"""

from __future__ import annotations

from collections.abc import Callable

from truejoin.domain.olap.sql_text import normalize_sql_for_cache


def split_literals(sql: str) -> tuple[str, list[str]]:
    """``sql`` with every string literal blanked, and the literals in order.

    Quoted identifiers stay in the skeleton: a candidate that renamed a
    column is not the same statement.
    """
    skeleton: list[str] = []
    literals: list[str] = []
    current: list[str] | None = None
    in_identifier = False
    index = 0
    while index < len(sql):
        ch = sql[index]
        if current is not None:
            if ch == "'" and sql[index + 1 : index + 2] == "'":
                current.append("'")
                index += 2
                continue
            if ch == "'":
                literals.append("".join(current))
                current = None
                skeleton.append("'?'")
            else:
                current.append(ch)
        elif in_identifier:
            skeleton.append(ch)
            in_identifier = ch != '"'
        elif ch == "'":
            current = []
        else:
            skeleton.append(ch)
            in_identifier = ch == '"'
        index += 1
    if current is not None:  # an unterminated literal is nobody's proof
        skeleton.append("'" + "".join(current))
    return normalize_sql_for_cache("".join(skeleton)), literals


def prove_substitution(
    original_sql: str,
    candidate_sql: str,
    absent: str,
    present: str,
    validate: Callable[[str], object],
) -> bool:
    """Whether ``candidate_sql`` is ``original_sql`` with ``absent`` replaced by ``present``.

    True only when all of these hold: the original had the literal ``absent``;
    the candidate has ``present`` everywhere the original had ``absent`` and
    is otherwise the same literals in the same order; with literals blanked
    the two statements are identical; and ``validate`` (the guardrails every
    query passes) accepts the candidate. Only string literals are covered.
    """
    if absent == present:
        return False
    skeleton, literals = split_literals(original_sql)
    candidate_skeleton, candidate_literals = split_literals(candidate_sql)
    if absent not in literals or skeleton != candidate_skeleton:
        return False
    if [present if literal == absent else literal for literal in literals] != candidate_literals:
        return False
    try:
        validate(candidate_sql)
    except Exception:
        return False
    return True
