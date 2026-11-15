# Changelog

One distribution and package, `truejoin` (called `trio` until 2026-11-13), made
from three: Warp 0.10.0, Fusion 1.2.0 and Stargate 0.1.0. Their own changelogs are frozen under
[docs/changelog/](docs/changelog/).

## [1.0.0] - in progress

### Added

- **Users made and managed in the console.** `settings.auth.users` keeps local
  users in a file (scrypt hashes, mode 0600); administrators make them, change
  what they may do, set their passwords, disable and remove them from a new
  **Users** tab (Turkish and English), or over `/auth/users`; a user changes
  their own password with the current one. The first administrator comes from
  `truejoin users add`. They sign in like a directory user, with a signed
  token that is checked against the account every time it is used: disabling a
  user, narrowing their permissions or changing their password takes effect at
  once. Nobody gives more than they hold, reaches an account stronger than
  their own, or locks themselves out; a missing name costs a hash too, wrong
  passwords pause the name, and every change is an audit event without a
  password. The routes are under `/auth/users`, not `/users`, which a
  database's own `users` table already serves.
- **Sign in with a directory: lldap, OpenLDAP, Active Directory, FreeIPA.**
  `settings.auth.ldap` checks a username and password against the company's
  directory (a service account finds the user, a bind as the user checks the
  password, groups become roles through `group_roles` and `role_permissions`),
  with presets that fill each product's filters. The console gets a
  *Sign in with a password* dialog (Turkish and English); scripts post to
  `/ui/sign-in/password`. What comes back is a short-lived token signed by
  truejoin and accepted as a bearer token everywhere an identity provider's is.
  The username is escaped into the filter, an empty password never reaches the
  directory, wrong passwords and unknown users read the same, repeated failures
  pause the username (and, ten times looser, the address), and production
  refuses plain `ldap://`, a missing `TRUEJOIN_SECRET_KEY` and `auth.ldap`
  with authentication off. New dependency: `ldap3`. Checked against a real
  lldap, which found what the in-memory directory could not (ldap3 wants whole
  seconds for its receive timeout). See *Signing in with a directory* in the
  deployment guide.
- **Settings changed from the console and served at once; the file keeps only
  what the server needs to start.** With `settings.settings_store.path` set,
  the console's new **Settings** tab (and `GET`/`PUT /settings`, `admin`)
  changes every setting the file does not write: limits, exports, raw SQL,
  masking, catalog and analysis options, the models, the analytics engine's
  budgets, cache and rate limit, remote sources, the MCP tools. A change is
  validated as a whole, kept, and served by a new generation of the server
  built beside the running one and swapped in; requests in flight finish on
  the old one, closed 30 s later. One that does not build changes nothing.
  - Kept in the file only: authentication, the audit trail on/off and its
    file, the pseudonym key, the stores, the URL layout and the DuckDB core
    (`docs/docs/configuration.md`). Anything the file writes is locked in the
    console; a value read from `${VAR}` stays in the file.
  - Sensitive settings are applied only with their names typed back. Secrets
    (`*.api_key`) are encrypted with `TRUEJOIN_SECRET_KEY` and never shown.
    The trail names the paths changed, never the values.
  - `truejoin config import -c full.yaml --minimal-out database.yaml` moves a
    file's settings to the store and writes the minimal file.
  - `config/database.yaml` is now that minimal file, with both stores on; the
    commented full reference moved to `config/reference.yaml`.
  - The server's lifespan builds a `Generation` (`http/app.py`): connections,
    routes, OLAP and MCP owned by one long-lived task, so a rebuild can run
    beside the generation serving. Other workers follow a change within the
    30 s catalog poll.

- **Databases added, changed and removed from the console, without a
  restart.** With `settings.database_store.path` set, the console's new
  **Databases** tab (and `GET`/`POST /databases`, `POST /databases/test`,
  `PUT`/`DELETE /databases/{name}`, all `admin`) adds a connection, tests it,
  changes it and removes it. A database is connected before it is kept and is
  served at once by its table routes, `/olap`, MCP and the catalog; a change
  that cannot connect leaves the old one serving; removing takes its routes
  and OLAP source down at once.
  - Passwords are encrypted in the store with the Fernet key in
    `TRUEJOIN_SECRET_KEY` (`database_store.key_env`); the server refuses to
    start with the store set and no key. No answer, log line or audit event
    carries one, and a driver's message has it taken out.
  - At startup an added database that cannot be reached is listed as not
    connected instead of stopping the start; a configured one still stops it.
  - With authentication off, changes are refused outside `APP_ENV=development`.
  - `access_change` audit events: `add_database`, `update_database`,
    `remove_database <name>`. Configured databases are listed read-only.
  - New: `domain/gateway/managed.py`, `ports/database_store.py`,
    `adapters/outbound/database_store/encrypted_json.py`,
    `application/databases.py`, `routes/databases.py`, `console/databases.js`;
    `LiveDatabases` in `adapters/inbound/http/app.py` attaches and detaches,
    and `FusionApp.attach_database` connects one more OLAP source.

