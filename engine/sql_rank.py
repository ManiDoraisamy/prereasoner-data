"""Deterministic semantic and execution-aware ranking for SQL AST candidates.

The ranker is deliberately separate from candidate generation.  Every adjustment is
recorded as a named feature, so model similarity can improve ordering without hiding
the structural reasons a candidate won.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import re
from typing import Any, Callable, Mapping, Sequence

from engine.sql_ast import Aggregate, ColumnRef, Comparison, Query, SelectQuery, SetQuery
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import SchemaGraph


ColumnKey = tuple[str, str]


@dataclass(frozen=True)
class SemanticSignals:
    """Fixed encoder similarities, keyed by semantic role and schema object."""

    column_roles: Mapping[str, Mapping[ColumnKey, float]]
    table_global: Mapping[str, float]
    sketch_profiles: tuple[Mapping[str, int], ...] = ()
    calculation_intents: Mapping[str, float] = field(default_factory=dict)
    calculation_operands: Mapping[str, Mapping[ColumnKey, float]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "SemanticSignals":
        return cls({}, {}, (), {}, {})


@dataclass(frozen=True)
class ExecutedCandidate:
    candidate: ScoredQuery
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class QuestionRoles:
    question: str
    tokens: tuple[str, ...]
    aggregate_positions: Mapping[str, tuple[int, ...]]
    count_requested: bool
    distinct_requested: bool
    group_requested: bool
    group_tables: frozenset[str]
    group_columns: frozenset[ColumnRef]
    counted_tables: frozenset[str]
    counted_columns: frozenset[ColumnRef]
    aggregate_targets: Mapping[str, frozenset[ColumnRef]]
    projection_columns: frozenset[ColumnRef]
    id_instead_of_name: bool


def semantic_role_phrases(question: str) -> dict[str, str]:
    """Extract compact role phrases for encoder-to-column cosine signals."""
    words = re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", question)
    low = [word.lower() for word in words]
    if not words:
        return {"global": question}

    aggregate = next((
        i for i, token in enumerate(low)
        if token in {"count", "sum", "total", "average", "avg", "mean",
                     "minimum", "min", "maximum", "max"}
        or (token == "number" and i + 1 < len(low) and low[i + 1] == "of")
        or (token == "how" and i + 1 < len(low) and low[i + 1] == "many")
    ), None)
    group = next((i for i, token in enumerate(low) if token in {"each", "per"}), None)
    filter_pos = next((i for i, token in enumerate(low)
                       if token in {"where", "with", "whose", "from", "after", "before", "between"}), None)
    order = next((i for i, token in enumerate(low)
                  if token in {"order", "ordered", "sort", "sorted", "top", "bottom", "highest",
                               "lowest", "most", "least"}), None)

    first_clause = min((i for i in (aggregate, group, filter_pos, order) if i is not None), default=len(words))
    phrases = {"global": " ".join(words)}
    if first_clause:
        phrases["projection"] = " ".join(words[:first_clause])
    if aggregate is not None:
        end = min((i for i in (group, filter_pos, order) if i is not None and i > aggregate), default=len(words))
        phrases["aggregate"] = " ".join(words[aggregate:end])
    if group is not None:
        end = min((i for i in (filter_pos, order) if i is not None and i > group), default=len(words))
        phrases["group"] = " ".join(words[group:end])
    if filter_pos is not None:
        phrases["filter"] = " ".join(words[filter_pos:])
    if order is not None:
        phrases["order"] = " ".join(words[order:])
    return {role: phrase for role, phrase in phrases.items() if phrase.strip()}


class CandidateRanker:
    def __init__(self, schema: SchemaGraph, signals: SemanticSignals | None = None):
        self.schema = schema
        self.signals = signals or SemanticSignals.empty()

    def rank(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        roles = analyze_question(question, self.schema)
        ranked = []
        for candidate in candidates:
            features = self._semantic_features(candidate.query, roles)
            score = candidate.score + sum(value for _, value in features)
            evidence = candidate.evidence + tuple(f"rank:{name}={value:+.3f}" for name, value in features if value)
            ranked.append(replace(candidate, score=score, evidence=evidence,
                                  features=candidate.features + features))
        return sorted(ranked, key=lambda candidate: (-candidate.score, candidate.sql))

    def rank_executions(self, question: str, executions: Sequence[ExecutedCandidate]) -> list[ExecutedCandidate]:
        ranked = []
        for execution in executions:
            features = execution_features(execution.candidate.query, execution.rows, execution.error)
            candidate = replace(
                execution.candidate,
                score=execution.candidate.score + sum(value for _, value in features),
                evidence=execution.candidate.evidence
                         + tuple(f"exec:{name}={value:+.3f}" for name, value in features if value),
                features=execution.candidate.features + features,
            )
            ranked.append(replace(execution, candidate=candidate))
        return sorted(ranked, key=lambda item: (-item.candidate.score, item.candidate.sql))

    def _semantic_features(self, query: Query, roles: QuestionRoles) -> tuple[tuple[str, float], ...]:
        if isinstance(query, SetQuery):
            if query.operator == "INTERSECT":
                aligned = "both" in roles.tokens or "and" in roles.tokens
            elif query.operator == "UNION":
                aligned = "either" in roles.tokens or "or" in roles.tokens
            else:
                aligned = bool(set(roles.tokens) & {"except", "without", "no", "not", "never"})
            left = self._semantic_features(query.left, roles)
            right = self._semantic_features(query.right, roles)
            return (
                (f"set_operator:{query.operator.lower()}", 6.0 if aligned else -5.0),
                *self._sketch_model_features(query),
                *((f"left:{name}", 0.5 * value) for name, value in left),
                *((f"right:{name}", 0.5 * value) for name, value in right),
            )
        select_columns = tuple(item.expression for item in query.select if isinstance(item.expression, ColumnRef))
        aggregates = tuple(item.expression for item in query.select if isinstance(item.expression, Aggregate))
        group_columns = query.group_by
        count_ranked = any(
            isinstance(term.expression, Aggregate) and term.expression.function == "COUNT"
            for term in query.order_by
        )
        features: list[tuple[str, float]] = [("base", 0.0)]

        if roles.count_requested:
            if count_ranked:
                features.append((
                    "count_ranked_entity",
                    4.0 if group_columns and query.limit is not None and select_columns else -3.0,
                ))
            elif roles.group_requested:
                features.append(("count_group_present", 2.5 if group_columns else -4.0))
            else:
                features.append(("count_without_spurious_group", 2.5 if not group_columns else -5.0))
                features.append(("count_without_raw_projection", -4.0 * len(select_columns)))

        count_aggregates = [aggregate for aggregate in aggregates if aggregate.function == "COUNT"]
        if roles.distinct_requested and count_aggregates:
            has_distinct = any(aggregate.distinct and isinstance(aggregate.operand, ColumnRef)
                               for aggregate in count_aggregates)
            features.append(("count_distinct", 5.0 if has_distinct else -4.0))
            if group_columns:
                features.append(("distinct_not_grouped", -3.0))

        if roles.count_requested and set(roles.tokens) & {"who", "that", "which"}:
            distinct_identities = [
                aggregate.operand
                for aggregate in count_aggregates
                if aggregate.distinct
                and isinstance(aggregate.operand, ColumnRef)
                and (_is_id(aggregate.operand.name) or _is_name(aggregate.operand.name))
            ]
            features.append(("count_distinct_entity", 4.0 if distinct_identities else 0.0))

        for column in group_columns:
            alignment = self._group_alignment(column, roles) or (
                count_ranked and any(
                    selected == column or selected.table == column.table
                    for selected in select_columns
                )
            )
            features.append((f"group_role:{_column_label(column)}", 2.5 if alignment else -4.0))

        for column in select_columns:
            if column in roles.projection_columns:
                value = 1.0
            elif self._group_alignment(column, roles):
                value = 1.5
            elif column.table in roles.counted_tables:
                value = -3.5
            else:
                value = -1.25 if aggregates else 0.0
            features.append((f"projection_role:{_column_label(column)}", value))

        if roles.id_instead_of_name:
            id_columns = [column for column in select_columns + group_columns if _is_id(column.name)]
            name_columns = [column for column in select_columns + group_columns if _is_name(column.name)]
            features.append(("requested_id", 4.0 if id_columns else -3.0))
            features.append(("rejected_name", -6.0 if name_columns else 1.0))

        for aggregate in aggregates:
            if not isinstance(aggregate.operand, ColumnRef):
                continue
            operand = aggregate.operand
            targets = roles.aggregate_targets.get(aggregate.function, frozenset())
            if aggregate.function == "COUNT":
                aligned = (
                    operand.table in roles.counted_tables
                    if roles.counted_tables
                    else operand in roles.counted_columns
                )
                features.append((f"count_target:{_column_label(operand)}", 1.5 if aligned else 0.0))
            elif targets:
                features.append((f"aggregate_target:{aggregate.function}:{_column_label(operand)}",
                                 3.0 if operand in targets else -3.5))

        from engine.calculations.registry import calculation_rank_features
        features.extend(calculation_rank_features(roles.question, query, self.schema, self.signals))

        travel_direction = _travel_direction(roles.tokens)
        if travel_direction:
            role_columns = {
                column
                for comparison in _comparisons(query.where)
                if isinstance(comparison.left, ColumnRef)
                for column in (comparison.left,)
            }
            role_columns.update(
                column for join in query.joins for pair in join.predicates for column in pair
            )
            directional_columns = [
                column for column in role_columns if _travel_column_role(column) is not None
            ]
            if directional_columns:
                aligned = any(
                    _travel_column_role(column) == travel_direction
                    for column in directional_columns
                )
                features.append((f"travel_direction:{travel_direction}", 3.0 if aligned else -3.0))

        features.extend(self._model_features(query))
        return tuple(features)

    def _group_alignment(self, column: ColumnRef, roles: QuestionRoles) -> bool:
        if column in roles.group_columns or column.table in roles.group_tables:
            return True
        for fk in self.schema.foreign_keys:
            for from_column, to_column in fk.column_pairs:
                if column == from_column and to_column.table in roles.group_tables:
                    return True
                if column == to_column and from_column.table in roles.group_tables:
                    return True
        return False

    def _model_features(self, query: SelectQuery) -> list[tuple[str, float]]:
        features = list(self._sketch_model_features(query))
        role_columns: dict[str, list[ColumnRef]] = {
            "projection": [item.expression for item in query.select if isinstance(item.expression, ColumnRef)],
            "aggregate": [item.expression.operand for item in query.select
                          if isinstance(item.expression, Aggregate)
                          and isinstance(item.expression.operand, ColumnRef)],
            "group": list(query.group_by),
            "order": [term.expression for term in query.order_by if isinstance(term.expression, ColumnRef)],
        }
        predicate_columns = []
        for predicate in _comparisons(query.where):
            if isinstance(predicate.left, ColumnRef):
                predicate_columns.append(predicate.left)
        role_columns["filter"] = predicate_columns

        for role, columns in role_columns.items():
            scores = self.signals.column_roles.get(role) or self.signals.column_roles.get("global") or {}
            if not columns or not scores:
                continue
            value = sum(float(scores.get((column.table, column.name), 0.0)) for column in columns) / len(columns)
            features.append((f"model_{role}", 2.0 * value))

        referenced = sorted(query.referenced_tables())
        if referenced and self.signals.table_global:
            value = sum(float(self.signals.table_global.get(table, 0.0)) for table in referenced) / len(referenced)
            features.append(("model_tables", 0.75 * value))
        return features

    def _sketch_model_features(self, query: Query) -> tuple[tuple[str, float], ...]:
        if not self.signals.sketch_profiles:
            return ()
        from engine.sql_profile import profile_query

        actual = profile_query(query).sketch_map
        for rank, expected in enumerate(self.signals.sketch_profiles):
            if actual == dict(expected):
                return ((f"model_sketch_profile:{rank + 1}", 4.0 / math.sqrt(rank + 1)),)
        return ()


def analyze_question(question: str, schema: SchemaGraph) -> QuestionRoles:
    tokens = _tokens(question)
    aggregate_positions: dict[str, list[int]] = {fn: [] for fn in ("COUNT", "SUM", "AVG", "MIN", "MAX")}
    for i, token in enumerate(tokens):
        number_is_column_label = (
            token == "number"
            and i > 0
            and any(
                tokens[i - 1] in _schema_tokens(column.ref.name)
                for column in schema.columns
            )
        )
        if (
            token == "count"
            or (token == "number" and i + 1 < len(tokens)
                and tokens[i + 1] == "of" and not number_is_column_label)
            or (token == "how" and i + 1 < len(tokens) and tokens[i + 1] == "many")
        ):
            aggregate_positions["COUNT"].append(i)
        elif token in {"sum", "total"}:
            aggregate_positions["SUM"].append(i)
        elif token in {"average", "avg", "mean"}:
            aggregate_positions["AVG"].append(i)
        elif token in {"minimum", "min"}:
            aggregate_positions["MIN"].append(i)
        elif token in {"maximum", "max"}:
            aggregate_positions["MAX"].append(i)

    all_aggregate_positions = sorted(position for positions in aggregate_positions.values() for position in positions)
    group_positions = [i for i, token in enumerate(tokens) if token in {"each", "per"}]
    group_positions += [i for i in range(len(tokens) - 1) if tokens[i:i + 2] == ("group", "by")]
    if all_aggregate_positions:
        group_positions += [i for i, token in enumerate(tokens)
                            if token == "by" and i > all_aggregate_positions[0]
                            and (i == 0 or tokens[i - 1] not in {"order", "ordered", "sort", "sorted"})]
    group_positions = sorted(set(group_positions))

    clause_stops = {"where", "with", "whose", "having", "order", "ordered", "sort", "sorted",
                    "top", "bottom", "after", "before", "between"}
    group_windows = []
    for position in group_positions:
        start = position + (2 if tokens[position:position + 2] == ("group", "by") else 1)
        later_aggregates = [p for p in all_aggregate_positions if p > position]
        end = min(later_aggregates + [i for i in range(start, len(tokens)) if tokens[i] in clause_stops]
                  + [len(tokens)])
        group_windows.append((start, end))

    count_stops = clause_stops | {
        "is", "are", "was", "were", "who", "that", "which",
        "use", "using", "used", "have", "has", "in", "from", "on", "at",
    }
    count_windows = []
    for position in aggregate_positions["COUNT"]:
        start = position + (2 if tokens[position:position + 2] == ("how", "many") else 1)
        end = min([p for p in group_positions if p > position]
                  + [i for i in range(start, len(tokens)) if tokens[i] in count_stops]
                  + [len(tokens)])
        count_windows.append((start, end))

    group_tables = _tables_in_windows(schema, tokens, group_windows)
    counted_tables = _tables_in_windows(schema, tokens, count_windows)
    group_columns = _columns_in_windows(schema, tokens, group_windows)
    counted_columns = _columns_in_windows(schema, tokens, count_windows)

    aggregate_targets: dict[str, frozenset[ColumnRef]] = {}
    for function, positions in aggregate_positions.items():
        if function == "COUNT" or not positions:
            continue
        candidates = []
        for column in schema.columns:
            name_tokens = _schema_tokens(column.ref.name)
            hits = [i for i, token in enumerate(tokens) if token in name_tokens]
            if not name_tokens or not set(name_tokens) <= set(tokens):
                continue
            distance = min((max(hit - position, 0) for position in positions for hit in hits if hit >= position),
                           default=999)
            candidates.append((distance, column.ref))
        if candidates:
            best_distance = min(distance for distance, _ in candidates)
            aggregate_targets[function] = frozenset(column for distance, column in candidates
                                                    if distance == best_distance)

    first_role = min(all_aggregate_positions + group_positions
                     + [i for i, token in enumerate(tokens) if token in clause_stops], default=len(tokens))
    projection_columns = _columns_in_windows(schema, tokens, [(0, first_role)])
    id_instead = ("instead" in tokens and bool({"id", "identifier"} & set(tokens))
                  and bool({"name", "names"} & set(tokens)))
    frozen_positions = {function: tuple(positions) for function, positions in aggregate_positions.items() if positions}
    return QuestionRoles(
        question=question,
        tokens=tokens,
        aggregate_positions=frozen_positions,
        count_requested=bool(aggregate_positions["COUNT"]),
        distinct_requested=bool({"distinct", "different", "unique"} & set(tokens)),
        group_requested=bool(group_positions),
        group_tables=frozenset(group_tables),
        group_columns=frozenset(group_columns),
        counted_tables=frozenset(counted_tables),
        counted_columns=frozenset(counted_columns),
        aggregate_targets=aggregate_targets,
        projection_columns=frozenset(projection_columns),
        id_instead_of_name=id_instead,
    )


def execution_features(query: Query, rows: Sequence[Sequence[Any]], error: str | None) -> tuple[tuple[str, float], ...]:
    if error is not None:
        return (("error", -1000.0),)
    materialized = tuple(tuple(row) for row in rows)
    features: list[tuple[str, float]] = []
    if not materialized:
        features.append(("empty_result", -1.25))
        return tuple(features)
    features.append(("nonempty_result", 0.25))
    values = [value for row in materialized for value in row]
    if values and all(value is None for value in values):
        features.append(("all_null_result", -2.0))
    if isinstance(query, SelectQuery):
        aggregates = [item.expression for item in query.select if isinstance(item.expression, Aggregate)]
        if aggregates and not query.group_by:
            features.append(("scalar_aggregate_shape", 0.5 if len(materialized) == 1 else -1.0))
        if query.group_by and len(materialized) > 1:
            features.append(("grouped_shape", 0.2))
        if query.limit is not None and len(materialized) <= query.limit:
            features.append(("limit_shape", 0.1))
    return tuple(features)


def execute_and_rerank(question: str, candidates: Sequence[ScoredQuery], schema: SchemaGraph,
                       executor: Callable[[str], tuple[Sequence[str], Sequence[Sequence[Any]]]],
                       max_candidates: int = 5,
                       preserve_top: bool = True) -> list[ExecutedCandidate]:
    """Execute a bounded semantic prefix without silently changing semantic top-1.

    Result-shape features remain useful for ordering fallback candidates after an
    execution failure. A merely nonempty result is not evidence that a lower-ranked
    query answers the question, so the successful semantic winner stays first.
    """
    observed = []
    for candidate in candidates[:max(1, max_candidates)]:
        try:
            columns, rows = executor(candidate.sql)
            observed.append(ExecutedCandidate(candidate, tuple(columns), tuple(tuple(row) for row in rows)))
        except Exception as exc:  # noqa: BLE001 - failed SQL is evidence against only that candidate
            observed.append(ExecutedCandidate(candidate, error=f"{type(exc).__name__}: {exc}"))
    ranked = CandidateRanker(schema).rank_executions(question, observed)
    if preserve_top and observed and observed[0].error is None:
        top_sql = observed[0].candidate.sql
        top = next(item for item in ranked if item.candidate.sql == top_sql)
        return [top] + [item for item in ranked if item.candidate.sql != top_sql]
    return ranked


def _columns_in_windows(schema: SchemaGraph, tokens: tuple[str, ...], windows: Sequence[tuple[int, int]]) -> set[ColumnRef]:
    out = set()
    for start, end in windows:
        window = set(tokens[start:end])
        for column in schema.columns:
            name_tokens = set(_schema_tokens(column.ref.name))
            if name_tokens and name_tokens <= window:
                out.add(column.ref)
    return out


def _tables_in_windows(schema: SchemaGraph, tokens: tuple[str, ...], windows: Sequence[tuple[int, int]]) -> set[str]:
    out = set()
    for start, end in windows:
        window = set(tokens[start:end])
        for table in schema.tables:
            name_tokens = set(_schema_tokens(table))
            if name_tokens and name_tokens <= window:
                out.add(table)
    return out


def _comparisons(predicate):
    if predicate is None:
        return ()
    if hasattr(predicate, "terms"):
        return tuple(comparison for term in predicate.terms for comparison in _comparisons(term))
    return (predicate,) if isinstance(predicate, Comparison) else ()


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_canon(token) for token in re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", text.lower()))


def _schema_tokens(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(
        "number" if token.lower() == "no" else _canon(token)
        for token in re.findall(r"[A-Za-z0-9]+", spaced)
        if _canon(token) != "id"
    )


def _canon(word: str) -> str:
    word = word.lower().strip()
    if word == "handed":
        return "hand"
    if word == "ids":
        return "id"
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _travel_direction(tokens: tuple[str, ...]) -> str | None:
    token_set = set(tokens)
    if token_set & {"leave", "leaving", "depart", "departing", "departure", "origin", "source"}:
        return "source"
    if token_set & {"arrive", "arriving", "arrival", "land", "landing", "destination", "dest"}:
        return "destination"
    return None


def _travel_column_role(column: ColumnRef) -> str | None:
    words = set(_schema_tokens(column.name))
    if words & {"source", "origin", "departure", "depart", "from"}:
        return "source"
    if words & {"destination", "dest", "arrival", "arrive", "landing", "to"}:
        return "destination"
    return None


def _is_id(name: str) -> bool:
    return bool(re.search(r"(^id$|_?id$|identifier|key$)", name, re.I))


def _is_name(name: str) -> bool:
    return bool(set(_schema_tokens(name)) & {"name", "title", "label"})


def _column_label(column: ColumnRef) -> str:
    return f"{column.table}.{column.name}"
