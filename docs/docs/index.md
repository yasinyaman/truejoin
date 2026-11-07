# trio

One process that opens corporate databases to agents through a reviewed
catalog, with row-level security, column masking and an audit trail on every
query, whoever wrote the SQL. PostgreSQL, MySQL, SQL Server and Oracle;
on-premises, air-gapped if need be.

It is three projects made into one package: **Warp** (database to governed
REST and OpenAPI, with a catalog a model writes and a person approves),
**Fusion** (in-memory OLAP on DuckDB and the tool surface for agents) and
**Stargate** (OpenAPI to MCP). The name `trio` is a placeholder.

## What it does

- **A REST API per table**, with filtering, sorting, pagination, typed schema
  and streaming export.
- **A catalog**: descriptions, semantic types, relationships and business
  terms, written by a model and read by nothing until a reviewer approves and
  certifies them.
- **Analytical queries** over `/olap` and MCP: SQL, metrics without SQL, and
  questions in words. Tables too big to load are read in slices.
- **Governance on every read.** A caller's row rules and masks apply to each
  table a query names, through aliases, joins and subqueries. One audit event
  per query, never the SQL.
- **Grounding.** Joins are checked against the keys the databases declare;
  `query_metrics` joins along those keys so a request cannot write a wrong one.
- **Measures from the past.** The query history and the views are mined for
  the definitions people repeat; a reviewer certifies the ones that are right,
  or writes one by hand.
- **Words a reviewer adds.** A catalog's abbreviations are written out before
  a question is matched, and its notes reach a model with the source
  (`list_sources`, the OpenAPI `x-llm-context`). Both are set in the console's
  Ontology overview or with `PATCH /catalog/{db}/guidance`, and used once the
  catalog is approved.

## Where to go

- [Deployment](deployment.md): the image, an air-gapped build, Compose, Helm,
  secrets, and what production refuses to start with.
- [Architecture](architecture.md) and the [decisions](adr/README.md) behind it.
- [Measures from the query history](query-history.md).
- `CHANGELOG.md` in the repository for what each phase added and what is not
  there yet.
