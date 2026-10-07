"""Grounding: a pool member is eligible only when its literals and its joins fit the request's data.

Literal grounding: a text literal compared with a column must be a value that column can hold.

Question values are linked against the request's data locally; they are not sent to Gemini for
rewriting (engine/sql_prompt.py). A comparison is mis-grounded when its literal occurs in no row of
its own column but does occur in another column of the request's tables. Such a query answers a
different question, so it is never eligible for selection (``TableQuery.select_query``). A literal
that occurs in no column is left alone: the question may name a value the data does not hold, and the
honest answer is then empty.

Checked: a text column tested against a text literal with `=`, `!=`, `<>`, `IN` or `NOT IN`, in
either operand order (`'Lyon' = city` is the same test as `city = 'Lyon'`). Matching is case- and
whitespace-insensitive. Numeric, date and pattern (`LIKE`) comparisons are out of scope.

Exclusions are checked like equality. `customer_name != 'Lyon'` on data where Lyon is only a city
excludes nothing: it is the Lyon mis-binding in negated form, and "customers not from Lyon" would list
everyone. The accepted cost: when two columns share a domain (a ship city and a billing city) and the
question names the one that legitimately lacks the value while its sibling holds it, the right reading
is ineligible, so a grounded member is selected instead or the question is not answered. A SQL model
mis-binds values far more often than a question names an empty value of one of two same-kind columns.

Join grounding: a join must not equate two columns the foreign keys keep apart.

For complex-promotions' "for each customer, list every product name they have ever bought" a SQL
model joined ``products ON orders.order_id = products.product_id``, skipping the ``order_items``
bridge the discovered foreign keys state. The query ran and matched no row, it was served, and the
anti-join it fed removed nothing (2026-10-01). A pair of equated columns from two
different tables is mis-joined when the foreign keys connect the two tables, the columns are not
one key under them (the foreign-key column pairs, closed transitively), and either both columns
are foreign-key columns, so the keys name each one and tell them apart, or one of them is a key
and the two share no value in the request's data, so the join can never match.

Left alone: tables no foreign key connects (the relationship may be undiscovered), self joins, a
shortcut through a shared parent key (``city.country_code = language.country_code``, both
referencing ``country.code``), a key column named for a role the discovery did not resolve whose
values overlap the other side (``orders.ship_to_id = customers.customer_id``), and attribute joins
(``orders.order_date = customers.signup_date``): the question may relate any values, and the
honest answer is then whatever matches.

Checked: every ``JOIN ... ON`` column pair and every ``=`` between two columns in a WHERE or HAVING
clause, in every scope, with qualifiers resolved to their physical tables. Columns of derived
tables are skipped: their lineage is computed, not uploaded.

Double counting: a SUM or AVG that reads only rows its joins repeat (``double_counted``).

Joined on a column that holds one value in several rows, a row of the other table appears once per
such row: a report row joined to its subscriptions appears once per subscription. A SUM or AVG that
reads only the repeated rows counts each of them once per match. For "what is the total Amount
broken down by Plan and Currency" the search's best reading summed a report tab's Total Amount once
per subscription the report row matched: 40 for a product whose subscriptions total 20
(2026-10-05). This is not an eligibility rule: a price summed over the order lines that repeat it,
or a station's latitude averaged over the trips that start there, reads the repeats on purpose: 49
of the 6,953 Spider train gold queries the importer reads do, and none of the 1,026 dev ones.
Selection serves a double-counting member only when every eligible member double counts
(``engine/sql_rank.select_ranked_candidate``).

Checked in every SELECT against its own joins: each SUM or AVG without DISTINCT whose operand reads
only columns of tables the joins repeat. A table's rows repeat when a join, followed outward from it
through the SELECT's joins, matches columns that hold one join value (one combination, for a
composite key) in more than one row of the request's data. Left alone: COUNT, which counts the
joined rows whichever column it names (COUNT(*) counts the same rows); MIN, MAX and DISTINCT
aggregates, which repeats do not change; an operand that reads a table the joins do not repeat
(``SUM(orders.quantity * products.price)`` is computed once per order); and the repeats a derived
table's columns would make, whose values are computed, not uploaded.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass

from engine.relations import join_keys
from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    BooleanExpr,
    ColumnRef,
    Comparison,
    ExistsPredicate,
    InPredicate,
    Literal,
    ScalarSubquery,
    SelectQuery,
    SetQuery,
    SQLType,
    SubquerySource,
    contradictory,
)
from engine.sql_schema import is_surrogate_key

_EQUALITY = frozenset({"=", "!=", "<>"})
_TEXT_COLUMNS = frozenset({SQLType.TEXT, SQLType.UNKNOWN})
# The aggregates a repeated row changes: MIN and MAX read each value whatever its repeats, and COUNT
# counts the joined rows whichever column it names.
_REPEAT_SENSITIVE = frozenset({"SUM", "AVG"})

# (physical table, column, literal text)
Binding = tuple[str, str, str]
# (physical table, column)
Column = tuple[str, str]
# two columns of different physical tables that a query equates
JoinPair = tuple[Column, Column]


def literal_bindings(query) -> tuple[Binding, ...]:
    """Every equality, exclusion or membership test of a text column against a text literal in
    ``query``, with the column's qualifier resolved to its physical table. Columns of derived tables
    are skipped: their values are computed, not uploaded."""
    return tuple(_walk(query).bindings)


def join_pairs(query) -> tuple[JoinPair, ...]:
    """Every pair of columns of two different physical tables that ``query`` equates: each
    ``JOIN ... ON`` pair and each ``=`` between two columns, in every scope."""
    return tuple(_walk(query).pairs)


def double_counted(query, tables: Mapping[str, dict]) -> tuple[Aggregate, ...]:
    """Every SUM or AVG in ``query`` that reads only rows its joins repeat in ``tables``
    (name -> {"columns", "rows"}), the request's tables."""
    return _JoinedRows(tables).double_counted(query)


