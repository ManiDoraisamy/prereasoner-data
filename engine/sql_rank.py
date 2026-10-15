"""Candidate scoring for the own-data SQL planner.

``CandidateRanker`` orders the typed-AST search pool with hand-written structural rules and encoder
similarities (engine/sql_search.py applies it). Every adjustment is a named feature, so model
similarity can improve ordering without hiding why a candidate won. ``PoolSelection`` records the
served choice: the best-ranked candidate that executes and is grounded, under the pool contract
(``SEARCH_CANDIDATES``, ``execution_op_limit``). No model writes or scores SQL here; the labelled
Gemini fallback (engine/question_rewrite.py) is recorded as ``FallbackRecord`` when it took part.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Mapping, Sequence

from engine.sql_ast import (
    Aggregate, ColumnRef, Comparison, DatePart, Literal, Query, SelectQuery, SetQuery, Star, keep_ties, render_query,
    share_aggregate,
)
from engine.sql_dates import period_grouping
from engine.sql_extrema import MAX_CUES, MIN_CUES
from engine.sql_expansion import (
    FUNCTION_WORDS, OutputRequest, asked_cues, by_groups, counted_noun_tables, field_numbers, output_requests,
    request_fields, share_cue, share_requested, words,
)
from engine.sql_expansion import tokens as expansion_tokens
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import SURROGATE_KEY_WORDS, SchemaGraph, canon, is_name_column, is_surrogate_key, name_words


ColumnKey = tuple[str, str]


@dataclass(frozen=True)
class SemanticSignals:
    """Fixed encoder similarities, keyed by semantic role and schema object."""

    column_roles: Mapping[str, Mapping[ColumnKey, float]]
    table_global: Mapping[str, float]
    calculation_intents: Mapping[str, float] = field(default_factory=dict)
    calculation_operands: Mapping[str, Mapping[ColumnKey, float]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "SemanticSignals":
        return cls({}, {}, {}, {})


@dataclass(frozen=True)
class QuestionRoles:
    question: str
    tokens: tuple[str, ...]
    aggregate_positions: Mapping[str, tuple[int, ...]]
    count_requested: bool
    distinct_requested: bool
    group_requested: bool
    # The question asks for one row per month of a date column (sql_dates.period_grouping).
    group_period: bool
    group_tables: frozenset[str]
    group_columns: frozenset[ColumnRef]
    counted_tables: frozenset[str]
    counted_heads: frozenset[str]   # the tables the noun right after the count word names: "how many countries"
    output_requests: tuple[OutputRequest, ...]   # an explicit list instruction's outputs, in order
    requested_fields: tuple[tuple[ColumnRef, ...], ...]   # the fields realizing each requested field (request_fields)
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
        self.column_words = frozenset(
            token for column in schema.columns for token in _schema_tokens(column.ref.name))
        self.relations = relation_sides(schema)

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
                *((f"left:{name}", 0.5 * value) for name, value in left),
                *((f"right:{name}", 0.5 * value) for name, value in right),
            )
        select_columns = tuple(item.expression for item in query.select if isinstance(item.expression, ColumnRef))
        # A share (sql_ast.share_of) answers with the aggregate it divides, as the total it shares out.
        shares = tuple(part for item in query.select
                       if (part := share_aggregate(item.expression, query)) is not None)
        aggregates = tuple(item.expression for item in query.select
                           if isinstance(item.expression, Aggregate)) + shares
        group_columns = query.group_by
        count_ranked = any(
            isinstance(term.expression, Aggregate) and term.expression.function == "COUNT"
            for term in query.order_by
        )
        features: list[tuple[str, float]] = [("base", 0.0)]
        asked = share_requested(roles.tokens, self.column_words)
        if shares or asked:
            # "what share of the total amount comes from Paris" asks for the fraction, not the total.
            features.append(("share_of_whole", 6.0 if shares and asked else -6.0))

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
                and (is_surrogate_key(aggregate.operand.name) or is_name_column(aggregate.operand.name))
            ]
            features.append(("count_distinct_entity", 4.0 if distinct_identities else 0.0))

        shown_columns = [column for column in (*select_columns, *group_columns) if isinstance(column, ColumnRef)]
        requested = {ref for refs in roles.requested_fields for ref in refs}
        if roles.output_requests:
            features.extend(self._output_request_features(select_columns, aggregates, roles))

        for column in group_columns:
            if (isinstance(column, ColumnRef) and is_surrogate_key(column.name) and column not in select_columns
                    and any(isinstance(other, ColumnRef) and other.table == column.table and other != column
                            for other in group_columns)):
                continue    # an unlisted key beside its table's other groups tells their rows apart (entity_groups)
            alignment = column in requested or (self._group_alignment(column, roles) and not (
                column not in roles.group_columns and self._named_beside(column, shown_columns, roles))) or (
                count_ranked and any(
                    selected == column or selected.table == column.table
                    for selected in select_columns
                )
            )
            features.append((f"group_role:{_column_label(column)}", 2.5 if alignment else -4.0))

        for column in select_columns:
            if column in roles.projection_columns or column in requested:
                value = 1.0
            elif self._group_alignment(column, roles) and not self._named_beside(column, shown_columns, roles):
                value = 1.5
            elif column.table in roles.counted_tables:
                value = -3.5
            else:
                value = -1.25 if aggregates else 0.0
            features.append((f"projection_role:{_column_label(column)}", value))
            # A field the question names only to filter on is not asked for: "the name and results of the battles when
            # the bulgarian commander is not 'Boril'" showed every row's commander, Boril excluded (Spider DEV 497).
            if filter_only(column, query, roles.tokens):
                features.append((f"filter_only_output:{_column_label(column)}", -8.0))
            # So is one it names only to order the rows by: "the first name of students in the descending order of
            # age" showed each age beside the name (held-out Spider train 736, 2026-10-12).
            if self._order_only(column, query, roles):
                features.append((f"order_only_output:{_column_label(column)}", -8.0))
            # A field named as its table is not asked for where the question names the table only as the owner of
            # other fields: "the carriers of devices" showed device.Device beside each carrier, and "the locations
            # and nicknames of schools" school.School (held-out Spider train 300, 105, 2026-10-12).
            if self._owner_only(column, roles):
                features.append((f"owner_only_output:{_column_label(column)}", -8.0))

        # A superlative over the one field the answer shows ranks by that field: "the age of the oldest dog" is the
        # greatest age, not the age of the earliest birth date (Spider DEV 988, 989, 2026-10-14).
        if (isinstance(query, SelectQuery) and query.limit is not None and len(query.order_by) == 1
                and not query.group_by and len(query.select) == 1 and select_columns
                and query.order_by[0].expression == select_columns[0]):
            features.append(("superlative_on_shown", 0.75))

        # An aggregate the question names only in a clause keeping the groups is not asked for: "the studios whose
        # average gross is above 4500000", "the job ids for those jobs which average salary is above 8000" (held-out
        # Spider train 743, 746, 750, 2026-10-12: each showed the average it kept the groups by); one it also asks
        # for is shown, and so is one it says after "with" ("cities with total sales over 100" shows each total).
        if isinstance(query, SelectQuery) and query.having is not None:
            kept = {comparison.left for comparison in _comparisons(query.having) if isinstance(comparison.left, Aggregate)}
            shown = {item.expression for item in query.select}
            for aggregate in sorted(kept, key=repr):
                said = self._condition_said(aggregate, roles)
                if aggregate in shown and said == "condition":
                    features.append((f"condition_only_output:{aggregate.function}", -8.0))
                elif aggregate not in shown and said == "asked":
                    features.append((f"asked_aggregate_unshown:{aggregate.function}", -8.0))

        # Rows a join repeats are one answer each: "the makers that produced some cars in 1970" listed a maker once per
        # car, and "the template type code for description 'Book'" once per template (Spider DEV 99, 351). Only where
        # a joined table refers to a shown one, or a shown field is itself a reference to another table's key, and
        # only against the answer that repeats them: a bonus for DISTINCT lifted unrelated joined readings over a
        # right one (DEV 324, 962). A ranking keeps its number of rows whatever repeats.
        if not query.distinct and repeats_shown_rows(self.schema, query, roles.tokens):
            features.append(("repeated_rows", -3.0))

        # A field's name the question says once binds one field: "the name of the employee who got the highest one
        # time bonus" is the employee's Name, not also the Name of a shop joined for nothing else (Spider DEV 279,
        # 2026-10-10: the employee's and the shop's names ranked first).
        said = [canon(token) for token in roles.tokens]
        bound: dict[str, set[str]] = {}
        for column in select_columns:
            bound.setdefault(column.name.casefold(), set()).add(column.table)
        for name, tables in sorted(bound.items()):
            head = canon((_schema_tokens(name) or (name,))[-1])
            if len(tables) > 1 and said.count(head) < len(tables):
                features.append((f"one_binding:{name}", -6.0))
        # So does the word several shown fields' names end in, said fewer times than they are shown: "the name and the
        # release year of the song by the youngest singer" is Song_Name, not also the concert_Name of a concert the
        # question never mentions (Spider DEV 6, 7, 2026-10-13). The field whose other words and table it never says is
        # the one not asked.
        heads: dict[str, list[ColumnRef]] = {}
        for column in dict.fromkeys(select_columns):
            name_tokens = _schema_tokens(column.name)
            if name_tokens:
                heads.setdefault(canon(name_tokens[-1]), []).append(column)
        for head, columns in sorted(heads.items()):
            if len({column.name.casefold() for column in columns}) < 2 or said.count(head) >= len(columns):
                continue
            for column in columns:
                owner = set(_schema_tokens(column.name)[:-1]) | set(_schema_tokens(column.table))
                if owner and not owner & set(said):
                    features.append((f"unsaid_owner:{_column_label(column)}", -6.0))

        if roles.id_instead_of_name:
            id_columns = [column for column in select_columns + group_columns if is_surrogate_key(column.name)]
            name_columns = [column for column in select_columns + group_columns if is_name_column(column.name)]
            features.append(("requested_id", 4.0 if id_columns else -3.0))
            features.append(("rejected_name", -6.0 if name_columns else 1.0))

        for aggregate in aggregates:
            if not isinstance(aggregate.operand, ColumnRef):
                # The rows of the table the counted noun names are what it counts: "How many countries are listed?"
                # counts the countries (Spider DEV 91, 92, 2026-10-13: car_makers' Country values were counted).
                if (aggregate.function == "COUNT" and isinstance(aggregate.operand, Star)
                        and query.from_table in roles.counted_heads):
                    features.append(("count_rows:counted_table", 1.5))
                continue
            operand = aggregate.operand
            targets = roles.aggregate_targets.get(aggregate.function, frozenset())
            if aggregate.function == "COUNT":
                aligned = (
                    operand.table in roles.counted_tables
                    if roles.counted_tables
                    else operand in roles.counted_columns
                )
                # A field of another table than the one the counted noun names is not what the question counts: not
                # where the noun only names a table further on ("How many sections does course ACCT-211 have?").
                features.append((f"count_target:{_column_label(operand)}",
                                 1.5 if aligned else -1.5 if roles.counted_heads - {operand.table} else 0.0))
            elif targets:
                features.append((f"aggregate_target:{aggregate.function}:{_column_label(operand)}",
                                 3.0 if operand in targets else -3.5))

        from engine.calculations.registry import calculation_rank_features
        features.extend(calculation_rank_features(roles.question, query, self.schema, self.signals))

        direction = travel_direction(roles.tokens)
        if direction:
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
                column for column in role_columns if travel_column_role(column) is not None
            ]
            if directional_columns:
                aligned = any(
                    travel_column_role(column) == direction
                    for column in directional_columns
                )
                features.append((f"travel_direction:{direction}", 3.0 if aligned else -3.0))

        # A table relating rows of one table to others of it reads from its owner's key: "high schoolers who have 3
        # friends" own the Friend rows (student_id), the friend_id side being the friends (Spider DEV 885-911,
        # 2026-10-10: the two readings tied and the friend's side came first by name). The question saying the
        # other key's participle ("liked by Kyle") asks for that side.
        joined = {frozenset(pair) for join in query.joins for pair in join.predicates}
        for relation, (owner, other, participle) in sorted(self.relations.items()):
            used = [key for key in (owner, other) if frozenset((key.from_column, key.to_column)) in joined]
            if len(used) == 1:
                wanted = other if participle is not None and participle in roles.tokens else owner
                features.append((f"relation_side:{relation}", 0.0 if used[0] == wanted else -1.0))

        features.extend(self._model_features(query))
        return tuple(features)

    @staticmethod
    def _owner_only(column: ColumnRef, roles: QuestionRoles) -> bool:
        """Whether ``column`` is named as its table is (device.Device, school.School) and the question says that name
        only after "of", as the owner of the fields it asks for ("the carriers of devices"). "The names of schools"
        asks for that field: it is the schools' name."""
        name = [canon(word) for word in name_words(column.name)]
        if not name or name != [canon(word) for word in name_words(column.table)]:
            return False
        said = [index for index, token in enumerate(roles.tokens) if token == name[-1]]

        def owner(index):
            start = index - len(name) + 1
            before = [token for token in roles.tokens[max(0, start - 4):max(0, start)]
                      if token not in {"the", "all", "a", "an", "their", "its", "each", "every", "these", "those"}]
            return bool(before) and before[-1] == "of" and (len(before) < 2 or before[-2] not in {"name", "title"})

        return bool(said) and all(owner(index) for index in said)

    def _order_only(self, column: ColumnRef, query: SelectQuery, roles: QuestionRoles) -> bool:
        """Whether ``query`` orders its rows by ``column`` and the question says the field only in the phrase that
        orders them: an ordering word ("order", "sorted", "ranked") and its "of", "by" or "to" before each place it
        names the field ("in the descending order of age", "sorted by their age")."""
        if not any(term.expression == column for term in query.order_by):
            return False
        name = [canon(word) for word in name_words(column.name)]
        said = [index for index, token in enumerate(roles.tokens) if name and token == name[-1]]

        # A place another table's word qualifies names that table's field: "the saving balance of the account with
        # the highest checking balance" names the savings' balance first.
        own = {canon(word) for word in name_words(column.table)}
        others = {canon(word) for table in self.schema.tables if table != column.table
                  for word in name_words(table)} - own
        said = [index for index in said
                if index - len(name) < 0 or roles.tokens[index - len(name)] not in others]

        def ordering(index):
            before = [token for token in roles.tokens[max(0, index - 6):index - len(name) + 1]
                      if token not in {"the", "their", "its", "his", "her", "a"} and token not in own]
            # The field a superlative after "with" picks the row by: "the account with the highest checking balance"
            # and "the stadium with highest average attendance" ask for the account and the stadium (held-out Spider
            # train 504). "Which 2 products have the highest price" shows the prices it ranks by (review F5).
            return (len(before) >= 2 and before[-1] in {"of", "by", "to"}
                    and bool(set(before[-4:-1]) & {"order", "ordered", "sort", "sorted", "rank", "ranked",
                                                   "according"})
                    or len(before) >= 2 and before[-1] in MAX_CUES | MIN_CUES and before[-2] == "with")

        return bool(said) and all(ordering(index) for index in said)

    @staticmethod
    def _condition_said(aggregate: Aggregate, roles: QuestionRoles) -> str | None:
        """How the question says ``aggregate``: "condition" when each word saying it opens a relative clause that
        compares it ("whose average gross is above", "which average salary is above"), "asked" when one does not,
        None when no word says it."""
        cues = _CONDITION_CUES.get(aggregate.function, frozenset())
        said = [index for index, token in enumerate(roles.tokens) if token in cues]
        if not said:
            return None
        return "condition" if all(
            set(roles.tokens[max(0, index - 3):index]) & _RELATIVE_WORDS
            and set(roles.tokens[index + 1:index + 6]) & _CONDITION_COMPARISONS for index in said) else "asked"

    def _output_request_features(self, select_columns: Sequence[ColumnRef], aggregates: Sequence[Aggregate],
                                 roles: QuestionRoles) -> list[tuple[str, float]]:
        """An explicit list instruction names the outputs (sql_expansion.output_requests, request_fields): an answer
        leaving a requested one out, or showing a field no request asks for, is not the answer asked. "List the continent
        id, continent name and the number of countries" shows no car maker's Country, and "List full name and the id"
        of makers shows their Id, not a model's ModelId (Spider DEV 89, 149, 2026-10-14)."""
        features: list[tuple[str, float]] = []
        if any(request.count for request in roles.output_requests) and not any(
                aggregate.function == "COUNT" for aggregate in aggregates):
            features.append(("output_request_missing:count", -4.0))
        if not roles.requested_fields:
            return features
        requested = {ref for refs in roles.requested_fields for ref in refs}
        for refs in roles.requested_fields:
            if not set(refs) & set(select_columns):
                features.append((f"output_request_missing:{_column_label(refs[0])}", -4.0))
        for column in dict.fromkeys(select_columns):
            if column not in requested:
                features.append((f"output_unrequested:{_column_label(column)}", -4.0))
        return features

    def _named_beside(self, column: ColumnRef | DatePart, shown: Sequence[ColumnRef], roles: QuestionRoles) -> bool:
        """Whether ``column``, which the question does not ask to show, stands beside a field it asks to show of the
        same entity (a table and the tables its keys link). The question asks for that entity by the field it names,
        and ``column`` is not asked: "all template ids and the number of documents using each template" shows the
        ids, not their type code too (Spider DEV 309, 367, 33, 2026-10-13: an entity's other field was shown)."""
        if not isinstance(column, ColumnRef) or column in roles.projection_columns:
            return False
        if is_surrogate_key(column.name) and {"id", "ids", "identifier"} & set(roles.tokens):
            return False    # "the full name of each car maker, along with its id" asks for the key (Spider DEV 94)
        entity = self._entity_tables(column)
        return any(other != column and other in roles.projection_columns and self._entity_tables(other) & entity
                   for other in shown)

    def _entity_tables(self, column: ColumnRef) -> set[str]:
        """``column``'s table and the tables its key pairs link it to."""
        tables = {column.table}
        for fk in self.schema.foreign_keys:
            for from_column, to_column in fk.column_pairs:
                if column == from_column:
                    tables.add(to_column.table)
                elif column == to_column:
                    tables.add(from_column.table)
        return tables

    def _group_alignment(self, column: ColumnRef | DatePart, roles: QuestionRoles) -> bool:
        if isinstance(column, DatePart):
            return roles.group_period and column.part == "year_month"
        if column in roles.group_columns or column.table in roles.group_tables:
            return True
        # "Show supplier and total amount" names an output dimension before the
        # measure. "Total amount paid to suppliers" does not. COUNT has its own
        # entity/cardinality interpretation and must not gain implicit groups.
        if not roles.count_requested and column in roles.projection_columns:
            return True
        for fk in self.schema.foreign_keys:
            for from_column, to_column in fk.column_pairs:
                if column == from_column and to_column.table in roles.group_tables:
                    return True
                if column == to_column and from_column.table in roles.group_tables:
                    return True
        return False

    def _model_features(self, query: SelectQuery) -> list[tuple[str, float]]:
        features: list[tuple[str, float]] = []
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


