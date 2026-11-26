"""The rewriter against random queries: each answers as if the caller's view were the table.

Every generated query runs twice for each of eight callers: rewritten, on the
raw tables (which are also reachable under every name the query could use, so
a reference the rewriter missed would read raw rows), and as written, on
tables that hold only that caller's rows with the masks applied in Python.
The two answers must be the same multiset of rows.

The grammar (``tests/fusion/sql_fuzz.py``) writes only what the rewriter
should wrap, so a refusal here fails too: this also holds the rewriter to
not refusing more than it must.

A deep run: ``TRUEJOIN_FUZZ_EXAMPLES=5000 uv run --frozen pytest -k fuzz``.
"""

from __future__ import annotations

from functools import cache
from typing import Any

import duckdb
from hypothesis import assume, given

from tests.fusion.sql_fuzz import (
    HASH_KEY,
    ROWS,
    SCHEMA,
    SCOPES,
    Scope,
    as_bag,
    column_types,
    expected_tables,
    fuzz_settings,
    load,
    queries,
)
from truejoin.adapters.outbound.sqlglot_access import SqlglotAccessRewriter
from truejoin.domain.governance.access import PSEUDONYM_FUNCTION, TableView
from truejoin.domain.governance.masking import mask_value
from truejoin.domain.olap.models import TableRef

REWRITER = SqlglotAccessRewriter()


def _raw_types(table: str) -> list[tuple[str, str]]:
    return [(c, "INTEGER" if kind == "int" else "VARCHAR") for c, kind in SCHEMA[table]]


BY_NAME = {scope.name: scope for scope in SCOPES}


@cache
def raw_engine() -> Any:
    """The real rows, under ``db.<t>`` and ``<t>`` both, with the keyed pseudonym."""
    conn = duckdb.connect()
    load(conn, ROWS, _raw_types)
    conn.create_function(
        PSEUDONYM_FUNCTION, lambda text: mask_value(text, "hash", HASH_KEY), ["VARCHAR"], "VARCHAR"
    )
    return conn


@cache
def expected_engine(name: str) -> Any:
    """The caller's view of each table, built without the rewriter."""
    scope = BY_NAME[name]
    conn = duckdb.connect()
    load(conn, expected_tables(scope), lambda table: column_types(scope, table))
    return conn


def views(scope: Scope) -> dict[TableRef, TableView]:
    """What the query service hands the rewriter: each name the query may use, one view."""
    out: dict[TableRef, TableView] = {}
    for table, columns in SCHEMA.items():
        view = TableView(
            f"db.{table}",
            tuple(column for column, _ in columns),
            scope.conditions_for(table),
            scope.masks_for(table),
        )
        out[TableRef("db", table)] = out[TableRef("", table)] = view
    return out


def answers(sql: str, scope: Scope) -> tuple[Any, Any, str]:
    try:
        want = expected_engine(scope.name).execute(sql).fetchall()
    except duckdb.Error:
        assume(False)  # the grammar wrote something DuckDB does not run
    secured = REWRITER.secure(sql, views(scope))
    return raw_engine().execute(secured).fetchall(), want, secured


@fuzz_settings(examples=40)
@given(sql=queries())
def test_a_restricted_query_answers_as_the_callers_view_would(sql):
    for scope in SCOPES:
        got, want, secured = answers(sql, scope)
        assert as_bag(got) == as_bag(want), (
            f"caller {scope.name}\n  query:     {sql}\n  rewritten: {secured}"
        )
