"""Random SELECTs over two small tables, and what a restricted caller should get from each.

The property the fuzz tests hold: for a caller with row rules and masks, a
query run through the rewriter on the real rows answers exactly what the same
query answers on a copy of the tables that holds only the caller's rows, with
the masks already applied (``expected_tables``, built in Python with the
domain's own ``mask_value``, not with the rewriter's SQL). Or the query is
refused. Nothing in between: no extra row, no raw value.

``queries()`` writes the SQL. It tracks each relation's columns and their
types, so what it writes runs, and it reaches for what a query can hide a
table behind: aliases, derived tables, CTEs (one built on another), joins of
every kind, LATERAL, set operations, grouping, windows, scalar, IN and EXISTS
subqueries, correlated ones included.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st

from truejoin.domain.governance.masking import mask_value

HASH_KEY = b"fuzz-secret-key-of-thirty-two-bytes!"

#: Set to a number for a long, random run: ``TRUEJOIN_FUZZ_EXAMPLES=5000 pytest -k fuzz``.
DEEP = os.environ.get("TRUEJOIN_FUZZ_EXAMPLES", "")


def fuzz_settings(examples: int) -> settings:
    """A few fixed examples in an ordinary run; as many random ones as asked in a deep one."""
    return settings(
        max_examples=int(DEEP) if DEEP else examples,
        derandomize=not DEEP,
        database=None,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )


#: name -> [(column, type)], type "int" or "text".
SCHEMA: dict[str, list[tuple[str, str]]] = {
    "customers": [("id", "int"), ("tenant", "text"), ("email", "text"), ("amount", "int")],
    "notes": [("id", "int"), ("customer_id", "int"), ("tenant", "text"), ("body", "text")],
}

ROWS: dict[str, list[tuple[Any, ...]]] = {
    "customers": [
        (1, "a", "alice@x.com", 10),
        (2, "b", "bob@y.org", 20),
        (3, "a", "cara@x.com", 5),
        (4, "c", "dan@z.net", 7),
        (5, None, "eve@x.com", 3),
        (6, "a", None, 12),
        (7, "b", "al", 10),
    ],
    "notes": [
        (1, 1, "a", "hello"),
        (2, 2, "b", "secret of b"),
        (3, 3, "a", None),
        (4, 4, "c", "about dan"),
        (5, 1, "b", "b writes about alice"),
        (6, 9, "a", "orphan"),
        (7, 6, None, "no tenant"),
    ],
}

#: Every raw text value a mask could hide, for the "nothing raw comes out" check.
RAW_TEXT = {
    row[i]
    for table, rows in ROWS.items()
    for row in rows
    for i, (column, kind) in enumerate(SCHEMA[table])
    if kind == "text" and column in ("email", "body") and row[i] is not None
}


@dataclass(frozen=True)
class Scope:
    """One caller: the same row rules on both tables (by column), and masks per column."""

    name: str
    rules: tuple[tuple[str, str, Any], ...] = ()
    masks: Mapping[str, str] = field(default_factory=dict)

    def conditions_for(self, table: str) -> tuple[tuple[str, str, Any], ...]:
        columns = {column for column, _ in SCHEMA[table]}
        return tuple(rule for rule in self.rules if rule[0] in columns)

    def masks_for(self, table: str) -> dict[str, str]:
        columns = {column for column, _ in SCHEMA[table]}
        return {c: s for c, s in self.masks.items() if c in columns}


SCOPES: tuple[Scope, ...] = (
    Scope("tenant", (("tenant", "eq", "a"),), {"email": "partial", "body": "redact"}),
    Scope("two-tenants", (("tenant", "in", ["a", "c"]),), {"email": "hash"}),
    Scope("not-b", (("tenant", "ne", "b"),), {"email": "last4", "body": "redact"}),
    Scope("small-ids", (("tenant", "is_null", False), ("id", "lt", 4)), {"email": "null"}),
    Scope("by-domain", (("email", "like", "%@x.com"),), {"email": "redact"}),
    Scope("nobody", (("tenant", "in", []),), {}),
    Scope("masks-only", (), {"email": "partial", "amount": "null", "body": "redact"}),
    Scope("rows-only", (("amount", "gte", 7),), {}),
    # Nothing withheld: the application takes its unrestricted path for this one.
    Scope("everyone"),
)


# -- what the caller should see, built without the rewriter --------------------


def _like(value: str, pattern: str) -> bool:
    regex = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, value, flags=re.DOTALL) is not None


def _holds(value: Any, operator: str, target: Any) -> bool:
    """One row rule in SQL's terms: a comparison with NULL keeps no row."""
    if operator == "is_null":
        return (value is None) == bool(target)
    if operator == "in":
        return value is not None and value in target
    if value is None:
        return False
    compare: dict[str, Callable[[Any, Any], bool]] = {
        "eq": lambda a, b: a == b,
        "ne": lambda a, b: a != b,
        "lt": lambda a, b: a < b,
        "lte": lambda a, b: a <= b,
        "gt": lambda a, b: a > b,
        "gte": lambda a, b: a >= b,
        "like": _like,
    }
    return compare[operator](value, target)