def analyze_question(question: str, schema: SchemaGraph) -> QuestionRoles:
    tokens = _tokens(question)
    aggregate_positions: dict[str, list[int]] = {fn: [] for fn in ("COUNT", "SUM", "AVG", "MIN", "MAX")}
    labels = field_numbers(question, (column.ref for column in schema.columns))
    for i, token in enumerate(tokens):
        if (
            token == "count"
            or (token == "number" and i + 1 < len(tokens)
                and tokens[i + 1] == "of" and i not in labels)
            or (token == "how" and i + 1 < len(tokens) and tokens[i + 1] == "many")
        ):
            aggregate_positions["COUNT"].append(i)
        elif token in AGGREGATE_CUE_WORDS["SUM"]:
            aggregate_positions["SUM"].append(i)
        elif token in AGGREGATE_CUE_WORDS["AVG"]:
            aggregate_positions["AVG"].append(i)
        elif token in {"minimum", "min"}:
            aggregate_positions["MIN"].append(i)
        elif token in {"maximum", "max"}:
            aggregate_positions["MAX"].append(i)
    # A spelled column name's aggregate word asks as the search reads it (sql_expansion.asked_cues): "the total of
    # the avg. monthly searches" asks no average.
    asked = set(asked_cues([(function, position) for function, positions in aggregate_positions.items()
                            for position in positions], tokens, schema))
    aggregate_positions = {function: [position for position in positions if (function, position) in asked]
                           for function, positions in aggregate_positions.items()}

    if not any(aggregate_positions.values()) and (share := share_cue(tokens, schema)):
        aggregate_positions[share[0]].append(share[1])            # the search reads the same cue
    all_aggregate_positions = sorted(position for positions in aggregate_positions.values() for position in positions)
    group_positions = [i for i, token in enumerate(tokens) if token in {"each", "per"}]
    group_positions += [i for i in range(len(tokens) - 1) if tokens[i:i + 2] == ("group", "by")]
    if all_aggregate_positions:
        # "ordered by" sorts and "owned by students" names who acted, but "orders by city" groups the orders
        # (sql_expansion.by_groups, the search's reading).
        raw = words(question)
        group_positions += [i for i, token in enumerate(tokens)
                            if i > all_aggregate_positions[0] and by_groups(tokens, raw, i)]
    group_positions = sorted(set(group_positions))
    period = period_grouping(tokens, schema)

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
    counted_heads = counted_noun_tables(tokens, schema)
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
            if not column.ref.type.numeric and not _heads_phrase(tokens, name_tokens, positions):
                continue
            distance = min((max(hit - position, 0) for position in positions for hit in hits if hit >= position),
                           default=999)
            candidates.append((distance, column.ref))
        if candidates:
            best_distance = min(distance for distance, _ in candidates)
            targets = {column for distance, column in candidates if distance == best_distance}
            # A field named by the aggregate word itself (a stadium's Average) is not what the word asks when its
            # phrase names another field: "the average and maximum capacities" averages Capacity (Spider DEV 17,
            # 2026-10-13).
            cue_named = {ref for _, ref in candidates if set(_schema_tokens(ref.name)) <= _COORDINATED_AGGREGATES}
            headed = {ref for _, ref in candidates
                      if ref not in cue_named and _heads_phrase(tokens, _schema_tokens(ref.name), positions)}
            if targets & cue_named and headed:
                targets = headed
            aggregate_targets[function] = frozenset(targets)

    first_role = min(all_aggregate_positions + group_positions
                     + [i for i, token in enumerate(tokens) if token in clause_stops], default=len(tokens))
    projection_columns = _columns_in_windows(schema, tokens, [(0, first_role)])
    id_instead = ("instead" in tokens and bool({"id", "identifier"} & set(tokens))
                  and bool({"name", "names"} & set(tokens)))
    frozen_positions = {function: tuple(positions) for function, positions in aggregate_positions.items() if positions}
    requests = output_requests(question, schema)
    fields = request_fields(question, schema)
    # A list asking for fields and a count asks for the count in each of those fields' groups: "Show different
    # publishers together with the number of publications they have" (held-out Spider train 532, 2026-10-15). Its
    # "different" before a field says each group once, which grouping does; only one inside the count asks a count of
    # distinct values ("the number of different products").
    listed_groups = bool(fields) and any(request.count for request in requests)
    distinct_words = {"distinct", "different", "unique"}
    said = expansion_tokens(question)
    distinct = (any(said[position] in distinct_words for request in requests if request.count
                    for position in range(request.start, request.end)) if listed_groups
                else bool(distinct_words & set(tokens)))
    return QuestionRoles(
        question=question,
        tokens=tokens,
        aggregate_positions=frozen_positions,
        count_requested=bool(aggregate_positions["COUNT"]),
        distinct_requested=distinct,
        group_requested=bool(group_positions) or period is not None or listed_groups,
        group_period=period is not None,
        group_tables=frozenset(group_tables),
        group_columns=frozenset(group_columns),
        counted_tables=frozenset(counted_tables),
        counted_heads=frozenset(counted_heads),
        output_requests=requests,
        requested_fields=fields,
        counted_columns=frozenset(counted_columns),
        aggregate_targets=aggregate_targets,
        projection_columns=frozenset(projection_columns),
        id_instead_of_name=id_instead,
    )