- **`truejoin pilot`: a customer's own questions, judged.** `truejoin pilot
  run questions.yaml --as <key>` asks each question through `answer_question`
  as that key, row rules and masks included, and runs the expected answer (a
  query, or the rows themselves) as the same key. Each question is then
  **correct**, **refused**, **wrong** or **not checked**. Answers are judged by
  their rows, never by their SQL. A question is left unchecked when its
  expected query fails or the answer reaches the 100-row tool cap.
  - The report lists wrong answers first and states whether the pilot passed
    (at most `--max-wrong`, 2 by default).
  - It holds counts and verdicts only, with no row values and no SQL, so it can
    leave the company. `--with-questions` adds the question text and refusal
    messages, for the customer's own copy.
  - `truejoin pilot compare "<question>" --as a --as b` asks one question as
    several keys, one after the other, with each key's rows. This output does
    hold row values.
  - The guide is `docs/docs/pilot.md` and the question template is
    `examples/pilot/questions.yaml`.
  - `python -m bench.pilot` writes example reports into `bench/results-pilot/`.
    It uses 20 bench questions never tuned on: the 16 held-out ones and 4
    unreadable ones. Asked as a key with e-mail and names masked, the result is
    11 correct, 6 refused and 3 wrong. The 3 wrong are the `terms` arm's
    held-out misses. Two of the refusals are the e-mail questions, which the
    mask refuses.
  - Code: `domain/olap/pilot.py`, `application/olap/pilot.py`.

### Changed

- **`olap.unread_questions` is `sql` by default.** A question the vocabulary
  cannot read (no known word, no naming word the reading uses, or tables no
  one request joins) goes to the configured model, which writes SQL from the
  schema the caller may see; the question and that schema leave the server
  when the model is a cloud one (stored values only with
  `analysis.share_samples_with_cloud_llm`). With no usable model (no key for
  the provider) the server starts and such questions are refused, as with
  `refuse`, which keeps the old behaviour.

- **A question that was not read says why, and what to do.** Asked about a
  database whose terms nobody had certified, the console answered with the
  message written for models and a row per word offering only the glossary
  and another database's terms. Now it says, in its own words and once, that
  none of the words are in the certified vocabulary, and then what is true of
  the catalogs: the terms questions are read with, the draft terms waiting to
  be certified in each catalog (with a way to the review), and the languages
  the words are in. Question mode shows the first two before anything is
  asked.
  - *Words as typed.* `check_question` gives each word also as `written`
    ("sipariş" for "siparis"), an answer read in part carries
    `unread_written` beside `unread`, and that is the spelling shown and added.
  - *A model proposes the places.* **Propose places** asks a model where each
    word belongs (`POST /catalog/suggest-words`, `update`) and points each row
    there with its reason; a person adds them, one by one or all the proposed
    at once. The model sees the words and the names in the approved catalogs
    (terms, columns, their synonyms, the terms' descriptions), never a stored
    value or the question; a place it names that was not offered is dropped.
    The audit trail counts the words. Draft terms take words too, and read
    them once certified. It is not an MCP tool.
  - *The settings that would have read it.* An administrator gets a link to
    `olap.unread_questions` when a model could have written SQL for the
    question, and to the languages.
  - *Languages are set from the console.* `settings.i18n.languages` and
    `translation_strategy` were hidden as settings nothing read; the analysis
    reads both, and the terms' words are drafted in every language listed.
    A catalog written before a language was added says so, and how to get
    the words. `translation_strategy` now takes only `single` or `multi`.

- **The console after a UX review: fourteen findings fixed.**
  - *Where you are.* The address carries the view (`/ui#settings`): a reload
    comes back to it and Back and Forward work. One `main`, a skip link, the
    view named in the page title, focus moved to it on a change.
  - *Settings.* An edit is kept until it is saved or discarded: changing
    section no longer loses it, a bar at the bottom counts what is unsaved
    across sections and saves it together, and leaving the page warns. Every
    setting says what it does (`domain/settings_catalog.HELP`, tested to be
    complete), JSON settings show an example, and the model slots are grouped.
  - *What an action answered* stays in sight: each view's status line is a
    sticky notification, not a line scrolled off the top.
  - *Who you are.* With authentication off the header says so and asks for no
    key; a connected key shows its name and a Disconnect; an unknown or missing
    key says so. `/ui/access` reports `auth_enabled`.
  - *Errors read as sentences.* No status code before the message; a typo is
    "This is not a query", not "Blocked Alias statement"; an empty Run says so.
  - *The way to questions.* Question mode says, before a run fails, that it
    needs certified terms, lists the three steps and links to Catalog
    (`check_readiness` now counts the tables with a certified term).
  - *Carbon, used as meant.* Lesser actions are tertiary buttons, not filled
    dark ones; fields on a white card are visible; review cells have room;
    green means it works, red that it does not, blue informs, purple asks for
    care; database and key forms get a wider panel.
  - *Small things.* A source no longer says "warp"; the shortcut reads ⌘ Enter
    on a Mac.
- **The console in Turkish.** A language switch in the header (EN/TR; the
  browser's language by default, the choice kept in the browser). 595 texts:
  every script's own, the page's, and the settings' sections and help.
  `tr("Save")` and ``tr`Remove ${x}?` `` look a text up in `console/tr.js`;
  only text written in the scripts is ever translated, never a value from the
  server, so a column called "Name" stays "Name". Server messages, setting
  names and the API reference stay in English. `scripts/console_i18n.py`
  finds and wraps the texts; a test fails on a wrapped text with no
  translation, or a translation that invents a value.

- **The console is built on the Carbon Design System.** The header and
  navigation are Carbon's UI shell (`cds-header`, and its side navigation on
  narrow screens); buttons are `cds-button`, status labels `cds-tag`, and
  confirmations a `cds-modal` instead of the browser's dialog. Fields and
  tables follow Carbon's text input and data table on its own theme tokens,
  so the form code is unchanged and large results stay fast. The g10 theme in
  light, g100 in dark, following the system; IBM Plex Sans and Mono.
  - Carbon web components 2.66.0 (Apache-2.0) and IBM Plex (OFL-1.1) are
    vendored under `console/vendor/` and served from `/ui/vendor/` (only the
    files present at start): no CDN, so air-gapped installs and the `'self'`
    CSP hold; the CSP gains `font-src 'self'`. `scripts/vendor_carbon.py`
    refreshes them; `vendor/README.md` records versions and sources.
  - A test follows every module the page loads to each chunk it imports, so a
    missing vendored file fails the build, not the browser.

- **The product is named Truejoin.** The placeholder `trio` is gone
  everywhere it was a name. The renames:
  - the Python package: `import truejoin`;
  - the distribution: `truejoin`, was `trio-engine`;
  - the command: `truejoin serve`, `truejoin catalog …`;
  - the Helm chart: `helm/truejoin`;
  - the image: `truejoin:<tag>`; the smoke compose reads `TRUEJOIN_IMAGE`.
  Smaller ones:
  - API keys handed out from now on start with `truejoin_`. Keys already
    issued keep working: only their digest is stored, and the prefix is
    never checked.
  - The masking function is `truejoin_pseudonym`.
  - The console's browser storage keys start with `truejoin-`, so a browser
    signs in once more.
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
  `POST /catalog/analyze` (and `--tables` on `truejoin catalog analyze`) used to
  write a catalog of just those tables, dropping the rest with their reviewed
  descriptions and terms; the others now stay as they were, unless they have
  left the database, and keep their review: only the re-analysed tables go
  back to pending.
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
  rules on. The same holds for `truejoin catalog enrich-openapi` and the pipeline.
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

