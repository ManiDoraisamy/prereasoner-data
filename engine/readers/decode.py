"""Readings from the reading model's outputs (engine.readers.models): ``decode`` takes every decision's most
probable option and ``candidates`` enumerates the most probable distinct readings, each with the log probability of
the decisions it took, for the builder (engine.readers.readings) to make into typed-AST queries.

The decisions are explicit: a table's use, a column's role, aggregate and operator, a value span, how many items
of each kind a block holds, structure, direction, limit. Generic semantics shape them: a column a condition pins
to one value is not shown again, a column matched with the question's value is not counted distinct, and a
condition on a date column reads its days as the engine reads dates (engine.sql_dates).
"""
import heapq
import json
import math
import re

from engine.readers import numerals
from engine.readers.layout import (AGGREGATES, COLUMN_POS_WEIGHT, COUNTS, GLOBAL, HAVING_OPERATORS, LAYOUT,
                                   OPERATORS, STRUCTURES, TABLE_POS_WEIGHT)
from engine.readers.readings import SUBQUERY
from engine.sql_ast import SQLType

LOWER_BOUNDS, UPPER_BOUNDS = (">", ">="), ("<", "<=")



def field(probabilities, name):
    offset, width = LAYOUT[name]
    return probabilities[offset:offset + width]


def literal(text, column, graph, operator):
    """The literal a span names for a column: a number for a numeric column, the stored spelling of a matching
    value, and %text% for LIKE."""
    text = unlisted(text).strip("'\"")
    if operator in ("LIKE", "NOT LIKE"):
        return f"%{text}%"
    table, name = column.split(".", 1)
    schema_column = graph.column_map.get((table, name))
    numeric = schema_column is not None and schema_column.ref.type in (SQLType.INTEGER, SQLType.REAL)
    number = numerals.number(text)
    if number is not None and (numeric or schema_column is None or not any(
            isinstance(v, str) and v.lower() == text.lower() for v in schema_column.values)):
        return number
    if schema_column is not None:
        for value in schema_column.values:
            if isinstance(value, str) and value.lower() == text.lower():
                return value
    return text


def span_text(span, example):
    part, start, end, _ = span
    return (example["question"] if part == 0 else example["text"])[start:end]


class Choices:
    """The decisions one decoding takes. Each is among options with probabilities, best first; ``overrides`` takes
    an option other than the best by its rank. ``seen`` keeps every decision's options; ``used`` keeps the rank
    taken at each decision this decoding reached, so ``score`` counts only the decisions that shaped its readings."""

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.seen, self.used = {}, {}

    def pick(self, key, options):
        options = sorted(options, key=lambda option: -option[1])
        self.seen.setdefault(key, options)
        rank = min(self.overrides.get(key, 0), len(options) - 1)
        self.used[key] = rank
        return options[rank][0]

    def yes(self, key, probability):
        return self.pick(key, [(True, probability), (False, 1.0 - probability)])

    def score(self):
        return sum(math.log(max(self.seen[key][rank][1], 1e-6)) for key, rank in self.used.items())


def softmax(logits):
    top = max(logits)
    weights = [math.exp(x - top) for x in logits]
    total = sum(weights)
    return [w / total for w in weights]


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


# Where a block's item counts come from: "head" (the count readings) or "threshold" (the items scored >= 0.5),
# kept for the ablation that separates the count heads from the rest of a model change.
COUNT_FROM = "head"


def count(output, choices, b, kind, scored, least=0):
    """How many items of ``kind`` block b holds: a decision over the count head's classes."""
    if COUNT_FROM == "threshold":
        return max(least, sum(1 for score, _ in scored if score >= 0.5))
    offset = GLOBAL[f"count_{kind}{b}"][0]
    top = COUNTS[kind]
    weights = softmax(output["globals"][offset:offset + top + 1])
    options = [(n, w) for n, w in enumerate(weights) if n >= least] or [(least, 1.0)]
    return choices.pick(("count", b, kind), options)