# Words that end a noun phrase: grammar words, and the words that open a clause, a comparison, a grouping or an
# ordering. Any other word after a column's name continues the phrase that name begins. "column" and "field" end
# it too: "the Name column" is the Name.
_PHRASE_BREAKS = frozenset(canon(word) for word in FUNCTION_WORDS | {
    "all", "any", "each", "every", "per", "group", "these", "those", "some", "no", "not", "nor", "but",
    "who", "whom", "whose", "which", "where", "when", "why", "how", "what", "there",
    "has", "have", "had", "do", "does", "did", "can", "could", "will", "would", "should", "shall", "may", "might",
    "having", "ordered", "sort", "sorted", "top", "bottom", "after", "before", "between", "during",
    "since", "until", "within", "without", "across", "over", "under", "above", "below", "among", "through",
    "versus", "vs", "more", "less", "fewer", "greater", "than", "excluding", "except", "including",
    "containing", "use", "using", "used", "column", "field",
})


def _ends_phrase(tokens: tuple[str, ...], index: int) -> bool:
    """Whether ``tokens[index]`` ends the noun phrase it follows: a word of ``_PHRASE_BREAKS``, or "order" opening an
    ordering ("order by"). Before another noun "order" is part of the phrase: "the total order amount" totals the
    amount (planted-text review, 2026-10-08)."""
    word = tokens[index]
    if word == "order":
        return index + 1 < len(tokens) and tokens[index + 1] == "by"
    return word in _PHRASE_BREAKS
