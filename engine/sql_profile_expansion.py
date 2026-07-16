"""Bounded typed-AST expansion driven by predicted structural profiles."""
from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations, product
import re
from typing import Iterable, Mapping, Sequence

from engine.sql_ast import (
    Aggregate,
    ColumnRef,
    OrderTerm,
    SelectItem,
    SelectQuery,
    Star,
)
from engine.sql_candidate import ScoredQuery
from engine.sql_expansion import build_candidate, physical_tables
from engine.sql_profile import profile_query
from engine.sql_schema import SchemaGraph


_CONTROLLED_FEATURES = frozenset({
    "distinct",
    "group_items",
    "limit",
    "order.asc",
    "order.desc",
    "select_items",
    "aggregate.AVG",
    "aggregate.COUNT",
    "aggregate.MAX",
    "aggregate.MIN",
    "aggregate.SUM",
})
_NUMBER_RE = re.compile(r"\b([1-9][0-9]*)\b")


@dataclass(frozen=True)
class ProfileSearchConfig:
    max_candidates: int = 32
    per_profile: int = 4
    generation_penalty: float = 5.0
    binding_quality_weight: float = 2.0
    preserve_baseline_top: bool = True

    def __post_init__(self) -> None:
        if self.max_candidates < 1 or self.per_profile < 1:
            raise ValueError("profile candidate budgets must be positive")
        if self.generation_penalty < 0 or self.binding_quality_weight < 0:
            raise ValueError("profile weights must be nonnegative")