- **One process.** `truejoin serve` runs one ASGI app: the REST gateway as before,
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
- **One CLI.** `truejoin serve [--transport http|stdio]`, `truejoin catalog ...`
  (Warp's commands, plus `approve`), `truejoin mcp {convert,info,formats}`
  (Stargate's) and `truejoin config {validate,show}` (secrets masked) replace
  `truejoin-warp`, `truejoin-catalog`, `truejoin-olap-rest`, `truejoin-olap-mcp` and
  `truejoin-mcpgen`. The image runs `truejoin serve`; compose has one `api` service.
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
- The gateway's raw `/query/execute` leaves one audit event per request, as
  the analytical reads do: the caller, the tables the SQL names, the status
  (a refusal or a failure too) and the row count; never the SQL or its
  parameters. A write the whitelist lets through is recorded as one
  (`create`, `update`, `delete`), judged by the statement's first word.
- `truejoin serve --transport stdio` acts as `mcp.stdio_api_key_name` with that
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
  `JOIN ... USING (col)` and `NATURAL JOIN` are judged too, as the column
  pairs they compare (`orders JOIN customers USING (id)` is `wrong_column`),
  but never rewritten: as `ON` they would change what `SELECT *` returns, so
  `repair` refuses a wrong one. One after a derived table, a CTE or a table
  whose columns are not known is not judged.
  A materialized view's SQL is checked once, when it is created: `advise`
  creates it as written and `create_view` returns the verdicts, `repair`
  keeps the repaired SQL, which every refresh then runs, and refuses a view
  on a join it can neither confirm nor fix.
  A key the database declares on several columns (a composite primary key
  and the foreign key that references it, reported one column at a time) is
  judged as one: a join on some of its columns is `incomplete_key`, and
  `repair` adds the others to that join's `ON` (a join written in `WHERE` or
  with `USING` is flagged, not completed). `query_metrics` still does not
  join along such a key: the parent counts as reached two ways and is left
  out, as before.
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
  one audit event. Transforms work over the join too (see below).
- **Many-to-many through a link table.** A table that holds nothing but keys
  to two others (and maybe an id of its own), as `enrollments` links
  `students` and `courses`, is a way between them: students per course,
  balance per department (courses' parent), or students in one department,
  asked from either end. The far side is read as distinct (row, group)
  pairs, so a row counts once per group it falls in, and once in all when
  that side only carries a condition: a student in two courses of one
  department counts once for it, and a link row loaded twice changes
  nothing. One link per question; a table reached through two links, or a
  table with anything besides keys (a grade), is not crossed. Terms and
  `describe_term` offer the tables reached this way.
- **Business terms** (`olap.terms`, on by default). Catalog analysis writes
  one term per table: what the table is called, the words for each column and
  for the values it stores. The model can attach words only to names that
  exist. A term is read by nothing until a reviewer certifies it:
  `truejoin catalog review` shows each table's term and approving the table
  certifies it; `truejoin catalog approve` and the HTTP review, which show no
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
- **`check_question`: how much of a question the vocabulary covers.** Before
  anything runs, each word of a question asked in words is shown with what
  it was recognised as (a table, a column, a stored value, an aggregation, a
  grain, a breakdown) or as nothing, with `covered` (`all`, `part`, `none`),
  the words not recognised, and the reading `answer_question` would take
  when the question named anything at all. Fillers, inflections and
  apostrophe suffixes are not counted as unknown. Over the bench's 48
  questions, the 39 it called covered were answered right 34 times by the
  `terms` arm and the 9 partly covered once: the unknown words are what the
  vocabulary lacked (ranking, "no address", a currency), which the planner
  now reads (below). On 16 questions written afterwards, 9 of the 10 it
  calls covered are answered right and 3 of the 6 partly covered. Values are matched
  as `answer_question` matches them, never a masked column's. The 18th tool,
  offered with the term tools.
- **The planner reads more of a question.** What `check_question` found it
  missing on the bench: "kırılımında" as a breakdown and two breakdowns at
  once ("segment ve kanal", in the order named); "rank" as a ranking, read
  in rank order; "e-postası olmayan", "no email" as a condition that the
  column is empty; a short code stored in capitals (TRY, USD) when the
  question writes it so, never "try"; the noun after "distinct" as what is
  counted; "en çok", "most", and "the N <things>" as one row per thing, by
  its key. On the 48 bench questions these were tuned on, the `terms` arm
  goes from 35 to 48 and `lexicon` from 23 to 31, none lost; on 16 questions
  written afterwards and never tuned on, from 4 to 12. Lower-case short
  values ("web") are still not matched.
- **A held-out question set for the bench.** `bench/questions/banka-heldout.json`
  holds 16 questions written after the planner was tuned and never used to
  tune it, with gold SQL for Oracle, SQL Server and DuckDB.
  `python -m bench.heldout` runs the arms that need no model on both sets in
  seconds: `terms` 48/48 tuned against 12/16 held out, `lexicon` 31 against 9.
  A test keeps a floor under the held-out numbers; the bench README says why a
  held-out question fixed on purpose moves to the tuned set.
- **A question the vocabulary reads none of can go to a model, as SQL.**
  `olap.unread_questions: sql` (the default; `refuse` turns it off) hands such a
  question to the configured model (`settings.llm`, `non_reasoning` when
  set) with the schema the caller may see: tables, columns and keys, the
  stored values not masked for them, the glossary and the notes. Its SQL
  runs as `query_data` does, through the guardrails and the caller's row
  rules and masks, and the answer carries it with `read_by: "model"` and the
  unread words. Stored values go to a cloud model only with
  `analysis.share_samples_with_cloud_llm`, as for catalog analysis; the
  question itself always goes. Measured on `banka-unreadable` with
  `qwen2.5-coder:7b`: 11 of 16, as the bench's hand-written schema arm, with
  different misses; the tuned and held-out sets never reach it. The console
  says so above such an answer and shows the model's SQL open, and offers no
  follow-up on it (there is no reading to change). Tried live against the
  GB10: a tenant's count came back as its own two customers, and the
  national id it asked for came back masked. This is the first step towards
  an in-product text-to-SQL agent, not the agent.
- **A question read in part says which words it was answered without.**
  `answer_question` answers a question it reads in part, leaving out the
  words it has nothing for, and used to say nothing about them: "kargo ile
  ödenen sipariş sayısı" counted every paid order without a word about
  "kargo". The answer now lists them as `unread` (the tool description tells
  a calling model to check the reading against them), and the console shows
  them above the result, with the *Means* field for a key that may edit
  catalogs.
- **The bench's model arms, re-run after today's planner changes.** At
  `acfbb5a`, with the same `qwen2.5-coder:7b` on the GB10, every arm gave
  the same verdict on every question of the tuned, held-out and unreadable
  sets as before the refusal and the value-word fix; only latencies moved.
  The reports in `bench/results-7b/` are this run's.
- **A refused question shows its unread words in the console.** In the
  Query tab's Question mode, a question that was not answered is checked
  with `check_question` and the words the vocabulary did not read are
  listed under the error. For a key that may edit catalogs, each comes with
  the Audit tab's *Means* field (glossary, a term's name or a column's
  word); adding one asks the question again. The loop from a refusal to an
  answer is one screen.
- **A value's own words are kept when they share its stem.** The catalog
  gives `standart` the word "standard", and `check_question` read it as that
  value, but `answer_question` never applied it: cleaning the vocabulary
  dropped any word starting with the name's stem, which is right for a
  column (matched by stem) and wrong for a value (matched as a whole word).
  "Total amount of standard customers" now filters on `standart`. Bench:
  follow-ups 15 → 16, the rest unchanged.
- **A question the vocabulary reads none of is refused, not guessed.**
  `answer_question` answered such a question with whatever table scored
  highest (a count of branches, as often as not): a confident wrong answer.
  When `check_question` would say `none`, it now refuses with the words it
  did not read and the ways forward (`search_terms`, `query_metrics`,
  `query_data`, a steward adding the words). Nothing runs. A follow-up is
  not refused this way, since it reads from the request it follows. No
  question in the tuned, held-out or follow-up bench sets reads as `none`,
  so their numbers are unchanged.
- **A bench set of questions the vocabulary cannot read.**
  `banka-unreadable` (16, `python -m bench.unreadable`, needs a model): on
  each, `check_question` says `none` and `answer_question` answered a guess,
  right twice (now refused, 0). The model writing SQL from the annotated
  schema gets 11, and `terms+model-on-none` (the model only where nothing
  was read) gets the same 11 there while staying `terms` on the other sets:
  72 of 80 over all three against 61.
- **The literal check runs only when an answer matched nothing.**
  `answer_question` used to look every condition's value up in the data
  before answering (a read, with its audit event, per condition), in case
  the catalog spells it differently from the data ("Paid" against "paid").
  It now answers first and looks only when that found no rows, or only zeros
  and nulls: a value the data holds costs nothing extra, and one it does not
  is repaired as before, with the answer run once more.
- **Follow-up questions.** `answer_question` takes `previous`, the `request`
  an earlier answer returned, and reads the new question as that request
  changed only where the words say: "peki şube bazında?" keeps the measure
  and the conditions and replaces the breakdown, "ve aylık" adds one, "iptal
  olanlar" replaces the condition on the same column, "ya ortalama?" averages
  the measure there was, "en yüksek 3" orders by it and keeps three. The
  table stays the earlier one. Stateless and without a model: the caller
  (a model, MCP, REST, the console's new *Follow up on the last answer* box)
  carries the conversation, and the caller's row rules and masks apply as to
  any question. The release smoke asks one, as a tenant, over MCP.
  Measured on a new bench set, `banka-followup` (16 follow-ups, Turkish and
  English, `python -m bench.followup`, no model): 15 right at first; the
  miss, English "standard" for the stored `standart`, is fixed (see above).
- **The bench's model arms, re-run.** `bench.run_duckdb` takes a question
  set (`bench/questions/banka-heldout.json` for the held-out one). With
  `qwen2.5-coder:7b` on the GB10, the arms that call the model alone are
  unchanged; `terms` answers 48/48 and 13/16 held out, and
  `catalog+join-path`, the same proposal corrected by the model, 43 and 11:
  the model now breaks more right readings than it mends. The bench README
  says what that means for the correction step; nothing in the product
  changed for it, and `olap.terms` stays on (the gate still passes). A new
  arm, `terms+gaps`, asks the model only when `check_question` finds a word
  unread: 48/48 (it never calls the model there) and 12/16 held out, still
  one behind `terms`, because an unread word does not mean a wrong reading.
- **Vocabulary gaps from real questions.** With `audit.question_text` on, the
  usage summary (`GET /audit/usage`, the console's Audit tab) reads every kept
  question as `check_question` would and lists the words the certified
  vocabulary does not know: distinct questions, calls, calls not answered and
  an example, unanswered first. A question answered with a word skipped counts
  as answered, so this is also where a quiet misreading shows. Read as the
  person asking for the summary, so a masked value is a gap to them; nothing
  new is written to the trail. A key that may edit catalogs writes a gap out
  there and then: the table's *Means* field adds it to an approved catalog's
  glossary (`PATCH /catalog/{db}/guidance` with `add_to_glossary`, merged on
  the server so a concurrent edit is not lost), and the list is counted again.
  Or, picked in the same field, the word itself goes to a certified term: as
  a name for it, or as a word for one of its columns
  (`PATCH /catalog/{db}/terms/{term}` with `add_synonyms` or `add_to_columns`,
  merged the same way).
- **Transforms over a join.** `rank`, `cumsum`, `lag`, `change` and the rest
  now work when a dimension or a filter is a column of another table
  ("rank the segments by number of transactions", across a link table too);
  they were refused. The joined, grouped SELECT becomes the `base` the windows
  are layered on, exactly as on one table, and still runs through the query
  pipeline, so the caller's row rules reach every table before anything is
  ranked. On the bench, `terms` answers 13 of the 16 held-out questions. That
  one is not an unseen gain: the gap was chosen to fix knowing a held-out
  question hit it, so the honest held-out number is 12 of the other 15.
- **The proof gate.** A statement repaired by swapping one literal runs only
  when it is proven to be the original with that literal changed and nothing
  else (`prove_substitution`). `answer_question` uses it when the vocabulary
  spells a value differently from the data.
- **Models by job.** Catalog analysis and term extraction use
  `llm.reasoning`; translation uses `llm.non_reasoning` when that slot names a
  model of its own.
- The certified terms are published in `x-llm-context`, per table.

- **Measures suggested from the query history** (`truejoin catalog suggest`). The
  most-run statements (`pg_stat_statements`, `sys.dm_exec_query_stats`,
  `V$SQL`, MySQL's and MariaDB's `performance_schema` digests, with MySQL 8's
  sample statement where it has one) and the view definitions of the schema
  are mined for the expressions they repeat: what sits inside an aggregate,
  arithmetic a statement selects, and each WHERE condition as a 1/0 flag. An
  expression is kept only when it is a scalar over one table of the catalog,
  with no parameter in it. A condition or arithmetic on a key or a date is
  dropped; what a view defines ranks first, the rest by how often it ran. The
  model names the candidates and may drop some, and never touches an
  expression. After NVIDIA Auto Ontology's history mining; reading views is
  new here.
- **A suggestion is a draft.** It is stored in the term of its table,
  uncertified, and is no measure until `truejoin catalog certify -e <name>`,
  which leaves the rest of the review as it is. A history that cannot be read
  is reported with the grant it needs, and the views are still used. See
  `docs/docs/query-history.md`.
- **Keys handed out from the console.** With `auth.key_store` naming a
  file, a caller with the new `admin` permission hands out and revokes API
  keys at run time (the console's Keys tab; `GET`/`POST /keys`,
  `DELETE /keys/{id}`), without editing the configuration or restarting. The
  secret is shown once and kept as a SHA-256 digest in a file written
  atomically, mode 0600, every change made under a lock on the file beside
  it so two workers cannot undo each other's. A key's permissions, tenant, roles, row rules and
  optional expiry are fixed when it is made; it is revoked, never edited, and
  a revoked key is refused at once in every process serving the file. Nobody
  gives a permission they do not hold, a new key carries exactly its
  grantor's roles (a role can lift or replace masks), and a caller held to
  row rules gives nothing. Names are never reused. Both changes are
  `access_change` audit events, by name only.
- **Saved analyses.** A catalog keeps examples: a question and the SQL that
  answers it here, saved through `POST /catalog/{db}/examples` or the
  console's Ontology overview (with *Run it* first), checked against the
  engine's guardrails and this catalog's tables, and certified like a
  measure (`PATCH .../examples/{name}`). A new tool, `find_examples`, offers
  the certified ones to a model before it writes SQL, matched by word stem
  after the glossary. A caller with row rules or masks is never shown a
  draft nor one with a value in it (a quoted or dollar-quoted string, or a
  number other than a row count or a GROUP BY/ORDER BY position), by the
  tool or by the catalog's ontology and export routes. They travel with
  export and import (uncertified on arrival) and are the Ossie model's
  sample questions.
- **Usage.** Every analytical tool call leaves a `tool_call` event in the
  audit trail: who, which tool, the outcome (answered, empty, refused,
  failed, error), the time taken, the rows, and how the request was read by
  name only (table, measures, breakdowns, the columns of its conditions, the
  abbreviations written out); never a value or the SQL. The words of a
  question only with `audit.question_text: true`. `GET /audit` filters by
  `tool` and `outcome`, and `GET /audit/usage` (optionally `?days=`) counts
  every call in the trail: outcomes, the most asked-for tools, tables,
  measures, breakdowns, filtered columns and abbreviations, and the newest
  100 calls that were not answered. The console's Audit tab shows it as the
  *Usage summary*. A search that finds nothing (`search_terms`,
  `find_examples`) counts as empty, and a limit that is not a number as a
  failed call rather than an internal error.
- **Sign-in with an identity provider (OIDC).** `auth.oidc` makes truejoin take
  bearer tokens from Keycloak, Entra ID, Okta and the like, beside the API
  keys: the signature against the provider's published keys (asymmetric
  algorithms only), issuer, audience and expiry are checked, and the caller's
  name, tenant and roles come from the claims, permissions from
  `role_permissions`, row rules from `row_filters` with `${tenant}` and
  `${username}` (`row_filter_exempt_roles` skips them). It covers the table
  routes, `/olap`, MCP and the console (a pasted token). MCP clients find the
  provider through `/.well-known/oauth-protected-resource` and the 401's
  `WWW-Authenticate` (RFC 9728). Keys are cached and fetched again when the
  provider rotates them. Production refuses an `http://` issuer. With
  `console_client_id` the console has *Sign in*: Authorization Code with
  PKCE, the code exchanged by truejoin (`POST /ui/sign-in/token`) so the page's
  CSP stays same-origin, and the token checked before the page gets it. When
  the provider also gives a refresh token, the console renews its access
  token a minute before it expires (`POST /ui/sign-in/refresh`, checked the
  same way, keeping a rotated refresh token), so a session lasts as long as
  the provider's. See `docs/docs/deployment.md`.
- **Tags across a catalog.** `GET /catalog/{db}/tags` lists every tag and
  what carries it; `POST /catalog/{db}/tags/{tag}` puts one on many tables
  and columns at once (or takes it off), `PATCH` renames it everywhere
  (merging into an existing one) and `DELETE` removes it. Each change is the
  edit a reviewer makes one by one, kept as an override and marking its table
  edited, so drafts only: a tag can make a column personal. The console
  lists them on the Ontology overview, and a search's tables and columns can
  be tagged in one go.
- **Owners.** `PATCH /catalog/{db}/owners` says who answers for a table or a
  term; it is not part of the review, so any status takes it. Shown on the
  review and the overview, editable there, and found by search.
- **Import, export in every language, and Apache Ossie.** `export?lang=all`
  writes the whole catalog, and `POST /catalog/{db}/import` reads it back
  (JSON or YAML, `replace=true` over an existing one) as a draft with every
  table pending and nothing certified: what another install approved is
  reviewed again here. `export?format=ossie` writes an Apache Ossie semantic
  model (spec 0.2.0.dev0, checked against its JSON Schema): datasets, typed
  fields with their terms' words, certified measures as metrics, declared
  and verified relationships, and the notes and glossary as AI context; a
  draft exports its structure only. The console's catalog header exports,
  and *Import a catalog* sits beside *Analyze a database*.
- **Re-analysis keeps the glossary, the notes and the owners.** A new
  analysis wrote a new catalog and lost them.
- **Query results: CSV, a chart, and recent queries.** The console's Query
  tab downloads the rows shown as CSV (UTF-8 with a byte-order mark, so a
  spreadsheet reads Turkish letters). A result with a label and a number, up
  to 60 labels, can be shown as bars, or as lines when the labels are dates.
  A second label column splits it into series (amount per month, one line
  per city; bars grouped per label), and several measures of one size (within
  ten times of each other) can be drawn together; never two scales on one
  axis. Up to six series in a validated categorical palette, light and dark,
  with a legend (and names at the line ends for up to four); past six, the
  largest are drawn and the rest left to the table. The last 30 queries run
  are listed and put back in the editor on a click; they are kept in this
  browser's storage only.
- **Search the catalogs.** `GET /catalog/search?q=` finds tables, columns,
  business terms, measures and abbreviations in every catalog the key may
  read, in any status: every word of the query in a name, a synonym, a tag
  or a description, in any of the catalog's languages and on the folded form
  ("musteri" finds "Müşteri"). Names rank first. Stored values are not
  searched. The console's Catalog tab has the search box; a result opens the
  table it lives on.
- **Abbreviations and notes for the model.** A catalog carries a glossary
  (`SP = sipariş`) and notes in a reviewer's words ("amounts are in TRY"),
  set in the console's Ontology overview or with `PATCH /catalog/{db}/guidance`
  (audited by field, never the words). `answer_question` and `search_terms`
  write the abbreviations out before matching, whole words and the longest
  first, and the answer lists the ones used; `list_sources` gives each
  source's notes, and the OpenAPI `x-llm-context` carries both. Like terms,
  only an approved catalog's are used, at once.
- **A measure can be written by hand.** On a term, in the console (*Add a
  measure*, with *Check*) or through `POST /catalog/{db}/terms/{term}/measures`
  (`.../measures/check` stores nothing). The expression is what goes inside
  the aggregate, in DuckDB SQL over one table the term covers; it is held to
  the rules a mined one follows, then summed over an empty table of its
  columns in an in-memory DuckDB, so a type the engine refuses is caught with
  the engine's reason. It is stored as a draft and certified like a
  suggestion. `DELETE .../measures/{name}` removes a draft or a measure.
  Adding and removing are audited by name, never by their SQL.

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
- **A Helm chart** (`helm/truejoin`): one replica replaced on change, config as a
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
  access by rewriting, the certified gate). `python -m truejoin` works.
- CI runs the smoke, the chart check, a strict docs build and a blocking
  dependency audit; a nightly workflow runs all four database engines and the
  no-network build.
- **A console at `/ui`**: static pages with no build step and a strict CSP,
  over the routes any client uses. *Query* browses the sources and runs SQL or
  a question in words under the caller's row rules and masks; *Catalog* is the
  review a steward does with `truejoin catalog review`: edit descriptions and
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
  air-gapped under the same CSP. *Try it* on each operation sends it with the
  key entered above (asking first for anything but a GET) and shows the
  status and the response.
  The catalog opens on an *Ontology overview*: what the catalog records and
  who wrote it, a diagram of the tables and their relationships (verified,
  unchecked, contradicted, declared key) that can be panned, zoomed (the
  buttons or Ctrl+wheel) and rearranged by dragging a table; selecting one
  dims what it does not join to and shows its terms, measures and joins, each
  neighbour a click away, with *Review this table*. Below it, every entity
  with its keys, indexes, tags, DB comments and columns (null, default,
  semantic type, tags, personal, masked for this key), the glossary of terms
  with their words and values, and every measure. It reads the new `GET /catalog/{db}/ontology`,
  the whole catalog in any status, with stored values withheld as on the
  table detail.
  A key with `create` can *analyze a database* from the console (all tables
  or the ones named; the result is a draft). It runs as a background job, so
  a long analysis never meets a proxy's timeout and the page can be left and
  come back to it: `POST /catalog/analyze/jobs` starts one (202, one per
  database at a time, 409 for a second), `GET /catalog/analyze/jobs[/{id}]`
  reports it, and `POST /catalog/analyze` still answers in the request. Jobs
  live in the process that started them, so behind several API workers a
  poll can miss. A key with `update` can *suggest measures*, through the new
  `POST /catalog/{db}/suggest` (what `truejoin catalog suggest` does, audited the
  same way). `GET /catalog` now lists the `connected` databases, and
  `/ui/access` says whether the key may `create`. *Re-open for review* turns
  an approved catalog back into a draft to edit it, through the new
  `POST /catalog/{db}/reopen` (what `truejoin catalog review` asks first; every
  table back to pending, audited as `reopen`); what is live stays live until
  it is approved again.
  *Audit* shows the trail newest first (when, who with tenant and roles,
  what, where, status, rows, what was withheld, request id), filtered by
  event, action, key, database, table or restricted reads. It reads the new
  `GET /audit`, which needs the new `audit` permission (or `all`) and
  searches the newest 8 MB of `audit.file`.
- **An approved catalog goes live without a restart.** Its masking labels
  (on the table routes and the analytical side alike), verified relationships
  and certified terms take effect as soon as it is approved through the API
  or the console; a running server checks the catalog index every 30 seconds
  for changes made with `truejoin catalog` or by another worker, and rebuilds the
  enriched OpenAPI spec when one changed. A draft never replaces what is
  live: re-opening or re-analysing a catalog keeps its last approved masks
  until it is approved again. A masked caller is now refused on a source whose
  masking policy has no labels yet, as it was on a source with no policy.
  `truejoin serve --transport stdio` follows the catalogs the same way.
- **Catalog changes are audited.** Every edit, approval, certification,
  analysis and deletion through the catalog routes leaves one `catalog_change`
  event naming what changed (fields, columns, terms, measures), never the
  text a reviewer wrote or an expression's SQL. `truejoin catalog` (analyze,
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

- **A question whose naming words the reading left out was answered
  anyway.** "takip listesinden olan ve daha önce kredi kullanmış müşterilerin
  hareket listesi" came back as `count 500`: "listesinden" matched the
  `listed_on` column, which put the reading on the watchlist, and the reading
  then counted its rows and used nothing of the question. A reading that uses
  no word naming a table, column or value is now refused like a question with
  no known word (the console shows the word proposals), or handed to the model
  with `olap.unread_questions: sql`; `check_question` says so too.
- `truejoin config validate` now fails, saying why, when `settings_store.path`
  or `database_store.path` is set and the key in its `key_env`
  (`TRUEJOIN_SECRET_KEY`) is missing or not a Fernet key: the server would
  refuse to start, and the check said the file was valid.
- `settings` and `auth` are reserved database names beside `catalog`,
  `audit`, `keys` and `databases`: a database by either name would have been
  shadowed by the server's own routes (`/settings`, `/auth/users`).
- **A question whose words were all "read" could still be answered as another
  question.** "çok sipariş veren 5 müşterinin kategori başına harcamalarını
  listele" came back as the number of categories, with one word reported
  unread: the rest were known to the vocabulary, and nothing checked that the
  reading used them. Recognising a word is not reading it:
  - *Refused.* When a word names a table the reading's own table does not
    reference (customers counted, with a condition on their orders),
    `answer_question` refuses, saying which table it would have read from
    and what that leaves out; with `olap.unread_questions: sql` the question
    goes to the model instead, as one with no known word does. The console
    says so in its own words and offers no word to add: the vocabulary is
    not what is missing.
  - *Said.* An answer lists as `unused` the words that were recognised and
    that the request makes no use of: a table within reach, a breakdown or a
    grain nothing took, and any number that is not the row limit ("2024",
    "son 3 ay": a number was never reported before). `check_question` gives
    `unused` and `apart` before anything is asked.
  - *A column's words are its own table's.* A word added to `users.id` was a
    word for every table's `id`, in every database, so it was "read" wherever
    the question went.
  - On the bench the tuned 48 are unchanged. Held out, `terms` answers 12,
    not 13: `h05-en` (unique customers who used the mobile channel) had been
    read as a count of every customer and marked right because all five used
    it; it is refused now, as `h05-tr`, a silent wrong answer before, is. The
    floor in `bench/tests/test_heldout.py` is 12; the tables in
    `bench/README.md` are the earlier run and have not been regenerated.
- **The console's rough edges.** A chart printed the word "null" under its
  title; a count axis had ticks at 0.5; the Keys view, with no key store,
  squeezed into the 280px side column; a phone-width page left half a screen
  empty under the table list; and the SQL placeholder named `db.users`, a
  table no database has. Buttons, tabs and links now answer a hover, a focused
  field shows it, a placeholder no longer reads like a value, result tables
  have a frame and a shaded header, the open item in a side list is marked,
  and an empty review pane says so in a box.
- **`SECURITY.md` describes this project.** It still pointed reports at the
  Warp repository's advisories, called the release 0.9.x, named
  `requirements*.lock` files that do not exist and `warp.*` module paths, and
  linked a README section and `docs/adr/` that are elsewhere. It now says 1.0.0
  is unreleased, installs from `uv.lock`, takes reports by email until the
  repository is public, and lists the controls from the analytics side too:
  the SQL guardrails, row rules and masks on analytical SQL, refusing questions
  it cannot read, join checks and the `audit.question_text` switch.
- **`audit.log_filter_columns: false` is honoured.** It was read by nothing,
  so the names of filtered columns were written anyway; the audit sink now
  drops them from every event. `analysis.excluded_schemas`, which did
  nothing either (tables are read from the one configured schema), is gone;
  a config that sets it still loads.
- **The console's sign-in calls cannot starve token checks.** The sign-in,
  code exchange and refresh routes need no key and wait on the identity
  provider; they shared the thread pool that checks every bearer token. They
  run on four threads of their own now, and past sixteen in flight the next
  is a 503 at once. Reading the audit trail runs off the event loop too.
- **A table named `audit`, `keys` or `catalog` no longer takes that route.**
  With one database its tables are also served at the bare API prefix, and
  those routes were added first: `GET /api/v1/audit` returned an `audit`
  table's rows to any reader, and the console's Keys tab would have inserted
  into a `keys` table. The truejoin routes come first now; such a table is still
  at `/api/v1/{db}/{table}`, with a warning at startup.
- **An identity provider that is down is not a bad token.** A 5xx from its
  token endpoint, or keys that cannot be fetched with none cached, answered
  401: an API client was told its token was invalid and the console signed
  its user out. It is a 503 (with `Retry-After`) for a bearer token and a
  502 for the console's sign-in and refresh, which the console retries.
- **A number named with no word for how is summed, not counted.**
  "şube bazında işlem tutarı" (no "toplam") was read as a count of
  transactions broken down by the amount; a question that names a numeric
  measure without an aggregation now takes the measure's own (a sum for an
  amount), unless it asks for a count. The bench's `lexicon` arm goes from 21
  to 23 of 48 and `terms` from 33 to 35, none lost. Found by the new usage
  summary.
