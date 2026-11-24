"""What a key with given row rules would see: rows per table, before it is made.

A key cannot be edited once it is handed out, so the administrator sees first
how many rows of each table its rules leave. Each count is an ordinary
analytical read: run under a scope, recorded as one audit event whose actor is
the administrator who asked (never the key, which does not exist yet). Only
counts come back, never a value.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from truejoin.application.olap.query import QueryService
from truejoin.domain.governance.access import AccessScope
from truejoin.domain.olap.errors import FusionError
from truejoin.domain.olap.models import TableRef

#: The most tables one preview counts: each costs two reads.
MAX_TABLES = 20


def preview_tables(
    query: QueryService, tables: Sequence[TableRef], previewed: AccessScope, own: AccessScope
) -> list[dict[str, Any]]:
    """Per table: the rows ``previewed`` would see, out of those ``own`` sees.

    ``error`` says why a count could not be made (the rewriter refused it, a
    table too large to count); the other table counts still come back.
    """
    rows: list[dict[str, Any]] = []
    for ref in tables[:MAX_TABLES]:
        row: dict[str, Any] = {"table": ref.full_name, "visible": None, "total": None, "error": ""}
        sql = f"SELECT COUNT(*) AS n FROM {ref.full_name}"
        for field, scope in (("visible", previewed), ("total", own)):
            try:
                row[field] = int(query.sql(sql, use_cache=False, scope=scope).rows[0][0])
            except FusionError as e:
                row["error"] = str(e)
                break
        rows.append(row)
    return rows
