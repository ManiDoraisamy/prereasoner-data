"""Typed evidence shared by deterministic calculation specifications.

The SQL planner may propose arithmetic, but a proposal is not proof that it realizes a
question.  This module describes completed ASTs without assigning domain meaning.  Registered
specifications consume the same branch, expression, predicate, and binding evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    BooleanExpr,
    ColumnRef,
    Comparison,
    Literal,
    Query,
    ScalarExpr,
    SelectQuery,
    SetQuery,
    expression_type,
)
from engine.sql_schema import SchemaGraph


@dataclass(frozen=True)
class CalculationIntent:
    """A deterministic request for one registered calculation family."""

    specification: str
    operation: str
    phrase: str
    target: str | None = None
    explicit: bool = True
    attributes: Mapping[str, str] = field(default_factory=dict)
    operands: Mapping[str, str] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {
            "specification": self.specification,
            "operation": self.operation,
            "phrase": self.phrase,
            "target": self.target,
            "explicit": self.explicit,
            "attributes": {
                key: value for key, value in sorted(self.attributes.items())
                if not key.startswith("_")
            },
            "operands": dict(sorted(self.operands.items())),
        }


@dataclass(frozen=True)
class CalculationPlan:
    """One fully bound expression a specification permits the planner to emit."""

    specification: str
    expression: ScalarExpr
    alias: str
    output_unit: str
    rule: str
    bindings: tuple[tuple[str, ColumnRef], ...]
    root_table: str
    score: float = 0.0
    # Row-level multiplicative calculations expose their base measure and factor so independent
    # specifications can compose without parsing SQL or duplicating the measure. Examples:
    # currency => amount * fx_rate; net discount => amount * (1 - discount_rate).
    measure: ColumnRef | None = None
    factor: ScalarExpr | None = None

    @property
    def required_columns(self) -> tuple[ColumnRef, ...]:
        return tuple(dict.fromkeys(column for _, column in self.bindings))

    def record(self) -> dict[str, Any]:
        return {
            "specification": self.specification,
            "alias": self.alias,
            "output_unit": self.output_unit,
            "rule": self.rule,
            "bindings": [
                {"role": role, "table": column.table, "column": column.name}
                for role, column in self.bindings
            ],
        }


@dataclass(frozen=True)
class PredicateFact:
    """A scalar comparison guaranteed on a branch's complete Boolean path."""

    table: str
    column: str
    operator: str
    value: Any

    def record(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "operator": self.operator,
            "value": self.value,
        }


@dataclass(frozen=True)
class JoinFact:
    """One complete join predicate carried by a typed query branch."""

    column_pairs: tuple[tuple[ColumnRef, ColumnRef], ...]

    def record(self) -> dict[str, Any]:
        return {
            "column_pairs": [
                {
                    "left": {"table": left.table, "column": left.name},
                    "right": {"table": right.table, "column": right.name},
                }
                for left, right in self.column_pairs
            ],
        }


@dataclass(frozen=True)
class OutputEvidence:
    expression: ScalarExpr
    numeric: bool
    aggregate_functions: tuple[str, ...]
    columns: tuple[ColumnRef, ...]

    def record(self) -> dict[str, Any]:
        return {
            "expression": expression_record(self.expression),
            "numeric": self.numeric,
            "aggregate_functions": list(self.aggregate_functions),
            "columns": [
                {"table": column.table, "column": column.name, "type": column.type.value}
                for column in self.columns
            ],
        }


@dataclass(frozen=True)
class BranchEvidence:
    outputs: tuple[OutputEvidence, ...]
    predicates: frozenset[PredicateFact] = frozenset()
    joins: tuple[JoinFact, ...] = ()
    # The GRAIN this branch computes at. A calculation only answers the question asked if it is
    # computed at the requested grain: "gdp per capita BY COUNTRY" answered by a single global
    # SUM(gdp)/SUM(population) is a different quantity, not a coarser version of the same one.
    grouping: tuple[ColumnRef, ...] = ()

    def record(self) -> dict[str, Any]:
        return {
            "outputs": [output.record() for output in self.outputs],
            "predicates": [fact.record() for fact in sorted(
                self.predicates,
                key=lambda fact: (fact.table, fact.column, fact.operator, repr(fact.value)),
            )],
            "joins": [fact.record() for fact in self.joins],
            "grouping": [{"table": column.table, "column": column.name}
                         for column in self.grouping],
        }


