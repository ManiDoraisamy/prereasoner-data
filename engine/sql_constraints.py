"""Aggregate constraints, disjunctions, and relational-subquery expansion.

This is an independent deterministic capability layer. Rules operate on typed
ASTs and schema objects, never SQL strings.
"""
from __future__ import annotations

from dataclasses import replace
import re
from typing import Sequence

from engine.sql_ast import (
    Aggregate,
    BooleanExpr,
    ColumnRef,
    Comparison,
    InPredicate,
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
)
from engine.sql_expansion import (
    CountThreshold,
    ExpansionSupport,
    and_terms as _and_terms,
    build_candidate as _candidate,
    column_matches as _column_matches,
    column_requested_as_output as _column_requested_as_output,
    count_requested as _count_requested,
    entity_join_key as _entity_join_key,
    explicit_order as _explicit_order,
    expression_table as _expression_table,
    is_id as _is_id,
    linker_noise as _linker_noise,
    name_tokens as _name_tokens,
    parse_number as _parse_number,
    physical_tables as _physical_tables,
    projection_window as _projection_window,
    semantic_tokens as _semantic_tokens,
    tokens as _tokens,
    unique_predicates as _unique_predicates,
)
from engine.sql_candidate import ScoredQuery


_COUNT_RELATION_CUES = frozenset({
    "contain", "contains", "conduct", "conducted", "design", "designed", "enroll", "enrolled",
    "has", "have", "having", "make", "operate", "perform", "performed", "produce", "shared",
    "teach", "used", "visit", "visited", "with",
})


class ConstraintQueryExpander(ExpansionSupport):
    """Add bounded candidates for Spider's remaining high-mass structures."""

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
            if self.threshold_targets_column(tokens, threshold):
                continue
            entity_options = self.entity_tables(tokens, threshold)
            for entity_table, entity_score in entity_options[:2]:
                counted_options = self.counted_tables(tokens, threshold, entity_table)
                for counted_table, relation_score in counted_options[:2]:
                    if relation_score <= 0:
                        continue
                    structures = self.having_structures(
                        candidates, entity_table, counted_table, threshold
                    )
                    for source, joins, where, structure_score in structures[:8]:
                        regular_numeric = [
                            comparison for comparison in self.numeric_comparisons(tokens)
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
                        projection_options = self.projection_options(
                            question, entity_table, where
                        )
                        if not projection_options:
                            continue
                        group_options = self.group_options(
                            question, entity_table, counted_table, joins, projection_options[0]
                        )
                        select_options = self.having_select_options(
                            question, projection_options
                        )
                        count = Aggregate("COUNT", Star())
                        having_terms = tuple(
                            Comparison(count, operator, Literal(value, SQLType.INTEGER))
                            for operator, value in threshold.predicates
                        )
                        having = and_predicates(having_terms)
                        for selected_where, where_bonus in where_options:
                            filtered_projection = self.projection_options(
                                question, entity_table, selected_where
                            )
                            selected_options = self.having_select_options(
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
                    comparison for comparison in self.numeric_comparisons(question_tokens)
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
        numeric = self.numeric_comparisons(tokens)
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
                    for column, _, _ in self.projection_columns(_tokens(question), table)
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
                                for column, score, position in self.projection_columns(
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
                        comparison for comparison in self.numeric_comparisons(_tokens(question))
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
            all_mentions = self.mentioned_columns(tokens, numeric=False)
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
            text_mentions = self.mentioned_columns(tokens, numeric=False)
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
            entity_table = self.entity_tables(_tokens(question), CountThreshold((("=", 1),), 0, 0, 0))[0][0]
            key = _entity_join_key(query, entity_table)
            if key is None:
                continue
            entity_select = self.projection_options(question, entity_table, None)[0]
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
                for column, score, position in self.projection_columns(tokens, table)
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
        numeric = self.numeric_comparisons(tokens)
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
                linked.extend(self.projection_columns(tokens, table))
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
