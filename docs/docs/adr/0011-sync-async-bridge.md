# 11. The sync/async bridge: one loop thread owns the OLAP side's gateways

- Status: Accepted
- Date: 2026-11-05

## Context

The gateway is async: asyncpg, aiomysql and aioodbc pools belong to the event
loop that created them. The OLAP engine is synchronous: DuckDB behind one
lock, services written as plain functions, about nine hundred tests that call
them directly. Once the engine reads databases in process instead of over
HTTP, the two have to meet.

Three kinds of caller reach the engine: REST handlers (run by the server in a
thread pool), MCP tool calls (run by the MCP library in a worker thread) and
the CLI. None of them is on the loop that owns a pool.

## Decision

`GatewayLoop` is a daemon thread running its own event loop. The OLAP side's
gateways are created and connected on it and never leave it.
`submit(coro, timeout)` schedules a coroutine there and waits for the result.

`InProcessSource` implements the engine's `DataSource` port on top: schema
discovery, row estimates and slice reads become `submit` calls. A slice is
streamed: a task on the loop thread feeds a bounded queue, and the engine
reads batches from it, so a slow consumer slows the database read instead of
filling memory.

The engine stays synchronous. Making `DataSource` async would have changed
every service, the store's lock discipline and the test suite, to gain
nothing: DuckDB does the work under a lock either way.

## Consequences

- Each database has a second pool, for the OLAP side, using the read-only
  credentials when the database has them.
- The one way to deadlock is calling `submit` from the loop thread itself, so
  that raises immediately.
- About fifty microseconds per batch for the thread hop.
- Loads are serialised per source table: two requests that both need a table
  not loaded yet used to stream into the same staging table. A test with
  thirty-two concurrent callers found it.
