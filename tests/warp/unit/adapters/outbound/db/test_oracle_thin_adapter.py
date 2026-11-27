"""OracleAdapter over a fake python-oracledb (no Oracle, no driver needed).

The adapter is the ODBC adapter's Oracle profile on a pool that looks like
aioodbc's; these tests hold that seam: binds renumbered, sessions set up, the
schema read from the session, exact numbers, and the factory's choice between
thin mode and ODBC.
"""

from __future__ import annotations

import decimal
import logging
import sys
import types
from contextlib import asynccontextmanager
from typing import Any

import pytest

from truejoin.adapters.outbound.db.factory import DatabaseFactory
from truejoin.adapters.outbound.db.odbc import ODBCAdapter
from truejoin.adapters.outbound.db.oracle import (
    OracleAdapter,
    ThinPool,
    pool_arguments,
    qmark_to_numeric,
)

CONFIG: dict[str, Any] = {
    "name": "ora",
    "type": "oracle",
    "host": "db.example",
    "port": 1521,
    "database": "FREEPDB1",
    "username": "warp",
    "password": "s3cret",
}


# --- a fake python-oracledb -----------------------------------------------------------


class FakeVar:
    def __init__(self, kind: Any, arraysize: int) -> None:
        self.kind, self.arraysize = kind, arraysize


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description: list[tuple[Any, ...]] | None = None
        self.rowcount = 0
        self.arraysize = 100
        self._rows: list[tuple[Any, ...]] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def var(self, kind: Any, arraysize: int) -> FakeVar:
        return FakeVar(kind, arraysize)

    async def execute(self, sql: str, params: Any = None) -> None:
        pool = self.connection.pool
        pool.executed.append((sql, params, id(self.connection)))
        result = pool.results.pop(0) if pool.results else ([], [])
        if isinstance(result, Exception):
            raise result
        columns, rows = result
        self.description = [(name,) for name in columns] if columns else None
        self._rows = list(rows)
        self.rowcount = len(rows)

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    async def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch


class FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self.autocommit = False
        self.outputtypehandler: Any = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


class FakePool:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.results: list[Any] = []
        self.executed: list[tuple[str, Any, int]] = []
        self.connections: list[FakeConnection] = []
        self.closed_with: dict[str, Any] | None = None

    @asynccontextmanager
    async def acquire(self):
        connection = FakeConnection(self)
        self.connections.append(connection)
        yield connection

    async def close(self, **kwargs: Any) -> None:
        self.closed_with = kwargs


def fake_oracledb() -> types.ModuleType:
    module = types.ModuleType("oracledb")
    for name in ("DB_TYPE_NUMBER", "DB_TYPE_CLOB", "DB_TYPE_NCLOB", "DB_TYPE_BLOB"):
        setattr(module, name, object())
    module.DB_TYPE_LONG = "LONG"
    module.DB_TYPE_LONG_RAW = "LONG_RAW"
    module.pools = []

    def create_pool_async(**kwargs: Any) -> FakePool:
        pool = FakePool(**kwargs)
        pool.results.extend(module.pending)
        module.pools.append(pool)
        return pool

    module.create_pool_async = create_pool_async
    module.pending = []
    return module


@pytest.fixture
def oracledb(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = fake_oracledb()
    monkeypatch.setitem(sys.modules, "oracledb", module)
    return module


# --- binds ----------------------------------------------------------------------------


class TestPlaceholders:
    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = :1 AND b = :2"),
            ("SELECT 'what?' FROM t WHERE a = ?", "SELECT 'what?' FROM t WHERE a = :1"),
            ("SELECT 'it''s ?' FROM t WHERE a = ?", "SELECT 'it''s ?' FROM t WHERE a = :1"),
            ('SELECT "odd?name" FROM t WHERE a = ?', 'SELECT "odd?name" FROM t WHERE a = :1'),
            ("SELECT a -- why?\nFROM t WHERE a = ?", "SELECT a -- why?\nFROM t WHERE a = :1"),
            (
                "SELECT /* ? */ a FROM t WHERE a IN (?, ?)",
                "SELECT /* ? */ a FROM t WHERE a IN (:1, :2)",
            ),
            ("SELECT 1 FROM DUAL", "SELECT 1 FROM DUAL"),
        ],
    )
    def test_qmarks_become_numbers_outside_text(self, sql, expected):
        assert qmark_to_numeric(sql) == expected


# --- configuration --------------------------------------------------------------------