def best(choices, b, kind, scored, n):
    """The n best-scored items, best first; whether the n-th gives way to the next-best is itself a decision."""
    ranked = [item for _, item in sorted(scored, key=lambda pair: -pair[0])]
    scores = sorted((score for score, _ in scored), reverse=True)
    taken = ranked[:n]
    if 0 < n < len(ranked):
        last, following = scores[n - 1], scores[n]
        total = max(last + following, 1e-9)
        if choices.pick(("swap", b, kind), [(False, last / total), (True, following / total)]):
            taken = ranked[:n - 1] + [ranked[n]]
    return taken


STORED_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")


def unlisted(text):
    """A pointed value without the marks of the list it was listed in ("2026-08-04)" from "(2026-08-03,
    2026-08-04)"): edge commas and semicolons, and a parenthesis the value does not close or open itself."""
    text = text.strip().strip(",;").strip()
    if text.endswith(")") and "(" not in text:
        text = text[:-1].strip()
    if text.startswith("(") and ")" not in text:
        text = text[1:].strip()
    return text


def date_column(column, graph):
    """Whether readings column ``column`` ("t.c") is a date column of ``graph``."""
    if column == "*" or ":" in column:
        return False
    schema_column = graph.column_map.get(tuple(column.split(".", 1)))
    return schema_column is not None and schema_column.ref.type == SQLType.DATE


def periodic(select, group, order, graph, question, tables=()):
    """A period the question groups by ("how many orders per month", engine.sql_dates.period_grouping) groups by
    that period of the date column the readers group by: its year-month, so that August 2025 and August 2026 stay
    two rows. A shown or sorted reading of that date column (itself, its earliest or latest) shows the period.

    When the readers group by no date column, only by surrogate keys (one group per row: "how many orders per
    month" read as "for each order id", 2026-10-22) or by nothing, the period groups the one date column of the
    ``tables`` they read, and is shown first; with no date column there, or several, the reading stands as read."""
    from engine.sql_dates import period_grouping
    from engine.sql_expansion import tokens
    from engine.sql_schema import is_surrogate_key

    if period_grouping(tokens(question), graph) is None:
        return select, group, order
    dates = [column for column in group if date_column(column, graph)]
    if not dates:
        if any(column == "*" or ":" in column or not is_surrogate_key(column.split(".", 1)[1]) for column in group):
            return select, group, order
        in_tables = sorted(f"{column.ref.table}.{column.ref.name}" for column in graph.columns
                           if column.ref.table in tables and column.ref.type == SQLType.DATE)
        if len(in_tables) != 1:
            return select, group, order
        keys, (date,) = set(group), in_tables
        dates, group = [date], [date]
        shown = []                                 # a key shown as its group's label gives way to the period
        for slot, aggregate, column, distinct in select:
            column = date if aggregate == "plain" and column in keys else column
            if not (aggregate == "plain" and column == date and any(s[2] == date and s[1] == "plain" for s in shown)):
                shown.append([slot, aggregate, column, distinct])
        if not any(aggregate == "plain" and column == date for _, aggregate, column, _ in shown):
            shown = [[-1, "plain", date, False]] + shown
        select = [[place, aggregate, column, distinct] for place, (_, aggregate, column, distinct) in enumerate(shown)]
    period = {column: f"year_month:{column}" for column in dates}
    select = [[slot, "plain", period[column], False] if column in period and aggregate in ("plain", "MIN", "MAX")
              else [slot, aggregate, column, distinct] for slot, aggregate, column, distinct in select]
    order = [["plain", period[column]] if column in period and aggregate in ("plain", "MIN", "MAX")
             else [aggregate, column] for aggregate, column in order]
    return select, [period.get(column, column) for column in group], order