@dataclass(frozen=True)
class ComputationEvidence:
    """Planner-independent computation evidence, preserving every set-operation branch."""

    branches: tuple[BranchEvidence, ...]
    verified: bool = True
    source: str = "typed_ast"

    @classmethod
    def unverified(cls, source: str = "planner_without_typed_evidence") -> "ComputationEvidence":
        return cls((), verified=False, source=source)

    def record(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "source": self.source,
            "branch_count": len(self.branches),
            "branches": [branch.record() for branch in self.branches],
        }


class CalculationSpecification(Protocol):
    """The complete contract for one deterministic calculation family."""

    name: str

    def detect(self, question: str) -> CalculationIntent | None: ...

    def plans(
        self,
        intent: CalculationIntent,
        graph: SchemaGraph,
        operand_scores: Mapping[str, Mapping[tuple[str, str], float]] | None = None,
    ) -> tuple[CalculationPlan, ...]: ...

    def assess(
        self,
        intent: CalculationIntent,
        evidence: ComputationEvidence,
        tables,
        graph: SchemaGraph,
    ) -> dict[str, Any]: ...


def expression_columns(expression: ScalarExpr) -> tuple[ColumnRef, ...]:
    if isinstance(expression, ColumnRef):
        return (expression,)
    if isinstance(expression, Aggregate):
        return expression_columns(expression.operand)
    if isinstance(expression, BinaryExpr):
        return tuple(dict.fromkeys(
            expression_columns(expression.left) + expression_columns(expression.right)
        ))
    return ()


def aggregate_functions(expression: ScalarExpr) -> tuple[str, ...]:
    if isinstance(expression, Aggregate):
        return (expression.function,) + aggregate_functions(expression.operand)
    if isinstance(expression, BinaryExpr):
        return aggregate_functions(expression.left) + aggregate_functions(expression.right)
    return ()


def expression_record(expression: ScalarExpr) -> dict[str, Any]:
    if isinstance(expression, ColumnRef):
        return {
            "kind": "column",
            "table": expression.table,
            "column": expression.name,
            "type": expression.type.value,
        }
    if isinstance(expression, Literal):
        return {"kind": "literal", "value": expression.value, "type": expression.type.value}
    if isinstance(expression, Aggregate):
        return {
            "kind": "aggregate",
            "function": expression.function,
            "distinct": expression.distinct,
            "operand": expression_record(expression.operand),
        }
    if isinstance(expression, BinaryExpr):
        return {
            "kind": "binary",
            "operator": expression.operator,
            "left": expression_record(expression.left),
            "right": expression_record(expression.right),
        }
    return {"kind": type(expression).__name__}


def guaranteed_predicates(predicate) -> frozenset[PredicateFact]:
    """Facts true on every path through a Boolean expression."""
    if (
        isinstance(predicate, Comparison)
        and isinstance(predicate.left, ColumnRef)
        and isinstance(predicate.right, Literal)
    ):
        return frozenset((PredicateFact(
            predicate.left.table,
            predicate.left.name,
            predicate.operator,
            predicate.right.value,
        ),))
    if not isinstance(predicate, BooleanExpr) or not predicate.terms:
        return frozenset()
    children = tuple(guaranteed_predicates(term) for term in predicate.terms)
    if predicate.operator == "OR":
        return frozenset(set.intersection(*(set(child) for child in children)))
    return frozenset().union(*children)


def _join_signature(column_pairs) -> tuple[tuple[str, str, str, str], ...]:
    forward = tuple(sorted(
        (left.table, left.name, right.table, right.name) for left, right in column_pairs
    ))
    reverse = tuple(sorted(
        (right.table, right.name, left.table, left.name) for left, right in column_pairs
    ))
    return min(forward, reverse)


