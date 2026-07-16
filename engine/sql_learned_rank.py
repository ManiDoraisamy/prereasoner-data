"""Frozen, deterministic learned ranking for SQL AST candidates.

Models consume only schema-independent feature names. Training is an offline
operation; inference uses fixed arithmetic and a stable SQL-text tie break.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from engine.sql_ast import (
    Aggregate,
    BooleanExpr,
    ColumnRef,
    Comparison,
    ExistsPredicate,
    InPredicate,
    Query,
    ScalarSubquery,
    SelectQuery,
    SetQuery,
    SubquerySource,
)
from engine.sql_candidate import ScoredQuery


MODEL_VERSION = 1
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")
_COUNT_CUES = frozenset({"count", "counts", "many", "number"})
_GROUP_CUES = frozenset({"each", "per"})
_ORDER_CUES = frozenset({
    "biggest", "earliest", "fewest", "first", "greatest", "highest", "largest",
    "last", "latest", "least", "lowest", "most", "oldest", "order", "ordered",
    "rarest", "shortest", "smallest", "sort", "sorted", "top", "youngest",
})
_NEGATION_CUES = frozenset({"except", "excluding", "never", "no", "not", "without"})
_STRUCTURAL_CUES = frozenset({
    "after", "all", "among", "and", "any", "at", "average", "before", "between",
    "both", "bottom", "count", "different", "distinct", "each", "either", "every",
    "except", "excluding", "fewest", "first", "for", "from", "greater", "group",
    "has", "have", "having", "highest", "how", "in", "last", "least", "less",
    "like", "lowest", "many", "maximum", "mean", "minimum", "more", "most", "never",
    "no", "not", "number", "oldest", "only", "or", "order", "ordered", "per", "same",
    "sort", "sorted", "sum", "than", "top", "total", "unique", "where", "which",
    "whose", "with", "without", "youngest",
})
_HASH_BUCKETS = 64


@dataclass(frozen=True)
class LinearRankerModel:
    """A JSON-serializable linear ranker with fixed feature normalization."""

    weights: Mapping[str, float]
    scales: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: int = MODEL_VERSION

    def __post_init__(self) -> None:
        if self.version != MODEL_VERSION:
            raise ValueError(
                f"unsupported SQL ranker version {self.version}; expected {MODEL_VERSION}"
            )
        for name, value in self.weights.items():
            if not name or not math.isfinite(float(value)):
                raise ValueError(f"invalid SQL ranker weight: {name!r}={value!r}")
        for name, value in self.scales.items():
            if not name or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"invalid SQL ranker scale: {name!r}={value!r}")

    def score(self, features: Mapping[str, float]) -> float:
        total = 0.0
        for name, weight in self.weights.items():
            value = float(features.get(name, 0.0))
            scale = float(self.scales.get(name, 1.0))
            total += float(weight) * value / scale
        return total

    def rerank(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        """Rerank a baseline-sorted pool without generating or mutating SQL."""
        return _rerank(self, "linear", question, candidates)

    def with_metadata(self, metadata: Mapping[str, Any]) -> "LinearRankerModel":
        return replace(self, metadata=dict(metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": "deterministic_linear_ranker",
            "weights": {name: float(value) for name, value in sorted(self.weights.items())},
            "scales": {name: float(value) for name, value in sorted(self.scales.items())},
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LinearRankerModel":
        if data.get("type") != "deterministic_linear_ranker":
            raise ValueError("not a deterministic SQL linear-ranker artifact")
        return cls(
            weights={str(name): float(value) for name, value in data.get("weights", {}).items()},
            scales={str(name): float(value) for name, value in data.get("scales", {}).items()},
            metadata=dict(data.get("metadata", {})),
            version=int(data.get("version", 0)),
        )

    @classmethod
    def load(cls, path: str) -> "LinearRankerModel":
        with open(path, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")


@dataclass(frozen=True)
class TreeNode:
    feature: str | None = None
    threshold: float = 0.0
    left: int = -1
    right: int = -1
    value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        if self.feature is None:
            return {"value": float(self.value)}
        return {
            "feature": self.feature,
            "threshold": float(self.threshold),
            "left": self.left,
            "right": self.right,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TreeNode":
        if "feature" not in data:
            return cls(value=float(data["value"]))
        return cls(
            feature=str(data["feature"]),
            threshold=float(data["threshold"]),
            left=int(data["left"]),
            right=int(data["right"]),
        )


@dataclass(frozen=True)
class DecisionTree:
    nodes: tuple[TreeNode, ...]

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValueError("SQL ranker decision trees cannot be empty")
        for node in self.nodes:
            values = (node.threshold, node.value)
            if not all(math.isfinite(float(value)) for value in values):
                raise ValueError("SQL ranker tree contains a non-finite value")
            if node.feature is not None:
                if not node.feature or not (0 <= node.left < len(self.nodes)) \
                        or not (0 <= node.right < len(self.nodes)):
                    raise ValueError("SQL ranker tree contains an invalid split")

    def score(self, features: Mapping[str, float]) -> float:
        index = 0
        for _ in range(len(self.nodes)):
            node = self.nodes[index]
            if node.feature is None:
                return float(node.value)
            value = float(features.get(node.feature, 0.0))
            index = node.left if value <= node.threshold else node.right
        raise ValueError("SQL ranker tree traversal contains a cycle")

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": [node.to_dict() for node in self.nodes]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionTree":
        return cls(tuple(TreeNode.from_dict(node) for node in data["nodes"]))


@dataclass(frozen=True)
class TreeEnsembleRankerModel:
    """A frozen gradient-boosted tree ensemble with dependency-free inference."""

    trees: tuple[DecisionTree, ...]
    learning_rate: float
    base_score: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    version: int = MODEL_VERSION

    def __post_init__(self) -> None:
        if self.version != MODEL_VERSION:
            raise ValueError(
                f"unsupported SQL ranker version {self.version}; expected {MODEL_VERSION}"
            )
        if not self.trees:
            raise ValueError("SQL tree ranker requires at least one tree")
        if not math.isfinite(self.learning_rate) or self.learning_rate <= 0:
            raise ValueError("SQL tree ranker learning_rate must be finite and positive")
        if not math.isfinite(self.base_score):
            raise ValueError("SQL tree ranker base_score must be finite")

    def score(self, features: Mapping[str, float]) -> float:
        return self.base_score + self.learning_rate * sum(
            tree.score(features) for tree in self.trees
        )

    def rerank(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        return _rerank(self, "trees", question, candidates)

    def with_metadata(self, metadata: Mapping[str, Any]) -> "TreeEnsembleRankerModel":
        return replace(self, metadata=dict(metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": "deterministic_tree_ranker",
            "learning_rate": float(self.learning_rate),
            "base_score": float(self.base_score),
            "trees": [tree.to_dict() for tree in self.trees],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TreeEnsembleRankerModel":
        if data.get("type") != "deterministic_tree_ranker":
            raise ValueError("not a deterministic SQL tree-ranker artifact")
        return cls(
            trees=tuple(DecisionTree.from_dict(tree) for tree in data.get("trees", ())),
            learning_rate=float(data["learning_rate"]),
            base_score=float(data.get("base_score", 0.0)),
            metadata=dict(data.get("metadata", {})),
            version=int(data.get("version", 0)),
        )

    @classmethod
    def load(cls, path: str) -> "TreeEnsembleRankerModel":
        with open(path, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")


RankerModel = LinearRankerModel | TreeEnsembleRankerModel


def load_ranker_model(path: str) -> RankerModel:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("type") == "deterministic_linear_ranker":
        return LinearRankerModel.from_dict(data)
    if data.get("type") == "deterministic_tree_ranker":
        return TreeEnsembleRankerModel.from_dict(data)
    raise ValueError(f"unsupported SQL ranker artifact type: {data.get('type')!r}")


def _rerank(
    model: RankerModel,
    label: str,
    question: str,
    candidates: Sequence[ScoredQuery],
) -> list[ScoredQuery]:
    baseline = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.sql))
    pool_size = len(baseline)
    ranked = []
    for rank, candidate in enumerate(baseline):
        vector = learned_feature_vector(question, candidate, rank, pool_size)
        score = model.score(vector)
        ranked.append(replace(
            candidate,
            score=score,
            evidence=candidate.evidence + (f"phase6:{label}={score:+.6f}",),
            features=candidate.features + ((f"phase6_{label}_score", score),),
        ))
    return sorted(ranked, key=lambda candidate: (-candidate.score, candidate.sql))


def learned_feature_vector(
    question: str,
    candidate: ScoredQuery,
    baseline_rank: int = 0,
    pool_size: int = 1,
) -> dict[str, float]:
    """Extract stable ranking features without table, column, or literal identities."""
    features = learned_question_features(question)
    for name in (
        "question_count", "question_group", "question_order", "question_negation",
        "question_disjunction", "question_distinct", "question_aggregate",
    ):
        features.setdefault(name, 0.0)
    features.update({
        "baseline_score": float(candidate.score),
        "baseline_reciprocal_rank": 1.0 / (baseline_rank + 1.0),
        "baseline_top1": float(baseline_rank == 0),
        "baseline_rank_fraction": baseline_rank / max(pool_size - 1, 1),
    })

    family_values: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    for name, value in candidate.features:
        if name.startswith("phase6_"):
            continue
        family = _feature_family(name)
        family_values[family] += float(value)
        family_counts[family] += _feature_count_weight(name)
    for family, value in family_values.items():
        features[f"heuristic_value.{family}"] = value
        features[f"heuristic_count.{family}"] = float(family_counts[family])

    phase_evidence = {item.split(":", 1)[0] for item in candidate.evidence}
    for phase in ("phase3", "phase4", "phase5"):
        features[f"origin.{phase}"] = float(phase in phase_evidence)

    ast = _ast_features(candidate.query)
    features.update(ast)
    features.update({
        "match.count": features["question_count"] * ast.get("ast.aggregate.COUNT", 0.0),
        "match.group": features["question_group"] * float(ast.get("ast.group_columns", 0.0) > 0),
        "match.order": features["question_order"] * float(ast.get("ast.order_terms", 0.0) > 0),
        "match.negation": features["question_negation"] * (
            ast.get("ast.negated_predicates", 0.0) + ast.get("ast.set.EXCEPT", 0.0)
        ),
        "match.disjunction": features["question_disjunction"] * (
            ast.get("ast.boolean.OR", 0.0) + ast.get("ast.set.UNION", 0.0)
        ),
        "mismatch.unasked_group": (1.0 - features["question_group"])
                                   * float(ast.get("ast.group_columns", 0.0) > 0),
        "mismatch.unasked_set": max(0.0, 1.0 - features["question_negation"]
                                    - features["question_disjunction"])
                                 * float(ast.get("ast.set_queries", 0.0) > 0),
    })
    return {name: float(value) for name, value in features.items() if value != 0.0}


def rerank_with_promotion_gate(
    model: RankerModel,
    question: str,
    candidates: Sequence[ScoredQuery],
) -> list[ScoredQuery]:
    """Allow only a calibrated profile-generated challenger to displace rank one."""
    if not candidates:
        return []
    ranked = model.rerank(question, candidates)
    threshold = float(model.metadata.get("promotion_gate", {}).get("margin_threshold", math.inf))
    fallback = candidates[0]
    scored_fallback = next(candidate for candidate in ranked if candidate.sql == fallback.sql)
    eligible = [
        candidate for candidate in ranked
        if candidate.sql != fallback.sql
        and "profile_binding_quality" in dict(candidate.features)
    ]
    challenger = eligible[0] if eligible else None
    if challenger is not None and challenger.score - scored_fallback.score >= threshold:
        return [challenger, scored_fallback] + [
            candidate for candidate in ranked
            if candidate.sql not in {challenger.sql, scored_fallback.sql}
        ]
    return [scored_fallback] + [
        candidate for candidate in ranked if candidate.sql != scored_fallback.sql
    ]


def learned_question_features(question: str) -> dict[str, float]:
    """Question-only features shared by online extraction and cached training groups."""
    tokens = tuple(token.lower() for token in _TOKEN_RE.findall(question))
    token_set = set(tokens)
    features: Counter[str] = Counter({
        "question_token_count": float(len(tokens)),
        "question_count": float(bool(token_set & _COUNT_CUES)),
        "question_group": float(bool(token_set & _GROUP_CUES)),
        "question_order": float(bool(token_set & _ORDER_CUES)),
        "question_negation": float(bool(token_set & _NEGATION_CUES)),
        "question_disjunction": float("or" in token_set or "either" in token_set),
        "question_distinct": float(bool(token_set & {"different", "distinct", "unique"})),
        "question_aggregate": float(bool(
            token_set & {"average", "avg", "count", "maximum", "mean", "min", "minimum",
                         "number", "sum", "total", "max"}
        )),
    })
    for token in sorted(token_set & _STRUCTURAL_CUES):
        features[f"question_cue.{token}"] = 1.0
    for token in tokens:
        features[f"question_hash.unigram.{_hash_bucket('u:' + token)}"] += 1.0
    for left, right in zip(tokens, tokens[1:]):
        features[f"question_hash.bigram.{_hash_bucket('b:' + left + ' ' + right)}"] += 1.0
    return {name: float(value) for name, value in features.items() if value != 0.0}


def _hash_bucket(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _HASH_BUCKETS


def _feature_family(name: str) -> str:
    """Collapse schema-specific feature suffixes into trainable global families."""
    parts = name.split(":")
    while parts and parts[0] in {"left", "right"}:
        parts.pop(0)
    if not parts:
        return "unknown"
    if parts[0] == "aggregate_target" and len(parts) > 1:
        return f"aggregate_target.{parts[1].lower()}"
    if parts[0] == "set_operator" and len(parts) > 1:
        return f"set_operator.{parts[1].lower()}"
    return parts[0]


def _feature_count_weight(name: str) -> float:
    parts = name.split(":")
    depth = 0
    while parts and parts[0] in {"left", "right"}:
        depth += 1
        parts.pop(0)
    return 0.5 ** depth


def _ast_features(query: Query) -> dict[str, float]:
    counts: Counter[str] = Counter()

    def visit_query(node: Query, depth: int) -> None:
        counts["select_blocks"] += 1
        counts["max_depth"] = max(counts["max_depth"], depth)
        if isinstance(node, SetQuery):
            counts["set_queries"] += 1
            counts[f"set.{node.operator}"] += 1
            visit_query(node.left, depth + 1)
            visit_query(node.right, depth + 1)
            return
        counts["select_items"] += len(node.select)
        counts["joins"] += len(node.joins)
        counts["left_joins"] += sum(join.kind == "LEFT" for join in node.joins)
        counts["aliases"] += int(node.from_alias is not None)
        counts["aliases"] += sum(join.alias is not None for join in node.joins)
        counts["distinct"] += int(node.distinct)
        counts["group_columns"] += len(node.group_by)
        counts["order_terms"] += len(node.order_by)
        counts["descending_terms"] += sum(term.direction == "DESC" for term in node.order_by)
        counts["limit"] += int(node.limit is not None)
        counts["having"] += int(node.having is not None)
        if isinstance(node.from_table, SubquerySource):
            counts["derived_tables"] += 1
            visit_query(node.from_table.query, depth + 1)
        for item in node.select:
            visit_expr(item.expression, depth)
        for term in node.order_by:
            visit_expr(term.expression, depth)
        visit_predicate(node.where, depth)
        visit_predicate(node.having, depth)

    def visit_expr(expression, depth: int) -> None:
        if isinstance(expression, Aggregate):
            counts["aggregates"] += 1
            counts[f"aggregate.{expression.function}"] += 1
            counts["aggregate_distinct"] += int(expression.distinct)
        elif isinstance(expression, ScalarSubquery):
            counts["scalar_subqueries"] += 1
            visit_query(expression.query, depth + 1)

    def visit_predicate(predicate, depth: int) -> None:
        if predicate is None:
            return
        counts["predicates"] += 1
        if isinstance(predicate, BooleanExpr):
            counts[f"boolean.{predicate.operator}"] += 1
            for term in predicate.terms:
                visit_predicate(term, depth)
            return
        if isinstance(predicate, ExistsPredicate):
            counts["exists"] += 1
            counts["negated_predicates"] += int(predicate.negated)
            visit_query(predicate.query, depth + 1)
            return
        if isinstance(predicate, InPredicate):
            counts["in_predicates"] += 1
            counts["negated_predicates"] += int(predicate.negated)
            visit_expr(predicate.left, depth)
            if isinstance(predicate.source, tuple):
                counts["in_list_values"] += len(predicate.source)
                for expression in predicate.source:
                    visit_expr(expression, depth)
            else:
                visit_query(predicate.source, depth + 1)
            return
        if isinstance(predicate, Comparison):
            counts["comparisons"] += 1
            counts[f"comparison.{predicate.operator}"] += 1
            visit_expr(predicate.left, depth)
            visit_expr(predicate.right, depth)

    visit_query(query, 0)
    return {f"ast.{name}": float(value) for name, value in counts.items() if value}
