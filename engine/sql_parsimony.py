"""Deterministic parsimony variants of pooled candidates.

Measured Spider miss families (spider/results/RESULTS.md) are dominated by candidates that
join or project MORE than the question asks: distractor tables force-joined by name
mentions, and one canonical column name projected once per owning table. Ranking cannot
select a parsimonious reading that was never proposed, so this expander adds, for each
pooled candidate, (a) the same clauses rebuilt on the minimal join tree covering only the
tables those clauses actually use, (b) single-binding projection variants when a
duplicate-named column is projected from several tables, and (c) drop-one-column projection
reductions for the value-superset miss band — each re-reduced to its minimal join tree.
A mentioned table can be a semantic restrictor ("names of poker players" joins
poker_player to FILTER people), which lexical signals cannot separate from join noise, so
every variant carries a generation penalty: pooled for selection, never outranking its
parent on the prior alone. Converting pool membership into top-1 wins is ranking's job.
"""
from __future__ import annotations

import itertools
from typing import Sequence

from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    BooleanExpr,
    ColumnRef,
    Comparison,
    ExistsPredicate,
    InPredicate,
    ScalarSubquery,
    SelectItem,
    SelectQuery,
    Star,
    SubquerySource,
    render_query,
    validate_query,
)
from engine.sql_candidate import ScoredQuery
from engine.sql_expansion import ExpansionSupport

# Matches the profile-expansion generation_penalty scale (ProfileSearchConfig).
_PARSIMONY_PENALTY = 5.0
# Variant fan-out bounds: at most this many duplicate-name groups per candidate, and at most
# this many single-binding choices per group, keep the expansion pool-sized, not exponential.
_MAX_BINDING_GROUPS = 2
_MAX_GROUP_BINDINGS = 3


