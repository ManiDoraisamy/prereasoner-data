"""Deterministic arg-extrema, top-N, and set-difference expansion."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Sequence

from engine.sql_ast import (
    Aggregate,
    ColumnRef,
    Comparison,
    Literal,
    OrderTerm,
    Predicate,
    SQLType,
    ScalarSubquery,
    SelectItem,
    SelectQuery,
    SetQuery,
    Star,
    and_predicates,
)
from engine.sql_expansion import (
    CountThreshold,
    ExpansionSupport,
    and_terms as _and_terms,
    build_candidate as _candidate,
    column_matches as _column_matches,
    is_id as _is_id,
    join_key as _join_key,
    linker_noise as _linker_noise,
    parse_number as _parse_number,
    physical_tables as _physical_tables,
    projection_window as _projection_window,
    semantic_tokens as _semantic_tokens,
    tokens as _tokens,
    unique_predicates as _unique_predicates,
)
from engine.sql_candidate import ScoredQuery


_MAX_CUES = frozenset({
    "biggest", "commonest", "greatest", "highest", "largest", "latest",
    "longest", "max", "maximum", "most", "oldest", "youngest",
})
_MIN_CUES = frozenset({
    "earliest", "fewest", "least", "lowest", "min", "minimum", "rarest",
    "shortest", "smallest",
})
_NEGATIVE_RE = re.compile(
    r"\b(?:except|without|no|not|never|did not|do not|does not|have not|has not)\b",
    re.I,
)


@dataclass(frozen=True)
class SuperlativeTarget:
    column: ColumnRef
    direction: str
    cue_position: int
    score: float
    explicit_position: int | None


@dataclass(frozen=True)
class FrequencyCue:
    position: int
    direction: str
    explicit_number: bool


class ExtremaQueryExpander(ExpansionSupport):
    """Add bounded candidates for row and grouped extrema plus set difference."""

    def expand(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        generated = []
        generated.extend(self._dual_extrema_candidates(question, candidates))
        generated.extend(self._row_superlative_candidates(question, candidates))
        generated.extend(self._frequency_superlative_candidates(question, candidates))
        generated.extend(self._difference_candidates(question, candidates))

        dedup: dict[str, ScoredQuery] = {}
        for candidate in generated:
            old = dedup.get(candidate.sql)
            if old is None or candidate.score > old.score:
                dedup[candidate.sql] = candidate
        return sorted(dedup.values(), key=lambda candidate: (-candidate.score, candidate.sql))[
            :self.max_candidates
        ]

    def _row_superlative_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        token_set = set(tokens)
        if token_set & {"different", "each", "per"}:
            return []
        if token_set & {"order", "ordered", "sort", "sorted"}:
            return []
        if token_set & {"average", "avg", "mean", "sum", "total"}:
            return []
        if _has_dual_extrema(tokens):
            return []
        if ("how" in token_set or "number" in token_set or "count" in token_set) and bool(
            token_set & {"greater", "larger", "less", "lower", "more", "smaller", "than"}
        ):
            return []
        if token_set & {"rarest", "commonest"} or (
            token_set & {"most", "least"} and "common" in token_set
        ):
            return []
        targets = self._superlative_targets(tokens)
        targets = [
            target for target in targets
            if not self._prefer_scalar_extrema(target, candidates)
        ]
        if not targets:
            return []
        frequency = _frequency_cue(tokens)
        if frequency is not None and frequency.explicit_number:
            relation_words = _frequency_relation_words(tokens, frequency.position)
            if not any(
                target.explicit_position is not None
                and set(_semantic_tokens(target.column.name)) & relation_words
                for target in targets
            ):
                return []

        limit, limit_position = _requested_limit(tokens, targets[0].cue_position)
        out = []
        for target in targets[:3]:
            for candidate in candidates[: self.max_candidates * 3]:
                query = candidate.query
                if not isinstance(query, SelectQuery):
                    continue
                physical = _physical_tables(query)
                if target.column.table not in physical:
                    continue
                projection = self._row_projection(tokens, query, target.column)
                if not projection:
                    continue
                base_terms = [
                    term for term in _and_terms(query.where)
                    if not _linker_noise(term) and not _target_extrema_predicate(term, target.column)
                ]
                direct_filters = [
                    comparison for comparison in self.numeric_comparisons(
                        tokens, frozenset({limit_position}) if limit_position is not None else frozenset()
                    )
                    if comparison.left.table in physical
                    and comparison.left != target.column
                ]
                where_options = [(and_predicates(_unique_predicates(base_terms)), 0.0)]
                if direct_filters:
                    nonnumeric = [
                        term for term in base_terms
                        if not (
                            isinstance(term, Comparison)
                            and isinstance(term.right, Literal)
                            and term.right.type.numeric
                        )
                    ]
                    where_options.append((
                        and_predicates(_unique_predicates(nonnumeric + direct_filters)),
                        1.0,
                    ))
                if base_terms:
                    where_options.append((None, -1.25))

                # Never let a NULL ordering value BE the extreme: SQLite sorts NULLs FIRST under ASC, so
                # "lowest price" over a nullable column would return the NULL row as the minimum. Guard ASC
                # extrema with IS NOT NULL (no-op when there are no NULLs). DESC sorts NULLs last -> safe.
                null_guard = Comparison(target.column, "IS NOT", Literal(None)) if target.direction == "ASC" else None
                for where, where_bonus in where_options:
                    transformed = replace(
                        query,
                        select=projection,
                        where=and_predicates((where, null_guard)),
                        group_by=(),
                        having=None,
                        order_by=(OrderTerm(target.column, target.direction),),
                        limit=limit,
                        distinct=query.distinct or _explicit_distinct(tokens),
                    )
                    built = _candidate(
                        transformed,
                        58.0 + target.score + where_bonus - 0.2 * len(query.joins),
                        candidate.evidence + ("extrema:row-superlative",),
                    )
                    if built is not None:
                        out.append(built)
        out.extend(self._direct_row_superlatives(
            tokens, targets, limit, limit_position, candidates
        ))
        return out

    def _dual_extrema_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        token_set = set(tokens)
        if not _has_dual_extrema(tokens):
            return []
        if token_set & {"average", "avg", "mean", "sum", "total"}:
            return []
        frequency = _frequency_cue(tokens)
        if frequency is not None and frequency.explicit_number:
            return []
        # Explicit MIN/MAX are already handled by the base aggregate planner.
        if token_set & {"minimum", "min", "maximum", "max"}:
            return []

        max_position = min(
            index for index, token in enumerate(tokens)
            if token in _MAX_CUES and _valid_superlative_cue(tokens, index)
        )
        min_position = min(
            index for index, token in enumerate(tokens)
            if token in _MIN_CUES and _valid_superlative_cue(tokens, index)
        )
        linked_targets = self._superlative_targets(tokens)
        if not linked_targets:
            return []
        fallback = linked_targets[0].column
        maximum_target = next(
            (target.column for target in linked_targets if target.direction == "DESC"), fallback
        )
        minimum_target = next(
            (target.column for target in linked_targets if target.direction == "ASC"), fallback
        )
        aggregate_targets = (
            (("MAX", maximum_target), ("MIN", minimum_target))
            if max_position < min_position
            else (("MIN", minimum_target), ("MAX", maximum_target))
        )
        select = tuple(
            SelectItem(Aggregate(function, target))
            for function, target in aggregate_targets
        )
        target_tables = {target.table for _, target in aggregate_targets}
        out = []
        if len(target_tables) == 1:
            source = next(iter(target_tables))
            direct = _candidate(
                SelectQuery(select, source),
                66.0,
                ("extrema:dual-extrema",),
            )
            if direct is not None:
                out.append(direct)
        for candidate in candidates[: self.max_candidates * 2]:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not target_tables <= _physical_tables(query):
                continue
            terms = tuple(
                term for term in _and_terms(query.where)
                if not _linker_noise(term)
                and not any(
                    _target_extrema_predicate(term, target)
                    for _, target in aggregate_targets
                )
            )
            transformed = replace(
                query,
                select=select,
                where=and_predicates(_unique_predicates(terms)),
                group_by=(),
                having=None,
                order_by=(),
                limit=None,
                distinct=False,
            )
            built = _candidate(
                transformed,
                68.0 + min(2.0, 0.25 * len(terms)) - 0.2 * len(query.joins),
                candidate.evidence + ("extrema:dual-extrema",),
            )
            if built is not None:
                out.append(built)
        return out

    def _direct_row_superlatives(
        self, tokens: tuple[str, ...], targets: Sequence[SuperlativeTarget], limit: int,
        limit_position: int | None, candidates: Sequence[ScoredQuery],
    ) -> list[ScoredQuery]:
        linked = []
        for table in self.schema.tables:
            linked.extend(self.projection_columns(tokens, table))
        linked.sort(key=lambda item: (item[2], -item[1], item[0].table, item[0].name))
        projection_columns = tuple(dict.fromkeys(column for column, _, _ in linked[:6]))
        if not projection_columns:
            return []

        categorical_options = []
        for candidate in candidates[:60]:
            query = candidate.query
            if not isinstance(query, SelectQuery):
                continue
            terms = [
                term for term in _and_terms(query.where)
                if isinstance(term, Comparison)
                and isinstance(term.right, Literal)
                and not term.right.type.numeric
                and not _linker_noise(term)
            ]
            if terms:
                categorical_options.append(terms)
        categorical_options.append([])

        out = []
        for target in targets[:3]:
            numeric = [
                    comparison for comparison in self.numeric_comparisons(
                        tokens, frozenset({limit_position}) if limit_position is not None else frozenset()
                    )
                if comparison.left != target.column
            ]
            for categorical in categorical_options[:8]:
                filters = _unique_predicates(categorical + numeric)
                required = {target.column.table}
                required.update(column.table for column in projection_columns)
                required.update(
                    term.left.table for term in filters
                    if isinstance(term, Comparison) and isinstance(term.left, ColumnRef)
                )
                for tree in self.schema.join_trees(required, limit=6):
                    shell = SelectQuery(
                        tuple(SelectItem(column) for column in projection_columns),
                        tree.root,
                        joins=tree.joins,
                    )
                    projection = self._row_projection(tokens, shell, target.column)
                    if not projection:
                        continue
                    query = replace(
                        shell,
                        select=projection,
                        where=and_predicates([*filters, *([Comparison(target.column, "IS NOT", Literal(None))]
                                                           if target.direction == "ASC" else [])]),
                        order_by=(OrderTerm(target.column, target.direction),),   # ASC: exclude NULL ordering values (SQLite sorts NULLs first)
                        limit=limit,
                        distinct=_explicit_distinct(tokens),
                    )
                    built = _candidate(
                        query,
                        64.0 + target.score + 0.75 * len(projection)
                        + min(2.0, float(len(categorical))) - 0.2 * len(tree.joins),
                        ("extrema:row-superlative:direct",),
                    )
                    if built is not None:
                        out.append(built)
        return out

    def _frequency_superlative_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = _tokens(question)
        cue = _frequency_cue(tokens)
        if cue is None:
            return []
        targets = self._superlative_targets(tokens)
        if cue.explicit_number:
            relation_words = _frequency_relation_words(tokens, cue.position)
            if any(
                target.explicit_position is not None
                and set(_semantic_tokens(target.column.name)) & relation_words
                for target in targets
            ):
                return []
        elif targets and targets[0].score >= 6.0:
            return []

        pseudo = CountThreshold((("=", 1),), cue.position, cue.position + 1, 0.0)
        limit, limit_position = _requested_limit(tokens, cue.position)
        out = []
        for entity_table, entity_score in self.entity_tables(tokens, pseudo)[:3]:
            projections = self.projection_options(question, entity_table, None)
            if not projections:
                continue
            for counted_table, relation_score in self.counted_tables(
                tokens, pseudo, entity_table
            )[:3]:
                if relation_score <= 0:
                    continue
                structures = [
                    (*structure, Star())
                    for structure in self.having_structures(
                        candidates, entity_table, counted_table, pseudo
                    )
                ]
                if cue.direction == "ASC" and entity_table != counted_table:
                    zero_inclusive = []
                    for tree in self.schema.join_trees(
                        {entity_table, counted_table}, preferred_root=entity_table, limit=4
                    ):
                        joins = tuple(replace(join, kind="LEFT") for join in tree.joins)
                        count_key = _join_key(joins, counted_table)
                        if count_key is not None:
                            zero_inclusive.append((
                                tree.root, joins, None, 0.75 - 0.2 * len(joins), count_key
                            ))
                    if not zero_inclusive:
                        inferred = self.inferred_entity_join(entity_table, counted_table)
                        if inferred is not None:
                            join = replace(inferred, table=counted_table, kind="LEFT")
                            count_key = _join_key((join,), counted_table)
                            if count_key is not None:
                                zero_inclusive.append((entity_table, (join,), None, 0.5, count_key))
                    if zero_inclusive:
                        structures = zero_inclusive
                for source, joins, where, structure_score, count_operand in structures[:8]:
                    physical = {source, *(join.table for join in joins)}
                    direct_filters = [
                        comparison for comparison in self.numeric_comparisons(
                            tokens,
                            frozenset({limit_position}) if limit_position is not None else frozenset(),
                        )
                        if comparison.left.table in physical
                    ]
                    where_options = [(where, 0.0)]
                    if direct_filters:
                        terms = [
                            term for term in _and_terms(where)
                            if not (
                                isinstance(term, Comparison)
                                and isinstance(term.right, Literal)
                                and term.right.type.numeric
                            )
                        ]
                        where_options.append((
                            and_predicates(_unique_predicates(terms + direct_filters)),
                            1.0,
                        ))
                    for projection_index, projection in enumerate(projections[:3]):
                        columns = tuple(
                            item.expression for item in projection
                            if isinstance(item.expression, ColumnRef)
                        )
                        if not columns:
                            continue
                        groups = self._frequency_groups(
                            entity_table, counted_table, joins, columns
                        )
                        count = Aggregate("COUNT", count_operand)
                        select = projection
                        if _frequency_count_output(tokens):
                            select = projection + (SelectItem(count),)
                        for selected_where, where_bonus in where_options:
                            query = SelectQuery(
                                select=select,
                                from_table=source,
                                joins=joins,
                                where=selected_where,
                                group_by=groups,
                                order_by=(OrderTerm(count, cue.direction),),
                                limit=limit,
                                distinct=False,
                            )
                            built = _candidate(
                                query,
                                60.0 + entity_score + relation_score + structure_score
                                + where_bonus + 1.5 * max(0, len(projection) - 1)
                                - 0.25 * projection_index - 0.2 * len(joins),
                                ("extrema:frequency-superlative",),
                            )
                            if built is not None:
                                out.append(built)
        return out

    def _difference_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        match = _NEGATIVE_RE.search(question)
        if match is None or "but" in _tokens(question):
            return []
        tokens = _tokens(question)
        normalized_prefix = _tokens(question[:match.start()])
        negative_position = len(normalized_prefix)
        pseudo = CountThreshold((("=", 1),), negative_position, negative_position + 1, 0.0)
        out = []
        for entity_table, entity_score in self.entity_tables(tokens, pseudo)[:3]:
            projection_options = self.projection_options(question, entity_table, None)
            if not projection_options:
                continue
            for relation_table, relation_score in self.counted_tables(
                tokens, pseudo, entity_table
            )[:3]:
                if relation_table == entity_table or relation_score <= 0:
                    continue
                structures = self.having_structures(
                    candidates, entity_table, relation_table, pseudo
                )
                for source, joins, where, structure_score in structures[:6]:
                    physical = {source, *(join.table for join in joins)}
                    terms = [term for term in _and_terms(where) if not _linker_noise(term)]
                    terms.extend(
                        comparison for comparison in self.numeric_comparisons(tokens)
                        if comparison.left.table in physical
                    )
                    for projection in projection_options[:3]:
                        left_terms, right_terms = _split_difference_terms(
                            _unique_predicates(terms), tokens, negative_position
                        )
                        left = SelectQuery(
                            projection,
                            entity_table,
                            where=and_predicates(left_terms),
                            distinct=_explicit_distinct(tokens),
                        )
                        right = SelectQuery(
                            projection,
                            source,
                            joins=joins,
                            where=and_predicates(right_terms),
                            distinct=False,
                        )
                        built = _candidate(
                            SetQuery(left, "EXCEPT", right),
                            51.0 + entity_score + relation_score + structure_score,
                            ("extrema:set-except",),
                        )
                        if built is not None:
                            out.append(built)
        return out

    def _superlative_targets(self, tokens: tuple[str, ...]) -> list[SuperlativeTarget]:
        cue_positions = [
            (index, token) for index, token in enumerate(tokens)
            if (token in _MAX_CUES or token in _MIN_CUES)
            and _valid_superlative_cue(tokens, index)
        ]
        if not cue_positions:
            return []
        token_set = set(tokens)
        out = []
        foreign_key_columns = {
            column
            for foreign_key in self.schema.foreign_keys
            for column in (foreign_key.from_column, foreign_key.to_column)
        }
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if _is_id(column.name) or column in foreign_key_columns:
                continue
            if not (
                column.type.numeric
                or column.type == SQLType.DATE
                or _observed_numeric(schema_column.values)
            ):
                continue
            semantic = set(_semantic_tokens(column.name))
            mention_positions = [
                index for index, token in enumerate(tokens) if token in semantic
            ]
            compact = re.sub(r"[^a-z0-9]", "", column.name.lower())
            mention_positions.extend(
                index for index, token in enumerate(tokens) if token == compact
            )
            measure_words = semantic - {"num", "number", "of"}
            explicit = _column_matches(column.name, token_set) or bool(
                measure_words and measure_words <= token_set
            )
            inferred_age = bool(
                semantic & {"age", "birth", "birthday", "date"}
                and any(cue in {"youngest", "oldest"} for _, cue in cue_positions)
            )
            inferred_mpg = compact == "mpg" and bool(token_set & {"fuel", "gas", "gasoline"})
            if not explicit and not inferred_age and not inferred_mpg:
                continue
            best = None
            for cue_position, cue in cue_positions:
                position = min(
                    mention_positions,
                    key=lambda value: abs(value - cue_position),
                ) if mention_positions else None
                distance = abs(position - cue_position) if position is not None else 2
                score = 6.0 + min(2.0, len(semantic & token_set)) - 0.2 * distance
                if inferred_age and cue in {"youngest", "oldest"}:
                    score += 2.5
                if inferred_mpg:
                    score += 2.0
                direction = _superlative_direction(cue, column)
                option = SuperlativeTarget(column, direction, cue_position, score, position)
                if best is None or option.score > best.score:
                    best = option
            if best is not None:
                out.append(best)
        return sorted(
            out,
            key=lambda item: (-item.score, item.cue_position, item.column.table, item.column.name),
        )

    def _row_projection(
        self, tokens: tuple[str, ...], query: SelectQuery, target: ColumnRef
    ) -> tuple[SelectItem, ...]:
        linked = []
        for table in _physical_tables(query):
            linked.extend(self.projection_columns(tokens, table))
        linked.sort(key=lambda item: (item[2], -item[1], item[0].table, item[0].name))
        filtered = []
        for column, score, position in linked:
            words = set(_semantic_tokens(column.name))
            shadowed = any(
                words < set(_semantic_tokens(other.name)) and abs(position - other_position) <= 1
                for other, _, other_position in linked
                if other != column
            )
            if not shadowed:
                filtered.append((column, score, position))
        of_positions = [index for index, token in enumerate(tokens) if token == "of"]
        if of_positions and any(position < of_positions[-1] for _, _, position in filtered):
            filtered = [
                item for item in filtered
                if item[2] < of_positions[-1] or item[0] == target
            ]
        columns = [column for column, _, _ in filtered]
        if not columns:
            columns = [
                item.expression for item in query.select
                if isinstance(item.expression, ColumnRef)
            ]
        if target not in columns and _target_requested_in_projection(target, tokens):
            columns.append(target)
        return tuple(SelectItem(column) for column in dict.fromkeys(columns[:4]))

    @staticmethod
    def _prefer_scalar_extrema(
        target: SuperlativeTarget, candidates: Sequence[ScoredQuery]
    ) -> bool:
        temporal = bool(set(_semantic_tokens(target.column.name)) & {"date", "time", "year"})
        if not temporal:
            return False
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery):
                continue
            selected = {
                item.expression for item in query.select
                if isinstance(item.expression, ColumnRef)
            }
            if target.column not in selected:
                continue
            if any(_target_extrema_predicate(term, target.column) for term in _and_terms(query.where)):
                return True
        return False

    def _frequency_groups(
        self, entity_table: str, counted_table: str, joins, columns: tuple[ColumnRef, ...]
    ) -> tuple[ColumnRef, ...]:
        entity_key = (
            _join_key(joins, entity_table)
            if entity_table != counted_table and self._projection_has_duplicates(columns)
            else None
        )
        return tuple(dict.fromkeys((*columns, entity_key) if entity_key is not None else columns))

    def _projection_has_duplicates(self, columns: tuple[ColumnRef, ...]) -> bool:
        schema_columns = [
            self.schema.column_map.get((column.table, column.name))
            for column in columns
        ]
        if not schema_columns or any(column is None or not column.values for column in schema_columns):
            return True
        row_count = min(len(column.values) for column in schema_columns if column is not None)
        rows = [
            tuple(repr(column.values[index]) for column in schema_columns if column is not None)
            for index in range(row_count)
        ]
        return len(rows) != len(set(rows))


def _has_dual_extrema(tokens: tuple[str, ...]) -> bool:
    has_maximum = any(
        token in _MAX_CUES and _valid_superlative_cue(tokens, index)
        for index, token in enumerate(tokens)
    )
    has_minimum = any(
        token in _MIN_CUES and _valid_superlative_cue(tokens, index)
        for index, token in enumerate(tokens)
    )
    return has_maximum and has_minimum


def _frequency_cue(tokens: tuple[str, ...]) -> FrequencyCue | None:
    normalized = " ".join(tokens)
    explicit_patterns = (
        r"\b(most|fewest|greatest|largest|highest|least|smallest|lowest) number of\b",
        r"\bnumber of .{0,30}\b(most|fewest|greatest|largest|highest|least|smallest|lowest)\b",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, normalized)
        if match:
            cue = match.group(1)
            position = normalized[:match.start(1)].count(" ")
            return FrequencyCue(
                position,
                "ASC" if cue in {"fewest", "least", "smallest", "lowest"} else "DESC",
                True,
            )
    for index, token in enumerate(tokens):
        if token in {"commonest", "rarest"}:
            return FrequencyCue(index, "ASC" if token == "rarest" else "DESC", False)
        if token in {"most", "least"} and index + 1 < len(tokens) and tokens[index + 1] in {
            "common", "frequent", "frequently", "time",
        }:
            return FrequencyCue(index, "ASC" if token == "least" else "DESC", False)
        if token in {"most", "fewest"}:
            return FrequencyCue(index, "ASC" if token == "fewest" else "DESC", False)
    return None


def _valid_superlative_cue(tokens: tuple[str, ...], index: int) -> bool:
    token = tokens[index]
    before = tokens[max(0, index - 2):index]
    if token in {"least", "most"} and before and before[-1] == "at":
        return False
    if "than" in before:
        return False
    return True


def _frequency_relation_words(tokens: tuple[str, ...], cue_position: int) -> set[str]:
    try:
        number_position = tokens.index("number", max(0, cue_position - 5))
        of_position = tokens.index("of", number_position + 1)
    except ValueError:
        return set(tokens[max(0, cue_position - 3):cue_position + 4])
    out = []
    for token in tokens[of_position + 1:of_position + 4]:
        if token in {"after", "before", "for", "in", "is", "that", "where", "which", "with", "whose"}:
            break
        out.append(token)
    return set(out)


def _frequency_count_output(tokens: tuple[str, ...]) -> bool:
    normalized = " ".join(tokens)
    return bool(
        re.search(r"\band (?:the )?(?:number|count|how many)\b", normalized)
        or re.search(r"\b(?:number|count) (?:of )?.{0,25}\bit has\b", normalized)
        or re.search(r"\bhow many .{0,30}\b(?:use|uses|have|has)\b", normalized)
    )


def _observed_numeric(values: Sequence[object]) -> bool:
    observed = [value for value in values if value is not None and str(value).strip()]
    if not observed:
        return False
    numeric = 0
    for value in observed:
        try:
            float(str(value).replace(",", ""))
            numeric += 1
        except ValueError:
            pass
    return numeric / len(observed) >= 0.8


def _superlative_direction(cue: str, column: ColumnRef) -> str:
    semantic = set(_semantic_tokens(column.name))
    birth_like = "birth" in semantic or ({"date", "year"} & semantic and "age" not in semantic)
    if cue == "youngest":
        return "DESC" if birth_like else "ASC"
    if cue == "oldest":
        return "ASC" if birth_like else "DESC"
    return "ASC" if cue in _MIN_CUES else "DESC"


def _requested_limit(tokens: tuple[str, ...], cue_position: int) -> tuple[int, int | None]:
    for index, token in enumerate(tokens):
        value = _parse_number(token)
        if isinstance(value, int) and 1 <= value <= 100:
            if abs(index - cue_position) <= 2 or (index > 0 and tokens[index - 1] in {"top", "bottom"}):
                return value, index
    return 1, None


def _target_extrema_predicate(predicate: Predicate, target: ColumnRef) -> bool:
    return (
        isinstance(predicate, Comparison)
        and predicate.left == target
        and isinstance(predicate.right, ScalarSubquery)
    )


def _target_requested_in_projection(target: ColumnRef, tokens: tuple[str, ...]) -> bool:
    projection_tokens = {token for _, token in _projection_window(tokens)}
    return _column_matches(target.name, projection_tokens)


def _explicit_distinct(tokens: tuple[str, ...]) -> bool:
    return bool(set(tokens) & {"distinct", "distinctive", "different", "unique"})


def _split_difference_terms(
    terms: Sequence[Predicate], tokens: tuple[str, ...], negative_position: int
) -> tuple[list[Predicate], list[Predicate]]:
    left = []
    right = []
    for term in terms:
        if not isinstance(term, Comparison) or not isinstance(term.right, Literal):
            right.append(term)
            continue
        value_tokens = _tokens(str(term.right.value))
        positions = [
            index for index in range(len(tokens) - len(value_tokens) + 1)
            if tokens[index:index + len(value_tokens)] == value_tokens
        ]
        if positions and min(positions) < negative_position:
            left.append(term)
        else:
            right.append(term)
    return left, right
