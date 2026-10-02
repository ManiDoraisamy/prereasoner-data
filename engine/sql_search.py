"""Deterministic search over typed SQL ASTs.

The searcher does not decode SQL tokens.  It links question spans to typed schema
objects, expands a bounded beam of semantic choices, searches the FK graph for
join trees (including bridge tables), validates complete ASTs, and only then
renders SQL. Ordered capability expanders add recursive queries, constraints,
disjunctions, relational subqueries, extrema, top-N, and set difference before
the inspectable ranker runs. An optional frozen model can rerank the completed
pool without changing its grammar.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Sequence

from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    BooleanExpr,
    ColumnRef,
    Comparison,
    DatePart,
    ExistsPredicate,
    InPredicate,
    Literal,
    Lower,
    OrderTerm,
    ScalarSubquery,
    SelectItem,
    SelectQuery,
    SQLType,
    Star,
    and_predicates,
    contradictory,
    render_query,
    share_of,
    validate_query,
)
from engine.numeric import parse_decimal
from engine.sql_candidate import ScoredQuery
from engine.sql_dates import served_date_phrases
from engine.sql_expansion import (
    implicit_sum_measures,
    measure_words_after,
    money_total_position,
    ordering_requested,
    share_cue,
    share_requested,
    words,
)
from engine.sql_profile_expansion import ProfileSearchConfig
from engine.sql_schema import SchemaGraph, is_surrogate_key

_NUMBER_RE = re.compile(r"^-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
_PROJECTION_CUES = frozenset({"show", "list", "display", "select", "give", "find", "which", "what"})
# Grammar words a question writes in lower case: "in", "and" or "are" is never Code2 'IN', Code 'AND' or
# Code 'ARE' (Spider world_1, 2026-10-02), while a question that names such a value writes it in capitals
# ("the division AS", every Spider train question of that kind).
_FUNCTION_WORDS = frozenset({
    "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or",
    "than", "that", "the", "this", "to", "was", "were", "with",
})
# Words that place a row first or last in time ("the first student to register", "the most recent order").
_EARLY_WORDS = frozenset({"first", "earliest"})
_LATE_WORDS = frozenset({"last", "latest", "recent", "newest"})
# "first name", "first, middle, and last name", "first and second line": a word of this run before
# "name" or "line" names a part of a name or an address, not a place in an ordering.
_NAME_PART_WORDS = frozenset({"first", "middle", "last", "second", "third", "and", "or", "full", "given", "family"})
_ID_WORDS = frozenset({"id", "identifier", "code", "key"})
# Words that ask for the values holding a text (canon() forms). "contain" and "include" also name a whole
# value: "the documents that contain the paragraph text 'Brazil'" and "the paragraph that includes the text
# 'Korea'" are those texts, so a whole value of the data stays an equality after them.
_SUBSTRING_CUES = frozenset({"substring", "letter"})
_INCLUDE_CUES = frozenset({"contain", "containing", "include", "including"})
# Comparatives and the operator each makes with the number after "than"; the measure some describe.
_COMPARATIVES = {
    "greater": ">", "higher": ">", "bigger": ">", "larger": ">", "older": ">", "heavier": ">", "taller": ">",
    "longer": ">", "later": ">", "lower": "<", "smaller": "<", "younger": "<", "lighter": "<", "shorter": "<",
    "cheaper": "<", "earlier": "<",
}
_COMPARATIVE_COLUMNS = {
    "older": frozenset({"age"}), "younger": frozenset({"age"}), "heavier": frozenset({"weight"}),
    "lighter": frozenset({"weight"}), "taller": frozenset({"height"}), "shorter": frozenset({"height", "length"}),
    "longer": frozenset({"length", "duration"}), "cheaper": frozenset({"price", "cost"}),
}
_CATEGORICAL_INITIALS = {
    "left": "l", "right": "r",
    "male": "m", "female": "f",
    "yes": "y", "no": "n",
    "true": "t", "false": "f",
    "unknown": "u",
}


@dataclass(frozen=True)
class _ColumnOption:
    column: ColumnRef
    score: float
    position: int


@dataclass(frozen=True)
class _Mention:
    position: int
    options: tuple[_ColumnOption, ...]


@dataclass(frozen=True)
class _Draft:
    projections: tuple[ColumnRef, ...] = ()
    aggregates: tuple[Aggregate, ...] = ()
    predicates: tuple[Comparison, ...] = ()
    score: float = 0.0
    evidence: tuple[str, ...] = ()


class SQLSearcher:
    def __init__(self, schema: SchemaGraph, beam_size: int = 64, max_candidates: int = 25):
        if not schema.tables:
            raise ValueError("SQL search requires at least one table")
        self.schema = schema
        self.beam_size = max(1, beam_size)
        self.max_candidates = max(1, max_candidates)

    @classmethod
    def from_tables(cls, tables: Sequence[dict], fks: Sequence[dict | tuple], **kwargs) -> "SQLSearcher":
        return cls(SchemaGraph.from_tables(tables, fks), **kwargs)

    def search(self, question: str, semantic_signals=None, rank_candidates: bool = True,
               expand_recursive: bool = True, expand_constraints: bool = True,
               expand_extrema: bool = True, expand_parsimony: bool = True,
               profile_max_candidates: int = 32,
               profile_per_profile: int = 4,
               profile_generation_penalty: float = 5.0,
               profile_binding_quality_weight: float = 2.0,
               profile_preserve_baseline_top: bool = True,
               profile_config: ProfileSearchConfig | None = None) -> list[ScoredQuery]:
        if profile_config is not None:
            profile_max_candidates = profile_config.max_candidates
            profile_per_profile = profile_config.per_profile
            profile_generation_penalty = profile_config.generation_penalty
            profile_binding_quality_weight = profile_config.binding_quality_weight
            profile_preserve_baseline_top = profile_config.preserve_baseline_top
        tokens = _tokens(question)
        if not tokens:
            return []
        table_scores = self._table_scores(tokens)
        mentions = self._column_mentions(tokens, table_scores)
        mentions = self._suppress_fk_attribute_qualifiers(tokens, mentions)
        clause_boundary = next((i for i, token in enumerate(tokens)
                                if token in {"where", "with", "whose", "having", "from", "for",
                                             "order", "ordered", "sort", "sorted", "rank", "ranked"}),
                               len(tokens))
        prefix_tokens = set(tokens[:clause_boundary])
        id_requested = bool(_ID_WORDS & set(tokens))
        explicit_projection_columns = {
            schema_column.ref
            for schema_column in self.schema.columns
            if (
                (link_words := _column_link_words(schema_column.ref, id_requested))
                and set(link_words) <= prefix_tokens
                and _column_link_positions(
                    schema_column.ref, tokens[:clause_boundary], self.schema, link_words
                )
            )
        }
        clause_only_columns = {
            option.column for mention in mentions for option in mention.options
            if option.column not in explicit_projection_columns
        }

        projection_choices = self._projection_choices(tokens, mentions, table_scores)
        # A table joins every reading only when the question names it with its words together ("car
        # makers"); Spider car_1, 2026-10-02: "how many car makers are there in each continent? List the
        # continent name" named car_names by "car" and "name" far apart, and every reading joined it,
        # multiplying the counted rows. A table named so still scores, for the root and its display.
        # A column word that names another table names that table: "TV Channel" is TV_Channel, not
        # TV_series' first word before its Channel column.
        table_words = {table: {_canon(word) for word in _name_words(table)} for table in self.schema.tables}
        named_tables = {table for table in self.schema.tables
                        if _names_together(tokens, [_canon(word) for word in _name_words(table)],
                                           self._column_forms(table).difference(
                                               *(words for other, words in table_words.items() if other != table)))}
        aggregate_choices = self._aggregate_choices(tokens, mentions)
        predicate_choices = self._predicate_choices(tokens, mentions, question)

        drafts = [_Draft()]
        drafts = self._expand(drafts, projection_choices, "projections")
        drafts = self._expand(drafts, aggregate_choices, "aggregates")
        drafts = self._expand(drafts, predicate_choices, "predicates")

        complete: list[ScoredQuery] = []
        for draft in drafts:
            groups = self._group_choices(tokens, mentions, table_scores, draft)
            for group_columns, group_score, group_evidence in groups:
                aggregated_columns = set().union(
                    *(_operand_columns(a.operand) for a in draft.aggregates)
                ) if draft.aggregates else set()
                raw_projection = tuple(c for c in draft.projections if c not in aggregated_columns)
                if not draft.aggregates and len(raw_projection) > 1:
                    predicate_columns = {predicate.left for predicate in draft.predicates
                                         if isinstance(predicate.left, ColumnRef)}
                    raw_projection = tuple(c for c in raw_projection
                                           if c in explicit_projection_columns
                                           or (c not in predicate_columns and c not in clause_only_columns))
                if draft.aggregates:
                    # Entity mentions can contribute an implicit display projection. Aggregate queries retain
                    # only columns the question names explicitly; _group_choices owns each/per/by grouping.
                    # An empty group choice is the scalar interpretation, so it must not inherit a display
                    # projection merely because the same words also identify a filter column.
                    raw_projection = tuple(
                        c for c in raw_projection
                        if c in explicit_projection_columns and group_columns
                    )
                    grouped = _unique_columns(group_columns + raw_projection)
                    expressions = tuple(SelectItem(c) for c in grouped) + tuple(
                        SelectItem(a) for a in draft.aggregates
                    )
                else:
                    grouped = ()
                    expressions = tuple(SelectItem(c) for c in raw_projection) or (SelectItem(Star()),)

                orders = self._order_choices(tokens, mentions, draft, question)
                for order_terms, limit, order_score, order_evidence in orders:
                    required = self._required_tables(expressions, draft.predicates, grouped, order_terms)
                    mentioned_tables = {table for table, score in table_scores.items()
                                        if score >= 2.5 and table in named_tables}
                    required.update(mentioned_tables)
                    if not required:
                        required.add(max(table_scores, key=table_scores.get) if table_scores else self.schema.tables[0])
                    root = self._preferred_root(required, table_scores, draft)
                    trees = self.schema.join_trees(required, root)
                    anchors = self._required_tables(tuple(SelectItem(a) for a in draft.aggregates),
                                                    draft.predicates, grouped, order_terms)
                    reachable = self._reachable(anchors or {root}) if not trees and len(required) > 1 else set()
                    stray = {item.expression for item in expressions if isinstance(item.expression, ColumnRef)
                             and item.expression.table not in reachable} if reachable else set()
                    if stray and not (mentioned_tables | anchors) - reachable and len(stray) < len(expressions):
                        # A projected column no foreign key reaches from the tables the filters, aggregates,
                        # groups and order read drops out: "the cost of each treatment and the corresponding
                        # treatment type description" also read "type" as the unjoined Charges.charge_type,
                        # and the reading vanished (Spider dog_kennels, 2026-10-02). A table the question
                        # names that cannot be reached leaves no reading: "how many flights does an airline
                        # have" without a key joining flights to airlines must not count airlines.
                        expressions = tuple(item for item in expressions if item.expression not in stray)
                        grouped = tuple(column for column in grouped if column not in stray)
                        required = self._required_tables(expressions, draft.predicates, grouped, order_terms)
                        required.update(mentioned_tables)
                        root = self._preferred_root(required, table_scores, draft)
                        trees = self.schema.join_trees(required, root)
                    for tree in trees:
                        query = SelectQuery(
                            select=expressions,
                            from_table=tree.root,
                            joins=tree.joins,
                            where=and_predicates(draft.predicates),
                            group_by=grouped,
                            order_by=order_terms,
                            limit=limit,
                            distinct=bool({"distinct", "different", "unique"} & set(tokens)) and not draft.aggregates,
                        )
                        try:
                            validate_query(query)
                            sql = render_query(query)
                        except (TypeError, ValueError):
                            continue
                        join_score = -0.2 * len(tree.joins) + 0.1 * tree.confidence
                        evidence = (draft.evidence + group_evidence + order_evidence
                                    + tuple(f"join:{self.schema.foreign_keys[i].signature}" for i in tree.edge_indexes))
                        complete.append(ScoredQuery(query, sql,
                                                    draft.score + group_score + order_score + join_score,
                                                    evidence))

        column_words = {_canon(word) for column in self.schema.columns for word in _name_words(column.ref.name)}
        if share_requested(tokens, column_words):
            complete.extend(self._share_candidates(complete))
        dedup: dict[str, ScoredQuery] = {}
        for candidate in complete:
            old = dedup.get(candidate.sql)
            if old is None or candidate.score > old.score:
                dedup[candidate.sql] = candidate
        pool_size = max(self.beam_size, self.max_candidates * 4)
        base = sorted(dedup.values(), key=lambda c: (-c.score, c.sql))[:pool_size]
        pool = base
        if expand_recursive or expand_constraints or expand_extrema or expand_parsimony:
            from engine.sql_constraints import ConstraintQueryExpander
            from engine.sql_extrema import ExtremaQueryExpander
            from engine.sql_parsimony import ParsimonyQueryExpander
            from engine.sql_recursive import RecursiveQueryExpander

            expansion_pipeline = (
                (expand_recursive, RecursiveQueryExpander),
                (expand_constraints, ConstraintQueryExpander),
                (expand_extrema, ExtremaQueryExpander),
                (expand_parsimony, ParsimonyQueryExpander),
            )
            for enabled, expander_type in expansion_pipeline:
                if not enabled:
                    continue
                generated = expander_type(self.schema, pool_size).expand(question, pool)
                pool = _merge_candidates(pool, generated)
        from engine.calculations.search import CalculationQueryExpander
        pool = _merge_candidates(
            pool,
            CalculationQueryExpander(self.schema, pool_size, semantic_signals).expand(question, pool),
        )
        profile_baseline = tuple(pool)
        profile_requested = bool(
            profile_config is not None
            and semantic_signals is not None
            and semantic_signals.sketch_profiles
        )
        if profile_requested:
            from engine.sql_profile_expansion import ProfileQueryExpander

            generated = ProfileQueryExpander(
                self.schema,
                semantic_signals,
                min(pool_size, max(1, profile_max_candidates)),
                max(1, profile_per_profile),
                max(0.0, profile_generation_penalty),
                max(0.0, profile_binding_quality_weight),
            ).expand(question, pool)
            pool = _merge_candidates(pool, generated)
        # The expansions read a conjunction of one column's values as either value or both
        # (engine/sql_constraints.py, engine/sql_recursive.py); the conjunction itself matches no row.
        pool = [candidate for candidate in pool if not contradictory(candidate.query)]
        # The tables the question names: their words together, and scored as a mention.
        named_here = {table for table in named_tables if table_scores.get(table, 0.0) >= 2.5}
        pool = _merge_candidates([], [self._simplified(candidate, named_here) for candidate in pool])
        if not rank_candidates:
            return pool[:self.max_candidates]
        from engine.sql_rank import CandidateRanker
        ranked = CandidateRanker(self.schema, semantic_signals).rank(
            question, pool
        )[:self.max_candidates]
        if profile_requested and profile_preserve_baseline_top and profile_baseline:
            fallback = CandidateRanker(self.schema).rank(question, profile_baseline)[0]
            fallback = replace(
                fallback,
                evidence=fallback.evidence + ("profile:fallback-top",),
            )
            ranked = [fallback] + [candidate for candidate in ranked if candidate.sql != fallback.sql]
            ranked = ranked[:self.max_candidates]
        return ranked

    def _expand(self, drafts: list[_Draft], choices: list[tuple[tuple, float, tuple[str, ...]]],
                field: str) -> list[_Draft]:
        expanded = []
        for draft in drafts:
            for value, score, evidence in choices:
                expanded.append(replace(draft, **{field: value}, score=draft.score + score,
                                        evidence=draft.evidence + evidence))
        return sorted(expanded, key=lambda d: (-d.score, repr(d)))[:self.beam_size]

    def _simplified(self, candidate: ScoredQuery, named_tables: set[str]) -> ScoredQuery:
        """``candidate`` without what its answer never reads: a second column its joins equate with one it
        projects (they carry one value), and a joined table no clause reads that the question does not
        name. Spider DEV, 2026-10-02: "the id and name of the museum with the most staff"
        projected museum.Museum_ID and visit.Museum_ID, and an unread join multiplies the rows a count
        counts. Every expansion's candidates pass through it; queries with subqueries or aliases stay."""
        query = candidate.query
        if (not isinstance(query, SelectQuery) or not query.joins or not isinstance(query.from_table, str)
                or query.from_alias or any(join.alias for join in query.joins) or _has_subquery(query)):
            return candidate
        # Columns the query's own joins equate carry one value in every row it reads: Cartoon.Channel,
        # TV_Channel.id and TV_series.Channel when both join TV_Channel.
        classes: dict[ColumnRef, ColumnRef] = {}

        def find(column: ColumnRef) -> ColumnRef:
            while classes.get(column, column) != column:
                column = classes[column]
            return column

        for join in query.joins:
            for left, right in join.predicates:
                classes[find(left)] = find(right)
        anchored = set().union(
            *(_clause_tables(item.expression) for item in query.select if not isinstance(item.expression, ColumnRef)),
            _clause_tables(query.where), _clause_tables(query.having),
            *(_clause_tables(term.expression) for term in query.order_by))
        kept: list[ColumnRef] = []
        for item in query.select:
            column = item.expression
            if not isinstance(column, ColumnRef):
                continue
            twin = next((other for other in kept if find(other) == find(column)), None)
            if twin is None:
                kept.append(column)
            elif column.table in anchored and twin.table not in anchored:
                kept[kept.index(twin)] = column
        echoes = {item.expression for item in query.select
                  if isinstance(item.expression, ColumnRef) and item.expression not in kept}
        # In a listing, a projected key that only repeats, through the joins, a table the query reads
        # for something else names that table: "the content of TV Channel with serial name Sky Radio"
        # projected TV_series.Channel and joined TV_series for it (Spider tvshow, 2026-10-02). A grouped
        # or aggregated query keeps its columns: they set what it groups.
        equated = [column for join in query.joins for pair in join.predicates for column in pair]
        listing = not query.group_by and not any(isinstance(item.expression, Aggregate) for item in query.select)
        for column in kept if listing else ():
            if column in echoes:
                continue
            others = set().union(
                *(_clause_tables(item.expression) for item in query.select
                  if item.expression != column and item.expression not in echoes),
                (other.table for other in query.group_by if other != column),
                _clause_tables(query.where), _clause_tables(query.having),
                *(_clause_tables(term.expression) for term in query.order_by))
            # The question's own rows keep their columns: "the currency symbol for every order" lists each
            # order's currency beside its symbol (tests.test_enrichment serving benchmark, 2026-10-02).
            if column.table == query.from_table or column.table in named_tables:
                continue
            if column.table not in others and any(
                    other.table in others and other.table != column.table and find(other) == find(column)
                    for other in equated):
                echoes.add(column)
        select = tuple(item for item in query.select if item.expression not in echoes)
        group_by = tuple(column for column in query.group_by if column not in echoes)
        joins = list(query.joins)
        root = query.from_table
        read = set().union(*(_clause_tables(item.expression) for item in select), *(column.table for column in group_by),
                           _clause_tables(query.where), _clause_tables(query.having),
                           *(_clause_tables(term.expression) for term in query.order_by))
        changed = bool(echoes)
        # COUNT(*) counts the rows the joins make: "the model with the most versions" counts car_names.
        counts_rows = any(_counts_rows(node) for node in (*(item.expression for item in query.select),
                                                          query.having, *(term.expression for term in query.order_by)))
        while not counts_rows:
            edges = [{left.table for left, _ in join.predicates} | {right.table for _, right in join.predicates}
                     for join in joins]
            leaf = next((index for index, join in enumerate(joins)
                         if join.table not in read and join.table not in named_tables
                         and not any(join.table in edge for other, edge in enumerate(edges) if other != index)), None)
            if leaf is not None:
                del joins[leaf]
                changed = True
                continue
            touching = [index for index, edge in enumerate(edges) if root in edge]
            if root not in read and root not in named_tables and len(touching) == 1:
                root = joins[touching[0]].table
                del joins[touching[0]]
                changed = True
                continue
            break
        if not changed:
            return candidate
        simplified = replace(query, select=select, group_by=group_by, from_table=root, joins=tuple(joins))
        try:
            validate_query(simplified)
            sql = render_query(simplified)
        except (TypeError, ValueError):
            return candidate
        return ScoredQuery(simplified, sql, candidate.score, candidate.evidence + ("simplified",))

    def _reachable(self, tables: set[str]) -> set[str]:
        """The tables the foreign keys connect to ``tables``, those included."""
        neighbours: dict[str, set[str]] = {}
        for fk in self.schema.foreign_keys:
            neighbours.setdefault(fk.from_column.table, set()).add(fk.to_column.table)
            neighbours.setdefault(fk.to_column.table, set()).add(fk.from_column.table)
        reached, frontier = set(tables), list(tables)
        while frontier:
            for table in neighbours.get(frontier.pop(), ()):
                if table not in reached:
                    reached.add(table)
                    frontier.append(table)
        return reached

    def _column_forms(self, table: str) -> frozenset[str]:
        """The words a question may name a column of ``table`` by: its whole name run together ("makeid")
        and its own words."""
        forms = set()
        for column in self.schema.columns:
            if column.ref.table == table:
                words = [_canon(word) for word in _name_words(column.ref.name)]
                forms.add("".join(words))
                forms.update(words)
        return frozenset(forms)

    def _table_scores(self, tokens: tuple[str, ...]) -> dict[str, float]:
        scores = {}
        token_set = set(tokens)
        for table in self.schema.tables:
            words = _name_words(table)
            coverage = sum(1 for word in words if _canon(word) in token_set)
            plural = any(_canon(token) == _canon(word) for token in tokens for word in words)
            scores[table] = (3.0 if coverage == len(words) and words else 0.0) + (1.0 if plural else 0.0)
        return scores

    def _column_mentions(self, tokens: tuple[str, ...], table_scores: dict[str, float]) -> tuple[_Mention, ...]:
        grouped: dict[int, list[_ColumnOption]] = {}
        token_set = set(tokens)
        id_requested = bool(_ID_WORDS & token_set)
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if is_surrogate_key(column.name) and not id_requested:
                continue
            meaningful = _column_link_words(column, id_requested)
            positions = _column_link_positions(column, tokens, self.schema, meaningful)
            if not positions:
                continue
            coverage = len({tokens[i] for i in positions} & set(meaningful)) / max(len(set(meaningful)), 1)
            if coverage < 1.0 and len(meaningful) > 1:
                continue
            position = max(positions)
            phrase = " ".join(meaningful)
            exact = phrase in " ".join(tokens)
            score = 3.0 + coverage + (1.0 if exact else 0.0) + 0.15 * table_scores.get(column.table, 0.0)
            grouped.setdefault(position, []).append(_ColumnOption(column, score, position))
        mentions = []
        for position, options in sorted(grouped.items()):
            options.sort(key=lambda option: (-option.score, option.column.table, option.column.name))
            mentions.append(_Mention(position, tuple(options[:4])))
        return tuple(mentions)

    def _projection_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                            table_scores: dict[str, float]) -> list[tuple[tuple, float, tuple[str, ...]]]:
        if mentions:
            beam: list[tuple[tuple[ColumnRef, ...], float, tuple[str, ...]]] = [((), 0.0, ())]
            for mention in mentions:
                expanded = []
                for columns, score, evidence in beam:
                    for option in mention.options:
                        chosen = _unique_columns(columns + (option.column,))
                        expanded.append((chosen, score + option.score,
                                         evidence + (f"column:{option.column.table}.{option.column.name}",)))
                beam = sorted(expanded, key=lambda item: (-item[1], repr(item[0])))[:self.beam_size]
            return [(columns, score, evidence) for columns, score, evidence in beam]

        if _PROJECTION_CUES & set(tokens):
            table_options = sorted(table_scores.items(), key=lambda item: (-item[1], item[0]))
            out = []
            for table, score in table_options[:3]:
                displays = self.schema.display_columns(table)
                if displays:
                    out.append(((displays[0],), max(score, 0.5), (f"entity-display:{table}.{displays[0].name}",)))
            if out:
                return out
        return [((), 0.0, ())]

    def _suppress_fk_attribute_qualifiers(
            self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...]) -> tuple[_Mention, ...]:
        """Treat adjacent ``country name``-style FK phrases as one target attribute.

        A source column named ``country`` is a qualifier when its trusted edge points to the
        table owning the immediately following ``name``/``title``/``label`` mention. Explicit
        lists such as ``country code and country name`` are non-adjacent and remain unchanged.
        """
        suppressed = set()
        display_names = {"name", "title", "label", "description"}
        for left, right in zip(mentions, mentions[1:]):
            if right.position != left.position + 1 or right.position >= len(tokens):
                continue
            target_tables = {
                option.column.table for option in right.options
                if set(_name_words(option.column.name)) & display_names
            }
            if not target_tables:
                continue
            qualifier = tokens[left.position]
            if not any(
                qualifier in {_canon(word) for word in _name_words(target)}
                for target in target_tables
            ):
                continue
            if all(any(
                any(child == option.column for child, _ in fk.column_pairs)
                and fk.to_column.table in target_tables
                for fk in self.schema.foreign_keys
            ) for option in left.options):
                suppressed.add(left.position)
        return tuple(mention for mention in mentions if mention.position not in suppressed)

    def _aggregate_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...]) -> list[tuple[tuple, float, tuple[str, ...]]]:
        cues: list[tuple[str, int]] = []
        for i, token in enumerate(tokens):
            number_is_column_label = (
                token == "number"
                and any(
                    mention.position == i - 1
                    or (
                        mention.position == i
                        and i > 0
                        and any(
                            "number" in _column_link_words(option.column, True)
                            and tokens[i - 1] in {
                                *(_canon(word) for word in _name_words(option.column.table)),
                                *(_canon(word) for word in _name_words(option.column.name)
                                  if word.lower() != "no"),
                            }
                            for option in mention.options
                        )
                    )
                    for mention in mentions
                )
            )
            if token in {"count", "counts"} or (
                token == "number"
                and i + 1 < len(tokens)
                and tokens[i + 1] == "of"
                and not number_is_column_label
            ):
                cues.append(("COUNT", i))
            elif token in {"sum", "total"}:
                if token == "total" and i + 1 < len(tokens) and tokens[i + 1] in {"number", "count"}:
                    continue
                cues.append(("SUM", i))
            elif token in {"average", "avg", "mean"}:
                cues.append(("AVG", i))
            elif token in {"minimum", "min"}:
                cues.append(("MIN", i))
            elif token in {"maximum", "max"}:
                cues.append(("MAX", i))
        for i in range(len(tokens) - 1):
            if tokens[i:i + 2] == ("how", "many"):
                cues.append(("COUNT", i))
        cues = list(dict.fromkeys(cues))
        # "count the number" and "average mean" are reinforcing paraphrases, not requests for duplicate
        # The base search supports several different aggregates in one query; repeated functions
        # collapse to their earliest cue until argument-scope parsing becomes more precise.
        seen_functions = set()
        unique_cues = []
        for function, position in sorted(cues, key=lambda item: item[1]):
            if function not in seen_functions:
                unique_cues.append((function, position))
                seen_functions.add(function)
        cues = unique_cues
        implicit_measures: dict[int, frozenset[str]] = {}
        if not cues:
            # "top 3 products by units sold" and "customers by revenue" imply SUM over
            # a measure column even without an aggregate word. The cue fires only when
            # the schema has a matching measure column. A bare money noun that appears
            # in a mentioned column's own vocabulary ("revenue" column, "sale price")
            # stays a column mention with its raw interpretation, and one that names a
            # real table stays the entity when the question counts or lists it ("how
            # many sales", "list the sales"). Otherwise the table's name is its money
            # total ("what's the sales in London"). A participle always asserts the sum.
            table_words = {
                _canon(word) for table in self.schema.tables for word in _name_words(table)
            }
            mention_words = {
                _canon(word)
                for mention in mentions for option in mention.options
                for word in _name_words(option.column.name)
            }
            money_total = money_total_position(tokens, table_words)
            for measure in implicit_sum_measures(tokens):
                word = tokens[measure.position]
                if not measure.participle and (
                    word in mention_words
                    or (word in table_words and measure.position != money_total)
                ):
                    continue
                if self._measure_targets(mentions, measure.position, measure.column_words):
                    # One SUM cue per question, matching the explicit-cue dedup above.
                    cues.append(("SUM", measure.position))
                    implicit_measures[measure.position] = measure.column_words
                    break
        if not cues and (share := share_cue(tokens, self.schema)) is not None:
            cues.append(share)
        if not cues:
            return [((), 0.0, ())]

        beam: list[tuple[tuple[Aggregate, ...], float, tuple[str, ...]]] = [((), 0.0, ())]
        for function, position in sorted(cues, key=lambda item: item[1]):
            options: list[tuple[Aggregate, float, str]] = []
            if function == "COUNT":
                options.append((Aggregate("COUNT", Star()), 4.0, "aggregate:COUNT(*)"))
                for option in self._target_columns(mentions, position, numeric=False)[:3]:
                    distinct = bool({"different", "distinct", "unique"} & set(tokens))
                    options.append((Aggregate("COUNT", option.column, distinct), 3.3 + option.score * 0.1,
                                    f"aggregate:COUNT({option.column.table}.{option.column.name})"))
                for column in self._counted_entity_identities(tokens, position):
                    options.append((Aggregate("COUNT", column, distinct=True), 3.8,
                                    f"aggregate:COUNT(DISTINCT {column.table}.{column.name})"))
                for column in self._distinct_counted_columns(tokens, position):
                    options.append((Aggregate("COUNT", column, distinct=True), 3.3,
                                    f"aggregate:COUNT(DISTINCT {column.table}.{column.name}):noun"))
            else:
                implicit_words = implicit_measures.get(position)
                if implicit_words is not None:
                    # An implicit measure stays below every explicit cue (4.0 base) so
                    # explicit phrasing always owns the plan when both are present.
                    targets = self._measure_targets(mentions, position, implicit_words)
                    base_score, cue_note = 3.6, ":implicit-measure"
                else:
                    base_score, cue_note = 4.0, ""
                    targets = self._target_columns(mentions, position, numeric=function in {"SUM", "AVG"})
                    preferred_words = (
                        measure_words_after(tokens, position)
                        if function in {"SUM", "AVG"} else None
                    )
                    if not targets:
                        targets = [
                            _ColumnOption(c.ref, 0.0, len(tokens)) for c in self.schema.columns
                            if c.ref.type.numeric and not is_surrogate_key(c.ref.name)
                        ]
                        if preferred_words:
                            # "total spend" without a column mention must not resolve by
                            # candidate-order luck: keep the columns that carry measure
                            # vocabulary when any do.
                            matching = [
                                option for option in targets
                                if {_canon(word) for word in _name_words(option.column.name)}
                                & preferred_words
                            ]
                            targets = matching or targets
                        targets = targets[:4]
                for option in targets[:4]:
                    if function in {"SUM", "AVG"} and not option.column.type.numeric:
                        continue
                    options.append((Aggregate(function, option.column), base_score + option.score * 0.1,
                                    f"aggregate:{function}({option.column.table}.{option.column.name}){cue_note}"))
            expanded = []
            for aggregates, score, evidence in beam:
                for aggregate, option_score, reason in options:
                    if aggregate in aggregates:
                        continue
                    expanded.append((aggregates + (aggregate,), score + option_score, evidence + (reason,)))
            beam = sorted(expanded, key=lambda item: (-item[1], repr(item[0])))[:self.beam_size]
        return [(aggregates, score, evidence) for aggregates, score, evidence in beam]

    def _distinct_counted_columns(self, tokens: tuple[str, ...], position: int) -> list[ColumnRef]:
        """The columns a counted noun after "distinct", "different" or "unique" names by a word of their
        name: "how many distinct countries do players come from" counts players.country_code once each
        (Spider wta_1, 2026-10-02: no such column was a mention, and the count counted rows)."""
        distinct_at = next((index for index in range(position, min(len(tokens), position + 4))
                            if tokens[index] in {"different", "distinct", "unique"}), None)
        if distinct_at is None or distinct_at + 1 >= len(tokens):
            return []
        noun = tokens[distinct_at + 1]
        named = [column.ref for column in self.schema.columns
                 if noun in {_canon(word) for word in _name_words(column.ref.name)}]
        return sorted(named, key=lambda column: (column.table, column.name))[:4]

    def _counted_entity_identities(
        self, tokens: tuple[str, ...], position: int
    ) -> list[ColumnRef]:
        """Identity columns for a repeated entity count such as ``winners who participated``."""
        relative = next(
            (index for index in range(position + 1, len(tokens))
             if tokens[index] in {"who", "that", "which"}),
            None,
        )
        if relative is None:
            return []
        subject = set(tokens[position + 1:relative]) - {"a", "an", "the", "of"}
        if not subject:
            return []

        matches = []
        for schema_column in self.schema.columns:
            column = schema_column.ref
            words = tuple(_canon(word) for word in _name_words(column.name))
            if not words or words[-1] not in {"id", "identifier", "key", "name"}:
                continue
            role = set(words[:-1])
            table_role = {_canon(word) for word in _name_words(column.table)}
            if (role and role & subject) or (not role and table_role & subject):
                matches.append(column)
        return sorted(matches, key=lambda column: (
            0 if _name_words(column.name)[-1].lower() == "name" else 1,
            column.table,
            column.name,
        ))[:4]

    def _target_columns(self, mentions: tuple[_Mention, ...], position: int, numeric: bool) -> list[_ColumnOption]:
        options = [option for mention in mentions for option in mention.options
                   if not numeric or option.column.type.numeric]
        options.sort(key=lambda option: (
            0 if option.position >= position else 1,
            abs(option.position - position),
            -option.score,
            option.column.table,
            option.column.name,
        ))
        return options

    def _measure_targets(self, mentions: tuple[_Mention, ...], position: int,
                         column_words: frozenset[str]) -> list[_ColumnOption]:
        """Numeric columns whose name carries the measure vocabulary, mentions first.

        An implicit measure never falls back to arbitrary numeric columns: when the
        schema has no quantity/amount-named column the cue simply does not fire and
        the question keeps its current interpretation.
        """
        def carries(column: ColumnRef) -> bool:
            return bool({_canon(word) for word in _name_words(column.name)} & column_words)

        preferred = [option for option in self._target_columns(mentions, position, numeric=True)
                     if carries(option.column)]
        if preferred:
            return preferred
        return [
            _ColumnOption(c.ref, 0.0, position) for c in self.schema.columns
            if c.ref.type.numeric and not is_surrogate_key(c.ref.name) and carries(c.ref)
        ]

    def _predicate_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                           question: str = "") -> list[tuple[tuple, float, tuple[str, ...]]]:
        groups: list[list[tuple[tuple[Comparison, ...], float, str]]] = []
        # A calendar phrase claims its tokens: "after August 10, 2026" is one date, not the number 10
        # and the year 2026, and "since 2026-08-06" is not the cell value 2026-08-06. Without a date column
        # to compare, "in May" stays a value of a text month column.
        phrases = served_date_phrases(question, tokens, self.schema)
        claimed = {index for phrase in phrases for index in range(phrase.start, phrase.end)}
        capitalized = frozenset(word.lower() for word in re.findall(r"\b[A-Z]{2,}\b", question))
        substrings, held = self._substring_groups(tokens, mentions, question, claimed)
        claimed = claimed | held
        groups.extend(substrings)
        groups.extend(self._value_predicate_groups(tokens, mentions, claimed, capitalized))
        groups.extend(self._date_phrase_groups(phrases, mentions))
        groups.extend(self._numeric_predicate_groups(tokens, mentions, claimed))
        if not groups:
            return [((), 0.0, ())]
        beam: list[tuple[tuple[Comparison, ...], float, tuple[str, ...]]] = [((), 0.0, ())]
        for group in groups:
            expanded = []
            for predicates, score, evidence in beam:
                for additions, option_score, reason in group:
                    expanded.append((predicates + additions, score + option_score, evidence + (reason,)))
            beam = sorted(expanded, key=lambda item: (-item[1], repr(item[0])))[:self.beam_size]
        return [(predicates, score, evidence) for predicates, score, evidence in beam]

    def _substring_groups(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...], question: str,
                          claimed: set[int]) -> tuple[list[list[tuple[tuple[Comparison, ...], float, str]]], set[int]]:
        """A text that a value contains: "the contestants whose names contain the substring 'Al'", "a song
        having 'Hey' in its name", "the documents that contain the letter w in their description" compare
        LOWER(column) LIKE '%al%' (Spider DEV, 2026-10-02: no reading had a substring filter). The text is
        quoted, or the word after "substring", "letter" or "word"; a whole value of the data stays an
        equality. The column is the text column the question names nearest the text, or one whose values
        hold it."""
        named = re.search(r"\b(?:substring|letter|word)\s+(?:the\s+)?['\"]?([\w-]+)", question, re.I)
        # "substring", "letter" or "the word X" ask for a text inside values even where the text is also a
        # whole value ("the substring 'Al'" beside a state code 'AL'); "contain", "include" and "'Hey' in its
        # name" only where it is not.
        explicit = bool(set(tokens) & _SUBSTRING_CUES) or named is not None
        if (not explicit and not set(tokens) & _INCLUDE_CUES
                and not re.search(r"\bin (?:its|their|the)\b", question, re.I)):
            return [], set()
        texts = [text.strip() for text in re.findall(r"(?<![\w])['\"]([^'\"]+)['\"](?![\w])", question)]
        if named and not texts:
            texts = [named.group(1)]
        groups, held = [], set()
        for text in texts:
            wanted = _tokens(text)
            if not wanted or (not explicit and self.schema.value_index.get(" ".join(wanted))):
                continue
            start = next((index for index in range(len(tokens) - len(wanted) + 1)
                          if tokens[index:index + len(wanted)] == wanted and index not in claimed), None)
            if start is None:
                continue
            folded = text.lower()
            holders = [column.ref for column in self.schema.columns
                       if column.ref.type == SQLType.TEXT
                       and any(folded in str(value).lower() for value in column.values if value is not None)]
            near = sorted((option for mention in mentions for option in mention.options
                           if option.column.type == SQLType.TEXT),
                          key=lambda option: (abs(option.position - start), -option.score,
                                              option.column.table, option.column.name))
            # A column whose values hold the text is the one compared, the nearest named first; without
            # one, the nearest named text column (the text may be missing from the data).
            targets = _unique_columns(tuple(option.column for option in near if option.column in holders)
                                      + tuple(holders)) or _unique_columns(tuple(option.column for option in near))
            options = [((Comparison(Lower(column), "LIKE", Literal(f"%{folded}%", SQLType.TEXT)),), 5.0,
                        f"substring:{column.table}.{column.name}")
                       for column in targets[:4]]
            if options:
                groups.append(options)
                held.update(range(start, start + len(wanted)))
        return groups, held

    def _value_predicate_groups(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                                claimed: set[int] = frozenset(),
                                capitalized: frozenset[str] = frozenset()) -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        matches: list[tuple[int, int, str, tuple[tuple[ColumnRef, Any], ...]]] = []
        for start in range(len(tokens)):
            for size in range(1, min(6, len(tokens) - start) + 1):
                if size == 1 and tokens[start] in _FUNCTION_WORDS and tokens[start] not in capitalized:
                    continue                    # "in" alone is no Code2 'IN'; "Welcome to NY" stays one value
                phrase = " ".join(tokens[start:start + size])
                options = self.schema.value_index.get(phrase)
                if options:
                    matches.append((start, start + size, phrase, options))
        matches.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
        occupied: set[int] = set(claimed)
        selected = []
        for match in matches:
            span = set(range(match[0], match[1]))
            if span & occupied:
                continue
            occupied.update(span)
            selected.append(match)
        selected.sort()
        groups = []
        for start, _, phrase, options in selected:
            operator = "!=" if set(tokens[max(0, start - 3):start]) & {"not", "except", "excluding", "without"} else "="
            choices = []
            for column, value in sorted(options, key=lambda item: (item[0].table, item[0].name))[:4]:
                literal = Literal(value, column.type)
                choices.append(((Comparison(column, operator, literal),), 5.0,
                                f"value:{column.table}.{column.name}{operator}{phrase}"))
            groups.append(choices)
        groups.extend(self._categorical_initial_groups(tokens, mentions, occupied))
        return groups

    def _categorical_initial_groups(
        self,
        tokens: tuple[str, ...],
        mentions: tuple[_Mention, ...],
        occupied: set[int],
    ) -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        """Resolve qualified one-letter categories such as ``left hand`` -> ``hand = 'L'``.

        The initial is considered only immediately before a word belonging to a recognized column
        mention. This keeps the rule structural: arbitrary words cannot become filters merely because
        a table happens to contain a one-letter category.
        """
        values_by_column: dict[ColumnRef, dict[str, Any]] = {}
        for values in self.schema.value_index.values():
            for column, value in values:
                text = str(value).strip()
                if len(text) == 1 and text.isalpha():
                    values_by_column.setdefault(column, {})[text.lower()] = value

        qualified: dict[tuple[int, str], list[tuple[int, ColumnRef, Any]]] = {}
        for mention in mentions:
            for option in mention.options:
                column = option.column
                values = values_by_column.get(column)
                if not values:
                    continue
                link_words = set(_column_link_words(column, id_requested=True))
                link_positions = [
                    index for index, token in enumerate(tokens)
                    if token in link_words
                ]
                for position in link_positions:
                    descriptor = position - 1
                    if descriptor < 0 or descriptor in occupied:
                        continue
                    token = tokens[descriptor]
                    value = values.get(_CATEGORICAL_INITIALS.get(token, ""))
                    if value is not None:
                        qualified.setdefault((descriptor, token), []).append(
                            (len(link_words), column, value)
                        )

        groups = []
        for (_, token), options in sorted(qualified.items()):
            specificity = max(item[0] for item in options)
            alternatives = []
            seen: set[tuple[ColumnRef, Any]] = set()
            for option_specificity, column, value in sorted(
                options, key=lambda item: (-item[0], item[1].table, item[1].name, repr(item[2]))
            ):
                key = (column, value)
                if option_specificity != specificity or key in seen:
                    continue
                seen.add(key)
                alternatives.append(
                    ((Comparison(column, "=", Literal(value, column.type)),), 4.75,
                     f"category-initial:{column.table}.{column.name}={token}")
                )
            if alternatives:
                groups.append(alternatives)
        return groups

    @staticmethod
    def _share_candidates(candidates: list[ScoredQuery]) -> list[ScoredQuery]:
        """Each one-aggregate candidate read as a share of its whole (sql_ast.share_of).

        "what share of the total amount comes from Paris" was planned as the Paris total and
        "share of total amount by city" as each city's total, and the coverage gate declined both over
        the dropped word (2026-10-02). The share reading ranks above the total it divides.
        """
        out = []
        for candidate in candidates:
            query = candidate.query
            if not isinstance(query, SelectQuery):
                continue
            aggregates = [index for index, item in enumerate(query.select) if isinstance(item.expression, Aggregate)]
            if not aggregates and query.where is not None and not query.group_by:
                # "what percentage of orders are from Lyon" counts the rows it lists.
                query = replace(query, select=(SelectItem(Aggregate("COUNT", Star())),), order_by=(),
                                limit=None, distinct=False)
                aggregates = [0]
            if len(aggregates) != 1 or query.having is not None or query.distinct or query.limit is not None:
                continue
            aggregate = query.select[aggregates[0]].expression
            if aggregate.function not in {"SUM", "COUNT"} or aggregate.distinct:
                continue
            select = list(query.select)
            select[aggregates[0]] = SelectItem(share_of(aggregate, query), "share")
            shared = SelectQuery(tuple(select), query.from_table, query.joins, query.where, query.group_by,
                                 from_alias=query.from_alias)
            try:
                validate_query(shared)
                sql = render_query(shared)
            except (TypeError, ValueError):
                continue
            out.append(ScoredQuery(shared, sql, candidate.score + 1.0, candidate.evidence + ("share:of-whole",)))
        return out

    def _date_phrase_groups(self, phrases, mentions: tuple[_Mention, ...]) -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        """One group per calendar phrase: its comparisons on each date column it may name."""
        groups = []
        for phrase in phrases:
            targets = [column for column in self._date_targets(mentions, phrase.start)
                       if column.type == SQLType.DATE]
            label = "-".join(str(part) for part in (phrase.year, phrase.month, phrase.day) if part is not None)
            if phrase.until is not None:
                label += ".." + "-".join(str(part) for part in phrase.until if part is not None)
            options = [(phrase.comparisons(target), 5.0,
                        f"date:{target.table}.{target.name}:{phrase.cue or 'in'}:{label}")
                       for target in targets[:4] if phrase.comparisons(target)]
            if options:
                groups.append(options)
        return groups

    def _numeric_predicate_groups(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                                  claimed: set[int] = frozenset()) -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        groups = []
        used_numbers: set[int] = set(claimed)
        for i, token in enumerate(tokens):
            if token != "between" or i in claimed:              # "between July 1 and July 10, 2026" is dates
                continue
            found = [(j, _number(tokens[j])) for j in range(i + 1, min(len(tokens), i + 7)) if _NUMBER_RE.match(tokens[j])]
            if len(found) < 2:
                continue
            targets = self._numeric_targets(mentions, i)
            options = []
            for target in targets[:4]:
                lo_i, lo = found[0]; hi_i, hi = found[1]
                used_numbers.update((lo_i, hi_i))
                options.append(((Comparison(target, ">=", Literal(lo, target.type)),
                                 Comparison(target, "<=", Literal(hi, target.type))), 5.0,
                                f"between:{target.table}.{target.name}"))
            if options:
                groups.append(options)

        patterns = [
            (("at", "least"), ">="), (("at", "most"), "<="), (("more", "than"), ">"),
            (("greater", "than"), ">"), (("less", "than"), "<"), (("fewer", "than"), "<"),
            (("over",), ">"), (("above",), ">"), (("under",), "<"), (("below",), "<"),
            (("after",), ">"), (("before",), "<"), (("since",), ">="),
            (("equal", "to"), "="), (("equals",), "="), (("exactly",), "="),
        ]
        for cue, operator in patterns:
            size = len(cue)
            for i in range(len(tokens) - size + 1):
                if tokens[i:i + size] != cue or i in claimed:
                    continue
                number_index = next((j for j in range(i + size, min(len(tokens), i + size + 5))
                                     if j not in used_numbers and _NUMBER_RE.match(tokens[j])), None)
                if number_index is None:
                    continue
                value = _number(tokens[number_index])
                date_targets = (
                    self._date_targets(mentions, i)
                    if cue in {("after",), ("before",), ("since",)}
                    and isinstance(value, int) and 1000 <= value <= 9999
                    else []
                )
                if date_targets:
                    options = []
                    for target in date_targets[:4]:
                        if target.type == SQLType.DATE:
                            date_operator, boundary = _date_year_boundary(cue[0], value)
                            literal = Literal(boundary, SQLType.DATE)
                        else:
                            # A year column holds the year itself: "countries that became independent
                            # after 1950" is IndepYear > 1950. A date literal there validated nowhere, and
                            # every reading of the question was dropped (Spider DEV, 2026-10-02: 15 such).
                            date_operator, literal = operator, Literal(value, target.type)
                        options.append(((Comparison(target, date_operator, literal),), 5.0,
                                        f"date:{target.table}.{target.name}{date_operator}{literal.value}"))
                else:
                    targets = self._numeric_targets(mentions, i)
                    options = [((Comparison(target, operator, Literal(value, target.type)),), 4.5,
                                f"comparison:{target.table}.{target.name}{operator}{value}")
                               for target in targets[:4]]
                if options:
                    groups.append(options)
                    used_numbers.add(number_index)

        # A comparative before "than" and a number compares the column it describes: "pets heavier than
        # 10", "every pet who is older than 1", "a greater weight than 10" (Spider pets_1, 2026-10-02: no
        # cue matched, and the filter was dropped).
        for i, token in enumerate(tokens):
            operator = _COMPARATIVES.get(token)
            if operator is None or i in claimed:
                continue
            than = next((j for j in range(i + 1, min(len(tokens), i + 4)) if tokens[j] == "than"), None)
            if than is None:
                continue
            number_index = next((j for j in range(than + 1, min(len(tokens), than + 3))
                                 if j not in used_numbers and _NUMBER_RE.match(tokens[j])), None)
            if number_index is None:
                continue
            value = _number(tokens[number_index])
            # The measure the word implies comes first ("older": an age column, mentioned or not); no
            # comparative compares an identifier.
            described = _COMPARATIVE_COLUMNS.get(token, frozenset())
            implied = tuple(column.ref for column in self.schema.columns
                            if column.ref.type.numeric and not is_surrogate_key(column.ref.name)
                            and described & {_canon(word) for word in _name_words(column.ref.name)})
            targets = [column for column in _unique_columns(implied + tuple(self._numeric_targets(mentions, i)))
                       if not is_surrogate_key(column.name)]
            options = [((Comparison(target, operator, Literal(value, target.type)),), 4.5,
                        f"comparison:{target.table}.{target.name}{operator}{value}")
                       for target in targets[:4]]
            if options:
                groups.append(options)
                used_numbers.add(number_index)

        for i, token in enumerate(tokens):
            if i in used_numbers or not re.fullmatch(r"(?:19|20)\d{2}", token):
                continue
            year_columns = [c.ref for c in self.schema.columns
                            if c.ref.type == SQLType.DATE or "year" in _name_words(c.ref.name)]
            if not year_columns:
                continue
            options = []
            for column in year_columns[:4]:
                if column.type == SQLType.DATE:
                    start, end = f"{token}-01-01", f"{int(token) + 1}-01-01"
                    options.append(((Comparison(column, ">=", Literal(start, SQLType.DATE)),
                                     Comparison(column, "<", Literal(end, SQLType.DATE))), 4.0,
                                    f"year:{column.table}.{column.name}={token}"))
                else:
                    options.append(((Comparison(column, "=", Literal(int(token), column.type)),), 4.0,
                                    f"year:{column.table}.{column.name}={token}"))
            if options:
                groups.append(options)
        return groups

    def _numeric_targets(self, mentions: tuple[_Mention, ...], position: int) -> list[ColumnRef]:
        options = [option for mention in mentions for option in mention.options if option.column.type.numeric]
        options.sort(key=lambda option: (abs(option.position - position), -option.score,
                                         option.column.table, option.column.name))
        refs = _unique_columns(tuple(option.column for option in options))
        if refs:
            return list(refs)
        return [c.ref for c in self.schema.columns if c.ref.type.numeric and not is_surrogate_key(c.ref.name)]

    def _date_targets(self, mentions: tuple[_Mention, ...], position: int) -> list[ColumnRef]:
        options = [
            option for mention in mentions for option in mention.options
            if option.column.type == SQLType.DATE or "year" in _name_words(option.column.name)
        ]
        options.sort(key=lambda option: (
            abs(option.position - position), -option.score,
            option.column.table, option.column.name,
        ))
        refs = _unique_columns(tuple(option.column for option in options))
        if refs:
            return list(refs)
        return [
            column.ref for column in self.schema.columns
            if column.ref.type == SQLType.DATE or "year" in _name_words(column.ref.name)
        ]

    def _group_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                       table_scores: dict[str, float], draft: _Draft) -> list[tuple[tuple[ColumnRef, ...], float, tuple[str, ...]]]:
        if not draft.aggregates:
            return [((), 0.0, ())]
        targets = set().union(*(_operand_columns(a.operand) for a in draft.aggregates))
        projection_groups = _unique_columns(tuple(c for c in draft.projections if c not in targets))
        explicit_positions = []
        for i, token in enumerate(tokens):
            if token in {"each", "per"} or (token == "by" and i > 0):
                explicit_positions.append(i)
        options: list[tuple[tuple[ColumnRef, ...], float, tuple[str, ...]]] = []
        for position in explicit_positions:
            nearby = sorted(
                (option for mention in mentions for option in mention.options
                 if option.column not in targets),
                key=lambda option: (0 if option.position >= position else 1,
                                    abs(option.position - position), -option.score,
                                    option.column.table, option.column.name),
            )
            for option in nearby[:3]:
                groups = _unique_columns(projection_groups + (option.column,))
                options.append((groups, 3.0 + option.score * 0.1,
                                (f"group:{option.column.table}.{option.column.name}",)))
            if tokens[position] in {"each", "per"} or not projection_groups:
                table_positions = {
                    table: min((i for i, token in enumerate(tokens)
                                if token in {_canon(w) for w in _name_words(table)}), default=len(tokens) + 5)
                    for table in self.schema.tables
                }
                for table, score in sorted(table_scores.items(), key=lambda item: (-item[1], item[0])):
                    if score <= 0:
                        continue
                    displays = self.schema.display_columns(table)
                    if displays:
                        groups = _unique_columns(projection_groups + (displays[0],))
                        distance = abs(table_positions[table] - position)
                        position_bonus = 1.5 if table_positions[table] >= position else 0.0
                        options.append((groups, 2.8 + score * 0.1 + position_bonus - 0.05 * distance,
                                        (f"group-entity:{table}.{displays[0].name}",)))
        if projection_groups:
            options.append((projection_groups, 2.5, tuple(f"group:{c.table}.{c.name}" for c in projection_groups)))
            if not explicit_positions and (
                any(a.function == "COUNT" for a in draft.aggregates)
                or not set(tokens) & {"distinct", "different", "unique"}
            ):
                # A mentioned dimension can name a recipient/filter, not an output
                # group ("total amount paid to suppliers"). Keep a scalar candidate
                # for aggregates; role-aware ranking decides between them. Retain
                # implicit grouping in "maximum age for different countries".
                options.append(((), 0.0, ("group:none-aggregate",)))
        if not options:
            options.append(((), 0.0, ()))
        dedup = {}
        for choice in options:
            old = dedup.get(choice[0])
            if old is None or choice[1] > old[1]:
                dedup[choice[0]] = choice
        return sorted(dedup.values(), key=lambda item: (-item[1], repr(item[0])))[:8]

    def _order_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                       draft: _Draft, question: str) -> list[tuple[tuple[OrderTerm, ...], int | None, float, tuple[str, ...]]]:
        token_set = set(tokens)
        direction = None
        if token_set & {"descending", "desc", "highest", "largest", "biggest", "most", "latest", "newest", "top"}:
            direction = "DESC"
        elif token_set & {"ascending", "asc", "lowest", "smallest", "least", "earliest", "oldest", "bottom"}:
            direction = "ASC"
        # "the first student to register" and "when was the last transcript released" order by time; the
        # "first" of "first name" places nothing (probe, 2026-10-02: "what are the first names of all
        # students" answered one row, ordered by age).
        temporal = next(((("ASC" if token in _EARLY_WORDS else "DESC"), i) for i, token in enumerate(tokens)
                         if token in _EARLY_WORDS | _LATE_WORDS and not _names_a_part(tokens, i)), None)

        limit = None
        for i, token in enumerate(tokens):
            if token in {"top", "bottom", "first", "last"} and not _names_a_part(tokens, i):
                limit = next((int(_number(tokens[j])) for j in range(i + 1, min(len(tokens), i + 4))
                              if _NUMBER_RE.match(tokens[j]) and int(_number(tokens[j])) > 0), None)
                limit = limit or 1
                break
        if limit is None and token_set & {"most", "least", "highest", "lowest", "largest", "smallest"}:
            limit = 1

        order_cue = (direction is not None or limit is not None or temporal is not None
                     or ordering_requested(question))
        if not order_cue:
            return [((), None, 0.0, ())]
        direction = direction or ("DESC" if draft.aggregates else "ASC")
        # A date column ordered by a word of time takes that word's direction.
        directions: dict[ColumnRef, str] = {}

        expressions: list[tuple[ColumnRef | Aggregate, float]] = []
        # "in descending order of age" names its target as "by age" does (Spider concert_singer, 2026-10-02:
        # it ordered by the stadium's Average and joined three tables for it).
        by_position = next((i for i, token in enumerate(tokens)
                            if token == "by" or (token == "of" and i > 0 and tokens[i - 1] == "order")), None)
        if by_position is not None:
            nearby = self._target_columns(mentions, by_position, numeric=False)
            expressions.extend((option.column, 2.0 - 0.1 * abs(option.position - by_position))
                               for option in nearby[:4])
        if not expressions and "alphabetically" in token_set:
            # "names ordered alphabetically" has no `by` target. Order the
            # requested text projection, not an unrelated numeric fallback.
            expressions.extend(
                (column, 2.0)
                for column in draft.projections
                if column.type == SQLType.TEXT
            )
        if draft.aggregates:
            aggregate_bonus = 3.0 if (by_position is not None or limit is not None) else 1.0
            expressions = [(aggregate, aggregate_bonus) for aggregate in draft.aggregates] + expressions
        if not expressions:
            typed = [c.ref for c in self.schema.columns
                     if c.ref.type in {SQLType.INTEGER, SQLType.REAL, SQLType.DATE} and not is_surrogate_key(c.ref.name)]
            if temporal is not None:
                # The date the question names first ("the first student to register" orders by the
                # registration date), then the table's other dates, as every Spider DEV question of this
                # shape does.
                named = [option.column for option in self._target_columns(mentions, temporal[1], numeric=False)
                         if option.column.type == SQLType.DATE]
                dated = _unique_columns(tuple(named) + tuple(c for c in typed if c.type == SQLType.DATE))
                for rank, column in enumerate(dated[:4]):
                    expressions.append((column, 1.5 - 0.1 * rank))
                    directions[column] = temporal[0]
            expressions.extend((column, 0.5) for column in typed[:4])
        out = []
        seen = set()
        for expression, expression_score in expressions:
            if expression in seen:
                continue
            seen.add(expression)
            ordered = directions.get(expression, direction)
            out.append(((OrderTerm(expression, ordered),), limit, 3.0 + expression_score,
                        (f"order:{_expr_label(expression)}:{ordered}",)))
        return sorted(out, key=lambda item: (-item[2], repr(item[0])))[:6] or [
            ((), limit, 0.5, (f"limit:{limit}",) if limit else ())
        ]

    @staticmethod
    def _required_tables(select: tuple[SelectItem, ...], predicates: tuple[Comparison, ...],
                         groups: tuple[ColumnRef, ...], orders: tuple[OrderTerm, ...]) -> set[str]:
        out = set()
        for item in select:
            out.update(_expression_tables(item.expression))
        for predicate in predicates:
            out.update(_expression_tables(predicate.left))
            out.update(_expression_tables(predicate.right))
        out.update(c.table for c in groups)
        for order in orders:
            out.update(_expression_tables(order.expression))
        return out

    @staticmethod
    def _preferred_root(required: set[str], table_scores: dict[str, float], draft: _Draft) -> str:
        count_tables = [a.operand.table for a in draft.aggregates if isinstance(a.operand, ColumnRef)]
        if count_tables:
            return count_tables[0]
        return sorted(required, key=lambda table: (-table_scores.get(table, 0.0), table))[0]