# Words an aggregate's own phrase may open with: "the total of the Amount".
_PHRASE_OPENERS = frozenset({"of", "the", "a", "an"})


def _heads_phrase(tokens: tuple[str, ...], name_tokens: tuple[str, ...], positions: Sequence[int]) -> bool:
    """Whether a column's name ends the phrase an aggregate word at one of ``positions`` begins: "the total
    Amount", "the total of the Amount". A text column named elsewhere is no aggregate's operand: before another
    word of the phrase it only says whose ("total keyword volume" totals a volume, "average student age"
    averages ages), and after "for" it names the rows ("...for all inspection checklist keywords"). A
    customer's keyword sheet (2026-10-06): every reading of "total keyword volume for all inspection checklist
    keywords" was refused for not totaling Keyword."""
    for position in positions:
        start = position + 1
        # Aggregate words coordinated after it share its field: "the average, minimum, and maximum age" averages Age,
        # whatever field is named Average (Spider DEV 4, 17, 2026-10-13: refused as no average of stadium.Average).
        while start < len(tokens) and (tokens[start] in _PHRASE_OPENERS or tokens[start] in _COORDINATED_AGGREGATES
                                       or tokens[start] in {"and", "or"} and start + 1 < len(tokens)
                                       and tokens[start + 1] in _COORDINATED_AGGREGATES):
            start += 1
        end = start
        while end < len(tokens) and tokens[end].isalpha() and not _ends_phrase(tokens, end):
            end += 1
        if end > start and tokens[end - 1] == name_tokens[-1]:
            return True
    return False


