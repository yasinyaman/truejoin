# truejoin

One package (`truejoin`; distribution `truejoin`, repository github.com/yasinyaman/truejoin) made from
three projects with their histories: **Warp** (DB → governed REST/OpenAPI + LLM
catalog with draft/review/approve), **Fusion** (DuckDB in-memory OLAP + 18 LLM
tools over REST/MCP/SDK) and **Stargate** (OpenAPI → MCP converter). Target:
one process, one config, one CLI, one image, on-prem (KVKK, Oracle/SQL Server).
The phased plan and the Auto Ontology patterns being ported are recorded in
`CHANGELOG.md` and `docs/`.

## Commands

- Install: `uv sync --all-extras`
- Test: `uv run --frozen pytest` (Docker-backed suite: `uv run pytest tests -m integration`, which
  starts PostgreSQL and MySQL; SQL Server and Oracle run in containers with their ODBC drivers:
  `make it-up`, `make it`, `make it-down`, see `docker/it/README.md`. It needs about 10 GB of
  Docker disk and passed on 2026-11-05: 34 on the host, 36 in the runner.)
- Lint: `uv run --frozen ruff check truejoin tests bench demo` and `ruff format --check ...`
- Types: `uv run --frozen mypy truejoin` (strict on `domain`, `ports`, `application`)
- Lock check: `uv lock --check`
- Serve: `uv run truejoin serve -c config/database.yaml` — one process, one port (8000): the REST
  gateway under `settings.api_prefix`, OLAP at `/olap`, MCP (streamable HTTP) at `mcp.http_path`.
  `truejoin serve --transport stdio` runs the MCP tools alone. The ASGI target is `truejoin.main:app`.
  `config/database.yaml` is the minimal file (both console stores on: set `TRUEJOIN_SECRET_KEY`);
  `config/reference.yaml` lists every setting; `truejoin config import` moves a full file to the console.
- Catalog: `uv run truejoin catalog -c config/database.yaml {analyze,review,approve,suggest,certify,export,list,info,enrich-openapi,pipeline}`
- Converter: `uv run truejoin mcp {convert,info,formats}` (generic OpenAPI/Postman → MCP definition)
- Config: `uv run truejoin config validate [--production]`, `uv run truejoin config show` (secrets masked)
- Users: `uv run truejoin users {list,add,passwd,disable,enable} -c <config>` (`add NAME --permission …
  --display --tenant --password`): the first local administrator, and getting back in; the store is
  `settings.auth.users.store`. `config validate` also refuses a console store without its key.
- Pilot: `uv run truejoin pilot run questions.yaml --as <key> [--with-questions]` (report: ids, counts,
  verdicts; never values or SQL) and `truejoin pilot compare "<question>" --as a --as b`. Guide:
  `docs/docs/pilot.md`; example reports: `python -m bench.pilot` → `bench/results-pilot/`.
- Release: `make image`, `make wheelhouse` then `make image-offline` (no network), `make smoke`
  (the built image in production mode against a real PostgreSQL, 24 checks; run it before
  shipping), `make helm-check`, `make docs`.

Always validate with `uv run --frozen ...`; locked tool versions are newer than system installs.

## Layout