def branch_realizes_plan(
    branch: BranchEvidence,
    plan: CalculationPlan,
    graph: SchemaGraph,
) -> bool:
    """Prove both the planned output and a complete registered-key path to its operands."""
    if not any(_output_realizes_plan(output.expression, plan) for output in branch.outputs):
        return False
    joined_columns = {
        column for fact in branch.joins for pair in fact.column_pairs for column in pair
    }
    if any(column not in joined_columns for role, column in plan.bindings
           if role == "source_currency"):
        return False
    required = {column.table for column in plan.required_columns}
    if not required or required == {plan.root_table}:
        return True

    known = {_join_signature(foreign_key.column_pairs) for foreign_key in graph.foreign_keys}
    adjacency: dict[str, set[str]] = {}
    for fact in branch.joins:
        if _join_signature(fact.column_pairs) not in known:
            continue
        tables = {column.table for pair in fact.column_pairs for column in pair}
        if len(tables) != 2:
            continue
        left, right = sorted(tables)
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    reached = {plan.root_table}
    pending = [plan.root_table]
    while pending:
        table = pending.pop()
        for neighbor in sorted(adjacency.get(table, ())):
            if neighbor not in reached:
                reached.add(neighbor)
                pending.append(neighbor)
    return required <= reached


def _multiplication_terms(expression: ScalarExpr) -> list[ScalarExpr]:
    if isinstance(expression, BinaryExpr) and expression.operator == "*":
        return _multiplication_terms(expression.left) + _multiplication_terms(expression.right)
    return [expression]


def _output_realizes_plan(expression: ScalarExpr, plan: CalculationPlan) -> bool:
    """Match one registered row factor inside an exactly typed aggregate expression."""
    if expression == plan.expression:
        return True
    if (
        plan.measure is None
        or plan.factor is None
        or not isinstance(expression, Aggregate)
        or expression.function != "SUM"
    ):
        return False
    available = _multiplication_terms(expression.operand)
    required = [plan.measure, *_multiplication_terms(plan.factor)]
    for term in required:
        try:
            available.remove(term)
        except ValueError:
            return False
    return True


def compose_row_plans(plans: tuple[CalculationPlan, ...]) -> CalculationPlan | None:
    """Compose compatible row factors into one exact ``SUM(measure * factors...)`` plan."""
    if not plans:
        return None
    if len(plans) == 1:
        return plans[0]
    measure = plans[0].measure
    if (
        measure is None
        or any(plan.measure != measure or plan.factor is None for plan in plans)
        or len({plan.root_table for plan in plans}) != 1
    ):
        return None
    operand: ScalarExpr = measure
    combined_factor: ScalarExpr | None = None
    for plan in plans:
        operand = BinaryExpr(operand, "*", plan.factor)  # type: ignore[arg-type]
        combined_factor = (plan.factor if combined_factor is None else
                           BinaryExpr(combined_factor, "*", plan.factor))  # type: ignore[arg-type]
    bindings = tuple(dict.fromkeys(binding for plan in plans for binding in plan.bindings))
    return CalculationPlan(
        "+".join(plan.specification for plan in plans),
        Aggregate("SUM", operand),
        "_and_".join(plan.alias for plan in plans),
        plans[-1].output_unit,
        "+".join(plan.rule for plan in plans),
        bindings,
        plans[0].root_table,
        sum(plan.score for plan in plans),
        measure,
        combined_factor,
    )


def describe_computation(query: Query) -> ComputationEvidence:
    """Describe arithmetic and guaranteed predicates from typed AST nodes, never SQL text."""
    if isinstance(query, SetQuery):
        left = describe_computation(query.left)
        right = describe_computation(query.right)
        return ComputationEvidence(
            left.branches + right.branches,
            verified=left.verified and right.verified,
        )
    if not isinstance(query, SelectQuery):
        return ComputationEvidence.unverified("unknown_query_type")
    outputs = tuple(
        OutputEvidence(
            item.expression,
            expression_type(item.expression).numeric,
            aggregate_functions(item.expression),
            expression_columns(item.expression),
        )
        for item in query.select
    )
    joins = tuple(JoinFact(join.predicates) for join in query.joins)
    return ComputationEvidence((BranchEvidence(
        outputs, guaranteed_predicates(query.where), joins, tuple(query.group_by),
    ),))
