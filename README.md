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
uv run trio config validate -c config/database.yaml
uv run trio serve -c config/database.yaml     # REST gateway, /olap and /mcp on :8000
uv run trio serve --transport stdio           # the MCP tools alone, for a desktop client
```

One process, one config file and one command since Phase 1. Since Phase 2,
`/olap` and MCP run every query under the caller's row rules and column masks
and leave one audit event per query. Since Phase 3, `olap.grounding` checks
every join against the keys the databases declare (`advise` reports a wrong
one, `repair` fixes it or refuses). See [CHANGELOG.md](CHANGELOG.md) and the origin READMEs under
[docs/origins/](docs/origins/).
