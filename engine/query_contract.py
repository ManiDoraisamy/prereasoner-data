"""The checks a total or average shares across routes, and the coverage record of an answer.

``unreadable_operand`` finds the column a total or average names whose cells are not all numbers: compose, the
registered calculations and the world routes refuse it, naming the cells. ``Coverage`` is the record an answer
carries of whether its query reads the whole question: the readers' accepted reading certifies an own-data answer
(engine/tables.py), and the world routes' wording gate records its own (engine/knowledge_query.py).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import weakref

from engine.answer_presentation import unreadable_measure_reason
from engine.numeric import NUMBER_WORD


def lexical_words(text):
    """Keep Unicode names; punctuation in schema labels separates words."""
    return tuple(word.removesuffix("'s") for word in
                 re.findall(NUMBER_WORD + r"|[^\W_]+(?:'[^\W_]+)?", str(text).casefold(), re.UNICODE))


# Nouns that say how much of a measure there is: "search volume", "order value". After a word of the tables'
# names they name that measure's amount (KnowledgeQuery._uncovered).
QUANTITY_WORDS = frozenset({
    "volume", "volumes", "quantity", "quantities", "amount", "amounts", "value", "values", "level", "levels",
})


def unreadable_cells(values):
    """The filled cells of a column the question totals or averages that are not numbers, one entry per cell in row
    order, or None when every one is. How many there are does not matter: the operand is the column the question names
    (sql_rank.aggregate_operand), and a mostly malformed amount column was once replaced by a sum of populations
    because a share of bad cells decided whether it was a measure (planted-text review, 2026-10-08)."""
    from engine.numeric import parse_decimal

    bad = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        try:
            parse_decimal(text)
        except (TypeError, ValueError):
            bad.append(text)
    return bad or None


_UNREADABLE_OPERANDS = weakref.WeakKeyDictionary()


def unreadable_operand(question, graph):
    """The column a total or average names whose cells are not all numbers, as ``(name, cells, function)``, or None.
    The operand is the most fully named column ending the aggregate phrase (sql_rank.aggregate_operand): "the total
    estimated amount" totals a numeric `estimated amount` beside an `amount` holding a malformed cell. It is reported
    only when every column of that name is text and one holds a cell that is no number, so no route can total it, and
    none may total another column or count the rows in its place: beside a saved reference "total amount in France in
    US dollars" totaled the reference's `estimated amount`, and without one the world path counted the orders (the
    planted-text test in the browser, 2026-10-09). The column must hold numbers: one with no number at all (dates, a
    Yes/No status) is no measure a question can mean, and its name after the measure is a participle, not the
    operand ("total transfers signed in August" read the dates of `signed`; the release's dataset gate, 2026-10-09).
    Read once per question and graph: the planner asks it of every candidate."""
    memo = _UNREADABLE_OPERANDS.setdefault(graph, {})
    if question in memo:
        return memo[question]
    from engine.sql_rank import aggregate_operand

    columns = {}
    for column in graph.columns:
        columns.setdefault(column.ref.name, []).append(column)
    names = sorted(columns, key=lambda name: (-len(_name_vocabulary([name])), name))
    found = None
    for function in ("SUM", "AVG"):
        operand = aggregate_operand(question, names, function)
        if operand is None or any(column.ref.type.numeric for column in columns[operand]):
            continue
        for column in columns[operand]:
            filled = [value for value in column.values if value is not None and str(value).strip()]
            cells = unreadable_cells(filled)
            if cells and len(cells) < len(filled):
                found = (operand, cells, function)
                break
        if found:
            break
    memo[question] = found
    return found


def unreadable_measure_violation(question, graph):
    """The refusal for ``unreadable_operand``, or None. A sheet whose only amounts are text has no query to total them
    at all, and the reply said "no valid AST candidate" (planted-text test, 2026-10-08)."""
    found = unreadable_operand(question, graph)
    return unreadable_measure_reason(*found) if found else None


# The model label of a reply refusing ``unreadable_operand``, on the composed and world routes alike.
UNREADABLE_MEASURE_MODEL = "engine - clarify (the named measure is not numeric)"


@dataclass(frozen=True)
class Coverage:
    complete: bool
    violations: tuple[str, ...] = ()
    version: int = 1

    def record(self):
        return {"complete": self.complete, "violations": list(self.violations), "version": self.version}


def _name_vocabulary(names):
    """The canonical words of schema ``names``, as the question spells them and as the schema splits them:
    "LifeExpectancy" is "life" and "expectancy" (engine/sql_schema.name_words)."""
    from engine.sql_schema import canon, name_words
    return {canon(word) for name in names for word in (*lexical_words(name), *name_words(name))}
