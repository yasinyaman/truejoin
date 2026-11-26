# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, report them privately:

- Email **yasnyaman@gmail.com** with details and reproduction steps.
- Or open a private security advisory on GitHub:
  <https://github.com/yasinyaman/truejoin/security/advisories/new>.

Please include:

- a description of the issue and its impact,
- steps to reproduce (a minimal PoC if possible),
- affected version/commit, and
- any suggested remediation.

We aim to acknowledge reports within a few business days and will keep you
updated on remediation progress. Please give us reasonable time to release a fix
before any public disclosure.

## Supported versions

1.0.0 is being prepared and is not released yet; until it is, security fixes
target the latest `main`. Install from the lock file (`uv sync --frozen`):
`uv.lock` pins every dependency with its hash.

## Security posture

Truejoin is built with safe-by-default behavior. Key controls:

- **Fail-safe production config** — with `APP_ENV=production`, startup is refused
  when authentication is disabled, the raw SQL endpoint is enabled, `CORS_ORIGINS`
  is `*`, an API key is a placeholder, row rules are set while authentication is
  off, the OpenAPI spec is public (unless `auth.allow_public_openapi`), an
  `auth.oidc` issuer or key URL is not `https://`, `auth.oidc`, `auth.ldap` or
  `auth.users` is set while authentication is off, `auth.ldap` uses `ldap://`
  without `start_tls`, `auth.ldap` or `auth.users` has no `TRUEJOIN_SECRET_KEY`,
  or a `hash` mask has a short secret (a missing one is refused everywhere).
- **Parameterized SQL** — all values are bound parameters (raw SQL uses `:name`
  placeholders bound by `truejoin.adapters.outbound.db.params`); dynamic identifiers
  go through a single strict whitelist/quoting layer
  (`truejoin.adapters.outbound.db.identifiers`). Filter values are converted to the
  column's type rather than guessed from their shape.
- **Catalog endpoints** — every `/api/v1/catalog/*` route requires an API key
  with the matching permission (read / create for analysis / update / delete).
  Catalog names are validated and the file store never resolves a path outside
  its root.
- **OpenAPI spec** — `/openapi.json` carries catalog descriptions and
  `x-llm-context`; it is served through the auth manager and, in production,
  startup is refused while it is public unless `auth.allow_public_openapi: true`.
  Example values from real rows are only injected when
  `catalog.openapi_include_examples: true`.
- **Raw SQL endpoint** — disabled by default; when enabled it whitelists
  commands, rejects multiple statements, and can be pointed at a dedicated
  read-only database role (`readonly_username`/`readonly_password`) so a
  whitelist bypass still cannot write.
- **Authentication** — API keys are compared in constant time
  (`secrets.compare_digest`) against stored SHA-256 hashes; plaintext keys are
  not retained. Callers can also present bearer tokens from an OpenID Connect
  provider (`auth.oidc`; asymmetric signatures only), or sign in with a password:
  against a directory (`auth.ldap`: lldap, OpenLDAP, Active Directory, FreeIPA,
  through ldap3; the username is escaped into the filter and an empty password
  never reaches the directory) or as a local user (`auth.users`: scrypt hashes in
  a file readable by the server's user only). A password sign-in returns a
  short-lived HS256 token whose key is derived from `TRUEJOIN_SECRET_KEY`
  (`truejoin/adapters/inbound/http/sessions.py`); a local user's token is checked
  against the account on every use, so disabling a user or changing their
  password ends it at once. Wrong passwords and unknown names get the same answer,
  and repeated failures pause the name and, more loosely, the address.
- **Mass-assignment protection** — create/update reject primary-key,
  auto-generated, and configured read-only columns.
- **Secrets** — provided via environment (`.env`, gitignored); no credentials in
  the repo. The container image runs as a non-root user.
- **LLM privacy** — sample data is not sent to cloud LLM providers unless opted
  in, PII-looking columns are masked before any LLM call, and sample values of
  PII columns (by name pattern or LLM semantic type) are never stored in the
  catalog.
- **Row-level security** — an API key may carry mandatory per-table conditions.
  Reads with a `WHERE` clause push them into SQL; reads and writes by primary
  key match in memory and report a row outside the policy as 404, not 403.
  Writes cannot create into, or move a row into, a scope the caller cannot
  read. A key with row rules is denied raw SQL even when it holds `all`.
- **Column masking** — keyed on the catalog's semantic types rather than column
  names, so a newly labelled PII column is masked as soon as it is discovered.
  Applied to CRUD responses and to `/export` in all three formats.
- **Analytical SQL** — only query statements pass (`SqlglotValidator`); multiple
  statements and file or network functions are refused, and DuckDB runs with
  `enable_external_access` off as the backstop.
- **Row rules and masks on analytical SQL** — for a restricted caller, every
  table a query names is replaced by a subquery carrying that caller's row
  conditions and masks, so aliases, joins, CTEs and aggregates cannot read
  around them ([ADR 12](docs/docs/adr/0012-access-rewrite.md)). This holds for
  SQL written by a model as much as by a person. Random queries check it on every
  test run: each must answer what it answers on tables holding only the
  caller's rows, masked, or be refused (`tests/fusion/sql_fuzz.py`).
- **Questions it cannot read** — by default (`olap.unread_questions: sql`) a
  model writes SQL for them from the caller's visible schema only; the
  question and that schema go to the configured model, and sample values are
  withheld from cloud models unless `analysis.share_samples_with_cloud_llm` is
  on. With no usable model, or with `refuse`, they are refused with the words
  that could not be read.
- **Join checks** — `olap.grounding` (`off` by default; `advise` or `repair`)
  judges each join against the catalog's keys and refuses one it cannot fix.
- **Audit trail** — one append-only event per data-touching request: actor,
  tenant, roles, action, table, row count, request id, and whether a row filter
  or a column mask applied.

### What the audit trail deliberately does not contain

Row values, filter literals and SQL parameters are **never** written to it, and
the text of a question only when `audit.question_text` is on.
Filter *column names* are recorded; the values compared against them are not.
An audit log that quotes the data it audits becomes a second copy of that data,
usually in a file with weaker access controls and a longer retention period
than the database it came from.

Events go to the dedicated `warp.audit` logger (the name kept from Warp, so
existing log routing keeps working), and to `audit.file` when one is
configured (appended, never rewritten). Route that logger to whatever retention
your obligations require — it is deliberately separate from application logs so
the two can be retained differently. Each line is one JSON object, so exporting
a period is `grep`/`jq` over the file rather than a bespoke tool.

See ["What production refuses"](docs/docs/deployment.md#what-production-refuses)
and the decision records in [`docs/docs/adr/`](docs/docs/adr/) for details.
