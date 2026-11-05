"""FakeGateway: an in-memory DatabaseGateway (typed schema, mini SQL, optional delay)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from tests.fusion.fakes.warp_transport import run_mock_sql
from tests.warp.conftest import MockDatabaseAdapter

_DB_TYPES = {bool: "boolean", int: "integer", float: "double precision"}


class FakeGateway(MockDatabaseAdapter):
    """Warp's mock adapter plus what the OLAP side needs from a real database.

    ``delay`` seconds are awaited before every call and every streamed batch,
    which is what makes a missing thread hop or a blocked loop show up.
    """

    def __init__(
        self,
        tables: Mapping[str, list[dict[str, Any]]],
        name: str = "db",
        delay: float = 0.0,
        foreign_keys: Mapping[str, list[tuple[str, str, str]]] | None = None,
    ) -> None:
        super().__init__({"name": name})
        #: table -> (column, referenced table, referenced column).
        self.foreign_keys = dict(foreign_keys or {})
        self._tables = {table: [dict(row) for row in rows] for table, rows in tables.items()}
        self._pool = None  # not connected until connect()
        self.delay = delay
        self.queries: list[str] = []

    async def _wait(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)

    async def connect(self) -> None:
        await self._wait()
        await super().connect()

    async def get_tables(self) -> list[str]:
        await self._wait()
        return await super().get_tables()

    async def get_table_schema(self, table: str) -> dict[str, Any]:
        rows = self._tables.get(table, [])
        names = list(dict.fromkeys(key for row in rows for key in row))
        columns = []
        for name in names:
            values = [row.get(name) for row in rows]
            kinds = {type(v) for v in values if v is not None}
            db_type = _DB_TYPES.get(next(iter(kinds)), "text") if len(kinds) == 1 else "text"
            columns.append(
                {"name": name, "type": db_type, "nullable": any(v is None for v in values)}
            )
        return {
            "table_name": table,
            "columns": columns,
            "primary_key": "id",
            "foreign_keys": [
                {"column": c, "references_table": t, "references_column": r}
                for c, t, r in self.foreign_keys.get(table, [])
            ],
            "indexes": [],
        }

    async def execute_query(self, query: str, params: dict | None = None) -> list[dict]:
        await self._wait()
        self.queries.append(query)
        return run_mock_sql(query, self._tables)

    async def select(self, *args: Any, **kwargs: Any) -> tuple:
        await self._wait()
        return await super().select(*args, **kwargs)

    async def stream_select(self, *args: Any, **kwargs: Any):
        async for batch in super().stream_select(*args, **kwargs):
            await self._wait()
            yield batch
