# Deployment

trio is one image, one process and one port. It reads one YAML file and the
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
make image                     # docker build -t trio-engine:dev .
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
  trio-engine:dev trio serve --transport stdio
```

`mcp.stdio_api_key_name` says which key's row rules and masks that one caller has.

### Kubernetes

```bash
helm install trio helm/trio -f my-values.yaml
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
  through the API or the console, within 30 seconds for `trio catalog` on the
  command line. A draft leaves what is live alone.

The chart has been linted and rendered, not installed on a cluster.

## Secrets

Two forms, both resolved when the config is loaded, neither ever logged
(`trio config show` masks them):

- `${NAME}` or `${NAME:default}`: an environment variable;
- `${file:/path}`: the contents of a file, without the trailing newline. A
  missing file is an error, not an empty password.

Prefer files. In Compose they are `secrets:`; in Kubernetes the chart mounts
the keys of `secrets.existingSecret` under `/run/secrets/trio`.

**Vault.** trio does not talk to Vault and has no SDK for it. Use the Agent
Injector: annotate the pod (`podAnnotations` in the chart) and read what the
agent writes with `${file:/vault/secrets/<name>}`.

```yaml
podAnnotations:
  vault.hashicorp.com/agent-inject: "true"
  vault.hashicorp.com/role: "trio"
  vault.hashicorp.com/agent-inject-secret-db-password: "secret/data/trio/db"
```

`masking.hash_secret` is a secret like any other: whoever holds it can
re-derive every hashed value.

## What to keep

| Path | What | If lost |
|---|---|---|
| `/app/catalogs` | The reviewed catalogs: descriptions, labels, terms, certifications | The review has to be done again |
| `/app/logs/audit.jsonl` | The audit trail (also on stdout as `warp.audit` log lines); `GET /audit` and the console's Audit tab read its newest 8 MB for keys with the `audit` permission | It is gone; ship it somewhere |
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
