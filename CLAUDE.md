# trio-engine

One package (`trio`, a placeholder name; distribution `trio-engine`) made from
three projects with their histories: **Warp** (DB → governed REST/OpenAPI + LLM
catalog with draft/review/approve), **Fusion** (DuckDB in-memory OLAP + 16 LLM
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

## Grounding

- `SchemaCatalog.joins` is the `JoinGraph` (`domain/catalog/join_graph.py`): declared foreign keys
  from `TableSchema.foreign_keys`, single-column primary keys as known keys, plus what
  `set_relationships` adds from an approved catalog (`relationship_edges`: `verified is True` only,
  loaded once at startup by `http/olap.py::load_catalogs`).
- `QueryService._ground` runs right after validation, before the scoped/open split, so a repaired
  query is planned, wrapped in the caller's views and audited like the caller's own. Modes
  (`olap.grounding`): `off`, `advise` (verdicts ride on `QueryResult.grounding`), `repair` (fix or
  raise `QueryError("Grounding: ...")`; an unresolved verdict never runs).
- `SqlglotGrounder` accepts a join when both columns are in the same key class (`same_key`), judges
  only joins where a side is a known key, and repairs only what is mechanical: one candidate edge
  for `wrong_column`; one table in between, in the ON of an inner/left join, not yet in the query
  and not a shared parent, for `missing_bridge`.
- Value overlap (`SourceService.value_overlap`, three bounded reads per direction, memoised until
  the next refresh) vouches for a join only when one column is not a known key. Two keys of
  different things are never compared: serial ids overlap by coincidence. A masked column is never
  probed on a caller's behalf.
- `verify_relationships` (`application/catalog/relationship_pruning.py`) marks, never removes; a
  relationship with `verified is False` is hidden from agents (`joinable_relationships`).

## Term layer (`olap.terms`, on by default)

- A `Term` (`domain/catalog/terms.py`) is what people call a table, its columns and their stored
  values, plus `SqlAttribute` expressions. `extract_terms` writes one per table during catalog
  analysis (reasoning slot, only when `olap.terms` is on); the model can attach words only to names
  that exist. A certified term survives re-analysis (`carry_over`).
- **Nothing reads a term until it is certified.** `DatabaseCatalog.certified_terms` is the one
  door: approved catalog and `term.certified`. `approve_table` certifies the table's term (the
  reviewer just saw it); `approve_catalog` certifies nothing; `catalog approve --certify-terms`
  says so in words. An expression is certified apart from its term.
- `load_catalogs(..., terms=True)` hands each source's catalog to `SemanticService.use_catalog`.
  `model_for` is then configured, else catalog (`semantic_from_catalog`: keys are counted not
  summed, descriptions, certified expressions as `Measure.expression`), else inferred.
- A certified expression is the one place SQL from outside reaches the compiler. `_expression_sql`
  holds it to a scalar over its own table's columns: no subquery, aggregate, window or qualified
  name. `Measure.reads` (parser-free, `columns_read`) is what the slice must hold.
- **`query_metrics` joins along keys, never along the request.** `orders` + `customers.segment`:
  `JoinGraph.parents` gives child-to-parent paths only (a row meets at most one row, so no sum is
  inflated); a parent reached by two keys is left out. `compile_joined` emits one flat SELECT that
  runs through `QueryService.sql(local_only=True)`, so slices, row rules, grounding and the audit
  event are the pipeline's. Transforms are refused with a join; a masked column or join key is
  refused for a restricted caller (`_refuse_masked`).
- `application/olap/lexicon.py` is the question planner (no model): `SemanticService.ask` builds it
  from the certified terms per call, leaving out the stored values of columns masked for the
  caller. `answer_question` = ask, literal check, run; the result carries `request` and `sql`.
- The proof gate (`domain/olap/literal_repair.py::prove_substitution`): a candidate statement runs
  only if it is the original with one string literal swapped, literal by literal, and passes the
  validator. `answer_question` uses it when a vocabulary value matches no row but a stored value
  folds to the same word.
- `Settings.offered_tools()` hides `search_terms`, `get_term`, `answer_question` from MCP while
  `olap.terms` is off; naming one in `mcp.tools` then refuses to load.
- The bench gate (`bench/runner.py::term_gate`, thresholds committed before the arm) passed on
  2026-11-05 at `3d43434`: 37/48 against the catalog arm's 34/48, nothing invented, p = 0.58 on
  accuracy. The user turned `olap.terms` on by default on that pass, knowing the lead is within
  noise; on costs one more model call per table at analysis and changes no query until a term is
  certified. Read `bench/README.md` before quoting the numbers, and do not tune the planner
  against the 48.

## Query-history mining (`trio catalog suggest`)

- `QueryHistorySource` (`ports/query_history.py`) has two reads: `recent_statements` and
  `view_definitions`. `QueryHistoryReader` (`adapters/outbound/db/history.py`) holds the SQL per
  engine and what each needs granted; anything that fails becomes `HistoryUnavailableError` with
  that grant in the message. It passes only the parameters a query names: the gateway's binder
  refuses an unused one.
- `SqlglotExpressionMiner` takes aggregate arguments, selected arithmetic and WHERE conditions
  (as `CASE WHEN ... THEN 1 ELSE 0 END`), each bound to one catalog table and rendered for DuckDB.
  It refuses exactly what `_expression_sql` refuses, plus parameters, so a mined expression is
  never rejected later for its shape (a test holds the two together).
- `history_mining.py`: `rank_candidates` (views first, then calls; conditions and arithmetic on
  keys or dates dropped), `name_candidates` (the model returns ids, names and descriptions, never
  SQL; a failing model keeps every candidate under a mechanical name), `HistoryMiningService.suggest`.
- Drafts go through `catalog_review.add_sql_attributes`, which forces `certified=False`.
  `certify_expressions` is the only way one becomes a measure, and it does not reopen the review.
- Verified on real PostgreSQL (with and without `pg_stat_statements`) and MySQL; the SQL Server and
  Oracle queries are unrun. `pg_stat_statements` replaces constants with `$n`, so on PostgreSQL
  literal conditions come from views only.

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
