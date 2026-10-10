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
import weakref
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
    SetQuery,
    SQLType,
    Star,
    SubquerySource,
    and_predicates,
    comparable_value,
    contradictory,
    render_query,
    share_of,
    validate_query,
)
from engine.numeric import NUMBER_TEXT, parse_decimal
from engine.sql_candidate import ScoredQuery
from engine.sql_dates import period_grouping, served_date_phrases
from engine.sql_durations import duration_phrases, question_date, span_comparisons
from engine.closed_class import COMPARATIVE_COLUMNS, COMPARATIVES
from engine.sql_expansion import (
    AGGREGATE_CUES,
    ALPHABETICAL_WORDS,
    REVERSE_ORDER_WORDS,
    FUNCTION_WORDS,
    asked_cues,
    by_groups,
    entity_groups,
    implicit_sum_measures,
    measure_words_after,
    money_total_position,
    name_tokens,
    naming_context,
    naming_words,
    ordering_requested,
    share_cue,
    share_requested,
    spelled_names,
    word_spans,
    words,
)
from engine.sql_schema import SchemaGraph, canon, is_name_column, is_surrogate_key, name_words, normalize_value

_PROJECTION_CUES = frozenset({"show", "list", "display", "select", "give", "find", "which", "what"})
# A text the question quotes: 'Al', "Sky Radio".
_QUOTED_TEXT = re.compile(r"(?<![\w])['\"]([^'\"]+)['\"](?![\w])")
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
# Words that ask for every value of a kind: "all inspection checklist", "every roof inspection".
_QUANTIFIERS = frozenset({"all", "every"})
_ARTICLES = frozenset({"the", "a", "an"})
# Words between a column's name and the value it introduces (canon() forms): "the year 2014", "the state of
# Hawaii", "earnings above 300000", "a population between 160000 and 900000".
_VALUE_BRIDGE_WORDS = frozenset({
    "the", "a", "an", "of", "is", "are", "was", "were", "be", "being", "equal", "to", "named", "called", "as",
    "more", "less", "than", "greater", "smaller", "larger", "fewer", "higher", "lower", "above", "below", "over",
    "under", "after", "before", "between", "since", "until", "at", "least", "most", "exactly", "exceeding",
    "exceed", "both", "either",
})
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
               expand_extrema: bool = True, expand_parsimony: bool = True) -> list[ScoredQuery]:
        tokens = _tokens(question)
        if not tokens:
            return []
        copies = self._unasked_copies(tokens, question)
        if copies:
            return SQLSearcher(self.schema.without(copies), self.beam_size, self.max_candidates).search(
                question, semantic_signals, rank_candidates, expand_recursive, expand_constraints,
                expand_extrema, expand_parsimony)
        # The words of a several-word value the question states are the value's: "the avg. monthly searches for
        # forklift inspection" names the keyword 'forklift inspection', not the Forklift and Inspection tabs,
        # which no key joins (a customer's three keyword tabs, 2026-10-02: no reading at all).
        stated, _ = self._value_matches(tokens, frozenset(), question)
        in_values = {index for start, end, _, _ in stated if end - start > 1 for index in range(start, end)}
        unvalued = tuple("" if index in in_values else token for index, token in enumerate(tokens))
        table_scores = self._table_scores(unvalued)
        mentions = self._column_mentions(tokens, table_scores)
        mentions = self._suppress_fk_attribute_qualifiers(tokens, mentions)
        clause_boundary = next((i for i, token in enumerate(tokens)
                                if token in {"where", "with", "whose", "having", "from", "for",
                                             "order", "ordered", "sort", "sorted", "rank", "ranked"}),
                               len(tokens))
        # A column word that introduces a stated value names the compared column, not one to list: "singer
        # names in concerts in year 2014", "the airline with abbreviation 'UAL'" (Spider DEV, 2026-10-02: 30
        # readings listed the column they filtered). Another mention of the column still asks for it.
        qualifiers = self._value_qualifiers(tokens, question)
        context = naming_context(self.schema)
        prefix = tuple("" if index in qualifiers else token for index, token in enumerate(tokens[:clause_boundary]))
        prefix_tokens = set(prefix)
        id_requested = bool(_ID_WORDS & set(tokens))
        references = self._reference_keys()
        explicit_projection_columns = {
            schema_column.ref
            for schema_column in self.schema.columns
            if (schema_column.ref not in references or _says(tokens, schema_column.ref)) and (
                (link_words := _column_link_words(schema_column.ref, id_requested))
                and set(naming_words(link_words, context[schema_column.ref.table])) <= prefix_tokens
                and _column_link_positions(schema_column.ref, prefix, self.schema, link_words)
            )
        }
        clause_only_columns = {
            option.column for mention in mentions for option in mention.options
            if option.column not in explicit_projection_columns
        }

        from engine.sql_expansion import complete_projection_requested
        complete_projection = complete_projection_requested(question)
        projection_choices = self._projection_choices(tokens, mentions, table_scores)
        # A table joins every reading only when the question names it with its words together ("car
        # makers"); Spider car_1, 2026-10-02: "how many car makers are there in each continent? List the
        # continent name" named car_names by "car" and "name" far apart, and every reading joined it,
        # multiplying the counted rows. A table named so still scores, for the root and its display.
        # A column word that names another table names that table: "TV Channel" is TV_Channel, not
        # TV_series' first word before its Channel column.
        table_words = {table: {canon(word) for word in name_words(table)} for table in self.schema.tables}
        named_tables = {table for table in self.schema.tables
                        if _names_together(unvalued, [canon(word) for word in name_words(table)],
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
            groups = self._group_choices(tokens, mentions, table_scores, draft, question)
            for group_columns, group_score, group_evidence in groups:
                aggregated_columns = set().union(
                    *(_operand_columns(a.operand) for a in draft.aggregates)
                ) if draft.aggregates else set()
                raw_projection = tuple(c for c in draft.projections if c not in aggregated_columns)
                if not draft.aggregates and len(raw_projection) > 1 and not complete_projection:
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
                    # A period's column is headed by the period it is: "month", not its SQL.
                    expressions = tuple(SelectItem(c, "month" if isinstance(c, DatePart) else None)
                                        for c in grouped) + tuple(
                        SelectItem(a) for a in draft.aggregates
                    )
                else:
                    grouped = ()
                    # A listing follows the question's order: "the airline names and abbreviations" lists the
                    # names first (Spider DEV, 2026-10-02: 8 listings matched gold but for the order). A
                    # grouped answer keeps its groups before their aggregates, as a table shows them.
                    listed = raw_projection if complete_projection else sorted(raw_projection, key=lambda column: _named_at(tokens, column))
                    expressions = tuple(SelectItem(c) for c in listed) or (SelectItem(Star()),)

                orders = self._order_choices(tokens, mentions, draft, question)
                for order_terms, limit, order_score, order_evidence in orders:
                    if not order_terms:
                        # The months of "total amount by month" read in calendar order.
                        order_terms = tuple(OrderTerm(c) for c in grouped if isinstance(c, DatePart))
                    required = self._required_tables(expressions, draft.predicates, grouped, order_terms)
                    mentioned_tables = {table for table, score in table_scores.items()
                                        if score >= 2.5 and table in named_tables}
                    required.update(mentioned_tables)
                    if not required:
                        required.add(max(table_scores, key=table_scores.get) if table_scores else self.schema.tables[0])
                    root = self._preferred_root(required, table_scores, draft)
                    trees = self.schema.join_trees(required, root)
                    # The anchors are what the question asks of the rows: its aggregates, filters, groups and order.
                    # A column it only names beside them ("how many subscriptions by Status", where Subscriptions
                    # is a report tab's column no key joins) is projected, so it may drop out below (2026-10-05).
                    anchors = self._required_tables(tuple(SelectItem(a) for a in draft.aggregates),
                                                    draft.predicates, group_columns, order_terms)
                    reachable = self.schema.reachable(anchors or {root}) if not trees and len(required) > 1 else set()
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
                        # Rows grouped by their name are grouped by what they are: "the names of the high schoolers
                        # and how many friends each has" keeps two Jordans apart (entity_groups). A category ("by
                        # country") is no name and stays one group.
                        named = {column.table for column in grouped if isinstance(column, ColumnRef)}
                        group_by = (entity_groups(self.schema, next(iter(named)), tree.joins, grouped)
                                    if draft.aggregates and tree.joins and len(named) == 1
                                    and all(isinstance(column, ColumnRef) and is_name_column(column.name)
                                            for column in grouped) else grouped)
                        query = SelectQuery(
                            select=expressions,
                            from_table=tree.root,
                            joins=tree.joins,
                            where=and_predicates(draft.predicates),
                            group_by=group_by,
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

        column_words = {canon(word) for column in self.schema.columns for word in name_words(column.ref.name)}
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
        # The expansions read a conjunction of one column's values as either value or both
        # (engine/sql_constraints.py, engine/sql_recursive.py); the conjunction itself matches no row.
        pool = [candidate for candidate in pool if not contradictory(candidate.query)]
        # A value the question states once is compared once: "concerts in year 2014" is no concert year and
        # song release year of 2014 (Spider DEV, 2026-10-02: 10 readings compared one value on two columns).
        pool = [candidate for candidate in pool if not _reads_a_value_twice(candidate.query, tokens)]
        # The tables the question names: their words together, and scored as a mention.
        named_here = {table for table in named_tables if table_scores.get(table, 0.0) >= 2.5}
        pool = _merge_candidates([], [self._simplified(candidate, named_here) for candidate in pool])
        if not rank_candidates:
            return _rows_named(pool[:self.max_candidates])
        from engine.sql_rank import CandidateRanker
        ranked = CandidateRanker(self.schema, semantic_signals).rank(
            question, pool
        )[:self.max_candidates]
        return _rows_named(ranked)

    def _unasked_copies(self, tokens: tuple[str, ...], question: str) -> frozenset[str]:
        """The tables the search leaves out as copies of another's layout (`SchemaGraph.layout_copies`): each
        copy gives the same readings, and six subscription tabs (three exports, three reports) crowded every
        word's options until no reading joined ("the total Amount broken down by Plan and Currency",
        2026-10-04). A group keeps the copies the question names, else the first holding a value it states,
        else the first one sent; the Sheets add-on sends the active tab first."""
        groups = self.schema.layout_copies
        if not groups:
            return frozenset()
        stated, _ = self._value_matches(tokens, frozenset(), question)
        holding = {column.table for _, _, _, options in stated for column, _ in options}
        in_values = {index for start, end, _, _ in stated if end - start > 1 for index in range(start, end)}
        unvalued = tuple("" if index in in_values else token for index, token in enumerate(tokens))
        table_scores = self._table_scores(unvalued)
        left_out: set[str] = set()
        for group in groups:
            named = [table for table in group if table_scores[table] >= 2.5
                     and _names_together(unvalued, [canon(word) for word in name_words(table)])]
            kept = named or [table for table in group if table in holding][:1] or [group[0]]
            left_out.update(table for table in group if table not in kept)
        return frozenset(left_out)

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

    def _column_forms(self, table: str) -> frozenset[str]:
        """The words a question may name a column of ``table`` by: its whole name run together ("makeid")
        and its own words."""
        forms = set()
        for column in self.schema.columns:
            if column.ref.table == table:
                words = [canon(word) for word in name_words(column.ref.name)]
                forms.add("".join(words))
                forms.update(words)
        return frozenset(forms)

    def _reference_keys(self) -> set[ColumnRef]:
        """The foreign keys named by the table they reference: Documents.Template_ID is "template". The
        table's word names that table, which the join reaches ("document names using templates with
        template type code BK"); the key is a mention only where the question says its whole name ("the
        template ids"). Spider cre_Doc_Template_Mgt, 2026-10-02: such readings listed the key beside the
        names."""
        return {child for foreign_key in self.schema.foreign_keys for child, parent in foreign_key.column_pairs
                if set(_column_link_words(child, False)) <= {canon(word) for word in name_words(parent.table)}}

    def _table_scores(self, tokens: tuple[str, ...]) -> dict[str, float]:
        scores = {}
        token_set = set(tokens)
        for table in self.schema.tables:
            words = name_words(table)
            coverage = sum(1 for word in words if canon(word) in token_set)
            plural = any(canon(token) == canon(word) for token in tokens for word in words)
            scores[table] = (3.0 if coverage == len(words) and words else 0.0) + (1.0 if plural else 0.0)
        return scores

    def _column_mentions(self, tokens: tuple[str, ...], table_scores: dict[str, float]) -> tuple[_Mention, ...]:
        grouped: dict[int, list[_ColumnOption]] = {}
        token_set = set(tokens)
        id_requested = bool(_ID_WORDS & token_set)
        context = naming_context(self.schema)
        # The modifiers each two-word column name puts before its last word: "first" and "last" before "name".
        modifiers: dict[str, set[str]] = {}
        for schema_column in self.schema.columns:
            words = _column_link_words(schema_column.ref, id_requested)
            if len(words) == 2:
                modifiers.setdefault(words[1], set()).add(words[0])
        references = self._reference_keys()
        unaggregated = tuple("" if token in AGGREGATE_CUES else token for token in tokens)
        for schema_column in self.schema.columns:
            column = schema_column.ref
            if is_surrogate_key(column.name) and not id_requested:
                continue
            if column in references and not _says(tokens, column):
                continue
            meaningful = _column_link_words(column, id_requested)
            positions = _column_link_positions(column, tokens, self.schema, meaningful)
            contested: set[int] = set()
            if column.type.numeric and unaggregated != tokens:
                # A word asking an aggregate before a measure's name may be the aggregate's, not the first word
                # of another column's name: "the total Amount by Plan" totals Amount as well as naming the
                # report tabs' Total Amount. Amount was no mention, and the question had no reading (near
                # copies of a subscriptions export, 2026-10-04). The name said whole stays the stronger mention.
                contested = set(_column_link_positions(column, unaggregated, self.schema, meaningful)) - set(positions)
                positions = sorted(set(positions) | contested)
            if not positions:
                continue
            said = {tokens[i] for i in positions}
            if not set(naming_words(meaningful, context[column.table])) <= said:
                continue
            coverage = len(said & set(meaningful)) / max(len(set(meaningful)), 1)
            position = max(positions)
            phrase = " ".join(meaningful)
            exact = phrase in " ".join(tokens)
            if len(meaningful) > 1:
                # A several-word name is a mention where the question says it: "the first name, middle name,
                # last name" are three mentions, and so are the modifiers of a shared last word in "the
                # first, middle, and last name" (Spider DEV, 2026-10-02: each three were one mention at the
                # last "name", read as one of them).
                size = len(meaningful)
                said = [start + size - 1 for start in range(len(tokens) - size + 1)
                        if tokens[start:start + size] == meaningful]
                if not said and size == 2:
                    said = [index for head in range(len(tokens)) if tokens[head] == meaningful[1]
                            for index in _coordinated_modifiers(tokens, head, modifiers[meaningful[1]])
                            if tokens[index] == meaningful[0]][:1]
                position = said[-1] if said else position
            exact = exact and position not in contested
            score = 3.0 + coverage + (1.0 if exact else 0.0) + 0.15 * table_scores.get(column.table, 0.0)
            grouped.setdefault(position, []).append(_ColumnOption(column, score, position))
        mentions = []
        for position, options in sorted(grouped.items()):
            options.sort(key=lambda option: (-option.score, option.column.table, option.column.name))
            mentions.append(_Mention(position, tuple(options[:4])))
        return tuple(mentions)

    def _projection_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                            table_scores: dict[str, float]) -> list[tuple[tuple, float, tuple[str, ...]]]:
        from engine.sql_expansion import complete_projection_requested
        if complete_projection_requested(' '.join(tokens)):
            # A column named only as the sort key is not the output projection.
            # Preserve every field, including IDs and unheaded helper fields.
            return [(tuple(column.ref for column in self.schema.columns if column.ref.table == table),
                     max(score, 0.5), (f'complete-projection:{table}',))
                    for table, score in sorted(table_scores.items(), key=lambda item: (-item[1], item[0]))[:3]]
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
                if set(name_words(option.column.name)) & display_names
            }
            if not target_tables:
                continue
            qualifier = tokens[left.position]
            if not any(
                qualifier in {canon(word) for word in name_words(target)}
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
                                *(canon(word) for word in name_words(option.column.table)),
                                *(canon(word) for word in name_words(option.column.name)
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
        # A several-word column name the question spells names the column (sql_expansion.asked_cues): "the total
        # of the avg. monthly searches" sums them, with no stray average (a customer's keyword tabs, 2026-10-02),
        # and "the total amount in Paris" still totals Total Amount. Before "column" or "field" it asks nothing.
        spelled = spelled_names(tokens, self.schema)
        explicit_field_cues = {
            position for _function, position in cues
            if position in spelled
            and set(tokens[max(0, position - 3):position]) & {"column", "field"}
        }
        if explicit_field_cues:
            cues = [(function, position) for function, position in cues
                    if position not in explicit_field_cues]
        cues = asked_cues(cues, tokens, self.schema)
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
                canon(word) for table in self.schema.tables for word in name_words(table)
            }
            mention_words = {
                canon(word)
                for mention in mentions for option in mention.options
                for word in name_words(option.column.name)
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
            options: list[tuple[Aggregate | tuple[Aggregate, ...], float, str]] = []
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
                                if {canon(word) for word in name_words(option.column.name)}
                                & preferred_words
                            ]
                            targets = matching or targets
                        targets = targets[:4]
                for option in targets[:4]:
                    if function in {"SUM", "AVG"} and not option.column.type.numeric:
                        continue
                    options.append((Aggregate(function, option.column), base_score + option.score * 0.1,
                                    f"aggregate:{function}({option.column.table}.{option.column.name}){cue_note}"))
                # One aggregate word over two fields it coordinates takes both: "the average latitude and longitude"
                # averages each (Spider train, 41 of 7,000 questions; one field was averaged and the other dropped).
                pair = _coordinated_targets(tokens, position, targets, numeric=function in {"SUM", "AVG"})
                if pair:
                    options.append((tuple(Aggregate(function, option.column) for option in pair),
                                    base_score + sum(option.score for option in pair) * 0.1 + 0.5,
                                    f"aggregate:{function}:coordinated"))
            expanded = []
            for aggregates, score, evidence in beam:
                for aggregate, option_score, reason in options:
                    added = aggregate if isinstance(aggregate, tuple) else (aggregate,)
                    if set(added) & set(aggregates):
                        continue
                    expanded.append((aggregates + added, score + option_score, evidence + (reason,)))
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
                 if noun in {canon(word) for word in name_words(column.ref.name)}]
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
            words = tuple(canon(word) for word in name_words(column.name))
            if not words or words[-1] not in {"id", "identifier", "key", "name"}:
                continue
            role = set(words[:-1])
            table_role = {canon(word) for word in name_words(column.table)}
            if (role and role & subject) or (not role and table_role & subject):
                matches.append(column)
        return sorted(matches, key=lambda column: (
            0 if name_words(column.name)[-1].lower() == "name" else 1,
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
            return bool({canon(word) for word in name_words(column.name)} & column_words)

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
        # A duration claims its words too: "for greater than 6 months" is how long a row lasted, not a quantity
        # above 6 and a billing interval of 'month' (engine/sql_durations.py).
        durations = duration_phrases(question, tokens)
        claimed |= {index for phrase in durations for index in range(phrase.start, phrase.end)}
        substrings, held = self._substring_groups(tokens, mentions, question, claimed)
        claimed = claimed | held
        groups.extend(substrings)
        groups.extend(self._value_predicate_groups(tokens, mentions, claimed, question))
        groups.extend(self._date_phrase_groups(phrases, mentions))
        groups.extend(self._duration_groups(durations))
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
        """The filter options of each text the question asks values to hold (``substring_requests``):
        LOWER(column) LIKE '%al%' (Spider DEV, 2026-10-02: no reading had a substring filter). The column
        is one the question says the text is in or whose whole value it quantified, else the text column the
        question names nearest the text whose values hold it, else one whose values hold it, else the nearest
        named text column (the text may be missing from the data)."""
        groups, held = [], set()
        for request in self._substring_requests(tokens, question, claimed):
            folded = request.text
            holders = [column.ref for column in self.schema.columns
                       if column.ref.type == SQLType.TEXT
                       and any(folded in str(value).lower() for value in column.values if value is not None)]
            near = sorted((option for mention in mentions for option in mention.options
                           if option.column.type == SQLType.TEXT),
                          key=lambda option: (abs(option.position - request.start), -option.score,
                                              option.column.table, option.column.name))
            targets = _unique_columns(request.subject + request.columns
                                      + tuple(option.column for option in near if option.column in holders)
                                      + tuple(holders)) or _unique_columns(tuple(option.column for option in near))
            # The first target is the likeliest, and scores so: with four alike, "a city containing the substring
            # 'West'" ranked a reading that searched the street first (Spider DEV 970, 2026-10-06).
            options = [((Comparison(Lower(column), "LIKE", Literal(f"%{folded}%", SQLType.TEXT)),),
                        5.0 - 0.5 * rank, f"substring:{column.table}.{column.name}")
                       for rank, column in enumerate(targets[:4])]
            if options:
                groups.append(options)
                held.update(range(request.start, request.end))
        return groups, held

    def _substring_requests(self, tokens: tuple[str, ...], question: str,
                            claimed: set[int] | frozenset[int]) -> tuple["SubstringRequest", ...]:
        """The texts the question asks values to hold, in question order:

        - a quoted text, or the word after "substring", "letter" or "word": "the contestants whose names
          contain the substring 'Al'", "a song having 'Hey' in its name", "the documents that contain the
          letter w in their description". "substring", "letter" and "the word X" ask for a text inside values
          even where it is also a whole value ("the substring 'Al'" beside a state code 'AL'). After "contain",
          "include" or "in its", a whole value of the data stays an equality where words stand between them:
          "the documents that contain the paragraph text 'Brazil'" are that paragraph's. Right after the word,
          it is the text values hold: "the keywords containing 'inspection checklist'" (a customer's keyword
          sheet, 2026-10-06), quoted or not.
        - a whole value after "all" or "every" that only one row holds while other values hold it inside
          them: "the volume for all inspection checklist" asks for every keyword holding the phrase, not the
          one keyword that is the phrase (the same sheet). "all Paris orders", where Paris is many rows'
          city, stays an equality.
        """
        named = re.search(r"\b(?:substring|letter|word)\s+(?:the\s+)?['\"]?([\w-]+)", question, re.I)
        explicit = bool(set(tokens) & _SUBSTRING_CUES) or named is not None
        cue_words = frozenset(token for token in tokens if token in _SUBSTRING_CUES | _INCLUDE_CUES)
        cue_words |= frozenset({"word"}) if named and named.group(0).lower().startswith("word") else frozenset()
        requests, taken = [], set(claimed)
        if explicit or set(tokens) & _INCLUDE_CUES or re.search(r"\bin (?:its|their|the)\b", question, re.I):
            texts = [text.strip() for text in _QUOTED_TEXT.findall(question)]
            if named and not texts:
                texts = [named.group(1)]
            for text in texts:
                wanted = _tokens(text)
                start = next((index for index in range(len(tokens) - len(wanted) + 1)
                              if tokens[index:index + len(wanted)] == wanted and index not in taken), None)
                if not wanted or start is None:
                    continue
                whole = self.schema.value_index.get(" ".join(wanted))
                if not explicit and whole and not _follows(tokens, start, _INCLUDE_CUES):
                    continue
                # A text that names a whole value is compared as the data writes it: "containing 'inspection
                # checklists'" holds the keyword 'inspection checklist'.
                compared = str(whole[0][1]).lower() if whole and not explicit else text.lower()
                requests.append(SubstringRequest(start, start + len(wanted), compared, cue_words,
                                                 subject=self._text_subject(tokens, start)))
                taken.update(range(start, start + len(wanted)))
        quoted = _quoted_positions(question)
        matches, _ = self._value_matches(tokens, frozenset(taken), question)
        for start, end, _phrase, options in matches:
            if set(range(start, end)) & quoted:
                continue
            texts = [(column, value) for column, value in options if column.type == SQLType.TEXT]
            if texts and _follows(tokens, start, _INCLUDE_CUES):
                requests.append(SubstringRequest(start, end, str(texts[0][1]).lower(), cue_words,
                                                 subject=self._text_subject(tokens, start)))
            elif texts and _follows(tokens, start, _QUANTIFIERS):
                texts = [(column, value) for column, value in texts if self._held_inside_others(column, value)]
                quantifier = tokens[start - 1] if tokens[start - 1] in _QUANTIFIERS else tokens[start - 2]
                if texts:
                    columns = _unique_columns(tuple(column for column, _ in texts))
                    requests.append(SubstringRequest(start, end, str(texts[0][1]).lower(), frozenset({quantifier}),
                                                     columns, columns))
        return tuple(sorted(requests, key=lambda request: request.start))

    def _text_subject(self, tokens: tuple[str, ...], start: int) -> tuple[ColumnRef, ...]:
        """The text columns the question says a text at ``start`` is in, named right before the "contain" or
        "include" that asks for it: "a city containing the substring 'West'" and "keywords that contain
        'inspection checklist'" name the city and the keywords; "the state whose name contains 'North'" names
        the state, and "contestants whose names contain 'Al'" their names. None where no such word stands
        there ("documents that contain the letter w in their description")."""
        cue = start - 1
        while cue >= 0 and tokens[cue] in _ARTICLES | _SUBSTRING_CUES | {"word", "text"}:
            cue -= 1
        if cue < 1 or tokens[cue] not in _INCLUDE_CUES:
            return ()
        before = cue - 1
        if tokens[before] in {"that", "which"} and before > 0:
            before -= 1

        def named(word: str) -> tuple[ColumnRef, ...]:
            return tuple(column.ref for column in self.schema.columns if column.ref.type == SQLType.TEXT
                         and canon(name_words(column.ref.name)[-1]) == word)

        if tokens[before] == "name" and before >= 2 and tokens[before - 1] == "whose":
            return named(tokens[before - 2]) or named("name")
        return named(tokens[before])

    def _held_inside_others(self, column: ColumnRef, value: Any) -> bool:
        """Whether one row of ``column`` holds ``value`` and another of its values holds it inside, word for
        word."""
        counts: dict[str, int] = {}
        for cell in self.schema.column_map[(column.table, column.name)].values:
            if cell is not None:
                normalized = normalize_value(cell)
                counts[normalized] = counts.get(normalized, 0) + 1
        target = normalize_value(value)
        return counts.get(target) == 1 and any(
            other != target and f" {target} " in f" {other} " for other in counts)

    def _value_matches(self, tokens: tuple[str, ...], claimed: set[int] | frozenset[int],
                       question: str) -> tuple[list[tuple[int, int, str, tuple[tuple[ColumnRef, Any], ...]]], set[int]]:
        """The data values the question states, longest first without overlap, in question order, and the
        positions they and ``claimed`` occupy. Words that spell a several-word column's name name that
        column unless quoted: "the first and last name" holds no value 'Last', "the package options" no
        'Option', "the vote ids" no state 'ID' (Spider DEV, 2026-10-02: 8 readings filtered on them)."""
        capitalized = _capitalized(question)
        quoted = _quoted_positions(question)
        named = spelled_names(tokens, self.schema)
        matches: list[tuple[int, int, str, tuple[tuple[ColumnRef, Any], ...]]] = []
        for start in range(len(tokens)):
            for size in range(1, min(6, len(tokens) - start) + 1):
                if size == 1 and tokens[start] in FUNCTION_WORDS and tokens[start] not in capitalized:
                    continue                    # "in" alone is no Code2 'IN'; "Welcome to NY" stays one value
                phrase = " ".join(tokens[start:start + size])
                span = set(range(start, start + size))
                if span <= named and not span <= quoted:
                    continue
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
        return selected, occupied

    def _value_qualifiers(self, tokens: tuple[str, ...], question: str) -> frozenset[int]:
        """Positions of the column words that introduce a value the question states: the words of the
        value's own column before it, across bridge words ("in year 2014", "the state of Hawaii", "earnings
        above 300000", "written by Joseph Kuhr"), or right after a data value ("'Brig' type ships"). A
        number's column is any numeric or date one ("hired after 2015")."""
        selected, _ = self._value_matches(tokens, frozenset(), question)
        spans = [(start, end, {column for column, _ in options}, True) for start, end, _, options in selected]
        numeric = {column.ref for column in self.schema.columns
                   if column.ref.type.numeric or column.ref.type == SQLType.DATE}
        spans += [(index, index + 1, numeric, False) for index, token in enumerate(tokens) if NUMBER_TEXT.match(token)]
        return frozenset(position for start, end, columns, data_value in spans
                         for position in _introducers(tokens, start, end, columns, data_value))

    def _value_predicate_groups(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                                claimed: set[int] = frozenset(),
                                question: str = "") -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        selected, occupied = self._value_matches(tokens, claimed, question)
        negated = _negated_matches(tokens, selected)
        groups = []
        for index, (start, end, phrase, options) in enumerate(selected):
            operator = "!=" if index in negated else "="
            # The column whose words introduce the value is the one it names: "left / L hand" is a hand,
            # not a first name 'L' (Spider wta_1, 2026-10-02).
            introduced = {column for column, _ in options if _introducers(tokens, start, end, {column}, True)}
            choices = []
            for column, value in sorted(options, key=lambda item: (item[0] not in introduced, item[0].table,
                                                                   item[0].name))[:4]:
                literal = Literal(value, column.type)
                choices.append(((Comparison(column, operator, literal),), 5.5 if column in introduced else 5.0,
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

    def _duration_groups(self, durations) -> list[list[tuple[tuple[Comparison, ...], float, str]]]:
        """One group per duration phrase: its span comparison over each pair of date columns it may run
        between, the surest pair scored highest (engine/sql_durations.span_pairs)."""
        groups = []
        until = question_date()
        for phrase in durations:
            options = [((comparison,), 5.0 - 0.1 * rank,
                        f"span:{comparison.left.table}.{comparison.left.start.name}"
                        f"..{comparison.left.end.name if comparison.left.end else comparison.left.until}:"
                        f"{phrase.operator}{phrase.amount}{phrase.unit}")
                       for rank, comparison in enumerate(span_comparisons(self.schema.columns, phrase, until))]
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
            found = [(j, _number(tokens[j])) for j in range(i + 1, min(len(tokens), i + 7)) if NUMBER_TEXT.match(tokens[j])]
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
                                     if j not in used_numbers and NUMBER_TEXT.match(tokens[j])), None)
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
            operator = COMPARATIVES.get(token)
            if operator is None or i in claimed:
                continue
            than = next((j for j in range(i + 1, min(len(tokens), i + 4)) if tokens[j] == "than"), None)
            if than is None:
                continue
            number_index = next((j for j in range(than + 1, min(len(tokens), than + 3))
                                 if j not in used_numbers and NUMBER_TEXT.match(tokens[j])), None)
            if number_index is None:
                continue
            value = _number(tokens[number_index])
            # The measure the word implies comes first ("older": an age column, mentioned or not); no
            # comparative compares an identifier.
            described = COMPARATIVE_COLUMNS.get(token, frozenset())
            implied = tuple(column.ref for column in self.schema.columns
                            if column.ref.type.numeric and not is_surrogate_key(column.ref.name)
                            and described & {canon(word) for word in name_words(column.ref.name)})
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
                            if c.ref.type == SQLType.DATE or "year" in name_words(c.ref.name)]
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
            if option.column.type == SQLType.DATE or "year" in name_words(option.column.name)
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
            if column.ref.type == SQLType.DATE or "year" in name_words(column.ref.name)
        ]

    def _group_choices(self, tokens: tuple[str, ...], mentions: tuple[_Mention, ...],
                       table_scores: dict[str, float], draft: _Draft,
                       question: str) -> list[tuple[tuple[ColumnRef, ...], float, tuple[str, ...]]]:
        if not draft.aggregates:
            return [((), 0.0, ())]
        targets = set().union(*(_operand_columns(a.operand) for a in draft.aggregates))
        projection_groups = _unique_columns(tuple(c for c in draft.projections if c not in targets))
        raw = words(question)
        explicit_positions = [i for i, token in enumerate(tokens)
                              if token in {"each", "per"} or by_groups(tokens, raw, i)]
        named_at = {option.column: option.position for mention in mentions for option in mention.options}
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
                # A projected column no key joins to the group's table is not grouped with it: it cannot be in
                # the same query, and grouping by it left "how many subscriptions by Status" no reading. A column
                # named after "by" is a group the question asks for, joined or not ("by Plan and Currency").
                reach = self.schema.reachable({option.column.table})
                groups = _unique_columns(tuple(column for column in projection_groups
                                               if column.table in reach or named_at.get(column, -1) > position)
                                         + (option.column,))
                options.append((groups, 3.0 + option.score * 0.1,
                                (f"group:{option.column.table}.{option.column.name}",)))
            if tokens[position] in {"each", "per"} or not projection_groups:
                table_positions = {
                    table: min((i for i, token in enumerate(tokens)
                                if token in {canon(w) for w in name_words(table)}), default=len(tokens) + 5)
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
        # "total amount by month", "monthly total amount", "how many orders per month": one row per year-month
        # of a date column (2026-10-02: each was an ungrouped total, or grouped by customer). The column is the
        # date the question names, else a date of a table the query reads, else the schema's one date.
        if period_grouping(tokens, self.schema) is not None:
            for column in self._period_columns(mentions, draft):
                groups = _unique_columns(projection_groups + (DatePart("year_month", column),))
                options.append((groups, 3.8, (f"group-period:{column.table}.{column.name}:year_month",)))
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

    def _period_columns(self, mentions: tuple[_Mention, ...], draft: _Draft) -> list[ColumnRef]:
        """The date columns a grouping by month reads, at most two in schema order: those the question names,
        else those of the tables the draft reads, else every date column."""
        named = [option.column for mention in mentions for option in mention.options
                 if option.column.type == SQLType.DATE]
        if named:
            return list(dict.fromkeys(named))[:2]
        dates = [column.ref for column in self.schema.columns if column.ref.type == SQLType.DATE]
        read = set().union(*(_clause_tables(aggregate.operand) for aggregate in draft.aggregates),
                           *(_clause_tables(column) for column in draft.projections),
                           *(_clause_tables(predicate) for predicate in draft.predicates))
        return ([column for column in dates if column.table in read] or dates)[:2]

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
                              if NUMBER_TEXT.match(tokens[j]) and int(_number(tokens[j])) > 0), None)
                limit = limit or 1
                break
        if limit is None and token_set & {"most", "least", "highest", "lowest", "largest", "smallest"}:
            limit = 1

        order_cue = (direction is not None or limit is not None or temporal is not None
                     or ordering_requested(question))
        if not order_cue:
            return [((), None, 0.0, ())]
        # "in alphabetical order" and "alphabetically" order a text field A to Z, Z to A when reversed.
        alphabetical = bool(token_set & ALPHABETICAL_WORDS)
        if alphabetical:
            direction = "DESC" if token_set & REVERSE_ORDER_WORDS else "ASC"
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
        if not expressions and alphabetical:
            # "names ordered alphabetically" and "names in alphabetical order" have no `by` target. Order the
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
        # An age grade orders each field in its own direction (sql_extrema.superlative_direction): "from the oldest
        # to the youngest" is an age descending and a birth date ascending. "Oldest" was ascending on every field,
        # so no candidate ordered singers by age from the oldest (Spider DEV 2, 2026-10-10).
        from engine.sql_extrema import superlative_direction
        graded = next((token for token in tokens if token in {"youngest", "oldest"}), None)
        out = []
        seen = set()
        for expression, expression_score in expressions:
            if expression in seen:
                continue
            seen.add(expression)
            ordered = directions.get(expression, direction)
            if graded and isinstance(expression, ColumnRef) and expression not in directions:
                ordered = superlative_direction(graded, expression)
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


