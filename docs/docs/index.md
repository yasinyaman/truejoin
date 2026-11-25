---
description: "Truejoin serves PostgreSQL, MySQL, SQL Server and Oracle to AI agents over MCP, REST and SQL, with row-level security, column masking and an audit trail on every query."
---

# Truejoin

One process that opens corporate databases to agents through a reviewed
catalog, with row-level security, column masking and an audit trail on every
query, whoever wrote the SQL. PostgreSQL, MySQL, SQL Server and Oracle;
on-premises, air-gapped if need be.

<video controls preload="metadata" width="100%" src="assets/console-demo.mp4">
  The console in a minute: <a href="assets/console-demo.mp4">console-demo.mp4</a>.
</video>

*The console: a tenant's key and an administrator's run the same query, one
statement joins a PostgreSQL and a MySQL, the catalog, a key with row rules
checked before it is handed out, and the audit trail.*

<video controls preload="metadata" width="100%" src="assets/claude-demo.mp4">
  Claude over MCP: <a href="assets/claude-demo.mp4">claude-demo.mp4</a>.
</video>

*The same data read by Claude Code over MCP, replayed as the session ran: one
tenant's rows, national ids masked, every read in the audit trail.*

It is three projects made into one package: **Warp** (database to governed
REST and OpenAPI, with a catalog a model writes and a person approves),
**Fusion** (in-memory OLAP on DuckDB and the tool surface for agents) and
**Stargate** (OpenAPI to MCP).

## What it does

- **A REST API per table**, with filtering, sorting, pagination, typed schema
  and streaming export.
- **A catalog**: descriptions, semantic types, relationships and business
  terms, written by a model and read by nothing until a reviewer approves and
  certifies them.
- **Analytical queries** over `/olap` and MCP: SQL, metrics without SQL, and
  questions in words. Tables too big to load are read in slices.
- **Follow-up questions.** `answer_question` takes the `request` an earlier
  answer returned as `previous`, and reads "and by branch?", "only the
  cancelled ones" or "the top 3" as that request changed where the new words
  say: a breakdown replaces the old one ("and monthly" adds one), a condition
  replaces one on the same column, an aggregation alone applies to the measure
  there was. No model, and nothing kept on the server between calls: the
  caller carries the conversation (the console's Question mode has a box for
  it). Each column of an answer says what it is (`labels`: the measure, its
  aggregation and the catalog's description, or the breakdown and its grain),
  and the console writes the reading out as a sentence. A question it reads in
  part lists the words it left out (`unread`:
  not in the vocabulary; `unused`: known, but the reading makes no use of
  them); one it reads none of, or whose words name tables no one request
  joins, is answered with SQL a model writes from the schema the caller may
  see, run under the same guardrails, row rules and masks, and marked as such
  (`olap.unread_questions: sql`, the default); with `refuse`, or with no
  usable model, it is refused.
- **Governance on every read.** A caller's row rules and masks apply to each
  table a query names, through aliases, joins and subqueries. One audit event
  per query, never the SQL.
- **Grounding.** Joins are checked against the keys the databases declare;
  `query_metrics` joins along those keys so a request cannot write a wrong one.
- **Measures from the past.** The query history and the views are mined for
  the definitions people repeat; a reviewer certifies the ones that are right,
  or writes one by hand.
- **Saved analyses.** Questions and the SQL a reviewer certified for them;
  `find_examples` hands the closest ones to a model before it writes SQL.
- **Words a reviewer adds.** A catalog's abbreviations are written out before
  a question is matched, and its notes reach a model with the source
  (`list_sources`, the OpenAPI `x-llm-context`). Both are set in the console's
  Ontology overview or with `PATCH /catalog/{db}/guidance`, and used once the
  catalog is approved.

## Where to go

- [Deployment](deployment.md): the image, an air-gapped build, Compose, Helm,
  secrets, and what production refuses to start with.
- [Configuration](configuration.md): what stays in the file, what the console changes.
- [Architecture](architecture.md) and the [decisions](adr/README.md) behind it.
- [Running a pilot](pilot.md) with a customer's own questions.
- [API reference](reference.md).
- [Measures from the query history](query-history.md).
- `CHANGELOG.md` in the repository for what each phase added and what is not
  there yet.
