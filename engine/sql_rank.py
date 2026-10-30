"""The own-data pool contract and selection record.

``PoolSelection`` records the served choice among the readers' candidates (engine/readers/search.py): the
best re-scored candidate that executes and is grounded, under the pool contract (``execution_op_limit``).
``analyze_question`` reads the roles a question's words give the schema, for the registered calculations
(engine/calculations).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Mapping, Sequence

from engine.currency_intent import is_currency_measure_column, is_currency_source_column
from engine.dataset_semantics import is_synthetic_currency_column
from engine.sql_ast import (
    Aggregate, ColumnRef, Comparison, SelectItem, SelectQuery, column_refs, keep_ties, named_groups, render_query,
)
from engine.sql_dates import period_grouping
from engine.sql_expansion import (
    PHRASE_BREAKS, OutputRequest, asked_cues, by_groups, counted_noun_tables,
    field_numbers, output_requests, request_fields, share_cue, words,
)
from engine.sql_expansion import tokens as expansion_tokens
from engine.sql_candidate import ScoredQuery
from engine.sql_schema import (
    SchemaGraph, canon, spaced_name,
)


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


def _ends_phrase(tokens: tuple[str, ...], index: int) -> bool:
    """Whether ``tokens[index]`` ends the noun phrase it follows: a word of ``PHRASE_BREAKS``, or "order" opening an
    ordering ("order by"). Before another noun "order" is part of the phrase: "the total order amount" totals the
    amount (planted-text review, 2026-10-08)."""
    word = tokens[index]
    if word == "order":
        return index + 1 < len(tokens) and tokens[index + 1] == "by"
    return word in PHRASE_BREAKS
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


def _comparisons(predicate):
    if predicate is None:
        return ()
    if hasattr(predicate, "terms"):
        return tuple(comparison for term in predicate.terms for comparison in _comparisons(term))
    return (predicate,) if isinstance(predicate, Comparison) else ()


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(canon(token) for token in words(text))


def _schema_tokens(name: str) -> tuple[str, ...]:
    spaced = spaced_name(name)
    return tuple(
        "number" if token.lower() == "no" else canon(token)
        for token in re.findall(r"[A-Za-z0-9]+", spaced)
        if canon(token) != "id"
    )


def _column_label(column: ColumnRef) -> str:
    return f"{column.table}.{column.name}"


# ---------------------------------------------------------------------------------------------
# Pool selection: the served choice among the search's candidates.
# ---------------------------------------------------------------------------------------------

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
class PoolSelection:
    """How one question's query was chosen. Serving, decomposition leaves, the Spider evaluator and
    the offline regression gate read this record.

    ``pool`` is the readers' candidates, most probable first (engine/readers/search.py). ``executable``
    records which members ran within their SQLite step budget (``execution_op_limit``) and ``grounded``
    which compare every text literal with a column that can hold it and join no two columns the foreign
    keys keep apart (engine/sql_grounding.py). ``ranking`` lists the eligible members, where both hold, in
    the re-scorer's order. ``selected`` is usually ``ranking[0]``; a query that keeps the question's dates,
    counts no row twice in a SUM or AVG (``double_counted``), keeps rows its order ranks (``undecided``), finds
    rows where a less probable member finds none (``outread``) or satisfies a registered calculation intent can
    prefer a later eligible member (``select_ranked_candidate``),
    and the readers' refusal leaves it
    None when it is not confident of the choice. The first pool member is the readers' most probable
    reading of the question (engine/decomposition.compound_candidate).
    """
    pool: tuple[ScoredQuery, ...]
    executable: tuple[bool, ...]
    grounded: tuple[bool, ...]
    ranking: tuple[int, ...]
    selected: int | None
    calculation_satisfied: tuple[bool, ...] = ()
    date_satisfied: tuple[bool, ...] = ()
    # Which members SUM or AVG only rows their joins repeat (engine/sql_grounding.double_counted).
    double_counted: tuple[bool, ...] = ()
    # Which members' order ranks none of the rows they keep above the first row they leave out (``ranking_cut_probe``).
    undecided: tuple[bool, ...] = ()
    # The headers each member reads a column of that another column shares, the question not saying which
    # (engine/sql_grounding.shared_headers): such a member is not eligible.
    shared_headers: tuple[tuple[str, ...], ...] = ()
    # The readers' refusal: its confidence in the choice, and the choice it declined when it was not confident.
    confidence: float | None = None
    declined: int | None = None
    # Which members match no row while the readers found a more probable member that does (``outread_by_rows``).
    outread: tuple[bool, ...] = ()
    # The chosen reading's test of a column of few values against a value it does not hold (sql_grounding.absent_value):
    # the choice is declined, and the reply asks which value was meant.
    absent_value: tuple[str, str, tuple[str, ...]] | None = None
    # table -> the column naming each row's currency, for tables that hold several currencies and a question that asks
    # for no conversion (``mixed_currencies``): a total of money is served per currency (``per_currency``).
    currencies: Mapping[str, ColumnRef] = field(default_factory=dict)

    @property
    def candidate(self) -> ScoredQuery | None:
        """The member served: the selected pool member, a top-1 ranking keeping every tied row
        (sql_ast.keep_ties). Ranking and evidence read the pool member itself."""
        if self.selected is None:
            return None
        member = self.pool[self.selected]
        split = per_currency(member.query, self.currencies)
        served = keep_ties(named_groups(split))
        if served is member.query:
            return member
        evidence = member.evidence
        if split is not member.query and evidence:
            evidence = (f"{evidence[0]}, for each {spaced_name(split.group_by[0].name).lower()}", *evidence[1:])
        return ScoredQuery(served, render_query(served), member.score, evidence)

    def constrained(self, admissible) -> PoolSelection:
        """This selection under a caller's contract, a filter on the ranking and never a rescore.

        Unchanged when the selected member satisfies ``admissible``; otherwise re-selected to the
        best-ranked eligible member that does (``selected`` is None when none does), so the record
        always describes the member that was actually served.
        """
        if self.selected is not None and admissible(self.pool[self.selected]):
            return self
        selected = next((index for index in self.ranking if admissible(self.pool[index])), None)
        return replace(self, selected=selected)

    def record(self) -> dict:
        """JSON evidence: pool counts, the winner's place among the eligible members, its readers' log
        probability, which preference chose it, and the refusal's confidence (with the member it declined)."""
        evidence = {
            "pool_size": len(self.pool),
            "executable": sum(self.executable),
            "misgrounded": sum(ran and not sound
                               for ran, sound in zip(self.executable, self.grounded)),
            "eligible": len(self.ranking),
            "selected": self.selected,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "declined": self.declined,
        }
        if self.selected is not None:
            evidence.update(
                rank=self.ranking.index(self.selected),
                score=round(self.pool[self.selected].score, 6),
                calculation_satisfied=bool(self.calculation_satisfied
                                           and self.calculation_satisfied[self.selected]),
                date_satisfied=bool(self.date_satisfied and self.date_satisfied[self.selected]),
                double_counted=bool(self.double_counted and self.double_counted[self.selected]),
                undecided=bool(self.undecided and self.undecided[self.selected]),
            )
        return evidence


