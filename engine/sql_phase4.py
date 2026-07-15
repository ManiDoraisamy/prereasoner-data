"""Phase 4 aggregate constraints, disjunctions, and relational subqueries.

Phase 4 is a separate deterministic expansion layer so the Phase 3 envelope stays
reproducible.  Rules operate on typed ASTs and schema objects, never SQL strings.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Iterable, Sequence

from engine.sql_ast import (
    Aggregate,
    BooleanExpr,
    ColumnRef,
    Comparison,
    InPredicate,
    Join,
    Literal,
    OrderTerm,
    Predicate,
    Query,
    SQLType,
    ScalarSubquery,
    SelectItem,
    SelectQuery,
    Star,
    and_predicates,
    render_query,
)
from engine.sql_search import SchemaGraph, ScoredQuery


_WORD_NUMBERS = {
    "zero": 0, "one": 1, "single": 1, "two": 2, "couple": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10,
}
_NOISE_VALUES = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "by", "for", "from", "in", "no", "not",
    "is", "of", "on", "or", "the", "to", "with", "without",
})
_COUNT_RELATION_CUES = frozenset({
    "contain", "contains", "conduct", "conducted", "design", "designed", "enroll", "enrolled",
    "has", "have", "having", "make", "operate", "perform", "performed", "produce", "shared",
    "teach", "used", "visit", "visited", "with",
})


@dataclass(frozen=True)
class CountThreshold:
    predicates: tuple[tuple[str, int], ...]
    start: int
    end: int
    strength: float

    @property
    def values(self) -> frozenset[int]:
        return frozenset(value for _, value in self.predicates)


class Phase4Expander:
    """Add bounded candidates for Spider's remaining high-mass structures."""

    def __init__(self, schema: SchemaGraph, max_candidates: int = 180):
        self.schema = schema
        self.max_candidates = max(1, max_candidates)

    def expand(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        generated: list[ScoredQuery] = []
        generated.extend(self._count_having_candidates(question, candidates))
        generated.extend(self._aggregate_having_candidates(question, candidates))
        generated.extend(self._or_candidates(question, candidates))
        generated.extend(self._scalar_aggregate_candidates(question, candidates))
        generated.extend(self._scalar_selector_candidates(question, candidates))
        generated.extend(self._membership_candidates(question, candidates))

        dedup: dict[str, ScoredQuery] = {}
        for candidate in generated:
            old = dedup.get(candidate.sql)
            if old is None or candidate.score > old.score:
                dedup[candidate.sql] = candidate
        return sorted(dedup.values(), key=lambda candidate: (-candidate.score, candidate.sql))[
            :self.max_candidates
        ]

    def _count_having_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        thresholds = _count_thresholds(tokens)
        if not thresholds:
            return []
        out = []
        for threshold in thresholds:
            if self._threshold_targets_column(tokens, threshold):
                continue
            entity_options = self._entity_tables(tokens, threshold)
            for entity_table, entity_score in entity_options[:2]:
                counted_options = self._counted_tables(tokens, threshold, entity_table)
                for counted_table, relation_score in counted_options[:2]:
                    if relation_score <= 0:
                        continue
                    structures = self._having_structures(
                        candidates, entity_table, counted_table, threshold
                    )
                    for source, joins, where, structure_score in structures[:8]:
                        regular_numeric = [
                            comparison for comparison in self._numeric_comparisons(tokens)
                            if comparison.left.table in {source, *(join.table for join in joins)}
                            and not (
                                isinstance(comparison.right, Literal)
                                and comparison.right.value in threshold.values
                            )
                        ]
                        where_options = [(where, 0.0)]
                        if regular_numeric:
                            existing = [
                                term for term in _and_terms(where)
                                if not (
                                    isinstance(term, Comparison)
                                    and isinstance(term.right, Literal)
                                    and term.right.type.numeric
                                )
                            ]
                            direct_where = and_predicates(_unique_predicates(existing + regular_numeric))
                            where_options.append((direct_where, 1.5))
                        projection_options = self._projection_options(
                            question, entity_table, where
                        )
                        if not projection_options:
                            continue
                        group_options = self._group_options(
                            question, entity_table, counted_table, joins, projection_options[0]
                        )
                        select_options = self._having_select_options(
                            question, projection_options
                        )
                        count = Aggregate("COUNT", Star())
                        having_terms = tuple(
                            Comparison(count, operator, Literal(value, SQLType.INTEGER))
                            for operator, value in threshold.predicates
                        )
                        having = and_predicates(having_terms)
                        for selected_where, where_bonus in where_options:
                            filtered_projection = self._projection_options(
                                question, entity_table, selected_where
                            )
                            selected_options = self._having_select_options(
                                question, filtered_projection
                            )
                            for select, select_bonus in selected_options[:4]:
                                raw = tuple(
                                    item.expression for item in select
                                    if isinstance(item.expression, ColumnRef)
                                )
                                groups = raw if any(
                                    isinstance(item.expression, Aggregate) for item in select
                                ) and raw else group_options[0]
                                query = SelectQuery(
                                    select=select,
                                    from_table=source,
                                    joins=joins,
                                    where=selected_where,
                                    group_by=groups,
                                    having=having,
                                )
                                score = (
                                    42.0 + threshold.strength + entity_score + relation_score
                                    + structure_score + select_bonus + where_bonus
                                )
                                built = _candidate(
                                    query,
                                    score,
                                    ("phase4:having:count",),
                                )
                                if built is not None:
                                    out.append(built)
                        # Single-table questions can group by a category other than the projection.
                        if entity_table == counted_table:
                            for group in group_options[1:3]:
                                query = SelectQuery(
                                    select=projection_options[0],
                                    from_table=source,
                                    joins=joins,
                                    where=where,
                                    group_by=group,
                                    having=having,
                                )
                                built = _candidate(
                                    query,
                                    41.0 + threshold.strength + entity_score + relation_score,
                                    ("phase4:having:count-category",),
                                )
                                if built is not None:
                                    out.append(built)
        return out

    def _aggregate_having_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        question_tokens = _tokens(question)
        tokens = set(question_tokens)
        requested = set()
        if tokens & {"average", "avg", "mean"}:
            requested.add("AVG")
        if tokens & {"sum", "total"}:
            requested.add("SUM")
        if not requested:
            return []
        out = []
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not query.group_by or query.where is None:
                continue
            aggregates = [
                item.expression for item in query.select
                if isinstance(item.expression, Aggregate) and item.expression.function in requested
                and isinstance(item.expression.operand, ColumnRef)
            ]
            terms = list(_and_terms(query.where))
            for aggregate in aggregates:
                movable = [
                    term for term in terms
                    if isinstance(term, Comparison) and term.left == aggregate.operand
                    and isinstance(term.right, Literal)
                ]
                movable.extend(
                    comparison for comparison in self._numeric_comparisons(question_tokens)
                    if comparison.left == aggregate.operand
                    and comparison not in movable
                )
                for comparison in movable:
                    if not _explicit_aggregate_constraint(
                        question_tokens, aggregate, comparison.right
                    ):
                        continue
                    where = and_predicates(
                        term for term in terms if term != comparison and not _linker_noise(term)
                    )
                    having_term = Comparison(aggregate, comparison.operator, comparison.right)
                    having = and_predicates(
                        tuple(_and_terms(query.having)) + (having_term,)
                        if query.having is not None else (having_term,)
                    )
                    transformed = replace(query, where=where, having=having, order_by=(), limit=None)
                    built = _candidate(
                        transformed,
                        candidate.score + 24.0,
                        candidate.evidence + (f"phase4:having:{aggregate.function.lower()}",),
                    )
                    if built is not None:
                        out.append(built)
                    sanitized = _sanitized_having_query(
                        transformed, aggregate, question
                    )
                    if sanitized is not None:
                        built = _candidate(
                            sanitized,
                            candidate.score + 29.0,
                            candidate.evidence
                            + (f"phase4:having:{aggregate.function.lower()}:sanitized",),
                        )
                        if built is not None:
                            out.append(built)
        return out

    def _or_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        if "or" not in tokens and "either" not in tokens:
            return []
        out = []
        numeric = self._numeric_comparisons(tokens)
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or query.where is None:
                continue
            terms = [term for term in _and_terms(query.where) if not _linker_noise(term)]
            if len(terms) >= 2:
                transformed = replace(
                    query,
                    where=_build_or_predicate(terms),
                    order_by=() if not _explicit_order(tokens) else query.order_by,
                    limit=None if not _explicit_order(tokens) else query.limit,
                )
                built = _candidate(
                    transformed,
                    candidate.score + 19.0,
                    candidate.evidence + ("phase4:where-or",),
                )
                if built is not None:
                    out.append(built)

            physical = _physical_tables(query)
            direct_numeric = [comparison for comparison in numeric if comparison.left.table in physical]
            categorical = [
                term for term in terms
                if isinstance(term, Comparison) and isinstance(term.right, Literal)
                and not term.right.type.numeric
            ]
            direct_terms = _unique_predicates(categorical + direct_numeric)
            if len(direct_terms) < 2:
                continue
            transformed = replace(
                query,
                where=_build_or_predicate(direct_terms),
                order_by=() if not _explicit_order(tokens) else query.order_by,
                limit=None if not _explicit_order(tokens) else query.limit,
            )
            built = _candidate(
                transformed,
                candidate.score + 21.0,
                candidate.evidence + ("phase4:where-or-linked",),
            )
            if built is not None:
                out.append(built)
            for select, select_bonus in self._or_select_options(question, query, direct_terms):
                sanitized = replace(
                    transformed,
                    select=select,
                    group_by=(),
                    having=None,
                    distinct=(
                        query.distinct
                        or bool(set(tokens) & {"distinct", "distinctive", "different", "unique"})
                        or _needs_distinct_for_relation(select, direct_terms)
                    ),
                )
                built = _candidate(
                    sanitized,
                    candidate.score + 24.0 + select_bonus,
                    candidate.evidence + ("phase4:where-or-sanitized",),
                )
                if built is not None:
                    out.append(built)
        out.extend(self._direct_or_candidates(question, candidates))
        return out

    def _scalar_aggregate_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        normalized = " ".join(_tokens(question))
        if set(_tokens(question)) & {"different", "each", "per"}:
            return []
        out = []
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery):
                continue
            aggregates = [
                item.expression for item in query.select
                if isinstance(item.expression, Aggregate)
                and item.expression.function in {"AVG", "MIN", "MAX"}
                and isinstance(item.expression.operand, ColumnRef)
            ]
            for aggregate in aggregates:
                operator = _scalar_aggregate_operator(aggregate.function, normalized)
                if operator is None:
                    continue
                target = aggregate.operand
                projections = tuple(
                    item for item in query.select if isinstance(item.expression, ColumnRef)
                )
                preferred = {
                    column
                    for table in _physical_tables(query)
                    for column, _, _ in self._projection_columns(_tokens(question), table)
                }
                filtered_projections = tuple(
                    item for item in projections if item.expression in preferred
                )
                if preferred and not filtered_projections:
                    continue
                projections = filtered_projections or projections
                if not projections:
                    continue
                inner = SelectQuery((SelectItem(aggregate),), target.table)
                terms = [
                    term for term in _and_terms(query.where) if not _linker_noise(term)
                    and not (
                        isinstance(term, Comparison) and term.left == target
                        and isinstance(term.right, Literal)
                    )
                ] if query.where is not None else []
                predicate = Comparison(target, operator, ScalarSubquery(inner))
                select_options = [projections]
                if _column_requested_as_output(target, question):
                    with_target = projections + (SelectItem(target),)
                    select_options.extend((with_target, tuple(reversed(with_target))))
                for select in select_options:
                    transformed = replace(
                        query,
                        select=select,
                        where=and_predicates(terms + [predicate]),
                        group_by=(),
                        having=None,
                        order_by=(),
                        limit=None,
                    )
                    built = _candidate(
                        transformed,
                        candidate.score + 25.0,
                        candidate.evidence
                        + (f"phase4:scalar-{aggregate.function.lower()}:{operator}",),
                    )
                    if built is not None:
                        out.append(built)
        functions = []
        token_set = set(_tokens(question))
        if token_set & {"average", "avg", "mean"}:
            functions.append("AVG")
        if token_set & {"earliest", "lowest", "minimum", "min"}:
            functions.append("MIN")
        if token_set & {"latest", "highest", "largest", "biggest", "maximum", "max"}:
            functions.append("MAX")
        for function in functions:
            operator = _scalar_aggregate_operator(function, normalized)
            if operator is None:
                continue
            cue_words = {
                "AVG": {"average", "avg", "mean"},
                "MIN": {"earliest", "lowest", "minimum", "min"},
                "MAX": {"latest", "highest", "largest", "biggest", "maximum", "max"},
            }[function]
            cue_positions = [
                index for index, token in enumerate(_tokens(question)) if token in cue_words
            ]
            targets = [
                schema_column.ref for schema_column in self.schema.columns
                if _column_matches(schema_column.ref.name, set(_tokens(question)))
                and (function != "AVG" or schema_column.ref.type.numeric)
                and any(
                    abs(column_position - cue_position) <= 3
                    for column_position, token in enumerate(_tokens(question))
                    if token in set(_semantic_tokens(schema_column.ref.name))
                    for cue_position in cue_positions
                )
            ]
            for target in targets:
                inner = SelectQuery(
                    (SelectItem(Aggregate(function, target)),), target.table
                )
                for candidate in candidates:
                    query = candidate.query
                    if not isinstance(query, SelectQuery) or target.table not in _physical_tables(query):
                        continue
                    select = tuple(
                        item for item in query.select
                        if isinstance(item.expression, ColumnRef) and item.expression != target
                    )
                    if not select:
                        linked = []
                        for table in _physical_tables(query):
                            linked.extend(
                                (column, score, position)
                                for column, score, position in self._projection_columns(
                                    _tokens(question), table
                                )
                                if column != target
                            )
                        linked.sort(
                            key=lambda item: (item[2], -item[1], item[0].table, item[0].name)
                        )
                        select = tuple(SelectItem(column) for column, _, _ in linked[:4])
                    if _column_requested_as_output(target, question):
                        select += (SelectItem(target),)
                    select = tuple(dict.fromkeys(select))
                    if not select:
                        continue
                    physical = _physical_tables(query)
                    direct_filters = [
                        comparison for comparison in self._numeric_comparisons(_tokens(question))
                        if comparison.left.table in physical and comparison.left != target
                    ]
                    categorical = [
                        term for term in _and_terms(query.where)
                        if isinstance(term, Comparison) and isinstance(term.right, Literal)
                        and not term.right.type.numeric and not _linker_noise(term)
                    ]
                    where = and_predicates(
                        _unique_predicates(categorical + direct_filters + [
                            Comparison(target, operator, ScalarSubquery(inner))
                        ])
                    )
                    transformed = replace(
                        query,
                        select=select,
                        where=where,
                        group_by=(), having=None, order_by=(), limit=None,
                    )
                    built = _candidate(
                        transformed,
                        candidate.score + 31.0,
                        candidate.evidence + (f"phase4:scalar-{function.lower()}:direct",),
                    )
                    if built is not None:
                        out.append(built)
        return out

    def _scalar_selector_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        token_set = set(tokens)
        out = []
        superlative_positions = [
            index for index, token in enumerate(tokens)
            if token in {"highest", "largest", "biggest", "most", "lowest", "smallest", "least"}
        ]
        comparative = ">" if token_set & {"larger", "greater", "higher", "more"} else (
            "<" if token_set & {"smaller", "lower", "less", "fewer"} else None
        )
        if superlative_positions and comparative is not None:
            all_mentions = self._mentioned_columns(tokens, numeric=False)
            for table in self.schema.tables:
                columns = [(position, column) for position, column in all_mentions if column.table == table]
                if len({column for _, column in columns}) < 2:
                    continue
                super_position = superlative_positions[-1]
                target = min(columns, key=lambda item: (abs(item[0] - super_position), item[1].name))[1]
                measures = [
                    column for _, column in columns
                    if column != target and column.type.numeric
                ]
                for measure in dict.fromkeys(measures):
                    direction = "DESC" if tokens[super_position] in {
                        "highest", "largest", "biggest", "most"
                    } else "ASC"
                    inner = SelectQuery(
                        (SelectItem(measure),),
                        table,
                        order_by=(OrderTerm(target, direction),),
                        limit=1,
                    )
                    select = (SelectItem(Aggregate("COUNT", Star())),) if _count_requested(tokens) else (
                        SelectItem(self.schema.display_columns(table)[0]),
                    )
                    outer = SelectQuery(
                        select,
                        table,
                        where=Comparison(measure, comparative, ScalarSubquery(inner)),
                    )
                    built = _candidate(
                        outer,
                        50.0,
                        ("phase4:scalar-row-selector",),
                    )
                    if built is not None:
                        out.append(built)

        rare_direction = None
        if "rarest" in token_set or {"least", "common"} <= token_set:
            rare_direction = "ASC"
        elif {"most", "common"} <= token_set or "commonest" in token_set:
            rare_direction = "DESC"
        if rare_direction is not None:
            text_mentions = self._mentioned_columns(tokens, numeric=False)
            targets = [column for _, column in text_mentions if not column.type.numeric]
            for target in dict.fromkeys(targets):
                inner = SelectQuery(
                    (SelectItem(target),),
                    target.table,
                    group_by=(target,),
                    order_by=(OrderTerm(Aggregate("COUNT", Star()), rare_direction),),
                    limit=1,
                )
                for candidate in candidates:
                    query = candidate.query
                    if not isinstance(query, SelectQuery) or target.table not in _physical_tables(query):
                        continue
                    select = tuple(
                        item for item in query.select
                        if isinstance(item.expression, ColumnRef) and item.expression != target
                    )
                    if not select:
                        continue
                    transformed = replace(
                        query,
                        select=select,
                        where=Comparison(target, "=", ScalarSubquery(inner)),
                        group_by=(),
                        having=None,
                        order_by=(),
                        limit=None,
                    )
                    built = _candidate(
                        transformed,
                        candidate.score + 27.0,
                        candidate.evidence + ("phase4:scalar-frequency-selector",),
                    )
                    if built is not None:
                        out.append(built)
        return out

    def _membership_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        normalized = " ".join(_tokens(question))
        out = []
        positive_cue = bool(re.search(
            r"\b(?:who|that) (?:have|has)|\b(?:gone|went) through|\bwith any\b|\bhave some\b",
            normalized,
        ))
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not query.joins:
                continue
            if positive_cue:
                scalar_aggregates = [
                    item for item in query.select
                    if isinstance(item.expression, Aggregate)
                    and isinstance(item.expression.operand, ColumnRef)
                ]
                if scalar_aggregates and not query.group_by:
                    entity_table = scalar_aggregates[0].expression.operand.table
                    key = _entity_join_key(query, entity_table)
                    entity_select = tuple(
                        item for item in query.select
                        if _expression_table(item.expression) in {None, entity_table}
                    )
                    if key is not None and entity_select:
                        membership = replace(
                            query,
                            select=(SelectItem(key),),
                            group_by=(), having=None, order_by=(), limit=None,
                        )
                        outer = SelectQuery(
                            entity_select,
                            entity_table,
                            where=InPredicate(key, membership),
                        )
                        built = _candidate(
                            outer,
                            candidate.score + 21.0,
                            candidate.evidence + ("phase4:in-membership",),
                        )
                        if built is not None:
                            out.append(built)

            if "but" not in normalized and "not" not in normalized:
                continue
            terms = list(_and_terms(query.where)) if query.where is not None else []
            negative = [
                term for term in terms
                if isinstance(term, Comparison) and term.operator in {"!=", "<>"}
                and isinstance(term.right, Literal)
            ]
            positive = [
                term for term in terms
                if isinstance(term, Comparison) and term.operator == "="
                and isinstance(term.right, Literal)
            ]
            if not negative or not positive:
                continue
            entity_table = self._entity_tables(_tokens(question), CountThreshold((("=", 1),), 0, 0, 0))[0][0]
            key = _entity_join_key(query, entity_table)
            if key is None:
                continue
            entity_select = self._projection_options(question, entity_table, None)[0]
            negative_terms = [Comparison(term.left, "=", term.right) for term in negative]
            membership = replace(
                query,
                select=(SelectItem(key),),
                where=and_predicates(negative_terms),
                group_by=(), having=None, order_by=(), limit=None,
            )
            outer_where = and_predicates(
                [*positive, InPredicate(key, membership, negated=True)]
            )
            outer = replace(
                query,
                select=entity_select,
                where=outer_where,
                group_by=(), having=None, order_by=(), limit=None,
            )
            built = _candidate(
                outer,
                candidate.score + 25.0,
                candidate.evidence + ("phase4:positive-and-not-in",),
            )
            if built is not None:
                out.append(built)
        if positive_cue:
            out.extend(self._direct_membership_candidates(question, candidates))
        return out

    def _direct_membership_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        cue_position = next(
            (index for index, token in enumerate(tokens) if token in {"have", "has", "through"}),
            len(tokens),
        )
        out = []
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or query.group_by:
                continue
            aggregates = [
                item for item in query.select
                if isinstance(item.expression, Aggregate)
                and isinstance(item.expression.operand, ColumnRef)
            ]
            if not aggregates:
                continue
            entity_table = aggregates[0].expression.operand.table
            subject_tokens = set(tokens[:cue_position])
            for fk in self.schema.foreign_keys:
                if fk.to_column.table == entity_table:
                    entity_key, relation_key = fk.to_column, fk.from_column
                elif fk.from_column.table == entity_table:
                    entity_key, relation_key = fk.from_column, fk.to_column
                else:
                    continue
                relation_words = set(_semantic_tokens(relation_key.table))
                if not relation_words & set(tokens):
                    continue
                role_words = set(_semantic_tokens(relation_key.name))
                role_bonus = 3.0 if role_words & subject_tokens else 0.0
                membership = SelectQuery(
                    (SelectItem(relation_key),), relation_key.table
                )
                outer = SelectQuery(
                    tuple(aggregates),
                    entity_table,
                    where=InPredicate(entity_key, membership),
                )
                built = _candidate(
                    outer,
                    47.0 + role_bonus,
                    ("phase4:in-membership:direct",),
                )
                if built is not None:
                    out.append(built)
        return out

    def _or_select_options(
        self, question: str, query: SelectQuery, predicates: Sequence[Predicate]
    ) -> list[tuple[tuple[SelectItem, ...], float]]:
        tokens = _tokens(question)
        token_set = set(tokens)
        aggregates = [
            item.expression for item in query.select if isinstance(item.expression, Aggregate)
        ]
        if aggregates:
            if _count_requested(tokens):
                count = next((aggregate for aggregate in aggregates if aggregate.function == "COUNT"), None)
                return [((SelectItem(count or Aggregate("COUNT", Star())),), 3.0)]
            function_cues = {
                "MAX": {"max", "maximum"}, "MIN": {"min", "minimum"},
                "AVG": {"avg", "average", "mean"}, "SUM": {"sum", "total"},
            }
            selected = [
                aggregate for aggregate in aggregates
                if function_cues.get(aggregate.function, set()) & token_set
                and (
                    not isinstance(aggregate.operand, ColumnRef)
                    or _column_matches(
                        aggregate.operand.name,
                        {token for _, token in _projection_window(tokens)},
                    )
                )
            ]
            if selected:
                return [(tuple(SelectItem(aggregate) for aggregate in selected), 3.0)]

        filter_columns = {
            predicate.left for predicate in predicates
            if isinstance(predicate, Comparison) and isinstance(predicate.left, ColumnRef)
        }
        linked = []
        for table in _physical_tables(query):
            linked.extend(
                (column, score, position)
                for column, score, position in self._projection_columns(tokens, table)
                if column not in filter_columns
            )
        linked.sort(key=lambda item: (item[2], -item[1], item[0].table, item[0].name))
        options = [((SelectItem(column),), score) for column, score, _ in linked[:4]]
        if linked:
            options.append((tuple(SelectItem(column) for column, _, _ in linked[:4]), 0.5))
        return options

    def _direct_or_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        numeric = self._numeric_comparisons(tokens)
        categorical = []
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery):
                continue
            categorical.extend(
                term for term in _and_terms(query.where)
                if isinstance(term, Comparison) and isinstance(term.right, Literal)
                and not term.right.type.numeric and not _linker_noise(term)
            )
        predicates = _unique_predicates(
            categorical + numeric + self._semantic_common_comparisons(tokens)
        )
        if len(predicates) < 2:
            return []
        required = {
            predicate.left.table for predicate in predicates
            if isinstance(predicate, Comparison) and isinstance(predicate.left, ColumnRef)
        }
        if not required:
            return []

        select_options = []
        if _count_requested(tokens):
            select_options.append(((SelectItem(Aggregate("COUNT", Star())),), 4.0))
        function = None
        if set(tokens) & {"maximum", "max"}:
            function = "MAX"
        elif set(tokens) & {"minimum", "min"}:
            function = "MIN"
        elif set(tokens) & {"average", "avg", "mean"}:
            function = "AVG"
        if function is not None:
            projection_tokens = {token for _, token in _projection_window(tokens)}
            for schema_column in self.schema.columns:
                column = schema_column.ref
                if _column_matches(column.name, projection_tokens) and (
                    function not in {"SUM", "AVG"} or column.type.numeric
                ):
                    select_options.append(((SelectItem(Aggregate(function, column)),), 4.0))
                    required.add(column.table)
        if not select_options:
            linked = []
            for table in self.schema.tables:
                linked.extend(self._projection_columns(tokens, table))
            linked.sort(key=lambda item: (item[2], -item[1], item[0].table, item[0].name))
            filter_columns = {
                predicate.left for predicate in predicates
                if isinstance(predicate, Comparison) and isinstance(predicate.left, ColumnRef)
            }
            for column, score, position in linked:
                if column in filter_columns:
                    continue
                position_bonus = max(0.0, 3.0 - 0.2 * position)
                select_options.append(((SelectItem(column),), score + position_bonus))
                required.add(column.table)
        if not select_options:
            projection_tokens = {token for _, token in _projection_window(tokens)}
            entity_tables = sorted(
                (
                    (len(set(_semantic_tokens(table)) & projection_tokens), table)
                    for table in self.schema.tables
                ),
                key=lambda item: (-item[0], item[1]),
            )
            if entity_tables and entity_tables[0][0] > 0:
                entity_table = entity_tables[0][1]
                display = self.schema.display_columns(entity_table)
                if display:
                    select_options.append(((SelectItem(display[0]),), 2.5))
                    required.add(entity_table)

        out = []
        for tree in self.schema.join_trees(required, limit=8):
            for select, select_bonus in select_options[:4]:
                query = SelectQuery(
                    select,
                    tree.root,
                    joins=tree.joins,
                    where=_build_or_predicate(predicates),
                    distinct=(
                        bool(set(tokens) & {"distinct", "distinctive", "different", "unique"})
                        or _needs_distinct_for_relation(select, predicates)
                    ),
                )
                built = _candidate(
                    query,
                    51.0 + select_bonus - 0.2 * len(tree.joins),
                    ("phase4:where-or:direct",),
                )
                if built is not None:
                    out.append(built)
        return out

    def _semantic_common_comparisons(self, tokens: tuple[str, ...]) -> list[Comparison]:
        if "official" not in tokens:
            return []
        out = []
        truthy = {"1", "t", "true", "y", "yes"}
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if "official" not in _semantic_tokens(column.name):
                continue
            value = next(
                (
                    value for value in schema_column.values
                    if str(value).strip().lower() in truthy
                ),
                None,
            )
            if value is not None:
                out.append(Comparison(column, "=", Literal(value, column.type)))
        return out

    def _entity_tables(
        self, tokens: tuple[str, ...], threshold: CountThreshold
    ) -> list[tuple[str, float]]:
        prefix = set(tokens[:max(threshold.start, 1)])
        all_tokens = set(tokens)
        scored = []
        for table in self.schema.tables:
            projection = self._projection_columns(tokens, table)
            table_words = set(_semantic_tokens(table))
            prefix_overlap = len(table_words & prefix)
            global_overlap = len(table_words & all_tokens)
            mention_positions = [
                index for index, token in enumerate(tokens[:threshold.start])
                if token in table_words
            ]
            mention_score = 8.0 + 0.1 * (threshold.start - min(mention_positions)) \
                if mention_positions else 0.0
            score = 2.0 * len(projection) + 2.0 * prefix_overlap + global_overlap + mention_score
            scored.append((table, score))
        return sorted(scored, key=lambda item: (-item[1], item[0]))

    def _counted_tables(
        self, tokens: tuple[str, ...], threshold: CountThreshold, entity_table: str
    ) -> list[tuple[str, float]]:
        suffix = set(tokens[max(0, threshold.start - 7):min(len(tokens), threshold.end + 7)])
        scored = []
        for table in self.schema.tables:
            words = set(_semantic_tokens(table))
            overlap = len(words & suffix)
            score = 3.0 * overlap
            if table != entity_table and overlap:
                score += 0.75
            scored.append((table, score))
        return sorted(scored, key=lambda item: (-item[1], item[0]))

    def _having_structures(
        self, candidates: Sequence[ScoredQuery], entity_table: str,
        counted_table: str, threshold: CountThreshold,
    ) -> list[tuple[str, tuple[Join, ...], Predicate | None, float]]:
        structures = []
        required = {entity_table, counted_table}
        join_trees = self.schema.join_trees(required, preferred_root=counted_table, limit=4)
        for tree in join_trees:
            structures.append((tree.root, tree.joins, None, -0.2 * len(tree.joins)))
        if entity_table != counted_table and not join_trees:
            inferred = self._inferred_entity_join(entity_table, counted_table)
            if inferred is not None:
                structures.append((counted_table, (inferred,), None, -0.25))
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not isinstance(query.from_table, str):
                continue
            if not required <= _physical_tables(query):
                continue
            where = _clean_threshold_where(query.where, threshold.values)
            structures.append((query.from_table, query.joins, where, 0.5 + 0.2 * len(_and_terms(where)) if where else 0.5))
        dedup = {}
        for structure in structures:
            key = (structure[0], structure[1], repr(structure[2]))
            old = dedup.get(key)
            if old is None or structure[3] > old[3]:
                dedup[key] = structure
        return sorted(dedup.values(), key=lambda item: (-item[3], repr(item)))

    def _inferred_entity_join(self, entity_table: str, counted_table: str) -> Join | None:
        """Infer a missing FK only from a unique parent key and near-subset child values."""
        entity_words = set(_semantic_tokens(entity_table))
        options = []
        for parent in self.schema.by_table.get(entity_table, ()):
            parent_values = _value_set(parent.values)
            if len(parent_values) < 2 or len(parent_values) < 0.95 * _value_count(parent.values):
                continue
            for child in self.schema.by_table.get(counted_table, ()):
                if not _compatible_types(parent.ref.type, child.ref.type):
                    continue
                child_values = _value_set(child.values)
                if len(child_values) < 2 or _value_count(child.values) < 1.2 * len(child_values):
                    continue
                overlap = len(parent_values & child_values) / len(child_values)
                if overlap < 0.9:
                    continue
                child_words = set(_semantic_tokens(child.ref.name))
                parent_words = set(_semantic_tokens(parent.ref.name))
                role_match = bool(child_words & entity_words)
                name_match = bool(child_words & parent_words)
                if not role_match and not name_match:
                    continue
                score = 4.0 * overlap + 2.0 * role_match + name_match
                options.append((score, parent.ref, child.ref))
        if not options:
            return None
        _, parent, child = max(
            options,
            key=lambda item: (item[0], item[1].name, item[2].name),
        )
        return Join(entity_table, child, parent)

    def _projection_columns(
        self, tokens: tuple[str, ...], table: str
    ) -> list[tuple[ColumnRef, float, int]]:
        window = _projection_window(tokens)
        token_set = {token for _, token in window}
        explicit_id = bool(token_set & {"id", "identifier", "code"})
        matches = []
        for schema_column in self.schema.by_table.get(table, ()):
            column = schema_column.ref
            compact = re.sub(r"[^a-z0-9]", "", column.name.lower())
            if _is_id(column.name) and not explicit_id and compact not in token_set:
                continue
            if not _column_matches(column.name, token_set, table):
                continue
            words = set(_semantic_tokens(column.name))
            compact_hits = {index for index, token in window if token == compact}
            semantic_hits = {index for index, token in window if token in words}
            coverage = compact_hits or semantic_hits
            specificity = len(words & token_set)
            position = min(coverage) if coverage else len(tokens) + 5
            matches.append((column, float(specificity), position, coverage, words))
        out = []
        for column, specificity, position, coverage, words in matches:
            shadowed = any(
                words < other_words and coverage and coverage <= other_coverage
                for other, _, _, other_coverage, other_words in matches
                if other != column
            )
            if not shadowed:
                out.append((column, specificity, position))
        return sorted(out, key=lambda item: (item[2], -item[1], item[0].name))

    def _projection_options(
        self, question: str, table: str, where: Predicate | None
    ) -> list[tuple[SelectItem, ...]]:
        tokens = _tokens(question)
        filter_columns = {
            term.left for term in _and_terms(where)
            if isinstance(term, Comparison) and isinstance(term.left, ColumnRef)
        } if where is not None else set()
        linked = [
            column for column, _, _ in self._projection_columns(tokens, table)
            if column not in filter_columns
        ]
        if not linked:
            linked = list(self.schema.display_columns(table)[:1])
        full = tuple(SelectItem(column) for column in dict.fromkeys(linked[:4]))
        options = [full]
        options.extend((SelectItem(column),) for column in linked[:4])
        return list(dict.fromkeys(options))

    def _group_options(
        self, question: str, entity_table: str, counted_table: str,
        joins: tuple[Join, ...], projection: tuple[SelectItem, ...],
    ) -> list[tuple[ColumnRef, ...]]:
        if entity_table != counted_table:
            key = _join_key(joins, entity_table)
            if key is not None:
                return [(key,)]
        tokens = _tokens(question)
        all_tokens = set(tokens)
        columns = [
            schema_column.ref for schema_column in self.schema.by_table.get(entity_table, ())
            if _column_matches(schema_column.ref.name, all_tokens, entity_table)
        ]
        projected = [item.expression for item in projection if isinstance(item.expression, ColumnRef)]
        if projected and all(_is_id(column.name) for column in projected):
            ordered = [column for column in columns if not _is_id(column.name)] + projected
        else:
            ordered = projected + columns
        options = [(column,) for column in dict.fromkeys(ordered)]
        return options or [tuple(self.schema.display_columns(entity_table)[:1])]

    @staticmethod
    def _having_select_options(
        question: str, projection_options: list[tuple[SelectItem, ...]]
    ) -> list[tuple[tuple[SelectItem, ...], float]]:
        tokens = _tokens(question)
        projection = projection_options[0]
        if _top_level_count(tokens):
            return [((SelectItem(Aggregate("COUNT", Star())),), 3.0)]
        out = [(projection, 2.0)]
        if _count_requested(tokens):
            count = SelectItem(Aggregate("COUNT", Star()))
            out.append(((count,) + projection, 4.0))
            out.append((projection + (count,), 3.5))
        out.extend((option, 0.25) for option in projection_options[1:3])
        return out

    def _numeric_comparisons(self, tokens: tuple[str, ...]) -> list[Comparison]:
        out = []
        mentions = self._mentioned_columns(tokens, numeric=True)
        for index, token in enumerate(tokens):
            value = _parse_number(token)
            if value is None:
                continue
            if isinstance(value, int) and 1900 <= value <= 2100:
                year_columns = [
                    column.ref for column in self.schema.columns
                    if "year" in _semantic_tokens(column.ref.name) or column.ref.type == SQLType.DATE
                ]
                targets = year_columns[:3]
            else:
                nearby = sorted(
                    mentions,
                    key=lambda item: (abs(item[0] - index), item[1].table, item[1].name),
                )
                targets = [column for position, column in nearby if abs(position - index) <= 4][:3]
            operator = _nearby_operator(tokens, index)
            for target in dict.fromkeys(targets):
                out.append(Comparison(target, operator, Literal(value, target.type)))
        return out

    def _threshold_targets_column(
        self, tokens: tuple[str, ...], threshold: CountThreshold
    ) -> bool:
        number_positions = [
            index for index in range(threshold.start, min(len(tokens), threshold.end + 1))
            if _parse_number(tokens[index]) is not None
        ]
        if not number_positions:
            return False
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if _is_id(column.name) or not column.type.numeric:
                continue
            for number_position in number_positions:
                local = set(tokens[max(0, number_position - 3):min(len(tokens), number_position + 2)])
                if _column_matches(column.name, local):
                    return True
        return False

    def _mentioned_columns(
        self, tokens: tuple[str, ...], numeric: bool
    ) -> list[tuple[int, ColumnRef]]:
        out = []
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if numeric and (not column.type.numeric or _is_id(column.name)):
                continue
            words = set(_semantic_tokens(column.name))
            for index, token in enumerate(tokens):
                if token in words:
                    out.append((index, column))
        return out


