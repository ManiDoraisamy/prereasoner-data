"""Readings: the per-item facts an encoder reads from (question, schema), and the deterministic builder that
assembles a typed-AST query from them.

A query is one or two blocks. Block 0 is the outer query; block 1, when present, is the other side of a set operation
or the subquery a block-0 condition compares with. Everything else is a set of facts keyed by a column (or by COUNT's
star), so each fact is one classification an encoder makes about one schema item:

    structure   single | UNION | INTERSECT | EXCEPT | NESTED             (one per question)
    per table   used in block b                                          (FROM tables, bridges included)
    per column  in block b: selected with aggregate a (slot s)           (a in plain/COUNT/SUM/AVG/MIN/MAX)
                (a condition may read a period of a date column: "month:t.c")
                            compared by operator o with value v          (v a question value, or block 1)
                            grouped by
                            in HAVING as aggregate a, operator o, value v
                            sorted by, under aggregate a
    per block   distinct, AND/OR between conditions, sort direction, limit, and whether the one shown aggregate is a
                share of a whole: "share" names the conditions that make the part ([] or absent: no share)

The builder adds nothing the readings do not say except the joins, which come from the schema's foreign-key graph.
``readings_of`` turns a typed-AST query into readings, or raises NotReadable with the reason the form cannot hold it;
``build`` turns readings back into candidate queries.
"""
from __future__ import annotations

from engine.sql_ast import (
    DATE_PARTS, Aggregate, BinaryExpr, BooleanExpr, ColumnRef, Comparison, DatePart, ExistsPredicate, InPredicate,
    Literal, Lower, OrderTerm, ScalarSubquery, SelectItem, SelectQuery, SetQuery, SQLType, Star,
)

AGGREGATES = ("plain", "COUNT", "SUM", "AVG", "MIN", "MAX")
OPERATORS = ("=", "!=", ">", "<", ">=", "<=", "LIKE", "NOT LIKE", "IN", "NOT IN")
STAR = "*"
SUBQUERY = {"block": 1}


class NotReadable(Exception):
    """The query has a form the readings cannot hold; the message is a short reason key."""


def _column(expr) -> str:
    """"t.c", "*", or "<part>:t.c" for a period of a date column ("month:orders.placed")."""
    if isinstance(expr, Star):
        return STAR
    if isinstance(expr, ColumnRef):
        return f"{expr.table}.{expr.name}"
    if isinstance(expr, DatePart):
        return f"{expr.part}:{expr.operand.table}.{expr.operand.name}"
    if isinstance(expr, Lower):
        return _column(expr.operand)
    raise NotReadable(f"operand:{type(expr).__name__}")


def _aggregated(expr) -> tuple[str, str, bool]:
    """(aggregate, column, distinct inside the aggregate) for a select, order or having operand."""
    if isinstance(expr, Aggregate):
        return expr.function, _column(expr.operand), expr.distinct
    if isinstance(expr, Star):
        raise NotReadable("bare-star")
    return "plain", _column(expr), False


def _conditions(predicate, nested: list) -> tuple[str, list]:
    """(AND|OR, [(left, operator, right)]) for a flat conjunction or disjunction."""
    if predicate is None:
        return "AND", []
    if isinstance(predicate, BooleanExpr):
        terms = []
        for term in predicate.terms:
            if isinstance(term, BooleanExpr) and term.operator != predicate.operator:
                # "x >= a AND x <= b" (a BETWEEN) inside an OR, or OR inside AND: the flat form cannot hold it
                raise NotReadable("mixed-and-or")
            terms.extend(_conditions(term, nested)[1])
        return predicate.operator, terms
    if isinstance(predicate, Comparison):
        right = predicate.right
        if isinstance(right, ScalarSubquery):
            nested.append(right.query)
            value = SUBQUERY
        elif isinstance(right, Literal):
            value = right.value
        else:
            raise NotReadable("compares-two-columns")
        operator = predicate.operator
        if operator not in OPERATORS:
            raise NotReadable(f"operator:{operator}")
        return "AND", [(predicate.left, operator, value)]
    if isinstance(predicate, InPredicate):
        if isinstance(predicate.source, tuple):
            raise NotReadable("in-list")
        nested.append(predicate.source)
        return "AND", [(predicate.left, "NOT IN" if predicate.negated else "IN", SUBQUERY)]
    if isinstance(predicate, ExistsPredicate):
        raise NotReadable("exists")
    raise NotReadable(f"predicate:{type(predicate).__name__}")