def mixed_currencies(graph) -> dict[str, ColumnRef]:
    """Table -> the column naming each row's currency, for each table whose rows hold more than one currency."""
    found = {}
    for column in graph.columns:
        if (is_currency_source_column(column.ref.name) and not is_synthetic_currency_column(column.ref.name)
                and len({str(value).strip().lower() for value in column.values
                         if value is not None and str(value).strip()}) > 1):
            found.setdefault(column.ref.table, column.ref)
    return found


def per_currency(query, currencies: Mapping[str, ColumnRef]):
    """The served form of a total or average of money over rows in several currencies: one row per currency. Added up,
    332,600 rupees and 205,194 dollars read as 545,337 of nothing, as "how much sales for long term customers" did
    (2026-10-30). A question that names a currency filters or converts; one that groups by it asks for this already."""
    if (not currencies or not isinstance(query, SelectQuery) or query.group_by or query.joins or query.limit is not None
            or query.distinct or not isinstance(query.from_table, str) or query.from_table not in currencies):
        return query
    measures = [item.expression for item in query.select]
    if not measures or not all(isinstance(measure, Aggregate) and measure.function in ("SUM", "AVG")
                               and isinstance(measure.operand, ColumnRef)
                               and is_currency_measure_column(measure.operand.name) for measure in measures):
        return query
    currency = currencies[query.from_table]
    if currency in column_refs(query):
        return query
    return replace(query, select=(SelectItem(currency), *query.select), group_by=(currency,))