- **`${tenant}` and `${username}` in a YAML row rule.** The config loader read
  them as environment variables and replaced them with nothing, so such a rule
  matched no row (closed, not open). They are left for the caller now.
- **A relationship labelled the wrong way round no longer joins the wrong
  way.** The direction of a catalog relationship came from its label alone;
  a model wrote "one-to-many" on the child, so transactions became the
  parent of customers and a count of customers per transaction status
  counted each customer once per transaction (4 for 3 customers). The one
  side now has to be its table's primary key: a label against the keys is
  turned round, and with no key at either end there is no edge. The join
  graph, the OLAP side and the Ossie export all take their edges from here.
- **A term synonym of several words needs all of them.** "Müşteri işlemleri"
  (customer transactions) counted as named when a question said only
  "müşteri", so "müşteri sayısı" counted transactions instead of customers.
  Matching now asks for every word of such a name. On the benchmark the
  `terms` arm (`answer_question`, no model) goes from 27 to 33 of 48, none
  lost.
- **A table's list and its single record write a row the same way.** A
  `numeric` came back as `"100.00"` in the list and `100.0` from
  `GET /{table}/{id}`, and MySQL's `TINYINT(1)` as `1` and `true`; both now
  follow the documented column type. `GET /{table}/{id}?fields=...` answered
  500, the response model wanting every column; it now returns the fields
  asked for.