def _block(query, nested: list) -> dict:
    if isinstance(query, SetQuery):
        raise NotReadable("set-inside-block")
    if not isinstance(query.from_table, str):
        raise NotReadable("from-subquery")
    if query.from_alias or any(join.alias for join in query.joins):
        raise NotReadable("self-join")
    tables = sorted({query.from_table, *(join.table for join in query.joins)})
    select = []
    for slot, item in enumerate(query.select):
        aggregate, column, distinct = _aggregated(item.expression)
        select.append([slot, aggregate, column, distinct])
    conj, raw = _conditions(query.where, nested)
    where = []
    for left, operator, value in raw:
        if isinstance(left, Aggregate):
            raise NotReadable("aggregate-in-where")
        where.append([_column(left), operator, value])
    having_conj, raw = _conditions(query.having, nested)
    having = []
    for left, operator, value in raw:
        aggregate, column, distinct = _aggregated(left)
        if distinct:
            raise NotReadable("distinct-aggregate-in-having")
        having.append([aggregate, column, operator, value])
    if having and len(having) > 1 and having_conj != "AND":
        raise NotReadable("having-or")
    directions = {term.direction for term in query.order_by}
    if len(directions) > 1:
        raise NotReadable("mixed-directions")
    order = []
    for term in query.order_by:
        aggregate, column, distinct = _aggregated(term.expression)
        if distinct:
            raise NotReadable("distinct-aggregate-in-order")
        order.append([aggregate, column])
    return {"tables": tables, "distinct": query.distinct, "select": select, "where": sorted(where, key=repr),
            "conj": conj if len(where) > 1 else "AND", "group": sorted(_column(g) for g in query.group_by),
            "having": sorted(having, key=repr), "order": order,
            "dir": next(iter(directions)) if directions else None, "limit": query.limit}


def readings_of(query) -> dict:
    """The readings of a typed-AST query: structure plus one or two blocks."""
    if isinstance(query, SetQuery):
        if isinstance(query.left, SetQuery) or isinstance(query.right, SetQuery):
            raise NotReadable("three-blocks")
        nested: list = []
        left, right = _block(query.left, nested), _block(query.right, nested)
        if nested:
            raise NotReadable("set-with-subquery")
        return {"structure": query.operator, "blocks": [left, right]}
    nested = []
    outer = _block(query, nested)
    if not nested:
        return {"structure": "single", "blocks": [outer]}
    if len(nested) > 1:
        raise NotReadable("two-subqueries")
    inner_nested: list = []
    inner = _block(nested[0], inner_nested)
    if inner_nested:
        raise NotReadable("three-blocks")
    return {"structure": "NESTED", "blocks": [outer, inner]}


# ---------------------------------------------------------------------------------------------------------------- build

def _ref(graph, column: str):
    if column == STAR:
        return Star()
    part, _, rest = column.partition(":")
    if rest and part in DATE_PARTS:
        return DatePart(part, _ref(graph, rest))
    table, name = column.split(".", 1)
    schema_column = graph.column_map.get((table, name))
    if schema_column is None:
        raise NotReadable("unknown-column")
    return schema_column.ref


def _operand(graph, aggregate: str, column: str, distinct: bool = False):
    ref = _ref(graph, column)
    if aggregate == "plain":
        if isinstance(ref, Star):
            raise NotReadable("bare-star")
        return ref
    return Aggregate(aggregate, ref, distinct)


