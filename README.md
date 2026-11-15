# Truejoin

One package made from three: **Warp** (database → governed REST/OpenAPI with an
LLM-written, human-approved catalog), **Fusion** (DuckDB in-memory OLAP and the
LLM tool surface) and **Stargate** (OpenAPI → MCP).

Goal: open a corporate database to agents through an approved catalog, with
row-level security, column masking and an audit trail on every query, whoever
wrote the SQL; Oracle and SQL Server included; on-prem.

```bash
uv sync --all-extras
uv run pytest
uv run ruff check truejoin tests bench demo
uv run mypy truejoin
```

```bash
make image          # the image; `make wheelhouse image-offline` builds it with no network
make smoke          # that image, production mode, real PostgreSQL: the release check
helm install truejoin helm/truejoin -f my-values.yaml
```

```bash
export TRUEJOIN_SECRET_KEY=$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
uv run truejoin config validate -c config/database.yaml   # also says when a key is missing
uv run truejoin serve -c config/database.yaml     # REST gateway, /olap and /mcp on :8000
uv run truejoin serve --transport stdio           # the MCP tools alone, for a desktop client
uv run truejoin users add yonetici --permission all -c config/database.yaml   # a first local administrator
```

Callers sign in with API keys, an identity provider (`auth.oidc`), the company
directory (`auth.ldap`: lldap, OpenLDAP, Active Directory, FreeIPA) or users made
in the console (`auth.users`); the console's *Users* and *Keys* tabs manage the
last two kinds. See [docs/docs/deployment.md](docs/docs/deployment.md).

One process, one config file and one command since Phase 1. Since Phase 2,
`/olap` and MCP run every query under the caller's row rules and column masks
and leave one audit event per query. Since Phase 3, `olap.grounding` checks
every join against the keys the databases declare (`advise` reports a wrong
one, `repair` fixes it or refuses). Since Phase 4, `query_metrics` reads a
referenced table's columns by following those keys (`customers.segment` beside
a measure of `orders`), and `olap.terms` adds reviewed business terms: the
words for tables, columns and values, which name the measures and let
`answer_question` answer a question asked in words, with no model at query
time (`check_question` shows first which words of it the vocabulary knows). Since Phase 5, `truejoin catalog suggest` mines the query history and the
views for the expressions people repeat and offers them as draft measures,
which `truejoin catalog certify` turns into measures. See [CHANGELOG.md](CHANGELOG.md), [bench/README.md](bench/README.md) and
the origin READMEs under [docs/origins/](docs/origins/).
