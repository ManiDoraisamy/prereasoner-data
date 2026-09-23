"""Map SQL text into the engine's typed AST.

One importer serves three callers: Spider gold SQL becomes proposer training targets
(``training/proposer/import_gold.py``), and at serving time every SQL line the proposer model
decodes is imported here before it may join the candidate pool (``engine/sql_proposer.py``).
Only shapes the typed AST can express are mapped; anything else raises ``Unsupported``, so model
text can never reach a database except as a validated, re-rendered AST. The importer has no
dataset access and never receives a gold answer or an execution label.
"""
from __future__ import annotations

import sqlglot
from sqlglot import expressions as sge

from engine.sql_ast import (
    Aggregate, BinaryExpr, BooleanExpr, ColumnRef, Comparison, ExistsPredicate,
    InPredicate, Join, Literal, OrderTerm, ScalarSubquery, SelectItem,
    SelectQuery, SetQuery, Star,
)
from engine.sql_schema import SchemaGraph


class Unsupported(Exception):
    """The SQL uses a shape the importer does not map; the message is a short reason key."""


class _Scope:
    """Alias resolution + typed column lookup for one SELECT scope."""

    def __init__(self, graph: SchemaGraph, parent: "_Scope | None" = None,
                 preserve_aliases: bool = False):
        self.graph = graph
        self.parent = parent
        self.preserve_aliases = preserve_aliases
        self.tables: dict[str, str] = {}       # alias/lower-name -> canonical table
        self.by_column: dict[str, list[ColumnRef]] = {}

    def add_table(self, name: str, alias: str | None):
        canonical = next((t for t in self.graph.by_table if t.lower() == name.lower()), None)
        if canonical is None:
            raise Unsupported("unknown-table")
        qualifier = (alias or name).lower()
        if qualifier in self.tables:
            raise Unsupported("duplicate-alias")
        if canonical in self.tables.values() and not self.preserve_aliases:
            # Aliases are dropped (refs are canonicalized), so a repeated table is ambiguous.
            raise Unsupported("self-join")
        self.tables[qualifier] = canonical
        if not self.preserve_aliases:
            self.tables.setdefault(name.lower(), canonical)
        for schema_column in self.graph.by_table[canonical]:
            ref = schema_column.ref
            if self.preserve_aliases:
                ref = ColumnRef(qualifier, ref.name, ref.type)
            self.by_column.setdefault(ref.name.lower(), []).append(ref)

    def column(self, qualifier: str | None, name: str) -> ColumnRef:
        if qualifier:
            table = self.tables.get(qualifier.lower())
            if table is None:
                if self.parent is not None:
                    return self.parent.column(qualifier, name)
                raise Unsupported("unknown-alias")
            for schema_column in self.graph.by_table[table]:
                if schema_column.ref.name.lower() == name.lower():
                    ref = schema_column.ref
                    return ColumnRef(qualifier.lower(), ref.name, ref.type) if self.preserve_aliases else ref
            raise Unsupported("unknown-column")
        options = self.by_column.get(name.lower(), ())
        if len({(ref.table, ref.name) for ref in options}) == 1:
            return options[0]
        if not options and self.parent is not None:
            return self.parent.column(qualifier, name)
        raise Unsupported("ambiguous-column" if options else "unknown-column")


_COMPARISONS = {
    sge.EQ: "=", sge.NEQ: "!=", sge.GT: ">", sge.GTE: ">=",
    sge.LT: "<", sge.LTE: "<=", sge.Like: "LIKE",
}
_AGGREGATES = {sge.Count: "COUNT", sge.Sum: "SUM", sge.Avg: "AVG",
               sge.Min: "MIN", sge.Max: "MAX"}
_ARITHMETIC = {sge.Add: "+", sge.Sub: "-", sge.Mul: "*", sge.Div: "/"}
_SET_OPS = {sge.Union: "UNION", sge.Intersect: "INTERSECT", sge.Except: "EXCEPT"}


def _literal(node) -> Literal:
    if isinstance(node, sge.Literal):
        if node.is_string:
            return Literal(node.this)
        text = node.this
        return Literal(float(text) if "." in str(text) else int(text))
    if isinstance(node, sge.Neg) and isinstance(node.this, sge.Literal):
        inner = _literal(node.this)
        return Literal(-inner.value)
    if isinstance(node, sge.Boolean):
        return Literal(1 if node.this else 0)
    if isinstance(node, sge.Null):
        return Literal(None)
    raise Unsupported(f"literal:{type(node).__name__}")


