"""Port: rewriting a query so it can only read what its caller may see."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from truejoin.domain.governance.access import TableView
from truejoin.domain.olap.models import TableRef


class AccessRewriter(Protocol):
    def secure(self, sql: str, views: Mapping[TableRef, TableView]) -> str:
        """``sql`` with every table it reads replaced by that table's view.

        ``views`` is keyed by the reference as the query writes it (an
        unqualified name has an empty ``source``). The result runs on the
        analytics store as it is.

        Implementations must fail closed: a table reference that is neither a
        key of ``views`` nor a CTE of the query itself, or anything in a FROM
        clause they do not understand, raises ``GuardrailViolation`` rather
        than passing through unwrapped.
        """
        ...
