"""Reading a database's statement history and view definitions.

Each engine keeps these in a different place, behind a different grant:

- PostgreSQL: the ``pg_stat_statements`` extension (it must be in
  ``shared_preload_libraries`` and created in the database; ``pg_read_all_stats``
  shows other users' statements). It stores statements with their constants
  replaced by ``$1``, so only expressions without a literal survive mining.
- SQL Server: ``sys.dm_exec_query_stats`` (``VIEW SERVER STATE``, or
  ``VIEW DATABASE STATE`` on Azure SQL), the plan cache since the last restart.
- Oracle: ``V$SQL`` (``SELECT`` on ``V_$SQL``), the shared pool.
- MySQL: ``performance_schema.events_statements_summary_by_digest`` (``SELECT``
  on ``performance_schema``), since the server started. MySQL 8 keeps one real
  statement per digest, literals and all (``QUERY_SAMPLE_TEXT``, cut at
  ``performance_schema_max_sql_text_length``); MariaDB has only the digest,
  constants replaced by ``?`` as PostgreSQL replaces them by ``$1``.

View definitions are read everywhere, and keep their literals.
"""

import logging
from dataclasses import dataclass
from typing import Any

from truejoin.adapters.outbound.db.dialect import Dialect, dialect_or_generic
from truejoin.domain.catalog.history import HistoryEntry, HistoryUnavailableError
from truejoin.ports.database import SqlReader

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryQueries:
    """Where one engine keeps its history.

    ``statements`` may name ``:limit``, ``:schema`` and ``:database`` and
    returns ``query``, ``calls`` and optionally ``last_seen``; ``views`` may
    name ``:schema`` and ``:database`` and returns ``name`` and
    ``definition``. ``needs`` is what to tell someone whose database refused.
    """

    statements: str | None
    views: str | None
    needs: str
    #: Tried when ``statements`` fails: the same rows from an older server.
    statements_fallback: str | None = None


_MYSQL_STATEMENTS = """
SELECT {query} AS query, COUNT_STAR AS calls, LAST_SEEN AS last_seen
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME = :database
  AND (DIGEST_TEXT LIKE 'SELECT%' OR DIGEST_TEXT LIKE 'WITH%')
ORDER BY COUNT_STAR DESC
LIMIT :limit
"""

QUERIES: dict[str, HistoryQueries] = {
    "postgresql": HistoryQueries(
        statements="""
SELECT s.query AS query, s.calls AS calls
FROM pg_stat_statements s
JOIN pg_database d ON d.oid = s.dbid
WHERE d.datname = current_database()
  AND (lower(ltrim(s.query)) LIKE 'select%' OR lower(ltrim(s.query)) LIKE 'with%')
ORDER BY s.calls DESC
LIMIT :limit
""",
        views="""
SELECT table_name AS name, view_definition AS definition
FROM information_schema.views
WHERE table_schema = :schema AND view_definition IS NOT NULL
""",
        needs="the pg_stat_statements extension (shared_preload_libraries, then CREATE "
        "EXTENSION pg_stat_statements) and, to see other users' statements, "
        "GRANT pg_read_all_stats",
    ),
    "mysql": HistoryQueries(
        statements=_MYSQL_STATEMENTS.format(query="COALESCE(QUERY_SAMPLE_TEXT, DIGEST_TEXT)"),
        statements_fallback=_MYSQL_STATEMENTS.format(query="DIGEST_TEXT"),  # MariaDB
        views="""
SELECT TABLE_NAME AS name, VIEW_DEFINITION AS definition
FROM information_schema.VIEWS
WHERE TABLE_SCHEMA = :database
""",
        needs="performance_schema switched on (the default on MySQL, not on MariaDB) "
        "and GRANT SELECT ON performance_schema.* for the statement history",
    ),
    # dm_exec_sql_text reports no database for ad-hoc statements; the plan's
    # own `dbid` attribute does.
    "mssql": HistoryQueries(
        statements="""
SELECT TOP (:limit) t.text AS query, s.execution_count AS calls,
       s.last_execution_time AS last_seen
FROM sys.dm_exec_query_stats s
CROSS APPLY sys.dm_exec_sql_text(s.sql_handle) t
CROSS APPLY sys.dm_exec_plan_attributes(s.plan_handle) a
WHERE a.attribute = 'dbid' AND CAST(a.value AS int) = DB_ID()
ORDER BY s.execution_count DESC
""",
        views="""
SELECT o.name AS name, m.definition AS definition
FROM sys.sql_modules m
JOIN sys.objects o ON o.object_id = m.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.type = 'V' AND s.name = :schema AND m.definition IS NOT NULL
""",
        needs="GRANT VIEW SERVER STATE (VIEW DATABASE STATE on Azure SQL) for the "
        "statement history, VIEW DEFINITION for the views",
    ),
    # shortcut: AWR (DBA_HIST_SQLTEXT) reaches further back than the shared
    # pool, but querying it needs the Diagnostics Pack licence; it is left
    # alone until a customer who holds one asks.
    "oracle": HistoryQueries(
        statements="""
SELECT DBMS_LOB.SUBSTR(SQL_FULLTEXT, 4000, 1) AS "query", EXECUTIONS AS "calls",
       LAST_ACTIVE_TIME AS "last_seen"
FROM V$SQL
WHERE PARSING_SCHEMA_NAME = :schema AND COMMAND_TYPE = 3
ORDER BY EXECUTIONS DESC
FETCH FIRST :limit ROWS ONLY
""",
        views="""
SELECT VIEW_NAME AS "name", TEXT_VC AS "definition"
FROM ALL_VIEWS
WHERE OWNER = :schema AND TEXT_VC IS NOT NULL
""",
        needs="GRANT SELECT ON V_$SQL for the statement history",
    ),
}


