"""Saved analyses: a question and the SQL that answers it, reviewed like a measure.

A model writes better SQL when it sees how this data is asked about: which
table holds the amount, how a status is spelled, what "active" means here.
An example is a steward's answer to one such question, written in the
engine's SQL (``source.table`` names, as ``query_data`` takes them), and like
an expression it is offered to nobody until it is certified.

Its SQL is shown to the models that call ``find_examples``. A literal in it
may be another tenant's value (``WHERE tenant = 'b'``, ``WHERE tenant_id =
42``), so a caller with row rules or masks is never shown an example that has
one, there or through the catalog routes.
"""

import re

from pydantic import BaseModel, Field

from trio.domain.catalog.localized import LocalizedText


class ExampleError(ValueError):
    """An example that cannot be saved, with the reason why."""


class QueryExample(BaseModel):
    """One question and the SQL a reviewer says answers it."""

    name: str = Field(..., description="An identifier, unique in the catalog")
    question: LocalizedText = Field(default_factory=LocalizedText)
    sql: str = Field(..., description="A SELECT over this catalog's tables, as query_data runs it")
    description: LocalizedText = Field(default_factory=LocalizedText)
    #: The tables it reads, as ``source.table``; found when it was saved.
    tables: list[str] = Field(default_factory=list)
    certified: bool = False

    @property
    def has_literals(self) -> bool:
        """Whether a value appears in it: one a restricted caller must not see.

        A quoted string, a dollar-quoted one (``$$acme$$``), or a number that
        stands alone (``tenant_id = 42``); a row count after LIMIT, OFFSET,
        TOP or FETCH and a column position in GROUP BY or ORDER BY are not. Conservative on purpose: a quote or a
        number anywhere else counts, in a comment or a constant like
        ``ROUND(x, 2)`` too, so a doubt hides the example rather than shows it.
        """
        return has_literals(self.sql)


#: A quote, or a dollar quote: ``$$`` or ``$tag$``.
_QUOTED = re.compile(r"'|\$\w*\$")
#: A number that is not part of a name (``t1``, ``"col 2"`` aside) nor a bind (``$1``).
_NUMBER = re.compile(r"(?<![\w.$])\d+(?:\.\d+)?(?:e[+-]?\d+)?(?!\w)", re.IGNORECASE)
#: Row counts, which are not data.
_ROW_COUNT = re.compile(
    r"\b(?:LIMIT|OFFSET|TOP|FETCH\s+(?:FIRST|NEXT))\s+\d+(?:\s*,\s*\d+)?", re.IGNORECASE
)


#: A GROUP BY or ORDER BY list, up to whatever clause ends it.
_BY_LIST = re.compile(
    r"\b(?:GROUP|ORDER)\s+BY\b.*?"
    r"(?=\b(?:HAVING|ORDER|LIMIT|OFFSET|FETCH|WINDOW|UNION|INTERSECT|EXCEPT)\b|\)|;|$)",
    re.IGNORECASE | re.DOTALL,
)
#: An item of such a list that is only a position (``GROUP BY 1``, ``ORDER BY 2 DESC``).
_POSITION = re.compile(
    r"(?:(?<=\bBY)|(?<=,))\s*\d+\s*(?=,|$|\s+(?:ASC|DESC|NULLS)\b)", re.IGNORECASE
)


def has_literals(sql: str) -> bool:
    """Whether ``sql`` quotes a string or names a number that is data.

    Row counts (``LIMIT 10``) and column positions (``GROUP BY 1``) are not.
    """
    if _QUOTED.search(sql):
        return True
    rest = _ROW_COUNT.sub(" ", sql)
    rest = _BY_LIST.sub(lambda m: _POSITION.sub(" pos", m.group(0)), rest)
    return bool(_NUMBER.search(rest))
