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

One process, one config file and one command since Phase 1. `/olap` and MCP
do not rewrite SQL per caller yet, so keys with row rules and masked callers
are refused there. See [CHANGELOG.md](CHANGELOG.md) and the origin READMEs under
[docs/origins/](docs/origins/).
