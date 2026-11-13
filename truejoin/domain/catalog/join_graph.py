"""JoinGraph: which columns of which tables may be joined, and through what.

Edges are the foreign keys a database declares, plus the relationships of an
approved catalog that were checked against the data. Names compare without
regard to case: Oracle reports upper case and queries are written in any.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from truejoin.domain.olap.models import TableRef

_Table = tuple[str, str]
_Column = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class JoinEdge:
    """``table.column`` references ``ref_table.ref_column``, both in ``source``."""

    source: str
    table: str
    column: str
    ref_table: str
    ref_column: str
    #: "fk" (declared in the database) or "catalog" (approved and verified).
    origin: str = "fk"

    @property
    def child(self) -> TableRef:
        return TableRef(self.source, self.table)

    @property
    def parent(self) -> TableRef:
        return TableRef(self.source, self.ref_table)

    def column_of(self, ref: TableRef) -> str | None:
        """This edge's column on ``ref``'s side, or None when it does not touch it."""
        if _table(ref) == _table(self.child):
            return self.column
        return self.ref_column if _table(ref) == _table(self.parent) else None

    def other_end(self, ref: TableRef) -> TableRef:
        """The table at the end of this edge that is not ``ref``."""
        return self.parent if _table(ref) == _table(self.child) else self.child

    def __str__(self) -> str:
        return f"{self.table}.{self.column} = {self.ref_table}.{self.ref_column}"


def _table(ref: TableRef) -> _Table:
    return ref.source.lower(), ref.table.lower()


def _column(ref: TableRef, column: str) -> _Column:
    return ref.source.lower(), ref.table.lower(), column.lower()


class JoinGraph:
    """Answers questions about joins: what a key matches, and how tables are reached.

    - ``same_key``: do two columns hold the same key? True for the two ends of
      an edge and for anything connected through edges, so two foreign keys to
      one parent may be compared directly.
    - ``edges_between``: how are two tables related directly?
    - ``shortest_path``: through which tables are they related at all?
    """

    def __init__(
        self,
        edges: Iterable[JoinEdge] = (),
        keys: Iterable[tuple[TableRef, str]] = (),
        bridges: Iterable[TableRef] = (),
        composite: Iterable[tuple[TableRef, tuple[str, ...]]] = (),
    ) -> None:
        """``keys`` are single-column primary keys: a key of their own even when
        nothing references them, so one is never mistaken for another's.

        ``composite`` are primary keys of more than one column. The databases
        report a composite foreign key one column at a time, so edges from one
        table to the columns of such a key are one key, to be joined on whole.

        ``bridges`` are link tables: nothing but keys to two other tables (and
        maybe an id of their own), the way a many-to-many is stored.
        """
        self._bridges = {_table(ref) for ref in bridges}
        self._composite: dict[_Table, list[tuple[str, ...]]] = {}
        for ref, columns in composite:
            self._composite.setdefault(_table(ref), []).append(tuple(c.lower() for c in columns))
        # One edge per pair of columns, whoever declared it: a catalog
        # relationship that repeats a foreign key must not make it look like
        # a second way to the same parent.
        unique: dict[tuple[_Column, _Column], JoinEdge] = {}
        for edge in edges:
            pair = (_column(edge.child, edge.column), _column(edge.parent, edge.ref_column))
            unique.setdefault(pair, edge)
        self._edges = tuple(unique.values())
        self._root: dict[_Column, _Column] = {}
        self._adjacent: dict[_Table, list[JoinEdge]] = {}
        for ref, column in keys:
            self._find(_column(ref, column))
        for edge in self._edges:
            child = _column(edge.child, edge.column)
            parent = _column(edge.parent, edge.ref_column)
            self._root[self._find(child)] = self._find(parent)
            if child[:2] != parent[:2]:  # a self-reference leads to no other table
                self._adjacent.setdefault(child[:2], []).append(edge)
                self._adjacent.setdefault(parent[:2], []).append(edge)

    def _find(self, column: _Column) -> _Column:
        root = self._root.setdefault(column, column)
        while root != self._root[root]:
            root = self._root[root]
        self._root[column] = root
        return root

    def __len__(self) -> int:
        return len(self._edges)

    @property
    def edges(self) -> tuple[JoinEdge, ...]:
        return self._edges

    def knows(self, ref: TableRef, column: str) -> bool:
        """Whether ``column`` is a key: a primary key, or either end of some edge."""
        return _column(ref, column) in self._root

    def same_key(
        self, left: TableRef, left_column: str, right: TableRef, right_column: str
    ) -> bool:
        a, b = _column(left, left_column), _column(right, right_column)
        return a in self._root and b in self._root and self._find(a) == self._find(b)

    def edges_between(self, left: TableRef, right: TableRef) -> list[JoinEdge]:
        """Every edge that connects the two tables, in either direction."""
        pair = {_table(left), _table(right)}
        return [
            edge
            for edge in self._adjacent.get(_table(left), ())
            if {_table(edge.child), _table(edge.parent)} == pair
        ]

    def composite_keys(self, left: TableRef, right: TableRef) -> list[tuple[JoinEdge, ...]]:
        """The keys of more than one column relating the two tables, an edge per column."""
        found = []
        for child, parent in ((left, right), (right, left)):
            edges = [e for e in self.edges_between(left, right) if _table(e.child) == _table(child)]
            for key in self._composite.get(_table(parent), ()):
                by_column = {e.ref_column.lower(): e for e in edges if e.ref_column.lower() in key}
                if len(by_column) == len(key):
                    found.append(tuple(by_column[c] for c in key))
        return found

    def parents(self, table: TableRef) -> dict[TableRef, tuple[JoinEdge, ...]]:
        """Every table ``table`` references, directly or through others, and the way there.

        Only child-to-parent steps, so each row of ``table`` meets at most one
        row of what it reaches: joining along such a path never repeats a row
        and never inflates a sum. Nearest first. A parent reached two ways (by
        two keys of one table, a sender and a receiver, or by two chains, a
        buyer's country and a shop's) is left out, and so is everything beyond
        it: nothing here can tell which was meant.
        """
        start = _table(table)
        found: dict[_Table, tuple[TableRef, tuple[JoinEdge, ...]]] = {}
        ambiguous: set[_Table] = set()
        queue: deque[tuple[_Table, tuple[JoinEdge, ...]]] = deque([(start, ())])
        while queue:
            current, path = queue.popleft()
            on_path = {start, *(_table(edge.parent) for edge in path)}
            up: dict[_Table, list[JoinEdge]] = {}
            for edge in self._adjacent.get(current, ()):
                if _table(edge.child) == current:
                    up.setdefault(_table(edge.parent), []).append(edge)
            for parent, edges in up.items():
                if parent in on_path:
                    continue
                if len(edges) != 1 or parent in found or parent in ambiguous:
                    ambiguous.add(parent)
                    found.pop(parent, None)
                    continue
                found[parent] = (edges[0].parent, (*path, edges[0]))
                queue.append((parent, (*path, edges[0])))
        return {
            ref: path
            for ref, path in found.values()
            if not any(_table(edge.parent) in ambiguous for edge in path)
        }

    def across(self, table: TableRef) -> dict[TableRef, tuple[JoinEdge, ...]]:
        """Tables ``table`` meets many of through a link table, and the way there.

        The first step goes down from ``table`` to a link table that
        references it, the second up to the link's other end, and any further
        steps up from there as ``parents`` would take them (a student's
        courses, then those courses' departments). Each row of ``table`` meets
        several rows at the far end, so whoever joins along such a path has to
        count each one once per group (see the semantic compiler). Tables
        ``table`` already references are left out (``parents`` reaches them
        without the fan-out), and so is a table reached two ways.
        """
        start = _table(table)
        near = {_table(ref) for ref in self.parents(table)} | {start}
        found: dict[_Table, tuple[TableRef, tuple[JoinEdge, ...]]] = {}
        ambiguous: set[_Table] = set()

        def offer(ref: TableRef, path: tuple[JoinEdge, ...]) -> None:
            key = _table(ref)
            if key in near or key in ambiguous:
                return
            if key in found:
                ambiguous.add(key)
                found.pop(key)
                return
            found[key] = (ref, path)

        for down in self._adjacent.get(start, ()):
            bridge = _table(down.child)
            if _table(down.parent) != start or bridge not in self._bridges:
                continue
            ups = [
                edge
                for edge in self._adjacent.get(bridge, ())
                if _table(edge.child) == bridge and _table(edge.parent) != start
            ]
            mine = [
                edge
                for edge in self._adjacent.get(bridge, ())
                if _table(edge.child) == bridge and _table(edge.parent) == start
            ]
            if len(ups) != 1 or len(mine) != 1:
                continue  # a link with two keys to one side says nothing about which
            (up,) = ups
            offer(up.parent, (down, up))
            for beyond, path in self.parents(up.parent).items():
                offer(beyond, (down, up, *path))
        return {ref: path for ref, path in found.values()}

    def shortest_path(self, left: TableRef, right: TableRef) -> list[JoinEdge]:
        """Edges leading from ``left`` to ``right`` through the fewest tables.

        Empty when they are the same table or nothing connects them. Edges are
        followed in both directions: a link table reaches both of its parents.
        """
        start, goal = _table(left), _table(right)
        came_by: dict[_Table, tuple[_Table, JoinEdge] | None] = {start: None}
        queue = deque([start])
        while queue and goal not in came_by:
            table = queue.popleft()
            for edge in self._adjacent.get(table, ()):
                child, parent = _table(edge.child), _table(edge.parent)
                other = parent if child == table else child
                if other not in came_by:
                    came_by[other] = (table, edge)
                    queue.append(other)
        path: list[JoinEdge] = []
        step = came_by.get(goal)
        while step is not None:
            path.append(step[1])
            step = came_by[step[0]]
        return path[::-1]
