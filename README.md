# trio-engine

One package made from three: **Warp** (database → governed REST/OpenAPI with an
LLM-written, human-approved catalog), **Fusion** (DuckDB in-memory OLAP and the
LLM tool surface) and **Stargate** (OpenAPI → MCP). The package name `trio` is a
placeholder until the product is named.

Goal: open a corporate database to agents through an approved catalog, with
row-level security, column masking and an audit trail on every query, whoever
wrote the SQL; Oracle and SQL Server included; on-prem.

```bash
uv sync --all-extras
uv run pytest
uv run ruff check trio tests bench demo
uv run mypy trio
```

```bash
make image          # the image; `make wheelhouse image-offline` builds it with no network
make smoke          # that image, production mode, real PostgreSQL: the release check
helm install trio helm/trio -f my-values.yaml
```

```bash
uv run trio config validate -c config/database.yaml
uv run trio serve -c config/database.yaml     # REST gateway, /olap and /mcp on :8000
uv run trio serve --transport stdio           # the MCP tools alone, for a desktop client
```

One process, one config file and one command since Phase 1. Since Phase 2,
`/olap` and MCP run every query under the caller's row rules and column masks
and leave one audit event per query. Since Phase 3, `olap.grounding` checks
every join against the keys the databases declare (`advise` reports a wrong
one, `repair` fixes it or refuses). Since Phase 4, `query_metrics` reads a
referenced table's columns by following those keys (`customers.segment` beside
a measure of `orders`), and `olap.terms` adds reviewed business terms: the
words for tables, columns and values, which name the measures and let
`answer_question` answer a question asked in words, with no model at query
time (`check_question` shows first which words of it the vocabulary knows). Since Phase 5, `trio catalog suggest` mines the query history and the
views for the expressions people repeat and offers them as draft measures,
which `trio catalog certify` turns into measures. See [CHANGELOG.md](CHANGELOG.md), [bench/README.md](bench/README.md) and
the origin READMEs under [docs/origins/](docs/origins/).
