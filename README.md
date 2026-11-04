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

Phase 0 state: the origin servers still run as separate processes
(`trio-warp`, `trio-olap-rest`, `trio-olap-mcp`). See [CHANGELOG.md](CHANGELOG.md)
and the origin READMEs under [docs/origins/](docs/origins/).
