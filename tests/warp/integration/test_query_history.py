"""Statement history and view definitions, read from real engines.

What each engine actually hands back is the point: PostgreSQL deparses a view
with casts and its own operators and stores statements with their constants
replaced; MySQL qualifies every column with the database. The miner has to
read those, not the tidy SQL a unit test writes.
"""

from collections.abc import AsyncIterator, Iterator

import pytest

from tests.warp.integration.conftest import POSTGRES_IMAGE, _connected, _docker_available
from truejoin.adapters.outbound.db.factory import DatabaseFactory
from truejoin.adapters.outbound.db.history import QueryHistoryReader
from truejoin.adapters.outbound.sqlglot_expressions import SqlglotExpressionMiner
from truejoin.application.settings import DatabaseConfig
from truejoin.domain.catalog.history import (
    AGGREGATE,
    CONDITION,
    PROJECTION,
    HistoryUnavailableError,
)
from truejoin.ports.database import DatabaseGateway

pytestmark = pytest.mark.integration

USERS = {"users": ["id", "username", "zip", "active", "created_at"]}


def mined(entry, dialect: str, columns=USERS) -> dict[str, str]:
    found = SqlglotExpressionMiner().mine(entry.sql, dialect, columns)
    return {m.kind: m.expression for m in found}


class TestPostgresViews:
    @pytest.fixture
    async def view(self, pg_gateway) -> AsyncIterator[None]:
        await pg_gateway.execute_query("DROP VIEW IF EXISTS nine_zips")
        await pg_gateway.execute_query(
            "CREATE VIEW nine_zips AS "
            "SELECT id, id * 2 + length(zip) AS score FROM users WHERE zip LIKE '9%'"
        )
        yield
        await pg_gateway.execute_query("DROP VIEW IF EXISTS nine_zips")

    async def test_a_view_is_read_and_mined_as_postgres_deparses_it(self, view, pg_gateway):
        reader = QueryHistoryReader(pg_gateway, "postgresql", "public")
        (entry,) = [e for e in await reader.view_definitions() if e.name == "nine_zips"]
        assert entry.origin == "view" and entry.id == "view:nine_zips"
        found = mined(entry, "postgresql")
        # Deparsed with casts and `~~`; still one condition and one projection on users.
        assert set(found) == {CONDITION, PROJECTION}
        assert "LIKE" in found[CONDITION] and "zip" in found[CONDITION]
        assert "id * 2" in found[PROJECTION]
        # And each is still something the compiler takes as a measure.
        import duckdb

        from truejoin.adapters.outbound.sqlglot_semantic import _expression_sql
        from truejoin.domain.olap.measures import Measure

        connection = duckdb.connect()
        connection.execute("CREATE TABLE users AS SELECT 1 AS id, '90210' AS zip")
        for expression in found.values():
            compiled = _expression_sql(
                Measure("m", "id", expression=expression, reads=("id", "zip"))
            )
            total = connection.execute(f'SELECT SUM({compiled}) FROM users AS "f"').fetchone()
            print(expression, "=>", total)
            assert total[0] is not None

    async def test_without_the_extension_the_history_says_what_it_needs(self, pg_gateway):
        reader = QueryHistoryReader(pg_gateway, "postgresql", "public")
        with pytest.raises(HistoryUnavailableError) as raised:
            await reader.recent_statements()
        assert "pg_stat_statements" in str(raised.value)
        assert "CREATE EXTENSION pg_stat_statements" in str(raised.value)


@pytest.fixture(scope="module")
def tracked_config() -> Iterator[DatabaseConfig]:
    """A PostgreSQL started with pg_stat_statements preloaded."""
    if not _docker_available():
        pytest.skip("Docker is not available")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(POSTGRES_IMAGE).with_command(
        "postgres -c shared_preload_libraries=pg_stat_statements"
    )
    with container as pg:
        yield DatabaseConfig(
            name="tracked",
            type="postgresql",
            host=pg.get_container_host_ip(),
            port=int(pg.get_exposed_port(5432)),
            database=pg.dbname,
            username=pg.username,
            password=pg.password,
        )