- **A table PostgreSQL never analysed has no row count**, rather than 0: the
  planner's `-1` (unknown) was clamped to 0 and shown as "0 rows" in the
  catalog, the console and the enriched OpenAPI spec. The console marks the
  count it shows as the estimate it is.
- **A mask that does not fit its column no longer fails the read.** A text
  mask on a number, or `null` on a `NOT NULL` column, made
  `GET /{table}/{id}` answer 500 (the list route already served it); the
  masked record is now served, and startup still warns that the masked value
  no longer matches the column's documented type.
- Two requests that both needed a table not loaded yet streamed into the same
  staging table and published over each other; one failed and could leave the
  table dropped. Loads are now serialised per source table.
- The default config path pointed inside the package after the move
  (`truejoin/config/database.yaml`); it is the repository's `config/database.yaml`.
- Six ways a restricted caller could read past its row rules or masks, found
  by review: a table sharing its alias or bare name with a subquery, VALUES,
  UNNEST or CTE was left unwrapped; a recursive CTE named like an `mv_*` view
  read the stored view (recursive CTEs are now refused); the `truejoin_pseudonym`
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
  image's `truejoin` launcher re-ran the server in every reload or worker process.
- Six more that hurt on default settings. A semi-join whose small side
  matched nothing sent `IN ()` to the database, which no engine parses; an
  empty list is now `1 = 0` (and an empty `in` row rule reads no row). A
  rate limit slowapi cannot read (`100/min`) turned throttling off; it is now
  refused when the config loads. A caller refused at `/olap` or `/mcp`
  (401/403) left no trace with the access log off; it is logged. `truejoin serve`
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
  rules (such keys are refused there) and no masks (such callers are
  refused); it is audited per request.