def dated(where, graph, question, ranges=True):
    """Conditions on a date column say the days the question's date phrase names, read as the engine reads dates
    (engine.sql_dates): "signed before July 10, 2026" is signed < '2026-07-10', "in August" the month 8. The model
    says which column a date is about; the date reader says which days. A date column the phrases cannot cover
    keeps the model's own reading, as does one the question gives in stored form ("before 2018-03-17 07:13:53",
    where the literal is exact to the second), and a range under OR (``ranges`` false), which would read as either
    of its ends."""
    from engine.sql_ast import DatePart
    from engine.sql_dates import served_date_phrases
    from engine.sql_expansion import tokens

    def is_date(column):
        return date_column(column, graph)

    stored = {column for column, _, value in where if isinstance(value, str) and STORED_DATE.fullmatch(value.strip())}
    columns = []
    for column, _, value in where:
        if is_date(column) and not isinstance(value, dict) and column not in columns and column not in stored:
            columns.append(column)
    phrases = list(served_date_phrases(question, tokens(question), graph)) if columns else []
    if not phrases:
        return where
    read = [w for w in where if w[0] not in columns]
    for i, column in enumerate(columns):
        comparisons = phrases[i].comparisons(graph.column_map[tuple(column.split(".", 1))].ref) \
            if i < len(phrases) else ()
        if not comparisons or (len(comparisons) > 1 and not ranges):
            read.extend(w for w in where if w[0] == column)
            continue
        for comparison in comparisons:
            left = comparison.left
            name = f"{left.operand.table}.{left.operand.name}" if isinstance(left, DatePart) \
                else f"{left.table}.{left.name}"
            read.append([f"{left.part}:{name}" if isinstance(left, DatePart) else name, comparison.operator,
                         comparison.right.value])
    return read


