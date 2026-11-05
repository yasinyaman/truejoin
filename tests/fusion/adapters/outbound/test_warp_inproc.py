"""GatewayLoop and InProcessSource: the sync/async seam of the single process.

The DataSource contract itself is covered by ``tests/fusion/ports``; this file
holds what is specific to running on a loop thread.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.fusion.fakes import FakeGateway
from trio.adapters.outbound.warp_inproc import GatewayLoop, InProcessSource
from trio.adapters.outbound.warp_inproc.source import QUEUE_DEPTH
from trio.domain.olap.errors import ConnectionError, QueryError
from trio.domain.olap.models import ForeignKey
from trio.domain.olap.slices import Predicate, SliceSpec

TABLES = {
    "users": [
        {"id": 1, "name": "Alice", "segment": "premium"},
        {"id": 2, "name": "Bob", "segment": "basic"},
        {"id": 3, "name": "Cara", "segment": "premium"},
    ],
    "audit_log": [{"id": 1, "note": "x"}],
}


class CountingGateway(FakeGateway):
    """Counts the batches the cursor produced and notices when it was closed."""

    produced = 0
    cursor_closed = False

    async def stream_select(self, *args, **kwargs):
        try:
            async for batch in super().stream_select(*args, **kwargs):
                self.produced += 1
                yield batch
        finally:
            self.cursor_closed = True


def _source(loop, gateway=None, **options):
    source = InProcessSource("src", gateway or FakeGateway(TABLES, name="src"), loop, **options)
    source.connect()
    return source


class TestGatewayLoop:
    def test_submit_runs_on_the_loop_thread(self, gateway_loop):
        async def where():
            return threading.current_thread().name

        assert gateway_loop.submit(where()) == "trio-gateway-loop"

    def test_submit_from_the_loop_thread_is_refused(self, gateway_loop):
        async def inner():
            return 1

        async def outer():
            return gateway_loop.submit(inner())

        with pytest.raises(RuntimeError, match="deadlock"):
            gateway_loop.submit(outer())

    def test_a_timeout_cancels_the_coroutine(self, gateway_loop):
        cancelled = threading.Event()

        async def slow():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(TimeoutError):
            gateway_loop.submit(slow(), timeout=0.05)
        assert cancelled.wait(2)

    def test_stop_is_idempotent_and_later_calls_are_refused(self):
        loop = GatewayLoop("short-lived").start()
        assert loop.running
        loop.stop()
        loop.stop()
        assert not loop.running

        async def nothing():
            return None

        with pytest.raises(RuntimeError, match="not running"):
            loop.submit(nothing())


class TestInProcessSource:
    def test_owns_the_gateway_connection(self, gateway_loop):
        gateway = FakeGateway(TABLES, name="src")
        source = InProcessSource("src", gateway, gateway_loop)
        assert not gateway.is_connected
        source.connect()
        assert gateway.is_connected and source.tables == ["users", "audit_log"]
        source.close()
        assert not gateway.is_connected

    def test_a_gateway_that_cannot_connect_is_a_connection_error(self, gateway_loop):
        class Down(FakeGateway):
            async def connect(self):
                raise OSError("refused")

        with pytest.raises(ConnectionError, match="refused"):
            InProcessSource("src", Down(TABLES), gateway_loop).connect()

    def test_excluded_tables_are_not_discovered(self, gateway_loop):
        source = _source(gateway_loop, excluded_tables=["audit_log"])
        assert set(source.discover_schema()) == {"users"}
        with pytest.raises(QueryError, match="unknown table"):
            source.fetch_table("audit_log")

    def test_schema_carries_types_and_row_estimates(self, gateway_loop):
        users = _source(gateway_loop).discover_schema()["users"]
        assert [(c.name, c.type) for c in users.columns] == [
            ("id", "integer"),
            ("name", "varchar"),
            ("segment", "varchar"),
        ]
        assert users.row_estimate == 3

    def test_schema_carries_the_keys_the_database_declares(self, gateway_loop):
        gateway = FakeGateway(
            {**TABLES, "orders": [{"id": 1, "user_id": 1, "batch_id": 4}]},
            foreign_keys={"orders": [("user_id", "users", "id"), ("batch_id", "batches", "id")]},
        )
        source = InProcessSource("src", gateway, gateway_loop)
        source.connect()
        orders = source.discover_schema()["orders"]
        assert orders.primary_key == ("id",)
        # `batches` is not a table this source offers: a key into it leads nowhere.
        assert orders.foreign_keys == (ForeignKey("user_id", "users", "id"),)
        source.close()

    def test_pushdown_is_off_unless_raw_query_is_enabled(self, gateway_loop):
        gateway = FakeGateway(TABLES, name="src")
        source = _source(gateway_loop, gateway)
        assert not source.supports_pushdown and not source.capabilities.pushdown
        with pytest.raises(QueryError, match="disabled"):
            source.execute_query("SELECT COUNT(*) AS count FROM users")
        assert gateway.queries == []

    def test_pushdown_passes_the_command_whitelist_first(self, gateway_loop):
        gateway = FakeGateway(TABLES, name="src")
        source = _source(gateway_loop, gateway, raw_query=True)
        with pytest.raises(QueryError, match="must start with"):
            source.execute_query("DELETE FROM users")
        assert gateway.queries == []

    def test_fetch_filtered_needs_no_raw_sql(self, gateway_loop):
        gateway = FakeGateway(TABLES, name="src")
        source = _source(gateway_loop, gateway)
        rows = source.fetch_filtered("users", {"segment": "premium"}, columns=["id"], limit=1)
        assert rows.to_records() == [{"id": 1}]
        assert gateway.queries == []
        assert gateway.stream_calls[-1]["filters"] == [("segment", "eq", "premium")]

    def test_an_unknown_column_is_refused_before_the_database(self, gateway_loop):
        gateway = FakeGateway(TABLES, name="src")
        source = _source(gateway_loop, gateway)
        with pytest.raises(QueryError, match="Unknown columns of users: nope"):
            source.fetch_slice("users", SliceSpec(predicates=(Predicate("nope", "eq", 1),)))
        with pytest.raises(QueryError, match="Unknown columns of users: nope"):
            source.fetch_slice("users", SliceSpec(columns=frozenset({"nope"})))
        assert gateway.stream_calls == []

    def test_a_value_the_column_cannot_take_is_filtered_in_memory(self, gateway_loop):
        gateway = FakeGateway(TABLES, name="src")
        source = _source(gateway_loop, gateway)
        spec = SliceSpec(predicates=(Predicate("id", "eq", "abc"),))
        assert list(source.fetch_slice("users", spec))[0].rows == []
        # The whole table was read, with no filter sent to the database.
        assert gateway.stream_calls[-1]["filters"] == []
        assert source.estimate_slice("users", spec) is None

    def test_a_failing_cursor_surfaces_as_a_query_error(self, gateway_loop):
        class Broken(FakeGateway):
            async def stream_select(self, *args, **kwargs):
                yield [{"id": 1, "name": "a", "segment": "b"}]
                raise OSError("connection reset")

        source = _source(gateway_loop, Broken(TABLES, name="src"))
        with pytest.raises(QueryError, match="connection reset"):
            source.fetch_table("users")

    def test_a_slow_consumer_holds_the_cursor_back(self, gateway_loop):
        rows = [{"id": i} for i in range(200)]
        gateway = CountingGateway({"big": rows}, name="src")
        source = _source(gateway_loop, gateway, batch_size=1)
        stream = iter(source.fetch_slice("big", SliceSpec.FULL))
        next(stream)
        time.sleep(0.1)
        # One batch taken, QUEUE_DEPTH waiting, one in the producer's hand.
        assert gateway.produced <= 1 + QUEUE_DEPTH + 1
        assert sum(len(batch.rows) for batch in stream) == 199

    def test_closing_early_releases_the_cursor(self, gateway_loop):
        gateway = CountingGateway({"big": [{"id": i} for i in range(200)]}, name="src")
        source = _source(gateway_loop, gateway, batch_size=1)
        stream = source.fetch_slice("big", SliceSpec.FULL)
        next(iter(stream))
        stream.close()
        deadline = time.monotonic() + 2
        while not gateway.cursor_closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gateway.cursor_closed
        assert gateway.produced < 200

    def test_a_silent_database_times_out_instead_of_hanging(self, gateway_loop):
        source = _source(gateway_loop, FakeGateway(TABLES, name="src"), timeout=0.05)
        source._gateway.delay = 1.0
        with pytest.raises(QueryError, match="Timed out"):
            source.fetch_table("users")

    def test_32_concurrent_sync_callers_do_not_deadlock(self, gateway_loop):
        """Every caller blocks on the one loop thread; none may block it back."""
        gateway = FakeGateway(TABLES, name="src", delay=0.01)
        source = _source(gateway_loop, gateway, raw_query=True, batch_size=1, timeout=10)

        def work(_):
            assert len(source.fetch_table("users")) == 3
            assert source.estimate_slice("users", SliceSpec.FULL) == 3
            return source.execute_query("SELECT COUNT(*) AS count FROM users").to_records()

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(work, range(32), timeout=30))
        assert results == [[{"count": 3}]] * 32