def double_counted_members(pool: Sequence, tables: Mapping[str, dict]) -> tuple[bool, ...]:
    """Per pool member (a ``ScoredQuery``): whether any of its SUM or AVG aggregates reads only rows
    its joins repeat in ``tables``, the request's tables."""
    rows = _JoinedRows(tables)
    return tuple(bool(rows.double_counted(candidate.query)) for candidate in pool)


def grounded_members(pool: Sequence, tables: Mapping[str, dict], graph) -> tuple[bool, ...]:
    """Per pool member (a ``ScoredQuery``): False when any of its literal tests is mis-grounded
    against ``tables`` (name -> {"columns", "rows"}), the request's tables, or when any of its joins
    equates two columns the foreign keys of ``graph`` (the request's ``SchemaGraph``) keep apart."""
    facts = [_walk(candidate.query) for candidate in pool]
    wanted = {_fold(text) for fact in facts for _, _, text in fact.bindings}
    holders = _value_holders(tables, wanted) if wanted else {}
    keys = _ForeignKeyClasses(graph, tables)
    return tuple(
        all(_grounded(binding, holders) for binding in fact.bindings)
        and not any(keys.contradicts(left, right) for left, right in fact.pairs)
        and not contradictory(candidate.query)
        for fact, candidate in zip(facts, pool)
    )


def _grounded(binding: Binding, holders: Mapping[str, set[tuple[str, str]]]) -> bool:
    table, column, text = binding
    places = holders.get(_fold(text))
    return not places or (table, column) in places


def _value_holders(tables: Mapping[str, dict], wanted: set[str]) -> dict[str, set[tuple[str, str]]]:
    """Folded literal -> the (table, column) pairs whose values contain it."""
    holders: dict[str, set[tuple[str, str]]] = {}
    for name, table in tables.items():
        columns = table["columns"]
        for row in table["rows"]:
            for column, value in zip(columns, row):
                if value is None:
                    continue
                folded = _fold(value)
                if folded in wanted:
                    holders.setdefault(folded, set()).add((name, column))
    return holders


def _fold(value) -> str:
    return " ".join(str(value).split()).casefold()