def decode_block(output, example, graph, b, nested, choices):
    columns = output["columns"]
    order_of = {name: i for i, name in enumerate(columns)}
    g = output["globals"]
    kept = [(p[b], t) for t, p in output["tables"].items()]
    tables = best(choices, b, "tables", kept, count(output, choices, b, "tables", kept, least=1))
    def value_options(column):
        spans = output["values"].get(f"{b}|{column}", [])
        texts, scores = [], []
        for span in spans:
            text = span_text(span, example)
            if text and text not in texts:
                texts.append(text)
                scores.append(span[3])
        return list(zip(texts, softmax(scores))) if texts else []

    compared = [(field(p[b], "where")[i], (column, operator)) for column, p in columns.items() if column != "*"
                for i, operator in enumerate(OPERATORS)]
    operators_of = {}
    for column, operator in best(choices, b, "where", compared, count(output, choices, b, "where", compared)):
        operators_of.setdefault(column, []).append(operator)
    where = []
    for column in sorted(operators_of, key=order_of.get):
        p = columns[column]
        operators = sorted(operators_of[column], key=OPERATORS.index)
        subquery = nested and choices.yes(("where-sub", b, column), field(p[b], "where_sub")[0])
        literal_operators = [o for o in operators if o not in ("IN", "NOT IN") and not subquery]
        if len(literal_operators) == 1 and choices.yes(("twice", b, column), field(p[b], "where_twice")[0]):
            literal_operators = literal_operators * 2
        options = value_options(column)
        texts = []
        if options and literal_operators:
            first = choices.pick(("value", b, column), options)
            texts = [first] + [t for t, _ in options if t != first][:len(literal_operators) - 1]
        if (len(set(literal_operators)) == 2 and len(texts) == 2
                and all(numerals.number(t) is not None for t in texts)):
            low, high = sorted(texts, key=numerals.number)
            texts = [low if o in LOWER_BOUNDS else high for o in literal_operators]
        for operator in operators:
            if operator in ("IN", "NOT IN") or subquery:
                where.append([column, operator, SUBQUERY])
        for i, operator in enumerate(literal_operators):
            if texts:
                where.append([column, operator, literal(texts[i % len(texts)], column, graph, operator)])
    conj = "AND"
    if len(where) > 1 and choices.yes(("or", b), sigmoid(g[GLOBAL[f"conj{b}"][0]])):
        conj = "OR"
    where = dated(where, graph, example["question"], ranges=conj == "AND")
    # A column the conditions pin to one value tells nothing more when shown, grouped or sorted by ("the players
    # from college UCLA" shows the players, not the college): only 52 of 9,450 gold blocks with an equality on a
    # value show that column. Such items are left out unless nothing else remains.
    pinned = {column for column, operator, value in where
              if operator == "=" and not isinstance(value, dict)
              and sum(1 for c, o, _ in where if c == column) == 1}

    matched = {column for column, operator, value in where if operator in ("=", "LIKE") and not isinstance(value, dict)}

    def unpinned(items, plain_of):
        kept = [(score, item) for score, item in items if not (plain_of(item) and plain_of(item) in pinned)]
        return kept or items

    # The star is shown counted (COUNT(*)) or plain, every column ("show all rows ordered by id").
    shown = unpinned([(field(p[b], "select")[a], (column, aggregate)) for column, p in columns.items()
                      for a, aggregate in enumerate(AGGREGATES) if column != "*" or aggregate in ("COUNT", "plain")],
                     lambda item: item[0] if item[1] == "plain" else None)
    select = []
    for column, aggregate in best(choices, b, "select", shown, count(output, choices, b, "select", shown, least=1)):
        a = AGGREGATES.index(aggregate)
        p = columns[column]
        slot = choices.pick(("slot", b, column, aggregate), list(enumerate(output["slots"][column][b][a])))
        # A column a condition matches with the question's own value (= or LIKE) is counted by its rows, not by its
        # distinct values: those are the one value, or the few that contain it ("How many Intake Consent documents are
        # there?" counts documents). Of 16,254 gold blocks of Spider and BIRD train, one counts such a column distinct.
        distinct = aggregate == "COUNT" and column != "*" and column not in matched and choices.yes(
            ("distinct-aggregate", b, column), field(p[b], "distinct_aggregate")[0])
        select.append([slot, aggregate, column, distinct])
    select.sort(key=lambda item: (item[0], order_of[item[2]]))
    select = [[i, aggregate, column, distinct] for i, (_, aggregate, column, distinct) in enumerate(select)]
    grouped = unpinned([(field(p[b], "group")[0], c) for c, p in columns.items() if c != "*"], lambda item: item)
    group = best(choices, b, "group", grouped, count(output, choices, b, "group", grouped))
    having = []
    held = [(field(p[b], "having")[a], (column, aggregate)) for column, p in columns.items()
            for a, aggregate in enumerate(AGGREGATES)
            if aggregate != "plain" and (column != "*" or aggregate == "COUNT")]
    for column, aggregate in best(choices, b, "having", held, count(output, choices, b, "having", held)):
        p = columns[column]
        operator = choices.pick(("having-op", b, column), list(zip(HAVING_OPERATORS, field(p[b], "having_op"))))
        if nested and choices.yes(("having-sub", b, column), field(p[b], "having_sub")[0]):
            having.append([aggregate, column, operator, SUBQUERY])
        else:
            options = value_options(column)
            text = choices.pick(("having-value", b, column), options) if options else "1"
            number = numerals.number(text)
            having.append([aggregate, column, operator, text if number is None else number])
    sorted_by = unpinned([(field(p[b], "order")[a], (column, aggregate)) for column, p in columns.items()
                          for a, aggregate in enumerate(AGGREGATES) if column != "*" or aggregate == "COUNT"],
                         lambda item: item[0] if item[1] == "plain" else None)
    order = [list(item) for item in best(choices, b, "order", sorted_by, count(output, choices, b, "order", sorted_by))]
    order = [[aggregate, column] for column, aggregate in order]
    offset = GLOBAL[f"dir{b}"][0]
    direction = None
    if order:
        weights = softmax(g[offset + 1:offset + 3])
        direction = choices.pick(("dir", b), [("ASC", weights[0]), ("DESC", weights[1])])
    offset = GLOBAL[f"limit{b}"][0]
    limit = choices.pick(("limit", b), list(zip((None, 1, "number"), softmax(g[offset:offset + 3]))))
    if limit == "number":
        numbers = []
        for span in output["limit"]:
            number = numerals.number(span_text(span, example))
            if isinstance(number, int) and number > 0 and number not in [n for n, _ in numbers]:
                numbers.append((number, span[3]))
        limit = choices.pick(("limit-number", b), list(zip([n for n, _ in numbers], softmax(
            [score for _, score in numbers])))) if numbers else 3
    select, group, order = periodic(select, group, order, graph, example["question"], tables)
    used = {c.partition(":")[2].split(".", 1)[0] if ":" in c else c.split(".", 1)[0] for c in
            [s[2] for s in select] + [w[0] for w in where] + group + [h[1] for h in having] + [o[1] for o in order]
            if c != "*"}
    tables = sorted(set(tables) | used)
    distinct = choices.yes(("distinct", b), sigmoid(g[GLOBAL[f"distinct{b}"][0]]))
    # A share of a whole: one aggregate shown, its conditions split into the part (the columns read as making it)
    # and the whole (the rest); when no condition reads as the part, all of them make it.
    share = []
    if (where and conj == "AND" and len(select) == 1 and select[0][1] != "plain"
            and choices.yes(("share", b), sigmoid(g[GLOBAL[f"share{b}"][0]]))):
        part = {c for c, p in columns.items() if c != "*" and field(p[b], "share_part")[0] >= 0.5}
        share = [i for i, w in enumerate(where) if w[0] in part or w[0].partition(":")[2] in part] or \
            list(range(len(where)))
    return {"tables": tables, "distinct": distinct, "select": select, "where": where, "conj": conj,
            "group": sorted(group), "having": having, "order": order, "dir": direction, "limit": limit,
            "share": share}


