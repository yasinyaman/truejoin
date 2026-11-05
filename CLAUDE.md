# trio-engine

One package (`trio`, a placeholder name; distribution `trio-engine`) made from
three projects with their histories: **Warp** (DB → governed REST/OpenAPI + LLM
catalog with draft/review/approve), **Fusion** (DuckDB in-memory OLAP + 13 LLM
tools over REST/MCP/SDK) and **Stargate** (OpenAPI → MCP converter). Target:
one process, one config, one CLI, one image, on-prem (KVKK, Oracle/SQL Server).
The phased plan and the Auto Ontology patterns being ported are recorded in
`CHANGELOG.md` and `docs/`.

## Commands

- Install: `uv sync --all-extras`
- Test: `uv run --frozen pytest` (Docker-backed suite: `uv run pytest tests/warp/integration -m integration`)
- Lint: `uv run --frozen ruff check trio tests bench demo` and `ruff format --check ...`
- Types: `uv run --frozen mypy trio` (strict on `domain`, `ports`, `application`)
- Lock check: `uv lock --check`
- Serve: `uv run trio serve -c config/database.yaml` — one process, one port (8000): the REST
  gateway under `settings.api_prefix`, OLAP at `/olap`, MCP (streamable HTTP) at `mcp.http_path`.
  `trio serve --transport stdio` runs the MCP tools alone. The ASGI target is `trio.main:app`.
- Catalog: `uv run trio catalog -c config/database.yaml {analyze,review,approve,export,list,info,enrich-openapi,pipeline}`
- Converter: `uv run trio mcp {convert,info,formats}` (generic OpenAPI/Postman → MCP definition)
- Config: `uv run trio config validate [--production]`, `uv run trio config show` (secrets masked)

Always validate with `uv run --frozen ...`; locked tool versions are newer than system installs.

## Layout

```
trio/
├── __init__.py (SDK), bootstrap.py (the one composition root: build_container, build_app,
│   build_olap, load_settings, convert_to_mcp), main.py (ASGI target), mcpgen.py (Stargate SDK),
│   runtime_mcpgen.py
├── domain/      olap/ (Fusion, whole)  gateway/ governance/ catalog/ (Warp, split)  mcp/ (Stargate)
├── ports/       flat: Fusion 6 + Warp 6 + Stargate 5 Protocols
├── application/ olap/ (Fusion)  gateway/ catalog/ container.py (Warp)  mcpgen/ (Stargate)
│                settings.py (the one Settings: databases, settings, olap, mcp; legacy env overlay)
├── adapters/outbound/  duckdb_store, sqlglot_*, memory_cache, threading_scheduler, registry,
│                       warp_inproc/ (GatewayLoop, InProcessSource: source type `warp`)
│                       warp_http/ (source type `warp-remote`)  db/ llm/ catalog_store/ export/
│                       audit/ yaml_config  mcp_gateway/ (Stargate parsers, httpx gateway)
├── adapters/inbound/   http/ (the app: lifespan, auth, gateway routes, olap.py mounts /olap + MCP)
│                       rest/ (olap_router factory)  mcp/ (curated server, mcp_sdk_host)
│                       cli/ (main.py: the `trio` tree; catalog.py, mcpgen.py)  sdk/
└── observability/      warp_logging.py
tests/{fusion,warp,stargate}/   origin suites    tests/unified/  settings + CLI    tests/e2e/  the single app
bench/  demo/  config/  docker/  docs/{origins,changelog,adr}  examples/mcpgen
```

## One process

- `GatewayLoop` is one daemon thread owning the asyncio loop the OLAP side's gateways live
  on. Sync code calls `loop.submit(coro)`; calling it from the loop thread raises. The HTTP
  routes keep their own gateways on the server's loop: two pools per database.
- OLAP handlers and MCP tools are plain `def` (threadpool / worker thread). Nothing on the loop
  thread calls back into the OLAP services; keep it that way.
- `InProcessSource.fetch_slice` streams through a `queue.Queue(maxsize=2)`; the loop task waits
  on an asyncio event, never on the queue. A value a column cannot take falls back to
  `fetch_slice_in_memory`; an unknown table or column raises before reaching the database.
- `SourceService` loads one source table at a time (`_loading`): staging tables share a name.
- `/olap` and MCP sit behind `olap_guard` (`http/olap.py`): the `query` permission. Each request
  becomes an `AccessScope` (`domain/governance/access.py`): row policy, per-source masking, actor.
- **Every analytical read takes a scope.** `QueryService.sql(..., scope=)`, `SemanticService.run(...,
  scope=)`, `ToolService.for_scope(scope)`. A restricted scope goes through `_sql_scoped`: each table
  is replaced by a subquery with the row conditions and masks (`SqlglotAccessRewriter`), nothing is
  pushed down, the cache key is the rewritten SQL. The rewriter fails closed: what it cannot wrap
  (mv_*, slices by name, table functions) raises `GuardrailViolation`. A new path that returns rows
  must take the scope or refuse a restricted one (`ToolService._require_unrestricted`).
- A condition or join key on a masked column must never narrow a fetch (`_scoped_shape`): the query
  compares the masked value, so fetching by the raw one would let a caller test guesses by counting.
- One `AuditEvent(action="query")` per analytical read, from `QueryService.record_access`. Names
  only, never SQL or values.
- No scope means unrestricted (the SDK). The MCP server behind HTTP reads the caller from a
  ContextVar set by `GuardedApp` and refuses when it is unset.

## Dependency rule

Enforced by `tests/fusion/architecture/test_dependency_rules.py` (one AST walker
for all three origins; it follows lazy imports):

| Layer | May import | Third-party |
|---|---|---|
| `trio.domain` | domain | pydantic only |
| `trio.ports` | domain, ports, `application.settings` (shortcut) | pydantic only |
| `trio.application` | domain, ports, application | pydantic, structlog (shortcut) |
| `trio.observability` | domain, application, observability | no |
| `trio.adapters.outbound` | domain, ports, outbound, `application.settings` (shortcut) | yes |
| `trio.adapters.inbound` | domain, ports, application, observability, inbound, `bootstrap`, `runtime_mcpgen`; `trio` and `trio.mcpgen` by exact name (version) | yes |
| root modules (`bootstrap`, `main`, `mcpgen`, `runtime_mcpgen`) | anything | yes |

The `shortcut:` comments in that test and in `pyproject.toml` are the debts
Phase 0 took on purpose; each names when to pay it.

## Conventions (carried over, still binding)

- All DuckDB access goes through `AnalyticsStore` (`DuckDBStore` holds the lock and the
  `enable_external_access=FALSE` latch). `SqlValidator.validate()` runs before any user SQL.
- Rows travel as `RowSet`; pandas/Arrow only inside adapters and `sdk/formats.py`.
- Slices never change a result; a table is "loaded" only when all of it is in the store.
- Warp: `create_table_as` only for `mv_*`; raw `/query/execute` is off by default and
  refused in production; masking uses the **approved** catalog only; a key with
  `row_filters` cannot run raw SQL.
- Tests use fakes, not mocks (`tests/fusion/fakes` incl. `FakeGateway`, `tests/warp/conftest.py`
  MockDatabaseAdapter). `tests/fusion/rest_app.py` puts the OLAP router on a bare app.
- Version lives only in `trio/__init__.py`; `tests/fusion/test_packaging.py` checks the
  CHANGELOG heading and the one console script (`trio`).
- Origin READMEs and changelogs are frozen under `docs/origins/` and `docs/changelog/`.
