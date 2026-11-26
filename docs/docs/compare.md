---
description: "Truejoin beside Google's MCP Toolbox for Databases, Microsoft's SQL MCP Server (Data API builder), Oracle's SQLcl MCP Server and DreamFactory: databases, what the model may run, row rules and masks, audit, deployment and cost."
---

# Truejoin beside the alternatives

Four other products put an MCP server in front of a relational database. Each
is a good answer to a narrower question than Truejoin's, and each is better
than Truejoin at something. This page says which, from each vendor's own
documentation as read on **2026-11-26**. Products change quickly: follow the
links before relying on a row, and open an issue when one is out of date.

## At a glance

| | Truejoin | MCP Toolbox for Databases | SQL MCP Server (Data API builder) | SQLcl MCP Server | DreamFactory |
|---|---|---|---|---|---|
| Maker | independent | Google | Microsoft | Oracle | DreamFactory Software |
| License, cost | Apache 2.0, free | Apache 2.0, free | MIT, free | part of SQLcl | open-source core (PostgreSQL only); commercial plans from $1,500 a month, billed yearly |
| PostgreSQL, MySQL | yes | yes | yes | no | yes |
| SQL Server | yes (ODBC) | yes | yes | no | commercial plans |
| Oracle | yes (ODBC) | yes | no | yes | $4,000 a month plan and up |
| What the model sends | SQL (one read-only `SELECT`), a structured metric request, or a question in words | the parameters of SQL the operator wrote; SQL of its own through the `execute-sql` tool kinds | the parameters of fixed tools over configured entities; never SQL | SQL of its own, run on a saved connection | calls to generated table and procedure tools; never SQL |
| Per-caller rows | row rules, applied to every table of every query by rewriting it | a parameter filled from a token claim, in the SQL the operator wrote | database policies, turned into a `WHERE` | the database account's own privileges | not documented |
| Per-caller columns | masks by semantic type (hash, partial, last four, redact, null) | none documented | fields included or excluded per role | the account's privileges | field-level security and masking per role |
| One query over two databases | yes, on two engines | not documented | no | no: one connection at a time | not documented |
| Writes | REST routes, per key; MCP tools read only | as the SQL the operator wrote allows | create, update, delete tools | anything the account may do | per role and HTTP verb |
| Audit | one event per read: who, which tables, rows, rules and masks; never SQL or values | OpenTelemetry traces and metrics | OpenTelemetry, Application Insights, file logs | each request in `DBTOOLS$MCP_LOG`, the SQL text in Oracle's examples | every query logged (commercial edition) |
| Runs | one process or image, on your servers, built with no network if need be | one binary or container | a container, on-premises or in any cloud | a local process beside the client | self-hosted, air-gapped supported |
| Maturity | 1.0.0 in preparation, not on PyPI | v1.18 | Data API builder 1.7 (MCP), 2.0 in preview | SQLcl 25.2 onwards | a commercial product with support |

## The differences that matter

**What the model is allowed to send.** Data API builder and DreamFactory never
take SQL from the model: it calls fixed tools whose SQL the product builds.
Nothing a model writes can join around a rule, and nothing it writes can join
at all. Toolbox does the same with SQL the operator writes in `tools.yaml`,
and offers `execute-sql` tool kinds that run whatever SQL they are given.
SQLcl's `run-sql` runs the model's SQL on the connection the user saved.
Truejoin takes the model's SQL and rewrites it: every table the query names
becomes a subquery holding only what that caller may read, and what it cannot
wrap is refused. That is the one place Truejoin trusts its own code more than
the others do, so it is the part to test hardest ([ADR 12](adr/0012-access-rewrite.md)).

**Where per-caller rules live.** In Toolbox a rule is a parameter of one
statement, filled from a token claim: it holds for that statement and nothing
else. Data API builder's database policies hold for every tool over an entity,
but single-table policies are supported on SQL Server and Synapse only, and
multi-table policies on none. SQLcl leaves it to the database: an Oracle user's
grants, or Oracle's own virtual private database. Truejoin holds row rules per API key and per sign-in through the identity
provider or the directory (a claim such as the tenant fills them), masks per
role, and applies both the same way on all four engines and across a join of
two of them.

