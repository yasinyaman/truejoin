"""Finding the reusable expressions in a statement (``ExpressionMiner``, on sqlglot).

Three things are taken from every SELECT of a statement:

- what sits inside an aggregate, when it is more than a column:
  ``SUM(CASE WHEN status = 'paid' THEN total END)``, ``AVG(price * quantity)``;
- arithmetic it selects, ``total - discount AS net``, which is what a view
  usually exists to define. Only arithmetic: a selected CASE or function may
  well be text, and a measure is something to add up;
- each condition of its WHERE, as a 1/0 flag: ``status = 'paid'`` becomes
  ``CASE WHEN status = 'paid' THEN 1 ELSE 0 END``, which a sum counts.

Each is kept only if all of its columns belong to one table of the catalog
and it holds no parameter, subquery, aggregate or window: that is exactly
what a certified measure expression may be, so nothing mined here is refused
later for its shape.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

import duckdb
import sqlglot
from sqlglot import exp

from truejoin.domain.catalog.history import AGGREGATE, CONDITION, PROJECTION, MinedExpression
from truejoin.domain.catalog.terms import MeasureError

DIALECTS = {"postgresql": "postgres", "mysql": "mysql", "mssql": "tsql", "oracle": "oracle"}

_PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Oracle's positional binds (``:1``), which sqlglot does not read. Renamed so
#: the statement parses; a bind is refused as a parameter either way.
_POSITIONAL_BIND = re.compile(r"(?<![:\w]):(\d+)\b")

#: The conditions worth offering: one comparison, nothing compound.
_COMPARISONS = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
    exp.Like,
    exp.ILike,
    exp.Is,
    exp.Between,
)

_ARITHMETIC = (exp.Add, exp.Sub, exp.Mul, exp.Div)

#: What a measure expression may not contain.
_FORBIDDEN = (exp.Query, exp.Subquery, exp.AggFunc, exp.Window, exp.Placeholder, exp.Parameter)

#: A column's ``type_kind`` as DuckDB, which runs the measures, would hold it.
_DUCKDB_TYPES = {
    "int": "BIGINT",
    "float": "DOUBLE",
    "bool": "BOOLEAN",
    "datetime": "TIMESTAMP",
    "date": "DATE",
    "time": "TIME",
    "uuid": "UUID",
    "bytes": "BLOB",
}


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _readable(error: Exception) -> str:
    """An engine's message for a person: one line, no colour codes, no candidate list."""
    text = " ".join(_ANSI.sub("", str(error)).split())
    return text.split(" Candidate functions:")[0][:240]