```
truejoin/
├── __init__.py (SDK), bootstrap.py (the one composition root: build_container, build_app,
│   build_olap, load_settings, convert_to_mcp), main.py (ASGI target), mcpgen.py (Stargate SDK),
│   runtime_mcpgen.py
├── domain/      olap/ (Fusion, whole)  gateway/ governance/ catalog/ (Warp, split)  mcp/ (Stargate)
│                settings_catalog.py (which settings are bootstrap, dangerous, secret)
├── ports/       flat: 33 Protocols in 24 modules (stores: key_store, user_store, settings_store,
│                database_store)
├── application/ olap/ (Fusion)  gateway/ catalog/ container.py (Warp)  mcpgen/ (Stargate)
│                settings.py (the one Settings: databases, settings, olap, mcp; legacy env overlay)
│                settings_service.py access_keys.py local_users.py databases.py usage.py
├── adapters/outbound/  duckdb_store, sqlglot_*, memory_cache, threading_scheduler, registry,
│                       warp_inproc/ (GatewayLoop, InProcessSource: source type `warp`)
│                       warp_http/ (source type `warp-remote`)  db/ llm/ catalog_store/ export/
│                       audit/ yaml_config  mcp_gateway/ (Stargate parsers, httpx gateway)
│                       database_store/ key_store/ settings_store/ user_store/ (the console's files)
├── adapters/inbound/   http/ (the app: lifespan, auth, gateway routes, olap.py mounts /olap + MCP)
│                         console/ (the /ui pages)  routes/ (audit catalog crud databases export keys
│                         query schema settings users)  oidc.py ldap.py sessions.py governance.py
│                       rest/ (olap_router factory)  mcp/ (curated server, mcp_sdk_host)
│                       cli/ (main.py: the `truejoin` tree; catalog.py, mcpgen.py, users.py)  sdk/
└── observability/      warp_logging.py
tests/{fusion,warp,stargate}/   origin suites    tests/unified/  settings + CLI    tests/e2e/  the single app
bench/  demo/  config/  docker/  docs/{origins,changelog}  docs/docs/{adr,…}  examples/mcpgen
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
- **The console speaks two languages.** Text written in a console script goes through `tr("...")`
  or ``tr`... ${x} ...` `` (i18n.js; Turkish in tr.js, built by `scripts/build_console_tr.py`). After
  adding text: add its Turkish, then `python scripts/console_i18n.py wrap`. Never pass a value
  from the server through `tr` (the settings' help and section names are the one exception: they
  are interface text). No local variable may be called `tr`.
- **The console is Carbon.** `el()` (console.js) turns `<button>` into `cds-button` (class gives the
  kind; `item`/`link` stay native) and `.badge` spans into `cds-tag` (carbon.js); use `ask()` for a
  confirmation, never `confirm()`. Colours come only from Carbon tokens (`--cds-*`, via console.css's
  own names); theme.js sets `cds-theme-zone-g10/g100`. New Carbon components: add the entry to
  `scripts/vendor_carbon.py` and re-vendor; the console test checks every imported chunk exists.
- **Settings change at run time.** `SettingsService` (`application/settings_service.py`) lays the
  console's store over the file; `domain/settings_catalog.py` says what is bootstrap (file only),
  dangerous (typed confirmation) or secret. A change rebuilds the whole stack: `start_generation`
  builds on a shadow `FastAPI`, `swap` puts its routes in, `_retire` closes the old one later
  (`http/app.py`). Anything the lifespan builds belongs in `_build`, and must be closed by its
  stack; a new setting needs no live wiring, but a new bootstrap one belongs in `BOOTSTRAP`.
- **Databases change at run time.** `LiveDatabases` (`http/app.py`) attaches and detaches a
  database added from the console (`database_store.path`): REST routes (recorded per database in
  `runtime.routes_of`, the admin routes moved back first), `settings.databases`, maskings and the
  OLAP source through `FusionApp.attach_database`. A new path that reads `settings.databases` or
  `runtime.gateways` must cope with the list changing; one that keeps a copy goes stale.
  Passwords live encrypted in the store (`EncryptedJsonDatabaseStore`, Fernet key in
  `TRUEJOIN_SECRET_KEY`) and never in an answer, log or audit event (`managed.scrub`).
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

## Signing in

- Four ways in, all checked by `AuthManager` (`http/auth.py`): API keys (configured, or handed out
  from the Keys tab), bearer tokens from an identity provider (`auth.oidc`, `http/oidc.py`), the
  directory (`auth.ldap`, `http/ldap.py`, presets lldap/openldap/active_directory/freeipa) and local
  users (`auth.users`, `application/local_users.py`, the console's Users tab).
- A password sign-in (directory or local) posts to `/ui/sign-in/password` and gets an HS256 token from
  `http/sessions.py`, keyed from `TRUEJOIN_SECRET_KEY` (by that name). A local name is only checked
  locally; any other goes to the directory. A local user's token carries the name and a fingerprint of
  the password hash, and is checked against the account on every use.
- User management is under `{api_prefix}/auth/users`, never `/users`: a database's own `users` table
  is served there. `auth` and `settings` are reserved database names (`domain/gateway/managed.py`).
- Whoever makes or changes a user follows the key rule (`domain/governance/keys.py`): nothing beyond
  what they hold, exactly their roles, nothing held to row rules, no account stronger than their own,
  and never their own account's lock-out.

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

## Query-history mining (`truejoin catalog suggest`)

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
- Verified on all four engines (and MariaDB). `pg_stat_statements` replaces constants with `$n`, so on
  PostgreSQL literal conditions come from views only; SQL Server, Oracle and MySQL 8 (its digest's
  `QUERY_SAMPLE_TEXT`) keep them. MariaDB has no sample column, so the reader falls back to the
  `?`-replaced `DIGEST_TEXT`. On Oracle the `V_$SQL`
  grant has to come from `SYS`.
- Real engines taught three things unit tests missed: the gateway's binder refuses a parameter the
  query does not name; engines store a view's condition in parentheses of their own; and MySQL
  ignores a column's inline `REFERENCES`, so only a table-level `FOREIGN KEY` declares a key there.
  Oracle reports table and column names in upper case and everything downstream copes, including
  SQL written in lower case.

## Packaging

- `Dockerfile`: `DEPS=online|wheelhouse` picks the dependency stage; the project is never
  installed as a package (copied beside the venv, started by a launcher), so the offline path
  needs no build backend. Base images are pinned by digest; the runtime has no curl (the
  healthcheck is Python) and writes only to `/app/{catalogs,data,logs}` and `/tmp`.
- A read-only root needs `olap.temp_directory`: DuckDB's default spill directory is beside the cwd.
- `docker/smoke/` + `scripts/smoke.py` are the release check. Its catalog is generated by
  `docker/smoke/make_catalog.py` and checked in; regenerate after a catalog format change.
- `helm/truejoin`: one replica, `Recreate` (a process holds what it loaded). `helm/snapshots/` are the
  reviewed renderings; a template change must update them (`tests/unified/test_packaging.py`).
- Production refuses the default `auth.public_paths` (it includes `/openapi.json`): a production
  config sets `public_paths: ["/health"]`.
- CI (`.github/workflows`) has never run: the repository is pushed to github.com/yasinyaman/truejoin
  (private) with Actions off. `docs.yml` publishes to `gh-pages` on a push to `main`: decide about
  Pages before turning Actions on. Every command CI calls was run locally.

## Dependency rule

Enforced by `tests/fusion/architecture/test_dependency_rules.py` (one AST walker
for all three origins; it follows lazy imports):

| Layer | May import | Third-party |
|---|---|---|
| `truejoin.domain` | domain | pydantic only |
| `truejoin.ports` | domain, ports, `application.settings` (shortcut) | pydantic only |
| `truejoin.application` | domain, ports, application | pydantic, structlog (shortcut) |
| `truejoin.observability` | domain, application, observability | no |
| `truejoin.adapters.outbound` | domain, ports, outbound, `application.settings` (shortcut) | yes |
| `truejoin.adapters.inbound` | domain, ports, application, observability, inbound, `bootstrap`, `runtime_mcpgen`; `truejoin` and `truejoin.mcpgen` by exact name (version) | yes |
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
- Version lives only in `truejoin/__init__.py`; `tests/fusion/test_packaging.py` checks the
  CHANGELOG heading and the one console script (`truejoin`).
- Origin READMEs and changelogs are frozen under `docs/origins/` and `docs/changelog/`.
