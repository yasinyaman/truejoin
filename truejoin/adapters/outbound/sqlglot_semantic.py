"""Compiles a semantic query into DuckDB SQL.

The shape is always the same: a flat ``base`` CTE that reads the table and
aggregates, then one CTE per level of transform nesting, then a final SELECT
that orders and limits.

One CTE per level is not a stylistic choice — SQL has no way to put a window
over another window in the same SELECT, so anything nested at all needs them,
and being uniform about it keeps the generated SQL predictable. DuckDB
collapses them anyway.

Everything is generated for, parsed as and executed by DuckDB. That matters:
the compiled SQL must never be handed to the neutral-dialect analyzer, whose
round trip rewrites string literals and drops quoting, nor shipped to a source
by pushdown, where an interval literal or a ``NULLS LAST`` default would parse
on both sides and answer differently.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlglot
from sqlglot import exp

from truejoin.domain.catalog.join_graph import JoinEdge
from truejoin.domain.olap.errors import GuardrailViolation, QueryError
from truejoin.domain.olap.measures import ROW_MEASURE, Measure, check_agg_args
from truejoin.domain.olap.metric_dsl import MetricExpr, measure_ref_of, transforms_of
from truejoin.domain.olap.models import TableRef
from truejoin.domain.olap.semantic_query import FACT_ALIAS, CompiledQuery, SemanticQuery
from truejoin.domain.olap.slices import Predicate

#: The alias of what a fact row meets across a link table (see ``compile_joined``).
ACROSS_ALIAS = "across"

DIALECT = "duckdb"

_BASE_CTE = "base"


def _quote(name: str) -> str:
    """Quote an identifier for DuckDB.

    Every identifier is quoted, which is also a guardrail: the validator's
    forbidden-function check looks for ``name(``, and a quoted ``"load"`` can
    never be followed by ``(``. A column called ``read_csv`` cannot smuggle a
    call through.
    """
    return exp.to_identifier(name, quoted=True).sql(dialect=DIALECT)


def _literal(value: Any) -> str:
    """Render a Python value as a DuckDB literal, escaping included."""
    return exp.convert(value).sql(dialect=DIALECT)


def _qualified(table: str) -> str:
    """Quote ``schema.table``; only the first dot separates the two parts.

    Matches ``DuckDBStore._quote_qualified``, because this has to name the
    table the store actually created — a source becomes a DuckDB schema, and a
    slice's own name (``orders__s_ab12cd34``) is a single identifier inside it.
    """
    schema, separator, name = table.partition(".")
    if not separator:
        return _quote(table)
    return f"{_quote(schema)}.{_quote(name)}"


def _predicate_sql(predicate: Predicate, alias: str) -> str:
    column = f"{_quote(alias)}.{_quote(predicate.column)}"
    op = predicate.op
    if op == "is_null":
        return f"{column} IS {'' if predicate.value else 'NOT '}NULL"
    if op == "in":
        values = ", ".join(_literal(v) for v in predicate.value)
        return f"{column} IN ({values})" if values else "FALSE"
    symbols = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    if op == "like":
        return f"{column} LIKE {_literal(predicate.value)}"
    return f"{column} {symbols[op]} {_literal(predicate.value)}"


def _aggregate_sql(expr: MetricExpr, query: SemanticQuery) -> str:
    """The aggregate for one metric's measure, as it appears in the base CTE."""
    ref = measure_ref_of(expr)
    measure = query.model.measure(ref.measure)
    agg = measure.check_agg(ref.agg)
    if measure.column == ROW_MEASURE:
        # `*:count` counts rows; `*:count_distinct` is the same question.
        return "COUNT(*)"
    column = (
        _expression_sql(measure)
        if measure.expression
        else f"{_quote(FACT_ALIAS)}.{_quote(measure.column)}"
    )
    if agg == "COUNT_DISTINCT":
        return f"COUNT(DISTINCT {column})"
    if agg == "WEIGHTED_AVG":
        weight_column = check_agg_args(agg, ref.args, ref.measure)["weight"]
        weight = f"{_quote(FACT_ALIAS)}.{_quote(weight_column)}"
        # NULLIF so a zero total weight is NULL rather than a division error.
        return f"SUM({column} * {weight}) / NULLIF(SUM({weight}), 0)"
    return f"{agg}({column})"


def _expression_sql(measure: Measure) -> str:
    """A certified expression, with its columns bound to the fact table.

    The expression was written outside this process, so it is held to what a
    measure may be: a scalar over the columns it declared. A subquery could
    read a table nobody planned or restricted; an aggregate or a window would
    change what the DSL's own aggregation means.

    Raises:
        QueryError: When it is not that.
    """
    problem = f"The expression behind measure '{measure.name}' cannot be used"
    try:
        tree = sqlglot.parse_one(measure.expression, dialect=DIALECT)
    except Exception as e:
        raise QueryError(f"{problem}: it does not parse ({e}).") from e
    if isinstance(tree, exp.Query) or tree.find(exp.Query, exp.Table, exp.AggFunc, exp.Window):
        raise QueryError(
            f"{problem}: it must be a plain expression over the table's columns, "
            f"with no subquery, aggregate or window."
        )
    declared = {name.lower(): name for name in measure.reads}
    for column in tree.find_all(exp.Column):
        actual = declared.get(column.name.lower())
        if column.table or actual is None:
            raise QueryError(f"{problem}: '{column.sql()}' is not a column of the table.")
        column.replace(
            exp.column(
                exp.to_identifier(actual, quoted=True), exp.to_identifier(FACT_ALIAS, quoted=True)
            )
        )
    return f"({tree.sql(dialect=DIALECT)})"


def _dimension_sql(column: str, grain: str | None, alias: str = FACT_ALIAS) -> str:
    qualified = f"{_quote(alias)}.{_quote(column)}"
    if grain is None:
        return qualified
    return f"DATE_TRUNC('{grain}', {qualified})"


class SqlglotSemanticCompiler:
    """Builds DuckDB SQL for a :class:`SemanticQuery`."""

    dialect = DIALECT

    def compile(
        self, query: SemanticQuery, tables: Mapping[TableRef, str] | None = None
    ) -> CompiledQuery:
        """Generate the SQL for ``query``.

        Args:
            query: An already-validated semantic query.
            tables: Where each table lives in the store (a slice name, usually).
                Missing entries fall back to the table's own full name.

        Returns:
            The full statement, the base scan on its own, and the output columns.

        Raises:
            QueryError: When the generated SQL does not parse as DuckDB, which
                would be a bug here rather than bad input.
        """
        source = (tables or {}).get(query.model.ref, query.model.ref.full_name)
        base_sql = self._base_sql(query, source)
        return CompiledQuery(
            sql=self._layered(query, base_sql),
            base_sql=self._canonical(base_sql),
            columns=query.output_columns(),
        )

    def _layered(self, query: SemanticQuery, base_sql: str) -> str:
        """``base_sql`` as the ``base`` CTE, one CTE per level of transforms, then order and limit."""
        ctes: list[tuple[str, str]] = [(_BASE_CTE, base_sql)]
        previous = _BASE_CTE
        # Each metric's column name as it stands after the levels applied so far.
        current: list[str] = [measure_ref_of(m).output_name() for m in query.metrics]
        chains = [transforms_of(metric) for metric in query.metrics]

        for level in range(max((len(chain) for chain in chains), default=0)):
            name = f"w_{level}"
            ctes.append((name, self._window_sql(query, chains, current, level, previous)))
            previous = name

        select = ", ".join(_quote(column) for column in query.output_columns())
        order_column, descending = query.order_column()
        tail = (
            f"SELECT {select} FROM {_quote(previous)} "
            f"ORDER BY {_quote(order_column)} {'DESC' if descending else 'ASC'} "
            f"LIMIT {int(query.limit)}"
        )
        with_clause = ", ".join(f"{_quote(name)} AS ({body})" for name, body in ctes)
        return self._canonical(f"WITH {with_clause} {tail}")

    def compile_joined(self, query: SemanticQuery) -> str:
        """One SELECT over the fact table and the tables it reaches.

        Inner joins along the keys in ``query.joins``, so a fact row whose key
        is empty or dangling is left out, as in the join a person would write.

        A path that goes down to a link table (``JoinGraph.across``) meets
        several rows per fact row. That side is read as one set of distinct
        ``(fact key, its columns)``, so each fact row counts once per group it
        falls in (a student in two courses of one department, once for the
        department), and once in all when only a condition is on that side.

        Raises:
            QueryError: When a step starts from a table no earlier step
                reached, which would be a bug in whoever built the query.
        """

        def key(ref: TableRef) -> tuple[str, str]:
            return ref.source.lower(), ref.table.lower()

        fact = key(query.model.ref)
        down = next(
            (e for e in query.joins if key(e.parent) == fact and key(e.child) != fact), None
        )
        far: set[tuple[str, str]] = set()
        far_edges: list[JoinEdge] = []
        if down is not None:
            far.add(key(down.child))
            pending = [e for e in query.joins if e is not down]
            grew = True
            while grew:
                grew = False
                for edge in list(pending):
                    if key(edge.child) in far:
                        far.add(key(edge.parent))
                        far_edges.append(edge)
                        pending.remove(edge)
                        grew = True
        near_edges = [e for e in query.joins if e is not down and e not in far_edges]

        def chain(
            start: dict[tuple[str, str], str], edges: list[JoinEdge], prefix: str
        ) -> list[str]:
            joins = []
            for index, edge in enumerate(edges, start=1):
                child = start.get(key(edge.child))
                if child is None:
                    raise QueryError(
                        f"Join step '{edge}' starts from a table the query does not read."
                    )
                alias = start.setdefault(key(edge.parent), f"{prefix}{index}")
                joins.append(
                    f"JOIN {_qualified(edge.parent.full_name)} AS {_quote(alias)} ON "
                    f"{_quote(child)}.{_quote(edge.column)} = {_quote(alias)}.{_quote(edge.ref_column)}"
                )
            return joins

        aliases = {fact: FACT_ALIAS}
        joins = chain(aliases, near_edges, "j")

        def on_far(table: TableRef | None) -> bool:
            return table is not None and key(table) in far

        projections = []
        if down is not None:
            link = {key(down.child): "l0"}
            inner = chain(link, far_edges, "l")
            picked = [
                f"{_dimension_sql(dim.column, dim.grain, link[key(dim.table)])} AS {_quote(f'd{i}')}"
                for i, dim in enumerate(query.dimensions)
                if dim.table is not None and on_far(dim.table)
            ]
            wheres = [
                _predicate_sql(p, link[key(table)])
                for table, p in query.joined_filters
                if on_far(table)
            ]
            subquery = (
                f"SELECT DISTINCT {_quote('l0')}.{_quote(down.column)} AS {_quote('k')}"
                + "".join(f", {column}" for column in picked)
                + f" FROM {_qualified(down.child.full_name)} AS {_quote('l0')} "
                + " ".join(inner)
                + (" WHERE " + " AND ".join(wheres) if wheres else "")
            )
            joins.append(
                f"JOIN ({subquery}) AS {_quote(ACROSS_ALIAS)} ON "
                f"{_quote(FACT_ALIAS)}.{_quote(down.ref_column)} = {_quote(ACROSS_ALIAS)}.{_quote('k')}"
            )

        for i, dim in enumerate(query.dimensions):
            if on_far(dim.table):
                expression = f"{_quote(ACROSS_ALIAS)}.{_quote(f'd{i}')}"
            else:
                expression = _dimension_sql(
                    dim.column,
                    dim.grain,
                    FACT_ALIAS if dim.table is None else aliases[key(dim.table)],
                )
            projections.append(f"{expression} AS {_quote(dim.output_name())}")
        projections += [
            f"{_aggregate_sql(metric, query)} AS {_quote(measure_ref_of(metric).output_name())}"
            for metric in query.metrics
        ]
        conditions = [_predicate_sql(p, FACT_ALIAS) for p in query.filters]
        conditions += [
            _predicate_sql(p, aliases[key(table)])
            for table, p in query.joined_filters
            if not on_far(table)
        ]
        order_column, descending = query.order_column()
        sql = (
            f"SELECT {', '.join(projections)} "
            f"FROM {_qualified(query.model.ref.full_name)} AS {_quote(FACT_ALIAS)} "
            + " ".join(joins)
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        if query.dimensions:
            sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(query.dimensions)))
        if query.is_windowed:
            # The joined, grouped rows are the base the transforms are layered on.
            return self._layered(query, sql)
        sql += (
            f" ORDER BY {_quote(order_column)} {'DESC' if descending else 'ASC'}"
            f" LIMIT {int(query.limit)}"
        )
        return self._canonical(sql)

    # -- the pieces ---------------------------------------------------------

    def _base_sql(self, query: SemanticQuery, source: str) -> str:
        """The flat scan: read the table, filter, group, aggregate.

        This is the part the SQL analyzer can read back, which is what the
        contract test uses to check the shape the query claimed.
        """
        projections = [
            f"{_dimension_sql(dim.column, dim.grain)} AS {_quote(dim.output_name())}"
            for dim in query.dimensions
        ]
        projections += [
            f"{_aggregate_sql(metric, query)} AS {_quote(measure_ref_of(metric).output_name())}"
            for metric in query.metrics
        ]
        sql = f"SELECT {', '.join(projections)} FROM {_qualified(source)} AS {_quote(FACT_ALIAS)}"
        if query.filters:
            conditions = " AND ".join(_predicate_sql(p, FACT_ALIAS) for p in query.filters)
            sql += f" WHERE {conditions}"
        if query.dimensions:
            # Group by position: the dimension expression may be a DATE_TRUNC,
            # and repeating it would be the only other option.
            sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(query.dimensions)))
        return sql

    def _window_sql(
        self,
        query: SemanticQuery,
        chains: Sequence[Sequence[Any]],
        current: list[str],
        level: int,
        previous: str,
    ) -> str:
        """One nesting level: apply each metric's transform, carry the rest through."""
        projections = [_quote(dim.output_name()) for dim in query.dimensions]
        for index, chain in enumerate(chains):
            if level >= len(chain):
                # This metric has no transform left; keep its column as it is.
                projections.append(_quote(current[index]))
                continue
            call = chain[level]
            inner = current[index]
            produced = f"{call.name}_{inner}"
            projections.append(f"{self._transform_sql(call, inner, query)} AS {_quote(produced)}")
            current[index] = produced
        return f"SELECT {', '.join(projections)} FROM {_quote(previous)}"

    def _transform_sql(self, call: Any, column: str, query: SemanticQuery) -> str:
        """The windowed expression for one transform over ``column``."""
        args = call.spec.bind(call.args)
        value = _quote(column)
        order = self._order_expression(query, column)

        if call.name == "cumsum":
            frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
            return f"SUM({value}) OVER (ORDER BY {order} {frame})"
        if call.name in ("lag", "lead"):
            return f"{call.name.upper()}({value}, {args['n']}) OVER (ORDER BY {order})"
        if call.name == "change":
            return f"{value} - LAG({value}, {args['n']}) OVER (ORDER BY {order})"
        if call.name == "change_pct":
            lag = f"LAG({value}, {args['n']}) OVER (ORDER BY {order})"
            # NULLIF so a zero base reads as "no answer" rather than raising.
            return f"({value} - {lag}) / NULLIF({lag}, 0)"
        if call.name == "time_shift":
            time_dimension = query.time_dimension()
            grain = time_dimension.grain if time_dimension else None
            if time_dimension is None or grain is None:  # pragma: no cover - validate() ran first
                raise QueryError("time_shift needs a time dimension with a grain.")
            periods = args["periods"]
            frame = (
                f"RANGE BETWEEN INTERVAL {periods} {grain.upper()} PRECEDING "
                f"AND INTERVAL {periods} {grain.upper()} PRECEDING"
            )
            # LAST_VALUE over a one-period-wide frame: the value of *that*
            # calendar period, and NULL when the period has no row. LAG would
            # answer with whatever the previous row happens to be.
            return (
                f"LAST_VALUE({value}) OVER "
                f"(ORDER BY {_quote(time_dimension.output_name())} {frame})"
            )
        if call.name in ("rank", "dense_rank", "percent_rank"):
            return f"{call.name.upper()}() OVER (ORDER BY {value} DESC)"
        if call.name == "ntile":
            return f"NTILE({args['buckets']}) OVER (ORDER BY {value} DESC)"
        raise QueryError(f"Transform '{call.name}' has no compiler.")  # pragma: no cover

    @staticmethod
    def _order_expression(query: SemanticQuery, fallback_column: str) -> str:
        """What a sequence window orders by.

        The time dimension when there is one, else the first dimension, else
        the value itself — which only happens when the query has no grouping
        at all and therefore exactly one row.
        """
        time_dimension = query.time_dimension()
        if time_dimension is not None:
            return _quote(time_dimension.output_name())
        if query.dimensions:
            return _quote(query.dimensions[0].output_name())
        return _quote(fallback_column)

    @staticmethod
    def _canonical(sql: str) -> str:
        """Parse as DuckDB and regenerate, proving the SQL is well formed.

        Parsing here rather than trusting the string is what turns a mistake in
        this module into an error at compile time instead of a DuckDB failure
        with no context.
        """
        try:
            parsed = sqlglot.parse_one(sql, dialect=DIALECT)
        except Exception as e:
            raise QueryError(f"Generated SQL did not parse: {e}") from e
        if parsed is None:  # pragma: no cover - parse_one raises instead
            raise GuardrailViolation("Generated SQL was empty")
        return parsed.sql(dialect=DIALECT)
