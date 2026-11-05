# Measures from the query history

`trio catalog suggest` reads what a database has been asked before and offers
the expressions its queries repeat as draft measures. A draft is read by
nothing until someone certifies it.

```bash
trio catalog suggest -d sales            # mine, name, store as drafts
trio catalog certify -d sales -e paid_total -e net_amount
trio serve                               # certified ones are measures from this start
```

`suggest` needs a catalog whose tables have terms (`trio catalog analyze` with
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
| MySQL | not read | none: MySQL keeps digests with every literal replaced | `information_schema.VIEWS` |

Without the grant `suggest` says what is missing and carries on with the
views. With neither readable it exits with an error.

Three things to know before relying on it:

- **PostgreSQL stores statements with their constants replaced by `$1`.** A
  condition such as `status = 'paid'` arrives as `status = $1` and is not
  mined. Arithmetic without a constant survives. On PostgreSQL the views are
  where the definitions with literals are.
- **SQL Server and Oracle keep the statement as it was written**, literals
  included, so conditions are mined from their history as well. What they
  keep is the plan cache and the shared pool: it reaches back to the last
  restart or flush.
- **Oracle's AWR (`DBA_HIST_SQLTEXT`) is not queried.** It reaches further
  back, and reading it needs the Diagnostics Pack licence.

All four readers are run against real servers by the integration suite
(`tests/warp/integration/test_query_history.py`): PostgreSQL with and without
the extension, MySQL, SQL Server and Oracle.