def _coordinated_targets(tokens: tuple[str, ...], position: int, targets: Sequence[_ColumnOption],
                         numeric: bool) -> tuple[_ColumnOption, _ColumnOption] | None:
    """The two fields the phrase an aggregate word at ``position`` begins coordinates, the first two it names after
    the word, joined by "and" with nothing but articles between: "the average latitude and longitude", "total cost
    and price". None when the phrase names one."""
    after = sorted((option for option in targets if option.position > position
                    and (not numeric or option.column.type.numeric)), key=lambda option: (option.position, -option.score))
    first = next(iter(after), None)
    if first is None:
        return None
    second = next((option for option in after if option.position > first.position
                   and option.column != first.column), None)
    if second is None:
        return None
    between = tokens[first.position + 1:second.position]
    if "and" not in between or set(between) - {"and", "the", "its", "their", "a", "an"}:
        return None
    if set(tokens[position + 1:first.position]) & {"and", "by", "of", "for", "per", "where", "with"}:
        return None
    return first, second


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
    canon_tokens = tuple(canon(token) for token in tokens)
    if any(canon_tokens[index:index + size] == tuple(words) for index in range(len(tokens) - size + 1)):
        return True
    turned = (words[-1], "of", *words[:-1])
    if any(canon_tokens[index:index + size + 1] == turned for index in range(len(tokens) - size)):
        return True
    return any(token == words[0] and canon_tokens[index + 1] in column_forms
               for index, token in enumerate(canon_tokens[:-1]))