def _merge_candidates(
    existing: Sequence[ScoredQuery], generated: Sequence[ScoredQuery]
) -> list[ScoredQuery]:
    """Merge one ordered expansion stage, keeping the best score per rendered SQL."""
    combined = {candidate.sql: candidate for candidate in existing}
    for candidate in generated:
        old = combined.get(candidate.sql)
        if old is None or candidate.score > old.score:
            combined[candidate.sql] = candidate
    return sorted(combined.values(), key=lambda candidate: (-candidate.score, candidate.sql))


def _clause_tables(node: Any) -> set[str]:
    """The table qualifiers a select item, predicate or ordering reads."""
    if node is None:
        return set()
    if isinstance(node, ColumnRef):
        return {node.table}
    if isinstance(node, (Aggregate, DatePart, Lower)):
        return _clause_tables(node.operand)
    if isinstance(node, (BinaryExpr, Comparison)):
        return _clause_tables(node.left) | _clause_tables(node.right)
    if isinstance(node, BooleanExpr):
        return set().union(*(_clause_tables(term) for term in node.terms))
    if isinstance(node, InPredicate):
        source = node.source if isinstance(node.source, tuple) else ()
        return _clause_tables(node.left).union(*(_clause_tables(value) for value in source))
    return set()


def _counts_rows(node: Any) -> bool:
    """Whether ``node`` holds a COUNT(*), whose rows are those the joins make."""
    if isinstance(node, Aggregate):
        return isinstance(node.operand, Star)
    if isinstance(node, (BinaryExpr, Comparison)):
        return _counts_rows(node.left) or _counts_rows(node.right)
    if isinstance(node, BooleanExpr):
        return any(_counts_rows(term) for term in node.terms)
    return False


