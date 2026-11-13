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
- a `hash` mask is configured with a short secret.

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
| `WITH_ORACLE_ODBC` | `0` (default), `1` | Oracle Instant Client from `docker/vendor/oracle`; never downloaded |

PostgreSQL and MySQL need no driver. `docker/vendor/README.md` says what to
put where for the other two.

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
fourteen checks that a tenant reads its own rows over REST, MCP and metrics
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

The chart has been linted and rendered, not installed on a cluster.

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
hand out and revoke keys at run time: the console's **Keys** tab, or `GET`,
`POST /keys` and `DELETE /keys/{id}` under the API prefix.

```yaml
settings:
  auth:
    enabled: true
    key_store: /app/keys/keys.json   # on a volume, like the catalogs
    api_keys:
      - {key: "${file:/run/secrets/truejoin/admin-key}", name: admin, permissions: [read, admin]}
```

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

## Adding databases from the console

Databases in `databases:` change with the configuration file and a restart.
With `database_store.path` naming a file, a caller with the `admin` permission
can also add, change and remove databases while the server runs: the
console's **Databases** tab, or `GET`, `POST /databases`, `POST /databases/test`,
`PUT /databases/{name}` and `DELETE /databases/{name}` under the API prefix.

```yaml
settings:
  database_store:
    path: /app/keys/databases.json   # on a volume, like the catalogs
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
  `keys`, `databases`) and the configured databases' names are taken.
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
did Ayşe Yılmaz spend"). With them, the console's *Usage summary* (Audit tab,
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
- **a certified term**, as another name for it: `PATCH /catalog/{db}/terms/{term}`
  with `{"add_synonyms": ["word"]}`;
- **one of a term's columns**, as another word for it: the same route with
  `{"add_to_columns": {"column": ["word"]}}`.

Each is added to what is there, on the server, so two stewards adding at once
lose nothing (`glossary`, `synonyms` and `columns` replace the whole list).
Only approved catalogs are offered, and the list is counted again at once.
The same field appears in the Query tab, under a question it did not answer
and above one it answered in part (the answer's `unread` words), for each
word it did not read; adding one asks the question again. That one
needs no kept question text: it is the question just asked.

The summary is `GET /audit/usage` (`?days=30` for the last thirty days): every
tool call in the current audit file is counted, not the newest page of them.
It reads the whole file each time and not the rotated ones; past a few hundred
MB, keep the trail elsewhere and count it there.

## What to keep

| Path | What | If lost |
|---|---|---|
| `/app/catalogs` | The reviewed catalogs: descriptions, labels, terms, certifications | The review has to be done again |
| `/app/logs/audit.jsonl` | The audit trail (also on stdout as `warp.audit` log lines); `GET /audit` and the console's Audit tab read its newest 8 MB for keys with the `audit` permission | It is gone; ship it somewhere |
| `auth.key_store` | The keys handed out from the console, as digests | Every handed-out key stops working; hand them out again |
| `database_store.path` | The databases added from the console, passwords encrypted | They are gone; add them again (the key alone is not enough) |
| `/app/data` | DuckDB spill and, when enabled, backups | Nothing: it is reloaded from the sources |

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