# The aggregate words one coordinates with another before the field they share (canon() forms).
_COORDINATED_AGGREGATES = frozenset({"average", "avg", "mean", "minimum", "min", "maximum", "max", "total", "sum"})


# The words that ask a total or an average, as analyze_question reads them.
AGGREGATE_CUE_WORDS = {"SUM": frozenset({"sum", "total"}), "AVG": frozenset({"average", "avg", "mean"})}


def aggregate_operand(question: str, names: Sequence[str], function: str) -> str | None:
    """The one of ``names`` (column names) that ends the phrase a ``function`` word begins: "the total amount of GBP
    orders" totals amount. This is the operand the question names, whatever the column holds (``_heads_phrase``), so a
    column that cannot be totaled is refused, never replaced by another (planted-text review, 2026-10-08). None when
    the question names none. A phrase ends with its sentence: "costs less than the average? Give me their first
    names" averages no name (Spider DEV 945, 2026-10-08)."""
    for sentence in re.split(r"[.?!;]+", str(question)):
        tokens = _tokens(sentence)
        positions = [index for index, token in enumerate(tokens) if token in AGGREGATE_CUE_WORDS.get(function, ())]
        if not positions:
            continue
        for name in names:
            name_tokens = _schema_tokens(name)
            if name_tokens and set(name_tokens) <= set(tokens) and _heads_phrase(tokens, name_tokens, positions):
                return name
    return None


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


