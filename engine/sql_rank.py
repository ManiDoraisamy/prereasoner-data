"""The own-data selection record.

``PoolSelection`` records the served choice among the own-data path's candidates (engine/program/answer.py,
engine/tables.py:TableQuery.select_query). ``analyze_question`` reads the roles a question's words give the schema, for
the registered calculations (engine/calculations).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence

from engine.sql_ast import (
    ColumnRef, Comparison,
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
# Pool selection: the served choice among the own-data path's candidates.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PoolSelection:
    """How one question's query was chosen (engine/tables.py:TableQuery.select_query). Serving, decomposition leaves
    and the evaluators read this record.

    ``pool`` is the own-data path's listed candidates in its decoder's order (engine/program/answer.py): each program
    that returned rows, as its typed AST (engine/program/lowering.py) and SQL, with its read-as line as ``evidence``
    and as ``score`` the probability the choice gave its answer, summed over the candidates returning that answer
    (engine/program/closed.py choose_answer). ``ranking`` orders the pool by that score, the first listing first among
    equals. ``selected`` is the decided candidate, answered or, below closed.T_FLAG, ``warned``: ``uncertain`` then
    names the coordinates the runner-up answer (first listed at ``runner_up``) reads differently
    (engine/program/language.coordinates); ``alike`` when that reading reads word for word like the answer's own (in
    the sheet's header words). When the likeliest answer is most likely wrong (engine/program/closed.py ERRED) the path
    decides nothing (``selected`` is None, so compose and the search may answer it) and ``erred`` is the likeliest
    candidate, served marked as most likely wrong when its retry (engine/retry.py) cannot answer: None when none
    returned rows.
    ``confidence`` is the likeliest answer's probability and ``probabilities`` the choice's own, per listing (r00, r01,
    ..., and none). ``programs``, ``rows`` and ``catalog`` hold each candidate's program, the rows it returned as the
    choice read them (a verdict as True or False, a cell by its stored value; ``answer`` gives them as served) and the
    workbook it reads; ``tables`` the request's tables the programs
    run on (engine/program/store.py, on the request's database). ``converts``: the request's money columns could be
    read in another currency, so the path's own convert question read whether the question wants its answer in one
    (engine/program/answer.py convert_target)."""
    pool: tuple[ScoredQuery, ...]
    ranking: tuple[int, ...]
    selected: int | None
    confidence: float | None = None
    erred: int | None = None
    warned: bool = False
    uncertain: tuple[str, ...] = ()
    runner_up: int | None = None
    probabilities: dict | None = None
    programs: tuple = ()
    rows: tuple = ()
    catalog: object = None
    tables: object = None
    converts: bool = False
    alike: bool = False

    @property
    def candidate(self) -> ScoredQuery | None:
        """The member decided, or None when the likeliest answer is most likely wrong."""
        return None if self.selected is None else self.pool[self.selected]

    def plan(self, index: int, slug: str, whole_tables: bool = False):
        """The one plan both deterministic emitters run for pool member ``index`` (engine/program/lowering.py to_plan),
        with the tables it reads loaded on the request's database; ``whole_tables`` declares each table whole, as the
        plans a decomposition fuses must (engine/decomposition.py)."""
        from engine.program.execute import prepare
        from engine.program.lowering import to_plan

        prepare(self.programs[index], self.tables)
        return to_plan(self.programs[index], self.catalog, slug, whole_tables)

    def estimated_rows(self, index: int) -> int:
        """The lines pool member ``index`` reads, for the deterministic run's row bound."""
        from engine.program.language import tables_of

        return sum(len(self.catalog.table(table_id).lines) for table_id in tables_of(self.programs[index]))

    def shown(self, index: int, rows) -> tuple[tuple, ...]:
        """Rows a deterministic run of pool member ``index`` returned, as its answer gives them (engine/program/execute.py
        displayed, then answered: what ``rows`` holds for it)."""
        from engine.program.execute import answered, displayed

        return answered(self.programs[index], displayed(self.programs[index], rows, self.tables), self.catalog)

    def answer(self, index: int) -> tuple[tuple, ...]:
        """Pool member ``index``'s rows as its answer gives them (engine/program/execute.py answered): what ``rows``
        holds as the choice read it, a verdict as Yes or No, a cell by how the sheet writes it."""
        from engine.program.execute import answered

        return answered(self.programs[index], self.rows[index], self.catalog)

    def columns(self, index: int) -> tuple[str, ...]:
        """The names of pool member ``index``'s answer columns, in the sheet's words (engine/program/output.py)."""
        from engine.program.output import answer_columns

        return answer_columns(self.programs[index], self.catalog)

    def record(self) -> dict:
        """JSON evidence: the pool's size, the decided member and its place in the ranking, the likeliest answer's
        probability, and the likeliest member when its answer is most likely wrong."""
        evidence = {
            "pool_size": len(self.pool),
            "eligible": len(self.ranking),
            "selected": self.selected,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
            "erred": self.erred,
            "warned": self.warned,
        }
        if self.selected is not None:
            evidence.update(rank=self.ranking.index(self.selected), score=round(self.pool[self.selected].score, 6))
        return evidence