def _says(tokens: tuple[str, ...], column: ColumnRef) -> bool:
    """Whether the question says the column's whole name: its words together ("template id") or its last
    word first ("the ids of the documents")."""
    words = tuple(canon(word) for word in name_words(column.name))
    if any(tokens[start:start + len(words)] == words for start in range(len(tokens) - len(words) + 1)):
        return True
    for start in range(len(tokens) - 1):
        if tokens[start] != words[-1] or tokens[start + 1] != "of":
            continue
        following = start + 2
        while following < len(tokens) and tokens[following] in {"the", "a", "an", "all", "each", "every"}:
            following += 1
        if tokens[following:following + len(words) - 1] == words[:-1]:
            return True
    return False


def _named_at(tokens: tuple[str, ...], column: ColumnRef) -> int:
    """Where the question first names a column, past the end when it does not: where its whole name is
    said ("template type codes"); at the last word of its name, or a modifier said just before it ("the
    first and last name"); at another word of its name ("the role" of role_code); at its table's word."""
    end = len(tokens)
    name = tuple(canon(word) for word in name_words(column.name))
    size = len(name)
    said = next((start for start in range(end - size + 1) if tokens[start:start + size] == name), None)
    if said is not None:
        return said
    last = next((index for index, token in enumerate(tokens) if name and token == name[-1]), None)
    if last is not None:
        first = next((index for index in range(last - 1, max(-1, last - 5), -1)
                      if size > 1 and tokens[index] == name[0]), None)
        return last if first is None else first
    for named in (set(name), {canon(word) for word in name_words(column.table)}):
        found = next((index for index, token in enumerate(tokens) if token in named), None)
        if found is not None:
            return found
    return end


