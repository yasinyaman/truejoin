"""Reading statement history and view definitions through a gateway."""

import pytest

from trio.adapters.outbound.db.history import QUERIES, QueryHistoryReader
from trio.domain.catalog.history import HistoryEntry, HistoryUnavailableError


class Reader:
    """A gateway that answers each query with canned rows, or refuses."""

    def __init__(self, statements=None, views=None, error: Exception | None = None) -> None:
        self.statements = statements or []
        self.views = views or []
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def execute_query(self, query, params=None):
        self.calls.append((query, dict(params or {})))
        # As the real binder does: a value nothing in the statement names is an error.
        unused = [name for name in (params or {}) if f":{name}" not in query]
        if unused:
            raise ValueError(f"Unused query parameter(s): {', '.join(unused)}")
        if self.error is not None:
            raise self.error
        return self.views if "limit" not in (params or {}) else self.statements


class TestStatements:
    async def test_rows_become_entries_most_run_first_as_the_database_gave_them(self):
        gateway = Reader(
            statements=[
                {"query": "SELECT 1", "calls": 40, "last_seen": None},
                {"query": "SELECT 2", "calls": None},
                {"query": None, "calls": 3},
            ]
        )
        entries = await QueryHistoryReader(gateway, "postgresql", "public").recent_statements(50)
        assert entries == [HistoryEntry("SELECT 1", 40), HistoryEntry("SELECT 2", 1)]
        query, params = gateway.calls[0]
        assert "pg_stat_statements" in query and params == {"limit": 50}

    @pytest.mark.parametrize(
        ("dialect", "source", "params"),
        [
            ("mssql", "sys.dm_exec_query_stats", {"limit": 500}),
            ("oracle", "V$SQL", {"limit": 500, "schema": "APP"}),
        ],
    )
    async def test_each_engine_is_asked_where_it_keeps_them_with_what_it_names(
        self, dialect, source, params
    ):
        gateway = Reader()
        await QueryHistoryReader(gateway, dialect, "APP").recent_statements()
        assert source in gateway.calls[0][0] and gateway.calls[0][1] == params

    async def test_a_refusal_says_what_it_needs(self):
        gateway = Reader(error=RuntimeError('relation "pg_stat_statements" does not exist'))
        with pytest.raises(HistoryUnavailableError) as raised:
            await QueryHistoryReader(gateway, "postgresql").recent_statements()
        message = str(raised.value)
        assert 'relation "pg_stat_statements" does not exist' in message
        assert "CREATE EXTENSION pg_stat_statements" in message and "pg_read_all_stats" in message

    async def test_mysql_has_no_history_worth_reading_and_is_not_asked(self):
        gateway = Reader()
        with pytest.raises(HistoryUnavailableError, match="every literal replaced"):
            await QueryHistoryReader(gateway, "mysql", database="shop").recent_statements()
        assert gateway.calls == []

    async def test_an_engine_nobody_wrote_queries_for(self):
        with pytest.raises(HistoryUnavailableError, match="No statement history is read"):
            await QueryHistoryReader(Reader(), "odbc").recent_statements()
        with pytest.raises(HistoryUnavailableError, match="No view definitions is read"):
            await QueryHistoryReader(Reader(), "odbc").view_definitions()


class TestViews:
    async def test_definitions_become_entries_named_after_their_view(self):
        gateway = Reader(
            views=[
                {"name": "paid_orders", "definition": "SELECT 1"},
                {"name": "hidden", "definition": None},
            ]
        )
        entries = await QueryHistoryReader(gateway, "postgresql", "sales").view_definitions()
        assert entries == [HistoryEntry("SELECT 1", origin="view", name="paid_orders")]
        assert entries[0].id == "view:paid_orders"
        assert gateway.calls[0][1] == {"schema": "sales"}

    async def test_mysql_is_scoped_by_database(self):
        gateway = Reader()
        await QueryHistoryReader(gateway, "mysql", database="shop").view_definitions()
        assert gateway.calls[0][1] == {"database": "shop"}

    async def test_a_refusal_names_the_grant(self):
        gateway = Reader(error=RuntimeError("permission denied"))
        with pytest.raises(HistoryUnavailableError, match="VIEW DEFINITION"):
            await QueryHistoryReader(gateway, "mssql", "dbo").view_definitions()


def test_a_statement_has_a_stable_short_id():
    one, two = HistoryEntry("SELECT 1", 5), HistoryEntry("SELECT 1", 900)
    assert one.id == two.id and one.id.startswith("sql:") and len(one.id) == 14
    assert HistoryEntry("SELECT 2").id != one.id


def test_every_engine_says_what_it_needs():
    assert set(QUERIES) == {"postgresql", "mysql", "mssql", "oracle"}
    assert all(queries.needs and queries.views for queries in QUERIES.values())