def _value(value, subquery):
    if value == SUBQUERY:
        if subquery is None:
            raise NotReadable("missing-subquery")
        return subquery
    return Literal(value)


def value_kind(expr):
    """"number" or "text" for a column of either type (or an aggregate of one), else None (unknown, dates)."""
    if isinstance(expr, Aggregate):
        return "number" if expr.function in ("COUNT", "SUM", "AVG") else value_kind(expr.operand)
    if isinstance(expr, ColumnRef):
        if expr.type in (SQLType.INTEGER, SQLType.REAL):
            return "number"
        if expr.type == SQLType.TEXT:
            return "text"
    return None


def _same_kind(left, subquery) -> None:
    """A set membership compares values of one kind: "product id is not among (the product names Paris customers
    bought)" compares a number with names, which never match, so every row passes (2026-10-22). The search reads
    such a membership again on the column the listed values are values of (engine.readers.search.referenced)."""
    inner = subquery.query if isinstance(subquery, ScalarSubquery) else subquery
    shown = getattr(inner, "select", ())
    if not shown:
        return
    kinds = {value_kind(left), value_kind(shown[0].expression)}
    if kinds == {"number", "text"}:
        raise NotReadable("in-compares-number-with-text")


def _predicate(conditions, conj):
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else BooleanExpr(conj, tuple(conditions))


def _join_options(graph, tables: list[str]) -> list[tuple[str, tuple]]:
    """(root, joins) for each foreign-key tree over exactly the block's tables, fewest joins first; when no tree has
    exactly those tables, the trees that add bridge tables."""
    if len(tables) == 1:
        return [(tables[0], ())]
    trees = graph.join_trees(tables, limit=8)
    exact = [tree for tree in trees if len(tree.joins) == len(tables) - 1]
    chosen = exact or list(trees)
    if not chosen:
        raise NotReadable("no-join-path")
    return [(tree.root, tree.joins) for tree in chosen]


