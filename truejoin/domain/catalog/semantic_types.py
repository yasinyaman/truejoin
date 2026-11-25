"""The semantic types a column can be labelled with, and what a reviewer is told about one.

A semantic type decides masking: ``masking.rules`` maps a type to a strategy,
matched exactly. So a type typed as ``Email`` or ``e-mail`` is masked by no
rule, silently. The console offers these names, and an edit that sets a type
neither listed here nor named by a masking rule comes back with a warning.
"""

from __future__ import annotations

from collections.abc import Iterable

#: What the analysis model is asked to choose from, in the prompt's order.
MODEL_TYPES: tuple[str, ...] = (
    "email",
    "phone",
    "amount",
    "status",
    "url",
    "date",
    "id",
    "national_id",
    "name",
    "address",
    "flag",
    "count",
    "percentage",
    "code",
    "json",
)

#: Every type the console offers: the model's, and the Turkish identity number by its own name.
SEMANTIC_TYPES: tuple[str, ...] = (*MODEL_TYPES, "tc_no")


def type_warning(semantic_type: str | None, masked: Iterable[str]) -> str:
    """Why a reviewer may not mean ``semantic_type``; empty when nothing is wrong.

    ``masked`` are the types the masking rules name (any role).
    """
    if not semantic_type:
        return ""
    named = {*SEMANTIC_TYPES, *masked}
    if semantic_type in named:
        return ""
    close = next((n for n in sorted(named) if n.casefold() == semantic_type.casefold()), None)
    hint = f" Did you mean '{close}'?" if close else ""
    return (
        f"'{semantic_type}' is not a semantic type this server knows, and no masking rule "
        f"names it: if it holds personal data, it will not be masked.{hint}"
    )