def _candidate(query: Query, score: float, evidence: tuple[str, ...]) -> ScoredQuery | None:
    try:
        sql = render_query(query)
    except (TypeError, ValueError):
        return None
    return ScoredQuery(query, sql, score, evidence)


def _explicit_aggregate_constraint(
    tokens: tuple[str, ...], aggregate: Aggregate, literal: Literal
) -> bool:
    cues = {
        "AVG": {"average", "avg", "mean"},
        "SUM": {"sum", "total"},
    }.get(aggregate.function, set())
    cue_positions = [index for index, token in enumerate(tokens) if token in cues]
    literal_positions = [
        index for index, token in enumerate(tokens)
        if _parse_number(token) == literal.value
    ]
    return any(
        abs(cue_position - literal_position) <= 7
        for cue_position in cue_positions
        for literal_position in literal_positions
    )


def _build_or_predicate(predicates: Sequence[Predicate]) -> Predicate:
    terms = _unique_predicates(predicates)
    by_column: dict[ColumnRef, list[Predicate]] = {}
    for term in terms:
        if isinstance(term, Comparison) and isinstance(term.left, ColumnRef):
            by_column.setdefault(term.left, []).append(term)
    repeated = [group for group in by_column.values() if len(group) >= 2]
    if not repeated:
        return BooleanExpr("OR", tuple(terms))
    alternatives = max(repeated, key=lambda group: (len(group), repr(group[0])))
    common = [term for term in terms if term not in alternatives]
    disjunction = BooleanExpr("OR", tuple(alternatives))
    return and_predicates(common + [disjunction]) or disjunction