def outread_by_rows(pool: Sequence[ScoredQuery], ranking: Sequence[int],
                    matched_nothing: Sequence[bool]) -> tuple[bool, ...]:
    """Per pool member: whether it matches no row while an eligible member the readers found more probable does.

    The re-scorer reads a question beside each reading, and a reading that repeats the question's words reads well:
    "sales for long term customers (above 6 months)" ranked duration = 'above 6 months' over the value the column
    holds, 'Greater than 6 months', which the readers found more probable, and the answer said no rows matched
    (2026-10-30). A reading the readers rated highest keeps its empty answer: "orders from Mars" matches nothing,
    and no less probable reading of another country or of no filter answers it instead.
    """
    best = max((pool[i].score for i in ranking if not matched_nothing[i]), default=None)
    return tuple(bool(matched_nothing[i] and best is not None and best > pool[i].score) for i in range(len(pool)))


def select_ranked_candidate(ranking: Sequence[int], calculation_satisfied: Sequence[bool],
                            date_satisfied: Sequence[bool] = (),
                            double_counted: Sequence[bool] = (),
                            undecided: Sequence[bool] = (),
                            outread: Sequence[bool] = ()) -> int | None:
    """The shared post-ranking serving rule; inputs are gold-blind candidate facts.

    A query that keeps the dates the question names (engine/sql_dates.realizes_dates) is served
    when any does: an undated reading ranked first, and "how many contracts were signed before
    July 10, 2026" was served undated and declined (2026-10-02). Among those, a query whose SUM or
    AVG reads only rows its joins repeat (engine/sql_grounding.double_counted) is served only when
    every one does: "the total Amount by Plan and Currency" ranked first a sum of a report's Total
    Amount once per subscription it matched (2026-10-05). Among those, a ranking whose order ranks
    none of the rows it keeps above the first row it leaves out (``undecided``: its first row ties
    with that one) is served only when every one is: "top 3 cities" ranked first the cities by
    their number of rows, one each, and served three of four by name (2026-10-23). A tie at the cut
    alone is the data's: "the five oldest people" ties the fifth and sixth ages. Among those, a query that
    matches no row while a more probable one finds rows (``outread_by_rows``) is served only when every one
    does. Among those, prefer a satisfied calculation, if any.
    """
    if not ranking:
        return None
    ranking = [i for i in ranking if date_satisfied and date_satisfied[i]] or list(ranking)
    ranking = [i for i in ranking if not (double_counted and double_counted[i])] or ranking
    ranking = [i for i in ranking if not (undecided and undecided[i])] or ranking
    ranking = [i for i in ranking if not (outread and outread[i])] or ranking
    return next((i for i in ranking if calculation_satisfied[i]), ranking[0])


def ranking_cut_probe(query):
    """(probe, limit, terms) for a ranking whose LIMIT keeps more than one row, else None: ``probe`` is the same
    query showing its ORDER BY terms after its own columns and keeping one row more, so the first row kept and the
    first left out can be compared on the terms that order them (``ranks_nothing``). A top-1 ranking keeps its ties
    when served (sql_ast.keep_ties); a DISTINCT one would change its rows by showing more."""
    if (not isinstance(query, SelectQuery) or not query.order_by or not query.limit or query.limit < 2
            or query.distinct or query.with_ties):
        return None
    terms = len(query.order_by)
    probe = replace(query, select=query.select + tuple(SelectItem(term.expression) for term in query.order_by),
                    limit=query.limit + 1)
    return probe, query.limit, terms


def ranks_nothing(rows, limit: int, terms: int) -> bool:
    """Whether the probe's rows (``ranking_cut_probe``) show an order that ranks none of the rows kept above the first
    one left out: the first row and the row after the cut sort equal on every ORDER BY term, so every row between
    them ties too."""
    return len(rows) > limit and tuple(rows[0][-terms:]) == tuple(rows[limit][-terms:])