class ParsimonyQueryExpander(ExpansionSupport):
    """Add minimal-join and single-binding projection variants of pooled candidates."""

    def expand(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        generated: list[ScoredQuery] = []
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not _is_flat(query):
                continue
            for projected, binding_evidence in _binding_variants(query):
                reduced = self._minimal_tree(projected)
                if reduced is None:
                    continue
                variant, dropped_joins = reduced
                evidence = binding_evidence + (("tables:minimal",) if dropped_joins else ())
                if not evidence or variant == query:
                    continue
                try:
                    validate_query(variant)
                    sql = render_query(variant)
                except (TypeError, ValueError):
                    continue
                generated.append(ScoredQuery(
                    variant, sql,
                    candidate.score - _PARSIMONY_PENALTY + 0.2 * dropped_joins,
                    candidate.evidence + evidence,
                ))
        dedup: dict[str, ScoredQuery] = {}
        for candidate in generated:
            old = dedup.get(candidate.sql)
            if old is None or candidate.score > old.score:
                dedup[candidate.sql] = candidate
        return sorted(dedup.values(), key=lambda candidate: (-candidate.score, candidate.sql))[
            :self.max_candidates
        ]

    def _minimal_tree(self, query: SelectQuery) -> tuple[SelectQuery, int] | None:
        """Rebuild the query on the minimal join tree its clauses require."""
        used = _used_tables(query)
        joined = {query.from_table} | {join.table for join in query.joins}
        if used is None or not used or used == joined:
            return query, 0
        preferred = query.from_table if query.from_table in used else None
        trees = self.schema.join_trees(used, preferred_root=preferred, limit=1)
        if not trees:
            return None
        tree = trees[0]
        rebuilt = SelectQuery(
            select=query.select,
            from_table=tree.root,
            joins=tree.joins,
            where=query.where,
            group_by=query.group_by,
            having=query.having,
            order_by=query.order_by,
            limit=query.limit,
            distinct=query.distinct,
        )
        return rebuilt, len(query.joins) - len(tree.joins)


def _binding_variants(query: SelectQuery):
    """Yield the query, single-binding reductions of duplicate-named projections, and
    drop-one-column reductions (the lenient-only miss band: predicted values are a strict
    superset of gold because an extra column is projected)."""
    yield query, ()
    plain = [item.expression for item in query.select if isinstance(item.expression, ColumnRef)]
    groups: dict[str, list[ColumnRef]] = {}
    for column in plain:
        groups.setdefault(column.name.lower(), []).append(column)
    duplicated = sorted(
        (name, columns) for name, columns in groups.items() if len(columns) > 1
    )[:_MAX_BINDING_GROUPS]
    if duplicated and not any(len(columns) > _MAX_GROUP_BINDINGS for _, columns in duplicated):
        for kept in itertools.product(*(columns for _, columns in duplicated)):
            dropped = {
                column
                for _, columns in duplicated
                for column in columns
            } - set(kept)
            if not dropped:
                continue
            replacement = {column: keep
                           for keep, (_, columns) in zip(kept, duplicated)
                           for column in columns}
            select = tuple(item for item in query.select
                           if not (isinstance(item.expression, ColumnRef)
                                   and item.expression in dropped))
            group_by, seen = [], set()
            for column in query.group_by:
                column = replacement.get(column, column)
                if column not in seen:
                    seen.add(column)
                    group_by.append(column)
            yield (
                SelectQuery(
                    select=select,
                    from_table=query.from_table,
                    joins=query.joins,
                    where=query.where,
                    group_by=tuple(group_by),
                    having=query.having,
                    order_by=query.order_by,
                    limit=query.limit,
                    distinct=query.distinct,
                ),
                tuple(f"projection:binding:{column.table}.{column.name}" for column in kept),
            )
    if 2 <= len(query.select) <= 6:
        for item in query.select:
            if not isinstance(item.expression, ColumnRef):
                continue
            column = item.expression
            yield (
                SelectQuery(
                    select=tuple(other for other in query.select if other is not item),
                    from_table=query.from_table,
                    joins=query.joins,
                    where=query.where,
                    group_by=query.group_by,
                    having=query.having,
                    order_by=query.order_by,
                    limit=query.limit,
                    distinct=query.distinct,
                ),
                (f"projection:drop:{column.table}.{column.name}",),
            )


def _is_flat(query: SelectQuery) -> bool:
    """Restrict v1 to single-scope SELECTs: no subquery sources, predicates, or operands."""
    if isinstance(query.from_table, SubquerySource):
        return False
    if any(isinstance(item.expression, ScalarSubquery) for item in query.select):
        return False
    return _predicate_is_flat(query.where) and _predicate_is_flat(query.having)


def _predicate_is_flat(predicate) -> bool:
    if predicate is None:
        return True
    if isinstance(predicate, BooleanExpr):
        return all(_predicate_is_flat(term) for term in predicate.terms)
    if isinstance(predicate, (ExistsPredicate, InPredicate)):
        return isinstance(predicate, InPredicate) and isinstance(predicate.source, tuple)
    if isinstance(predicate, Comparison):
        return not (isinstance(predicate.left, ScalarSubquery)
                    or isinstance(predicate.right, ScalarSubquery))
    return True


def _used_tables(query: SelectQuery) -> set[str] | None:
    """Tables the clauses reference; None when Star projection pins every joined table."""
    used: set[str] = set()
    for item in query.select:
        if _expression_tables(item.expression, used) is None:
            return None
    for column in query.group_by:
        used.add(column.table)
    for term in query.order_by:
        if _expression_tables(term.expression, used) is None:
            return None
    for predicate in (query.where, query.having):
        if _predicate_tables(predicate, used) is None:
            return None
    return used


def _expression_tables(expression, used: set[str]) -> set[str] | None:
    if isinstance(expression, ColumnRef):
        used.add(expression.table)
        return used
    if isinstance(expression, Aggregate):
        if isinstance(expression.operand, Star):
            return used
        return _expression_tables(expression.operand, used)
    if isinstance(expression, BinaryExpr):
        if _expression_tables(expression.left, used) is None:
            return None
        return _expression_tables(expression.right, used)
    if isinstance(expression, Star):
        return None
    return used


def _predicate_tables(predicate, used: set[str]) -> set[str] | None:
    if predicate is None:
        return used
    if isinstance(predicate, BooleanExpr):
        for term in predicate.terms:
            if _predicate_tables(term, used) is None:
                return None
        return used
    if isinstance(predicate, InPredicate):
        if _expression_tables(predicate.left, used) is None:
            return None
        for value in predicate.source:
            if _expression_tables(value, used) is None:
                return None
        return used
    if isinstance(predicate, Comparison):
        if _expression_tables(predicate.left, used) is None:
            return None
        return _expression_tables(predicate.right, used)
    return used
