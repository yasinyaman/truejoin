"""Oracle through python-oracledb in thin mode: no Instant Client, no ODBC driver.

Thin mode is pure Python speaking Oracle Net, so an air-gapped site installs one
wheel and nothing else. Everything Oracle-specific (the catalog queries, the
identity-column insert, the session schema, row estimates) is the ODBC
adapter's, unchanged: this module only gives it a pool that behaves like
aioodbc's, so there is one Oracle implementation and two ways to reach the
server. ``type: oracle`` uses this one; a config naming an ODBC driver
(``options.driver``) or an ODBC ``connection_string`` uses ODBC
(``DatabaseFactory.create``).

What thin mode changes:

- Binds are ``:1, :2``, not ``?``. The statements this package writes use
  ``?``; they are renumbered here (``qmark_to_numeric``), never inside a string
  literal, a quoted name or a comment.
- ``NUMBER`` without a declared precision arrives as an exact ``Decimal``
  (an output type handler), where the ODBC driver hands over a double that has
  already lost digits past 2**53. CLOBs and BLOBs arrive as ``str`` and
  ``bytes``, like any other column.
- Each session is in autocommit mode, as the ODBC pool is.

``oracledb`` is imported lazily in :meth:`OracleAdapter.connect`, so the factory
and the unit tests work without the ``oracle`` extra.
"""

from __future__ import annotations

import decimal
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from truejoin.adapters.outbound.db.odbc import ODBCAdapter, session_identity_statement
from truejoin.domain.governance.session import SessionIdentity

logger = logging.getLogger(__name__)


def qmark_to_numeric(sql: str) -> str:
    """Renumber ``?`` placeholders as ``:1, :2, ...`` for python-oracledb.

    A ``?`` inside a single-quoted literal (with ``''`` escapes), a double-quoted
    identifier, a ``--`` comment or a ``/* */`` comment is text and stays as it is.
    """
    out: list[str] = []
    index, number, length = 0, 0, len(sql)
    while index < length:
        char = sql[index]
        if char in ("'", '"'):
            end = index + 1
            while end < length:
                if sql[end] == char:
                    if end + 1 < length and sql[end + 1] == char:  # '' or "" escape
                        end += 2
                        continue
                    break
                end += 1
            out.append(sql[index : end + 1])
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            out.append(sql[index:end])
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            out.append(sql[index:end])
            index = end
        elif char == "?":
            number += 1
            out.append(f":{number}")
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _output_types(oracledb: Any) -> Any:
    """The output type handler: exact numbers, and LOBs as plain values."""

    def handler(cursor: Any, metadata: Any) -> Any:
        type_code = metadata.type_code
        if type_code is oracledb.DB_TYPE_NUMBER:
            # NUMBER(p) with no scale is an integer and comes back as int; anything
            # else (a scale, or no declared precision at all) as an exact Decimal.
            if not (metadata.precision and metadata.scale == 0):
                return cursor.var(decimal.Decimal, arraysize=cursor.arraysize)
        elif type_code is oracledb.DB_TYPE_CLOB or type_code is oracledb.DB_TYPE_NCLOB:
            return cursor.var(oracledb.DB_TYPE_LONG, arraysize=cursor.arraysize)
        elif type_code is oracledb.DB_TYPE_BLOB:
            return cursor.var(oracledb.DB_TYPE_LONG_RAW, arraysize=cursor.arraysize)
        return None

    return handler


