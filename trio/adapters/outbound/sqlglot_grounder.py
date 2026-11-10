"""SqlglotGrounder: verdicts on a query's joins, and the two repairs safe to make.

The three verdicts follow NVIDIA Auto Ontology's join-path check; three things
differ. A join is accepted when both columns hold the same key (two foreign
keys to one parent), not only when it is an edge itself, so a direct join
between siblings is not sent through their parent for nothing. Shared values
vouch for a join only when one of the columns is not a known key: `orders.id`
and `customers.id` share nearly all their values and nothing else. And a
repair is either provably local or not made at all: there is no model in the
loop to hand the rest to.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

import sqlglot
from sqlglot import exp

from trio.domain.catalog.join_graph import JoinEdge, JoinGraph
from trio.domain.olap.grounding import (
    MISSING_BRIDGE,
    OVERLAP_KEEP,
    UNVERIFIED,
    WRONG_COLUMN,
    Grounding,
    JoinTable,
    JoinVerdict,
)
from trio.domain.olap.models import TableRef
from trio.ports.grounding import Probe

logger = logging.getLogger(__name__)

DIALECT = "duckdb"

_Aliases = dict[str, set[JoinTable | None]]


@dataclass(slots=True)
class _Side:
    """One column of a join condition, attributed to the table it belongs to."""

    column: exp.Column
    table: JoinTable
    #: How the query refers to that table.
    alias: str


@dataclass(slots=True)
class _Finding:
    verdict: JoinVerdict
    eq: exp.EQ
    left: _Side
    right: _Side
    #: The known way from the left table to the right one.
    edges: list[JoinEdge]
    #: Read from USING or NATURAL: judged, never repaired.
    spelled: bool = False


class SqlglotGrounder:
    def ground(
        self,
        sql: str,
        tables: Mapping[TableRef, JoinTable],
        graph: JoinGraph,
        probe: Probe,
        repair: bool = False,
    ) -> Grounding:
        if not len(graph) or len(tables) < 2:
            return Grounding(sql)
        try:
            tree = sqlglot.parse_one(sql, read=DIALECT)
        except Exception as e:
            # The validator decides what may run; this only advises on joins.
            logger.info("Joins not checked, the query did not parse: %s", e)
            return Grounding(sql)

        known = dict(tables)
        findings = _findings(tree, known, graph, probe)
        if not repair:
            return Grounding(sql, _distinct(f.verdict for f in findings))
        repaired = [replace(f.verdict, repaired=True) for f in findings if _repair(f, known)]
        if not repaired:
            return Grounding(sql, _distinct(f.verdict for f in findings))
        # The rewritten query is judged like any other: a join no repair could
        # reach is still in it, and comes back as a verdict of its own.
        left_over = [f.verdict for f in _findings(tree, known, graph, probe)]
        return Grounding(tree.sql(dialect=DIALECT), _distinct([*repaired, *left_over]))


def _distinct(verdicts: Iterable[JoinVerdict]) -> tuple[JoinVerdict, ...]:
    return tuple(dict.fromkeys(verdicts))


def _same(left: str | None, right: str) -> bool:
    return left is not None and left.lower() == right.lower()


# -- verdicts ----------------------------------------------------------------


def _findings(
    tree: exp.Expr, tables: Mapping[TableRef, JoinTable], graph: JoinGraph, probe: Probe
) -> list[_Finding]:
    aliases: _Aliases = {}
    for node in tree.find_all(exp.Table):
        # None for a CTE, a view or anything else that is not a source table.
        aliases.setdefault(node.alias_or_name.lower(), set()).add(
            tables.get(TableRef(node.db or "", node.name))
        )

    written = [
        (eq, False)
        for eq in tree.find_all(exp.EQ)
        # a column against a value is a filter, not a join
        if isinstance(eq.this, exp.Column) and isinstance(eq.expression, exp.Column)
        if eq.find_ancestor(exp.Join, exp.Where) is not None
    ]
    findings: list[_Finding] = []
    for eq, spelled in [*written, *((eq, True) for eq in _spelled_out(tree, tables))]:
        left, right = _side(eq.this, aliases), _side(eq.expression, aliases)
        if left is None or right is None:
            continue
        a, b = left.table.ref, right.table.ref
        x, y = left.column.name, right.column.name
        if a == b or a.source != b.source:
            continue  # a self-join, or two databases no key can span
        if not (graph.knows(a, x) or graph.knows(b, y)) or graph.same_key(a, x, b, y):
            continue

        direct = graph.edges_between(a, b)
        overlap = None
        if not (graph.knows(a, x) and graph.knows(b, y)):
            # The graph can be incomplete: a column it does not know that
            # shares its values with a key is a relationship nobody declared,
            # not a mistake. Two known keys of different things are never
            # asked: serial ids overlap whatever they identify.
            overlap = probe(a, x, b, y)
            if overlap is not None and overlap >= OVERLAP_KEEP:
                continue
        if direct:
            kind = WRONG_COLUMN
            # Two tables can be related twice (ordered_by, approved_by): keep
            # the key that shares a column with what was written, if one does.
            edges = [
                e for e in direct if _same(e.column_of(a), x) or _same(e.column_of(b), y)
            ] or direct
        else:
            edges = graph.shortest_path(a, b)
            kind = MISSING_BRIDGE if edges else UNVERIFIED
        verdict = JoinVerdict(
            kind, f"{a.table}.{x}", f"{b.table}.{y}", tuple(str(e) for e in edges), overlap
        )
        findings.append(_Finding(verdict, eq, left, right, edges, spelled))
    return findings


def _spelled_out(tree: exp.Expr, tables: Mapping[TableRef, JoinTable]) -> list[exp.EQ]:
    """The column pairs each USING or NATURAL join compares, as ON would write them.

    Left out of the tree: rewriting such a join as ON changes what ``SELECT *``
    returns and makes the shared column ambiguous, so these are judged only.
    A join after anything that is not a known table is skipped, since the
    column could be that thing's: missing information never flags.
    """

    def columns(node: exp.Expr) -> set[str]:
        found = isinstance(node, exp.Table) and tables.get(TableRef(node.db or "", node.name))
        return {c.lower() for c in found.columns} if found else set()

    pairs: list[exp.EQ] = []
    for select in tree.find_all(exp.Select):
        start = select.args.get("from_")
        earlier = [start.this] if start else []
        for join in select.args.get("joins") or []:
            joined = join.this
            names = [i.name.lower() for i in join.args.get("using") or []]
            if join.method == "NATURAL":
                names = sorted(columns(joined))
            known = [(node, columns(node)) for node in earlier]
            if names and isinstance(joined, exp.Table) and all(cols for _, cols in known):
                for name in names:
                    owners = [node for node, cols in known if name in cols]
                    if len(owners) == 1:
                        pairs.append(
                            exp.EQ(
                                this=exp.column(name, owners[0].alias_or_name),
                                expression=exp.column(name, joined.alias_or_name),
                            )
                        )
            earlier.append(joined)
    return pairs


def _side(column: exp.Column, aliases: _Aliases) -> _Side | None:
    """The table ``column`` belongs to, when exactly one can be named."""
    if column.table:
        found = aliases.get(column.table.lower(), set())
        if len(found) != 1:
            return None
        (table,) = found
        return _Side(column, table, column.table) if table else None
    name = column.name.lower()
    owners = [
        (alias, table)
        for alias, found in aliases.items()
        for table in found
        if table is not None and name in {c.lower() for c in table.columns}
    ]
    return _Side(column, owners[0][1], owners[0][0]) if len(owners) == 1 else None


# -- repairs -----------------------------------------------------------------


def _repair(finding: _Finding, tables: dict[TableRef, JoinTable]) -> bool:
    """Rewrite the join in place when there is exactly one way to; say whether."""
    kind, edges = finding.verdict.verdict, finding.edges
    if finding.spelled:
        return False
    if kind == WRONG_COLUMN and len(edges) == 1:
        for side in (finding.left, finding.right):
            name = edges[0].column_of(side.table.ref)
            if name is not None and not _same(name, side.column.name):
                side.column.set("this", exp.to_identifier(name, quoted=True))
        return True
    if kind == MISSING_BRIDGE and len(edges) == 2:
        return _bridge(finding, tables)
    return False


def _bridge(finding: _Finding, tables: dict[TableRef, JoinTable]) -> bool:
    """Put the one table in between into the query, just before the join at fault.

    Only where that is mechanical: the condition sits in the ON of an inner or
    left join that brings one of the two tables in, and the table in between
    is not in the query yet (whether to reuse it would be a judgment call).
    """
    join = finding.eq.find_ancestor(exp.Join, exp.Where, exp.Select)
    if not isinstance(join, exp.Join) or not isinstance(join.this, exp.Table):
        return False
    select = join.parent
    if not isinstance(select, exp.Select):
        return False
    if join.side not in ("", "LEFT") or join.kind not in ("", "INNER") or join.method:
        return False

    first, second = finding.edges
    bridge = first.other_end(finding.left.table.ref)
    if first.parent == bridge and second.parent == bridge:
        # Two children of one parent: going through it pairs every row of one
        # with every row of the other, which is rarely what was asked.
        return False
    taken = {
        name.lower()
        for node in finding.eq.root().find_all(exp.Table)
        for name in (node.name, node.alias_or_name)
    }
    if bridge.table.lower() in taken:
        return False

    joined = join.this.alias_or_name.lower()
    near, near_edge, far, far_edge = finding.left, first, finding.right, second
    if finding.left.alias.lower() == joined:
        near, near_edge, far, far_edge = finding.right, second, finding.left, first
    elif finding.right.alias.lower() != joined:
        return False  # the condition relates two tables that were joined earlier

    name = exp.to_identifier(bridge.table, quoted=True)

    def of_bridge(edge: JoinEdge) -> exp.Column:
        return exp.Column(
            this=exp.to_identifier(edge.column_of(bridge), quoted=True), table=name.copy()
        )

    def of_side(side: _Side, edge: JoinEdge) -> exp.Column:
        qualifier = side.column.args.get("table")
        return exp.Column(
            this=exp.to_identifier(edge.column_of(side.table.ref), quoted=True),
            table=qualifier.copy() if qualifier else exp.to_identifier(side.alias),
        )

    hop = exp.Join(
        this=exp.Table(this=name, db=exp.to_identifier(bridge.source, quoted=True)),
        on=exp.EQ(this=of_side(near, near_edge), expression=of_bridge(near_edge)),
        side=join.args.get("side"),
        kind=join.args.get("kind"),
    )
    finding.eq.replace(exp.EQ(this=of_bridge(far_edge), expression=of_side(far, far_edge)))
    joins = select.args["joins"]
    at = next(i for i, existing in enumerate(joins) if existing is join)
    select.set("joins", [*joins[:at], hop, *joins[at:]])
    tables[bridge] = JoinTable(bridge, frozenset())
    return True
