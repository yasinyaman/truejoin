"""InProcessSource: DataSource + PushdownCapable straight on a DatabaseGateway.

The same contract as ``warp_http.WarpSource`` without the HTTP hop: every
gateway coroutine runs on the ``GatewayLoop`` thread and the synchronous OLAP
services wait for it. Rows of a slice cross the thread boundary through a
bounded queue, so a slow consumer slows the database cursor down instead of
filling memory.
"""

from __future__ import annotations

import asyncio
import logging
import queue
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping, Sequence
from typing import Any

from truejoin.adapters.outbound.warp_http.source import _KIND_TYPES
from truejoin.adapters.outbound.warp_inproc.loop import GatewayLoop
from truejoin.domain.gateway.filtering import parse_filter_conditions
from truejoin.domain.gateway.raw_query import QueryValidator
from truejoin.domain.gateway.sql_types import type_kind
from truejoin.domain.olap.errors import ConnectionError, QueryError
from truejoin.domain.olap.models import (
    ColumnInfo,
    RowSet,
    RowStream,
    SourceCapabilities,
    SourceSchema,
    TableSchema,
    keys_of,
)
from truejoin.domain.olap.slices import Predicate, SliceSpec, columns_of
from truejoin.ports.database import DatabaseGateway

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_BATCH_SIZE = 5000
#: Batches waiting between the loop thread and the consumer (backpressure).
QUEUE_DEPTH = 2

_DONE = object()

Filters = list[tuple[str, str, Any]]