class _Cursor:
    """An oracledb cursor with the aioodbc surface the ODBC adapter calls."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._cursor.close()

    @property
    def description(self) -> Any:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    async def execute(self, sql: str, args: Sequence[Any] | None = None) -> None:
        statement = qmark_to_numeric(sql)
        if args:
            await self._cursor.execute(statement, list(args))
        else:
            await self._cursor.execute(statement)

    async def fetchall(self) -> list[Any]:
        rows: list[Any] = await self._cursor.fetchall()
        return rows

    async def fetchone(self) -> Any:
        return await self._cursor.fetchone()

    async def fetchmany(self, size: int) -> list[Any]:
        rows: list[Any] = await self._cursor.fetchmany(size)
        return rows


class _Connection:
    """An oracledb connection whose ``cursor()`` hands out :class:`_Cursor`."""

    def __init__(self, connection: Any) -> None:
        self.raw = connection

    def cursor(self) -> _Cursor:
        return _Cursor(self.raw.cursor())


class ThinPool:
    """An ``oracledb.AsyncConnectionPool`` behind the aioodbc pool surface.

    ``acquire()`` sets up each session as the ODBC pool's are (autocommit, the
    output type handler) before handing it out.
    """

    def __init__(self, pool: Any, oracledb: Any) -> None:
        self._pool = pool
        self._handler = _output_types(oracledb)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Connection]:
        async with self._pool.acquire() as connection:
            connection.autocommit = True
            connection.outputtypehandler = self._handler
            yield _Connection(connection)

    def close(self) -> None:
        """Nothing to do before ``wait_closed``: oracledb closes in one awaited call."""

    async def wait_closed(self) -> None:
        await self._pool.close(force=True)


def pool_arguments(config: Mapping[str, Any]) -> dict[str, Any]:
    """Keyword arguments for ``oracledb.create_pool_async`` from a database config.

    ``options.dsn`` is used verbatim (an Easy Connect string, a full descriptor
    or a ``tnsnames.ora`` alias with ``options.config_dir``); otherwise host,
    port and ``database`` (the service name) name the server.
    ``options.protocol: tcps`` turns on TLS.
    """
    options: Mapping[str, Any] = config.get("options") or {}
    pool_max = int(options.get("pool_size", 10))
    arguments: dict[str, Any] = {
        "user": config.get("username") or None,
        "password": config.get("password") or None,
        "min": min(int(options.get("pool_min_size", 2)), pool_max),
        "max": pool_max,
        "increment": 1,
    }
    if options.get("dsn"):
        arguments["dsn"] = str(options["dsn"])
    else:
        arguments["host"] = config.get("host") or "localhost"
        arguments["port"] = int(config.get("port") or 1521)
        if config.get("database"):
            arguments["service_name"] = str(config["database"])
    for key in ("protocol", "config_dir", "wallet_location"):
        if options.get(key):
            arguments[key] = str(options[key])
    return arguments


class OracleAdapter(ODBCAdapter):
    """Oracle through python-oracledb thin mode, with the ODBC adapter's Oracle logic."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__({**config, "type": "oracle"})

    async def connect(self) -> None:
        """Open the pool and run one statement, so a wrong password fails here."""
        try:
            import oracledb
        except ImportError as e:
            raise RuntimeError(
                "Oracle support needs the 'oracle' extra: pip install 'truejoin[oracle]'"
            ) from e
        pool = oracledb.create_pool_async(**pool_arguments(self.config))
        self._pool = ThinPool(pool, oracledb)
        try:
            rows, _ = await self._execute("SELECT USER AS username FROM DUAL")
        except BaseException:
            await self.disconnect()
            raise
        if self._resolve_schema and rows:
            self._schema = str(next(iter(rows[0].values()))).upper()
            logger.debug("Oracle schema resolved from the session: %s", self._schema)

    async def _apply_identity(self, conn: Any, identity: SessionIdentity | None) -> None:
        """The client identifier as a session attribute; the context procedure as a call.

        python-oracledb sends ``client_identifier`` with the session's next round
        trip, so the identifier costs no statement of its own; an application
        context still needs its procedure called.
        """
        config = self._session_identity
        assert config is not None
        if config.client_identifier is not None:
            value = identity.value(config.client_identifier) if identity is not None else None
            conn.raw.client_identifier = value or ""
        if config.context_procedure is not None:
            sql, args = session_identity_statement(
                replace(config, client_identifier=None), identity, mssql=False
            )
            async with conn.cursor() as cur:
                await cur.execute(sql, args)

    @staticmethod
    def _oracle_column(col: Mapping[str, Any], is_pk: bool) -> dict[str, Any]:
        """As ODBC's, but exact: thin mode returns an undeclared NUMBER as a Decimal."""
        column = ODBCAdapter._oracle_column(col, is_pk)
        column["inexact"] = False
        return column
