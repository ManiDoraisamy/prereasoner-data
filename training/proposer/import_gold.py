"""Import Spider TRAIN gold SQL into the engine's typed AST, execution-verified.

An example is covered only when the imported AST validates, renders through the engine's
own renderer, executes on the capped tables, and strict-matches the gold execution. The
coverage percentage is Phase D's ceiling; the rejection histogram says which AST mapping
to add next. Training-time only — never imported by engine code, dev split never read.

Usage:
    python -m training.proposer.import_gold --limit 1000        # coverage sample
    python -m training.proposer.import_gold \
        --targets training/proposer/data/experiments/d1/targets.jsonl   # full + targets
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sqlglot
from sqlglot import expressions as sge

from engine.sql_ast import (
    Aggregate,
    BooleanExpr,
    ColumnRef,
    Comparison,
    ExistsPredicate,
    InPredicate,
    Join,
    Literal,
    OrderTerm,
    ScalarSubquery,
    SelectItem,
    SelectQuery,
    SetQuery,
    Star,
    render_query,
    validate_query,
)
from engine.sql_schema import SchemaGraph
from spider.probe.evalutil import build_mem_db, exec_sql_timed, load_capped
from spider.probe.full_eval import _git_provenance
from spider.probe.spider_eval import compare, spider_foreign_keys


class Unsupported(Exception):
    """Gold uses a shape the importer does not map yet; the reason is the histogram key."""


class _Scope:
    """Alias resolution + typed column lookup for one SELECT scope."""

    def __init__(self, graph: SchemaGraph, parent: "_Scope | None" = None):
        self.graph = graph
        self.parent = parent
        self.tables: dict[str, str] = {}       # alias/lower-name -> canonical table
        self.by_column: dict[str, list[ColumnRef]] = {}

    def add_table(self, name: str, alias: str | None):
        canonical = next((t for t in self.graph.by_table if t.lower() == name.lower()), None)
        if canonical is None:
            raise Unsupported("unknown-table")
        if canonical in self.tables.values():
            # Aliases are dropped (refs are canonicalized), so a repeated table is ambiguous.
            raise Unsupported("self-join")
        self.tables[(alias or name).lower()] = canonical
        self.tables.setdefault(name.lower(), canonical)
        for schema_column in self.graph.by_table[canonical]:
            self.by_column.setdefault(schema_column.ref.name.lower(), []).append(schema_column.ref)

    def column(self, qualifier: str | None, name: str) -> ColumnRef:
        if qualifier:
            table = self.tables.get(qualifier.lower())
            if table is None:
                if self.parent is not None:
                    return self.parent.column(qualifier, name)
                raise Unsupported("unknown-alias")
            for schema_column in self.graph.by_table[table]:
                if schema_column.ref.name.lower() == name.lower():
                    return schema_column.ref
            raise Unsupported("unknown-column")
        options = [ref for ref in self.by_column.get(name.lower(), ())
                   if ref.table in set(self.tables.values())]
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
        except Unsupported:
            # SQLite resolves a double-quoted token as an identifier first, then falls back
            # to a string literal; Spider gold uses "double-quoted" strings pervasively.
            if not node.table and isinstance(node.this, sge.Identifier) and node.this.quoted:
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
    scope = _Scope(graph, parent)

    from_clause = node.args.get("from_") or node.args.get("from")
    if from_clause is None or not isinstance(from_clause.this, sge.Table):
        raise Unsupported("from:missing-or-subquery")
    root = from_clause.this
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
                          None, tuple(pairs[1:])))

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
            if not isinstance(expression, (ColumnRef, Aggregate)):
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
    )


def import_gold_sql(gold_sql: str, graph: SchemaGraph):
    """Parse gold SQL and map it into the engine's typed AST (raises Unsupported)."""
    try:
        tree = sqlglot.parse_one(gold_sql, read="sqlite")
    except Exception as exc:  # noqa: BLE001
        raise Unsupported(f"parse:{type(exc).__name__}") from exc
    return _select(tree, graph)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "spider", "data"))
    ap.add_argument("--dbs", default=os.path.join(ROOT, "spider", "data", "dbs"))
    ap.add_argument("--split", default="train_spider.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cap", type=int, default=5000)
    ap.add_argument("--targets", default="",
                    help="write covered (schema, question, rendered SQL) SFT targets here")
    args = ap.parse_args()
    if args.split.startswith("dev"):
        ap.error("dev split is evaluation-only")

    with open(os.path.join(args.data, args.split), encoding="utf-8") as handle:
        examples = json.load(handle)
    with open(os.path.join(args.data, "tables.json"), encoding="utf-8") as handle:
        tables_meta = {table["db_id"]: table for table in json.load(handle)}
    if args.limit:
        examples = examples[:args.limit]

    db_cache: dict[str, tuple] = {}
    outcomes = collections.Counter()
    reasons = collections.Counter()
    out = None
    if args.targets:
        os.makedirs(os.path.dirname(args.targets), exist_ok=True)
        out = open(args.targets, "w", encoding="utf-8")
        out.write(json.dumps({"_meta": {"split": args.split, "cap": args.cap,
                                        **_git_provenance(ROOT)}}) + "\n")

    for i, example in enumerate(examples):
        db_id = example["db_id"]
        db_path = os.path.join(args.dbs, db_id + ".sqlite")
        if not os.path.exists(db_path):
            outcomes["missing_db"] += 1
            continue
        if db_id not in db_cache:
            capped = load_capped(db_path, cap=args.cap)
            fks = spider_foreign_keys(tables_meta[db_id])
            graph = SchemaGraph.from_tables(list(capped.values()), fks)
            db_cache[db_id] = (capped, build_mem_db(list(capped.values())), graph)
        capped, connection, graph = db_cache[db_id]
        gold_rows, gold_error = exec_sql_timed(connection, example["query"], timeout=8.0)
        if gold_error or gold_rows is None:
            outcomes["gold_error"] += 1
            continue
        try:
            query = import_gold_sql(example["query"], graph)
            validate_query(query)
            rendered = render_query(query)
        except Unsupported as exc:
            outcomes["unsupported"] += 1
            reasons[str(exc).split("'")[0][:40]] += 1
            continue
        except (TypeError, ValueError) as exc:
            outcomes["invalid_ast"] += 1
            reasons[f"validate:{str(exc)[:34]}"] += 1
            continue
        rows, error = exec_sql_timed(connection, rendered, timeout=8.0)
        if error or rows is None:
            outcomes["render_exec_error"] += 1
            reasons[f"exec:{str(error)[:34]}"] += 1
            continue
        if compare(gold_rows, rows).get("strict"):
            outcomes["covered"] += 1
            if out is not None:
                out.write(json.dumps({"idx": i, "db_id": db_id,
                                      "question": example["question"],
                                      "sql": rendered}) + "\n")
        else:
            outcomes["denotation_mismatch"] += 1
        if (i + 1) % 500 == 0:
            print(f"  {i + 1}/{len(examples)}  {dict(outcomes)}", flush=True)
    if out is not None:
        out.close()

    total = sum(outcomes.values())
    print(f"\ncoverage: {outcomes['covered']}/{total} "
          f"({round(100 * outcomes['covered'] / max(total, 1), 1)}%)  {dict(outcomes)}")
    print("top rejection reasons:")
    for reason, count in reasons.most_common(12):
        print(f"  {reason:44s} {count}")


if __name__ == "__main__":
    main()