def _needs_distinct_for_relation(
    select: tuple[SelectItem, ...], predicates: Sequence[Predicate]
) -> bool:
    select_tables = {
        item.expression.table
        for item in select
        if isinstance(item.expression, ColumnRef)
    }
    if not select_tables:
        return False
    return any(
        isinstance(predicate, Comparison)
        and isinstance(predicate.left, ColumnRef)
        and predicate.left.table not in select_tables
        for predicate in predicates
    )


def _count_thresholds(tokens: tuple[str, ...]) -> list[CountThreshold]:
    out = []
    for index in range(len(tokens)):
        if tokens[index:index + 2] == ("between", "one"):
            pass
        if tokens[index] == "between" and index + 3 < len(tokens):
            low = _parse_number(tokens[index + 1])
            high = _parse_number(tokens[index + 3]) if tokens[index + 2] in {"and", "to"} else None
            if low is not None and high is not None:
                out.append(CountThreshold(((">=", int(low)), ("<=", int(high))), index, index + 4, 4.0))
        patterns = [
            (("at", "least"), ">=", 3.5), (("at", "most"), "<=", 3.5),
            (("more", "than"), ">", 3.0), (("greater", "than"), ">", 3.0),
            (("fewer", "than"), "<", 3.0), (("less", "than"), "<", 3.0),
            (("no", "more", "than"), "<=", 3.5), (("exactly",), "=", 3.0),
            (("only",), "=", 2.5), (("over",), ">", 2.0),
        ]
        for cue, operator, strength in patterns:
            if tokens[index:index + len(cue)] != cue:
                continue
            number_index = index + len(cue)
            if number_index >= len(tokens):
                continue
            number = _parse_number(tokens[number_index])
            if number is not None:
                out.append(CountThreshold(((operator, int(number)),), index, number_index + 1, strength))
        number = _parse_number(tokens[index])
        if number is not None and tokens[index + 1:index + 3] == ("or", "more"):
            out.append(CountThreshold(((">=", int(number)),), index, index + 3, 3.5))
        if number is not None and tokens[index + 1:index + 3] == ("or", "fewer"):
            out.append(CountThreshold((("<=", int(number)),), index, index + 3, 3.5))
        if number is not None:
            context = set(tokens[max(0, index - 3):index])
            explicit = context & {"at", "least", "most", "more", "greater", "fewer", "less", "than", "over", "only", "exactly"}
            if context & _COUNT_RELATION_CUES and not explicit:
                out.append(CountThreshold((("=", int(number)),), index, index + 1, 1.0))
    dedup = {}
    for threshold in out:
        key = threshold.predicates
        old = dedup.get(key)
        if old is None or threshold.strength > old.strength:
            dedup[key] = threshold
    return sorted(dedup.values(), key=lambda item: (-item.strength, item.start))[:4]


