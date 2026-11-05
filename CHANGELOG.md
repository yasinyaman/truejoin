# Changelog

One distribution (`trio-engine`, package `trio`) made from three: Warp 0.10.0,
Fusion 1.2.0 and Stargate 0.1.0. Their own changelogs are frozen under
[docs/changelog/](docs/changelog/).

## [1.0.0] - in progress

### Changed

- **One package.** The three hexagonal cores were moved into one tree with
  their histories: Fusion's domain and application stay whole under `olap/`,
  Warp's are split into `gateway/`, `governance/` and `catalog/`, Stargate's
  become `mcp/` and `mcpgen/`. Ports are one flat package. One dependency
  walker (`tests/fusion/architecture/test_dependency_rules.py`) replaces
  Fusion's walker, Warp's import-linter contracts and Stargate's architecture
  test.
- **Dropped.** Stargate's 14 deprecated shim modules, Warp's hashed
  requirements locks and `pydantic-settings`, the mcp 1.x host fallback.
- Python 3.12 is the floor; one `uv.lock`.

- **One process.** `trio serve` runs one ASGI app: the REST gateway as before,
  the OLAP routes at `/olap` and the MCP tools over streamable HTTP at
  `mcp.http_path` (`/mcp`). The OLAP side no longer calls the gateway over
  HTTP: `InProcessSource` reads each configured database through its own
  `DatabaseGateway`, on one loop thread (`GatewayLoop`) that owns those pools,
  using the read-only credentials when the database has them. The HTTP source
  remains for a Warp on another node as source type `warp-remote`
  (`olap.remote`); `warp` now means in-process.
- **One config.** The YAML file gains `olap:` (DuckDB, row budgets, cache,
  backups, refresh, semantic models, `grounding`, `terms`) and `mcp:`
  (`profile`, `tools`, `stdio_api_key_name`, `http_path`). `settings.llm` is a
  set of slots (`reasoning`, `non_reasoning`, `embed`, `rerank`) that fall back
  to `default`; the old flat block is read as `default`. `${file:/path}` reads
  a secret from a mounted file, and a missing file is an error.