def _coordinated_modifiers(tokens: tuple[str, ...], head: int, modifiers: set[str]) -> list[int]:
    """The positions of the modifiers coordinated before ``head``: "first", "middle" and "last" in "the
    first, middle, and last name"."""
    found = []
    index = head - 1
    while index >= 0 and (tokens[index] in modifiers or tokens[index] in {"and", "or"}):
        if tokens[index] in modifiers:
            found.append(index)
        index -= 1
    return found if len(found) > 1 else []


def _reads_a_value_twice(query: Any, tokens: tuple[str, ...]) -> bool:
    """Whether a WHERE of ``query``, in any of its SELECTs, compares a value the question states on
    different columns in more of its AND-ed terms than the question states it. One term may compare it on
    several columns ("the first or last name Smith"); a value the question does not state (a date bound,
    a category's initial) is not counted."""
    if isinstance(query, SetQuery):
        return _reads_a_value_twice(query.left, tokens) or _reads_a_value_twice(query.right, tokens)
    if not isinstance(query, SelectQuery):
        return False
    if isinstance(query.from_table, SubquerySource) and _reads_a_value_twice(query.from_table.query, tokens):
        return True
    readings: dict[Any, set[tuple[int, Any]]] = {}
    for index, term in enumerate(_and_terms(query.where)):
        for comparison in _comparisons(term):
            if isinstance(comparison.right, Literal) and not isinstance(comparison.left, Literal):
                readings.setdefault(comparable_value(comparison.right.value), set()).add((index, comparison.left))
    for value, read in readings.items():
        columns = {left for _, left in read}
        if len(columns) > 1 and len({index for index, _ in read}) > 1:
            stated = _times_stated(value, tokens)
            if stated and len(columns) > stated:
                return True
    return False