def _scalar_aggregate_operator(function: str, normalized: str) -> str | None:
    if function == "AVG":
        if re.search(r"\b(?:older|above|greater|higher|more)\s+(?:than\s+)?(?:the\s+)?(?:average|avg|mean)\b", normalized):
            return ">"
        if re.search(r"\b(?:younger|below|less|lower|fewer|smaller)\s+(?:than\s+)?(?:the\s+)?(?:average|avg|mean)\b", normalized):
            return "<"
    if function == "MIN" and re.search(r"\b(?:earliest|lowest|minimum|min)\b", normalized):
        if re.search(r"\b(?:more|greater|higher) than (?:the )?(?:lowest|minimum|min)\b", normalized):
            return ">"
        if re.search(r"\bnot (?:have )?(?:the )?(?:minimum|lowest)\b", normalized):
            return ">"
        return "="
    if function == "MAX" and re.search(r"\b(?:latest|highest|largest|biggest|maximum|max)\b", normalized):
        return "="
    return None


def _sanitized_having_query(
    query: SelectQuery, having_aggregate: Aggregate, question: str
) -> SelectQuery | None:
    tokens = _tokens(question)
    projection_tokens = {token for _, token in _projection_window(tokens)}
    function_cues = {
        "AVG": {"average", "avg", "mean"}, "SUM": {"sum", "total"},
        "MIN": {"minimum", "min"}, "MAX": {"maximum", "max"},
    }
    selected_aggregates = []
    for item in query.select:
        expression = item.expression
        if not isinstance(expression, Aggregate):
            continue
        if not function_cues.get(expression.function, set()) & projection_tokens:
            continue
        if isinstance(expression.operand, ColumnRef) and not _column_matches(
            expression.operand.name, projection_tokens
        ):
            continue
        selected_aggregates.append(item)

    groups = sorted(
        set(query.group_by),
        key=lambda column: (
            -len(set(_semantic_tokens(column.name)) & projection_tokens),
            column.table,
            column.name,
        ),
    )
    if not groups:
        return None
    group = groups[0]
    select = tuple(selected_aggregates) + (SelectItem(group),)
    if not selected_aggregates:
        return None
    return replace(query, select=select, group_by=(group,), order_by=(), limit=None)


