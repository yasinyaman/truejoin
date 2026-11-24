"""sqlglot-backed AccessRewriter: every table a query reads becomes that caller's view of it.

``FROM db.orders o`` turns into
``FROM (SELECT "id", <mask> AS "email" FROM "db"."orders" WHERE "tenant" = 'a') AS o``.
Wrapping the table, rather than patching the columns that mention it, is what
makes this hold under aliases, aggregates, joins, windows and subqueries alike:
nothing outside the parentheses can reach a row or a value the view left out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.scope import traverse_scope

from truejoin.domain.governance.access import PSEUDONYM_FUNCTION, TableView
from truejoin.domain.governance.masking import REDACTED
from truejoin.domain.olap.errors import GuardrailViolation
from truejoin.domain.olap.models import TableRef

_DIALECT = "duckdb"

#: What a table reference may carry and still be replaced by a subquery.
_PLAIN_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})

#: Functions that read a table named by a string, which no wrapper would see.
_TABLE_READING_FUNCTIONS = frozenset({"query", "query_table"})

_COMPARISONS: dict[str, type[exp.Binary]] = {
    "eq": exp.EQ,
    "ne": exp.NEQ,
    "gt": exp.GT,
    "gte": exp.GTE,
    "lt": exp.LT,
    "lte": exp.LTE,
    "like": exp.Like,
}

# Each mirrors ``masking.mask_value``; {c} is the quoted column, {t} its text form.
_MASKS: dict[str, str] = {
    "null": "NULL",
    "redact": f"CASE WHEN {{c}} IS NULL THEN NULL ELSE '{REDACTED}' END",
    "last4": (
        f"CASE WHEN {{c}} IS NULL THEN NULL "
        f"WHEN LENGTH({{t}}) > 4 THEN '{REDACTED}' || RIGHT({{t}}, 4) ELSE '{REDACTED}' END"
    ),
    "partial": (
        f"CASE WHEN {{c}} IS NULL THEN NULL "
        f"WHEN STRPOS({{t}}, '@') > 0 THEN "
        f"(CASE WHEN STRPOS({{t}}, '@') > 1 THEN LEFT({{t}}, 1) ELSE '' END) "
        f"|| '{REDACTED}@' || SUBSTR({{t}}, STRPOS({{t}}, '@') + 1) "
        f"WHEN LENGTH({{t}}) <= 2 THEN '{REDACTED}' "
        f"ELSE LEFT({{t}}, 1) || '{REDACTED}' || RIGHT({{t}}, 1) END"
    ),
    "hash": f"{PSEUDONYM_FUNCTION}({{t}})",
}


def _refuse(reason: str) -> GuardrailViolation:
    return GuardrailViolation(
        f"Guardrail: {reason}. Row rules or column masks apply to this caller, "
        f"so a query may read source tables only, each as a plain table reference."
    )


class SqlglotAccessRewriter:
    """Replaces table references with per-caller views, or refuses."""

    def masked_outputs(
        self, sql: str, outputs: Sequence[str], tables: Mapping[TableRef, TableView]
    ) -> frozenset[str]:
        masked = {
            (ref.table.lower(), column.lower())
            for ref, view in tables.items()
            for column in view.masks
        }
        if not masked:
            return frozenset()
        schema: dict[str, dict[str, dict[str, str]]] = {}
        for ref, view in tables.items():
            schema.setdefault(ref.source, {})[ref.table] = {
                column: "TEXT" for column in view.columns
            }
        found: set[str] = set()
        for output in outputs:
            try:
                node = lineage(output, sql, schema=schema, dialect=_DIALECT)
            except Exception:  # noqa: BLE001 - a label only: fall back to the name
                if any(column == output.lower() for _, column in masked):
                    found.add(output)
                continue
            if any(leaf in masked for leaf in _plain_leaves(node)):
                found.add(output)
        return frozenset(found)

    def secure(self, sql: str, views: Mapping[TableRef, TableView]) -> str:
        try:
            parsed = sqlglot.parse_one(sql, read=_DIALECT)
            scopes = traverse_scope(parsed) if isinstance(parsed, exp.Query) else []
        except SqlglotError as e:
            raise _refuse(f"the query could not be analysed ({e})") from e
        if not isinstance(parsed, exp.Query):
            raise _refuse("only SELECT queries can be restricted")

        _check_from_items(parsed)

        # Scope analysis, not a list of CTE names: a CTE called `orders` in one
        # subquery must not excuse a read of the real `orders` in another. Keyed
        # on the table's own name, never its alias: `scope.sources` is keyed by
        # alias, so `db.orders AS t, (SELECT 1) AS t` would otherwise pass the
        # real table off as the derived one.
        cte_references: set[int] = set()
        tables: list[exp.Table] = []
        for scope in scopes:
            ctes = {name.casefold() for name in scope.cte_sources}
            for table in scope.tables:
                if not table.db and not table.catalog and table.name.casefold() in ctes:
                    cte_references.add(id(table))
                else:
                    tables.append(table)
        seen = cte_references | {id(table) for table in tables}
        for table in parsed.find_all(exp.Table):
            if id(table) not in seen:
                raise _refuse(f"'{table.sql(_DIALECT)}' is read in a way that cannot be restricted")

        for table in tables:
            table.replace(_view_of(table, views))
        return parsed.sql(dialect=_DIALECT)


def _check_from_items(parsed: exp.Expr) -> None:
    """Refuse anything in a FROM clause that could read a table behind our back."""
    for node in parsed.find_all(exp.From, exp.Join):
        item = node.this
        if isinstance(item, exp.Lateral):
            item = item.this
        if not isinstance(item, exp.Table | exp.Subquery | exp.Unnest | exp.Values):
            raise _refuse(f"'{node.this.sql(_DIALECT)}' is not a table or a subquery")
    for with_ in parsed.find_all(exp.With):
        # A recursive CTE's anchor may name the CTE itself and still be bound,
        # by DuckDB, to the stored table of that name.
        if with_.args.get("recursive"):
            raise _refuse("recursive CTEs cannot be restricted")
    for call in parsed.find_all(exp.Anonymous):
        if call.name.lower() in _TABLE_READING_FUNCTIONS:
            raise _refuse(f"{call.this}() reads a table by name")
        # The keyed hash behind the `hash` mask: callable, it would hash guesses.
        if call.name.lower() == PSEUDONYM_FUNCTION:
            raise _refuse(f"{call.this}() is reserved for masking")


def _view_of(table: exp.Table, views: Mapping[TableRef, TableView]) -> exp.Subquery:
    if not isinstance(table.this, exp.Identifier) or table.args.get("catalog"):
        raise _refuse(f"'{table.sql(_DIALECT)}' is not a source table")
    extras = sorted(k for k, v in table.args.items() if v and k not in _PLAIN_TABLE_ARGS)
    if extras:
        raise _refuse(f"'{table.sql(_DIALECT)}' carries {', '.join(extras)}")
    view = views.get(TableRef(table.db or "", table.name))
    if view is None:
        raise _refuse(f"'{table.sql(_DIALECT)}' is not a source table this caller may read")
    if not view.columns:
        raise _refuse(f"the columns of '{table.name}' are not known")

    schema, _, name = view.name.rpartition(".")
    select = exp.select(*(_projection(column, view) for column in view.columns)).from_(
        exp.table_(name, db=schema or None, quoted=True)
    )
    if view.conditions:
        select = select.where(exp.and_(*(_condition(*c) for c in view.conditions)))
    # The original name stays usable as a qualifier (``orders.id``), and an
    # alias with a column list keeps it.
    alias = table.args.get("alias") or exp.TableAlias(this=exp.to_identifier(table.name))
    return exp.Subquery(this=select, alias=alias.copy())


def _projection(column: str, view: TableView) -> exp.Expr:
    quoted = exp.column(column, quoted=True)
    strategy = view.masks.get(column)
    if strategy is None:
        return quoted
    template = _MASKS.get(strategy)
    if template is None:
        raise _refuse(f"unknown masking strategy '{strategy}' on '{column}'")
    c = quoted.sql(_DIALECT)
    masked = sqlglot.parse_one(template.format(c=c, t=f"CAST({c} AS TEXT)"), read=_DIALECT)
    return exp.alias_(masked, column, quoted=True)


def _condition(column: str, operator: str, value: Any) -> exp.Expr:
    """One row rule as SQL, with the value as a literal (never interpolated text)."""
    target = exp.column(column, quoted=True)
    if operator == "is_null":
        null = exp.Is(this=target, expression=exp.Null())
        return null if value else exp.Not(this=null)
    if operator == "in":
        if not value:
            return exp.false()
        return exp.In(this=target, expressions=[exp.convert(v) for v in value])
    comparison = _COMPARISONS.get(operator)
    if comparison is None:
        raise _refuse(f"unknown row-rule operator '{operator}' on '{column}'")
    return comparison(this=target, expression=exp.convert(value))


def _plain_leaves(node: Node) -> list[tuple[str, str]]:
    """The ``(table, column)`` an output's value comes from, where no aggregate sits between."""
    leaves: list[tuple[str, str]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current.expression, exp.Table) and current.expression.find(exp.AggFunc):
            continue  # a count or a sum of masked values shows none of them
        if not current.downstream:
            source = current.source
            if isinstance(source, exp.Table):
                leaves.append((source.name.lower(), current.name.split(".")[-1].lower()))
        stack.extend(current.downstream)
    return leaves
