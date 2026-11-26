---
description: "Mining a database's query history and views for the measures people repeat, offered as draft measures to certify."
---

# Measures from the query history

`truejoin catalog suggest` reads what a database has been asked before and offers
the expressions its queries repeat as draft measures. A draft is read by
nothing until someone certifies it.

```bash
truejoin catalog suggest -d sales            # mine, name, store as drafts
truejoin catalog certify -d sales -e paid_total -e net_amount
```

A certified expression is a measure at once: a running server picks the change
up within 30 seconds.

`suggest` needs a catalog whose tables have terms (`truejoin catalog analyze` with
`olap.terms` on). It reads two things and uses whichever it may:

- the **statement history**: the most-run SELECT statements;
- the **view definitions** of the configured schema.

From each statement it takes what sits inside an aggregate
(`SUM(CASE WHEN status = 'paid' THEN total END)`), arithmetic that is selected
(`total - discount AS net`) and each condition of a WHERE, as a 1/0 flag to be
summed. An expression is kept only when all of its columns belong to one table
of the catalog and it holds no parameter, subquery, aggregate or window. A
condition or arithmetic on a key or on a date is dropped: `id = 7` and
`created_at >= '2024-01-01'` say which rows one query wanted, not what
something is. What a view defines is ranked first; the rest by how often it
ran. The model then names the candidates and may drop some. It never writes or
changes an expression.

## What each engine needs

| Engine | Statement history | Grant | Views |
|---|---|---|---|
| PostgreSQL | `pg_stat_statements` | the extension in `shared_preload_libraries`, `CREATE EXTENSION pg_stat_statements`, and `GRANT pg_read_all_stats` to see other users' statements | `information_schema.views` (views the user owns or is a member of the owner of) |
| SQL Server | `sys.dm_exec_query_stats` | `GRANT VIEW SERVER STATE` (`VIEW DATABASE STATE` on Azure SQL) | `sys.sql_modules`, with `VIEW DEFINITION` |
| Oracle | `V$SQL` | `GRANT SELECT ON V_$SQL TO <user>`, given by `SYS` in the pluggable database (`SYSTEM` cannot) | `ALL_VIEWS` |
| MySQL / MariaDB | `performance_schema.events_statements_summary_by_digest` | `GRANT SELECT ON performance_schema.* TO <user>`; on MariaDB also `performance_schema=ON` (off by default) | `information_schema.VIEWS` |

Without the grant `suggest` says what is missing and carries on with the
views. With neither readable it exits with an error.

Four things to know before relying on it:

- **PostgreSQL stores statements with their constants replaced by `$1`.** A
  condition such as `status = 'paid'` arrives as `status = $1` and is not
  mined. Arithmetic without a constant survives. On PostgreSQL the views are
  where the definitions with literals are.
- **MySQL 8 keeps one real statement per digest** (`QUERY_SAMPLE_TEXT`),
  literals included, cut at `performance_schema_max_sql_text_length` (1024
  by default; a cut statement does not parse and is skipped). MariaDB has
  only the digest, constants replaced by `?` as on PostgreSQL. Either way it
  reaches back to the server's last restart.
- **SQL Server and Oracle keep the statement as it was written**, literals
  included, so conditions are mined from their history as well. What they
  keep is the plan cache and the shared pool: it reaches back to the last
  restart or flush.
- **Oracle's AWR (`DBA_HIST_SQLTEXT`) is not queried.** It reaches further
  back, and reading it needs the Diagnostics Pack licence.

All the readers are run against real servers by the integration suite
(`tests/warp/integration/test_query_history.py`): PostgreSQL with and without
the extension, MySQL, MariaDB, SQL Server and Oracle.

## Writing a measure by hand

A reviewer can write one too, on a term in the console's catalog review (*Add a
measure*) or through the API:

```bash
curl -X POST "$TRUEJOIN/api/v1/catalog/sales/terms/order/measures/check" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table": "orders", "expression": "total - discount"}'
curl -X POST "$TRUEJOIN/api/v1/catalog/sales/terms/order/measures" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"table": "orders", "name": "net_amount", "expression": "total - discount",
       "description": "What the customer paid"}'
```

The expression is what goes inside the aggregate, written in DuckDB SQL over the
table's columns: `total - discount`, not `SUM(total - discount)`. It is held to
the rules a mined expression follows (one table of the term, no subquery,
aggregate, window or parameter), then aggregated over an empty table of those
columns in an in-memory DuckDB, where measures run: text multiplied by a
number, or a sum of text, is refused there with the engine's reason rather than
on the first question that uses it. `check` stores nothing.

`aggregation` says how it is aggregated: `sum` (the default, and what every
mined expression is), `avg`, `min`, `max`, `count`, `count_distinct` or
`median`; the trial runs that one, so a lowest or a distinct count of text
passes and an average of text does not. A measure certified as anything but a
sum answers only as itself: `basket_value:sum` on an average is refused, and a
question that says "total" of it is read as the average, with "total" listed
among the words it did not use. A 1/0 flag is counted by its sum; `count` of
one is refused, since it would count every row. The sum and the average of one
expression are two measures, each certified on its own; changing a measure's
aggregation is removing it and adding the new one, which is a draft again.

What is added is a draft, exactly like a suggestion, and becomes a measure when
it is certified. `DELETE .../measures/{name}` removes a draft or a measure; a
certified one stops being offered at once. Both are audited by name, never by
their SQL, which may hold literals.
