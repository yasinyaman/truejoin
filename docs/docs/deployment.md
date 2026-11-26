---
description: "Deploying Truejoin: production refusals, sizing and limits, signing in with API keys, OIDC, LDAP or local users, databases and settings from the console, auditing and a production checklist."
---

# Deployment

truejoin is one image, one process and one port. It reads one YAML file and the
environment, and it **refuses to start in production with an unsafe
configuration**.

## What production refuses

With `APP_ENV=production` (the image's default) startup fails, saying why,
when any of these hold:

- `settings.auth.enabled` is false;
- `settings.enable_raw_query` is true;
- `CORS_ORIGINS` contains `*`;
- an API key is a placeholder;
- `/openapi.json` is in `auth.public_paths`, unless
  `auth.allow_public_openapi: true` accepts that the enriched spec is public.
  The default `public_paths` includes it, so a production config sets
  `public_paths: ["/health"]`;
- an OIDC `issuer` or `jwks_url` is not `https://`;
- `auth.oidc`, `auth.ldap` or `auth.users` is set while `auth.enabled` is false;
- `auth.ldap.url` is `ldap://` without `start_tls: true`;
- `auth.ldap` or `auth.users` is set and `TRUEJOIN_SECRET_KEY` is not;
- API keys carry `row_filters` while `auth.enabled` is false;
- a `hash` mask has a secret shorter than 32 characters (a missing one is
  refused in every environment).

Always on, in every environment: parameterised queries, mass-assignment
protection, timing-safe key checks, masking of personal samples before any
model call, and DuckDB's external access latch.

## The image

```bash
make image                     # docker build -t truejoin:dev .
```

Multi-stage, base images pinned by digest, dependencies exactly as `uv.lock`
names them. The runtime stage has no compiler, no package manager cache and no
curl; it runs as uid 1000 and writes only to `/app/catalogs`, `/app/data`,
`/app/logs` and `/tmp`, so it runs with a read-only root filesystem.

| Build argument | Values | |
|---|---|---|
| `DEPS` | `online` (default), `wheelhouse` | Where the Python dependencies come from |
| `WITH_MSSQL_ODBC` | `1` (default), `vendor`, `0` | Microsoft ODBC Driver 18: from Microsoft, from `docker/vendor/mssql`, or none |
| `WITH_ORACLE_ODBC` | `0` (default), `1` | Oracle Instant Client from `docker/vendor/oracle`, for Oracle through ODBC only; never downloaded |

PostgreSQL, MySQL and Oracle need no driver: Oracle runs on python-oracledb in
thin mode, which speaks Oracle Net itself. SQL Server needs Microsoft's ODBC
driver, and Oracle needs Instant Client only for a database that names an ODBC
driver. `docker/vendor/README.md` says what to put where for those.

An Oracle database:

```yaml
databases:
  - name: erp
    type: oracle
    host: oracle.internal
    port: 1521
    database: ERPPDB            # the service name
    username: truejoin_ro
    password: ${ERP_PASSWORD}
    options:
      # dsn: "tcps://oracle.internal:2484/ERPPDB"   # instead of host/port/database
      # protocol: tcps                              # TLS
      # wallet_location: /app/data/wallet
      # schema: ERP                                  # default: the user's own
```

### A build with no network

```bash
make wheelhouse                # on a connected machine: the locked wheels, ~100 MB
make image-offline             # docker build --network none --build-arg DEPS=wheelhouse
```

`make wheelhouse` downloads the wheels inside the builder image, so they match
the platform that will install them, and verifies each against the hash in the
lock. Carry `docker/wheelhouse/`, `docker/vendor/` and the two base images
(by the digests in the `Dockerfile`) across the gap; `make image-offline` then
fetches nothing. The offline install uses `--no-index --require-hashes`.

## Running it

### Compose

`docker-compose.yml` is the development stack (the app with reload, PostgreSQL
and MySQL, SQL Server and Oracle behind profiles). `docker/smoke/` is a
production-shaped one: the built image in production mode, read-only, without
capabilities, the database password as a mounted file.

```bash
make smoke
```

builds the image, starts that stack and runs `scripts/smoke.py` against it:
twenty-four checks that a tenant reads its own rows over REST, MCP and metrics
with the personal column masked and never another tenant's from the cache,
that joins follow the declared keys, that a wrong join is repaired, that a
question in words is answered, and that each read left one audit event with no
SQL in it. It is the release check; run it from a clean checkout.

For Claude Desktop or another stdio client:

```bash
docker run --rm -i -v "$PWD/config:/app/config:ro" -e APP_ENV=development \
  truejoin:dev truejoin serve --transport stdio
```

`mcp.stdio_api_key_name` says which key's row rules and masks that one caller has.

### Kubernetes

```bash
helm install truejoin helm/truejoin -f my-values.yaml
```

The chart runs **one replica, replaced on change**: the process holds what it
loaded and the catalog it started with, so two pods would answer from two
copies. `values.yaml` documents every setting; `helm/snapshots/` holds a
default and a hardened rendering, which `make helm-check` compares against.

- `config` is the YAML file, rendered into a ConfigMap. Keep secrets out of it.
- The pod is non-root, read-only, without capabilities or a service-account
  token; `/app/catalogs` and `/app/data` are volumes.
- `networkPolicy.enabled` closes everything except what `ingressFrom` and
  `egressTo` name (and DNS): the databases, and the model endpoint if one is
  used.
- An approved catalog goes live without a restart: at once for a change made
  through the API or the console, within 30 seconds for `truejoin catalog` on the
  command line. A draft leaves what is live alone.

The chart has been installed on a local kind cluster (the release notes in
`RELEASING.md` say what that covered), not yet on a production one.

## Sizing and limits

What decides the size of the machine is how much of the source tables
analytical queries pull into DuckDB, not how big the databases are. The REST
routes read the databases directly and hold nothing; the OLAP side (`/olap`,
the MCP tools, the console's Query screen) loads what a query needs into the
process's memory and answers from there.

**In production every analytical query runs in DuckDB.** A query is handed to
the source database whole only when raw SQL is enabled, and production refuses
to start with it enabled. So the numbers below are the whole story there.

### What a query loads

Each table an analytical query names gets one plan, in this order of
preference:

| Plan | When |
|---|---|
| Reuse | What is already held covers the query: the whole table, or a slice whose conditions and columns include the query's. |
| Whole table | The source estimates at most `olap.full_load_max_rows` rows (500,000), or cannot estimate. |
| Slice | The table is bigger, and the query has a condition on it: only the rows its `WHERE` keeps and the columns it reads, up to `olap.slice_max_rows` (500,000). |
| Fetch by key | The table is bigger, a slice would not do, and an inner join on an equality key ties it to a table that fits: it is fetched by that table's keys, at most `olap.semi_join_max_keys` (50,000), `olap.in_chunk_size` (1,000) per request. |
| Refused | None of these fits. The answer names the ways out: a condition, fewer columns, a join to a smaller table, `load_table` with `where`, or a higher limit. |

Only simple comparisons with a literal narrow a fetch (`=`, `!=`, `<`, `<=`,
`>`, `>=`, `LIKE`, `IN`, `IS NULL`, joined by `AND`). A query the planner
cannot read table by table (a CTE, a subquery, a set operation, a window
function) is planned as if it had no conditions: tables up to the whole-table
limit are loaded whole, bigger ones are refused.

Everything held, whole tables and slices together, counts against
`olap.slice_budget_rows` (2,000,000 rows). When a query would go over it, the
least recently used tables and slices are dropped first; if that is not
enough, the query is refused.

### A caller held to row rules or masks

Such a caller's query never reaches the source database as written. Their
row rules become conditions of the fetch, so a tenant's query on a big table
fetches that tenant's slice. Two things cost more than for an unrestricted
caller:

- A query that is not a flat `SELECT` loads whole tables, all tenants' rows,
  before the caller's view of them is applied. Above the whole-table limit it
  is refused.
- A condition or join key on a masked column does not narrow the fetch (the
  query compares the masked value), so such a query reads as if that
  condition were not there.

`search_data` on a table too big to load whole is refused for them.

### Memory

The row budget counts rows, not bytes, so the memory a row takes depends on
the table. One measurement (DuckDB 1.5.6, as locked; ten columns: three
integers, a decimal, a timestamp, a boolean and four short texts, one of 32
characters):

| Rows held | DuckDB memory |
|---|---|
| 500,000 | about 70 MiB |
| 2,000,000 | about 270 MiB |

A wide table or long text costs more; measure on your own data during a pilot.
Held data is rarely what fills memory: joins and aggregations over it are.
`olap.memory_limit` (4GB by default) caps DuckDB, and a query that needs more
spills to `olap.temp_directory`, up to `olap.max_temp_directory_size` when set.
Set the directory on a read-only root (the chart sets `/app/data/tmp`, on the
data volume).

Leave room above `olap.memory_limit` for the Python process. The chart's
default requests 2Gi and limits the pod to 6Gi, which leaves about 2GB above
DuckDB's default; raise both together.

### Connections and threads

- **Two pools per database.** The REST routes and the OLAP side keep a pool
  each, of up to `options.pool_size` connections (10 by default). Plan for
  twice that on the database's connection limit, per truejoin process.
- `olap.threads` (4) is how many threads DuckDB uses for one query. Queries
  run one at a time in DuckDB, behind one lock.
- `olap.rate_limit` (100 a minute) applies to `/olap` per API key, and per
  address for a caller who signs in another way.
- A tool returns at most 100 rows and says `truncated` when there were more.

### How fresh the data is

A table or slice, once loaded, answers later queries as it was when it was
loaded; results are also cached for `olap.cache_ttl` (300 seconds). The REST
routes always read the database as it is now.

`olap.refresh_interval` reloads what is held on a timer, in seconds:

```yaml
olap:
  refresh_interval: 900          # every 15 minutes; 0 (the default) = never
  refresh:                       # optional: top these up instead of fetching them whole
    db.transactions: {watermark_column: updated_at, key_columns: [id]}
```

Each refresh fetches every whole table again, or only the rows above its
watermark for a table `olap.refresh` names, and publishes it with a rename,
so a query sees the old rows or the new ones and never half of each. Slices
are dropped and fetched again by the next query that needs them, and the
result cache is cleared. A query that was about to read a dropped slice fails
once; asking again fetches it.

It is off by default because each refresh reads every loaded table from its
database again: with 500,000-row tables that is real load on a production
database. Set the interval to how stale an answer may be, not shorter.

Without it, or between refreshes, what is held is also dropped, and fetched
again on the next query that needs it, when:

- the process restarts (with the default in-memory `olap.database`);
- a setting is changed from the console, which rebuilds the stack;
- the database is detached and attached again from the console;
- the row budget drops it to make room.

### One process, so scale up

The chart runs one replica because a process answers from what it loaded:
two would hold two copies and could disagree. For more load, give the one
process more memory and threads, and keep the row limits in step with them.

## Secrets

Two forms, both resolved when the config is loaded, neither ever logged
(`truejoin config show` masks them):

- `${NAME}` or `${NAME:default}`: an environment variable;
- `${file:/path}`: the contents of a file, without the trailing newline. A
  missing file is an error, not an empty password.

`${tenant}` and `${username}` are not variables: in a row rule they are filled
in from each caller, so the loader leaves them as they are.

Prefer files. In Compose they are `secrets:`; in Kubernetes the chart mounts
the keys of `secrets.existingSecret` under `/run/secrets/truejoin`.

**Vault.** truejoin does not talk to Vault and has no SDK for it. Use the Agent
Injector: annotate the pod (`podAnnotations` in the chart) and read what the
agent writes with `${file:/vault/secrets/<name>}`.

```yaml
podAnnotations:
  vault.hashicorp.com/agent-inject: "true"
  vault.hashicorp.com/role: "truejoin"
  vault.hashicorp.com/agent-inject-secret-db-password: "secret/data/truejoin/db"
```

`masking.hash_secret` is a secret like any other: whoever holds it can
re-derive every hashed value.

## Handing out keys from the console

Configured keys change with the configuration file and a restart. With
`auth.key_store` naming a file, a caller with the `admin` permission can also
hand out and revoke keys at run time: the console (**Administration**, **Keys**), or `GET`,
`POST /keys` and `DELETE /keys/{id}` under the API prefix.

```yaml
settings:
  auth:
    enabled: true
    key_store: /app/data/keys.json   # on a volume, like the catalogs
    api_keys:
      - {key: "${file:/run/secrets/truejoin/admin-key}", name: admin, permissions: [read, admin]}
```

- A key's row rules are checked when it is made: a table or column that no
  served database (nor an approved catalog) has is refused, with the closest
  name, since a rule on a misspelt table would restrict nothing and a key
  cannot be edited. `POST /keys/preview` (and *Check the rules* in the
  console) counts, per table, the rows the rules would leave against the rows
  you see, before the key exists. It needs the analytics engine; each count is
  an analytical read audited under your name, plus one event naming the key
  previewed. Rules written in the configuration file (keys, `auth.oidc`,
  `auth.ldap`) are not checked this way.
- A new key's secret (`truejoin_...`) is in the answer that made it and nowhere
  else; the file keeps its SHA-256 digest, written atomically and readable by
  the server's user only. Its last four characters are kept as a hint. Each
  change is made under a lock on `<file>.lock` beside it, so workers sharing
  the file never write over each other (no lock on Windows).
- What it may do is fixed when it is made: permissions, tenant, roles, row
  rules (as in the config, `${tenant}` and `${username}` included; checked
  then), an optional expiry in days. It is never edited: revoke it and hand
  out another. A revoked key is refused from the next request on, in every
  process that serves the same file.
- Nobody gives more than they hold: no permission their own key lacks
  (`all` gives any permission), and a caller held to row rules hands out
  nothing. A new key carries exactly its grantor's roles, in order, `all` or
  not: a role can lift masks and row rules, and because a role's own masking
  rules replace the default ones, a key with a role left off could see more.
  To hand out keys of another role, give that role an admin key of its own.
- Names are never reused, a configured key's included, so the audit trail
  names one key. Handing out and revoking are `access_change` events
  (`create_key <name>`, `revoke_key <name>`) with who did it.
- The configured keys are listed beside them, by name and grant only.

## Row security in the database too

Truejoin applies its own row rules and masks. A database can have its own as
well, a SQL Server security policy or an Oracle VPD policy, written by a DBA who
wants them to hold whatever reads the tables. For those to apply to what
Truejoin reads, each statement's session has to say who it runs for.
`options.session_identity` asks for that, per database:

```yaml
databases:
  - name: crm                      # SQL Server
    type: mssql
    options:
      session_identity:
        session_context: {actor: truejoin_actor, tenant: truejoin_tenant, roles: truejoin_roles}
  - name: erp                      # Oracle
    type: oracle
    options:
      session_identity:
        client_identifier: actor            # or tenant
        context_procedure: app_ctx.set_caller   # optional: (actor, tenant, roles)
```

- **SQL Server**: `sp_set_session_context` sets each key on the session before
  the statement, so a predicate function reads `SESSION_CONTEXT(N'truejoin_tenant')`.
- **Oracle**: the client identifier is set
  (`SYS_CONTEXT('USERENV', 'CLIENT_IDENTIFIER')`), and the procedure, when named,
  is called with the caller's name, tenant and roles (comma-separated), so it
  can set an application context that only it may set (`CREATE CONTEXT ...
  USING`). With python-oracledb the identifier travels with the next round trip
  and costs no statement of its own.
- **Every session is told, every time.** Values are set when a pooled session
  is handed out, NULL when no caller is running, so a session never keeps the
  previous caller's identity. The values are bound, never written into SQL; the
  procedure's name is checked at load.

**What runs as the caller, and what as the service.** The REST routes (tables,
raw SQL, export) run as the signed-in caller. The OLAP side and MCP, catalog
analysis, history mining, discovery and refresh run as the service, with every
value NULL: what the OLAP side loads is served to every caller who asks the
same thing (Truejoin's own rules then restrict it per caller), and a catalog
describes the table, not one person's rows of it. So a policy must let the
service read when its context is NULL, or the OLAP side reads through
`readonly_username`, a user the policy exempts. For example, SQL Server:

```sql
CREATE FUNCTION sec.tenant_rows(@tenant nvarchar(64)) RETURNS TABLE WITH SCHEMABINDING AS
RETURN SELECT 1 AS ok
WHERE SESSION_CONTEXT(N'truejoin_tenant') IS NULL          -- the service
   OR @tenant = CAST(SESSION_CONTEXT(N'truejoin_tenant') AS nvarchar(64));
CREATE SECURITY POLICY sec.tenants ADD FILTER PREDICATE sec.tenant_rows(tenant) ON dbo.orders;
```

Oracle, with `client_identifier: tenant`:

```sql
CREATE OR REPLACE FUNCTION tenant_rows(schema_name VARCHAR2, table_name VARCHAR2)
RETURN VARCHAR2 AS
BEGIN
  IF SYS_CONTEXT('USERENV', 'CLIENT_IDENTIFIER') IS NULL THEN RETURN NULL; END IF;  -- the service
  RETURN 'tenant = SYS_CONTEXT(''USERENV'', ''CLIENT_IDENTIFIER'')';
END;
/
BEGIN DBMS_RLS.ADD_POLICY('ERP', 'ORDERS', 'tenants', 'ERP', 'TENANT_ROWS'); END;
```

A NULL context lets anything through that sets none, so the service user
should be one only Truejoin connects as. Only SQL Server and Oracle are told;
the option is refused for other engines.

## Adding databases from the console

Databases in `databases:` change with the configuration file and a restart.
With `database_store.path` naming a file, a caller with the `admin` permission
can also add, change and remove databases while the server runs: the
console (**Administration**, **Databases**), or `GET`, `POST /databases`, `POST /databases/test`,
`PUT /databases/{name}` and `DELETE /databases/{name}` under the API prefix.

```yaml
settings:
  database_store:
    path: /app/data/databases.json  # on a volume, like the catalogs
    key_env: TRUEJOIN_SECRET_KEY     # the default
```

```bash
# The encryption key, once; keep it with the other secrets.
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

- **Passwords.** They are encrypted in the file with the Fernet key in
  `TRUEJOIN_SECRET_KEY`. The server will not start with the store set and the
  key missing. The rest of the file is plain JSON, written atomically and
  readable by the server's user only. No answer, log line or audit event
  holds a password; a blank password on a change keeps the one kept. Losing
  or changing the key makes the stored passwords unreadable: enter them again.
- **At once.** A database is connected before it is kept: one that cannot
  connect is refused with the driver's message (password removed). Once
  added, its tables are served right away by the table routes
  (`{api_prefix}/{name}/...`), SQL (`/olap`), MCP and the catalog. A change
  tries the new settings first and, if they fail, the old ones keep serving.
  Removing takes its routes and its OLAP source down at once; its catalog stays.
- **Names** are a SQL schema and a URL segment both: letters, digits and
  underscores. The names of the server's own routes (`catalog`, `audit`,
  `keys`, `databases`, `settings`, `auth`) and the configured databases' names are taken.
- **At startup** the added databases are connected after the configured ones.
  One that cannot be reached does not stop the start: it is listed as not
  connected, with the reason, to be fixed or removed from the console. A
  configured one still stops it, as before.
- **Who.** Every route needs `admin`. With authentication off, changes are
  refused outside `APP_ENV=development`: anyone who reaches the server could
  otherwise make it connect to any host it can reach.
- Adding, changing and removing are `access_change` events
  (`add_database <name>`, `update_database <name>`, `remove_database <name>`)
  with who did it, never the host, user or password.
- Several workers or replicas sharing the file: only the process that made
  the change serves it at once; the others pick it up on their next start.

## Signing in with an identity provider

Beside the API keys, truejoin takes bearer tokens from an OpenID Connect provider
(Keycloak, Entra ID, Okta, ...). Nobody is stored in truejoin: who a caller is and
what they may do come from the token's claims.

```yaml
settings:
  auth:
    enabled: true
    oidc:
      issuer: https://login.example.com/realms/acme   # as the tokens carry it
      audience: truejoin                                  # aud the tokens must have
      roles_claim: realm_access.roles                 # dotted path; "roles" by default
      tenant_claim: tenant                            # optional
      role_permissions:
        analyst: [read, query]
        steward: [read, query, update, create]
        dpo: [read, query, audit]
      row_filters:                                    # for every caller who signs in...
        orders: [{column: tenant_id, value: "${tenant}"}]
      row_filter_exempt_roles: [dpo]                  # ...but these
      resource_url: https://truejoin.example.com          # this server, as clients reach it
```

A token is accepted when the provider's published key verifies its signature
(RS/PS/ES algorithms only, never `none` or an HMAC), it names the configured
issuer and audience, and it is unexpired (30 seconds of leeway). The keys come
from the provider's discovery document and are cached for five minutes; a key
id the cache does not know is fetched again, at most once a minute, so a key
rotation needs no restart. A token that lacks a claim the row rules need is
refused at sign-in. Production refuses an `http://` issuer.

The caller's permissions are the union of their roles' (`role_permissions`)
plus `default_permissions`; masks and the audit trail follow the roles and the
name (`username_claim`, then `sub`) as they follow an API key's.

**MCP clients** find the provider themselves: a request without credentials
is answered `401` with `WWW-Authenticate: Bearer resource_metadata=...`, and
`/.well-known/oauth-protected-resource` names the issuer (RFC 9728). The
client runs the OAuth flow with the provider and comes back with a token;
register it there as a client of the `truejoin` audience.

**The console** signs its user in when `console_client_id` is set: *Sign in*
sends them to the provider (Authorization Code with PKCE, S256), and the code
they come back with is exchanged by truejoin itself, so the page keeps talking to
truejoin only (its CSP is unchanged) and the provider needs no CORS for it. The
token is checked like any other before the page gets it, and lives in that
tab only. Register the console at the provider as a **public** client with
PKCE, the redirect URI `<truejoin's URL>/ui` (`resource_url` when truejoin is behind a
proxy), and an access token whose audience is the configured `audience`
(Keycloak: an audience mapper). A token can also be pasted where the API key
goes.

When the provider gives a refresh token with the sign-in, the console renews
its access token a minute before it expires (and when a sleeping tab is looked
at again), through truejoin (`POST /ui/sign-in/refresh`); the new token is checked
like the first, and a rotated refresh token replaces the old one. Both live in
that tab's storage only. Keycloak gives one by default; Okta and Auth0 want
`offline_access` in `console_scopes`. A provider that cannot be reached is a
`502`, and the console tries again every ten seconds while its token holds.
Without a refresh token, or once the provider refuses it (session ended,
token revoked), the next call after the token expires answers 401 and the user
signs in again.

## Users made in the console

For a team with no directory, and for the accounts a directory should not hold (a
service desk's, a contractor's), administrators can make users in the console
(**Administration**, **Users**). Name, password and permissions are all it takes; nothing else has to
run. These users sign in with a username and password beside the API keys, the
directory and the identity provider.

```yaml
settings:
  auth:
    enabled: true
    users:
      store: ./data/users.json        # password hashes, readable by the server's user only
      min_password_length: 12         # length is what is checked, not which characters
      session_ttl_seconds: 28800      # how long a sign-in lasts
      max_failures: 5                 # wrong passwords per user before sign-in pauses
      lockout_seconds: 300
```

Someone has to be the first administrator, so the CLI works on the file directly:

```bash
truejoin users add yonetici --permission all --display "Yönetici" -c config/database.yaml
truejoin users list | passwd | disable | enable
```

From then on the console does the rest: make a user, change their permissions, set a
new password for them, disable them (a disabled user is refused from the next request on,
whatever they were signed in with) or remove them. The same is available over the API under
the API prefix, `/api/v1/auth/users` (`GET`, `POST`, `PATCH /{name}`, `POST /{name}/password`,
`DELETE /{name}`), all needing the `admin` permission, plus `PUT /api/v1/auth/users/me/password`
for a user changing their own, which needs only to be signed in and to know the current password.

A name is 1 to 64 letters, digits and `. _ @ -`. `me`, `admin`, `root`, `system` and the
configured API keys' names are taken, so the audit trail never shows two callers under one name.

**Passwords** are kept as scrypt hashes (salted, 32 MiB and about 60 ms a check), never
in a file, a log, an answer or the audit trail. A name that does not exist costs a hash too, so
how long a refusal takes does not say which names exist. A wrong password, an unknown name
and a disabled account all read *Wrong username or password*, and repeated failures pause
the name (and, ten times looser, the address) with `429` and `Retry-After`.

**Who may change whom** follows the rule for keys. Nobody gives a user more than they hold; nobody
held to row rules makes users (a user without them would see what the maker cannot); a user
carries exactly its maker's roles (a role can lift masks and row rules, and leaving one off can
loosen them); and nobody changes, resets or removes an account stronger than their own, since
resetting a password is taking the account over. Nobody removes or disables the account they
are using, or lowers its permissions, so an administrator cannot lock themselves out.

**Sessions** are the tokens of directory sign-in (below). A local user's token carries the name
and a fingerprint of the password it was made with, not the permissions: disabling or removing the
user, changing what they may do, or changing their password applies to a sign-in already made, at
once. Changing a password ends every sign-in made with the old one, the person's own included.

Every change is an `access_change` event naming the user (`create_user ayse`,
`update_user ayse permissions`, `reset_password ayse`, `delete_user ayse`), never a password.
Production needs `TRUEJOIN_SECRET_KEY` (sessions would end at every restart otherwise) and
`auth.enabled: true`. A name that exists locally is only ever checked locally, so a directory
account of the same name cannot be reached through it.

Not here: a password reset by e-mail, a forced change at first sign-in, a second factor, and row
rules per user (use a key, the directory or the identity provider for those).

## Signing in with a directory (lldap, OpenLDAP, Active Directory)

For a company that has a directory and no identity provider, truejoin can check a
username and password against it. Nothing is stored in truejoin: the directory says
who the person is and which groups they belong to, and the configuration says what
each group may do.

```yaml
settings:
  auth:
    enabled: true
    ldap:
      preset: lldap                    # lldap | openldap | active_directory | freeipa
      url: ldaps://ldap.example.com:636   # or ldap:// with start_tls: true
      bind_dn: uid=truejoin,ou=people,dc=example,dc=com   # a read-only search account
      bind_password: ${file:/run/secrets/ldap_bind}
      user_base: ou=people,dc=example,dc=com
      allowed_groups: [analysts, stewards]   # empty = anyone the directory finds
      group_roles:                           # group -> roles (an unmapped group is a role of its own name)
        analysts: [analyst]
        stewards: [steward]
      role_permissions:                      # as for API keys and auth.oidc
        analyst: [read, query]
        steward: [read, query, update, create]
      session_ttl_seconds: 28800             # how long a sign-in lasts
```

A **preset** fills the user filter and attributes for that product (lldap and
Active Directory read the groups from the user's own `memberOf`; OpenLDAP searches
`group_base` for groups listing the user). Anything the file writes wins. For another
directory, set `user_filter` (with `{username}`), `username_attr`, and either
`member_of_attr` or `group_base` and `group_filter` (with `{user_dn}`).

How a sign-in goes: the username is escaped into the search filter, a service
account finds exactly one entry, and the password is checked by binding as that
entry. An empty password is refused before any bind (many servers treat it as an
anonymous bind and answer success). A wrong password and an unknown user get the same
answer. After `max_failures` wrong passwords (5) a username is paused for
`lockout_seconds` (300) with `429` and `Retry-After`; one address may fail ten times
as often before it is paused, so an office behind one proxy is not locked out by one
person's typing.

What the person receives is a token signed by truejoin (HS256, keyed from
`TRUEJOIN_SECRET_KEY`), good for `session_ttl_seconds`. It is a bearer token like an
identity provider's, so the console, scripts and MCP clients need nothing else:

```bash
curl -s -X POST https://truejoin.example.com/ui/sign-in/password \
  -H 'content-type: application/json' -d '{"username": "ayse", "password": "..."}'
# {"access_token": "...", "expires_at": 1795000000, "name": "Ayşe Yılmaz"}
```

The token carries the person and their roles, not their permissions: what a role may
do is read from the configuration when the token is used, so narrowing a role applies
to people already signed in. There is no refresh token: when the sign-in ends the
next call answers 401 and the person signs in again. The console shows *Sign in with a
password* when `auth.ldap` or `auth.users` is set (beside *Sign in*, if `auth.oidc` is too).

In production, truejoin refuses `ldap://` without `start_tls`, a missing
`TRUEJOIN_SECRET_KEY` (sessions would end at every restart and workers could not
accept each other's), and `auth.ldap` with `auth.enabled: false`. `tls_ca_file`
names a CA bundle for a private certificate; certificates are always checked.

## Usage

Every analytical tool call (REST `/olap/tools`, MCP, the console) also leaves a
`tool_call` event in the audit trail: who, which tool, how it went (`answered`,
`empty`, `refused`, `failed`, `error`), how long it took and how many rows
came back, and how the request was read, by name only: the table, the
measures, the breakdowns, the columns a condition was on and the abbreviations
the glossary wrote out. Never a value, never the SQL: a condition on
`status = 'iptal'` is recorded as `durum`.

The words of a question asked in words are not kept unless
`audit.question_text: true`, because a question can name a person ("how much
did Ayşe Yılmaz spend"). With them, the console's *Usage* reading (Administration, Audit;
`audit` permission) lists the questions that met no rows or a refusal, which is
where the glossary and the terms fall short; without them it shows how those
calls were read.

With the words kept, the summary also reads every question as `check_question`
would and lists **the words the vocabulary does not know**: each with how many
distinct questions used it, how many calls, how many of those were not
answered, and one question it came in. The ones in unanswered questions come
first, then the most asked. A question answered with a word skipped still
counts as answered, so a word can top this list while every call says
`answered`: that is the reading quietly leaving it out. The words are read as
the person looking at the summary, so a value hidden from them by a mask shows
as a gap. This is what to teach the vocabulary next (a synonym, a value, a
glossary entry), from real questions rather than a benchmark. It is under
`vocabulary` in `GET /audit/usage`, and absent when no words are kept.

A key that may also edit catalogs gets a **Means** field on each word in the
console, which says where the word goes:

- **the glossary**, written out as other words ("kargo firmasına" → "şube"):
  `PATCH /catalog/{db}/guidance` with `{"add_to_glossary": {"word": "what it means"}}`;
- **a term**, as another name for it: `PATCH /catalog/{db}/terms/{term}`
  with `{"add_synonyms": ["word"]}` (a draft term takes the word too, and
  reads it once it is certified);
- **one of a term's columns**, as another word for it: the same route with
  `{"add_to_columns": {"column": ["word"]}}`.

Each is added to what is there, on the server, so two stewards adding at once
lose nothing (`glossary`, `synonyms` and `columns` replace the whole list).
Only approved catalogs are offered, and the list is counted again at once.
The same field appears on the Query screen, under a question it did not answer
and above one it answered in part (the answer's `unread` words), for each
word it did not read; adding one asks the question again. That one
needs no kept question text: it is the question just asked.

A question can also be answered without a word the vocabulary knows: a
number, a table or a breakdown the reading made no use of. The Query screen lists
those above the answer (the answer's `unused` words) with no field to add
them, as adding a word changes nothing there. One whose words name tables no
single reading joins is refused, with the table it would have been read from
and what that would have left out.

A question none of whose words were read is refused, and so is one whose
reading would use none of the words that name a table, column or value (a
word can match a column by its stem and still be left out). The Query screen says
what the catalogs hold instead of the words alone: the terms questions are
read with, the draft terms each catalog still has to certify, and the
languages the words are in. A question about a table whose term is a draft
is not short of words: the term has to be certified first.

**Propose places** asks a model where the words belong: `POST
/catalog/suggest-words` with `{"words": [...], "lang": "tr"}` (`update`)
returns, for each word it could place, the catalog, the term and the column
with a reason, and the words it could not place. Nothing is saved: the rows
are pointed at the proposals and a person adds them. The model (the light
one when `settings.llm.non_reasoning` is set) is sent the words and the
names in the approved catalogs: terms, columns, their synonyms and the
terms' descriptions. It is not sent stored values, samples or the question.

For questions in another language, list it in `settings.i18n.languages`
(Settings, Language) and analyze the catalog again: the words of a term are
drafted in every language listed. An analysis replaces draft terms and
leaves certified ones as they are, so add a language before certifying, or
add words to certified terms one by one.

The summary is `GET /audit/usage` (`?days=30` for the last thirty days): every
tool call in the current audit file is counted, not the newest page of them.
It reads the whole file each time and not the rotated ones; past a few hundred
MB, keep the trail elsewhere and count it there.

## What to keep

| Path | What | If lost |
|---|---|---|
| `/app/catalogs` | The reviewed catalogs: descriptions, labels, terms, certifications | The review has to be done again |
| `/app/logs/audit.jsonl` | The audit trail (also on stdout as `warp.audit` log lines); `GET /audit` and the console's Audit page read its newest 8 MB for keys with the `audit` permission | It is gone; ship it somewhere |
| `auth.key_store` | The keys handed out from the console, as digests | Every handed-out key stops working; hand them out again |
| `database_store.path` | The databases added from the console, passwords encrypted | They are gone; add them again (the key alone is not enough) |
| `settings_store.path` | The settings changed from the console | They fall back to the file; set them again |
| `auth.users.store` | Every local user, as password hashes | They are gone; make them again |
| `/app/data` | DuckDB spill, backups and, when put there, the stores above | The stores as above; the data itself is reloaded from the sources |

## Checklist

1. Authentication on, real keys from files, `public_paths: ["/health"]`.
2. `CORS_ORIGINS` an explicit list.
3. A database account that can read what the API should read and nothing else;
   the read-only credentials set, so the OLAP side cannot write.
4. Row rules on the keys that need them; masking rules for the semantic types
   the catalog labels; the catalog approved.
5. Auditing on, the file on a volume or collected from stdout.
6. The image by digest, read-only, non-root; a network policy that names the
   databases.
7. `make smoke` green on the build you are about to ship.