class _ForeignKeyClasses:
    """The keys the foreign keys state, the tables they connect, and the request's join values."""

    def __init__(self, graph, tables: Mapping[str, dict]):
        column_parent: dict[Column, Column] = {}
        table_parent: dict[str, str] = {}
        for foreign_key in graph.foreign_keys:
            for left, right in foreign_key.column_pairs:
                _union(column_parent, (left.table, left.name), (right.table, right.name))
            _union(table_parent, foreign_key.from_column.table, foreign_key.to_column.table)
        self._key = {column: _find(column_parent, column) for column in tuple(column_parent)}
        self._component = {table: _find(table_parent, table) for table in tuple(table_parent)}
        self._tables = tables
        self._values: dict[Column, frozenset[str]] = {}

    def contradicts(self, left: Column, right: Column) -> bool:
        """Whether equating ``left`` and ``right``, columns of two different tables, contradicts the
        foreign keys."""
        component = self._component.get(left[0])
        if component is None or component != self._component.get(right[0]):
            return False
        left_key, right_key = self._key.get(left), self._key.get(right)
        if left_key is not None and left_key == right_key:
            return False
        if left_key is not None and right_key is not None:
            return True
        if left_key is None and right_key is None and not (
                is_surrogate_key(left[1]) or is_surrogate_key(right[1])):
            return False
        a, b = self._column_values(left), self._column_values(right)
        return bool(a) and bool(b) and a.isdisjoint(b)

    def _column_values(self, column: Column) -> frozenset[str]:
        if column not in self._values:
            table = self._tables.get(column[0])
            values: frozenset[str] = frozenset()
            if table is not None and column[1] in table["columns"]:
                index = list(table["columns"]).index(column[1])
                values = frozenset(
                    key for key in join_keys([row[index] if index < len(row) else None for row in table["rows"]])
                    if key is not None
                )
            self._values[column] = values
        return self._values[column]


class _JoinedRows:
    """Which rows a SELECT's joins repeat, read from the request's join values."""

    def __init__(self, tables: Mapping[str, dict]):
        self._tables = tables
        self._repeats: dict[tuple[str | None, tuple[str, ...]], bool] = {}

    def double_counted(self, query) -> tuple[Aggregate, ...]:
        found: list[Aggregate] = []
        self._collect(query, found)
        return tuple(dict.fromkeys(found))

    def _collect(self, query, found: list[Aggregate]) -> None:
        if isinstance(query, SetQuery):
            self._collect(query.left, found)
            self._collect(query.right, found)
            return
        for part in fields(query):
            for inner in _within(getattr(query, part.name), (SelectQuery, SetQuery)):
                self._collect(inner, found)
        terms = (tuple(item.expression for item in query.select),
                 tuple(term.expression for term in query.order_by), query.having)
        aggregates = []
        for aggregate in _within(terms, Aggregate):
            columns = tuple(_within(aggregate.operand, ColumnRef))
            if aggregate.function in _REPEAT_SENSITIVE and not aggregate.distinct and columns:
                aggregates.append((aggregate, columns))
        if not query.joins or not aggregates:
            return
        repeated = self._repeated(query)
        found.extend(aggregate for aggregate, columns in aggregates
                     if all(repeated(column.table) for column in columns))

    def _repeated(self, query) -> Callable[[str], bool]:
        """Whether the SELECT's joins repeat the rows of the table one of its qualifiers names."""
        scope: dict[str, str | None] = {}
        if isinstance(query.from_table, SubquerySource):
            scope[query.from_table.alias] = None
        else:
            scope[query.from_alias or query.from_table] = query.from_table
        for join in query.joins:
            scope[join.alias or join.table] = join.table
        keys: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for join in query.joins:
            for left, right in join.predicates:
                if left.table == right.table or left.table not in scope or right.table not in scope:
                    continue
                if left.table > right.table:
                    left, right = right, left
                keys.setdefault((left.table, right.table), []).append((left.name, right.name))
        # qualifier -> [(the qualifier it joins, the columns of that one it matches)]
        edges: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
        for (one, other), names in keys.items():
            edges.setdefault(one, []).append((other, tuple(name for _, name in names)))
            edges.setdefault(other, []).append((one, tuple(name for name, _ in names)))

        def repeated(qualifier: str) -> bool:
            # Outward only: the join a table was reached through already matched one row per row.
            seen, frontier = {qualifier}, [qualifier]
            while frontier:
                for joined, names in edges.get(frontier.pop(), ()):
                    if joined in seen:
                        continue
                    if self._holds_repeats(scope[joined], names):
                        return True
                    seen.add(joined)
                    frontier.append(joined)
            return False

        return repeated

    def _holds_repeats(self, table: str | None, names: tuple[str, ...]) -> bool:
        """Whether columns ``names`` of ``table`` hold one join value (one combination of values) in
        more than one row. A derived table's (None) values are computed, not uploaded: not read."""
        key = (table, names)
        if key not in self._repeats:
            data = self._tables.get(table) if table is not None else None
            repeats = False
            if data is not None and all(name in data["columns"] for name in names):
                indexes = [list(data["columns"]).index(name) for name in names]
                columns = [join_keys([row[index] if index < len(row) else None for row in data["rows"]])
                           for index in indexes]
                values = list(zip(*columns))
                values = [value for value in values if None not in value]
                repeats = len(set(values)) < len(values)
            self._repeats[key] = repeats
        return self._repeats[key]


