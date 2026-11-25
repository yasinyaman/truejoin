---
description: "Which Truejoin settings live in the YAML file and which are changed from the console, with the environment variables and the settings that need confirmation."
---

# Configuration: the file and the console

A server reads a small YAML file (`config/database.yaml`) for what it needs
before anything else. Everything else is set from the console's **Settings**
tab by an admin, kept in `settings.settings_store.path`, and served at once:
the server rebuilds itself with the new settings before the change is
answered. `config/reference.yaml` lists every setting with comments.

## What the file holds

These are read from the file only; the console shows them read-only under
**Startup (file only)**:

| Settings | Why they stay in the file |
|---|---|
| `settings.api_prefix`, `mcp.http_path`, `mcp.stdio_api_key_name` | Every client's URLs depend on them |
| `settings.auth.*` (enabled, keys, public paths, OpenID Connect, the directory `auth.ldap`, local users `auth.users`) | From the console, one mistake or one intruder could lock everyone out |
| `settings.audit.enabled`, `settings.audit.file` | An intruder must not be able to switch off the trail of what they did |
| `settings.masking.hash_secret` | The pseudonym key: changing it breaks every join on a pseudonym |
| `settings.catalog.storage_path`, `settings.auth.key_store`, `settings.database_store.*`, `settings.settings_store.*` | Where the server keeps what the console changes |
| `olap.enabled`, `olap.database`, `olap.memory_limit`, `olap.threads`, `olap.external_access`, `olap.temp_directory`, `olap.max_temp_directory_size` | The DuckDB engine itself |
| `databases` | The databases every start needs; more can be added from the console (Administration, Databases) |

Environment variables stay as they are: `APP_ENV`, `CONFIG_PATH`, `CORS_ORIGINS`
(empty means `*`, which production refuses), `API_HOST`, `API_PORT`, `API_WORKERS`,
`LOG_LEVEL`, `LOG_FORMAT`; a model's key from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or
`GOOGLE_API_KEY` when its `api_key` is empty; the deprecated `FUSION_*` and `WARP_*`
variables, which still set their `olap` settings and lock them as the file would; and
`TRUEJOIN_SECRET_KEY`, the Fernet key that encrypts what the console keeps (database
passwords, model API keys) and signs password sign-ins. It is required whenever a store,
`auth.ldap` or `auth.users` is set: a store without it refuses to start (and
`truejoin config validate` says so), and sessions read it by that name even when a
store's `key_env` names another:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

## What the console changes

Every other setting: the API's limits and exports, raw SQL, masking rules,
what the audit trail keeps of a question, the catalog and analysis options,
the models (`settings.llm.*`), the languages catalogs are written in, the analytics engine's budgets,
cache, rate limit and grounding, remote sources, and the MCP profile and tools.

- **At once.** A change is checked as a whole (every validator, and the
  production rules under `APP_ENV=production`), kept, and served by a new
  generation of the server built beside the running one: table routes,
  `/olap`, MCP and the catalog. Requests already running finish on the old
  one, which is closed 30 seconds later. If the new one does not build,
  nothing changes and the change is not kept. A file-backed `olap.database`
  cannot be open twice: then `/olap` pauses while it rebuilds. A rebuild
  reconnects the databases and starts the OLAP cache empty; tables load again
  as they are queried.
- **Locked by the file.** A setting the file writes is shown with a `file`
  badge and cannot be changed from the console. Delete it from the file to
  manage it there. A value that reads the environment (`${VAR}`) stays in the file.
- **Sensitive settings** (raw SQL, masking, where samples and questions are
  sent, sample values in the OpenAPI spec, keeping question text, the models,
  remote sources, the backup path, the debug routes, the MCP profile and tools)
  are applied only after their names are typed back.
- **Secrets** (`*.api_key`) are encrypted in the store and never shown; a
  blank value keeps the one kept.
- **Who.** Only a caller with the `admin` permission (an API key, or a signed-in
  user whose roles or permissions grant it). With authentication off, changes are
  refused outside `APP_ENV=development`.
- **Trail.** Each change is an `access_change` event naming the paths changed
  (`settings olap.cache_ttl,...`), never their values.
- **Reset** returns a setting to the file's or the default value.
- **Not offered:** `settings.docs_url`, `settings.redoc_url`,
  `settings.llm.language_prompts`, `settings.i18n.fallback_language` and
  `settings.i18n.auto_translate` are not read by anything, so the console leaves them
  out (`/docs` and `/redoc` are served outside production only).
- **Several workers** sharing the store pick a change up within 30 seconds.

The same is available over the API: `GET`, `PUT /settings`
(`{"changes": {path: value}, "reset": [path], "confirm": [path]}`) under the
API prefix.

## Moving an existing file to the console

```bash
truejoin config import -c full.yaml --minimal-out database.yaml
```

copies every setting the console may hold from `full.yaml` into the store
(the file must name `settings.settings_store.path`, and `TRUEJOIN_SECRET_KEY`
must be set), and writes `database.yaml` with only what must stay: the
startup settings, the databases, and any value read from the environment.
Serve with the new file. Without `--minimal-out` it lists what to delete from
the file; until then the file keeps those settings locked.