def unweighted(p, weight):
    """The probability a reading holds, from one trained with its yes weighed ``weight`` to 1: the odds divided by
    the weight."""
    return p / (p + weight * (1.0 - p)) if p < 1.0 else 1.0


def unweighted_outputs(output):
    """The outputs with every yes/no probability unweighted (slots are distributions and keep theirs)."""
    offset, width = LAYOUT["slot"]
    columns = {name: [[x if offset <= i < offset + width else unweighted(x, COLUMN_POS_WEIGHT)
                       for i, x in enumerate(block)] for block in blocks]
               for name, blocks in output["columns"].items()}
    tables = {name: [unweighted(x, TABLE_POS_WEIGHT) for x in p] for name, p in output["tables"].items()}
    return dict(output, columns=columns, tables=tables, unweighted=True)


def decode(output, example, graph, choices=None):
    """The readings the outputs say, taking each decision's best option unless ``choices`` overrides it."""
    choices = choices or Choices()
    if not output.get("unweighted"):
        output = unweighted_outputs(output)
    structure = choices.pick(("structure",), list(zip(STRUCTURES, softmax(output["globals"][:5]))))
    if structure == "single":
        return {"structure": "single", "blocks": [decode_block(output, example, graph, 0, False, choices)]}
    if structure in ("UNION", "INTERSECT", "EXCEPT"):
        return {"structure": structure, "blocks": [decode_block(output, example, graph, 0, False, choices),
                                                   decode_block(output, example, graph, 1, False, choices)]}
    outer = decode_block(output, example, graph, 0, True, choices)
    if not (any(w[2] == SUBQUERY for w in outer["where"]) or any(h[3] == SUBQUERY for h in outer["having"])):
        return {"structure": "single", "blocks": [outer]}
    return {"structure": "NESTED", "blocks": [outer, decode_block(output, example, graph, 1, False, choices)]}


FLOOR = 0.03


def candidates(output, example, graph, k=16, floor=FLOOR):
    """Up to k distinct readings, most probable first, as (log probability of the decisions they took, readings).
    Enumerated best-first over the uncertain decisions of the best decoding (an option other than the best has
    probability >= ``floor``); decisions a candidate reaches anew take their best option."""
    base = Choices()
    output = unweighted_outputs(output)
    found = {json.dumps(decode(output, example, graph, base), sort_keys=True): base.score()}
    keys = [key for key, options in base.seen.items() if len(options) > 1 and options[1][1] >= floor]
    costs = {key: [math.log(max(base.seen[key][0][1], 1e-6)) - math.log(max(option[1], 1e-6))
                   for option in base.seen[key]] for key in keys}
    heap = [(costs[key][1], ((i, 1),)) for i, key in enumerate(keys)]
    heapq.heapify(heap)
    visited = set()
    while heap and len(found) < k and len(visited) < 8 * k:
        cost, assignment = heapq.heappop(heap)
        if assignment in visited:
            continue
        visited.add(assignment)
        choices = Choices({keys[i]: rank for i, rank in assignment})
        readings = json.dumps(decode(output, example, graph, choices), sort_keys=True)
        found.setdefault(readings, choices.score())
        current = dict(assignment)
        for i, key in enumerate(keys):
            rank = current.get(i, 0) + 1
            if rank < len(costs[key]) and base.seen[key][rank][1] >= floor:
                following = dict(current)
                following[i] = rank
                heapq.heappush(heap, (cost - costs[key][rank - 1] + costs[key][rank],
                                      tuple(sorted(following.items()))))
    ranked = sorted(found.items(), key=lambda item: -item[1])
    return [(score, json.loads(readings)) for readings, score in ranked]
