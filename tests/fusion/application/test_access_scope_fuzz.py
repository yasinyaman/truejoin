"""Random queries through the whole path: validation, planning, slices, the rewrite, DuckDB.

The rewriter's own fuzz test (``test_sqlglot_access_fuzz.py``) holds the
rewrite; this one holds what happens around it. Two engines, each kept for
the whole module so that what one caller loaded is there for the next:

- ``whole``: every table small enough to load whole, and nested queries of
  every shape;
- ``sliced``: nothing over five rows loads whole, so a caller's rows come in
  slices and by key; flat SELECTs, the shapes the planner can slice.

Each answer must equal the caller's view computed in Python, or be one of the
planner's own refusals (a table or a slice too big). Anything else fails:
an error from the engine means a slice did not hold what the query needed.

A deep run: ``TRUEJOIN_FUZZ_EXAMPLES=5000 uv run --frozen pytest -k fuzz``.
"""

from __future__ import annotations

import tempfile
from functools import cache
from typing import Any

import duckdb
from hypothesis import assume, given

from tests.fusion.fakes import FakeSourceFactory, ManualScheduler
from tests.fusion.sql_fuzz import (
    HASH_KEY,
    SCOPES,
    access_scope,
    as_bag,
    column_types,
    expected_tables,
    fuzz_settings,
    load,
    queries,
    source_tables,
)
from truejoin.application.olap.settings import Settings
from truejoin.bootstrap import build_app
from truejoin.domain.governance.access import PSEUDONYM_FUNCTION
from truejoin.domain.governance.masking import mask_value
from truejoin.domain.olap.errors import QueryError

#: What the planner and the keyed fetch say when they will not load something (too many
#: rows, keys or slices); any other failure is a bug.
PLANNER_REFUSALS = (
    "Refusing to load",
    "Refusing to keep fetching",
    "Refusing to fetch",
    "Slice budget exhausted",
)

ROW_LIMITS = {"whole": 100_000, "sliced": 5}


BY_NAME = {scope.name: scope for scope in SCOPES}


@cache
def engine(name: str) -> Any:
    limit = ROW_LIMITS[name]
    settings = Settings(
        threads=1,
        memory_limit="256MB",
        backup_path=tempfile.mkdtemp(prefix="truejoin-fuzz-"),
        full_load_max_rows=limit,
        slice_max_rows=limit,
        semi_join_max_keys=limit,
    )
    app = build_app(settings, scheduler=ManualScheduler(), source_factory=FakeSourceFactory())
    app.sources.connect("db", {"type": "fake", "tables": source_tables()})
    app.store.define_function(PSEUDONYM_FUNCTION, lambda text: mask_value(text, "hash", HASH_KEY))
    return app


@cache
def expected_engine(name: str) -> Any:
    scope = BY_NAME[name]
    conn = duckdb.connect()
    load(conn, expected_tables(scope), lambda table: column_types(scope, table))
    return conn


@cache
def scope_of(name: str) -> Any:
    return access_scope(BY_NAME[name])


def check(name: str, sql: str) -> None:
    app = engine(name)
    for scope in SCOPES:
        try:
            want = expected_engine(scope.name).execute(sql).fetchall()
        except duckdb.Error:
            assume(False)
        try:
            got = app.query.sql(sql, scope=scope_of(scope.name), use_cache=False).rows
        except QueryError as e:
            assert str(e).startswith(PLANNER_REFUSALS), f"caller {scope.name}\n  {sql}\n  {e}"
            continue
        assert as_bag(got) == as_bag(want), f"caller {scope.name} on {name}\n  {sql}"


@fuzz_settings(examples=15)
@given(sql=queries())
def test_any_query_answers_as_the_callers_view_would(sql):
    check("whole", sql)


@fuzz_settings(examples=80)
@given(sql=queries(flat=True))
def test_slices_and_keyed_fetches_change_no_answer(sql):
    check("sliced", sql)