class SqlglotExpressionMiner:
    """Mines aggregate arguments and WHERE conditions; checks one a reviewer wrote."""

    def check(self, expression: str, columns: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
        """See ``ExpressionChecker.check``.

        The shape is held to what a mined expression may be, then the
        expression is summed over an empty table of those columns in an
        in-memory DuckDB: that is where measures run, so a type it refuses
        (text times a number, a sum of text) is caught here rather than on
        the first question that uses the measure.
        """
        try:
            tree = sqlglot.parse_one(expression, dialect="duckdb")
        except Exception as e:
            raise MeasureError(f"It does not parse: {_readable(e)}") from e
        if isinstance(tree, _FORBIDDEN) or tree.find(*_FORBIDDEN, exp.Table):
            raise MeasureError(
                "It must be a plain expression over the table's columns, with no subquery, "
                "aggregate, window or parameter: write what goes inside the aggregate, "
                "price * quantity rather than SUM(price * quantity)."
            )
        known = {name.lower(): name for name in columns}
        used: dict[str, None] = {}
        for column in list(tree.find_all(exp.Column)):
            if column.table:
                raise MeasureError(f"Write '{column.name}' without a table: '{column.sql()}'.")
            actual = known.get(column.name.lower())
            if actual is None:
                raise MeasureError(f"'{column.name}' is not a column of the table.")
            if not _PLAIN_NAME.match(actual):
                raise MeasureError(f"Column '{actual}' has a name a measure cannot use.")
            used[actual] = None
            column.replace(exp.column(actual))
        if not used:
            raise MeasureError("It reads no column of the table.")
        sql = tree.sql(dialect="duckdb")
        reads = tuple(used)
        definition = ", ".join(
            f'"{name}" {_DUCKDB_TYPES.get(columns[name], "VARCHAR")}' for name in reads
        )
        connection = duckdb.connect(":memory:", config={"enable_external_access": False})
        try:
            connection.execute(f"CREATE TABLE measured ({definition})")
            connection.execute(f"SELECT SUM({sql}) FROM measured").fetchall()
        except duckdb.Error as e:
            raise MeasureError(f"It cannot be added up as written: {_readable(e)}") from e
        finally:
            connection.close()
        return sql, reads

    def mine(
        self, sql: str, dialect: str, columns: Mapping[str, Collection[str]]
    ) -> list[MinedExpression]:
        """See ``ExpressionMiner.mine``."""
        if dialect == "oracle":
            sql = _POSITIONAL_BIND.sub(r":b\1", sql)
        try:
            statements = sqlglot.parse(sql, dialect=DIALECTS.get(dialect))
        except Exception:
            return []
        known = {
            table.lower(): (table, {name.lower(): name for name in names})
            for table, names in columns.items()
        }
        found: dict[tuple[str, str], MinedExpression] = {}
        for statement in statements:
            if statement is None:
                continue
            for select in statement.find_all(exp.Select):
                for mined in self._from_select(select, known):
                    found.setdefault((mined.table, mined.expression), mined)
        return list(found.values())

    def _from_select(
        self, select: exp.Select, known: Mapping[str, tuple[str, dict[str, str]]]
    ) -> list[MinedExpression]:
        # The catalog tables this SELECT reads directly, by the name it uses for each.
        scope: dict[str, str] = {}
        sources = [select.args.get("from_"), *(select.args.get("joins") or [])]
        for source in sources:
            table = source.this if source is not None else None
            if isinstance(table, exp.Table) and table.name.lower() in known:
                scope[table.alias_or_name.lower()] = table.name.lower()
        if not scope:
            return []

        candidates: list[tuple[exp.Expr, str]] = []
        for aggregate in select.find_all(exp.AggFunc):
            argument = aggregate.this
            if aggregate.parent_select is not select or argument is None:
                continue
            if isinstance(argument, exp.Column | exp.Star | exp.Distinct | exp.Literal):
                continue
            candidates.append((argument, AGGREGATE))
        for projection in select.expressions:
            selected = projection.unalias()
            while isinstance(selected, exp.Paren):
                selected = selected.this
            if isinstance(selected, _ARITHMETIC):
                candidates.append((selected, PROJECTION))
        where = select.args.get("where")
        if where is not None:
            for condition in (
                where.this.flatten() if isinstance(where.this, exp.And) else [where.this]
            ):
                # Engines store a condition wrapped in parentheses of their own.
                while isinstance(condition, exp.Paren):
                    condition = condition.this
                inner = condition
                while isinstance(inner, exp.Not | exp.Paren):
                    inner = inner.this
                if isinstance(inner, _COMPARISONS):
                    candidates.append((condition, CONDITION))

        mined = []
        for node, kind in candidates:
            bound = self._bind(node, scope, known)
            if bound is None:
                continue
            table, expression, used = bound
            if kind == CONDITION:
                expression = f"CASE WHEN {expression} THEN 1 ELSE 0 END"
            mined.append(MinedExpression(table, expression, kind, used))
        return mined

    @staticmethod
    def _bind(
        node: exp.Expr,
        scope: Mapping[str, str],
        known: Mapping[str, tuple[str, dict[str, str]]],
    ) -> tuple[str, str, tuple[str, ...]] | None:
        """``node`` as DuckDB SQL over one table's own column names, or None."""
        if isinstance(node, _FORBIDDEN) or node.find(*_FORBIDDEN):
            return None
        bound = node.copy()
        owners: set[str] = set()
        used: dict[str, None] = {}
        for column in list(bound.find_all(exp.Column)):
            name = column.name.lower()
            if column.table:
                tables = [scope[column.table.lower()]] if column.table.lower() in scope else []
            else:
                tables = [t for t in dict.fromkeys(scope.values()) if name in known[t][1]]
            if len(tables) != 1 or name not in known[tables[0]][1]:
                return None
            actual = known[tables[0]][1][name]
            if not _PLAIN_NAME.match(actual):
                return None
            owners.add(tables[0])
            used[actual] = None
            column.replace(exp.column(actual))
        if len(owners) != 1:
            return None
        return known[owners.pop()][0], bound.sql(dialect="duckdb"), tuple(used)
