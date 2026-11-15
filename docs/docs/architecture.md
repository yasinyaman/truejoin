# Architecture

One hexagonal (ports and adapters) package. Dependencies point inward and one
test enforces it: `tests/fusion/architecture/test_dependency_rules.py` walks
every import, lazy ones included.

```
inbound adapters (HTTP, MCP, CLI, SDK) --> application (services) --> ports (Protocols)
                                                  |                        ^
                                                  v                        | implemented by
                                           domain (pure)        outbound adapters (databases, DuckDB,
                                                                 sqlglot, LLM providers, catalog store)
truejoin.bootstrap wires adapters into the services.
```

| Layer | Package | May import |
|---|---|---|
| Domain | `truejoin.domain.{olap,gateway,governance,catalog,mcp}`, `truejoin.domain.settings_catalog` | domain; pydantic for the catalog's file format |
| Ports | `truejoin.ports` | domain, ports, `truejoin.application.settings`; pydantic |
| Application | `truejoin.application.{olap,gateway,catalog,mcpgen}` and its modules (`settings`, `settings_service`, `access_keys`, `local_users`, `databases`, `usage`, `container`) | domain, ports, application; pydantic, structlog |
| Observability | `truejoin.observability` | domain, application, observability |
| Outbound adapters | `truejoin.adapters.outbound.*` | domain, ports, `truejoin.application.settings`, their drivers |
| Inbound adapters | `truejoin.adapters.inbound.{http,rest,mcp,cli,sdk}` | domain, ports, application, observability, `truejoin.bootstrap`, `truejoin.runtime_mcpgen` and their frameworks |
| Composition | `truejoin.bootstrap`, `truejoin.main` | anything |

`tests/fusion/architecture/test_dependency_rules.py` holds the exact rules and checks
every import, lazy ones included.

## One process

`truejoin serve` runs one ASGI app: the REST gateway (a router per table), the
OLAP routes at `/olap`, and the MCP tools over streamable HTTP at `/mcp`. The
gateway's authentication guards all three: API keys, and bearer tokens from an
identity provider, the directory or local users. `--transport stdio` serves the MCP tools
alone. See [ADR 10](adr/0010-one-package-one-process.md).

The gateway is async (asyncpg, aiomysql, aioodbc) and the OLAP engine is
synchronous (DuckDB behind one lock). They meet on a loop thread that owns the
OLAP side's database pools; see [ADR 11](adr/0011-sync-async-bridge.md).

## The path of an analytical query

```
validate (SELECT only, no file or network functions)
  -> ground (joins against the declared keys: advise or repair)
  -> restrict (each table replaced by the caller's view of it: row rules, masks)
  -> plan (reuse what is loaded, load whole, load a slice, semi-join, or refuse)
  -> execute on DuckDB -> one audit event
```

Every surface reaches this path: `query_data`, `/olap/query`, `query_metrics`
with a join, and `answer_question`. See [ADR 12](adr/0012-access-rewrite.md).

## The catalog, and who may read it

Analysis writes a draft: descriptions, semantic types, relationships checked
against the data, and business terms. A reviewer approves tables; the CLI
review, which shows each table's term, certifies it as well. Only an approved catalog labels columns for masking,
adds relationships to the join graph, names measures and answers questions.
See [ADR 13](adr/0013-certified-gate.md).