def _clean_threshold_where(
    predicate: Predicate | None, values: frozenset[int]
) -> Predicate | None:
    if predicate is None:
        return None
    terms = []
    for term in _and_terms(predicate):
        if _linker_noise(term):
            continue
        if isinstance(term, Comparison) and isinstance(term.right, Literal):
            try:
                if int(term.right.value) in values:
                    continue
            except (TypeError, ValueError):
                pass
        terms.append(term)
    return and_predicates(terms)


def _and_terms(predicate: Predicate | None) -> tuple[Predicate, ...]:
    if predicate is None:
        return ()
    if isinstance(predicate, BooleanExpr) and predicate.operator == "AND":
        return tuple(term for child in predicate.terms for term in _and_terms(child))
    return (predicate,)


def _linker_noise(predicate: Predicate) -> bool:
    return (
        isinstance(predicate, Comparison)
        and isinstance(predicate.right, Literal)
        and str(predicate.right.value).strip().lower() in _NOISE_VALUES
    )


def _physical_tables(query: SelectQuery) -> set[str]:
    out = {query.from_table} if isinstance(query.from_table, str) else set()
    out.update(join.table for join in query.joins)
    return out


def _join_key(joins: tuple[Join, ...], table: str) -> ColumnRef | None:
    columns = [column for join in joins for column in (join.left, join.right) if column.table == table]
    return sorted(set(columns), key=lambda column: (0 if _is_id(column.name) else 1, column.name))[0] if columns else None