def filter_only(column: ColumnRef, query: SelectQuery, tokens: Sequence[str]) -> bool:
    """Whether ``query`` compares ``column`` with a value and the question says the field only where it says that
    comparison: the value, or a comparison word, follows each place it names the field. The ranker does not prefer
    showing such a field, and the search adds a listing showing the rows' display field beside one that shows only such
    fields (sql_search._display_twin)."""
    values = [comparison.right for comparison in _comparisons(query.where)
              if comparison.left == column and isinstance(comparison.right, Literal)]
    if not values:
        return False
    name = [canon(word) for word in name_words(column.name)]
    said = [index for index, token in enumerate(tokens) if name and token == name[-1]]
    value_words = {canon(word) for value in values for word in words(str(value.value))}
    comparing = {"is", "not", "named", "called", "than", "over", "under", "above", "below", "equal", "between",
                 "more", "less", "greater", "smaller", "after", "before"}

    # "the name and population of cities that have a population between ..." asks for the population once. The
    # comparison may come first: "projects that require between 100 and 300 hours", "customers who do not use Cash
    # as payment method" (held-out Spider train 396, 420, 2026-10-12: each showed the field compared).
    def compared(index):
        start = index - len(name) + 1
        before = set(tokens[max(0, start - 4):max(0, start)])
        return bool(set(tokens[index + 1:index + 5]) & value_words
                    or (index + 1 < len(tokens) and tokens[index + 1] in comparing)
                    or before & value_words or before & {"between", "than"})

    return bool(said) and all(compared(index) for index in said)


def repeats_shown_rows(schema: SchemaGraph, query: Query, tokens: Sequence[str]) -> bool:
    """Whether a joined, unaggregated listing shows a row once per row of what it joins: a joined table refers to a
    shown table by a key its rows repeat (each shown row once per referring row), or a shown field refers to another
    table's key and repeats (its value once per row holding it). A ranking keeps its number of rows whatever repeats,
    and rows the question asks for one by one are its own ("the country name for each registration"). The search
    adds such a listing's DISTINCT twin and the ranker prefers it (Spider DEV 99, 351, 535)."""
    if (not isinstance(query, SelectQuery) or not query.joins or query.group_by or query.limit is not None
            or not isinstance(query.from_table, str)
            or not all(isinstance(item.expression, ColumnRef) for item in query.select)):
        return False
    shown_columns = [item.expression for item in query.select]
    shown = {column.table for column in shown_columns}
    read = {query.from_table, *(join.table for join in query.joins)}
    each = {canon(token) for index, token in enumerate(tokens) for before in tokens[max(0, index - 2):index]
            if before in {"each", "every", "per"}}
    for key in schema.foreign_keys:
        for child, parent in key.column_pairs:
            if child.table not in read or not schema.repeats((child,)):
                continue
            if {canon(word) for word in name_words(child.table)} & each:
                continue
            if child.table not in shown and parent.table in shown:
                return True
            if any(column.table == child.table and column.name == child.name for column in shown_columns):
                return True
    return False


# The words that say an aggregate, and the comparison words after one that make it a condition (canon() forms).
_CONDITION_CUES = {
    "AVG": frozenset({"average", "avg", "mean"}),
    "SUM": frozenset({"total", "sum"}),
    "MAX": frozenset({"maximum", "max"}),
    "MIN": frozenset({"minimum", "min"}),
    "COUNT": frozenset({"count", "number", "many"}),
}
_CONDITION_COMPARISONS = frozenset({"above", "below", "over", "under", "more", "less", "greater", "higher", "lower",
                                    "larger", "smaller", "bigger", "fewer", "exceed", "exceeds", "least", "than"})
