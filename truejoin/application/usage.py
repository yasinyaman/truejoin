"""What the tool calls in the audit trail add up to: how often each was answered, and on what.

Counted here, over every call, rather than in the page over the newest few
hundred it could fetch: a summary of 500 calls read as the whole history.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable
from typing import Any

#: Lists of names each call carries, tallied across calls.
TALLIES = ("measures", "dimensions", "filtered_columns", "glossary")

#: Distinct question texts counted; beyond this, new ones are not.
MAX_QUESTIONS = 5000


def summarize(
    calls: Iterable[dict[str, Any]], top: int = 10, unanswered: int = 100
) -> dict[str, Any]:
    """Counts by outcome, the most frequent tools, tables and names, and the newest misses.

    ``calls`` are ``tool_call`` events oldest first, as the audit reader gives them.
    ``asked`` maps each kept question text to ``[calls, calls not answered]``;
    it is for working out vocabulary gaps, not for showing as it is. A question
    the built-in assistant put to a tool is the model's wording, not a person's,
    and stays out of it; ``by_assistant`` counts the calls it made.
    """
    outcomes: Counter[str] = Counter()
    tallies: dict[str, Counter[str]] = {name: Counter() for name in ("tools", "tables", *TALLIES)}
    missed: deque[dict[str, Any]] = deque(maxlen=unanswered)
    first = last = None
    total = by_assistant = 0
    questions: dict[str, list[int]] = {}
    for call in calls:
        total += 1
        first = first or call.get("at")
        last = call.get("at") or last
        outcome = str(call.get("outcome", ""))
        outcomes[outcome] += 1
        tallies["tools"][str(call.get("tool", ""))] += 1
        if call.get("table"):
            tallies["tables"][str(call["table"])] += 1
        for name in TALLIES:
            tallies[name].update(str(v) for v in call.get(name) or [])
        assistant = call.get("via") == "assistant"
        by_assistant += assistant
        words = None if assistant else call.get("question")
        if words and (words in questions or len(questions) < MAX_QUESTIONS):
            # shortcut: distinct questions past MAX_QUESTIONS go uncounted; stream to a store if trails get that varied.
            tally = questions.setdefault(str(words), [0, 0])
            tally[0] += 1
            tally[1] += outcome != "answered"
        if outcome != "answered":
            missed.append(call)
    return {
        "calls": total,
        "by_assistant": by_assistant,
        "from": first,
        "to": last,
        "outcomes": {
            name: outcomes[name] for name in ("answered", "empty", "refused", "failed", "error")
        },
        **{name: counter.most_common(top) for name, counter in tallies.items()},
        "questions_kept": bool(questions),
        "asked": questions,
        "unanswered": list(reversed(missed)),
    }
