# Changelog

One distribution (`trio-engine`, package `trio`) made from three: Warp 0.10.0,
Fusion 1.2.0 and Stargate 0.1.0. Their own changelogs are frozen under
[docs/changelog/](docs/changelog/).

## [1.0.0] - in progress

### Changed

- **National id numbers stay out of the catalog.** The first analysis run
  with a real model over a real database kept a `tc_kimlik` column's TC
  numbers in `sample_values` and as term values, where every catalog reader,
  a row-filtered tenant included, could read them: the personal-column check
  knew only English names, and the model typed the column `id`. Analysis now
  offers the model a `national_id` semantic type, the check knows `kimlik`,
  `tckn`, `tc_no` and `vergi_no`, and a personal tag the model adds on its own
  (`pii`, `personal`, `personal_info`, ...) counts too. A reviewer who marks a
  column personal (by semantic type or tag) clears what analysis kept of it.
  Write a masking rule for `national_id` to mask such columns.
- **Analysing some tables keeps the catalog's others.** `tables` on
  `POST /catalog/analyze` (and `--tables` on `trio catalog analyze`) used to
  write a catalog of just those tables, dropping the rest with their reviewed
  descriptions and terms; the others now stay as they were, unless they have
  left the database.
- **The catalog routes keep stored values from restricted callers**, by the
  rule the OLAP term tools already used: a caller with row rules on a table
  gets none of its columns' sample values or term values (they may come from
  other tenants' rows), and every caller gets none for a semantic type masked
  for them. Applies to the draft table detail and to `/export`; the stored
  catalog is unchanged.
- **`catalog.openapi_include_examples` publishes only what every caller may
  see.** The enriched spec is one document for everybody, so it now leaves
  out examples for personal columns (by name, semantic type or tag), for any
  semantic type a masking rule names, and for every table an API key has row
  rules on. The same holds for `trio catalog enrich-openapi` and the pipeline.
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
  certifies it; `trio catalog approve` and the HTTP review, which show no
  terms, certify none (`--certify-terms` does); an expression is certified
  apart from its term. What was certified survives re-analysis, with
  `olap.terms` on or off. After NVIDIA Auto Ontology's term layer,
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
- **A console at `/ui`**: static pages with no build step and a strict CSP,
  over the routes any client uses. *Query* browses the sources and runs SQL or
  a question in words under the caller's row rules and masks; *Catalog* is the
  review a steward does with `trio catalog review`: edit descriptions and
  semantic types, approve a table and certify its terms, certify draft
  expressions as measures. For it the draft table detail lists the covering
  terms, `POST /catalog/{db}/approve/{table}` takes `certify_term`, and
  `PATCH /catalog/{db}/terms/{term}` edits a term's words or certifies its
  expressions, `POST /catalog/{db}/terms/certify` certifies every term at
  once, and a relationship shows whether the data bears it out (`verified`).
  `/ui/access` tells the page what the key may do, so a key without `update`
  sees the review read-only.
  *Docs* is the API and MCP reference read live from this server: every REST
  operation from `/openapi.json` with its parameters and body, the MCP
  tools from the endpoint's own `tools/list` with their arguments, and how
  to connect a client (URL, header, stdio). No Swagger assets, so it works
  air-gapped under the same CSP.
  The catalog opens on an *Ontology overview*: what the catalog records and
  who wrote it, a diagram of the tables and their relationships (verified,
  unchecked, contradicted, declared key), every entity with its keys,
  indexes, tags, DB comments and columns (null, default, semantic type, tags,
  personal, masked for this key), the glossary of terms with their words and
  values, and every measure. It reads the new `GET /catalog/{db}/ontology`,
  the whole catalog in any status, with stored values withheld as on the
  table detail.
  A key with `create` can *analyze a database* from the console (all tables
  or the ones named, the seconds counted while the model works; the result is
  a draft), and a key with `update` can *suggest measures*, through the new
  `POST /catalog/{db}/suggest` (what `trio catalog suggest` does, audited the
  same way). `GET /catalog` now lists the `connected` databases, and
  `/ui/access` says whether the key may `create`.
- **Catalog changes are audited.** Every edit, approval, certification,
  analysis and deletion through the catalog routes leaves one `catalog_change`
  event naming what changed (fields, columns, terms, measures), never the
  text a reviewer wrote or an expression's SQL. `trio catalog` (analyze,
  pipeline, review, approve, suggest, certify) records the same events, with
  `cli:<login name>` as the actor.

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
- Six ways a restricted caller could read past its row rules or masks, found
  by review: a table sharing its alias or bare name with a subquery, VALUES,
  UNNEST or CTE was left unwrapped; a recursive CTE named like an `mv_*` view
  read the stored view (recursive CTEs are now refused); the `trio_pseudonym`
  hash function could be called to check guesses; a row rule written in
  another case than the table (`customers` for Oracle's `CUSTOMERS`) applied
  to nothing; a database with masking on but no approved catalog served
  masked callers raw values (it now refuses them); and `get_term` /
  `search_terms` showed stored values of masked or row-ruled columns.
- Nine more from the same review. Term extraction sent stored sample values
  to a cloud model with `analysis.share_samples_with_cloud_llm` off. A
  condition value the column cannot take (a date not written `YYYY-MM-DD`)
  read the whole table and kept an empty slice as complete; it is now
  refused. A LIMIT was read at the source before WHERE conditions the
  planner could not capture, or before an OFFSET. Editing a table's
  relationships dropped the data's verdict on them. A catalog relationship
  repeating a foreign key made its parent ambiguous, and a one-to-many
  written on the parent ran the wrong way. A table reached by two chains of
  keys (a buyer's country, a shop's country) was picked by declaration
  order; it is now left out, as one reached by two keys already was. And the
  image's `trio` launcher re-ran the server in every reload or worker process.
- Six more that hurt on default settings. A semi-join whose small side
  matched nothing sent `IN ()` to the database, which no engine parses; an
  empty list is now `1 = 0` (and an empty `in` row rule reads no row). A
  rate limit slowapi cannot read (`100/min`) turned throttling off; it is now
  refused when the config loads. A caller refused at `/olap` or `/mcp`
  (401/403) left no trace with the access log off; it is logged. `trio serve`
  with `API_WORKERS` above 1 built one analytical engine per worker; it now
  refuses, unless `olap.enabled` is false. The chart wrote an unquoted
  number in `secrets.create` or `env` changed (`0755` as `493`); it now
  fails the render. An Oracle-only image built without unixODBC and failed
  at the first connection; the build now stops, and the vendor README lists
  the packages.

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
  views there; SQL Server's and Oracle's keep them. Oracle's AWR is not read
  (it needs the Diagnostics Pack).
- The `embed` and `rerank` slots are read and validated but nothing uses
  them.
- The integration suite passes on all four engines: PostgreSQL and MySQL
  started by the tests themselves, SQL Server and Oracle through `make it-up`
  and `make it` (`docker/it`: a runner image with both ODBC drivers). That
  covers the adapters, the in-process OLAP path, declared-key joins and the
  history readers. SQL Server ran under amd64 emulation on Apple Silicon,
  not on x86-64 hardware; the nightly workflow that would do that has never
  run.
- The Helm chart has been linted and rendered, never installed on a cluster.
  The CI workflows have never run: the repository has no remote. The product
  image's vendored ODBC build paths (`WITH_MSSQL_ODBC=vendor`,
  `WITH_ORACLE_ODBC=1`) are written and unbuilt; the test runner installs
  both drivers the online way.
- The single-app test suite runs against a fake gateway; the same path over
  real PostgreSQL and SQL Server is in `tests/warp/integration` (needs Docker).