def _has_subquery(query: SelectQuery) -> bool:
    """Whether any clause of ``query`` holds a subquery, which may read the outer tables."""
    def walk(node: Any) -> bool:
        if isinstance(node, (ScalarSubquery, ExistsPredicate)):
            return True
        if isinstance(node, InPredicate):
            return not isinstance(node.source, tuple) or walk(node.left)
        if isinstance(node, (Aggregate, DatePart, Lower)):
            return walk(node.operand)
        if isinstance(node, (BinaryExpr, Comparison)):
            return walk(node.left) or walk(node.right)
        if isinstance(node, BooleanExpr):
            return any(walk(term) for term in node.terms)
        return False
    return (any(walk(item.expression) for item in query.select) or walk(query.where) or walk(query.having)
            or any(walk(term.expression) for term in query.order_by))


def _names_together(tokens: tuple[str, ...], words: list[str], column_forms: frozenset[str] = frozenset()) -> bool:
    """Whether the question names a several-word table by its words together: "car makers", turned
    around with "of" ("names of cars"), or its first word before one of its columns ("the car makeid" of
    car_names)."""
    if len(words) < 2:
        return True
    size = len(words)
    canon_tokens = tuple(_canon(token) for token in tokens)
    if any(canon_tokens[index:index + size] == tuple(words) for index in range(len(tokens) - size + 1)):
        return True
    turned = (words[-1], "of", *words[:-1])
    if any(canon_tokens[index:index + size + 1] == turned for index in range(len(tokens) - size)):
        return True
    return any(token == words[0] and canon_tokens[index + 1] in column_forms
               for index, token in enumerate(canon_tokens[:-1]))


