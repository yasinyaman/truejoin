# trio-engine

One package (`trio`, a placeholder name; distribution `trio-engine`) made from
three projects with their histories: **Warp** (DB → governed REST/OpenAPI + LLM
catalog with draft/review/approve), **Fusion** (DuckDB in-memory OLAP + 12 LLM
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
- Servers (Phase 0, still separate processes): `trio-warp` (REST :8000, YAML config),
  `trio-olap-rest --auto-discover` (OLAP REST :9000, env config), `trio-olap-mcp` (stdio),
  `trio-catalog` (catalog CLI), `trio-mcpgen` (generic OpenAPI → MCP)

Always validate with `uv run --frozen ...`; locked tool versions are newer than system installs.

## Layout

```
trio/
├── __init__.py, bootstrap.py (Fusion), bootstrap_warp.py, bootstrap_mcpgen.py,
│   runtime_mcpgen.py, main.py (Warp ASGI), cli.py (Warp catalog CLI), mcpgen.py (Stargate SDK)
├── domain/      olap/ (Fusion, whole)  gateway/ governance/ catalog/ (Warp, split)  mcp/ (Stargate)
├── ports/       flat: Fusion 6 + Warp 6 + Stargate 5 Protocols
├── application/ olap/ (Fusion)  gateway/ catalog/ settings.py container.py (Warp)  mcpgen/ (Stargate)
├── adapters/outbound/  duckdb_store, sqlglot_*, memory_cache, threading_scheduler, registry,
│                       warp_http/ (Fusion's HTTP source)  db/ llm/ catalog_store/ export/ audit/
│                       yaml_config (Warp)  mcp_gateway/ (Stargate parsers, httpx gateway)
├── adapters/inbound/   rest/ mcp/ cli/ sdk/ (Fusion)  http/ cli/catalog.py (Warp)
│                       mcp/mcp_sdk_host.py cli/mcpgen.py (Stargate)
└── observability/      logging.py (Fusion), warp_logging.py
tests/{fusion,warp,stargate}/   origin suites, each with its own conftest
bench/  demo/  config/  docker/  docs/{origins,changelog,adr}  examples/mcpgen
```

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
| `trio.adapters.inbound` | domain, ports, application, observability, inbound, `bootstrap*`, `runtime_mcpgen` | yes |
| root modules (`bootstrap*`, `main`, `cli`, `mcpgen`, `runtime_mcpgen`) | anything | yes |

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
- Tests use fakes, not mocks (`tests/fusion/fakes`, `tests/warp/conftest.py` MockDatabaseAdapter).
- Version lives only in `trio/__init__.py`; `tests/fusion/test_packaging.py` checks the
  CHANGELOG heading and the console scripts.
- Origin READMEs and changelogs are frozen under `docs/origins/` and `docs/changelog/`.