class TestPoolArguments:
    def test_host_port_and_service(self):
        arguments = pool_arguments(CONFIG)
        assert arguments["host"] == "db.example" and arguments["port"] == 1521
        assert arguments["service_name"] == "FREEPDB1"
        assert (arguments["user"], arguments["password"]) == ("warp", "s3cret")
        assert (arguments["min"], arguments["max"]) == (2, 10)
        assert "dsn" not in arguments

    def test_a_dsn_is_used_verbatim(self):
        options = {"dsn": "tcps://db.example:2484/svc?wallet_location=/w", "pool_size": 4}
        arguments = pool_arguments({**CONFIG, "options": options})
        assert arguments["dsn"] == options["dsn"]
        assert "host" not in arguments and arguments["max"] == 4

    def test_tls_and_wallets_pass_through(self):
        options = {"protocol": "tcps", "wallet_location": "/w", "config_dir": "/tns"}
        arguments = pool_arguments({**CONFIG, "options": options})
        assert arguments["protocol"] == "tcps"
        assert (arguments["wallet_location"], arguments["config_dir"]) == ("/w", "/tns")

    def test_the_minimum_never_exceeds_the_maximum(self):
        arguments = pool_arguments({**CONFIG, "options": {"pool_size": 1}})
        assert (arguments["min"], arguments["max"]) == (1, 1)


class TestFactory:
    def test_oracle_is_thin_mode(self):
        assert type(DatabaseFactory.create(CONFIG)) is OracleAdapter

    @pytest.mark.parametrize(
        "options",
        [{"driver": "Oracle 23 ODBC driver"}, {"connection_string": "DSN=ora"}],
    )
    def test_an_odbc_driver_or_connection_string_keeps_odbc(self, options):
        adapter = DatabaseFactory.create({**CONFIG, "options": options})
        assert type(adapter) is ODBCAdapter
        assert adapter.dialect.name == "oracle"


# --- connecting -----------------------------------------------------------------------