def _build_block(graph, block: dict, subquery=None, joins_choice: int = 0) -> SelectQuery:
    options = _join_options(graph, block["tables"])
    root, joins = options[min(joins_choice, len(options) - 1)]
    # A shown period is headed by the period it is, "month", not by its SQL.
    operands = [_operand(graph, aggregate, column, distinct)
                for _, aggregate, column, distinct in sorted(block["select"], key=lambda item: item[0])]
    select = tuple(SelectItem(operand, "month" if isinstance(operand, DatePart) else None) for operand in operands)
    where = []
    for column, operator, value in block["where"]:
        left = _ref(graph, column)
        if operator in ("IN", "NOT IN"):
            if subquery is None:
                raise NotReadable("missing-subquery")
            _same_kind(left, subquery)
            where.append(InPredicate(left, subquery.query if isinstance(subquery, ScalarSubquery) else subquery,
                                     negated=operator == "NOT IN"))
        elif operator in ("LIKE", "NOT LIKE") and isinstance(value, str) and isinstance(left, ColumnRef):
            # A text a column's values contain, in any case: LOWER(column) LIKE '%west%' reads alike on SQLite, where
            # the pool runs, and PostgreSQL, whose LIKE alone is case-sensitive (sql_ast.Lower).
            where.append(Comparison(Lower(left), operator, Literal(value.lower())))
        else:
            where.append(Comparison(left, operator, _value(value, subquery)))
    having = [Comparison(_operand(graph, aggregate, column), operator, _value(value, subquery))
              for aggregate, column, operator, value in block["having"]]
    order = tuple(OrderTerm(_operand(graph, aggregate, column), block["dir"] or "ASC")
                  for aggregate, column in block["order"])
    # A grouped block shows and sorts by the group's own fields: a plain column it shows or sorts by is grouped too
    # ("the title of each course with two prerequisites" groups by the course and shows its title), which leaves
    # the groups unchanged when the column depends on the group key, as a name depends on its row's id.
    group = [_ref(graph, column) for column in block["group"]]
    distinct = block["distinct"]
    if group:
        # Groups are ordered by a value of each group: a plain number column a grouped block sorts by but neither
        # groups nor shows is each group's total ("the top 2 customers by total spend" orders each customer by
        # SUM(line_total)). Grouped by it as well, each group would split into one row per value.
        shown = {item.expression for item in select if isinstance(item.expression, ColumnRef)}
        order = tuple(OrderTerm(Aggregate("SUM", term.expression), term.direction)
                      if isinstance(term.expression, ColumnRef) and term.expression not in group
                      and term.expression not in shown and term.expression.type.numeric else term
                      for term in order)
        for expression in [item.expression for item in select] + [term.expression for term in order]:
            if isinstance(expression, ColumnRef) and expression not in group:
                group.append(expression)
        # Groups with no aggregate anywhere are the distinct rows of what they show: SELECT DISTINCT is the same
        # query, and both deterministic emitters express it (a GROUP BY without an aggregate they do not).
        aggregated = having or any(isinstance(expression, Aggregate) for expression in
                                   [item.expression for item in select] + [term.expression for term in order])
        if not aggregated and set(group) <= shown:
            group, distinct = [], True
    limit = block["limit"]
    if (not group and not having and limit == 1 and len(select) == 1 and len(order) == 1
            and isinstance(select[0].expression, ColumnRef) and order[0].expression == select[0].expression):
        # The highest or lowest value of the one column shown and ranked is its MAX or MIN: the same value, NULLs
        # aside (which MIN skips and an ascending order puts first), and an aggregate both deterministic emitters
        # express ("What is the highest purchase cost?").
        function = "MAX" if order[0].direction == "DESC" else "MIN"
        select, order, limit, distinct = (SelectItem(Aggregate(function, select[0].expression)),), (), None, False
    if block.get("share"):
        return _share(graph, block, root, joins, select, where)
    return SelectQuery(select=select, from_table=root, joins=tuple(joins), where=_predicate(where, block["conj"]),
                       group_by=tuple(group),
                       having=_predicate(having, "AND"), order_by=order, limit=limit,
                       distinct=distinct)


def _share(graph, block, root, joins, select, where):
    """A share: the block's one aggregate over the rows every condition keeps, divided by the same aggregate over
    the rows only the whole's conditions keep (the conditions ``block["share"]`` does not name). "What share of the
    total amount comes from Paris" is SUM(amount) over the Paris rows / SUM(amount) over all; "the percentage of the
    ratings for movie X that score 5" keeps "movie X" in both."""
    if len(select) != 1 or not isinstance(select[0].expression, Aggregate) or block["conj"] != "AND":
        raise NotReadable("share-shape")
    aggregate = select[0].expression
    part = set(block["share"])
    whole = SelectQuery(select=(SelectItem(aggregate),), from_table=root, joins=tuple(joins),
                        where=_predicate([w for i, w in enumerate(where) if i not in part], "AND"))
    return SelectQuery(select=(SelectItem(BinaryExpr(aggregate, "/", ScalarSubquery(whole)), "share"),),
                       from_table=root, joins=tuple(joins), where=_predicate(where, "AND"))


def build(readings: dict, graph):
    """The query the readings describe, joined along the schema's foreign keys."""
    blocks = readings["blocks"]
    structure = readings["structure"]
    if structure == "single":
        return _build_block(graph, blocks[0])
    if structure in ("UNION", "INTERSECT", "EXCEPT"):
        return SetQuery(_build_block(graph, blocks[0]), structure, _build_block(graph, blocks[1]))
    inner = _build_block(graph, blocks[1])
    return _build_block(graph, blocks[0], subquery=ScalarSubquery(inner))
