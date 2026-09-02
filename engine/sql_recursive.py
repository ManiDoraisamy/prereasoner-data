"""Recursive-query expansion for the deterministic SQL planner.

The expander adds a bounded set of recursive candidates to the base search pool.
Each rule is grammar-driven and inspectable; no SQL text is decoded or repaired.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
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
    SetQuery,
    Star,
    SubquerySource,
    and_predicates,
    render_query,
)
from engine.numeric import parse_decimal
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import ForeignKey, SchemaGraph


_NEGATIVE_RE = re.compile(
    r"\b(?:without|never|except|excluding|no|not\s+have|do\s+not|does\s+not|did\s+not)\b",
    re.I,
)
_STOP_VALUES = frozenset({
    "a", "an", "and", "any", "as", "at", "by", "for", "from", "in", "no", "not",
    "of", "on", "or", "the", "to", "with", "without",
})
_SOURCE_WORDS = frozenset({"source", "origin", "depart", "departure", "from"})
_DESTINATION_WORDS = frozenset({"dest", "destination", "arrival", "arrive", "to"})


class RecursiveQueryExpander:
    """Generate recursive AST candidates from high-confidence structural cues."""

    def __init__(self, schema: SchemaGraph, max_candidates: int = 160):
        self.schema = schema
        self.max_candidates = max(1, max_candidates)

    def expand(self, question: str, base: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        generated: list[ScoredQuery] = []
        generated.extend(self._set_candidates(question, base))
        generated.extend(self._anti_membership_candidates(question, base))
        generated.extend(self._scalar_average_candidates(question, base))
        generated.extend(self._superlative_subquery_candidates(question))
        generated.extend(self._nested_count_candidates(question))
        generated.extend(self._self_join_candidates(question))

        dedup: dict[str, ScoredQuery] = {}
        for candidate in generated:
            old = dedup.get(candidate.sql)
            if old is None or candidate.score > old.score:
                dedup[candidate.sql] = candidate
        return sorted(dedup.values(), key=lambda candidate: (-candidate.score, candidate.sql))[
            :self.max_candidates
        ]

    def _set_candidates(self, question: str, base: Sequence[ScoredQuery]) -> list[ScoredQuery]:
        out = []
        tokens = _tokens(question)
        for candidate in base:
            query = candidate.query
            if not isinstance(query, SelectQuery) or query.where is None or query.having is not None:
                continue
            terms = list(_and_terms(query.where))
            groups: dict[tuple[str, str], list[Comparison]] = defaultdict(list)
            for term in terms:
                if (isinstance(term, Comparison) and isinstance(term.left, ColumnRef)
                        and isinstance(term.right, Literal)):
                    groups[(term.left.table, term.left.name)].append(term)
            repeated_groups = [
                distinct for group in groups.values()
                if len(distinct := _distinct_comparisons(group)) >= 2
            ]
            if len(repeated_groups) > 1 and set(tokens) & {"or", "either"}:
                continue
            for comparisons in groups.values():
                comparisons = _distinct_comparisons(comparisons)
                if len(comparisons) < 2:
                    continue
                pair = _best_conflicting_pair(comparisons, question)
                if pair is None:
                    continue
                operator = _set_operator(tokens, pair)
                if operator is None:
                    continue
                predicate_column = pair[0].left
                if not isinstance(predicate_column, ColumnRef):
                    continue
                alternatives = (
                    comparisons
                    if len(comparisons) > 2
                    and all(comparison.operator == "=" for comparison in comparisons)
                    else list(pair)
                )
                common = [
                    term for term in terms
                    if term not in alternatives and not _linker_noise(term)
                ]

                aggregate_items = tuple(
                    item for item in query.select if isinstance(item.expression, Aggregate)
                )
                if aggregate_items:
                    entity_table = self._entity_table(question, query, predicate_column.table)
                    if entity_table is None:
                        continue
                    displays = self.schema.display_columns(entity_table)
                    if not displays:
                        continue
                    branch_select_options = [((SelectItem(displays[0]),), 0.0)]
                else:
                    branch_select_options = [(query.select, 0.0)]
                    entity_table = self._entity_table(question, query, predicate_column.table)
                    if entity_table is not None:
                        for column, overlap in self._projection_columns(question, entity_table):
                            branch_select_options.append(((SelectItem(column),), float(overlap)))

                seen_selects = set()
                for branch_select, projection_bonus in branch_select_options:
                    if branch_select in seen_selects:
                        continue
                    seen_selects.add(branch_select)
                    branches = []
                    for comparison in alternatives:
                        branches.append(replace(
                            query,
                            select=branch_select,
                            where=and_predicates(common + [comparison]),
                            group_by=(),
                            having=None,
                            order_by=(),
                            limit=None,
                            distinct=False,
                        ))
                    compound: Query = branches[0]
                    for branch in branches[1:]:
                        compound = SetQuery(compound, operator, branch)
                    if aggregate_items:
                        result: Query = SelectQuery(
                            (SelectItem(Aggregate("COUNT", Star())),),
                            SubquerySource(compound, "matches"),
                        )
                    else:
                        result = compound
                    built = _candidate(
                        result,
                        candidate.score + (16.0 if operator == "INTERSECT" else 13.0)
                        + projection_bonus,
                        candidate.evidence + (f"recursive:set:{operator.lower()}",),
                    )
                    if built is not None:
                        out.append(built)
        return out

    def _anti_membership_candidates(
        self, question: str, base: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        if not _NEGATIVE_RE.search(question):
            return []
        out = []
        for candidate in base:
            query = candidate.query
            if not isinstance(query, SelectQuery) or not query.joins or query.having is not None:
                continue
            entity_table = self._entity_table(question, query)
            if entity_table is None:
                continue
            entity_select = _entity_select(query.select, entity_table)
            if not entity_select:
                displays = self.schema.display_columns(entity_table)
                if not displays:
                    continue
                entity_select = (SelectItem(displays[0]),)
            entity_key = _entity_join_key(query, entity_table)
            if entity_key is None:
                continue

            positive_where = _positive_predicate(query.where)
            membership = replace(
                query,
                select=(SelectItem(entity_key),),
                where=positive_where,
                group_by=(),
                having=None,
                order_by=(),
                limit=None,
                distinct=False,
            )
            outer = SelectQuery(
                select=entity_select,
                from_table=entity_table,
                where=InPredicate(entity_key, membership, negated=True),
                distinct=query.distinct,
            )
            built = _candidate(
                outer,
                candidate.score + 16.0,
                candidate.evidence + ("recursive:not-in",),
            )
            if built is not None:
                out.append(built)

            if not any(isinstance(item.expression, (Aggregate, Star)) for item in entity_select):
                right = replace(membership, select=entity_select)
                difference = SetQuery(
                    SelectQuery(entity_select, entity_table, distinct=query.distinct),
                    "EXCEPT",
                    right,
                )
                built = _candidate(
                    difference,
                    candidate.score + 7.0,
                    candidate.evidence + ("recursive:set:except",),
                )
                if built is not None:
                    out.append(built)
        return out

    def _scalar_average_candidates(
        self, question: str, base: Sequence[ScoredQuery]
    ) -> list[ScoredQuery]:
        tokens = set(_tokens(question))
        if not tokens & {"average", "avg", "mean"}:
            return []
        normalized = " ".join(_tokens(question))
        if re.search(
            r"\b(?:older|above|greater|higher|more)\s+(?:than\s+)?(?:the\s+)?(?:average|avg|mean)\b",
            normalized,
        ):
            operator = ">"
        elif re.search(
            r"\b(?:younger|below|less|lower|fewer)\s+(?:than\s+)?(?:the\s+)?(?:average|avg|mean)\b",
            normalized,
        ):
            operator = "<"
        else:
            return []

        out = []
        for candidate in base:
            query = candidate.query
            if not isinstance(query, SelectQuery):
                continue
            averages = [
                item.expression for item in query.select
                if isinstance(item.expression, Aggregate)
                and item.expression.function == "AVG"
                and isinstance(item.expression.operand, ColumnRef)
            ]
            for average in averages:
                target = average.operand
                inner = SelectQuery((SelectItem(average),), target.table)
                projection_columns = [
                    item.expression for item in query.select
                    if isinstance(item.expression, ColumnRef)
                    and item.expression.table == target.table
                    and item.expression != target
                ]
                for projection in sorted(
                    set(projection_columns),
                    key=lambda column: (-_column_question_overlap(column, question), column.name),
                )[:4]:
                    outer = SelectQuery(
                        (SelectItem(projection),),
                        target.table,
                        where=Comparison(target, operator, ScalarSubquery(inner)),
                    )
                    built = _candidate(
                        outer,
                        candidate.score + 15.0 + _column_question_overlap(projection, question),
                        candidate.evidence + (f"recursive:scalar-avg:{operator}",),
                    )
                    if built is not None:
                        out.append(built)
        return out

    def _superlative_subquery_candidates(self, question: str) -> list[ScoredQuery]:
        tokens = set(_tokens(question))
        if tokens & {"highest", "largest", "biggest", "maximum", "max", "latest", "newest"}:
            direction = "DESC"
        elif tokens & {"lowest", "smallest", "minimum", "min", "earliest", "oldest"}:
            direction = "ASC"
        else:
            return []

        targets = [
            column.ref for column in self.schema.columns
            if column.ref.type.numeric
            and not _is_id(column.ref.name)
            and set(_name_tokens(column.ref.name)) <= tokens
        ]
        count_requested = bool(tokens & {"count", "number"}) or "how many" in " ".join(_tokens(question))
        out = []
        for target in targets:
            for fk in self.schema.foreign_keys:
                if fk.is_composite:
                    continue
                if target.table == fk.from_column.table:
                    lookup_key, outer_key = fk.from_column, fk.to_column
                elif target.table == fk.to_column.table:
                    lookup_key, outer_key = fk.to_column, fk.from_column
                else:
                    continue
                if outer_key.table == target.table:
                    continue
                inner = SelectQuery(
                    (SelectItem(lookup_key),),
                    target.table,
                    order_by=(OrderTerm(target, direction),),
                    limit=1,
                )
                if count_requested:
                    select = (SelectItem(Aggregate("COUNT", Star())),)
                else:
                    displays = self.schema.display_columns(outer_key.table)
                    if not displays:
                        continue
                    select = (SelectItem(displays[0]),)
                outer = SelectQuery(
                    select,
                    outer_key.table,
                    where=Comparison(outer_key, "=", ScalarSubquery(inner)),
                )
                built = _candidate(
                    outer,
                    36.0 + _column_question_overlap(target, question),
                    (f"recursive:scalar-superlative:{direction.lower()}",),
                )
                if built is not None:
                    out.append(built)
        return out

    def _nested_count_candidates(self, question: str) -> list[ScoredQuery]:
        tokens = _tokens(question)
        token_set = set(tokens)
        if not token_set & {"count", "number"}:
            return []
        outer_function = None
        if token_set & {"average", "avg", "mean"}:
            outer_function = "AVG"
        elif token_set & {"maximum", "max"}:
            outer_function = "MAX"
        elif token_set & {"minimum", "min"}:
            outer_function = "MIN"
        if outer_function is None:
            return []
        group_position = next((i for i, token in enumerate(tokens) if token in {"per", "each"}), None)
        if group_position is None:
            return []

        mentions = _table_mentions(self.schema, tokens)
        group_tables = [table for position, table in mentions if position > group_position]
        counted_tables = [table for position, table in mentions if position < group_position]
        if not group_tables or not counted_tables:
            return []

        out = []
        for group_table in dict.fromkeys(group_tables):
            for counted_table in dict.fromkeys(reversed(counted_tables)):
                if group_table == counted_table:
                    continue
                trees = self.schema.join_trees(
                    {group_table, counted_table}, preferred_root=group_table, limit=3
                )
                for tree in trees:
                    group_key = _tree_key(tree.joins, group_table)
                    counted_key = _tree_key(tree.joins, counted_table)
                    if group_key is None:
                        displays = self.schema.display_columns(group_table)
                        group_key = displays[0] if displays else None
                    if group_key is None or counted_key is None:
                        continue
                    joins = tuple(replace(join, kind="LEFT") for join in tree.joins)
                    inner = SelectQuery(
                        (
                            SelectItem(group_key),
                            SelectItem(Aggregate("COUNT", counted_key), "value_count"),
                        ),
                        tree.root,
                        joins=joins,
                        group_by=(group_key,),
                    )
                    count_column = ColumnRef("counts", "value_count", SQLType.INTEGER)
                    outer = SelectQuery(
                        (SelectItem(Aggregate(outer_function, count_column)),),
                        SubquerySource(inner, "counts"),
                    )
                    built = _candidate(
                        outer,
                        38.0 - 0.2 * len(joins),
                        (f"recursive:nested:{outer_function.lower()}-count",),
                    )
                    if built is not None:
                        out.append(built)
        return out

    def _self_join_candidates(self, question: str) -> list[ScoredQuery]:
        out = []
        values = _value_mentions(self.schema, question)
        grouped_fks: dict[tuple[str, str], list[ForeignKey]] = defaultdict(list)
        for fk in self.schema.foreign_keys:
            if fk.is_composite:
                continue
            grouped_fks[(fk.from_column.table, fk.to_column.table)].append(fk)

        tokens = _tokens(question)
        token_set = set(tokens)
        for (child_table, parent_table), fks in grouped_fks.items():
            if len(fks) < 2:
                continue
            parent_values: dict[tuple[str, str], list[tuple[int, ColumnRef, object]]] = defaultdict(list)
            for position, column, value in values:
                if column.table == parent_table:
                    parent_values[(column.table, column.name)].append((position, column, value))

            source_fk = next((fk for fk in fks if _fk_role(fk) == "source"), None)
            destination_fk = next((fk for fk in fks if _fk_role(fk) == "destination"), None)
            if source_fk is not None and destination_fk is not None:
                for mentions in parent_values.values():
                    distinct = _distinct_value_mentions(mentions)
                    if len(distinct) < 2:
                        continue
                    assignments = _assign_route_values(tokens, distinct[:2])
                    if assignments is None:
                        continue
                    source_value, destination_value = assignments
                    query = _route_self_join(
                        child_table,
                        parent_table,
                        source_fk,
                        destination_fk,
                        source_value,
                        destination_value,
                        count_requested=bool(token_set & {"count", "number"})
                        or "how many" in " ".join(tokens),
                        schema=self.schema,
                    )
                    built = _candidate(query, 46.0, ("recursive:self-join:route",))
                    if built is not None:
                        out.append(built)

            relationship_fk = next(
                (fk for fk in fks if set(_name_tokens(fk.from_column.name)) & token_set), None
            )
            if relationship_fk is None:
                continue
            source_options = [fk for fk in fks if fk != relationship_fk]
            if not source_options:
                continue
            source_fk = source_options[0]
            for mentions in parent_values.values():
                distinct = _distinct_value_mentions(mentions)
                if not distinct:
                    continue
                value = distinct[0]
                display = self.schema.display_columns(parent_table)
                if not display:
                    continue
                query = _relationship_self_join(
                    child_table,
                    parent_table,
                    source_fk,
                    relationship_fk,
                    value,
                    display[0],
                )
                built = _candidate(query, 44.0, ("recursive:self-join:relationship",))
                if built is not None:
                    out.append(built)
        return out

    def _entity_table(
        self, question: str, query: SelectQuery, excluded_table: str | None = None
    ) -> str | None:
        physical = []
        if isinstance(query.from_table, str):
            physical.append(query.from_table)
        physical.extend(join.table for join in query.joins)
        physical = list(dict.fromkeys(table for table in physical if table != excluded_table))
        if not physical:
            return None

        negative = _NEGATIVE_RE.search(question)
        negative_position = negative.start() if negative else len(question)
        scored = []
        for table in physical:
            mention = _text_table_position(question, table)
            selected = sum(1 for item in query.select if _expression_table(item.expression) == table)
            before_negative = 1 if mention is not None and mention < negative_position else 0
            root = 1 if isinstance(query.from_table, str) and query.from_table == table else 0
            mention_rank = -(mention if mention is not None else 10_000)
            scored.append((before_negative, mention_rank, selected, root, table))
        return max(scored)[-1]

    def _projection_columns(self, question: str, table: str) -> list[tuple[ColumnRef, int]]:
        options = []
        question_tokens = set(_tokens(question))
        for schema_column in self.schema.by_table.get(table, ()):
            column = schema_column.ref
            if _is_id(column.name):
                continue
            overlap = len(set(_semantic_name_tokens(column.name)) & question_tokens)
            if overlap:
                options.append((column, overlap))
        return sorted(options, key=lambda item: (-item[1], item[0].name))[:3]


def _candidate(query: Query, score: float, evidence: tuple[str, ...]) -> ScoredQuery | None:
    try:
        sql = render_query(query)
    except (TypeError, ValueError):
        return None
    return ScoredQuery(query, sql, score, evidence)


def _and_terms(predicate: Predicate) -> tuple[Predicate, ...]:
    if isinstance(predicate, BooleanExpr) and predicate.operator == "AND":
        return tuple(term for child in predicate.terms for term in _and_terms(child))
    return (predicate,)


def _distinct_comparisons(comparisons: Iterable[Comparison]) -> list[Comparison]:
    out = []
    seen = set()
    for comparison in comparisons:
        key = (comparison.operator, repr(comparison.right))
        if key not in seen:
            seen.add(key)
            out.append(comparison)
    return out


def _best_conflicting_pair(
    comparisons: Sequence[Comparison], question: str
) -> tuple[Comparison, Comparison] | None:
    ordered = sorted(
        comparisons,
        key=lambda comparison: _literal_position(question, comparison.right),
    )
    for index, left in enumerate(ordered):
        for right in ordered[index + 1:]:
            if left.right == right.right:
                continue
            if left.operator == right.operator == "=":
                return left, right
            if _ranges_conflict(left, right):
                return left, right
    return None


def _ranges_conflict(left: Comparison, right: Comparison) -> bool:
    if not isinstance(left.right, Literal) or not isinstance(right.right, Literal):
        return False
    try:
        left_value = parse_decimal(left.right.value, enforce_input_bounds=False)
        right_value = parse_decimal(right.right.value, enforce_input_bounds=False)
    except (TypeError, ValueError):
        return False
    lower = []
    upper = []
    for comparison, value in ((left, left_value), (right, right_value)):
        if comparison.operator in {">", ">="}:
            lower.append(value)
        elif comparison.operator in {"<", "<="}:
            upper.append(value)
    return bool(lower and upper and max(lower) >= min(upper))


def _set_operator(
    tokens: tuple[str, ...], pair: tuple[Comparison, Comparison]
) -> str | None:
    token_set = set(tokens)
    if "either" in token_set or ("or" in token_set and "both" not in token_set):
        return "UNION"
    if "both" in token_set:
        return "INTERSECT"
    if "and" in token_set and _ranges_conflict(*pair):
        return "INTERSECT"
    return None


def _linker_noise(predicate: Predicate) -> bool:
    return (
        isinstance(predicate, Comparison)
        and isinstance(predicate.right, Literal)
        and str(predicate.right.value).strip().lower() in _STOP_VALUES
    )


def _entity_select(select: tuple[SelectItem, ...], table: str) -> tuple[SelectItem, ...]:
    out = []
    for item in select:
        expression = item.expression
        if isinstance(expression, Star):
            out.append(item)
        elif isinstance(expression, ColumnRef) and expression.table == table:
            out.append(item)
        elif (isinstance(expression, Aggregate) and isinstance(expression.operand, ColumnRef)
              and expression.operand.table == table):
            out.append(item)
    return tuple(out)


def _entity_join_key(query: SelectQuery, table: str) -> ColumnRef | None:
    options = []
    for join in query.joins:
        if len(join.predicates) != 1:
            continue
        for column in join.predicates[0]:
            if column.table == table:
                options.append(column)
    if not options:
        return None
    return sorted(set(options), key=lambda column: (0 if _is_id(column.name) else 1, column.name))[0]


def _positive_predicate(predicate: Predicate | None) -> Predicate | None:
    if predicate is None:
        return None
    if isinstance(predicate, BooleanExpr):
        return BooleanExpr(predicate.operator, tuple(_positive_predicate(term) for term in predicate.terms))
    if isinstance(predicate, Comparison) and predicate.operator in {"!=", "<>"}:
        return Comparison(predicate.left, "=", predicate.right)
    return predicate


def _expression_table(expression) -> str | None:
    if isinstance(expression, ColumnRef):
        return expression.table
    if isinstance(expression, Aggregate) and isinstance(expression.operand, ColumnRef):
        return expression.operand.table
    return None


def _column_question_overlap(column: ColumnRef, question: str) -> int:
    return len(set(_semantic_name_tokens(column.name)) & set(_tokens(question)))


def _tree_key(joins: tuple[Join, ...], table: str) -> ColumnRef | None:
    options = [
        column for join in joins if len(join.predicates) == 1 for column in join.predicates[0]
        if column.table == table
    ]
    return sorted(set(options), key=lambda column: (0 if _is_id(column.name) else 1, column.name))[0] if options else None


def _table_mentions(schema: SchemaGraph, tokens: tuple[str, ...]) -> list[tuple[int, str]]:
    out = []
    for table in schema.tables:
        words = _name_tokens(table)
        for index in range(len(tokens) - len(words) + 1):
            if tokens[index:index + len(words)] == words:
                out.append((index, table))
                break
    return sorted(out)


def _value_mentions(schema: SchemaGraph, question: str) -> list[tuple[int, ColumnRef, object]]:
    tokens = _tokens(question)
    matches = []
    for phrase, options in schema.value_index.items():
        words = _tokens(phrase)
        if not words or set(words) <= _STOP_VALUES:
            continue
        for index in range(len(tokens) - len(words) + 1):
            if tokens[index:index + len(words)] != words:
                continue
            for column, value in options:
                matches.append((index, column, value, len(words)))
    matches.sort(key=lambda item: (-item[3], item[0], item[1].table, item[1].name))
    occupied = set()
    selected = []
    for position, column, value, length in matches:
        key = (position, column.table, column.name, str(value))
        if key in occupied:
            continue
        occupied.add(key)
        selected.append((position, column, value))
    return sorted(selected, key=lambda item: (item[0], item[1].table, item[1].name))


def _distinct_value_mentions(
    mentions: Sequence[tuple[int, ColumnRef, object]]
) -> list[tuple[int, ColumnRef, object]]:
    out = []
    seen = set()
    for mention in sorted(mentions):
        key = str(mention[2]).strip().lower()
        if key not in seen:
            seen.add(key)
            out.append(mention)
    return out


def _fk_role(fk: ForeignKey) -> str | None:
    words = set(_name_tokens(fk.from_column.name))
    if words & _SOURCE_WORDS:
        return "source"
    if words & _DESTINATION_WORDS:
        return "destination"
    return None


def _assign_route_values(
    tokens: tuple[str, ...], mentions: Sequence[tuple[int, ColumnRef, object]]
) -> tuple[tuple[int, ColumnRef, object], tuple[int, ColumnRef, object]] | None:
    source = destination = None
    for mention in mentions:
        position = mention[0]
        context = set(tokens[max(0, position - 4):position])
        if context & _SOURCE_WORDS:
            source = mention
        if context & _DESTINATION_WORDS:
            destination = mention
    if source is None:
        source = mentions[0]
    if destination is None:
        destination = next((mention for mention in mentions if mention != source), None)
    if destination is None or destination == source:
        return None
    return source, destination


def _aliased_additional(
    foreign_key: ForeignKey, base_alias: str, other_alias: str
) -> tuple[tuple[ColumnRef, ColumnRef], ...]:
    """Re-alias a (possibly composite) FK's SECONDARY column pairs onto the self-join aliases,
    mirroring the primary pair, so a composite FK renders a complete ``ON a=b AND c=d`` rather
    than a silent partial join. Empty for scalar FKs, so the SQL is byte-identical on the
    reachable (non-composite) path — the ``is_composite`` guard in ``_self_join_candidates``
    keeps composite FKs out today; this makes the helper correct if that ever changes."""
    return tuple(
        (ColumnRef(base_alias, child.name, child.type),
         ColumnRef(other_alias, parent.name, parent.type))
        for child, parent in foreign_key.additional_columns
    )


def _route_self_join(
    child_table: str,
    parent_table: str,
    source_fk: ForeignKey,
    destination_fk: ForeignKey,
    source_value: tuple[int, ColumnRef, object],
    destination_value: tuple[int, ColumnRef, object],
    count_requested: bool,
    schema: SchemaGraph,
) -> SelectQuery:
    base_alias, source_alias, destination_alias = "base", "source", "destination"
    joins = (
        Join(
            parent_table,
            ColumnRef(base_alias, source_fk.from_column.name, source_fk.from_column.type),
            ColumnRef(source_alias, source_fk.to_column.name, source_fk.to_column.type),
            alias=source_alias,
            additional=_aliased_additional(source_fk, base_alias, source_alias),
        ),
        Join(
            parent_table,
            ColumnRef(base_alias, destination_fk.from_column.name, destination_fk.from_column.type),
            ColumnRef(destination_alias, destination_fk.to_column.name, destination_fk.to_column.type),
            alias=destination_alias,
            additional=_aliased_additional(destination_fk, base_alias, destination_alias),
        ),
    )
    source_column = ColumnRef(source_alias, source_value[1].name, source_value[1].type)
    destination_column = ColumnRef(destination_alias, destination_value[1].name, destination_value[1].type)
    where = and_predicates((
        Comparison(source_column, "=", Literal(source_value[2], source_value[1].type)),
        Comparison(destination_column, "=", Literal(destination_value[2], destination_value[1].type)),
    ))
    if count_requested:
        select = (SelectItem(Aggregate("COUNT", Star())),)
    else:
        displays = schema.display_columns(child_table)
        display = displays[0] if displays else source_fk.from_column
        select = (SelectItem(ColumnRef(base_alias, display.name, display.type)),)
    return SelectQuery(select, child_table, joins=joins, where=where, from_alias=base_alias)


def _relationship_self_join(
    child_table: str,
    parent_table: str,
    source_fk: ForeignKey,
    relationship_fk: ForeignKey,
    value: tuple[int, ColumnRef, object],
    display: ColumnRef,
) -> SelectQuery:
    base_alias, source_alias, target_alias = "relation", "owner", "related"
    joins = (
        Join(
            parent_table,
            ColumnRef(base_alias, source_fk.from_column.name, source_fk.from_column.type),
            ColumnRef(source_alias, source_fk.to_column.name, source_fk.to_column.type),
            alias=source_alias,
            additional=_aliased_additional(source_fk, base_alias, source_alias),
        ),
        Join(
            parent_table,
            ColumnRef(base_alias, relationship_fk.from_column.name, relationship_fk.from_column.type),
            ColumnRef(target_alias, relationship_fk.to_column.name, relationship_fk.to_column.type),
            alias=target_alias,
            additional=_aliased_additional(relationship_fk, base_alias, target_alias),
        ),
    )
    selected = ColumnRef(target_alias, display.name, display.type)
    filtered = ColumnRef(source_alias, value[1].name, value[1].type)
    return SelectQuery(
        (SelectItem(selected),),
        child_table,
        joins=joins,
        where=Comparison(filtered, "=", Literal(value[2], value[1].type)),
        from_alias=base_alias,
    )


def _literal_position(question: str, expression) -> int:
    if not isinstance(expression, Literal):
        return 10_000
    position = question.lower().find(str(expression.value).strip().lower())
    return position if position >= 0 else 10_000


def _text_table_position(question: str, table: str) -> int | None:
    question_tokens = _tokens(question)
    words = _name_tokens(table)
    for index in range(len(question_tokens) - len(words) + 1):
        if question_tokens[index:index + len(words)] == words:
            return index
    return None


def _is_id(name: str) -> bool:
    words = _name_tokens(name)
    return bool(words) and words[-1] in {"id", "identifier", "key", "code"}


def _name_tokens(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(_canon(token) for token in re.findall(r"[A-Za-z0-9]+", spaced))


def _semantic_name_tokens(name: str) -> tuple[str, ...]:
    compact = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if compact in {"fname", "firstname"}:
        return ("first", "name")
    if compact in {"lname", "lastname"}:
        return ("last", "name")
    return _name_tokens(name)


def _tokens(text: str) -> tuple[str, ...]:
    out = []
    for token in re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", text.lower()):
        if token.endswith("'s"):
            token = token[:-2]
        out.append(_canon(token))
    return tuple(out)


def _canon(word: str) -> str:
    word = word.lower().strip()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("ses"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