def _and_terms(predicate: Any) -> list[Any]:
    if isinstance(predicate, BooleanExpr) and predicate.operator == "AND":
        return [term for child in predicate.terms for term in _and_terms(child)]
    return [] if predicate is None else [predicate]


def _rows_named(candidates: Sequence[ScoredQuery]) -> list[ScoredQuery]:
    """``candidates`` with each listing of numbers alone showing first the text column its filter keeps
    several values of. "the Avg. monthly searches for all Keyword containing 'inspection checklist'" listed 38
    numbers, and nothing said which keyword each was (a customer's keyword sheet, 2026-10-06). A listing that
    shows a text or key column already names its rows, one value filtered on names none, and an aggregate, a
    group, DISTINCT or a single row asks for values, not rows: those stay as they are. It runs after the
    ranking, which reads the question's words; the column is what the answer shows, not what it was asked."""
    named: dict[str, ScoredQuery] = {}
    for candidate in candidates:
        query = candidate.query
        if (isinstance(query, SelectQuery) and query.select and not query.group_by and query.having is None
                and not query.distinct and query.limit != 1
                and all(_a_number(item.expression) for item in query.select)):
            picked = tuple(dict.fromkeys(column for term in _and_terms(query.where)
                                         if (column := _several_values(term)) is not None))
            if picked:
                labelled = replace(query, select=tuple(SelectItem(column) for column in picked) + query.select)
                try:
                    validate_query(labelled)
                    candidate = replace(candidate, query=labelled, sql=render_query(labelled),
                                        evidence=candidate.evidence + ("rows named",))
                except (TypeError, ValueError):
                    pass
        named.setdefault(candidate.sql, candidate)
    return list(named.values())


