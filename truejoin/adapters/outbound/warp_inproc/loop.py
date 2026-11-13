"""GatewayLoop: the one thread whose asyncio loop owns the database gateways.

asyncpg/aioodbc pools belong to the loop that created them, and the OLAP side
is synchronous with three kinds of callers (the HTTP threadpool, mcp's worker
threads, the CLI). One owning loop serves all of them: a sync caller hands a
coroutine over with ``submit`` and waits for the result.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from typing import Any


class GatewayLoop:
    """A daemon thread running one event loop until ``stop``."""

    def __init__(self, name: str = "truejoin-gateway-loop") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def start(self) -> GatewayLoop:
        self._thread.start()
        return self

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def spawn[T](self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """Schedule ``coro`` on the loop and return its future without waiting."""
        if threading.current_thread() is self._thread:
            coro.close()
            # Waiting here would block the only thread that can finish the work.
            raise RuntimeError("GatewayLoop was called from its own thread; this would deadlock")
        if not self.running:
            coro.close()
            raise RuntimeError("GatewayLoop is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def submit[T](self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """Run ``coro`` on the loop and wait for its result (``TimeoutError`` cancels it)."""
        future = self.spawn(coro)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def call_soon(self, callback: Callable[[], object]) -> None:
        """Run a plain callback on the loop thread (ignored once the loop is gone)."""
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass

    def stop(self) -> None:
        """Stop the loop and join the thread (idempotent)."""
        if not self.running:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