def _scalar(node, scope: _Scope):
    node = node.unnest() if isinstance(node, sge.Paren) else node
    if isinstance(node, sge.Column):
        try:
            return scope.column(node.table or None, node.name)
        except Unsupported as exc:
            # SQLite resolves a double-quoted token as an identifier first, then falls back
            # to a string literal; Spider gold uses "double-quoted" strings pervasively.
            if (str(exc) == "unknown-column" and not node.table
                    and isinstance(node.this, sge.Identifier) and node.this.quoted):
                return Literal(node.name)
            raise
    if isinstance(node, sge.Star):
        return Star()
    if type(node) in _AGGREGATES:
        operand, distinct = node.this, False
        if isinstance(operand, sge.Distinct):
            distinct = True
            if len(operand.expressions) != 1:
                raise Unsupported("distinct-multi-operand")
            operand = operand.expressions[0]
        return Aggregate(_AGGREGATES[type(node)], _scalar(operand, scope), distinct)
    if isinstance(node, (sge.Subquery, sge.Select)):
        return ScalarSubquery(_select(node.unnest() if isinstance(node, sge.Subquery) else node,
                                      scope.graph, scope))
    if type(node) in _ARITHMETIC:
        left = _scalar(node.left, scope)
        right = _scalar(node.right, scope)
        # BinaryExpr is numeric row/aggregate arithmetic; its operands may not themselves
        # be aggregates (the validator enforces this), so a bare Sub of two columns or
        # literals is in range while SUM(a)-SUM(b) is left to the calculation path.
        if isinstance(left, Aggregate) or isinstance(right, Aggregate):
            raise Unsupported("arithmetic:aggregate-operand")
        return BinaryExpr(left, _ARITHMETIC[type(node)], right)
    if isinstance(node, (sge.Literal, sge.Neg, sge.Boolean, sge.Null)):
        return _literal(node)
    raise Unsupported(f"scalar:{type(node).__name__}")


def _predicate(node, scope: _Scope):
    node = node.unnest() if isinstance(node, sge.Paren) else node
    if isinstance(node, sge.And):
        return BooleanExpr("AND", (_predicate(node.left, scope), _predicate(node.right, scope)))
    if isinstance(node, sge.Or):
        return BooleanExpr("OR", (_predicate(node.left, scope), _predicate(node.right, scope)))
    if isinstance(node, sge.Not):
        inner = node.this
        if isinstance(inner, sge.In):
            return _in_predicate(inner, scope, negated=True)
        if isinstance(inner, sge.Exists):
            return ExistsPredicate(_select(inner.this.unnest(), scope.graph, scope), negated=True)
        raise Unsupported(f"not:{type(inner).__name__}")
    if isinstance(node, sge.In):
        return _in_predicate(node, scope, negated=False)
    if isinstance(node, sge.Exists):
        return ExistsPredicate(_select(node.this.unnest(), scope.graph, scope), negated=False)
    if isinstance(node, sge.Between):
        column = _scalar(node.this, scope)
        return BooleanExpr("AND", (
            Comparison(column, ">=", _scalar(node.args["low"], scope)),
            Comparison(column, "<=", _scalar(node.args["high"], scope)),
        ))
    if type(node) in _COMPARISONS:
        return Comparison(_scalar(node.left, scope), _COMPARISONS[type(node)],
                          _scalar(node.right, scope))
    raise Unsupported(f"predicate:{type(node).__name__}")


def _in_predicate(node, scope: _Scope, negated: bool):
    left = _scalar(node.this, scope)
    query = node.args.get("query")
    if query is not None:
        return InPredicate(left, _select(query.unnest(), scope.graph, scope), negated=negated)
    values = tuple(_literal(value) for value in node.expressions)
    if not values:
        raise Unsupported("in:empty")
    return InPredicate(left, values, negated=negated)