class TestConnect:
    @pytest.mark.asyncio
    async def test_the_schema_is_read_from_the_session(self, oracledb):
        oracledb.pending.append((["USERNAME"], [("warp",)]))
        adapter = OracleAdapter(CONFIG)
        await adapter.connect()
        pool = oracledb.pools[0]
        assert pool.kwargs["service_name"] == "FREEPDB1"
        assert pool.executed[0][0] == "SELECT USER AS username FROM DUAL"
        assert adapter.introspection_schema == "WARP"

    @pytest.mark.asyncio
    async def test_a_configured_schema_wins(self, oracledb):
        oracledb.pending.append((["USERNAME"], [("warp",)]))
        adapter = OracleAdapter({**CONFIG, "options": {"schema": "app"}})
        await adapter.connect()
        assert adapter.introspection_schema == "APP"

    @pytest.mark.asyncio
    async def test_a_session_that_cannot_run_fails_the_connect_and_closes_the_pool(self, oracledb):
        oracledb.pending.append(RuntimeError("ORA-01017: invalid username/password"))
        adapter = OracleAdapter(CONFIG)
        with pytest.raises(RuntimeError, match="ORA-01017"):
            await adapter.connect()
        assert oracledb.pools[0].closed_with == {"force": True}
        assert not adapter.is_connected

    @pytest.mark.asyncio
    async def test_the_missing_extra_is_explained(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(sys.modules, "oracledb", None)
        with pytest.raises(RuntimeError, match=r"truejoin\[oracle\]"):
            await OracleAdapter(CONFIG).connect()

    @pytest.mark.asyncio
    async def test_disconnect_closes_the_pool(self, oracledb):
        oracledb.pending.append((["USERNAME"], [("warp",)]))
        adapter = OracleAdapter(CONFIG)
        await adapter.connect()
        await adapter.disconnect()
        assert oracledb.pools[0].closed_with == {"force": True}
        assert not adapter.is_connected


async def connected(oracledb: types.ModuleType, *results: Any) -> tuple[OracleAdapter, FakePool]:
    oracledb.pending[:] = [(["USERNAME"], [("warp",)]), *results]
    adapter = OracleAdapter(CONFIG)
    await adapter.connect()
    return adapter, oracledb.pools[0]


class TestStatements:
    @pytest.mark.asyncio
    async def test_raw_sql_binds_by_number(self, oracledb):
        adapter, pool = await connected(oracledb, (["ID"], [(7,)]))
        rows = await adapter.execute_query(
            "SELECT id FROM orders WHERE status = :status AND note <> 'a?'",
            {"status": "paid"},
        )
        assert rows == [{"ID": 7}]
        sql, params, _ = pool.executed[-1]
        assert sql == "SELECT id FROM orders WHERE status = :1 AND note <> 'a?'"
        assert params == ["paid"]

    @pytest.mark.asyncio
    async def test_every_session_is_autocommit_with_exact_numbers(self, oracledb):
        adapter, pool = await connected(oracledb, (["N"], [(1,)]))
        await adapter.execute_query("SELECT 1 AS n FROM DUAL")
        for connection in pool.connections:
            assert connection.autocommit is True
            assert connection.outputtypehandler is not None

    @pytest.mark.asyncio
    async def test_a_stream_reads_in_batches(self, oracledb):
        adapter, _ = await connected(oracledb, (["ID"], [(1,), (2,), (3,)]))
        batches = [b async for b in adapter.stream_select("orders", batch_size=2)]
        assert batches == [[{"ID": 1}, {"ID": 2}], [{"ID": 3}]]

    @pytest.mark.asyncio
    async def test_an_identity_insert_reads_the_sequence_on_the_same_session(self, oracledb):
        adapter, pool = await connected(
            oracledb,
            (["column_name", "sequence_name"], [("ID", "ISEQ$$_1")]),  # identity lookup
            ([], []),  # the INSERT
            (["CURRVAL"], [(42,)]),  # CURRVAL, same session
        )
        adapter._pk_cache["orders"] = "id"
        new_id = await adapter._oracle_insert(
            "orders", 'INSERT INTO "ORDERS" ("NAME") VALUES (?)', ["x"]
        )
        assert new_id == 42
        insert, currval = pool.executed[-2], pool.executed[-1]
        assert insert[0] == 'INSERT INTO "ORDERS" ("NAME") VALUES (:1)'
        assert currval[0].endswith(".CURRVAL FROM DUAL")
        assert insert[2] == currval[2]


# --- types ----------------------------------------------------------------------------


class TestOutputTypes:
    def handler(self, oracledb: types.ModuleType):
        pool = ThinPool(FakePool(), oracledb)
        return pool._handler

    def column(self, type_code: Any, precision: int = 0, scale: int = -127):
        return types.SimpleNamespace(type_code=type_code, precision=precision, scale=scale)

    def test_an_undeclared_number_is_an_exact_decimal(self, oracledb):
        var = self.handler(oracledb)(
            FakeCursor(FakeConnection(FakePool())), self.column(oracledb.DB_TYPE_NUMBER)
        )
        assert var.kind is decimal.Decimal

    def test_a_number_with_a_scale_is_a_decimal(self, oracledb):
        column = self.column(oracledb.DB_TYPE_NUMBER, precision=10, scale=2)
        var = self.handler(oracledb)(FakeCursor(FakeConnection(FakePool())), column)
        assert var.kind is decimal.Decimal

    def test_an_integer_number_stays_an_int(self, oracledb):
        column = self.column(oracledb.DB_TYPE_NUMBER, precision=10, scale=0)
        assert self.handler(oracledb)(FakeCursor(FakeConnection(FakePool())), column) is None

    def test_lobs_arrive_as_plain_values(self, oracledb):
        cursor = FakeCursor(FakeConnection(FakePool()))
        handler = self.handler(oracledb)
        assert handler(cursor, self.column(oracledb.DB_TYPE_CLOB)).kind == "LONG"
        assert handler(cursor, self.column(oracledb.DB_TYPE_BLOB)).kind == "LONG_RAW"

    def test_an_undeclared_number_is_not_flagged_inexact(self, caplog):
        column = {
            "column_name": "ID",
            "data_type": "NUMBER",
            "nullable": "N",
            "numeric_precision": None,
            "numeric_scale": None,
        }
        assert OracleAdapter._oracle_column(column, True)["inexact"] is False
        assert ODBCAdapter._oracle_column(column, True)["inexact"] is True
        adapter = OracleAdapter(CONFIG)
        with caplog.at_level(logging.WARNING):
            adapter._warn_about_inexact_numbers(
                "orders", [OracleAdapter._oracle_column(column, True)]
            )
        assert caplog.records == []


class TestSessionIdentity:
    @pytest.mark.asyncio
    async def test_the_identifier_and_the_procedure_are_one_block_per_acquire(self, oracledb):
        from truejoin.domain.governance.session import CURRENT_IDENTITY, SessionIdentity

        options = {
            "session_identity": {
                "client_identifier": "tenant",
                "context_procedure": "ctx.set_caller",
            }
        }
        oracledb.pending[:] = [([], []), (["USERNAME"], [("warp",)]), ([], []), (["N"], [(1,)])]
        adapter = OracleAdapter({**CONFIG, "options": options})
        await adapter.connect()  # as the service
        pool = oracledb.pools[0]
        # Not the connection's client_identifier attribute: set to "" for the
        # service, Oracle closed the session on the round trip that carried it.
        cleared = pool.executed[0]
        assert cleared[0] == "BEGIN DBMS_SESSION.CLEAR_IDENTIFIER; ctx.set_caller(:1, :2, :3); END;"
        assert cleared[1] == [None, None, None]
        assert pool.executed[1][0] == "SELECT USER AS username FROM DUAL"
        token = CURRENT_IDENTITY.set(SessionIdentity("alice", tenant="a"))
        try:
            await adapter.execute_query("SELECT 1 AS n FROM DUAL")
        finally:
            CURRENT_IDENTITY.reset(token)
        procedure, statement = pool.executed[-2], pool.executed[-1]
        assert procedure[0] == (
            "BEGIN DBMS_SESSION.SET_IDENTIFIER(:1); ctx.set_caller(:2, :3, :4); END;"
        )
        assert procedure[1] == ["a", "alice", "a", None]
        assert not any(hasattr(c, "client_identifier") for c in pool.connections)
        assert statement[0] == "SELECT 1 AS n FROM DUAL"
