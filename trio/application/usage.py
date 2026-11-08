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


def summarize(
    calls: Iterable[dict[str, Any]], top: int = 10, unanswered: int = 100
) -> dict[str, Any]:
    """Counts by outcome, the most frequent tools, tables and names, and the newest misses.

    ``calls`` are ``tool_call`` events oldest first, as the audit reader gives them.
    """
    outcomes: Counter[str] = Counter()
    tallies: dict[str, Counter[str]] = {name: Counter() for name in ("tools", "tables", *TALLIES)}
    missed: deque[dict[str, Any]] = deque(maxlen=unanswered)
    first = last = None
    total = 0
    asked = False  # whether any call kept the words of its question
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
        asked = asked or "question" in call
        if outcome != "answered":
            missed.append(call)
    return {
        "calls": total,
        "from": first,
        "to": last,
        "outcomes": {
            name: outcomes[name] for name in ("answered", "empty", "refused", "failed", "error")
        },
        **{name: counter.most_common(top) for name, counter in tallies.items()},
        "questions_kept": asked,
        "unanswered": list(reversed(missed)),
    }