def _within(node, kinds) -> Iterator:
    """The nodes of ``kinds`` in ``node``, an AST node or a tuple of them, outside nested queries."""
    if isinstance(node, kinds):
        yield node
    elif isinstance(node, (tuple, list)):
        for item in node:
            yield from _within(item, kinds)
    elif is_dataclass(node) and not isinstance(node, (type, SelectQuery, SetQuery)):
        for part in fields(node):
            yield from _within(getattr(node, part.name), kinds)


def _find(parent: dict, item):
    parent.setdefault(item, item)
    while parent[item] != item:
        parent[item] = parent[parent[item]]
        item = parent[item]
    return item


def _union(parent: dict, left, right) -> None:
    left, right = _find(parent, left), _find(parent, right)
    if left != right:
        parent[max(left, right)] = min(left, right)


@dataclass
class _Facts:
    bindings: list[Binding] = field(default_factory=list)
    pairs: list[JoinPair] = field(default_factory=list)


def _walk(query) -> _Facts:
    facts = _Facts()
    _walk_query(query, {}, facts)
    return facts


def _walk_query(query, outer: Mapping[str, str | None], out: _Facts) -> None:
    if isinstance(query, SetQuery):
        _walk_query(query.left, outer, out)
        _walk_query(query.right, outer, out)
        return
    scope = dict(outer)                      # an inner qualifier shadows an outer one
    if isinstance(query.from_table, SubquerySource):
        _walk_query(query.from_table.query, {}, out)
        scope[query.from_table.alias] = None
    else:
        scope[query.from_alias or query.from_table] = query.from_table
    for join in query.joins:
        scope[join.alias or join.table] = join.table
    for join in query.joins:
        for left, right in join.predicates:
            _pair(left, right, scope, out)
    for item in query.select:
        _walk_expr(item.expression, scope, out)
    for term in query.order_by:
        _walk_expr(term.expression, scope, out)
    _walk_predicate(query.where, scope, out)
    _walk_predicate(query.having, scope, out)


def _walk_expr(expr, scope: Mapping[str, str | None], out: _Facts) -> None:
    if isinstance(expr, ScalarSubquery):
        _walk_query(expr.query, scope, out)
    elif isinstance(expr, Aggregate):
        _walk_expr(expr.operand, scope, out)
    elif isinstance(expr, BinaryExpr):
        _walk_expr(expr.left, scope, out)
        _walk_expr(expr.right, scope, out)


def _walk_predicate(predicate, scope: Mapping[str, str | None], out: _Facts) -> None:
    if predicate is None:
        return
    if isinstance(predicate, BooleanExpr):
        for term in predicate.terms:
            _walk_predicate(term, scope, out)
    elif isinstance(predicate, ExistsPredicate):
        _walk_query(predicate.query, scope, out)
    elif isinstance(predicate, InPredicate):
        _walk_expr(predicate.left, scope, out)
        if isinstance(predicate.source, tuple):
            for value in predicate.source:
                _bind(predicate.left, value, scope, out)
        else:
            _walk_query(predicate.source, scope, out)
    elif isinstance(predicate, Comparison):
        _walk_expr(predicate.left, scope, out)
        _walk_expr(predicate.right, scope, out)
        if predicate.operator in _EQUALITY:
            # Both operand orders: imported model SQL may put the literal first.
            _bind(predicate.left, predicate.right, scope, out)
            _bind(predicate.right, predicate.left, scope, out)
        if predicate.operator == "=":
            _pair(predicate.left, predicate.right, scope, out)


def _bind(column, literal, scope: Mapping[str, str | None], out: _Facts) -> None:
    if not (isinstance(column, ColumnRef) and isinstance(literal, Literal)):
        return
    if column.type not in _TEXT_COLUMNS or not isinstance(literal.value, str):
        return
    if not literal.value.strip():
        return
    table = scope.get(column.table)
    if table is not None:
        out.bindings.append((table, column.name, literal.value))


def _pair(left, right, scope: Mapping[str, str | None], out: _Facts) -> None:
    if not (isinstance(left, ColumnRef) and isinstance(right, ColumnRef)):
        return
    left_table, right_table = scope.get(left.table), scope.get(right.table)
    if left_table is None or right_table is None or left_table == right_table:
        return
    out.pairs.append(((left_table, left.name), (right_table, right.name)))