class QueryHistoryReader:
    """``QueryHistorySource`` over a gateway's ``execute_query``."""

    def __init__(
        self,
        adapter: SqlReader,
        db_type: str | Dialect = "postgresql",
        schema: str = "public",
        database: str = "",
    ) -> None:
        """Store the adapter and the dialect/schema/database to read."""
        self.adapter = adapter
        self.db_type = dialect_or_generic(db_type).name
        self.schema = schema
        self.database = database or schema
        self.queries = QUERIES.get(self.db_type)

    def _params(self, query: str, **extra: object) -> dict[str, object]:
        """The values ``query`` names: the gateway refuses a parameter nothing uses."""
        values = {"schema": self.schema, "database": self.database, **extra}
        return {name: value for name, value in values.items() if f":{name}" in query}

    def _unavailable(self, what: str, cause: object = "") -> HistoryUnavailableError:
        if self.queries is None:
            return HistoryUnavailableError(f"No {what} is read from a {self.db_type} database.")
        # A driver's error arrives as a padded tuple; keep what a person can read.
        said = " ".join(str(cause).replace("\x00", "").split())[:240]
        because = f" ({said})" if said else ""
        return HistoryUnavailableError(
            f"The {what} of this {self.db_type} database cannot be read{because}. "
            f"It needs {self.queries.needs}."
        )

    async def recent_statements(self, limit: int = 500) -> list[HistoryEntry]:
        """The most-run SELECT statements, most frequent first."""
        if self.queries is None or self.queries.statements is None:
            raise self._unavailable("statement history")
        rows: list[dict[str, Any]] | None = None
        error: Exception | None = None
        for query in filter(None, (self.queries.statements, self.queries.statements_fallback)):
            try:
                rows = await self.adapter.execute_query(
                    query, self._params(query, limit=int(limit))
                )
                break
            except Exception as e:
                error = e  # the last refusal: the fallback's, on the server it was meant for
        if rows is None:
            raise self._unavailable("statement history", error) from error
        return [
            HistoryEntry(
                sql=str(row["query"]),
                calls=int(row.get("calls") or 1),
                last_seen=row.get("last_seen"),
            )
            for row in rows
            if row.get("query")
        ]

    async def view_definitions(self) -> list[HistoryEntry]:
        """The definition of every view in the configured schema."""
        if self.queries is None or self.queries.views is None:
            raise self._unavailable("view definitions")
        try:
            rows = await self.adapter.execute_query(
                self.queries.views, self._params(self.queries.views)
            )
        except Exception as e:
            raise self._unavailable("view definitions", e) from e
        return [
            HistoryEntry(sql=str(row["definition"]), origin="view", name=str(row["name"]))
            for row in rows
            if row.get("definition")
        ]