**What the audit says.** SQLcl keeps the SQL text with each request, in a table
of the database. Toolbox and Data API builder emit OpenTelemetry for a
collector to keep. Truejoin writes one append-only event per read with names
and counts only. That suits personal-data rules (KVKK, GDPR), which treat a log
that quotes the data as a second copy of it. A forensic team that needs the
statement itself gets nothing from Truejoin's log; `audit.question_text`
keeps the words of a question asked in words, and nothing keeps SQL.

## Where the others are stronger

- **Live data.** All four read the database at the time of the call.
  Truejoin's analytical side loads what a query needs into DuckDB and answers
  later queries from it until it is dropped or reloaded, on a timer if
  `olap.refresh_interval` is set; its REST routes are live. The deployment
  guide says
  [how fresh the data is](deployment.md#how-fresh-the-data-is).
- **Engines.** Toolbox connects to far more sources than four, Google Cloud's
  managed databases and non-relational ones among them.
- **A narrow, safe surface by construction.** A product whose model cannot
  write SQL has less to get wrong than one that rewrites the SQL a model
  wrote. Where a fixed set of tools answers the questions people ask, Data
  API builder or DreamFactory is the simpler thing to trust.
- **Writes.** Data API builder, DreamFactory and SQLcl let an agent change
  data. Truejoin's MCP tools only read.
- **Support and track record.** Each of the others has a company behind it,
  releases in use and a support channel. Truejoin has none of those yet.
- **The Oracle-only site.** For a site with Oracle alone, SQLcl's server and
  Oracle's managed one on OCI (OAuth 2.0 through OCI IAM, Streamable HTTP)
  come from the vendor and lean on the database's own security.

## Where Truejoin is the one that fits

- Several engines at once, Oracle and SQL Server among them, under one set of
  row rules and masks, and a query that joins two of them.
- A model that writes its own SQL, held to what its caller may see without a
  rule being written per statement.
- Questions in words answered with no model at query time, from a
  vocabulary a person approved ([the pilot guide](pilot.md) measures it on your
  own questions).
- Nothing leaving the site: a local model for the catalog, an image built with
  no network, an audit trail with no values in it.

## They can run side by side

Each of these is a server an MCP client can list next to the others. A
common split: Truejoin for analytical questions over several databases under
per-caller rules, and a vendor's server for the narrow, write-capable tasks
over one.

## Sources

Read on 2026-11-26.

- MCP Toolbox for Databases: [README](https://github.com/googleapis/mcp-toolbox/blob/main/README.md),
  [tools](https://mcp-toolbox.dev/documentation/configuration/tools/),
  [authentication](https://mcp-toolbox.dev/documentation/configuration/authentication/),
  [read-only mode](https://mcp-toolbox.dev/documentation/configuration/security/read-only/).
- SQL MCP Server and Data API builder: [MCP overview](https://learn.microsoft.com/en-us/azure/data-api-builder/mcp/overview),
  [DML tools](https://learn.microsoft.com/en-us/azure/data-api-builder/mcp/data-manipulation-language-tools),
  [feature availability](https://learn.microsoft.com/en-us/azure/data-api-builder/feature-availability),
  [overview](https://learn.microsoft.com/en-us/azure/data-api-builder/overview),
  [row-level security](https://learn.microsoft.com/en-us/azure/data-api-builder/concept/security/row-level-security).
- SQLcl MCP Server: [tools](https://docs.oracle.com/en/database/oracle/sql-developer-vscode/25.3/sqdnx/sqlcl-mcp-server-tools.html),
  [how it works](https://docs.oracle.com/en/database/oracle/sql-developer-vscode/25.3/sqdnx/how-sqlcl-mcp-server-works.html),
  [monitoring](https://docs.oracle.com/en/database/oracle/sql-developer-command-line/25.3/sqcug/monitoring-sqlcl-mcp-server.html);
  OCI Database Tools [MCP server](https://docs.oracle.com/iaas/database-tools/doc/overview-mcp-server.html).
- DreamFactory: [pricing](https://www.dreamfactory.com/pricing),
  [MCP server](https://www.dreamfactory.com/use-cases/mcp-server),
  [README](https://github.com/dreamfactorysoftware/dreamfactory/blob/master/README.md).
- Truejoin: [README](https://github.com/yasinyaman/truejoin/blob/main/README.md),
  [SECURITY.md](https://github.com/yasinyaman/truejoin/blob/main/SECURITY.md),
  [ADR 12](adr/0012-access-rewrite.md), [deployment](deployment.md).