def _entity_join_key(query: SelectQuery, table: str) -> ColumnRef | None:
    return _join_key(query.joins, table)


def _expression_table(expression) -> str | None:
    if isinstance(expression, ColumnRef):
        return expression.table
    if isinstance(expression, Aggregate) and isinstance(expression.operand, ColumnRef):
        return expression.operand.table
    return None


def _unique_predicates(predicates: Iterable[Predicate]) -> list[Predicate]:
    out = []
    seen = set()
    for predicate in predicates:
        key = repr(predicate)
        if key not in seen:
            seen.add(key)
            out.append(predicate)
    return out


def _nearby_operator(tokens: tuple[str, ...], index: int) -> str:
    before = tokens[max(0, index - 4):index]
    after = tokens[index + 1:index + 3]
    if "not" in before and len(before) >= 2 and before[-2:] == ("more", "than"):
        return "<="
    if "before" in before or "under" in before or "below" in before:
        return "<"
    if "after" in before or "over" in before or "above" in before:
        return ">"
    if len(before) >= 2 and before[-2:] in {
        ("longer", "than"), ("more", "than"), ("greater", "than")
    }:
        return ">"
    if len(before) >= 2 and before[-2:] in {
        ("shorter", "than"), ("less", "than"), ("fewer", "than")
    }:
        return "<"
    if after == ("or", "more"):
        return ">="
    if after in {("or", "after"), ("or", "later")}:
        return ">="
    if after in {("or", "before"), ("or", "earlier")}:
        return "<="
    return "="


