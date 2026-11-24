# 12. Row rules and masks on analytical queries, by rewriting the query

- Status: Accepted
- Date: 2026-11-05

## Context

ADR 9 put row rules and column masks on the table routes, where each request
reads one table and the gateway builds the SQL. An analytical query is the
caller's own SQL over data that is already loaded into DuckDB, whole rows and
raw values, for every caller at once. Applying the rules to the result is too
late: an aggregate over rows the caller may not see, or a masked column read
through an alias, a join or a subquery, has already leaked.

## Decision

Every analytical read takes an `AccessScope`: the caller's row policy, the
masks that apply to their roles, and who they are. For a restricted scope,
each table the query names is replaced by a subquery that carries that
caller's row conditions and masks (`SqlglotAccessRewriter`). Aliases, joins,
CTEs, aggregates and subqueries then all read the restricted view, because
there is nothing else to read.

Around that:

- **It fails closed.** What cannot be wrapped (a materialized view, a slice
  named directly, a table function, anything that is not a plain source
  table) refuses the query.
- **Row conditions reach the planner**, so a tenant's query on a table too
  big to load fetches that tenant's slice. A condition or join key on a
  masked column never narrows a fetch: the query compares the masked value,
  and fetching by the raw one would let a caller test guesses by counting.
- **No pushdown and no shared cache.** The masks are written for the store;
  the cache key is the rewritten SQL, so two tenants cannot share an entry.
- **Masks come from the approved catalog only**, and produce what the table
  routes produce, the keyed hash included. The result names the columns that
  show a masked value (`masked`), traced from the caller's own SQL with
  sqlglot's lineage (an alias or a CTE keeps the mark; an aggregate drops it).
  It is a label for the reader: the rewrite does the masking, and the label
  narrows no fetch.
- **Grounding runs before the rewrite**, so a table a repair adds is
  restricted like the rest.
- **One audit event per read**: who, which tables, how many rows, whether row
  rules applied, which columns were masked. Never the SQL.

`query_metrics` and `answer_question` reach the same path. A metric over a
masked column is refused rather than computed over masked values.

## Consequences

- A restricted caller's queries always run locally, and a query that is not
  a flat SELECT loads whole tables for them.
- A new path that returns rows must take the scope or refuse a restricted
  one; there is no default that leaks.
- The gateway's raw `/query/execute` is outside this: it runs on the source
  database. It refuses callers with row rules or masks, and production
  refuses to start with it enabled.
- This is the one component with no precedent in the three origins and it
  carries the privacy promise. Its tests were written first.