@pytest.fixture
async def tracked(tracked_config) -> AsyncIterator[DatabaseGateway]:
    gateway = DatabaseFactory().create(tracked_config)
    await gateway.connect()
    try:
        await gateway.execute_query("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        await gateway.execute_query("DROP TABLE IF EXISTS sales")
        await gateway.execute_query(
            "CREATE TABLE sales (id SERIAL PRIMARY KEY, status VARCHAR(10), "
            "amount NUMERIC(10,2), qty INT)"
        )
        await gateway.execute_query("INSERT INTO sales (status, amount, qty) VALUES ('paid', 5, 2)")
        yield gateway
    finally:
        await gateway.disconnect()


class TestPostgresStatements:
    SALES = {"sales": ["id", "status", "amount", "qty"]}

    async def test_statements_come_back_counted_with_their_constants_replaced(self, tracked):
        for _ in range(3):
            await tracked.execute_query(
                "SELECT SUM(amount * qty) AS revenue FROM sales WHERE status = 'paid'"
            )
        reader = QueryHistoryReader(tracked, "postgresql", "public")
        entries = await reader.recent_statements(200)
        (entry,) = [e for e in entries if "SUM(amount * qty)" in e.sql]
        assert entry.calls >= 3 and entry.origin == "history"
        # The literal is gone, so the condition is a parameter and is not mined;
        # the arithmetic has no constant in it and survives.
        assert "'paid'" not in entry.sql and "$1" in entry.sql
        assert mined(entry, "postgresql", self.SALES) == {AGGREGATE: "amount * qty"}

    async def test_only_statements_that_read_are_returned(self, tracked):
        entries = await QueryHistoryReader(tracked, "postgresql", "public").recent_statements(500)
        assert entries
        assert all(e.sql.lstrip().lower().startswith(("select", "with")) for e in entries)


class TestMySql:
    @pytest.fixture
    async def view(self, mysql_gateway) -> AsyncIterator[None]:
        await mysql_gateway.execute_query("DROP VIEW IF EXISTS nine_zips")
        await mysql_gateway.execute_query(
            "CREATE VIEW nine_zips AS "
            "SELECT id, id * 2 + length(zip) AS score FROM users WHERE zip LIKE '9%'"
        )
        yield
        await mysql_gateway.execute_query("DROP VIEW IF EXISTS nine_zips")

    async def test_a_view_is_read_and_mined_as_mysql_stores_it(
        self, view, mysql_gateway, mysql_config
    ):
        reader = QueryHistoryReader(mysql_gateway, "mysql", database=mysql_config.database)
        (entry,) = [e for e in await reader.view_definitions() if e.name == "nine_zips"]
        found = mined(entry, "mysql")
        # Stored with every column qualified by database and table.
        assert set(found) == {CONDITION, PROJECTION}
        assert found[CONDITION] == "CASE WHEN zip LIKE '9%' THEN 1 ELSE 0 END"

    async def test_statements_come_back_with_their_literals_once_granted(
        self, mysql_gateway, mysql_config
    ):
        reader = QueryHistoryReader(mysql_gateway, "mysql", database=mysql_config.database)
        with pytest.raises(HistoryUnavailableError, match=r"GRANT SELECT ON performance_schema"):
            await reader.recent_statements()

        # The container's root password is testcontainers' default.
        root = mysql_config.model_copy(update={"username": "root", "password": "test"})
        async for admin in _connected(root):
            await admin.execute_query(
                f"GRANT SELECT ON performance_schema.* TO '{mysql_config.username}'@'%'"
            )
        for _ in range(3):
            await mysql_gateway.execute_query(
                "SELECT SUM(id * 2) AS s FROM users WHERE zip LIKE '9%'"
            )
        entries = await reader.recent_statements(200)
        (entry,) = [e for e in entries if "SUM(id * 2)" in e.sql]
        assert entry.calls >= 3 and entry.last_seen is not None
        # MySQL 8 keeps a real sample, so the condition's literal survives to be mined.
        assert mined(entry, "mysql") == {
            AGGREGATE: "id * 2",
            CONDITION: "CASE WHEN zip LIKE '9%' THEN 1 ELSE 0 END",
        }


class TestMariaDb:
    """No sample statement on MariaDB: the reader falls back to the digest."""

    async def test_the_digest_is_read_with_its_constants_replaced(
        self, mariadb_gateway, mariadb_config
    ):
        reader = QueryHistoryReader(mariadb_gateway, "mariadb", database=mariadb_config.database)
        with pytest.raises(HistoryUnavailableError, match="GRANT SELECT ON performance_schema"):
            await reader.recent_statements()

        root = mariadb_config.model_copy(update={"username": "root", "password": "test"})
        async for admin in _connected(root):
            await admin.execute_query(
                f"GRANT SELECT ON performance_schema.* TO '{mariadb_config.username}'@'%'"
            )
        for _ in range(3):
            await mariadb_gateway.execute_query(
                "SELECT SUM(active * id) AS s FROM users WHERE zip LIKE '9%'"
            )
        entries = await reader.recent_statements(200)
        (entry,) = [e for e in entries if "SUM" in e.sql and "`users`" in e.sql]
        assert entry.calls >= 3 and "?" in entry.sql and "'9%'" not in entry.sql
        # As on PostgreSQL: arithmetic without a constant survives; the
        # condition's constant became `?`, so it is not mined.
        assert mined(entry, "mysql") == {AGGREGATE: "active * id"}


class TestSqlServer:
    """`sys.dm_exec_query_stats` and `sys.sql_modules`, as SQL Server keeps them."""

    @pytest.fixture
    async def view(self, mssql_gateway) -> AsyncIterator[None]:
        await mssql_gateway.execute_query("DROP VIEW IF EXISTS nine_zips")
        await mssql_gateway.execute_query(
            "CREATE VIEW nine_zips AS "
            "SELECT id, id * 2 + LEN(zip) AS score FROM users WHERE zip LIKE '9%'"
        )
        yield
        await mssql_gateway.execute_query("DROP VIEW IF EXISTS nine_zips")

    async def test_a_view_is_read_through_its_create_statement(self, view, mssql_gateway):
        reader = QueryHistoryReader(mssql_gateway, "mssql", "dbo")
        (entry,) = [e for e in await reader.view_definitions() if e.name == "nine_zips"]
        found = mined(entry, "mssql")
        assert set(found) == {CONDITION, PROJECTION}
        assert found[CONDITION] == "CASE WHEN zip LIKE '9%' THEN 1 ELSE 0 END"

    async def test_the_plan_cache_gives_back_what_ran_in_this_database(self, mssql_gateway):
        for _ in range(3):
            await mssql_gateway.execute_query(
                "SELECT COUNT(*) AS n FROM users WHERE zip LIKE '9%' AND active = 1"
            )
        reader = QueryHistoryReader(mssql_gateway, "mssql", "dbo")
        entries = await reader.recent_statements(500)
        ours = [e for e in entries if "FROM users WHERE zip LIKE" in e.sql]
        assert ours and max(e.calls for e in ours) >= 1
        assert all(e.origin == "history" for e in entries)


class TestOracle:
    """`ALL_VIEWS`, and `V$SQL` behind a grant the test user does not have."""

    @pytest.fixture
    async def view(self, oracle_gateway) -> AsyncIterator[None]:
        await oracle_gateway.execute_query(
            "CREATE OR REPLACE VIEW nine_zips AS "
            "SELECT id, id * 2 + LENGTH(zip) AS score FROM users WHERE zip LIKE '9%'"
        )
        yield
        await oracle_gateway.execute_query("DROP VIEW nine_zips")

    async def test_a_view_is_read_from_the_session_users_schema(self, view, oracle_gateway):
        reader = QueryHistoryReader(oracle_gateway, "oracle", "WARP")
        (entry,) = [e for e in await reader.view_definitions() if e.name == "NINE_ZIPS"]
        found = mined(entry, "oracle")
        # Stored as it was written; the columns come out as the catalog spells them.
        assert set(found) == {CONDITION, PROJECTION}
        assert found[CONDITION] == "CASE WHEN zip LIKE '9%' THEN 1 ELSE 0 END"

    async def test_the_history_is_read_or_the_missing_grant_is_named(self, oracle_gateway):
        """An application user normally may not read V$SQL: that has to be said, not raised raw."""
        await oracle_gateway.execute_query("SELECT COUNT(*) FROM users WHERE zip LIKE '9%'")
        reader = QueryHistoryReader(oracle_gateway, "oracle", "WARP")
        try:
            entries = await reader.recent_statements(200)
        except HistoryUnavailableError as e:
            assert "GRANT SELECT ON V_$SQL" in str(e)
        else:
            assert all(e.sql and e.calls >= 1 for e in entries)