- Grounding judges joins written as `ON` or `WHERE` equalities between two
  columns, `USING` and `NATURAL JOIN` (judged, not repaired), and a
  materialized view's SQL when it is created. A composite key is known only
  when it is the referenced table's primary key; one on a unique constraint
  is still judged one column at a time.
- `olap.terms` is on by default because its benchmark gate passed
  (`bench/README.md`: `catalog+join-path` 43 of 48 against the catalog arm's
  34, and 11 of 16 against 10 on the held-out set, nothing invented), not
  because the model's correction was shown to help: since the planner was
  taught from its misses, `answer_question` alone answers more (48 and 13),
  and the correction breaks right readings. On, catalog analysis makes one
  more model call per table; `terms: off` restores the old cost and hides
  the four tools. A term knows the stored values the catalog kept samples
  of, a handful per column. `answer_question` reads one measure, up to two
  breakdowns, the conditions it has words for, rankings and "the top N",
  and follow-ups; a question it reads none of is refused, and one it reads
  in part is answered with the words it skipped left out.
- Not in 1.0.0: an in-product text-to-SQL agent (a model writes SQL only as
  a caller of the tools, or, with `olap.unread_questions: sql`, once for a
  question the vocabulary reads none of; no plan, no retry, no
  conversation), more engines (Snowflake, Databricks, ClickHouse,
  Trino), and group administration (groups and their roles come from the
  directory or the identity provider; local users carry permissions, not
  groups). Databases can be added from the console (`database_store`) and
  users made there (`auth.users`).
