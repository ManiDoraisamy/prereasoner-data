"""Deterministic projection/table variants of pooled candidates.

Measured Spider miss families (spider/results/RESULTS.md) are dominated by candidates whose
projection or join set differs from the asked reading by ONE bounded edit. Ranking cannot
select a reading that was never proposed, so this expander adds, for each pooled candidate:
(a) the same clauses rebuilt on the minimal join tree covering only the tables those clauses
actually use, (b) single-binding variants when a duplicate-named column is projected from
several tables, (c) drop-one and drop-two column reductions (value-superset misses),
(d) add-one-column variants from question-linked columns of already-joined tables
(value-subset misses), (e) aggregate-operand swaps to question-linked same-table columns,
and (f) a DISTINCT toggle — each re-reduced to its minimal join tree. A mentioned table can
be a semantic restrictor ("names of poker players" joins poker_player to FILTER people),
which lexical signals cannot separate from join noise, so every variant carries a generation
penalty: pooled for selection, never outranking its parent on the prior alone. Converting
pool membership into top-1 wins is ranking's job.
"""
from __future__ import annotations

import itertools
from typing import Callable, Sequence

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
from engine.sql_expansion import ExpansionSupport, tokens

# Matches the profile-expansion generation_penalty scale (ProfileSearchConfig).
_PARSIMONY_PENALTY = 5.0
# Variant fan-out bounds: at most this many duplicate-name groups per candidate, and at most
# this many single-binding choices per group, keep the expansion pool-sized, not exponential.
_MAX_BINDING_GROUPS = 2
_MAX_GROUP_BINDINGS = 3


class ParsimonyQueryExpander(ExpansionSupport):
    """Add minimal-join and single-binding projection variants of pooled candidates."""

    def expand(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        question_tokens = tokens(question)
        linked_cache: dict[str, tuple[ColumnRef, ...]] = {}

        def linked(table: str) -> tuple[ColumnRef, ...]:
            if table not in linked_cache:
                linked_cache[table] = tuple(
                    ref for ref, _score, _position
                    in self.projection_columns(question_tokens, table)
                )
            return linked_cache[table]

        generated: list[ScoredQuery] = []
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not _is_flat(query):
                continue
            for projected, binding_evidence in _projection_variants(query, linked):
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


def _replaced_select(query: SelectQuery, select, group_by=None, distinct=None):
    return SelectQuery(
        select=tuple(select),
        from_table=query.from_table,
        joins=query.joins,
        where=query.where,
        group_by=query.group_by if group_by is None else tuple(group_by),
        having=query.having,
        order_by=query.order_by,
        limit=query.limit,
        distinct=query.distinct if distinct is None else distinct,
    )


def _projection_variants(query: SelectQuery, linked: Callable[[str], tuple[ColumnRef, ...]]):
    """Yield the query plus bounded single-edit projection variants.

    Families and their measured motivation are in the module docstring; yield order decides
    which derivation's evidence survives SQL dedup, most-specific first."""
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
                _replaced_select(query, select, group_by=group_by),
                tuple(f"projection:binding:{column.table}.{column.name}" for column in kept),
            )
    plain_items = [item for item in query.select if isinstance(item.expression, ColumnRef)]
    if 2 <= len(query.select) <= 6:
        # drop-one and drop-two column reductions (value-superset misses)
        for removed in itertools.chain(
            ((item,) for item in plain_items),
            itertools.islice(itertools.combinations(plain_items, 2),
                             10 if len(query.select) >= 3 else 0),
        ):
            if len(removed) >= len(query.select):
                continue
            gone = set(removed)
            tag = "drop" if len(removed) == 1 else "drop2"
            yield (
                _replaced_select(query, (item for item in query.select if item not in gone)),
                tuple(f"projection:{tag}:{item.expression.table}.{item.expression.name}"
                      for item in removed),
            )
    # add-one question-linked column of an already-joined table (value-subset misses);
    # grouped queries add the column to GROUP BY too, or the validator rejects the variant.
    if len(query.select) <= 5:
        projected = {item.expression for item in plain_items}
        joined_tables = [query.from_table] if isinstance(query.from_table, str) else []
        joined_tables += [join.table for join in query.joins]
        additions = [column for table in dict.fromkeys(joined_tables)
                     for column in linked(table) if column not in projected][:4]
        for column in additions:
            group_by = (tuple(query.group_by) + (column,)) if query.group_by else None
            for select in ((SelectItem(column), *query.select),
                           (*query.select, SelectItem(column))):
                yield (
                    _replaced_select(query, select, group_by=group_by),
                    (f"projection:add:{column.table}.{column.name}",),
                )
    # aggregate-operand swap to a question-linked column of the same table
    aggregate_items = [item for item in query.select if isinstance(item.expression, Aggregate)]
    if len(aggregate_items) == 1 and isinstance(aggregate_items[0].expression.operand, ColumnRef):
        item = aggregate_items[0]
        operand = item.expression.operand
        for column in [c for c in linked(operand.table) if c != operand][:2]:
            swapped = SelectItem(Aggregate(item.expression.function, column,
                                           item.expression.distinct))
            yield (
                _replaced_select(query, (swapped if it is item else it for it in query.select)),
                (f"aggregate:operand:{column.table}.{column.name}",),
            )
    # DISTINCT toggle for plain projections
    if not aggregate_items and plain_items:
        yield (
            _replaced_select(query, query.select, distinct=not query.distinct),
            ("projection:distinct-toggle",),
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