class InProcessSource:
    """Config keys (via ``from_config``):

    type: "warp" · gateway (a ``DatabaseGateway``) · loop (the ``GatewayLoop``) ·
    raw_query (pushdown allowed, default off) · raw_query_whitelist ·
    excluded_tables · batch_size · timeout (seconds per call / per batch) ·
    statement_timeout_ms

    The source owns the gateway it is given: ``connect`` opens it on the loop
    thread and ``close`` disconnects it there.
    """

    source_type = "warp"

    def __init__(  # noqa: PLR0913
        self,
        name: str,
        gateway: DatabaseGateway,
        loop: GatewayLoop,
        *,
        raw_query: bool = False,
        raw_query_whitelist: Sequence[str] | None = None,
        excluded_tables: Sequence[str] = (),
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
        statement_timeout_ms: int = 0,
    ) -> None:
        self.name = name
        self._gateway = gateway
        self._loop = loop
        self._raw_query = raw_query
        self._validator = QueryValidator(list(raw_query_whitelist or ["SELECT"]))
        self._excluded = frozenset(excluded_tables)
        self._batch_size = batch_size
        self._timeout = timeout
        self._statement_timeout_ms = statement_timeout_ms
        self._tables: list[str] = []
        #: table -> {column: kind}, in the table's column order.
        self._kinds: dict[str, dict[str, str]] = {}
        self._connected = False

    @classmethod
    def from_config(cls, name: str, config: Mapping[str, Any]) -> InProcessSource:
        gateway, loop = config.get("gateway"), config.get("loop")
        if gateway is None or loop is None:
            raise ConnectionError(
                f"Source '{name}' of type 'warp' runs in-process and needs a 'gateway' and a "
                "'loop'; use type 'warp-remote' with 'base_url' to reach a Warp over HTTP"
            )
        return cls(
            name,
            gateway,
            loop,
            raw_query=bool(config.get("raw_query", False)),
            raw_query_whitelist=config.get("raw_query_whitelist"),
            excluded_tables=config.get("excluded_tables") or (),
            batch_size=int(config.get("batch_size", DEFAULT_BATCH_SIZE)),
            timeout=float(config.get("timeout", DEFAULT_TIMEOUT)),
            statement_timeout_ms=int(config.get("statement_timeout_ms", 0)),
        )

    # -- DataSource ---------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tables(self) -> list[str]:
        return list(self._tables)

    def connect(self) -> None:
        try:
            if not self._gateway.is_connected:
                self._call(self._gateway.connect())
            tables = self._call(self._gateway.get_tables())
        except Exception as e:
            raise ConnectionError(f"Cannot connect to database '{self.name}': {e}") from e
        self._tables = [t for t in tables if t not in self._excluded]
        self._connected = True
        logger.info("Connected to %s in-process (tables=%d)", self.name, len(self._tables))

    def close(self) -> None:
        if not self._connected:
            return
        self._connected = False
        try:
            self._call(self._gateway.disconnect())
        except Exception as e:
            logger.warning("Error disconnecting %s: %s", self.name, e)

    def discover_schema(self) -> SourceSchema:
        """Typed columns, keys and planner row estimates, read in one hop to the loop."""
        self._require_connected()
        gateway, tables = self._gateway, list(self._tables)

        async def read() -> tuple[dict[str, dict[str, Any]], dict[str, int | None]]:
            raw = {table: await gateway.get_table_schema(table) for table in tables}
            try:
                estimates = await gateway.row_estimates(tables) if tables else {}
            except Exception as e:
                logger.warning("Row estimates for %s unavailable: %s", self.name, e)
                estimates = {}
            return raw, estimates

        try:
            raw, estimates = self._call(read())
        except Exception as e:
            raise ConnectionError(f"Failed to discover tables of '{self.name}': {e}") from e
        schema: SourceSchema = {}
        for table, description in raw.items():
            kinds: dict[str, str] = {}
            columns: list[ColumnInfo] = []
            for column in description.get("columns", []):
                name = str(column["name"])
                kinds[name] = type_kind(
                    str(column.get("type", "")), column.get("udt_name"), column.get("full_type")
                )
                columns.append(
                    ColumnInfo(
                        name=name,
                        type=_KIND_TYPES.get(kinds[name], "varchar"),
                        nullable=bool(column.get("nullable", True)),
                    )
                )
            self._kinds[table] = kinds
            estimate = estimates.get(table)
            primary_key, foreign_keys = keys_of(description)
            schema[table] = TableSchema(
                columns=columns,
                row_estimate=estimate if isinstance(estimate, int) else -1,
                primary_key=primary_key,
                # A key into a table this source does not offer leads nowhere.
                foreign_keys=tuple(fk for fk in foreign_keys if fk.references_table in raw),
            )
        return schema

    @property
    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            pushdown=self.supports_pushdown, slices=True, arrow=False, row_estimates=True
        )

    @property
    def supports_pushdown(self) -> bool:
        """Raw SQL reaches the database only when ``enable_raw_query`` allows it."""
        return self._raw_query

    def fetch_table(self, table: str, max_rows: int | None = None) -> RowSet:
        """Every row of a table, materialized in memory (prefer ``fetch_slice``)."""
        stream = self.fetch_slice(table, SliceSpec.FULL, max_rows=max_rows)
        rows: list[tuple[Any, ...]] = []
        columns: tuple[str, ...] = ()
        try:
            for batch in stream:
                columns = columns or batch.columns
                rows.extend(batch.rows)
        finally:
            stream.close()
        return RowSet(columns=columns, rows=rows)

    def fetch_slice(
        self, table: str, spec: SliceSpec = SliceSpec.FULL, max_rows: int | None = None
    ) -> RowStream:
        """Stream the rows and columns ``spec`` asks for from a server-side cursor."""
        kinds = self._table_kinds(table)
        filters = self._filters(table, spec.predicates)
        unknown = sorted((spec.columns or frozenset()) - set(kinds)) if kinds else []
        if unknown:
            raise QueryError(f"Unknown columns of {table}: {', '.join(unknown)}")
        # The table's own order, so every batch and the store agree on it.
        columns = [c for c in kinds if spec.columns is None or c in spec.columns] or None
        limit = spec.limit if max_rows is None else min(max_rows, spec.limit or max_rows)
        gateway, batch_size, timeout_ms = (
            self._gateway,
            self._batch_size,
            self._statement_timeout_ms,
        )

        def open_cursor() -> AsyncIterator[list[dict[str, Any]]]:
            return gateway.stream_select(
                table,
                columns=columns,
                filters=filters,
                batch_size=batch_size,
                limit=limit,
                statement_timeout_ms=timeout_ms,
            )

        stream = _GatewayRowStream(
            self._loop, open_cursor, table, tuple(columns or ()), self._timeout
        )
        stream.row_limit = limit
        return stream

    def estimate_slice(self, table: str, spec: SliceSpec = SliceSpec.FULL) -> int | None:
        """How many rows the slice holds: the count the list endpoint reports."""
        try:
            filters = self._filters(table, spec.predicates)
            _, total = self._call(
                self._gateway.select(table, filters=filters, pagination={"limit": 1, "offset": 0})
            )
        except Exception as e:
            logger.debug("Row estimate for %s unavailable: %s", table, e)
            return None
        return total if isinstance(total, int) and total >= 0 else None

    # -- PushdownCapable ----------------------------------------------------

    def execute_query(self, sql: str) -> RowSet:
        self._require_connected()
        if not self._raw_query:
            raise QueryError(f"Raw SQL is disabled for '{self.name}' (settings.enable_raw_query)")
        try:
            self._validator.validate(sql)
            rows = self._call(self._gateway.execute_query(sql))
        except Exception as e:
            raise QueryError(f"Query execution on '{self.name}' failed: {e}") from e
        return RowSet.from_records(rows)

    def fetch_filtered(
        self,
        table: str,
        filters: Mapping[str, object],
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> RowSet:
        """Equality filters are a slice, so this needs no raw SQL."""
        spec = SliceSpec(
            columns=columns_of(columns),
            predicates=tuple(Predicate(column, "eq", value) for column, value in filters.items()),
            limit=limit,
        )
        stream = self.fetch_slice(table, spec)
        try:
            batches = list(stream)
        finally:
            stream.close()
        return RowSet(
            columns=batches[0].columns if batches else (),
            rows=[row for batch in batches for row in batch.rows],
        )

    # -- helpers ------------------------------------------------------------

    def _call[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return self._loop.submit(coro, self._timeout)

    def _require_connected(self) -> None:
        if not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

    def _table_kinds(self, table: str) -> dict[str, str]:
        """Column kinds of a known table; anything else never reaches the gateway."""
        self._require_connected()
        if table not in self._tables:
            raise QueryError(f"Failed to fetch data from {table}: unknown table")
        if table not in self._kinds:
            self.discover_schema()
        return self._kinds.get(table, {})

    def _filters(self, table: str, predicates: Sequence[Predicate]) -> Filters:
        """Predicates as gateway filters.

        An unknown column, or a value the column's type cannot take
        (``id = 'abc'``, a date not written ``YYYY-MM-DD``), raises. Comparing
        in memory instead would read the whole table past every row budget,
        and the driver's typed values never equal the raw string, so the
        slice would come back empty and be kept as complete.
        """
        kinds = self._table_kinds(table)
        unknown = sorted({p.column for p in predicates} - set(kinds)) if kinds else []
        if unknown:
            raise QueryError(f"Unknown columns of {table}: {', '.join(unknown)}")
        try:
            return parse_filter_conditions(
                [(p.column, p.op, list(p.value) if p.op == "in" else p.value) for p in predicates],
                None,
                kinds,
            )
        except ValueError as e:
            raise QueryError(
                f"A condition on {table} cannot be sent to the source ({e}). Write the "
                f"value as the column's type takes it, e.g. a date as 'YYYY-MM-DD'."
            ) from e


class _GatewayRowStream:
    """A sync ``RowStream`` fed by a task on the gateway loop.

    The task puts batches on a ``queue.Queue(maxsize=QUEUE_DEPTH)`` and, when
    it is full, waits on an asyncio event the consumer sets after every
    ``get``; the loop thread itself never blocks.
    """

    row_limit: int | None = None

    def __init__(
        self,
        loop: GatewayLoop,
        open_cursor: Callable[[], AsyncIterator[list[dict[str, Any]]]],
        table: str,
        columns: tuple[str, ...],
        timeout: float,
    ) -> None:
        self._loop = loop
        self._open = open_cursor
        self._table = table
        self._columns = columns
        self._timeout = timeout
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_DEPTH)
        self._room = asyncio.Event()
        self._future = loop.spawn(self._pump())

    async def _put(self, item: Any) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                # Cleared and re-checked without yielding: a consumer's set()
                # only runs on this thread after this block, so none is lost.
                self._room.clear()
                if not self._queue.full():
                    continue
                await self._room.wait()

    async def _pump(self) -> None:
        batches = self._open()
        try:
            async for batch in batches:
                if batch:
                    await self._put(batch)
            await self._put(_DONE)
        except Exception as e:
            await self._put(e)
        finally:
            # Also on cancellation, so the cursor and its connection go back now.
            aclose = getattr(batches, "aclose", None)
            if aclose is not None:
                await aclose()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def schema(self) -> TableSchema | None:
        return None

    def arrow_reader(self) -> Any:
        return None

    def __iter__(self) -> Iterator[RowSet]:
        empty = True
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._timeout)
                except queue.Empty:
                    raise QueryError(
                        f"Timed out after {self._timeout}s waiting for rows of {self._table}"
                    ) from None
                self._loop.call_soon(self._room.set)
                if item is _DONE:
                    break
                if isinstance(item, Exception):
                    raise QueryError(f"Failed to fetch data from {self._table}: {item}") from item
                self._columns = self._columns or tuple(item[0])
                empty = False
                columns = self._columns
                yield RowSet(columns=columns, rows=[tuple(r.get(c) for c in columns) for r in item])
            if empty:
                yield RowSet(columns=self._columns, rows=[])
        finally:
            self.close()

    def close(self) -> None:
        self._future.cancel()
