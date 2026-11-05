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
  `refresh_view`, `cache_stats`); `mcp.profile: admin` restores every tool
  and `mcp.tools` narrows by name. A name that does not exist refuses to
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

- **`query_metrics` joins along the declared keys.** A dimension or a filter
  written `table.column` belongs to a table the measured one references,
  directly or through others (`orders` with `customers.segment`, or with
  `branches.city` two keys away). The request never says how the tables are
  related: the path is read from the join graph, child to parent only, so each
  measured row meets at most one row and no sum is inflated. A table that
  holds several rows per measured row is refused with the way round, and so is
  a parent reached by two keys. The generated SELECT runs through the query
  pipeline like a caller's own: slices, row rules on every table, grounding,
  one audit event. Transforms cannot be combined with a join yet.
- **Business terms** (`olap.terms`, on by default). Catalog analysis writes
  one term per table: what the table is called, the words for each column and
  for the values it stores. The model can attach words only to names that
  exist. A term is read by nothing until a reviewer certifies it:
  `trio catalog review` shows each table's term and approving the table
  certifies it; `trio catalog approve` certifies none unless told to
  (`--certify-terms`); an expression is certified apart from its term. What
  was certified survives re-analysis. After NVIDIA Auto Ontology's term layer,
  which has no such gate.
- **The reviewed catalog names the measures.** With terms on, a table covered
  by a certified term takes its semantic model from the catalog: a key is
  counted, never summed; columns carry their descriptions; a certified
  expression is a measure (`paid_total:sum`). An expression must be a scalar
  over its own table's columns; anything else is refused when it is used.
- **Three tools**: `answer_question` answers a question asked in words
  (Turkish or English) by matching it against the certified vocabulary, with
  no model at query time, and returns the request it understood and the SQL
  with the rows; `search_terms` and `get_term` show the vocabulary. Offered
  over MCP only while `olap.terms` is on. `describe_table` adds the reviewed
  descriptions, and `check_readiness` says whether questions can be asked.
- **The proof gate.** A statement repaired by swapping one literal runs only
  when it is proven to be the original with that literal changed and nothing
  else (`prove_substitution`). `answer_question` uses it when the vocabulary
  spells a value differently from the data.
- **Models by job.** Catalog analysis and term extraction use
  `llm.reasoning`; translation uses `llm.non_reasoning` when that slot names a
  model of its own.
- The certified terms are published in `x-llm-context`, per table.

- **Measures suggested from the query history** (`trio catalog suggest`). The
  most-run statements (`pg_stat_statements`, `sys.dm_exec_query_stats`,
  `V$SQL`) and the view definitions of the schema are mined for the
  expressions they repeat: what sits inside an aggregate, arithmetic a
  statement selects, and each WHERE condition as a 1/0 flag. An expression is
  kept only when it is a scalar over one table of the catalog, with no
  parameter in it. A condition or arithmetic on a key or a date is dropped;
  what a view defines ranks first, the rest by how often it ran. The model
  names the candidates and may drop some, and never touches an expression.
  After NVIDIA Auto Ontology's history mining; reading views is new here.
- **A suggestion is a draft.** It is stored in the term of its table,
  uncertified, and is no measure until `trio catalog certify -e <name>`,
  which leaves the rest of the review as it is. A history that cannot be read
  is reported with the grant it needs, and the views are still used. See
  `docs/docs/query-history.md`.

- **An image that builds with no network.** `make wheelhouse` fetches the
  locked wheels once; `make image-offline` then builds with
  `docker build --network none`, hashes checked. Base images are pinned by
  digest. ODBC drivers are vendored (`docker/vendor/`) or left out; Oracle's
  client is never downloaded by the build.
- **A hardened runtime.** No curl in the image (the healthcheck is Python),
  uid 1000, and nothing written outside `/app/catalogs`, `/app/data`,
  `/app/logs` and `/tmp`, so it runs with a read-only root filesystem.
  `olap.temp_directory` says where DuckDB spills; its default is beside the
  working directory.
- **A Helm chart** (`helm/trio`): one replica replaced on change, config as a
  ConfigMap, secrets as files from an existing Secret, volumes for catalogs
  and data, optional ingress and a network policy that closes everything it
  does not name. Non-root, read-only, no capabilities, no service-account
  token.
- **A release smoke** (`make smoke`): the built image in production mode
  against a real PostgreSQL, checked for a tenant's rows and masks over REST,
  MCP and metrics, cache isolation, joins along keys, a repaired join, a
  question in words, and one audit event per read.
- **Documentation for the merged product**: deployment, architecture, the
  query history, and ADRs 10 to 13 (one process, the sync/async bridge,
  access by rewriting, the certified gate). `python -m trio` works.
- CI runs the smoke, the chart check, a strict docs build and a blocking
  dependency audit; a nightly workflow runs all four database engines and the
  no-network build.

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
- `olap.terms` is on by default because its benchmark gate passed
  (`bench/README.md`: 37 of 48 against the catalog arm's 34, nothing
  invented), not because the term arm was shown to be the more accurate one:
  that lead is within noise. On, catalog analysis makes one more model call
  per table; `terms: off` restores the old cost and hides the three tools. A
  catalog certified while the server runs is read at the next start. A term knows the stored values the catalog kept samples of,
  a handful per column. `answer_question` reads one measure, one breakdown
  and the conditions it has words for.
- History mining offers measures only: a selected `CASE` (a derived
  dimension) and a join condition written in a query are not suggested.
  PostgreSQL's history has its constants replaced, so conditions come from
  views there. The SQL Server and Oracle readers have not been run against a
  real server, and Oracle's AWR is not read (it needs the Diagnostics Pack).
- The `embed` and `rerank` slots are read and validated but nothing uses
  them.
- The Helm chart has been linted and rendered, never installed on a cluster.
  The CI workflows have never run: the repository has no remote. The vendored
  ODBC build paths (`WITH_MSSQL_ODBC=vendor`, `WITH_ORACLE_ODBC=1`) are
  written and unbuilt.
- The single-app test suite runs against a fake gateway; the same path over
  real PostgreSQL and SQL Server is in `tests/warp/integration` (needs Docker).