def expected_tables(scope: Scope) -> dict[str, list[tuple[Any, ...]]]:
    """Each table as the caller may see it: their rows only, masks applied."""
    out: dict[str, list[tuple[Any, ...]]] = {}
    for table, rows in ROWS.items():
        names = [column for column, _ in SCHEMA[table]]
        conditions = scope.conditions_for(table)
        masks = scope.masks_for(table)
        kept = []
        for row in rows:
            record = dict(zip(names, row, strict=True))
            if all(_holds(record[c], op, v) for c, op, v in conditions):
                kept.append(
                    tuple(
                        mask_value(record[c], masks[c], HASH_KEY) if c in masks else record[c]
                        for c in names
                    )
                )
        out[table] = kept
    return out


def column_types(scope: Scope, table: str) -> list[tuple[str, str]]:
    """The DuckDB type of each column once masked: a mask turns a value into text."""
    masks = scope.masks_for(table)
    types = []
    for column, kind in SCHEMA[table]:
        strategy = masks.get(column)
        if strategy == "null":
            types.append((column, "INTEGER" if kind == "int" else "VARCHAR"))
        elif strategy is not None or kind == "text":
            types.append((column, "VARCHAR"))
        else:
            types.append((column, "INTEGER"))
    return types


def load(conn: Any, tables: Mapping[str, Sequence[tuple[Any, ...]]], types: Callable) -> None:
    """Create every table twice, as ``db.<t>`` and as ``main.<t>``, and fill it."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS db")
    for table, rows in tables.items():
        columns = ", ".join(f'"{c}" {t}' for c, t in types(table))
        marks = ", ".join("?" for _ in SCHEMA[table])
        for schema in ("db", "main"):
            conn.execute(f"CREATE TABLE {schema}.{table} ({columns})")
            if rows:
                conn.executemany(f"INSERT INTO {schema}.{table} VALUES ({marks})", list(rows))


def as_bag(rows: Iterable[Sequence[Any]]) -> Counter:
    """A result as a multiset, floats rounded: the order of rows says nothing here."""

    def norm(value: Any) -> Any:
        return round(value, 6) if isinstance(value, float) else value

    return Counter(tuple(norm(v) for v in row) for row in rows)


# -- the queries ----------------------------------------------------------------

INT, TEXT = "int", "text"
TEXT_LITERALS = ("'a'", "'b'", "'c'", "'alice@x.com'", "'hello'", "'%@x.com'", "''")


@dataclass
class Rel:
    """Something a FROM can name: its SQL, the alias to qualify by, its columns."""

    sql: str
    alias: str
    columns: list[tuple[str, str]]


@dataclass
class Gen:
    draw: Callable[[Any], Any]
    #: Flat SELECTs only (no CTE, set operation or window), the shapes the planner slices.
    flat: bool = False
    counter: int = 0
    ctes: list[tuple[str, list[tuple[str, str]]]] = field(default_factory=list)

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def pick(self, options: Sequence[Any]) -> Any:
        return self.draw(st.sampled_from(list(options)))

    def chance(self, p: float = 0.5) -> bool:
        return self.draw(st.floats(0, 1)) < p

    # relations

    def base(self) -> Rel:
        table = self.pick(sorted(SCHEMA))
        written = self.pick([f"db.{table}", table])
        alias = self.fresh("t")
        return Rel(f"{written} AS {alias}", alias, list(SCHEMA[table]))

    def relation(self, depth: int) -> Rel:
        kinds = ["base", "base"]
        if depth > 0:
            kinds.append("derived")
        if self.ctes:
            kinds.append("cte")
        kind = self.pick(kinds)
        if kind == "derived":
            sql, columns = self.select(depth - 1)
            alias = self.fresh("d")
            return Rel(f"({sql}) AS {alias}", alias, columns)
        if kind == "cte":
            name, columns = self.pick(self.ctes)
            alias = self.fresh("r")
            return Rel(f"{name} AS {alias}", alias, columns)
        return self.base()

    # expressions over the columns in scope, as (sql, type)

    def column(self, scope: Sequence[Rel], kind: str | None = None) -> tuple[str, str] | None:
        options = [(f"{r.alias}.{c}", t) for r in scope for c, t in r.columns]
        if kind is not None:
            options = [o for o in options if o[1] == kind]
        return self.pick(options) if options else None

    def literal(self, kind: str) -> str:
        if kind == INT:
            return str(self.draw(st.integers(-1, 25)))
        return self.pick(TEXT_LITERALS)

    def scalar(self, scope: Sequence[Rel], kind: str, depth: int) -> str:
        choice = self.pick(["column", "column", "literal", "expr", "subquery"])
        if choice == "subquery" and depth > 0:
            return self.scalar_subquery(scope, kind, depth - 1)
        found = self.column(scope, kind)
        if choice == "literal" or found is None:
            return self.literal(kind)
        if choice == "expr":
            if kind == INT:
                return f"({found[0]} {self.pick(['+', '-', '*'])} {self.literal(INT)})"
            return self.pick(
                [
                    f"COALESCE({found[0]}, 'none')",
                    f"UPPER({found[0]})",
                    f"({found[0]} || '!')",
                    f"CASE WHEN {found[0]} IS NULL THEN 'null' ELSE {found[0]} END",
                ]
            )
        return found[0]

    def scalar_subquery(self, scope: Sequence[Rel], kind: str, depth: int) -> str:
        inner = self.base()
        correlated = self.column(scope, INT) if scope and self.chance() else None
        where = ""
        if correlated is not None:
            mine = self.column([inner], INT)
            if mine is not None:
                where = f" WHERE {mine[0]} = {correlated[0]}"
        if kind == INT:
            agg = self.pick(["COUNT(*)", "MAX({c})", "MIN({c})", "SUM({c})"])
            target = self.column([inner], INT)
        else:
            agg = self.pick(["MAX({c})", "MIN({c})"])
            target = self.column([inner], TEXT)
        assert target is not None
        return f"(SELECT {agg.format(c=target[0])} FROM {inner.sql}{where})"

    def condition(self, scope: Sequence[Rel], depth: int) -> str:
        choice = self.pick(
            ["compare", "compare", "null", "like", "in", "exists", "or", "and", "not"]
        )
        if choice in ("or", "and"):
            op = choice.upper()
            return f"({self.condition(scope, depth)} {op} {self.condition(scope, depth)})"
        if choice == "not":
            return f"NOT ({self.condition(scope, depth)})"
        if choice in ("in", "exists") and depth > 0:
            inner = self.relation(depth - 1)
            if choice == "in":
                outer = self.column(scope, INT)
                mine = self.column([inner], INT)
                if outer is not None and mine is not None:
                    return f"{outer[0]} IN (SELECT {mine[0]} FROM {inner.sql})"
            else:
                outer = self.column(scope, INT)
                mine = self.column([inner], INT)
                if outer is not None and mine is not None:
                    return f"EXISTS (SELECT 1 FROM {inner.sql} WHERE {mine[0]} = {outer[0]})"
        if choice == "null":
            found = self.column(scope)
            if found is not None:
                return f"{found[0]} IS {self.pick(['', 'NOT '])}NULL"
        if choice == "like":
            found = self.column(scope, TEXT)
            if found is not None:
                return f"{found[0]} LIKE {self.pick(TEXT_LITERALS)}"
        kind = self.pick([INT, TEXT])
        left = self.column(scope, kind)
        if left is None:
            return "TRUE"
        op = self.pick(["=", "<>", "<", ">=", "=", "="])
        return f"{left[0]} {op} {self.scalar(scope, kind, depth)}"

    def shared_condition(self, scope: Sequence[Rel]) -> str:
        """One comparison on every alias that has the column: a table read twice
        is then fetched once, narrowed by what all of its aliases say."""
        column, kind = self.pick(sorted({c for r in scope for c in r.columns}))
        value = self.literal(kind)
        op = self.pick(["=", "<>", ">=", "<"])
        holders = [r for r in scope if (column, kind) in r.columns]
        return " AND ".join(f"{r.alias}.{column} {op} {value}" for r in holders)

    def from_clause(self, depth: int) -> tuple[str, list[Rel]]:
        first = self.relation(depth)
        sql, scope = first.sql, [first]
        # A flat query is where slices and keyed fetches happen, and joins are what they serve.
        for _ in range(self.draw(st.integers(1 if self.flat else 0, 2))):
            join = self.pick(["JOIN", "LEFT JOIN", "RIGHT JOIN", "FULL JOIN", "CROSS JOIN", ","])
            outer = self.column(scope, INT)
            if join == "," and outer is not None and self.chance(0.4):
                # LATERAL: a subquery that may name the tables to its left.
                inner = self.base()
                mine = self.column([inner], INT)
                assert mine is not None
                alias = self.fresh("l")
                columns = [(c, t) for c, t in inner.columns]
                projection = ", ".join(f"{inner.alias}.{c} AS {c}" for c, _ in columns)
                sql += (
                    f", LATERAL (SELECT {projection} FROM {inner.sql} "
                    f"WHERE {mine[0]} = {outer[0]}) AS {alias}"
                )
                scope.append(Rel("", alias, columns))
                break  # DuckDB has no outer join after a correlated LATERAL
            other = self.relation(depth)
            if join in ("CROSS JOIN", ","):
                sql += f" {join} {other.sql}" if join != "," else f", {other.sql}"
            else:
                left = self.column(scope, INT)
                right = self.column([other], INT)
                on = "TRUE" if left is None or right is None else f"{left[0]} = {right[0]}"
                sql += f" {join} {other.sql} ON {on}"
            scope.append(other)
        return sql, scope

    def select(self, depth: int, want: Sequence[str] | None = None) -> tuple[str, list]:
        """One SELECT and its output columns; ``want`` fixes their types (set operations)."""
        from_sql, scope = self.from_clause(depth)
        conditions = [self.condition(scope, depth)] if self.chance(0.6) else []
        if self.flat and self.chance(0.4):
            conditions.append(self.shared_condition(scope))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        shapes = ["plain", "plain", "group"] if self.flat else ["plain", "plain", "group", "window"]
        shape = "plain" if want is not None else self.pick(shapes)
        if shape == "group":
            key = self.column(scope)
            assert key is not None
            aggregates = [
                ("COUNT(*)", INT),
                (f"COUNT(DISTINCT {key[0]})", INT),
            ]
            number = self.column(scope, INT)
            if number is not None:
                aggregates += [(f"SUM({number[0]})", INT), (f"MAX({number[0]})", INT)]
            text = self.column(scope, TEXT)
            if text is not None:
                aggregates += [(f"MIN({text[0]})", TEXT), (f"COUNT({text[0]})", INT)]
            chosen = self.draw(st.lists(st.sampled_from(aggregates), min_size=1, max_size=3))
            items = [(key[0], key[1]), *chosen]
            having = (
                f" HAVING COUNT(*) {self.pick(['>', '>=', '='])} {self.draw(st.integers(0, 3))}"
                if self.chance(0.3)
                else ""
            )
            body = f"{from_sql}{where} GROUP BY {key[0]}{having}"
        elif shape == "window":
            part = self.column(scope)
            order = self.column(scope)
            number = self.column(scope, INT)
            assert part is not None and order is not None
            items = [(part[0], part[1])]
            items.append((f"RANK() OVER (PARTITION BY {part[0]} ORDER BY {order[0]})", INT))
            if number is not None:
                items.append((f"SUM({number[0]}) OVER (PARTITION BY {part[0]})", INT))
            items.append(("COUNT(*) OVER ()", INT))
            body = f"{from_sql}{where}"
            if self.chance(0.3):
                body += f" QUALIFY COUNT(*) OVER () > {self.draw(st.integers(0, 4))}"
        else:
            kinds = list(want) if want is not None else None
            if kinds is None:
                kinds = self.draw(st.lists(st.sampled_from([INT, TEXT]), min_size=1, max_size=3))
            items = [(self.scalar(scope, kind, depth), kind) for kind in kinds]
            if want is None and self.chance(0.15):
                star = self.pick(scope)
                if star.sql:
                    items = [(f"{star.alias}.{c}", t) for c, t in star.columns]
            body = f"{from_sql}{where}"
        distinct = "DISTINCT " if shape != "window" and self.chance(0.2) else ""
        names = [self.fresh("c") for _ in items]
        projection = ", ".join(
            f"{sql} AS {name}" for (sql, _), name in zip(items, names, strict=True)
        )
        columns = [(name, kind) for name, (_, kind) in zip(names, items, strict=True)]
        return f"SELECT {distinct}{projection} FROM {body}", columns

    def query(self, depth: int) -> str:
        if self.flat:
            return self.select(0)[0]
        with_sql = ""
        if self.chance(0.35):
            parts = []
            for _ in range(self.draw(st.integers(1, 2))):
                sql, columns = self.select(max(depth - 1, 0))
                name = self.fresh("x")
                parts.append(f"{name} AS ({sql})")
                self.ctes.append((name, columns))
            with_sql = "WITH " + ", ".join(parts) + " "
        if self.chance(0.2):
            first, columns = self.select(depth)
            second, _ = self.select(depth, want=[t for _, t in columns])
            op = self.pick(["UNION", "UNION ALL", "EXCEPT", "INTERSECT", "EXCEPT ALL"])
            return f"{with_sql}({first}) {op} ({second})"
        sql, _ = self.select(depth)
        return with_sql + sql


@st.composite
def queries(draw: Callable[[Any], Any], depth: int = 2, flat: bool = False) -> str:
    """One random SELECT over ``customers`` and ``notes``, each named ``db.<t>`` or ``<t>``.

    ``flat`` keeps to one SELECT with joins and conditions, which the planner can
    serve from slices; otherwise ``depth`` bounds how far queries nest.
    """
    return Gen(draw, flat=flat).query(depth)


# -- the same callers, as the application sees them -------------------------------

#: Each masked column carries a label of its own name, so a mask is set per column.
LABELS = {table: {c: c for c, _ in columns} for table, columns in SCHEMA.items()}


def access_scope(scope: Scope) -> Any:
    """``scope`` as the AccessScope a signed-in caller would carry."""
    from truejoin.domain.governance.access import AccessScope
    from truejoin.domain.governance.masking import CatalogMasking, MaskingPolicy
    from truejoin.domain.governance.row_policy import build_policy

    rules = {
        table: [
            {"column": c, "operator": op, "value": v} for c, op, v in scope.conditions_for(table)
        ]
        for table in SCHEMA
    }
    masking = CatalogMasking(
        policy=MaskingPolicy(rules=dict(scope.masks), hash_key=HASH_KEY), semantic_types=LABELS
    )
    return AccessScope(
        actor=f"fuzz-{scope.name}",
        policy=build_policy({t: r for t, r in rules.items() if r}),
        masking={"db": masking},
        masked=bool(scope.masks),
    )


def source_tables() -> dict[str, list[dict[str, Any]]]:
    """The raw rows as a fake source serves them."""
    return {
        table: [dict(zip([c for c, _ in SCHEMA[table]], row, strict=True)) for row in rows]
        for table, rows in ROWS.items()
    }