- History mining offers measures only: a selected `CASE` (a derived dimension)
  and a join condition written in a query are not suggested. PostgreSQL's and
  MariaDB's history has its constants replaced, so conditions come from views
  there; SQL Server's, Oracle's and MySQL 8's keep them (MySQL's sample is cut
  at 1024 characters by default, and a cut statement is skipped). Oracle's AWR
  is not read (it needs the Diagnostics Pack).
- The `embed` and `rerank` slots are read and validated but nothing uses
  them.
- The integration suite passes on all four engines: PostgreSQL, MySQL and
  MariaDB started by the tests themselves, SQL Server and Oracle through
  `make it-up` and `make it` (`docker/it`: a runner image with both ODBC
  drivers).
  That covers the adapters, the in-process OLAP path, declared-key joins and
  the history readers. SQL Server ran under amd64 emulation on Apple Silicon,
  not on x86-64 hardware; the nightly workflow that would do that has never
  run.
- The Helm chart has been installed once, on a local kind cluster
  (2026-11-09, arm64): ready under the hardened settings, the network policy
  enforced both ways (an unlabelled client and the Kubernetes API timed out,
  PostgreSQL was reached). Not on a real cluster, with an ingress, Vault or
  x86-64 nodes; `RELEASING.md` has the details.
- The CI workflows have never run on a CI service: the repository has no
  remote. Each step of `ci.yml` was run by hand, on 2026-11-08 and again on
  2026-11-09 at `50b0d18` (macOS, Apple Silicon), and passed: the tests on
  Python 3.12 and 3.13 with the coverage gate (84.76%), the benchmark
  harness's tests, the integration suite (PostgreSQL, MySQL, MariaDB; SQL
  Server and Oracle skip there, as `ci.yml` expects), lint, format, mypy,
  the lock check, the chart snapshots, the strict docs build, `make smoke`
  (24 checks, a follow-up question among them) and `pip-audit` (nothing
  known). `nightly.yml` and `docs.yml` were not. The product image's vendored ODBC build paths
  (`WITH_MSSQL_ODBC=vendor`, `WITH_ORACLE_ODBC=1`) are written and unbuilt;
  the test runner installs both drivers the online way.
- The single-app test suite runs against a fake gateway; the same path over
  real PostgreSQL and SQL Server is in `tests/warp/integration` (needs Docker).