def _column_requested_as_output(column: ColumnRef, question: str) -> bool:
    normalized = " ".join(_tokens(question))
    words = _semantic_tokens(column.name)
    temporal = bool(set(words) & {"year", "date", "time"})
    if temporal and "production time" in normalized:
        return True
    return temporal and bool(
        re.search(r"\b(?:what|which|show|list|give|find).{0,80}\b(?:year|date|time)\b", normalized)
    )


def _top_level_count(tokens: tuple[str, ...]) -> bool:
    normalized = " ".join(tokens[:8])
    return normalized.startswith("how many") or normalized.startswith("what is the number") \
        or normalized.startswith("what are the number")


def _count_requested(tokens: tuple[str, ...]) -> bool:
    return bool(set(tokens) & {"count", "number"}) or "how many" in " ".join(tokens)


def _explicit_order(tokens: tuple[str, ...]) -> bool:
    return bool(set(tokens) & {"order", "ordered", "sort", "sorted", "top", "bottom"})


def _parse_number(token: str) -> int | float | None:
    if token in _WORD_NUMBERS:
        return _WORD_NUMBERS[token]
    if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return float(token) if "." in token else int(token)
    return None


def _is_id(name: str) -> bool:
    words = _name_tokens(name)
    return bool(words) and words[-1] in {"id", "identifier", "key", "code"}


