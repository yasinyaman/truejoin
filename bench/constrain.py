"""Turning a semantic model into a decoding constraint.

The first run failed the semantic arm on 17 of 40 questions, and every one of
those failures was a *form* error: a metric written ``sum(tutar)`` instead of
``tutar:sum``, a dimension the table does not have, an ``order_by`` handed
over as a list of objects. Not one was a wrong choice of measure.

Form errors are the kind a schema can make unrepresentable, and a semantic
model already holds exactly what such a schema needs. So instead of teaching a
model the DSL — by fine-tuning it, or by retrieving examples into the prompt —
the *shape* of the request is constrained while the model decodes, and the DSL
string is assembled here from choices it could not get syntactically wrong.

This is the one advantage arm C has that arms A and B structurally cannot:
the legal metrics of a semantic model can be enumerated; the legal SQL
statements against a schema cannot.

What it does **not** do is make an answer correct. A constrained model picks a
real measure — not necessarily the right one. Constraining removes the form
errors so that what remains is the interesting question: can the model map a
business question onto the right measure? That residual is what a retrieval
step or a fine-tune would have to earn its keep against.

Every rule here is read from the domain rather than restated: which
aggregations a measure accepts comes from ``Measure.check_agg``, so a rule
change in Fusion cannot leave a stale copy behind in the benchmark.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from trio.application.olap.lexicon import to_dsl

__all__ = ["dimension_values", "request_schema", "table_branch", "to_dsl", "values_by_table"]

#: Transforms offered to a constrained model.
#:
#: ``ntile`` is left out deliberately: it is the only transform with a
#: *required* argument, and a schema that let the model pick it without one
#: would reintroduce exactly the class of error this module exists to remove.
#: Every other transform has a working default.
EXCLUDED_TRANSFORMS = frozenset({"ntile"})

#: Filter operators the arm offers. A subset of the DSL's, kept to the ones a
#: question can actually ask for without a second value.
FILTER_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "like")

#: Aggregations considered for each measure. Which of them survive is decided
#: per measure by the domain, not here.
CANDIDATE_AGGS = ("sum", "avg", "min", "max", "count", "count_distinct", "median")

#: The largest row cap a constrained request may ask for. Not a policy — the
#: DSL clamps to its own maximum anyway — just a bound the grammar can state.
MAX_LIMIT = 1000

#: A dimension with at most this many distinct values has its values offered as
#: an enum, so a filter cannot compare against one that was never stored.
#:
#: This is a fairness correction as much as an improvement. The catalog arm's
#: context spells out ``durum: basarili | iptal`` and ``kanal: mobil | web |
#: sube``; Fusion's semantic model does not carry values at all, so the first
#: run had the semantic arm guessing English values against Turkish data —
#: `durum = 'canceld'` where the column holds `iptal`. Withholding from one arm
#: what another is handed measures the harness, not the layer.
MAX_ENUMERABLE_VALUES = 25


def legal_aggregations(measure: Any) -> list[str]:
    """The aggregations this measure actually accepts.

    Asks the domain rather than re-deriving the rule: ``check_agg`` is what
    ``SemanticQuery.validate`` will apply later, so anything that survives
    here is guaranteed to survive there.
    """
    allowed = []
    for agg in CANDIDATE_AGGS:
        try:
            measure.check_agg(agg)
        except Exception:
            continue
        allowed.append(agg)
    return allowed


def _metric_branch(
    measure: Any, transforms: Sequence[str], aggregations: Sequence[str] | None = None
) -> dict[str, Any]:
    """One ``oneOf`` branch: this measure, with only the aggregations it takes.

    Splitting per measure is what makes the constraint exact. A single
    ``{measure: enum, aggregation: enum}`` object would allow ``segment:sum``,
    which the schema would accept and the domain would then reject.
    """
    properties: dict[str, Any] = {
        "measure": {"const": measure.name},
        "aggregation": {
            "enum": list(aggregations) if aggregations is not None else legal_aggregations(measure)
        },
    }
    if transforms:
        properties["transform"] = {"enum": ["", *transforms]}
    return {
        "type": "object",
        "properties": properties,
        "required": ["measure", "aggregation"],
        "additionalProperties": False,
    }


def dimension_values(semantic: Any, table: str, dimension: Any) -> list[Any]:
    """The values of a dimension that is a category, or an empty list.

    Asks through the DSL itself — a grouped row count *is* the dimension's
    domain — so discovery needs no second query path and honours the same
    slice rules as any other read.

    Two things are excluded, and the grouped count decides both. A dimension
    with too many values is not worth enumerating. And a dimension whose every
    value occurs exactly once is an identifier rather than a category: a
    transaction id, an amount, an email address. Enumerating those would leak
    per-row data into a prompt — the email column in this fixture is the case
    that makes the rule worth stating — and would teach the model nothing,
    because a vocabulary that never repeats is not a vocabulary.
    """
    described = dimension.as_dict()
    if described.get("temporal"):
        return []
    try:
        result = semantic.run(
            table, ["*:count"], [described["name"]], limit=MAX_ENUMERABLE_VALUES + 1
        )
    except Exception:
        return []
    rows = result.rows if hasattr(result, "rows") else result.get("rows", [])
    if not rows or len(rows) > MAX_ENUMERABLE_VALUES:
        return []
    columns = list(result.columns if hasattr(result, "columns") else result.get("columns", []))
    name = described["name"]
    if name not in columns or len(columns) < 2:
        return []
    # Rows come back as tuples from the service and as mappings from the tool
    # layer; reading by column position works for both.
    index, counts = columns.index(name), columns[-1]
    pairs = [
        (row[name], row[counts]) if isinstance(row, Mapping) else (row[index], row[-1])
        for row in rows
    ]
    # NULL is not part of the vocabulary, and it must not prop up the
    # "does it repeat?" test either: an email column with two missing
    # addresses would otherwise pass on the strength of its NULL group.
    present = [(value, count) for value, count in pairs if value is not None]
    if not present or all(_as_count(count) <= 1 for _, count in present):
        return []
    return sorted((value for value, _ in present), key=str)


def _as_count(value: Any) -> int:
    """A row count as an int, however the store typed it."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _dimension_branch(dimension: Any, prefix: str = "") -> dict[str, Any]:
    """One ``oneOf`` branch per dimension.

    A grain is only offered on a temporal dimension — the property is absent
    from the other branches, so ``kanal:month`` cannot be expressed at all.
    """
    described = dimension.as_dict()
    properties: dict[str, Any] = {"column": {"const": prefix + described["name"]}}
    # A referenced table's dates carry no grain here: it keeps the grammar
    # small, and no question in the set asks for one.
    grains = [] if prefix else described.get("grains") or []
    if grains:
        properties["grain"] = {"enum": ["", *grains]}
    return {
        "type": "object",
        "properties": properties,
        "required": ["column"],
        "additionalProperties": False,
    }