def _select(node, graph: SchemaGraph, parent: _Scope | None = None) -> SelectQuery:
    if type(node) in _SET_OPS:
        return SetQuery(_select(node.this, graph, parent), _SET_OPS[type(node)],
                        _select(node.expression, graph, parent))
    if not isinstance(node, sge.Select):
        raise Unsupported(f"query:{type(node).__name__}")
    from_clause = node.args.get("from_") or node.args.get("from")
    if from_clause is None or not isinstance(from_clause.this, sge.Table):
        raise Unsupported("from:missing-or-subquery")
    root = from_clause.this
    physical = [root.name.lower()] + [j.this.name.lower() for j in node.args.get("joins", ())
                                      if isinstance(j.this, sge.Table)]
    # Ordinary queries retain their historical canonical SQL. Only repeated-table
    # scopes need alias-qualified ColumnRefs and aliased FROM/JOIN bindings.
    preserve_aliases = len(set(physical)) != len(physical)
    scope = _Scope(graph, parent, preserve_aliases)
    scope.add_table(root.name, root.alias or None)
    root_table = scope.tables[(root.alias or root.name).lower()]

    joins = []
    for join_node in node.args.get("joins", ()):  # INNER equality joins only
        side = join_node.this
        if not isinstance(side, sge.Table) or (join_node.side or "").upper() in ("LEFT", "RIGHT", "FULL"):
            raise Unsupported("join:non-inner-or-subquery")
        scope.add_table(side.name, side.alias or None)
        condition = join_node.args.get("on")
        if condition is None:
            raise Unsupported("join:missing-on")
        pairs = []
        def flatten(c):
            if isinstance(c, sge.And):
                flatten(c.left); flatten(c.right)
            elif isinstance(c, sge.EQ):
                pairs.append((_scalar(c.left, scope), _scalar(c.right, scope)))
            else:
                raise Unsupported("join:non-equality")
        flatten(condition)
        if not pairs or not all(isinstance(a, ColumnRef) and isinstance(b, ColumnRef)
                                for a, b in pairs):
            raise Unsupported("join:non-column-condition")
        joined = scope.tables[(side.alias or side.name).lower()]
        joins.append(Join(joined, pairs[0][0], pairs[0][1], "INNER",
                          (side.alias or side.name).lower() if preserve_aliases else None,
                          tuple(pairs[1:])))

    select_items = []
    for item in node.expressions:
        expression, alias = item, None
        if isinstance(item, sge.Alias):
            expression, alias = item.this, item.alias
        select_items.append(SelectItem(_scalar(expression, scope), alias))

    where = node.args.get("where")
    group = node.args.get("group")
    having = node.args.get("having")
    order = node.args.get("order")
    limit = node.args.get("limit")

    group_by = tuple(_scalar(g, scope) for g in group.expressions) if group else ()
    if any(not isinstance(g, ColumnRef) for g in group_by):
        raise Unsupported("group:non-column")
    order_terms = []
    if order:
        for ordered in order.expressions:
            expression = _scalar(ordered.this, scope)
            if not isinstance(expression, (ColumnRef, Aggregate, BinaryExpr)):
                raise Unsupported("order:expression")
            order_terms.append(OrderTerm(expression, "DESC" if ordered.args.get("desc") else "ASC"))
    limit_value = None
    if limit is not None:
        literal = _literal(limit.expression)
        limit_value = int(literal.value)

    return SelectQuery(
        select=tuple(select_items),
        from_table=root_table,
        joins=tuple(joins),
        where=_predicate(where.this, scope) if where else None,
        group_by=group_by,
        having=_predicate(having.this, scope) if having else None,
        order_by=tuple(order_terms),
        limit=limit_value,
        distinct=bool(node.args.get("distinct")),
        from_alias=(root.alias or root.name).lower() if preserve_aliases else None,
    )


def import_sql(sql: str, graph: SchemaGraph):
    """Parse SQLite-dialect SQL and map it into the typed AST, or raise ``Unsupported``.

    The input may be untrusted model output, so a parse tree the mapping cannot interpret is
    reported as unsupported rather than surfacing as an arbitrary exception.
    """
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
    except Exception as exc:  # noqa: BLE001 - every parser failure means "not importable"
        raise Unsupported(f"parse:{type(exc).__name__}") from exc
    try:
        return _select(tree, graph)
    except (AttributeError, IndexError, KeyError, RecursionError, TypeError, ValueError) as exc:
        raise Unsupported(f"malformed:{type(exc).__name__}") from exc