def _a_number(expression: Any) -> bool:
    """Whether a listed ``expression`` is a number that names no row: a numeric column other than a key, or
    arithmetic over a row's numbers."""
    if isinstance(expression, ColumnRef):
        return expression.type.numeric and not is_surrogate_key(expression.name)
    return isinstance(expression, BinaryExpr) and not _has_aggregate(expression)


def _has_aggregate(expression: Any) -> bool:
    if isinstance(expression, Aggregate):
        return True
    return isinstance(expression, BinaryExpr) and (_has_aggregate(expression.left) or _has_aggregate(expression.right))


def _several_values(term: Any) -> ColumnRef | None:
    """The text column a filter ``term`` keeps more than one value of, if any: a LIKE pattern with a
    wildcard, NOT LIKE, !=, IN over two or more values or NOT IN, or an OR of such comparisons on one
    column. An equality keeps one value, and a numeric column's values name no row."""
    if isinstance(term, BooleanExpr) and term.operator == "OR":
        alternatives = [_compared_text(alternative) for alternative in term.terms]
        columns = {column for column, _ in alternatives}
        if None in columns or len(columns) != 1 or len(term.terms) < 2:
            return None
        if any(several for _, several in alternatives) or len({repr(alternative) for alternative in term.terms}) > 1:
            return columns.pop()
        return None
    column, several = _compared_text(term)
    return column if several else None