def orderable_columns(model: Any, pairs: Sequence[tuple[str, str]]) -> list[str]:
    """The names a result of this table could be ordered by.

    Built by asking the domain what each choice would be called rather than
    re-deriving the convention here: a measure and an aggregation become one
    output column, and only the domain knows how it is spelled.

    Transform outputs are left out to keep the list short — ordering by a
    running total is rare, and the enum has to stay small enough to be a
    grammar. What this removes is the whole class of failure that ``order_by``
    produced when it was free text: a model writing ``tutar DESC``, or
    ``rank(tutar, 'asc')``, into a field that wanted a column name. Direction
    is a separate boolean, so it has no reason to be spelled into the name.
    """
    from trio.domain.olap.metric_dsl import parse_metric
    from trio.domain.olap.semantic_query import parse_dimension

    names = [""]
    for dimension in model.dimensions:
        described = dimension.as_dict()
        for grain in described.get("grains") or [""]:
            name = f"{described['name']}:{grain}" if grain else described["name"]
            try:
                names.append(parse_dimension(name, model).output_name())
            except Exception:
                continue
    for measure, agg in pairs:
        try:
            names.append(parse_metric(f"{measure}:{agg}").output_name())
        except Exception:
            continue
    return list(dict.fromkeys(names))


def _filter_branches(
    model: Any, values: Mapping[str, Sequence[Any]], prefix: str = ""
) -> list[dict[str, Any]]:
    """One filter branch per column, with its stored values when they are known.

    A column whose values are enumerated can only be compared against one of
    them, which removes the last place a constrained request could still name
    something that does not exist.
    """
    branches = []
    for dimension in model.dimensions:
        name = dimension.as_dict()["name"]
        known = values.get(name)
        branches.append(
            {
                "type": "object",
                "properties": {
                    "column": {"const": prefix + name},
                    "op": {"enum": ["eq", "ne"] if known else list(FILTER_OPS)},
                    "value": (
                        {"enum": list(known)}
                        if known
                        else {"type": ["string", "number", "boolean"]}
                    ),
                },
                "required": ["column", "op", "value"],
                "additionalProperties": False,
            }
        )
    return branches