def _projection_window(tokens: tuple[str, ...]) -> tuple[tuple[int, str], ...]:
    commands = {"find", "give", "list", "return", "show", "what", "which"}
    boundaries = {"has", "have", "having", "shared", "that", "under", "where", "which", "who", "whose", "with"}
    late_commands = [index for index, token in enumerate(tokens) if token in commands and index > 2]
    if late_commands:
        start = late_commands[-1] + 1
        end = next(
            (index for index in range(start + 1, len(tokens)) if tokens[index] in boundaries),
            len(tokens),
        )
        return tuple(enumerate(tokens[start:end], start))
    start = 1 if tokens and tokens[0] in commands else 0
    end = next(
        (index for index in range(max(start + 1, 2), len(tokens)) if tokens[index] in boundaries),
        len(tokens),
    )
    return tuple(enumerate(tokens[start:end], start))


def _column_matches(name: str, tokens: set[str], table: str | None = None) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if compact in tokens:
        return True
    special = {
        "fname": ({"first"}, {"name"}),
        "firstname": ({"first"}, {"name"}),
        "lname": ({"last"}, {"name"}),
        "lastname": ({"last"}, {"name"}),
        "sex": ({"sex", "gender"},),
        "gender": ({"sex", "gender"},),
        "mpg": ({"mpg", "mile"}, {"mpg", "gallon"}),
    }
    groups = special.get(compact)
    if groups is None:
        aliases = {
            "maker": {"maker", "manufacturer"},
            "weight": {"weight", "weigh", "weighed", "weighing"},
            "accelerate": {"accelerate", "acceleration"},
            "hometown": {"hometown", "town", "city"},
            "death": {"death", "killed"},
        }
        table_words = set(_name_tokens(table)) if table is not None else set()
        words = [
            word for word in _name_tokens(name)
            if word not in {"of"} and (word not in table_words or len(_name_tokens(name)) == 1)
        ]
        groups = tuple(aliases.get(word, {word}) for word in words)
    return bool(groups) and all(bool(group & tokens) for group in groups)


def _semantic_tokens(name: str) -> tuple[str, ...]:
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    special = {
        "fname": ("first", "name"), "firstname": ("first", "name"),
        "lname": ("last", "name"), "lastname": ("last", "name"),
        "sex": ("sex", "gender"), "gender": ("sex", "gender"),
    }
    words = list(special.get(compact, _name_tokens(name)))
    synonyms = {
        "maker": ("manufacturer",), "manufacturer": ("maker",),
        "weight": ("weigh", "weighed", "weighing"),
        "accelerate": ("acceleration",), "hometown": ("town", "city"),
        "death": ("killed",), "song": ("songs",), "visit": ("visited",),
    }
    expanded = list(words)
    for word in words:
        expanded.extend(synonyms.get(word, ()))
    return tuple(dict.fromkeys(expanded))


def _value_set(values: Sequence[object]) -> set[object]:
    return {value for value in values if value is not None and str(value).strip()}


def _value_count(values: Sequence[object]) -> int:
    return sum(value is not None and bool(str(value).strip()) for value in values)


def _compatible_types(left: SQLType, right: SQLType) -> bool:
    return left == right or (left.numeric and right.numeric)


def _name_tokens(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(_canon(token) for token in re.findall(r"[A-Za-z0-9]+", spaced))


def _tokens(text: str) -> tuple[str, ...]:
    out = []
    for token in re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", text.lower()):
        if token.endswith("'s"):
            token = token[:-2]
        out.append(_canon(token))
    return tuple(out)


def _canon(word: str) -> str:
    word = word.lower().strip()
    if word == "ids":
        return "id"
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