def _compared_text(term: Any) -> tuple[ColumnRef | None, bool]:
    """The text column ``term`` compares with values, and whether it keeps more than one of them."""
    if isinstance(term, InPredicate) and isinstance(term.source, tuple) and isinstance(term.left, ColumnRef):
        if term.left.type != SQLType.TEXT or not all(isinstance(value, Literal) for value in term.source):
            return None, False
        return term.left, term.negated or len({repr(value) for value in term.source}) > 1
    if not isinstance(term, Comparison) or not isinstance(term.right, Literal):
        return None, False
    column = term.left.operand if isinstance(term.left, Lower) else term.left
    if not isinstance(column, ColumnRef) or column.type != SQLType.TEXT:
        return None, False
    if term.operator in {"!=", "<>", "NOT LIKE"}:
        return column, True
    if term.operator == "LIKE":
        return column, bool(re.search(r"[%_]", str(term.right.value)))
    return (column, False) if term.operator == "=" else (None, False)


def _comparisons(predicate: Any) -> list[Comparison]:
    if isinstance(predicate, BooleanExpr):
        return [comparison for term in predicate.terms for comparison in _comparisons(term)]
    return [predicate] if isinstance(predicate, Comparison) else []


def _times_stated(value: Any, tokens: tuple[str, ...]) -> int:
    """How many times the question states ``value``: a number as a number, a text as its words."""
    if isinstance(value, float):
        return sum(1 for token in tokens if NUMBER_TEXT.match(token) and float(_number(token)) == value)
    wanted = _tokens(str(value))
    if not wanted:
        return 0
    return sum(1 for start in range(len(tokens) - len(wanted) + 1) if tokens[start:start + len(wanted)] == wanted)


def _introducers(tokens: tuple[str, ...], start: int, end: int, columns: set[ColumnRef],
                 data_value: bool) -> set[int]:
    """The positions of the words of ``columns`` that introduce the value at ``start``-``end``: before it,
    across bridge words ("in year 2014", "the state of Hawaii", "earnings above 300000"), or right after a
    data value ("'Brig' type ships")."""
    words = {word for column in columns for word in _column_link_words(column, True)}
    found = set()
    before = start - 1
    while before >= 0 and (tokens[before] in words or tokens[before] in _VALUE_BRIDGE_WORDS):
        if tokens[before] in words:
            found.add(before)
        before -= 1
    after = end
    while data_value and after < len(tokens) and tokens[after] in words:
        found.add(after)
        after += 1
    return found


def _quoted_positions(question: str) -> frozenset[int]:
    """The positions of the question's words that stand inside quotes."""
    quoted = [match.span(1) for match in _QUOTED_TEXT.finditer(question.lower())]
    return frozenset(index for index, (start, end) in enumerate(word_spans(question))
                     if any(left <= start and end <= right for left, right in quoted))


# The words that exclude the value after them ("orders not Done", "every order except refunds"), and the ones
# that join a value to the one before it into one list ("not Done or Cancelled").
_NEGATION_CUES = frozenset({"not", "except", "excluding", "without"})
_LIST_JOINERS = frozenset({"and", "or", "nor"}) | _ARTICLES


def _negated_matches(tokens: tuple[str, ...], selected) -> frozenset[int]:
    """The indexes of the value matches (``start``, ``end``, ...; in order) a negation excludes. A negation cue
    excludes the first value within three words after it, and each value "and", "or" or "nor" joins to an
    excluded one: "orders not Done or Cancelled", "excluding Paris, Lyon and Nice". Another value between the
    cue and a value ends the exclusion: in "orders not Done in France", France is where the orders are, and the
    search compared country != 'France' (a release review, 2026-10-07). A cue inside a value belongs to the
    value ("tasks that are Not Started")."""
    inside = {position for start, end, *_ in selected for position in range(start, end)}
    cues = [position for position, token in enumerate(tokens) if token in _NEGATION_CUES and position not in inside]
    negated: set[int] = set()
    for index, (start, _end, *_) in enumerate(selected):
        if index - 1 in negated and all(token in _LIST_JOINERS for token in tokens[selected[index - 1][1]:start]):
            negated.add(index)
            continue
        cue = max((position for position in cues if start - 3 <= position < start), default=None)
        if cue is not None and not any(cue < other[0] < start for other in selected):
            negated.add(index)
    return frozenset(negated)


