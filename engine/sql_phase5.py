"""Phase 5 deterministic arg-extrema, top-N, and set-difference search."""
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
    Query,
    SQLType,
    ScalarSubquery,
    SelectItem,
    SelectQuery,
    SetQuery,
    Star,
    and_predicates,
)
from engine.sql_phase4 import (
    CountThreshold,
    Phase4Expander,
    _and_terms,
    _candidate,
    _column_matches,
    _is_id,
    _linker_noise,
    _parse_number,
    _physical_tables,
    _projection_window,
    _semantic_tokens,
    _tokens,
    _unique_predicates,
)
from engine.sql_search import ScoredQuery


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


class Phase5Expander(Phase4Expander):
    """Add bounded candidates for row and grouped extrema plus set difference."""

    def expand(self, question: str, candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        generated = []
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
        if token_set & {"average", "avg", "mean", "sum", "total"}:
            return []
        targets = self._superlative_targets(tokens)
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

        limit = _requested_limit(tokens, targets[0].cue_position)
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
                    comparison for comparison in self._numeric_comparisons(tokens)
                    if comparison.left.table in physical
                    and comparison.left != target.column
                    and not _is_limit_literal(comparison, tokens, target.cue_position, limit)
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

                for where, where_bonus in where_options:
                    transformed = replace(
                        query,
                        select=projection,
                        where=where,
                        group_by=(),
                        having=None,
                        order_by=(OrderTerm(target.column, target.direction),),
                        limit=limit,
                        distinct=query.distinct or _explicit_distinct(tokens),
                    )
                    built = _candidate(
                        transformed,
                        58.0 + target.score + where_bonus - 0.2 * len(query.joins),
                        candidate.evidence + ("phase5:row-superlative",),
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
        limit = _requested_limit(tokens, cue.position)
        out = []
        for entity_table, entity_score in self._entity_tables(tokens, pseudo)[:3]:
            projections = self._projection_options(question, entity_table, None)
            if not projections:
                continue
            for counted_table, relation_score in self._counted_tables(
                tokens, pseudo, entity_table
            )[:3]:
                if relation_score <= 0:
                    continue
                structures = self._having_structures(
                    candidates, entity_table, counted_table, pseudo
                )
                for source, joins, where, structure_score in structures[:8]:
                    physical = {source, *(join.table for join in joins)}
                    direct_filters = [
                        comparison for comparison in self._numeric_comparisons(tokens)
                        if comparison.left.table in physical
                        and not _is_limit_literal(comparison, tokens, cue.position, limit)
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
                        for selected_where, where_bonus in where_options:
                            query = SelectQuery(
                                select=projection,
                                from_table=source,
                                joins=joins,
                                where=selected_where,
                                group_by=groups,
                                order_by=(OrderTerm(Aggregate("COUNT", Star()), cue.direction),),
                                limit=limit,
                                distinct=False,
                            )
                            built = _candidate(
                                query,
                                60.0 + entity_score + relation_score + structure_score
                                + where_bonus + 1.5 * max(0, len(projection) - 1)
                                - 0.25 * projection_index - 0.2 * len(joins),
                                ("phase5:frequency-superlative",),
                            )
                            if built is not None:
                                out.append(built)
        return out

    def _difference_candidates(
        self, question: str, candidates: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        match = _NEGATIVE_RE.search(question)
        if match is None:
            return []
        tokens = _tokens(question)
        normalized_prefix = _tokens(question[:match.start()])
        negative_position = len(normalized_prefix)
        pseudo = CountThreshold((("=", 1),), negative_position, negative_position + 1, 0.0)
        out = []
        for entity_table, entity_score in self._entity_tables(tokens, pseudo)[:3]:
            projection_options = self._projection_options(question, entity_table, None)
            if not projection_options:
                continue
            for relation_table, relation_score in self._counted_tables(
                tokens, pseudo, entity_table
            )[:3]:
                if relation_table == entity_table or relation_score <= 0:
                    continue
                structures = self._having_structures(
                    candidates, entity_table, relation_table, pseudo
                )
                for source, joins, where, structure_score in structures[:6]:
                    physical = {source, *(join.table for join in joins)}
                    terms = [term for term in _and_terms(where) if not _linker_noise(term)]
                    terms.extend(
                        comparison for comparison in self._numeric_comparisons(tokens)
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
                            ("phase5:set-except",),
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
            explicit = _column_matches(column.name, token_set)
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
            linked.extend(self._projection_columns(tokens, table))
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
    def _frequency_groups(
        entity_table: str, counted_table: str, joins, columns: tuple[ColumnRef, ...]
    ) -> tuple[ColumnRef, ...]:
        if len(columns) == 1:
            return columns
        join_columns = [
            column for join in joins for column in (join.left, join.right)
            if column.table == entity_table
        ]
        keys = [column for column in columns if _is_id(column.name)]
        if keys:
            return (keys[0],)
        if entity_table != counted_table and join_columns:
            return (join_columns[0],)
        return columns


def _frequency_cue(tokens: tuple[str, ...]) -> FrequencyCue | None:
    normalized = " ".join(tokens)
    explicit_patterns = (
        r"\b(most|fewest|greatest|largest|highest|least) number of\b",
        r"\bnumber of .{0,30}\b(most|fewest|greatest|largest|highest|least)\b",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, normalized)
        if match:
            cue = match.group(1)
            position = normalized[:match.start(1)].count(" ")
            return FrequencyCue(position, "ASC" if cue in {"fewest", "least"} else "DESC", True)
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


def _requested_limit(tokens: tuple[str, ...], cue_position: int) -> int:
    for index, token in enumerate(tokens):
        value = _parse_number(token)
        if isinstance(value, int) and 1 <= value <= 100:
            if abs(index - cue_position) <= 2 or (index > 0 and tokens[index - 1] in {"top", "bottom"}):
                return value
    return 1


def _target_extrema_predicate(predicate: Predicate, target: ColumnRef) -> bool:
    return (
        isinstance(predicate, Comparison)
        and predicate.left == target
        and isinstance(predicate.right, ScalarSubquery)
    )


def _is_limit_literal(
    comparison: Comparison, tokens: tuple[str, ...], cue_position: int, limit: int
) -> bool:
    if not isinstance(comparison.right, Literal) or comparison.right.value != limit:
        return False
    positions = [
        index for index, token in enumerate(tokens) if _parse_number(token) == limit
    ]
    return any(abs(position - cue_position) <= 2 for position in positions)


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