def table_branch(
    model: Any,
    transforms: Sequence[str],
    values: Mapping[str, Sequence[Any]] | None = None,
    referenced: Sequence[tuple[Any, Mapping[str, Sequence[Any]]]] = (),
) -> dict[str, Any]:
    """The request schema for one table.

    ``referenced`` are the models (and stored values) of the tables this one
    references. Their columns are offered as ``table.column`` dimensions and
    filters, which is how ``query_metrics`` takes them; the join is not the
    model's to write.
    """
    values = values or {}
    own = {d.name for d in model.dimensions}
    dimensions = [_dimension_branch(d) for d in model.dimensions]
    filters = _filter_branches(model, values)
    joined_names = []
    for other, other_values in referenced:
        prefix = f"{other.ref.table}."
        offered = [d for d in other.dimensions if d.name not in own and not d.temporal]
        dimensions += [_dimension_branch(d, prefix) for d in offered]
        filters += [
            branch
            for branch in _filter_branches(other, other_values, prefix)
            if branch["properties"]["column"]["const"].removeprefix(prefix) not in own
        ]
        joined_names += [f"{other.ref.table}_{d.name}" for d in offered]
    # Computed once and handed to the branches, rather than recomputed inside
    # each of them: every call attempts seven aggregations and catches the
    # refusals as exceptions.
    by_measure = {m.name: legal_aggregations(m) for m in model.measures}
    pairs = [(name, agg) for name, aggs in by_measure.items() for agg in aggs]
    return {
        "type": "object",
        "properties": {
            "table": {"const": model.ref.full_name},
            "metrics": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "oneOf": [
                        _metric_branch(m, transforms, by_measure[m.name]) for m in model.measures
                    ]
                },
            },
            "dimensions": {
                "type": "array",
                "maxItems": 3,
                "items": {"oneOf": dimensions},
            },
            "filters": {
                "type": "array",
                "maxItems": 2,
                "items": {"oneOf": filters},
            },
            # Enumerated, after free text was measured and found to be the
            # last hole in the constraint: it produced 10 of 40 failures, all
            # of them a model writing SQL — `tutar DESC` — where a column name
            # belonged. Ordering by a name this particular query did not
            # select is still expressible, and is a real mistake worth seeing.
            "order_by": {"enum": [*orderable_columns(model, pairs), *joined_names]},
            "descending": {"type": "boolean"},
            # Without this a constrained reply could not carry a row cap at
            # all: `additionalProperties` is closed, so a seeded `limit` was
            # dropped on every "top N" question and silently became 100.
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
        "required": ["table", "metrics"],
        "additionalProperties": False,
    }


def request_schema(
    semantic: Any,
    tables: Sequence[str],
    values: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
    parents: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """A JSON schema admitting exactly the requests these tables can answer.

    One ``oneOf`` branch per table, because measures and dimensions differ
    between them and a union would let a model ask for one table's column on
    another's.

    ``values`` (``table -> column -> stored values``) replaces discovering
    them from the data; ``parents`` (``table -> the tables it references``)
    adds those tables' columns to each branch.
    """
    if not tables:
        raise ValueError("request_schema needs at least one table to describe.")
    # Straight from the registry: transforms are global, and `describe` would
    # build a model and a set of examples only to have them thrown away.
    from trio.domain.olap.transforms import TRANSFORMS

    transforms = [name for name in TRANSFORMS if name not in EXCLUDED_TRANSFORMS]

    def stored(table: str) -> Mapping[str, Sequence[Any]]:
        if values is not None:
            return values.get(table, {})
        return {
            d.as_dict()["name"]: found
            for d in semantic.model_for(table).dimensions
            if (found := dimension_values(semantic, table, d))
        }

    branches = []
    for table in tables:
        referenced = [
            (semantic.model_for(parent), stored(parent))
            for parent in (parents or {}).get(table, ())
        ]
        branches.append(
            table_branch(semantic.model_for(table), transforms, stored(table), referenced)
        )
    if len(branches) == 1:
        return branches[0]
    return {"oneOf": branches}


def values_by_table(schema: Mapping[str, Any]) -> dict[str, dict[str, list[Any]]]:
    """The value enums a request schema carries, per table.

    Lets everything downstream read the vocabulary off the schema that was
    just built instead of rediscovering it — each discovery is a full DSL
    round trip, and there is nothing new to learn the second time.
    """
    branches = schema.get("oneOf") or [schema]
    found: dict[str, dict[str, list[Any]]] = {}
    for branch in branches:
        properties = branch.get("properties", {})
        table = properties.get("table", {}).get("const")
        if not table:
            continue
        per_column: dict[str, list[Any]] = {}
        for option in properties.get("filters", {}).get("items", {}).get("oneOf", []):
            option_properties = option.get("properties", {})
            values = option_properties.get("value", {}).get("enum")
            if values:
                per_column[option_properties["column"]["const"]] = list(values)
        found[table] = per_column
    return found