class ProfileQueryExpander:
    """Instantiate predicted profiles on compatible, already-valid query scaffolds."""

    def __init__(
        self,
        schema: SchemaGraph,
        signals,
        max_candidates: int = 32,
        per_profile: int = 4,
        generation_penalty: float = 5.0,
        binding_quality_weight: float = 2.0,
    ):
        self.schema = schema
        self.signals = signals
        self.max_candidates = max(1, max_candidates)
        self.per_profile = max(1, per_profile)
        self.generation_penalty = max(0.0, float(generation_penalty))
        self.binding_quality_weight = max(0.0, float(binding_quality_weight))
        self._columns = {
            (column.ref.table, column.ref.name): column.ref for column in schema.columns
        }

    def expand(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        profiles = tuple(self.signals.sketch_profiles[:16])
        if not profiles:
            return []
        buckets: list[list[ScoredQuery]] = []
        for profile_rank, target in enumerate(profiles):
            generated: dict[str, ScoredQuery] = {}
            target_map = {str(name): int(value) for name, value in target.items() if value}
            scaffolds = sorted(
                (candidate for candidate in candidates if isinstance(candidate.query, SelectQuery)),
                key=lambda candidate: (
                    self._profile_distance(profile_query(candidate.query).sketch_map, target_map),
                    -candidate.score,
                    candidate.sql,
                ),
            )[:24]
            for scaffold in scaffolds:
                for query in self._variants(question, scaffold.query, target_map):
                    if profile_query(query).sketch_map != target_map:
                        continue
                    built = build_candidate(
                        query,
                        scaffold.score - self.generation_penalty - 0.25 * profile_rank,
                        scaffold.evidence + (f"profile-expand:{profile_rank + 1}",),
                    )
                    if built is None:
                        continue
                    quality = self._binding_quality(query)
                    built = replace(
                        built,
                        score=built.score + self.binding_quality_weight * quality,
                        features=built.features + (("profile_binding_quality", quality),),
                    )
                    old = generated.get(built.sql)
                    if old is None or built.score > old.score:
                        generated[built.sql] = built
            buckets.append(sorted(
                generated.values(),
                key=lambda candidate: (
                    -dict(candidate.features).get("profile_binding_quality", 0.0),
                    -candidate.score,
                    candidate.sql,
                ),
            )[:self.per_profile])

        selected = []
        for position in range(self.per_profile):
            for bucket in buckets:
                if position < len(bucket):
                    selected.append(bucket[position])
                    if len(selected) == self.max_candidates:
                        return self._ordered(selected)
        return self._ordered(selected)

    @staticmethod
    def _profile_distance(actual: Mapping[str, int], target: Mapping[str, int]) -> int:
        names = set(actual) | set(target)
        return sum(abs(int(actual.get(name, 0)) - int(target.get(name, 0))) for name in names)

    def _compatible_scaffold(
        self, actual: Mapping[str, int], target: Mapping[str, int]
    ) -> bool:
        names = (set(actual) | set(target)) - _CONTROLLED_FEATURES
        return all(int(actual.get(name, 0)) == int(target.get(name, 0)) for name in names)

    def _variants(
        self, question: str, query: SelectQuery, target: Mapping[str, int]
    ) -> Iterable[SelectQuery]:
        actual = profile_query(query).sketch_map
        if not self._compatible_scaffold(actual, target):
            return ()
        visible = physical_tables(query)
        projection_count = int(target.get("select_items", len(query.select)))
        projection_columns = self._role_columns("projection", visible, query)
        aggregate_columns = self._role_columns("aggregate", visible, query)
        group_count = int(target.get("group_items", 0))
        order_spec = tuple(
            (direction.upper(), int(target.get(f"order.{direction}", 0)))
            for direction in ("asc", "desc")
            if target.get(f"order.{direction}")
        )
        limit = self._limit(question, query.limit) if target.get("limit") else None
        out = []
        for aggregate_functions in self._select_aggregate_allocations(target, projection_count):
            column_count = projection_count - len(aggregate_functions)
            projection_options = self._column_sets(projection_columns, column_count)
            aggregate_options = self._aggregate_sets(aggregate_functions, aggregate_columns)
            for columns, aggregates in product(projection_options, aggregate_options):
                select = tuple(SelectItem(column) for column in columns) + tuple(
                    SelectItem(aggregate) for aggregate in aggregates
                )
                group_options = self._group_sets(group_count, columns, visible, query)
                for groups in group_options:
                    expressions = tuple(columns) + tuple(aggregates)
                    order_options = self._order_sets(order_spec, expressions, visible, query)
                    for orders in order_options:
                        out.append(replace(
                            query,
                            select=select,
                            group_by=groups,
                            order_by=orders,
                            limit=limit,
                            distinct=bool(target.get("distinct")),
                        ))
                        if len(out) >= 192:
                            return tuple(out)
        return tuple(out)

    @staticmethod
    def _select_aggregate_allocations(
        target: Mapping[str, int], select_items: int
    ) -> tuple[tuple[str, ...], ...]:
        functions = ("COUNT", "SUM", "AVG", "MIN", "MAX")
        ranges = [range(int(target.get(f"aggregate.{function}", 0)) + 1) for function in functions]
        allocations = []
        for counts in product(*ranges):
            if sum(counts) > select_items:
                continue
            allocations.append(tuple(
                function for function, count in zip(functions, counts) for _ in range(count)
            ))
        order_occurrences = int(target.get("order.asc", 0)) + int(target.get("order.desc", 0))
        expected_select_occurrences = max(
            0,
            sum(int(target.get(f"aggregate.{function}", 0)) for function in functions)
            - order_occurrences,
        )
        return tuple(sorted(
            allocations,
            key=lambda value: (abs(len(value) - expected_select_occurrences), value),
        ))

    def _role_columns(
        self, role: str, visible: set[str], query: SelectQuery
    ) -> tuple[ColumnRef, ...]:
        existing = []
        for item in query.select:
            expression = item.expression
            if isinstance(expression, ColumnRef):
                existing.append(expression)
            elif isinstance(expression, Aggregate) and isinstance(expression.operand, ColumnRef):
                existing.append(expression.operand)
        existing.extend(query.group_by)
        existing.extend(
            term.expression for term in query.order_by if isinstance(term.expression, ColumnRef)
        )
        scores = self.signals.column_roles.get(role, {})
        ranked = sorted(
            (
                (float(score), self._columns[key])
                for key, score in scores.items()
                if key in self._columns and self._columns[key].table in visible
            ),
            key=lambda item: (-item[0], item[1].table, item[1].name),
        )
        fallbacks = [
            column.ref for column in self.schema.columns if column.ref.table in visible
        ]
        return tuple(dict.fromkeys([column for _, column in ranked[:10]] + existing + fallbacks))

    def _binding_quality(self, query: SelectQuery) -> float:
        role_columns = {
            "projection": tuple(
                item.expression for item in query.select
                if isinstance(item.expression, ColumnRef)
            ),
            "aggregate": tuple(
                item.expression.operand for item in query.select
                if isinstance(item.expression, Aggregate)
                and isinstance(item.expression.operand, ColumnRef)
            ),
            "group": query.group_by,
            "order": tuple(
                term.expression for term in query.order_by
                if isinstance(term.expression, ColumnRef)
            ),
        }
        values = []
        for role, columns in role_columns.items():
            scores = self.signals.column_roles.get(role, {})
            values.extend(float(scores.get((column.table, column.name), 0.0)) for column in columns)
        values.extend(
            float(self.signals.table_global.get(table, 0.0))
            for table in sorted(query.referenced_tables())
        )
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _column_sets(columns: Sequence[ColumnRef], count: int) -> tuple[tuple[ColumnRef, ...], ...]:
        if count == 0:
            return ((),)
        return tuple(combinations(columns[:10], count))[:24]

    def _aggregate_sets(
        self, functions: Sequence[str], columns: Sequence[ColumnRef]
    ) -> tuple[tuple[Aggregate, ...], ...]:
        if not functions:
            return ((),)
        choices = []
        for function in functions:
            options = []
            if function == "COUNT":
                options.append(Aggregate("COUNT", Star()))
                options.extend(Aggregate("COUNT", column) for column in columns[:6])
            else:
                compatible = [
                    column for column in columns
                    if function in {"MIN", "MAX"} or column.type.numeric
                ]
                options.extend(Aggregate(function, column) for column in compatible[:6])
            if not options:
                return ()
            choices.append(tuple(options))
        return tuple(product(*choices))[:32]

    def _group_sets(
        self,
        count: int,
        projections: Sequence[ColumnRef],
        visible: set[str],
        query: SelectQuery,
    ) -> tuple[tuple[ColumnRef, ...], ...]:
        if count == 0:
            return ((),)
        candidates = tuple(dict.fromkeys(
            tuple(projections)
            + self._role_columns("group", visible, query)
        ))
        return tuple(combinations(candidates[:10], count))[:24]

    def _order_sets(
        self,
        specification: Sequence[tuple[str, int]],
        expressions: Sequence[ColumnRef | Aggregate],
        visible: set[str],
        query: SelectQuery,
    ) -> tuple[tuple[OrderTerm, ...], ...]:
        directions = tuple(direction for direction, count in specification for _ in range(count))
        if not directions:
            return ((),)
        candidates = tuple(dict.fromkeys(
            tuple(expressions)
            + tuple(self._role_columns("order", visible, query))
        ))
        choices = tuple(combinations(candidates[:10], len(directions)))[:24]
        return tuple(
            tuple(OrderTerm(expression, direction) for expression, direction in zip(choice, directions))
            for choice in choices
        )

    @staticmethod
    def _limit(question: str, existing: int | None) -> int:
        if existing is not None:
            return existing
        match = _NUMBER_RE.search(question)
        return int(match.group(1)) if match else 1

    def _ordered(self, candidates: Iterable[ScoredQuery]) -> list[ScoredQuery]:
        return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.sql))[
            :self.max_candidates
        ]