_POLARITY: "weakref.WeakKeyDictionary[SchemaGraph, dict[str, tuple[frozenset, tuple[frozenset, ...]]]]" = \
    weakref.WeakKeyDictionary()


def value_polarity(question: str, schema: SchemaGraph) -> tuple[frozenset, tuple[frozenset, ...]]:
    """The (table, column, value) readings of the values ``question`` states: the set it keeps, and for each
    value a negation excludes (``_negated_matches``) the set of its readings, as the search compares them.
    The completeness check (engine/query_contract.py) refuses a query that excludes a kept value, keeps an
    excluded one, or leaves an excluded value in. Read once per question and schema graph."""
    by_question = _POLARITY.setdefault(schema, {})
    found = by_question.get(question)
    if found is None:
        tokens = _tokens(question)
        claimed = {index for phrase in (*served_date_phrases(question, tokens, schema),
                                        *duration_phrases(question, tokens))
                   for index in range(phrase.start, phrase.end)}
        claimed |= {index for request in substring_requests(question, schema)
                    for index in range(request.start, request.end)}
        selected, _ = SQLSearcher(schema)._value_matches(tokens, claimed, question) if tokens else ([], set())
        negated = _negated_matches(tokens, selected)
        kept, excluded = set(), []
        for index, (_start, _end, _phrase, options) in enumerate(selected):
            readings = frozenset((column.table, column.name, value) for column, value in options)
            if index in negated:
                excluded.append(readings)
            else:
                kept |= readings
        found = by_question[question] = (frozenset(kept), tuple(excluded))
    return found


def _follows(tokens: tuple[str, ...], start: int, cues: frozenset[str]) -> bool:
    """Whether the word before ``start``, or the one before an article there, is one of ``cues``."""
    before = start - 1
    if before >= 0 and tokens[before] in _ARTICLES:
        before -= 1
    return before >= 0 and tokens[before] in cues


@dataclass(frozen=True)
class SubstringRequest:
    """A text the question asks values to hold (``SQLSearcher._substring_requests``): where it stands in the
    question's words, the lower-case text a LIKE compares, the words that ask for it, the columns whose whole
    value it quantified ("all inspection checklist"), when it did, and the columns the question says the text
    is in, when it says: those, or the city of "a city containing" (``SQLSearcher._text_subject``)."""
    start: int
    end: int
    text: str
    cues: frozenset[str]
    columns: tuple[ColumnRef, ...] = ()
    subject: tuple[ColumnRef, ...] = ()


_REQUESTS: "weakref.WeakKeyDictionary[SchemaGraph, dict[str, tuple[SubstringRequest, ...]]]" = \
    weakref.WeakKeyDictionary()


def substring_requests(question: str, schema: SchemaGraph) -> tuple[SubstringRequest, ...]:
    """The texts ``question`` asks values to hold, as the search reads them: the completeness check
    (engine/query_contract.py) requires each and reads the words that ask for one it compares. Read once per
    question and schema graph."""
    by_question = _REQUESTS.setdefault(schema, {})
    requests = by_question.get(question)
    if requests is None:
        tokens = _tokens(question)
        claimed = {index for phrase in (*served_date_phrases(question, tokens, schema),
                                        *duration_phrases(question, tokens))
                   for index in range(phrase.start, phrase.end)}
        requests = SQLSearcher(schema)._substring_requests(tokens, question, claimed) if tokens else ()
        by_question[question] = requests
    return requests


def realizes_substring(query: Any, text: str, columns: Sequence[ColumnRef] = ()) -> bool:
    """Whether ``query`` compares a column with LIKE '%text%', anywhere in it, and one of ``columns`` when
    any are given."""
    from dataclasses import fields, is_dataclass

    pattern = f"%{text}%"
    named = {(column.table, column.name) for column in columns}

    def found(node: Any) -> bool:
        if isinstance(node, Comparison) and node.operator == "LIKE" and isinstance(node.right, Literal):
            compared = node.left.operand if isinstance(node.left, Lower) else node.left
            if str(node.right.value).lower() == pattern and (
                    not named or (getattr(compared, "table", None), getattr(compared, "name", None)) in named):
                return True
        if is_dataclass(node) and not isinstance(node, type):
            return any(found(getattr(node, field.name)) for field in fields(node))
        if isinstance(node, (tuple, list)):
            return any(found(item) for item in node)
        return False

    return found(query)


def _capitalized(question: str) -> frozenset[str]:
    """The question's words written in capitals ("NY", "UAL", a lone "A" after the first word): a function
    word so written is a value."""
    return frozenset(word.lower() for word in re.findall(r"\b[A-Z]{2,}\b|(?<=[\s'\"(])[A-Z]\b", question))


def _names_a_part(tokens: tuple[str, ...], index: int) -> bool:
    """Whether the word at ``index`` begins a run of name-part words that ends at "name" or "line"."""
    following = index + 1
    while following < len(tokens) and tokens[following] in _NAME_PART_WORDS:
        following += 1
    return following < len(tokens) and tokens[following] in {"name", "line"}


def _tokens(question: str) -> tuple[str, ...]:
    return tuple(canon(token) for token in words(question))




def _column_link_words(column: ColumnRef, id_requested: bool) -> tuple[str, ...]:
    words = tuple("number" if word.lower() == "no" else canon(word)
                  for word in name_words(column.name))
    table_words = {canon(word) for word in name_words(column.table)}
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
        word.lower() for word in name_words(column.name)
    }:
        entity_words = {
            canon(word)
            for word in name_words(column.table) + name_words(column.name)
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
        canon(word) for word in name_words(column.table)
    } | {
        canon(word) for word in name_words(column.name)
        if canon(word) not in {"name", "id", "identifier", "key"}
    }
    schema_entities = {
        canon(word)
        for schema_column in schema.columns
        for word in name_words(schema_column.ref.table) + name_words(schema_column.ref.name)[:-1]
        if canon(word) not in {"name", "id", "identifier", "key"}
    }
    # A several-word name the question spells names the column whatever precedes it: "of the avg. monthly
    # searches" is no other entity's, though "Top of page bid (low range)" makes "of" an entity word (a
    # customer's three keyword tabs, 2026-10-02: the column was never mentioned).
    own = name_tokens(column.name)
    spelled = {index for start in range(len(tokens) - len(own) + 1)
               if len(own) > 1 and tokens[start:start + len(own)] == own
               for index in range(start, start + len(own))}
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
        if position in spelled or not entity_context or entity_context & column_entities:
            qualified.append(position)
            continue
        if set(link_words) == {"name"}:
            context_tables = {
                table for table in schema.tables
                if set(map(canon, name_words(table))) <= entity_context
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
    if isinstance(expression, (Aggregate, DatePart)):
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
