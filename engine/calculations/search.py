"""Typed AST expansion from registered calculation plans."""
from __future__ import annotations

from engine.calculations.registry import (
    calculation_operand_scores,
    detect_calculations,
    specifications,
)
from engine.sql_ast import (
    SelectItem,
    SelectQuery,
    SubquerySource,
    render_query,
    validate_query,
)
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import SchemaGraph


class CalculationQueryExpander:
    def __init__(self, schema: SchemaGraph, max_candidates: int = 64, semantic_signals=None):
        self.schema = schema
        self.max_candidates = max(1, max_candidates)
        self.semantic_signals = semantic_signals

    def expand(self, question: str, candidates) -> list[ScoredQuery]:
        intents = detect_calculations(question)
        if not intents:
            return []
        by_name = {specification.name: specification for specification in specifications()}
        learned = getattr(self.semantic_signals, "calculation_intents", {}) or {}
        generated = {}
        for intent in intents:
            plans = by_name[intent.specification].plans(
                intent, self.schema, calculation_operand_scores(intent, self.semantic_signals)
            )
            for plan in plans:
                for candidate in candidates[:12]:
                    base = candidate.query
                    if not isinstance(base, SelectQuery) or isinstance(base.from_table, SubquerySource):
                        continue
                    group_by = tuple(column for column in base.group_by if column not in plan.required_columns)
                    select = tuple(SelectItem(column) for column in group_by) + (
                        SelectItem(plan.expression, alias=plan.alias),
                    )
                    required = {column.table for column in plan.required_columns + group_by}
                    if base.where is not None:
                        probe = SelectQuery((SelectItem(plan.expression),), plan.root_table, where=base.where)
                        required.update(probe.referenced_tables())
                    if not required:
                        required.add(plan.root_table)
                    for tree in self.schema.join_trees(required, plan.root_table):
                        query = SelectQuery(
                            select,
                            tree.root,
                            joins=tree.joins,
                            where=base.where,
                            group_by=group_by,
                            limit=base.limit if group_by else None,
                        )
                        try:
                            validate_query(query)
                            sql = render_query(query)
                        except (TypeError, ValueError):
                            continue
                        learned_score = float(learned.get(intent.specification, 0.0))
                        score = candidate.score + plan.score + min(max(learned_score, -1.0), 1.0)
                        result = ScoredQuery(
                            query,
                            sql,
                            score,
                            candidate.evidence + (
                                f"calculation:{plan.specification}:{plan.rule}",
                                *(f"calculation-bind:{role}={column.table}.{column.name}"
                                  for role, column in plan.bindings),
                            ),
                        )
                        previous = generated.get(sql)
                        if previous is None or result.score > previous.score:
                            generated[sql] = result
        return sorted(generated.values(), key=lambda candidate: (-candidate.score, candidate.sql))[
            :self.max_candidates
        ]