_RELATIVE_WORDS = frozenset({"which", "whose", "where", "that"})


def _comparisons(predicate):
    if predicate is None:
        return ()
    if hasattr(predicate, "terms"):
        return tuple(comparison for term in predicate.terms for comparison in _comparisons(term))
    return (predicate,) if isinstance(predicate, Comparison) else ()


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(canon(token) for token in words(text))


def _schema_tokens(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return tuple(
        "number" if token.lower() == "no" else canon(token)
        for token in re.findall(r"[A-Za-z0-9]+", spaced)
        if canon(token) != "id"
    )




def relation_sides(schema: SchemaGraph) -> dict[str, tuple]:
    """Each table holding exactly two keys to one other table, as (owner key, other key, the other's participle or
    None): the other key is named for the relation ("Friend"."friend_id", "Likes"."liked_id", "PersonFriend"."friend"),
    the owner is the remaining one ("student_id"). A table whose keys neither or both name it relates no owner."""
    pairs: dict[tuple[str, str], list] = {}
    for key in schema.foreign_keys:
        if key.from_column.table != key.to_column.table and not key.additional_columns:
            pairs.setdefault((key.from_column.table, key.to_column.table), []).append(key)
    sides = {}
    for (relation, _), keys in sorted(pairs.items()):
        if len(keys) != 2:
            continue
        relation_words = {canon(word) for word in name_words(relation)}

        def role(key):
            words = [word for word in name_words(key.from_column.name) if word not in SURROGATE_KEY_WORDS]
            return next((word for word in words if canon(word) in relation_words
                         or (word.endswith("ed") and {word[:-1], word[:-2]} & relation_words)), None)

        named = [(key, role(key)) for key in keys]
        others = [(key, word) for key, word in named if word is not None]
        if len(others) == 1:
            other, word = others[0]
            owner = next(key for key, _ in named if key is not other)
            sides[relation] = (owner, other, word if word.endswith("ed") else None)
    return sides


# The words that say which end of a trip a question asks about.
TRAVEL_WORDS = {
    "source": frozenset({"leave", "leaving", "depart", "departing", "departure", "origin", "source"}),
    "destination": frozenset({"arrive", "arriving", "arrival", "land", "landing", "destination", "dest"}),
}


def travel_direction(tokens: tuple[str, ...]) -> str | None:
    token_set = set(tokens)
    return next((role for role, said in TRAVEL_WORDS.items() if token_set & said), None)


def travel_column_role(column: ColumnRef) -> str | None:
    words = set(_schema_tokens(column.name))
    if words & {"source", "origin", "departure", "depart", "from"}:
        return "source"
    if words & {"destination", "dest", "arrival", "arrive", "landing", "to"}:
        return "destination"
    return None


def _column_label(column: ColumnRef) -> str:
    return f"{column.table}.{column.name}"


# ---------------------------------------------------------------------------------------------
# Pool selection: the served choice among the search's candidates.
# ---------------------------------------------------------------------------------------------

# The pool contract (engine/tables.py:TableQuery.select_query): how many ranked search candidates
# are pooled, and the SQLite VM steps a pooled query may take before it counts as not running. The
# step budget is deterministic and machine-independent; it stops a pathological join, not a normal
# query. It grows with the cells of the tables a query reads (``execution_op_limit``): a query that
# reads its tables once or twice takes a few steps a cell (all 50,000 rows of a 22-column tab, ordered,
# take 2.3), while a join on a value most rows of two tabs share takes steps in proportion to the
# product of their rows. Two customer subscription exports joined on their currency ran to the old
# flat 100M steps, about 2.4 s each in production, for 110 s of one question (2026-10-05). The floor
# covers joins that fan small tables out: the heaviest of the 14,079 readings Spider's dev questions
# pool, cities joined to their countries' languages, takes 1.1M steps (39 a cell).
SEARCH_CANDIDATES = 25
EXECUTION_OPS_PER_CELL = 20
EXECUTION_OP_FLOOR = 10_000_000
EXECUTION_OP_LIMIT = 100_000_000


def execution_op_limit(query, tables) -> int:
    """The SQLite VM steps pooled ``query`` may take: ``EXECUTION_OPS_PER_CELL`` for each cell of the
    tables it reads, at least ``EXECUTION_OP_FLOOR`` and at most ``EXECUTION_OP_LIMIT``. ``tables`` maps
    the request's table names to {"columns", "rows"}; a query naming a table it does not hold keeps the
    ceiling."""
    cells = 0
    for name in query.referenced_tables():
        table = tables.get(name)
        if table is None:
            return EXECUTION_OP_LIMIT
        cells += len(table["rows"]) * len(table["columns"])
    return min(EXECUTION_OP_LIMIT, max(EXECUTION_OP_FLOOR, EXECUTION_OPS_PER_CELL * cells))


@dataclass(frozen=True)
class FallbackRecord:
    """How the labelled Gemini fallback (engine/question_rewrite.py) took part in one selection.

    ``kind`` is "rewrite" when the search answered Gemini's one-request rewording, and "none" when
    the rewording could not produce a query. "model" is the Gemini model id; ``note`` explains an
    unsuccessful rewrite.
    """
    kind: str
    model: str
    question: str | None = None
    note: str = ""

    def __post_init__(self):
        if self.kind not in {"rewrite", "none"}:
            raise ValueError(f"unknown fallback kind: {self.kind}")

    def record(self) -> dict:
        out = {"kind": self.kind, "model": self.model}
        if self.question is not None:
            out["question"] = self.question
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class PoolSelection:
    """How one question's query was chosen. Serving, decomposition leaves, the Spider evaluator and
    the offline regression gate read this record.

    ``pool`` is the deterministic search's candidates in its ranked order (``CandidateRanker``), or the
    fallback's when ``fallback`` says so. ``executable`` records which members ran within their
    SQLite step budget (``execution_op_limit``) and ``grounded`` which compare every text literal with a column
    that can hold it and join no two columns the foreign keys keep apart (engine/sql_grounding.py).
    ``ranking`` lists the eligible members, where both hold, in pool order. ``selected`` is usually
    ``ranking[0]``; a query that keeps the question's dates, counts no row twice in a SUM or AVG
    (``double_counted``), satisfies a registered calculation intent or totals a named money column can
    prefer a later eligible member (``select_ranked_candidate``). The first pool member is the search's
    own structural reading of the question (engine/decomposition.compound_candidate).
    """
    pool: tuple[ScoredQuery, ...]
    executable: tuple[bool, ...]
    grounded: tuple[bool, ...]
    ranking: tuple[int, ...]
    selected: int | None
    calculation_satisfied: tuple[bool, ...] = ()
    money_total: tuple[bool, ...] = ()
    date_satisfied: tuple[bool, ...] = ()
    # Which members SUM or AVG only rows their joins repeat (engine/sql_grounding.double_counted).
    double_counted: tuple[bool, ...] = ()
    fallback: FallbackRecord | None = None

    @property
    def candidate(self) -> ScoredQuery | None:
        """The member served: the selected pool member, a top-1 ranking keeping every tied row
        (sql_ast.keep_ties). Ranking and evidence read the pool member itself."""
        if self.selected is None:
            return None
        member = self.pool[self.selected]
        served = keep_ties(member.query)
        if served is member.query:
            return member
        return ScoredQuery(served, render_query(served), member.score, member.evidence)

    @property
    def served_by(self) -> str:
        """``search`` for its own reading or ``gemini-rewrite`` for a search over one rewording."""
        if self.selected is None or self.fallback is None or self.fallback.kind == "none":
            return "search"
        return f"gemini-{self.fallback.kind}"

    def constrained(self, admissible) -> PoolSelection:
        """This selection under a caller's contract, a filter on the ranking and never a rescore.

        Unchanged when the selected member satisfies ``admissible``; otherwise re-selected to the
        best-ranked eligible member that does (``selected`` is None when none does), so the record
        always describes the member that was actually served. A fallback whose reading the contract
        refuses served nothing, and its record says so.
        """
        if self.selected is not None and admissible(self.pool[self.selected]):
            return self
        selected = next((index for index in self.ranking if admissible(self.pool[index])), None)
        fallback = self.fallback
        if selected is None and fallback is not None and fallback.kind != "none":
            fallback = replace(fallback, kind="none",
                               note="the caller's contract refuses the fallback's reading")
        return replace(self, selected=selected, fallback=fallback)

    def record(self) -> dict:
        """JSON evidence: pool counts, the winner's place among the eligible members and its search
        score, which preference chose it, and the fallback when it took part."""
        evidence = {
            "pool_size": len(self.pool),
            "executable": sum(self.executable),
            "misgrounded": sum(ran and not sound
                               for ran, sound in zip(self.executable, self.grounded)),
            "eligible": len(self.ranking),
            "selected": self.selected,
            "served_by": self.served_by,
        }
        if self.selected is not None:
            evidence.update(
                rank=self.ranking.index(self.selected),
                score=round(self.pool[self.selected].score, 6),
                calculation_satisfied=bool(self.calculation_satisfied
                                           and self.calculation_satisfied[self.selected]),
                money_total=bool(self.money_total and self.money_total[self.selected]),
                date_satisfied=bool(self.date_satisfied and self.date_satisfied[self.selected]),
                double_counted=bool(self.double_counted and self.double_counted[self.selected]),
            )
        if self.fallback is not None:
            evidence["fallback"] = self.fallback.record()
        return evidence


def select_ranked_candidate(ranking: Sequence[int], calculation_satisfied: Sequence[bool],
                            money_total: Sequence[bool],
                            date_satisfied: Sequence[bool] = (),
                            double_counted: Sequence[bool] = ()) -> int | None:
    """The shared post-ranking serving rule; inputs are gold-blind candidate facts.

    A query that keeps the dates the question names (engine/sql_dates.realizes_dates) is served
    when any does: an undated reading ranked first, and "how many contracts were signed before
    July 10, 2026" was served undated and declined (2026-10-02). Among those, a query whose SUM or
    AVG reads only rows its joins repeat (engine/sql_grounding.double_counted) is served only when
    every one does: "the total Amount by Plan and Currency" ranked first a sum of a report's Total
    Amount once per subscription it matched (2026-10-05). Among those, prefer a satisfied
    calculation, if any. A named money total then constrains that choice, retaining it when
    compatible or taking the first ranked total.
    """
    if not ranking:
        return None
    ranking = [i for i in ranking if date_satisfied and date_satisfied[i]] or list(ranking)
    ranking = [i for i in ranking if not (double_counted and double_counted[i])] or ranking
    selected = next((i for i in ranking if calculation_satisfied[i]), ranking[0])
    totals = [i for i in ranking if money_total[i]]
    if totals and not money_total[selected]:
        selected = totals[0]
    return selected