def _names_a_part(tokens: tuple[str, ...], index: int) -> bool:
    """Whether the word at ``index`` begins a run of name-part words that ends at "name" or "line"."""
    following = index + 1
    while following < len(tokens) and tokens[following] in _NAME_PART_WORDS:
        following += 1
    return following < len(tokens) and tokens[following] in {"name", "line"}


def _tokens(question: str) -> tuple[str, ...]:
    return tuple(_canon(token) for token in words(question))


def _name_words(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(word.lower() for word in re.findall(r"[A-Za-z0-9]+", spaced))


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


def _column_link_words(column: ColumnRef, id_requested: bool) -> tuple[str, ...]:
    words = tuple("number" if word.lower() == "no" else _canon(word)
                  for word in _name_words(column.name))
    table_words = {_canon(word) for word in _name_words(column.table)}
    meaningful = tuple(
        word for word in words
        if word != "id" and word not in table_words
    )
    if not meaningful and is_surrogate_key(column.name) and id_requested:
        return ("id",)
    return meaningful or tuple(word for word in words if word != "id")


def _column_link_positions(
    column: ColumnRef,
    tokens: tuple[str, ...],
    schema: SchemaGraph,
    link_words: tuple[str, ...],
) -> list[int]:
    positions = [i for i, token in enumerate(tokens) if token in link_words]
    if link_words == ("number",) and "no" in {
        word.lower() for word in _name_words(column.name)
    }:
        entity_words = {
            _canon(word)
            for word in _name_words(column.table) + _name_words(column.name)
            if word.lower() != "no"
        }
        positions = [
            position for position in positions
            if set(tokens[max(0, position - 2):position]) & entity_words
        ]
    generic_link = not set(link_words) - {"name", "id"}
    duplicate_link = sum(
        _column_link_words(schema_column.ref, True) == link_words
        for schema_column in schema.columns
    ) > 1
    if len(schema.tables) <= 1 or (not generic_link and not duplicate_link):
        return positions

    column_entities = {
        _canon(word) for word in _name_words(column.table)
    } | {
        _canon(word) for word in _name_words(column.name)
        if _canon(word) not in {"name", "id", "identifier", "key"}
    }
    schema_entities = {
        _canon(word)
        for schema_column in schema.columns
        for word in _name_words(schema_column.ref.table) + _name_words(schema_column.ref.name)[:-1]
        if _canon(word) not in {"name", "id", "identifier", "key"}
    }
    qualified = []
    for position in positions:
        context = set(tokens[max(0, position - 2):position])
        if "its" in context:
            antecedent = next(
                (token for token in reversed(tokens[:position]) if token in schema_entities),
                None,
            )
            if antecedent:
                context.add(antecedent)
        if position + 1 < len(tokens) and tokens[position + 1] == "of":
            context.update(tokens[position + 2:position + 4])
        entity_context = context & schema_entities
        if not entity_context or entity_context & column_entities:
            qualified.append(position)
            continue
        if set(link_words) == {"name"}:
            context_tables = {
                table for table in schema.tables
                if set(map(_canon, _name_words(table))) <= entity_context
            }
            if any(
                foreign_key.from_column.table in context_tables
                and foreign_key.to_column.table == column.table
                for foreign_key in schema.foreign_keys
            ):
                qualified.append(position)
    return qualified


def _number(value: str):
    cleaned = value.replace(",", "")
    return parse_decimal(cleaned, enforce_input_bounds=False) if "." in cleaned else int(cleaned)


def _date_year_boundary(cue: str, year: int) -> tuple[str, str]:
    if cue == "after":
        return ">=", f"{year + 1:04d}-01-01"
    if cue == "before":
        return "<", f"{year:04d}-01-01"
    return ">=", f"{year:04d}-01-01"


def _unique_columns(columns: tuple[ColumnRef, ...]) -> tuple[ColumnRef, ...]:
    return tuple(dict.fromkeys(columns))


def _expression_tables(expression: Any) -> set[str]:
    if isinstance(expression, ColumnRef):
        return {expression.table}
    if isinstance(expression, Aggregate):
        return _expression_tables(expression.operand)
    if isinstance(expression, BinaryExpr):                       # SUM(amount * fx.rate): both tables are required
        return _expression_tables(expression.left) | _expression_tables(expression.right)
    return set()


def _operand_columns(operand: Any) -> set:
    """ColumnRefs consumed by an aggregate operand, including columns nested in a BinaryExpr
    (e.g. amount and rate in SUM(amount * rate)). Used so an aggregated column is not also treated
    as a bare projection that would force a spurious GROUP BY."""
    if isinstance(operand, ColumnRef):
        return {operand}
    if isinstance(operand, BinaryExpr):
        return _operand_columns(operand.left) | _operand_columns(operand.right)
    return set()


def _operand_label(operand: Any) -> str:
    if isinstance(operand, Star):
        return "*"
    if isinstance(operand, ColumnRef):
        return f"{operand.table}.{operand.name}"
    if isinstance(operand, BinaryExpr):
        return f"{_operand_label(operand.left)} {operand.operator} {_operand_label(operand.right)}"
    return "expr"


def _expr_label(expression: ColumnRef | Aggregate) -> str:
    if isinstance(expression, ColumnRef):
        return f"{expression.table}.{expression.name}"
    return f"{expression.function}({_operand_label(expression.operand)})"