- **One CLI.** `trio serve [--transport http|stdio]`, `trio catalog ...`
  (Warp's commands, plus `approve`), `trio mcp {convert,info,formats}`
  (Stargate's) and `trio config {validate,show}` (secrets masked) replace
  `trio-warp`, `trio-catalog`, `trio-olap-rest`, `trio-olap-mcp` and
  `trio-mcpgen`. The image runs `trio serve`; compose has one `api` service.
- **One auth.** The gateway's API keys guard `/olap` and MCP: the `query`
  permission is required, and a key's row rules no longer keep it out. Fusion's `AuthMiddleware`, its CORS and logging
  middleware and its single `FUSION_API_KEY` are gone; rate limiting applies to
  the `/olap` routes only.
- The MCP server exposes the `analyst` profile by default (no `create_view`,
  `refresh_view`, `cache_stats`); `mcp.profile: admin` restores all thirteen
  tools and `mcp.tools` narrows by name. A name that does not exist refuses to
  load.

- **Row rules and masks on every analytical query.** A caller with
  `row_filters`, or one the masking rules apply to, runs `/olap`, the MCP tools
  and `query_metrics` under an `AccessScope`: each table the query names is
  replaced by a subquery carrying that caller's row conditions and masks
  (`SqlglotAccessRewriter`), so aliases, aggregates, joins, CTEs and
  subqueries all read the restricted view. The masks produce what the table
  routes produce, `hash` included. What cannot be wrapped is refused:
  materialized views, slices by name, table functions, anything that is not a
  plain source table. Row conditions also reach the planner, so a tenant's
  query on a table too big to load fetches that tenant's slice; a condition on
  a masked column never narrows a fetch. Restricted queries are not pushed
  down and are cached under the rewritten SQL. `create_view`, `refresh_view`,
  `load_table` and the backup routes are refused for a restricted caller, and
  `query_metrics` refuses a question that reads a masked column.
- **One audit event per analytical query** (`action: query`): actor, tenant,
  roles, request id, tables, row count, whether row rules applied and which
  columns were masked. Never the SQL. Refusals are recorded with their status.
- **`check_readiness`**, a thirteenth tool: sources connected, tables
  discovered, the semantic layer present, an approved catalog per source.
- The gateway's raw `/query/execute` refuses a caller the table routes would
  mask; it used to return raw values to them.
- `trio serve --transport stdio` acts as `mcp.stdio_api_key_name` with that
  key's row rules and masks, instead of refusing such a key.

- **Joins are checked against the keys that exist** (`olap.grounding`, off by
  default). Each column-to-column join of a query is compared with the join
  graph: the foreign keys the databases declare plus the relationships of an
  approved catalog that the data bore out. Three verdicts, after NVIDIA Auto
  Ontology's join-path check: `wrong_column` (the tables are related by other
  columns), `missing_bridge` (only through a table in between), `unverified`
  (nothing relates them). `advise` runs the query and returns the verdicts
  with the result; `repair` swaps the column or joins the bridge table in when
  that is mechanical, returns the SQL it ran, and refuses the query otherwise.
  A repaired query is planned, restricted and audited like any other, so a
  table a repair adds is read through the caller's row rules. Two columns that
  are keys of different things are flagged whatever their values (serial ids
  overlap by coincidence); a column the graph does not know is let through
  when a bounded sample of its values is found in the other.
- **Foreign and primary keys travel with the schema**: `TableSchema` carries
  them, the in-process source reads them from the gateway, `/schema` reports
  `foreign_keys` for a Warp on another node, and `describe_table` and the
  schema context list them.
- **Catalog relationships are checked against the data.** Analysis marks each
  inferred relationship `verified`: true for a declared key or when sampled
  values of the source column exist in the target, false when none do or a
  name does not exist. A contradicted one stays in the catalog, flagged for
  the reviewer, and is left out of the OpenAPI context and the join graph.
- `check_readiness` reports the join graph when grounding is on, and is not
  ready for a source with no key and no verified relationship.

### Deprecated

- Every `FUSION_*` / `WARP_*` variable is still applied on top of the config
  file and logged as deprecated: the engine ones map to `olap.*`, the
  Warp-over-HTTP ones to `olap.remote.*`, `FUSION_API_KEY` becomes an API key
  named `fusion` with `all` permissions, and `FUSION_ENV`, `FUSION_HOST`,
  `FUSION_PORT`, `FUSION_CORS_ORIGINS`, `FUSION_LOG_LEVEL`, `FUSION_LOG_FORMAT`
  stand in for `APP_ENV`, `API_HOST`, `API_PORT`, `CORS_ORIGINS`, `LOG_LEVEL`,
  `LOG_FORMAT` when those are unset. `FUSION_LOG_FILE` is no longer read.

### Fixed

- Two requests that both needed a table not loaded yet streamed into the same
  staging table and published over each other; one failed and could leave the
  table dropped. Loads are now serialised per source table.
- The default config path pointed inside the package after the move
  (`trio/config/database.yaml`); it is the repository's `config/database.yaml`.

### Not yet

- A restricted caller's queries always run locally, a query that is not a
  flat SELECT loads whole tables for them, and `search_data` on a table too
  big to load is refused for them where an unrestricted caller would get a
  slice.
- The gateway's raw `/query/execute` still runs outside this pipeline: no row
  rules (such keys are refused there), no audit event per query.
- Grounding judges joins written as `ON` or `WHERE` equalities between two
  columns; `USING` and `NATURAL JOIN` are not judged, a composite key is
  checked one column at a time, and a materialized view's SQL is not checked.
  A catalog approved while the server runs joins the graph at the next start.
- `olap.terms` and the `embed`/`rerank` slots are read and validated but
  nothing acts on them; every LLM call uses the `reasoning` slot.
- The single-app test suite runs against a fake gateway; the same path over
  real PostgreSQL and SQL Server is in `tests/warp/integration` (needs Docker).
