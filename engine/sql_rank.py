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
    Aggregate, ColumnRef, Comparison, DatePart, Query, SelectQuery, SetQuery, keep_ties, render_query,
    share_aggregate,
)
from engine.sql_dates import period_grouping
from engine.sql_expansion import FUNCTION_WORDS, asked_cues, by_groups, share_cue, share_requested, words
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import SchemaGraph, canon, is_surrogate_key


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
                and (is_surrogate_key(aggregate.operand.name) or _is_name(aggregate.operand.name))
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
            id_columns = [column for column in select_columns + group_columns if is_surrogate_key(column.name)]
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

        features.extend(self._model_features(query))
        return tuple(features)

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
        group_requested=bool(group_positions) or period is not None,
        group_period=period is not None,
        group_tables=frozenset(group_tables),
        group_columns=frozenset(group_columns),
        counted_tables=frozenset(counted_tables),
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
        while start < len(tokens) and tokens[start] in _PHRASE_OPENERS:
            start += 1
        end = start
        while end < len(tokens) and tokens[end].isalpha() and not _ends_phrase(tokens, end):
            end += 1
        if end > start and tokens[end - 1] == name_tokens[-1]:
            return True
    return False


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




def travel_direction(tokens: tuple[str, ...]) -> str | None:
    token_set = set(tokens)
    if token_set & {"leave", "leaving", "depart", "departing", "departure", "origin", "source"}:
        return "source"
    if token_set & {"arrive", "arriving", "arrival", "land", "landing", "destination", "dest"}:
        return "destination"
    return None


def travel_column_role(column: ColumnRef) -> str | None:
    words = set(_schema_tokens(column.name))
    if words & {"source", "origin", "departure", "depart", "from"}:
        return "source"
    if words & {"destination", "dest", "arrival", "arrive", "landing", "to"}:
        return "destination"
    return None


def _is_name(name: str) -> bool:
    return bool(set(_schema_tokens(name)) & {"name", "title", "label"})


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
