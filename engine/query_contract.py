"""Shared request completeness policy for the deterministic planner.

Operator and literal evidence comes from the typed AST. Schema normalization is
separate from execution; unresolved meaning cannot certify a partial answer.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import weakref

from engine.answer_presentation import is_unreadable_measure_reason, unreadable_measure_reason
from engine.numeric import NUMBER_WORD


def lexical_words(text):
    """Keep Unicode names; punctuation in schema labels separates words."""
    return tuple(word.removesuffix("'s") for word in
                 re.findall(NUMBER_WORD + r"|[^\W_]+(?:'[^\W_]+)?", str(text).casefold(), re.UNICODE))


# Nouns that say how much of a measure there is: "search volume", "order value". After a word of the tables'
# names they name that measure's amount (KnowledgeQuery._uncovered); a question the search cannot read that
# asks one of them is offered the numeric columns (engine/tables.py, unread_clarification).
QUANTITY_WORDS = frozenset({
    "volume", "volumes", "quantity", "quantities", "amount", "amounts", "value", "values", "level", "levels",
})
# An aggregate word right before a field's name, in lexical_words form: "by total amount" ranks by a measure.
_MEASURE_WORDS = r" (?:total|sum of|average|avg|mean|maximum|max|minimum|min|number of|count of) "


_CELL_WORDS = weakref.WeakKeyDictionary()


def cell_words(graph):
    """The canonical words of every observed cell, built once per schema graph. Rebuilt for
    each candidate and each check, they cost seconds a question on a few thousand rows
    (2026-10-04)."""
    words = _CELL_WORDS.get(graph)
    if words is None:
        from engine.sql_schema import canon, distinct_values
        words = frozenset(canon(word) for column in graph.columns for value in distinct_values(column.values)
                          if value is not None for word in lexical_words(value))
        _CELL_WORDS[graph] = words
    return words


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


def explains_refusal(violation):
    """Whether a ``constraint_violations`` entry is written for the user, so a refusal can be its reply: a repeated
    field to choose, a field that cannot be computed, or a measure with cells that are not numbers."""
    return (violation.startswith(("Which repeated field", "The field ", "The requested total for "))
            or is_unreadable_measure_reason(violation))


@dataclass(frozen=True)
class Coverage:
    complete: bool
    violations: tuple[str, ...] = ()
    version: int = 1

    def record(self):
        return {"complete": self.complete, "violations": list(self.violations), "version": self.version}


def coverage(question, candidate, graph, *, calculation_satisfied=False):
    if candidate is None:
        return Coverage(False, ("no executable interpretation",))
    violations = constraint_violations(question, candidate.query, graph)
    if has_unread_terms(question, candidate, graph, calculation_satisfied=calculation_satisfied):
        violations += ("unresolved question wording",)
    return Coverage(not violations, tuple(dict.fromkeys(violations)))


def mandatory_predicates(predicate):
    """An OR branch cannot prove a constraint that must hold for every returned row."""
    from engine.sql_ast import BooleanExpr
    if isinstance(predicate, BooleanExpr):
        if predicate.operator == "AND":
            for term in predicate.terms:
                yield from mandatory_predicates(term)
    elif predicate is not None:
        yield predicate


def relational_operator_evidence(question, query, graph):
    """Consume relational instruction words only after the AST proves them.

    These are SQL operations, not aliases for business nouns. A field or observed
    value named ``Unique``/``Descending`` remains data. Missing or reversed
    operations must not be accepted merely by adding words to a noise list.
    """
    from engine.sql_ast import Aggregate, ColumnRef, SelectQuery, SubquerySource
    from engine.sql_expansion import ExpansionSupport, ordering_requested, tokens
    from engine.sql_schema import canon

    if not isinstance(query, SelectQuery):
        return frozenset(), ()
    text = ' '.join(lexical_words(question))
    instruction_words = set(lexical_words(text)) & {'distinct', 'different', 'unique',
                                                   'ascending', 'descending', 'asc', 'desc', 'table', 'tables'}
    if not instruction_words:
        return frozenset(), ()
    data_words = {canon(word) for column in graph.columns
                  for word in lexical_words(column.ref.name)} | cell_words(graph)
    requested_words = instruction_words - data_words
    consumed, violations = set(), []
    scope_words = requested_words & {'table', 'tables'}
    if scope_words:
        referenced = {query.from_table} if isinstance(query.from_table, str) else set()
        referenced.update(join.table for join in query.joins if isinstance(join.table, str))
        for table in graph.tables:
            label = ' '.join(lexical_words(table))
            if not label:
                continue
            if re.search(r'\b(?:tables?\s+'+re.escape(label)+'|'+re.escape(label)+r'\s+tables?)\b', text):
                if table in referenced:
                    consumed.update(scope_words)
                else:
                    violations.append('the requested table is not used: '+table)
    distinct_words = requested_words & {'distinct', 'different', 'unique'}
    if distinct_words:
        counted = [item.expression for item in query.select
                   if isinstance(item.expression, Aggregate)
                   and item.expression.function == 'COUNT']
        # Groups list each value once, the counts beside them included: "different citizenships of singers and the
        # number of singers of each" (Spider DEV 1012, 2026-10-12: refused for the count beside the groups).
        projected = [item.expression for item in query.select if not isinstance(item.expression, Aggregate)]
        grouped_projection = (bool(query.group_by) and bool(projected)
                              and all(isinstance(item, ColumnRef) and item in query.group_by
                                      for item in projected))
        # A count of the groups a FROM subquery keeps counts each group once: "how many different countries have
        # more than 1 car maker".
        counted_groups = bool(counted) and isinstance(query.from_table, SubquerySource) and bool(
            query.from_table.query.group_by)
        if (query.distinct or (counted and all(item.distinct for item in counted)) or grouped_projection
                or counted_groups):
            consumed.update(distinct_words)
        else:
            violations.append('requested distinct results are missing')
    direction_words = requested_words & {'ascending', 'descending', 'asc', 'desc'}
    if direction_words and ordering_requested(question):
        expected = {'ASC' if word in {'ascending', 'asc'} else 'DESC' for word in direction_words}
        if len(expected) != 1 or not query.order_by or query.order_by[0].direction not in expected:
            violations.append('requested ordering direction is missing or reversed')
        else:
            consumed.update(direction_words)
            consumed.add('order')
        # Match the explicitly named ordering target through the same schema
        # linker used by search, rather than mistaking a projected field for proof.
        match = re.search(r'\b(?:order(?:ed)?\s+by|order\s+of|sort(?:ed)?\s+by)\s+([^?!.;]+)', text)
        if match:
            named = {ref for _, ref in ExpansionSupport(graph).mentioned_columns(tokens(match[1]), False)}
            actual = query.order_by[0].expression if query.order_by else None
            actual = actual.operand if isinstance(actual, Aggregate) else actual
            if named and actual not in named:
                violations.append('requested ordering uses a different field')
    return frozenset(consumed), tuple(violations)


def _value_comparisons(predicates):
    """(column, value, folded, excludes) for each value the mandatory ``predicates`` compare a column with:
    column = value or column != value in either order, the column bare or read through LOWER (folded,
    so the value matches without case), and each literal of a column IN or NOT IN list."""
    from engine.sql_ast import ColumnRef, Comparison, InPredicate, Literal, Lower

    def column_of(expression):
        if isinstance(expression, ColumnRef):
            return expression, False
        if isinstance(expression, Lower) and isinstance(expression.operand, ColumnRef):
            return expression.operand, True
        return None, False

    for p in predicates:
        if isinstance(p, Comparison) and p.operator in {"=", "!=", "<>"}:
            left, right = (p.right, p.left) if isinstance(p.left, Literal) else (p.left, p.right)
            column, folded = column_of(left)
            if column is not None and isinstance(right, Literal):
                yield column, right.value, folded, p.operator != "="
        elif isinstance(p, InPredicate) and isinstance(p.source, tuple):
            column, folded = column_of(p.left)
            if column is not None:
                for item in p.source:
                    if isinstance(item, Literal):
                        yield column, item.value, folded, p.negated


def _select_branches(query):
    """The select queries a set query combines, in order; a select query is its own one branch."""
    from engine.sql_ast import SetQuery
    if isinstance(query, SetQuery):
        yield from _select_branches(query.left)
        yield from _select_branches(query.right)
    else:
        yield query


def _subtracts(query):
    """Whether ``query`` removes one branch's rows from another's (an EXCEPT anywhere in its set operations)."""
    from engine.sql_ast import SetQuery
    return isinstance(query, SetQuery) and (query.operator == "EXCEPT" or _subtracts(query.left)
                                            or _subtracts(query.right))


def _polar_branches(query, subtracted=False):
    """(select branch, subtracted) for each branch of ``query``: the right side of an EXCEPT is subtracted, so the
    values it keeps are the ones the whole query excludes."""
    from engine.sql_ast import SetQuery
    if isinstance(query, SetQuery):
        yield from _polar_branches(query.left, subtracted)
        yield from _polar_branches(query.right, not subtracted if query.operator == "EXCEPT" else subtracted)
    else:
        yield query, subtracted


def constraint_violations(question, query, graph):
    """What ``query`` leaves out of the question's constraints. A set query answers with each branch's rows, so each
    branch keeps the result's shape (its aggregates, projection, ordering and grain); what the question asks of the
    rows is met by the query as a whole: "a birth year before 1945 and after 1955" by an INTERSECT of the two
    comparisons, "people who do not play poker" by an EXCEPT of the players, a field the question names by either
    branch. Each branch once answered for the whole question, so every set-query answer to such a question was
    refused (Spider DEV, 2026-10-10)."""
    from engine.sql_ast import SelectQuery
    from engine.sql_expansion import population_presence_reading, population_presence_witness
    presence = population_presence_reading(question, graph)
    if presence is not None and not population_presence_witness(query, presence):
        return ("the requested common values require independently present populations",)
    branches = tuple(_select_branches(query))
    if not all(isinstance(branch, SelectQuery) for branch in branches):
        return ("unsupported interpretation scope",)
    # A count request must show its count, not merely use an aggregate in a filter or ordering. A numeric column
    # that already stores the requested count is totaled, using the operand reader's existing shared rule.
    uncounted = ()
    if _OPENS_WITH_COUNT.match(question):
        from engine.sql_expansion import counted_measure_columns
        schema = [{"table": column.ref.table, "name": column.ref.name,
                   "affinity": "REAL" if column.ref.type.numeric else "TEXT"} for column in graph.columns]
        quantities = frozenset((column["table"], column["name"])
                               for column in counted_measure_columns(question, schema))
        if not all(_shows_count(branch, quantities) for branch in branches):
            uncounted = ("the question asks how many, and the answer computes no count",)
    return tuple(dict.fromkeys(violation for branch in branches
                               for violation in _select_violations(question, branch, graph, query))) + uncounted


_OPENS_WITH_COUNT = re.compile(r"\s*(?:how\s+many|count)\b", re.I)


def _shows_count(query, quantities=frozenset()):
    """Whether an output computes COUNT or totals an explicitly requested stored-count column. Aggregates in
    WHERE/HAVING/order or FROM subqueries do not supply the output. Projected scalar queries read their own outputs."""
    from engine.sql_ast import Aggregate, ColumnRef, ScalarSubquery
    from dataclasses import fields, is_dataclass

    aliases = {join.alias: join.table for join in query.joins if join.alias}
    if query.from_alias and isinstance(query.from_table, str):
        aliases[query.from_alias] = query.from_table

    def found(value):
        if isinstance(value, Aggregate):
            operand = value.operand
            return value.function == "COUNT" or (
                value.function == "SUM" and isinstance(operand, ColumnRef)
                and (aliases.get(operand.table, operand.table), operand.name) in quantities)
        if isinstance(value, ScalarSubquery):
            return all(_shows_count(branch, quantities) for branch in _select_branches(value.query))
        if is_dataclass(value):
            return any(found(getattr(value, field.name)) for field in fields(value))
        if isinstance(value, (tuple, list)):
            return any(found(item) for item in value)
        return False

    return found(tuple(item.expression for item in query.select))


def _select_violations(question, query, graph, whole):
    """``constraint_violations`` for the select ``query``, one branch of the query ``whole`` (``query`` itself when
    it is not a set query)."""
    from engine.sql_ast import (
        Aggregate, ColumnRef, Comparison, DatePart, ExistsPredicate, InPredicate, SelectQuery, Star, SubquerySource,
    )
    from engine.sql_dates import realizes_dates, served_date_phrases
    from engine.sql_durations import duration_phrases, realizes_durations
    from engine.sql_expansion import ExpansionSupport, tokens
    from engine.sql_candidate import ScoredQuery

    violations = []
    # SQLite and PostgreSQL both accept SUM/AVG over some text expressions, but
    # do not agree on their coercion. More importantly, SQLite silently treats
    # arbitrary strings such as spreadsheet formula errors as zero. Do not let
    # a runnable aggregate turn malformed source cells into an authoritative
    # number; counts and other numeric columns remain available.
    from engine.sql_ast import column_refs, expression_type
    from dataclasses import fields, is_dataclass

    def aggregates(value):
        if isinstance(value, Aggregate):
            yield value
        if is_dataclass(value):
            for field in fields(value):
                yield from aggregates(getattr(value, field.name))
        elif isinstance(value, (tuple, list)):
            for item in value:
                yield from aggregates(item)

    for aggregate in dict.fromkeys(aggregates(query)):
        if aggregate.function in {"SUM", "AVG"} and not expression_type(aggregate.operand).numeric:
            field = getattr(aggregate.operand, "name", "selected measure")
            violations.append(f"The field {field!r} contains nonnumeric or ambiguous values and cannot be totaled")
    counts_groups = any(aggregate.function == "COUNT"
                        for aggregate in aggregates((query.select, query.order_by, query.having)))
    # Nor does a query answer a total of a named column that cannot be totaled by totaling another column or counting
    # the rows: beside a saved reference, "which city has the highest total amount" ranked the reference's estimated
    # amounts (unreadable_operand).
    unreadable = unreadable_measure_violation(question, graph)
    if unreadable:
        violations.append(unreadable)
    question_tokens = tokens(question)
    from engine.sql_expansion import count_preserves_population, counted_references, scalar_counted_table
    population = scalar_counted_table(query, question_tokens, graph)
    if population is not None and not count_preserves_population(query, population, graph):
        violations.append(f"the scalar count does not count rows of {population} at their own grain")
    for _, table in counted_references(query, question, graph)[:1]:
        violations.append(f"the count counts the {table} rows referring to what the question counts")
    phrases = served_date_phrases(question, question_tokens, graph)
    violations.extend(relational_operator_evidence(question, query, graph)[1])
    # The spreadsheet importer preserves duplicate headers with their original
    # column letters. Displaying both is safe; choosing one for a calculation or
    # filter needs the user to distinguish it, rather than an arbitrary model pick.
    used = set(column_refs(whole))
    projected = {item.expression for item in query.select if isinstance(item.expression, ColumnRef)}
    constrained = set(column_refs((query.where, query.having, query.group_by, query.order_by)))
    constrained.update(ref for item in query.select if not isinstance(item.expression, ColumnRef)
                       for ref in column_refs(item.expression))
    from engine.sql_schema import canon
    from engine.closed_class import closed_class_words
    question_text = ' ' + ' '.join(canon(word) for word in lexical_words(question)) + ' '
    noise = {canon(word) for word in closed_class_words(question)}
    table_labels = {' '.join(canon(word) for word in lexical_words(table)) for table in graph.tables}
    duplicate_groups = {}
    for column in graph.columns:
        match = re.fullmatch(r'(.*) \[column ([A-Z]+)\](?: \d+)?', column.ref.name)
        if match:
            duplicate_groups.setdefault((column.ref.table, match[1].casefold()), set()).add(column.ref)
    for (table, base), siblings in duplicate_groups.items():
        label = ' '.join(canon(word) for word in lexical_words(base))
        if table not in query.referenced_tables() or len(siblings) < 2 or label in table_labels or ' '+label+' ' not in question_text:
            continue
        explicit_position = any(re.search(r'\bcolumn\s+'+re.escape(re.search(r'\[column ([A-Z]+)\]', ref.name)[1])+r'\b', question, re.I)
                                for ref in siblings)
        if explicit_position:
            specified = {ref for ref in siblings if re.search(r'\bcolumn\s+'+re.escape(re.search(r'\[column ([A-Z]+)\]', ref.name)[1])+r'\b', question, re.I)}
            if not specified & used:
                violations.append('The requested field is not used: '+', '.join(sorted(ref.name for ref in specified)))
        if not explicit_position and not (siblings <= projected and not siblings & constrained):
            violations.append('Which repeated field should be used: '+', '.join(sorted(ref.name for ref in siblings))+'?')
    named_fields = {}
    from engine.closed_class import action_words
    from engine.sql_expansion import naming_context, naming_words
    context = naming_context(graph)
    commands = {canon(word) for word in action_words(question)}
    for column in graph.columns:
        words = tuple(canon(word) for word in lexical_words(column.ref.name))
        label = ' '.join(words)
        if label and label not in table_labels and not (len(words) == 1 and label in noise):
            named_fields.setdefault(label, set()).add(column.ref)
        # A field is asked by the words the search names it by too, its kind left unsaid: "the professional's id, role
        # and email" asks for role_code and email_address (Spider DEV 928, 929, 2026-10-13: the professionals' ids
        # alone passed). The question's command is no such word: "Return the most common first name" asks for no
        # return_date (held-out Spider train 156).
        said = ' '.join(naming_words(words, context.get(column.ref.table, frozenset())))
        if said and said != label and said not in table_labels and not (
                len(said.split()) == 1 and said in noise | commands):
            named_fields.setdefault(said, set()).add(column.ref)
    from dataclasses import replace
    from engine.sql_expansion import AGGREGATE_CUES, spelled_names
    from engine.sql_rank import analyze_question
    roles = analyze_question(question, graph)
    # An aggregate word that only spells a field's name ("Avg. monthly searches") names that
    # field when no other word asks for a calculation: either reading may answer, so neither is
    # required. Another word that does ask ("the total Avg. monthly searches") keeps its target,
    # which the field's full name links.
    spelled = spelled_names(roles.tokens, graph)
    if all(position in spelled for positions in roles.aggregate_positions.values() for position in positions):
        roles = replace(roles, aggregate_positions={}, aggregate_targets={}, count_requested=False)
    count_requested = roles.count_requested
    from engine.closed_class import recipient_classes
    recipients = {canon(word) for word in recipient_classes(question)}
    data_literals = cell_words(graph) if recipients else frozenset()
    def aggregate_functions(value):
        found = {value.function} if isinstance(value, Aggregate) else set()
        if is_dataclass(value):
            for field in fields(value):
                found.update(aggregate_functions(getattr(value, field.name)))
        elif isinstance(value, (tuple, list)):
            for item in value:
                found.update(aggregate_functions(item))
        return found
    def aggregate_columns(value):
        found = {}
        if isinstance(value, Aggregate):
            found.setdefault(value.function, set()).update(column_refs(value.operand))
        if is_dataclass(value):
            for field in fields(value):
                for function, refs in aggregate_columns(getattr(value, field.name)).items():
                    found.setdefault(function, set()).update(refs)
        elif isinstance(value, (tuple, list)):
            for item in value:
                for function, refs in aggregate_columns(item).items():
                    found.setdefault(function, set()).update(refs)
        return found
    # What the query computes: its answer, and the aggregates it compares to keep groups, in it or in the query it
    # reads FROM. "How many customers have a total amount over 10" totals the amounts in the HAVING its count reads
    # (the review of 2026-10-11).
    # An average the rows are compared with is computed too, where the question compares rows with their average:
    # "how many dogs have an age below the average" (sql_expansion.average_comparison; Spider DEV 974, 275, 2026-10-12:
    # refused for the average it compares with).
    from engine.sql_ast import ScalarSubquery
    from engine.sql_expansion import average_comparison
    compared = average_comparison(question) is not None
    computed = []
    source = query
    while isinstance(source, SelectQuery):
        computed.extend((source.select, source.having))
        if compared:
            computed.extend(comparison.right.query.select for comparison in _comparisons(source.where)
                            if isinstance(comparison.right, ScalarSubquery))
        source = source.from_table.query if isinstance(source.from_table, SubquerySource) else None
    measures = aggregate_columns(computed)
    if roles.aggregate_positions.get('SUM') and not count_requested and 'SUM' not in aggregate_functions(query.select):
        violations.append('A requested total needs a sum of values, not a row count or projection')
    counts_rows = count_requested and any(isinstance(item.expression, Aggregate)
        and item.expression.function == 'COUNT' and isinstance(item.expression.operand, Star)
        for item in query.select)
    ranked_grouping = (query.limit is not None and any(
        isinstance(term.expression, Aggregate) for term in query.order_by
    ))
    reached = graph.reachable(query.referenced_tables())
    # A field's name inside the longer name of a table the query reads names that table: "countries with more than
    # 2 car makers" reads car_makers, and asks for none of its Maker fields (Spider DEV 108, 2026-10-10).
    table_masked = question_text
    for table_label in sorted((' '.join(canon(word) for word in lexical_words(table))
                               for table in whole.referenced_tables()), key=len, reverse=True):
        if table_label:
            table_masked = table_masked.replace(' ' + table_label + ' ', ' ')
    question_words = set(question_text.split())
    every_measure = aggregate_columns(whole)
    # An aliased table's shown field is its table's: a self-join's "related"."name" shows Highschooler.name ("the names
    # of Kyle's friends", Spider DEV 890, 2026-10-12: refused for a name it showed).
    read_fields = used | _base_refs(whole)
    asked_tables = _asked_tables(question_tokens, graph)
    from engine.sql_rank import aggregate_operand
    # The fields an explicit list instruction asks for are read as the search built them and the ranker reads them
    # (sql_expansion.request_fields): "List the continent id, continent name ..." is realized by ContId, and its "id"
    # asks for no other table's Id (Spider DEV 89, 2026-10-14: the reading showing exactly the list was refused).
    from engine.sql_expansion import (output_requests, repeated_requests, request_fields, request_slots,
                                      tokens as list_tokens)
    said = list_tokens(question)
    requests = output_requests(question, graph)
    # Each output a list instruction names is shown: "Show names of technicians and the number of machines" is no
    # count of the technicians' names alone (held-out Spider train 138, 572, 2026-10-17: the field asked to be shown
    # was read only as the count's argument). One said again in the same words is asked once. Only a list of fields
    # and a count, or of fields each its own, is read so: values a list joins ("the continents Asia and Europe") are no
    # outputs (Spider DEV 726, 746, 2026-10-17), and "the claim date, settlement date" asks for two dates, not the
    # settlement date alone (held-out Spider train 920, fresh holdout 709, 2026-10-17).
    asked = {tuple(said[request.start:request.end]) for request in requests}
    listed = [request for request in requests if not request.count]
    listed_fields = request_fields(question, graph) if len(listed) > 1 else []
    each_a_field = (len(listed_fields) == len(listed) and all(listed_fields)
                    and len(set().union(*map(set, listed_fields))) >= len(listed))
    if (len(asked) > 1 and len(query.select) < len(asked)
            and (any(request.count for request in requests) and listed or each_a_field and len(asked) == len(listed))
            and not any(isinstance(item.expression, Star) for item in query.select)):
        violations.append(f"the answer shows {len(query.select)} of the {len(asked)} outputs the question lists")
    realized_items = [set(said[request.start:request.end])
                      for request, refs in zip(listed, request_fields(question, graph)) if set(refs) & projected]
    # An explicitly repeated, unambiguously bound field must be shown each time.
    # A set of read columns cannot prove multiple output occurrences; aliases
    # identify the same field but do not add an output of their own.
    # Only a request said again in the same words asks for another occurrence (sql_expansion.repeated_requests).
    from collections import Counter
    bound = [next(iter(slot)) if slot is not None and len(slot) == 1 else None
             for slot in request_slots(question, graph)]
    repeated = Counter({ref for ref in bound if ref is not None})
    repeated.update(ref for ref, earlier in zip(bound, repeated_requests(question, graph))
                    if ref is not None and earlier is not None and bound[earlier] == ref)
    aliases = {join.alias: join.table for join in query.joins if join.alias}
    if query.from_alias and isinstance(query.from_table, str):
        aliases[query.from_alias] = query.from_table
    shown = Counter(replace(item.expression, table=aliases.get(item.expression.table, item.expression.table))
                    for item in query.select if isinstance(item.expression, ColumnRef))
    for ref, required in repeated.items():
        if required > 1 and 0 < shown[ref] < required:
            violations.append(f"requested output occurrence is missing: {ref.name}")
    for label, refs in named_fields.items():
        if ' '+label+' ' not in table_masked:
            continue
        if any(set(label.split()) <= item for item in realized_items):
            continue
        # An aggregate word that is also a field's name (a stadium's "Average" attendance) is the aggregate when the
        # phrase it begins names another field the query aggregates that way: "singers above the average age"
        # averages Age (sql_rank.aggregate_operand), whatever the stadium table holds (Spider DEV 12, 2026-10-10).
        function = AGGREGATE_CUES.get(label)
        if function and aggregate_operand(question, [ref.name for ref in every_measure.get(function, ())], function):
            continue
        # A role linker can include an entity named in "top customers by total spend"
        # among the possible SUM targets. If the query correctly uses that entity as
        # its output grain, it is not a requested measure and must not make the
        # otherwise correct ranking look semantically incomplete.
        if ranked_grouping and refs & (projected | set(query.group_by)):
            continue
        requested_targets = {function: set(targets) for function, targets
                             in roles.aggregate_targets.items()}
        mismatched = [function for function, targets in requested_targets.items()
                      if refs & targets and not refs & measures.get(function, set())]
        if mismatched:
            named = sorted(refs, key=lambda ref: (ref.table, ref.name))[0]
            cells = unreadable_cells(next((column.values for column in graph.columns if column.ref == named), ()))
            if cells:
                violations.append(unreadable_measure_reason(named.name, cells, mismatched[0]))
            elif named.type.numeric:
                # A numeric field the query does not aggregate is a reading the search did not build, not a field to
                # repair: "the average distance and average price" was refused for nonnumeric price cells, all of them
                # numbers (review F4, 2026-10-11).
                violations.append(f"requested aggregate is missing: {mismatched[0]} of {named.name}")
            else:
                violations.append('The requested total for ' + repr(sorted(ref.name for ref in refs)[0])
                                  + ' could not be computed from that field; check for nonnumeric/error cells or '
                                  'choose a numeric field')
            continue
        if refs & read_fields:
            continue
        # A field's name is also part of a longer field's name the query uses when the question says that longer
        # name's other words: "the name and the release year of the song" asks for Song_Name and Song_release_year,
        # "pets whose age" for pet_age (Spider DEV 6 and 69). Not when a table the question names and the query
        # reads holds the named field itself, unless the longer field is in that table: "the name and location of
        # the stadiums which some concerts happened" asks for the stadiums' Name, not concert_Name (DEV 41,
        # 2026-10-10: any of the question's words once made it the longer field).
        # A longer name the question says together, in order, is that field whatever other table holds the shorter
        # one: "the first name and last name" of Owners beside a Dogs.name, "the average fastest lap speed" of
        # fastestLapSpeed beside lapTimes.lap, "the last names of authors" of lname (Spider DEV 930, held-out Spider
        # train 169, 727, 2026-10-12). Said apart, it is the field of the table the question names the asked field's
        # owner by, after "of" ("the name of the breed" of breed_name, "the names and ids of all countries" of
        # CountryName and CountryId), or of the rows it asks for (`_asked_tables`). A name the search splits further
        # (a camel-cased one) needs one of those: "at least 2 models" with "the id" said apart is no ModelId (Spider
        # DEV 175).
        from engine.sql_expansion import name_part_words
        from engine.sql_schema import name_words
        label_words = set(label.split())
        named_holders = {named.table for named in refs if named.table in whole.referenced_tables()
                         and ' ' + ' '.join(canon(word) for word in lexical_words(named.table)) + ' ' in question_text}
        # The owner the question names after "of" decides; the rows it asks for only where it names none: "the names
        # of the dogs of the rarest breed" asks for the dogs' names, not breed_name (Spider DEV 954, 2026-10-12).
        owners = _owner_tables(label.split(), question_tokens, graph) or asked_tables

        def longer(ref):
            words = _name_vocabulary((ref.name,))
            parts = name_part_words(ref.name) or tuple(canon(word) for word in name_words(ref.name))
            if label_words < set(parts) and ' ' + ' '.join(parts) + ' ' in question_text:
                return True
            if label_words < words and words <= question_words:
                return ref.table in owners or ref.table in {named.table for named in refs} or not named_holders
            return label_words < set(parts) and set(parts) <= question_words and ref.table in owners

        if any(longer(ref) for ref in read_fields):
            continue
        # "The names of orchestras" is the field named as the table the question names their owner by.
        if label_words <= {"name", "title"} and any(
                ref.table in owners and [canon(word) for word in name_words(ref.name)]
                == [canon(word) for word in name_words(ref.table)][-1:] for ref in read_fields):
            continue
        # "The full name" is a first and a last name the query reads, not a Name field of another table.
        if label_words == {"name"} and _full_name_read(list(question_tokens), read_fields):
            continue
        # "the total Amount" asks the total of Amount as well as naming a Total Amount field: a query that
        # aggregates the field the rest of the name names, with the aggregate its first word asks for, uses
        # what the question asked (near copies of a subscriptions export, 2026-10-04).
        cue, _, rest = label.partition(' ')
        if rest and cue in AGGREGATE_CUES and named_fields.get(rest, set()) & measures.get(AGGREGATE_CUES[cue], set()):
            continue
        if roles.aggregate_positions and label in recipients and label not in data_literals:
            continue
        # Counting a populated field in the base table is exactly COUNT(*).
        # This evidence is local data, not a learned guess about missing values.
        if counts_rows and any(ref.table == query.from_table and graph.column_map[(ref.table, ref.name)].values
            and all(value is not None and str(value).strip() for value in graph.column_map[(ref.table, ref.name)].values)
            for ref in refs):
            continue
        # A field that only tables the query cannot reach hold, named where the question counts rows, names
        # the rows counted: "how many subscriptions are there by Status" counts an export's rows, and the
        # report tabs' Subscriptions column joins none of them (2026-10-05).
        if counts_rows and not any(ref.table in reached for ref in refs):
            continue
        if not any(isinstance(item.expression, Star) for item in query.select):
            violations.append('The requested field is not used: '+sorted(ref.name for ref in refs)[0])
    # A field named after its table's name ("the highest savings balances") is that table's field: another table's
    # field of that name, from a table the question does not name, is not the one asked. Ordering accounts by their
    # checking balance answered "the three accounts with the highest savings balances" (held-out Spider train,
    # 2026-10-11).
    for label, refs in named_fields.items():
        said_with = {ref for ref in refs if ' ' + ' '.join(canon(word) for word in lexical_words(ref.table)) + ' '
                     + label + ' ' in question_text}
        others = {ref for ref in (refs & used) - said_with
                  if not set(canon(word) for word in lexical_words(ref.table)) <= question_words}
        if said_with and others:
            violations.append(f"the {label} asked is {sorted(said_with)[0].table}'s, not "
                              f"{sorted(others)[0].table}'s")
    for ref in sorted(used, key=lambda ref: (ref.table, ref.name)):
        match = re.fullmatch(r'(.*) \[column ([A-Z]+)\](?: \d+)?', ref.name)
        if not match:
            continue
        siblings = {column.ref for column in graph.columns if column.ref.table == ref.table
                    and re.fullmatch(re.escape(match[1]) + r' \[column [A-Z]+\]', column.ref.name, re.I)}
        if len(siblings) < 2 or (ref not in constrained and siblings <= projected):
            continue
        explicit = ' '.join(lexical_words(ref.name)) in ' '.join(lexical_words(question))
        explicit = explicit or bool(re.search(r'\bcolumn\s+'+re.escape(match[2])+r'\b', question, re.I))
        if not explicit:
            violations.append('Which repeated field should be used: '+', '.join(sorted(r.name for r in siblings))+'?')
    from engine.sql_expansion import complete_projection_requested
    if complete_projection_requested(question):
        projected = {item.expression for item in query.select if isinstance(item.expression, ColumnRef)}
        required = {column.ref for column in graph.columns if column.ref.table == query.from_table}
        if not any(isinstance(item.expression, Star) for item in query.select) and not required <= projected:
            violations.append('showing all rows must preserve every source column')
        # A superlative states its cutoff: "all information on the airport that has the largest number of
        # international passengers" keeps one row (held-out Spider train 297, 2026-10-12).
        from engine.sql_extrema import MAX_CUES, MIN_CUES
        if query.limit is not None and not set(question_tokens) & (MAX_CUES | MIN_CUES | {"top"}):
            violations.append('showing all rows must not impose an unstated row cutoff')
    # A text the question asks values to hold is a LIKE, not one value: a rewording that turned "all
    # inspection checklist" into the keyword 'inspection checklist' answered for one of 38 (2026-10-06).
    from engine.sql_search import realizes_substring, substring_requests
    for request in substring_requests(question, graph):
        if not realizes_substring(whole, request.text, request.subject):
            violations.append(f"the values holding {request.text!r} are not the ones compared")
    if phrases and not realizes_dates(query, phrases):
        violations.append("requested calendar constraint is missing or changed")
    elif phrases:
        named_dates = {column for _, column in ExpansionSupport(graph).mentioned_columns(question_tokens, False)
                       if column.type.value == 'date'}
        filtered_dates = {p.left.operand if isinstance(p.left, DatePart) else p.left
                          for p in mandatory_predicates(query.where) if isinstance(p, Comparison)}
        if named_dates and not named_dates & filtered_dates:
            violations.append("the calendar filter uses a different date column")
    durations = duration_phrases(question, question_tokens)
    if durations and not realizes_durations(query, durations):
        violations.append("requested duration is missing or changed")
    date_positions = frozenset(i for phrase in (*phrases, *durations) for i in range(phrase.start, phrase.end))
    requested = ExpansionSupport(graph).numeric_comparisons(question_tokens, date_positions)
    from engine.sql_expansion import counted_thresholds
    if any(item.operator != "=" for item in requested) or counted_thresholds(question_tokens, graph.tables):
        from engine.sql_ast import render_query
        member = ScoredQuery(whole, render_query(whole), 0.0, ())
        if not realizes_numeric_comparisons(question, member, graph, exclude_positions=date_positions):
            violations.append("requested comparison direction, value or operand is missing")
    elif _compares_another_field(question_tokens, whole, graph, date_positions):
        violations.append("requested comparison direction, value or operand is missing")
    actual = tuple(mandatory_predicates(query.where)) + tuple(mandatory_predicates(query.having))
    if re.search(r"\bnon[- ]empty\b", question, re.I):
        named = {column for _, column in ExpansionSupport(graph).mentioned_columns(question_tokens, False)}
        for ref in named:
            values = graph.column_map[(ref.table, ref.name)].values
            if not any(v is None or str(v).strip() == '' for v in values):
                continue
            counted = any(isinstance(item.expression, Aggregate) and item.expression.function == 'COUNT'
                          and item.expression.operand == ref for item in query.select)
            excludes_null = counted or any(isinstance(p, Comparison) and p.left == ref
                                           and p.operator == 'IS NOT' for p in actual)
            excludes_blank = not any(v is not None and str(v).strip() == '' for v in values) or any(
                isinstance(p, Comparison) and p.left == ref and p.operator in {'!=', '<>'}
                and getattr(p.right, 'value', None) == '' for p in actual)
            if not excludes_null or not excludes_blank:
                violations.append('non-empty row counting includes missing values')
    from engine.closed_class import EXCLUSION_CUES
    # The whole query makes the exclusions: a branch's own predicates, and, after an EXCEPT, the predicates of the
    # branch it subtracts, whose kept values are the ones excluded.
    polar = tuple((predicate, subtracted) for branch, subtracted in _polar_branches(whole)
                  for predicate in (*mandatory_predicates(branch.where), *mandatory_predicates(branch.having)))
    every = tuple(predicate for predicate, _ in polar)
    # A value the query compares is data, not an instruction: "Newsletter No" and "tasks that
    # are Not Started" filter on the cells 'No' and 'Not Started'. A cue outside those values
    # still needs its exclusion ("tasks that are not Done").
    cue_text = question
    for literal in sorted(_compared_texts(every), key=len, reverse=True):
        cue_text = re.sub(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", " ", cue_text, flags=re.I)
    if EXCLUSION_CUES.search(cue_text) and not re.search(r"\bnon[- ]empty\b", question, re.I):
        if not _subtracts(whole) and not any(
                (isinstance(p, Comparison) and p.operator in {"!=", "<>", "NOT LIKE", "IS NOT"})
                or (isinstance(p, (ExistsPredicate, InPredicate)) and p.negated) for p in every):
            violations.append("requested exclusion is missing")
    # The rows an exclusion removes are the ones the question names: a table joined to them that the question never
    # names, reads nothing from and connects nothing narrows them, and its rows' absence keeps what should go. "Templates
    # that are not used in any documents" subtracted only documents that have paragraphs (Spider DEV 316, 2026-10-10).
    question_words = {canon(word) for word in lexical_words(question)}
    for branch, scope in _select_queries(whole, graph=graph):
        if scope.negated and (table := _dangling_unnamed_join(branch, question_words)) is not None:
            violations.append(f"the exclusion is narrowed by {table}, which the question does not name")
    # An exclusion is the one the question makes: in "orders not Done in France", status != 'Done' with
    # country = 'France', and neither another field's exclusion nor the excluded value kept stands for it
    # (sql_search.value_polarity; a release review, 2026-10-07). A column is read through LOWER, whose
    # value matches without case, and on either side of the comparison; a literal IN or NOT IN list
    # keeps or excludes each value. A query that excludes through a negated subquery ("customers who
    # never bought X") makes its exclusion inside that subquery.
    from engine.sql_search import value_polarity
    kept, excluded_groups = value_polarity(question, graph)
    excluded = frozenset().union(*excluded_groups)
    made = set()
    for column, value, folded, excludes in (
            (column, value, folded, excludes != subtracted)
            for predicate, subtracted in polar
            for column, value, folded, excludes in _value_comparisons((predicate,))):
        def held(readings):
            return any(table == column.table and name == column.name
                       and (str(stated).casefold() == str(value).casefold() if folded else stated == value)
                       for table, name, stated in readings)
        if excludes:
            made |= {reading for reading in excluded if held((reading,))}
        if excludes and held(kept) and not held(excluded):
            violations.append(f"the query excludes {value!r}, which the question keeps")
        elif not excludes and held(excluded) and not held(kept):
            violations.append(f"the query keeps only {value!r}, which the question excludes")
    by_subquery = any(p.negated and (isinstance(p, ExistsPredicate) or not isinstance(p.source, tuple))
                      for p in every if isinstance(p, (ExistsPredicate, InPredicate)))
    for group in () if by_subquery else excluded_groups:
        if not group & made:
            violations.append(f"the query does not exclude {sorted(group)[0][2]!r}, which the question excludes")
    text = " ".join(lexical_words(question))
    for column in sorted(graph.columns, key=lambda c: -len(c.ref.name)):
        label = " ".join(lexical_words(column.ref.name))
        text = text.replace(label, " ")
        if label.startswith("avg "):
            text = text.replace("average " + label[4:], " ")
    aggregates = {item.expression.function for item in query.select if isinstance(item.expression, Aggregate)}
    # An extreme that selects the row holding it is realized as "the highest" is, by an ordering in its direction:
    # "the country with the maximum GNP" (sql_expansion.selects_extreme).
    from engine.sql_expansion import selected_extremes
    selecting = selected_extremes(said)
    for pattern, function in ((r"\b(?:maximum|max)\b", "MAX"), (r"\b(?:minimum|min)\b", "MIN"),
                              (r"\b(?:average|avg|mean)\b", "AVG")):
        if re.search(pattern, text) and function not in aggregates and function not in measures:
            if function in selecting:
                if not query.order_by or query.order_by[0].direction != selecting[function]:
                    violations.append("requested extremum or ordering is missing")
                continue
            violations.append("requested aggregate is missing: " + function)
    # Its direction is the field's (sql_extrema.superlative_direction): the highest rank is the smallest number.
    from engine.sql_extrema import superlative_direction
    for pattern, function, cue in ((r"\b(?:highest|largest)\b", "MAX", "highest"),
                                   (r"\b(?:lowest|smallest)\b", "MIN", "lowest")):
        if re.search(pattern, text) and function not in aggregates:
            first = query.order_by[0] if query.order_by else None
            direction = (superlative_direction(cue, first.expression) if first is not None
                         and isinstance(first.expression, ColumnRef) else "DESC" if function == "MAX" else "ASC")
            if first is None or first.direction != direction:
                violations.append("requested extremum or ordering is missing")
    # The rows a ranking keeps are read where the search reads them (sql_extrema.ranking_cutoff): "the top 3", "the two
    # people with the largest age".
    from engine.sql_extrema import MAX_CUES, MIN_CUES, ranking_cutoff
    ranks = tuple(index for index, token in enumerate(question_tokens)
                  if token in {"top", "bottom"} | MAX_CUES | MIN_CUES and index not in date_positions)
    cutoff, said = ranking_cutoff(question_tokens, ranks) if ranks else (1, None)
    if said is not None and said not in date_positions and (query.limit != cutoff or not query.order_by):
        violations.append("requested ranking cutoff is missing")
    # The rows a ranking keeps are shown, not only the field that ranks them: "top 2 products by price" listed two
    # prices. A field the question asks for before its cutoff is shown as asked ("the ages of the 3 oldest people").
    from engine.sql_extrema import ranked_table
    from engine.sql_expansion import semantic_tokens
    ranked = ranked_table(question_tokens, graph.tables)
    if ranked is not None:
        shown = [item.expression for item in query.select]
        ordered = {term.expression for term in query.order_by}
        asked = set(question_tokens[:ranked[1]])
        if shown and all(expression in ordered for expression in shown) and not any(
                set(semantic_tokens(ref.name)) & asked for ref in column_refs(tuple(shown))):
            violations.append(f"the ranked {ranked[0]} are not shown, only the field that ranks them")
    # "How many countries have more than 2 car makers" counts the countries a HAVING keeps: one number, not a count
    # in each country (the review of 2026-10-11: "how many customers have more than 2 orders" answered 3, one
    # customer's orders). A question asking for groups ("each", "per", "by", "for different") counts in each, and a
    # ranking keeps the groups it ranks.
    from engine.sql_expansion import groups_requested, top_level_count
    if (top_level_count(question_tokens) and query.group_by and query.having is not None and query.limit is None
            and not groups_requested(question)):
        violations.append("the question counts the groups that qualify, not the rows in each")
    # The rows the question asks for are the ones the answer shows: "what are airlines that have flights arriving at
    # airport 'AHD'" was answered with airport names, and "the flight numbers of flights arriving at APG" too (Spider
    # DEV 235, 249, 2026-10-12, once "arriving" was read; empty on Spider's data, wrong on any other). A table the
    # question's opening names must show a field of its own, of a table it is joined to by a key, or a key referring
    # to it. An aggregate answers about the rows it computes over.
    asked = _asked_tables(question_tokens, graph)
    if asked and isinstance(query.from_table, str) and not any(
            isinstance(item.expression, Aggregate) for item in query.select):
        aliases = {query.from_alias or query.from_table: query.from_table,
                   **{join.alias or join.table: join.table for join in query.joins}}
        shown = [(aliases.get(item.expression.table, item.expression.table), item.expression)
                 for item in query.select if isinstance(item.expression, ColumnRef)]
        linked = {(left.table, right.table) for key in graph.foreign_keys for left, right in key.column_pairs}
        linked |= {(right, left) for left, right in linked}
        if shown and not any(table in asked or any((table, wanted) in linked for wanted in asked)
                             for table, _ in shown):
            violations.append(f"the answer shows {shown[0][0]}'s fields, not the {sorted(asked)[0]} asked for")
    # A count grouped by the key of the one table it reads counts one row in each group, while the tables whose foreign
    # keys refer to that key hold its occurrences: "the transaction type code that occurs the most frequently" counted
    # each type's own row, and every type tied at 1 (held-out Spider train 370, 2026-10-11). A key that itself refers
    # to another table's rows is counted where it is: "the number of likes for each student id" counts the Likes rows
    # of each student (Spider DEV 902, 2026-10-12: refused for the Highschooler key Likes refers to).
    if (not query.joins and isinstance(query.from_table, str) and query.group_by
            and all(isinstance(group, ColumnRef) and group.table == query.from_table for group in query.group_by)
            and graph.identifies(query.group_by) and counts_groups):
        keys = {(group.table, group.name) for group in query.group_by}
        if any((parent.table, parent.name) in keys and child.table != query.from_table
               for key in graph.foreign_keys for child, parent in key.column_pairs):
            violations.append(f"each group counts one {query.from_table} row, not the rows its key joins")
    # A count of a group's own distinct values is 1 in every group, a key the joins equate with it included: "the
    # number of teachers from each hometown" was COUNT(DISTINCT Hometown) by Hometown, and "how many templates
    # correspond to each type code" counted the type code its group joins (Spider DEV 393, 330, 2026-10-12).
    if query.group_by:
        same = {(group.table, group.name) for group in query.group_by if isinstance(group, ColumnRef)}
        pairs = [pair for join in query.joins for pair in ((join.left, join.right), *join.additional)]
        grown = True
        while grown:
            grown = False
            for left, right in pairs:
                ends = {(left.table, left.name), (right.table, right.name)}
                if ends & same and not ends <= same:
                    same |= ends
                    grown = True
        if any(isinstance(item.expression, Aggregate) and item.expression.function == "COUNT"
               and item.expression.distinct and isinstance(item.expression.operand, ColumnRef)
               and (item.expression.operand.table, item.expression.operand.name) in same for item in query.select):
            violations.append("each group counts its own value once")
    # Explicitly named output grain must be represented, rather than just appearing
    # somewhere in the schema: a field named in full after "by/per/each", up to the clause's
    # end. "sorted by average Price" orders the groups and asks for no grain of its own, and a
    # name shared by two tables (a join key) is met by grouping either one. A field an aggregate
    # word takes is the measure the groups are ranked by, not a grain: "top 2 cities by total
    # amount" groups the cities, and only the queries also grouped by amount met it (an owner's
    # replay, 2026-10-10: "Lyon 200, Lyon 160" for Lyon 470, Paris 270).
    # A "by" naming who acted asks for no grain either (sql_expansion.by_groups): "How many car models were produced
    # by the maker with full name American Motor Company?" counts that maker's models (Spider DEV 147, 148,
    # 2026-10-17: every count was refused for not grouping by maker).
    lowered = question.casefold()
    grouped = set(query.group_by)
    from engine.sql_expansion import by_groups, word_spans, words
    spans = word_spans(question)
    starts = {span[0]: index for index, span in enumerate(spans)} if len(spans) == len(question_tokens) else {}
    for match in re.finditer(r"\b(?:by|per|each)\s+([^?!.;,]+)", lowered):
        if re.search(r"\b(?:sort|sorted|order|ordered|rank|ranked)\s+$", lowered[:match.start()]):
            continue
        if match[0].startswith("by") and match.start() in starts and not by_groups(
                question_tokens, words(question), starts[match.start()]):
            continue
        tail = re.split(r"\b(?:sort|sorted|order|ordered|rank|ranked)\s+by\b|\b(?:where|whose|having)\b",
                        match[1])[0]
        tail_words = " " + " ".join(lexical_words(tail)) + " "
        labels = {}
        for column in graph.columns:
            label = " ".join(lexical_words(column.ref.name))
            if label and " " + label + " " in tail_words and not re.search(
                    _MEASURE_WORDS + re.escape(label) + " ", tail_words):
                labels.setdefault(label, set()).add(column.ref)
        if aggregates and any(not refs & grouped for refs in labels.values()):
            violations.append("requested output grain is missing")
    return tuple(dict.fromkeys(violations))


def _compares_another_field(question_tokens, query, graph, skipped):
    """Whether ``query`` compares a number the question compares with a field it names just before, whose cells are not
    all numbers, with another field: "cars with horsepower more than 150" over Spider's text Horsepower was answered
    with the cylinders over 150 (Spider DEV 125, 2026-10-11). ``ExpansionSupport.numeric_comparisons`` reads only
    numeric fields, so such a comparison was not checked at all."""
    from engine.sql_ast import Aggregate, Literal
    from engine.sql_expansion import COPULAS, THRESHOLD_WORDS, ExpansionSupport, nearby_operator, parse_number
    named = ExpansionSupport(graph).mentioned_columns(question_tokens, numeric=False)
    for index, token in enumerate(question_tokens):
        value = parse_number(token)
        if value is None or index in skipped or nearby_operator(question_tokens, index) == "=":
            continue
        # The field's name ends where the comparison's words begin: "horsepower more than 150", not "customers have
        # more than 2 orders".
        end = index
        while end > 0 and question_tokens[end - 1] in THRESHOLD_WORDS | COPULAS:
            end -= 1
        said = {column for position, column in named if position == end - 1}
        if not said or any(column.type.numeric for column in said):
            continue
        for select, _ in _select_queries(query, graph=graph):
            for comparison in (*_comparisons(select.where, True), *_comparisons(select.having, True)):
                left = comparison.left.operand if isinstance(comparison.left, Aggregate) else comparison.left
                if (isinstance(comparison.right, Literal) and comparison.right.value == value
                        and left not in said):
                    return True
    return False


def _compared_texts(predicates):
    """The text values a query's mandatory predicates compare a column with."""
    from engine.sql_ast import Comparison, InPredicate, Literal
    for predicate in predicates:
        values = ()
        if isinstance(predicate, Comparison):
            values = (predicate.right,)
        elif isinstance(predicate, InPredicate) and not predicate.negated and isinstance(predicate.source, tuple):
            values = predicate.source
        for value in values:
            if isinstance(value, Literal) and isinstance(value.value, str) and value.value.strip():
                yield value.value.strip()


def has_unread_terms(question, candidate, graph, *, calculation_satisfied=False):
    """Ask the stateless rewriter when a runnable plan leaves wording out or returns every field.

    Matching one named column can produce executable SQL while ignoring another part of the request.
    Unmatched wording and a wildcard projection over a named field are bounded rewrite signals.
    """
    if candidate is None:
        return True
    reading = read_question(question, candidate, graph, calculation_satisfied=calculation_satisfied)
    return bool(reading.unread) or reading.wildcard


def _name_vocabulary(names):
    """The canonical words of schema ``names``, as the question spells them and as the search splits them:
    "LifeExpectancy" is "life" and "expectancy" (engine/sql_schema.name_words)."""
    from engine.sql_schema import canon, name_words
    return {canon(word) for name in names for word in (*lexical_words(name), *name_words(name))}


# The words of an instruction to order the rows ("sorted by", "in descending order"); "alphabetical" says more and is
# read apart (_orders_alphabetically).
_ORDERING_WORDS = frozenset({"order", "ordered", "sort", "sorted", "ascending", "descending", "increasing",
                             "decreasing"})


def _ordering_columns(node):
    """The (field, direction) pairs every ORDER BY in ``node`` orders by, subqueries included (a ranking keeps its
    order there)."""
    from dataclasses import fields, is_dataclass
    from engine.sql_ast import SelectQuery, column_refs
    found = set()
    if isinstance(node, SelectQuery):
        found.update((ref, term.direction) for term in node.order_by for ref in column_refs((term.expression,)))
    if is_dataclass(node):
        for part in fields(node):
            found |= _ordering_columns(getattr(node, part.name))
    elif isinstance(node, (tuple, list)):
        for item in node:
            found |= _ordering_columns(item)
    return found


def _orders_rows(query):
    """Whether ``query`` orders the rows it returns."""
    from engine.sql_ast import SetQuery
    if isinstance(query, SetQuery):
        return _orders_rows(query.left) or _orders_rows(query.right)
    return bool(getattr(query, "order_by", ()))


# Words that rank the rows, so a spelled number beside them is a cutoff: "the top three", "the two largest".
_RANKING_WORDS = frozenset({"top", "bottom", "first", "last", "most", "least", "highest", "lowest", "largest",
                            "smallest"})


def _compared_numbers(node):
    """The numbers ``node``'s comparisons test a column against, subqueries included (``Literal`` operands)."""
    from dataclasses import fields, is_dataclass
    from engine.sql_ast import Comparison, Literal
    found = set()
    if isinstance(node, Comparison):
        for side in (node.left, node.right):
            if isinstance(side, Literal) and isinstance(side.value, (int, float)) and not isinstance(side.value, bool):
                found.add(float(side.value))
    if is_dataclass(node):
        for field in fields(node):
            found |= _compared_numbers(getattr(node, field.name))
    elif isinstance(node, (tuple, list)):
        for item in node:
            found |= _compared_numbers(item)
    return found


def _number_read(question_tokens, index, value, query, ranked):
    """Whether ``query`` realizes the spelled number at ``question_tokens[index]`` in its role. After a comparison cue
    ("more than two", "at least three") it is a threshold, read by a comparison with that number; in a question that
    ranks ("the two oldest", "top three") it is the cutoff, read by the query's own LIMIT; otherwise either reads it.
    Any number anywhere in the SQL once read it, so "the two people with the largest Age, excluding Person_ID 2" was
    complete over a query keeping three rows (review, 2026-10-08)."""
    from engine.sql_expansion import threshold_number
    compared = float(value) in _compared_numbers(query)
    limited = getattr(query, "limit", None) == value
    if threshold_number(question_tokens, index):
        return compared
    return limited if ranked else (compared or limited)


def _one_relationship(query, graph, value_words, participle):
    """Whether each value ``query`` compares that the participle's object names (``value_words``) relates to the rows
    the way the participle says. A value has one relationship when the rows it selects are the same whichever column
    of the query's tables holds it, and no second foreign key joins its table to another table the query reads. With
    more than one, the ranker's travel reading decides (sql_rank.travel_direction: "departing" is the source): the
    compared column, or the key the query joins its table through, must carry the participle's direction. "flights
    departing from APG" read DestAirport = 'APG' as well as SourceAirport, since APG is in both (review, 2026-10-08).
    False when no compared value is named."""
    from engine.sql_ast import Comparison, ColumnRef, Join, Literal
    from engine.sql_rank import travel_column_role, travel_direction
    from engine.sql_schema import canon

    tables = query.referenced_tables()
    direction = travel_direction((canon(participle),))
    joins, comparisons, stack = [], [], [query]
    while stack:
        node = stack.pop()
        if isinstance(node, Join):
            joins.append(node)
        if isinstance(node, Comparison):
            comparisons.append(node)
        if hasattr(node, "__dataclass_fields__"):
            stack.extend(getattr(node, name) for name in node.__dataclass_fields__)
        elif isinstance(node, (tuple, list)):
            stack.extend(node)

    def held(value):
        """(table, rows) for each column of the query's tables holding ``value``: the rows it selects there."""
        wanted = str(value).strip().casefold()
        places = {}
        for column in graph.columns:
            if column.ref.table in tables:
                rows = frozenset(index for index, cell in enumerate(column.values)
                                 if cell is not None and str(cell).strip().casefold() == wanted)
                if rows:
                    places[column.ref] = (column.ref.table, rows)
        return places

    found = False
    for comparison in comparisons:
        sides = (comparison.left, comparison.right)
        column = next((side for side in sides if isinstance(side, ColumnRef)), None)
        literal = next((side for side in sides if isinstance(side, Literal) and isinstance(side.value, (str, int, float))
                        and not isinstance(side.value, bool)), None)
        if column is None or literal is None or not value_words & {
                canon(word) for word in lexical_words(str(literal.value))}:
            continue
        found = True
        selections = set(held(literal.value).values())
        keyed = any(len([key for key in graph.foreign_keys
                         if {table for pair in key.column_pairs for table in (pair[0].table, pair[1].table)}
                         == {column.table, other}]) > 1
                    for other in tables - {column.table})
        if len(selections) <= 1 and not keyed:
            continue
        relating = {column} | {side for join in joins for pair in join.predicates for side in pair
                               if column.table in {pair[0].table, pair[1].table} and side.table != column.table}
        roles = {travel_column_role(ref) for ref in relating} - {None}
        if direction is None or roles != {direction}:
            return False
    return found


def _orders_alphabetically(query, reverse):
    """Whether ``query`` orders its rows first by a text column, A to Z, or Z to A when ``reverse``. Any ordering once
    read "in alphabetical order": a list of names ordered by age, or descending (review, 2026-10-08)."""
    from engine.sql_ast import ColumnRef, SQLType
    terms = getattr(query, "order_by", ())
    if not terms:
        return False
    first = terms[0]
    return (isinstance(first.expression, ColumnRef) and first.expression.type == SQLType.TEXT
            and first.direction == ("DESC" if reverse else "ASC"))


@dataclass(frozen=True)
class Reading:
    """What one candidate makes of the question's words, lower case as the question spells them."""
    unread: tuple[str, ...]     # the words it does not read
    named: tuple[str, ...]      # the words it reads as a table, a column or a compared value
    wildcard: bool              # it returns every field where the question names one


def read_question(question, candidate, graph, *, calculation_satisfied=False):
    """The ``Reading`` of ``question`` by ``candidate``, a runnable pool member."""
    from engine.sql_ast import (
        Aggregate, ColumnRef, SelectQuery, SetQuery, SQLType, Star, SubquerySource, column_refs, share_aggregate,
    )
    from engine.sql_schema import canon
    from engine.closed_class import (
        action_words, closed_class_words, degree_words, measure_participles, number_words, value_participles,
    )
    from engine.sql_dates import served_date_phrases, realizes_dates
    from engine.sql_durations import duration_phrases, realizes_durations
    from engine.sql_expansion import ALPHABETICAL_WORDS, REVERSE_ORDER_WORDS, WORD_NUMBERS, tokens

    recognized_question = str(question)
    if calculation_satisfied:
        from engine.calculations import detect_calculations

        for intent in detect_calculations(question):
            recognized_question = re.sub(
                re.escape(intent.phrase), " ", recognized_question, count=1, flags=re.IGNORECASE,
            )
        recognized_question = re.sub(r"\b(?:after|before)\s+(?:subtracting|deducting|adding|applying)\b",
                                     " ", recognized_question, flags=re.I)
    phrases = served_date_phrases(question, tokens(question), graph)
    realized_dates = bool(phrases) and realizes_dates(candidate.query, phrases)
    date_words = {word for phrase in phrases for word in tokens(question)[phrase.start:phrase.end]} if realized_dates else set()
    durations = duration_phrases(question, tokens(question))
    duration_words = ({word for phrase in durations for word in tokens(question)[phrase.start:phrase.end]}
                      if durations and realizes_durations(candidate.query, durations) else set())
    spelled = tuple(word for word in lexical_words(recognized_question)
                    if word not in date_words and canon(word) not in duration_words)
    words = tuple(canon(word) for word in spelled)
    schema_words = _name_vocabulary(name for column in graph.columns for name in (column.ref.table, column.ref.name))
    # A schema word is read when it names a table the query reads or a column of one. Any table's columns once
    # counted, so a word was read because an unrelated table had a column of that name, while "the life
    # expectancy" was unread over the LifeExpectancy column the query read (Spider DEV, 2026-10-08). The tables
    # and the columns the query uses are read in the words the search splits their names into; the other columns
    # of its tables only in the words they are written with, so "official languages" is unread by a count that
    # leaves IsOfficial out.
    read_tables = candidate.query.referenced_tables()
    read_names = _name_vocabulary((*read_tables, *(column.name for column in column_refs(candidate.query))))
    read_names.update(canon(word) for column in graph.columns if column.ref.table in read_tables
                      for word in lexical_words(column.ref.name))
    sql_literals = {
        canon(word)
        for literal in re.findall(r"'((?:[^']|'')*)'", candidate.sql)
        for word in lexical_words(literal.replace("''", "'"))
    }
    sql_literals.update(
        canon(number)
        for number in re.findall(r"(?<![A-Za-z0-9_])[+-]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9_])", candidate.sql)
    )
    # A code word is read by a comparison on its bound field, never merely by a literal elsewhere in the SQL.
    # Sex='M' realizes "male"; Stage='M' does not, even if this schema also holds Sex codes. A place's word for what
    # is of it is read so too: "African" by Continent = 'Africa' (SchemaGraph._build_value_index).
    compared_codes = set()
    for select, _ in _select_queries(candidate.query, graph=graph):
        aliases = {join.alias: join.table for join in select.joins if join.alias}
        if select.from_alias and isinstance(select.from_table, str):
            aliases[select.from_alias] = select.from_table
        predicates = (*mandatory_predicates(select.where), *mandatory_predicates(select.having))
        for column, value, folded, _ in _value_comparisons(predicates):
            if isinstance(value, str):
                compared_codes.add((aliases.get(column.table, column.table), column.name,
                                    value.casefold() if folded else value, folded))
    sql_literals.update(word for word in set(words) if len(word) > 1 and any(
        isinstance(value, str)
        and ((column.table, column.name, value, False) in compared_codes
             or (column.table, column.name, value.casefold(), True) in compared_codes)
        for column, value in graph.value_index.get(word, ())))
    ordinary_words = {
        "a", "an", "am", "and", "are", "as", "at", "be", "been", "being", "by", "can", "could",
        "did", "do", "does", "for", "from", "give", "has", "have", "how", "in", "is", "it",
        "me", "of", "on", "or", "please", "show", "the", "there", "to", "what", "when", "where",
        "which", "who", "whom", "with", "would", "was", "were", "that", "all", "many", "much", "number", "total", "sum",
        "current", "row", "rows", "header", "headers", "blank", "empty", "non",
        "average", "avg", "mean", "maximum", "minimum", "max", "min", "count", "highest", "lowest", "largest", "smallest", "most",
        "least", "top", "bottom", "per", "each", "than", "more", "less", "greater", "above", "below",
        "before", "after", "between", "not", "no", "except", "excluding", "without", "month", "year",
        "day", "date", "value", "values", "column", "field", "data", "sheet", "spreadsheet", "tell",
        "find", "list", "get", "return", "display", "calculate", "compute", "bought", "ordered",
    }
    if realizes_numeric_comparisons(question, candidate, graph):
        ordinary_words.update({
            "over", "under", "above", "below", "greater", "less", "more", "fewer",
            "than", "least", "most", "at",
        })
    # "Equal to" is read by a query comparing for equality: "whose Country is equal to 'France'" (Spider DEV,
    # 2026-10-12: 42 of 55 Gemini rewordings refused for an unread word said "equal to").
    if _compares_for_equality(candidate.query):
        ordinary_words.add("equal")
    observed = cell_words(graph)
    ordinary_words.update(canon(word) for word in closed_class_words(question))
    ordinary_words.update(relational_operator_evidence(question, candidate.query, graph)[0])
    # "contain", "substring" or "all" asked for the text a LIKE compares: read once the query compares it.
    from engine.sql_search import realizes_substring, substring_requests
    ordinary_words.update(cue for request in substring_requests(question, graph)
                          if realizes_substring(candidate.query, request.text, request.subject)
                          for cue in request.cues)
    ordinary_words.update(canon(word) for word in action_words(question) if canon(word) not in observed)
    # The presence words of two populations' common values are read by that set ("living there").
    from engine.sql_expansion import population_presence_reading, population_presence_witness
    presence = population_presence_reading(question, graph)
    if presence is not None and population_presence_witness(candidate.query, presence):
        ordinary_words.update(tokens(question))
    # A verb is read by the field or table its doer names, where the query uses one: "players who won in both 2013
    # and 2016" by matches.winner_id, not by loser_id (Spider DEV 448, 451, 452, 2026-10-17: once "won" named no
    # surname 'Won', only the winner's reading was right). It never makes another word required.
    from engine.closed_class import agent_noun, verb_lemmas
    used_words = {canon(word) for name in (*read_tables, *(column.name for column in column_refs(candidate.query)))
                  for word in lexical_words(name)}
    ordinary_words.update(canon(verb) for verb, lemma in verb_lemmas(question).items()
                          if any(agent_noun(lemma, word) for word in used_words))
    # "Corresponding" pairs an output with another the answer shows: "the different document ids along with the number
    # of paragraphs corresponding to each", "the cost of each treatment and the corresponding treatment type
    # description" (Spider DEV 368, 882, 948, 2026-10-17: each right answer was refused for it). An answer showing one
    # output pairs nothing, so "the lowest version number, along with its corresponding template type code" over the
    # version alone stays refused (DEV 336, 848, 884). The word pairs an output only where it says one: "corresponding
    # to each" after it, or the words after it naming a field the answer shows or, for a count it shows, "number", or
    # "corresponding to" the table whose fields it shows ("the ids and details corresponding to projects", fresh holdout
    # 988); a misspelling ("corresonding", Spider DEV 841) is the word. "Two
    # or more corresponding books", "its corresponding problem id" over a product id and "corresponding to the
    # customer with the highest credit score" are read by no answer (held out 111, 223, 400, 430, 809, 844, 2026-10-17:
    # each was answered wrong once the word was read wherever two outputs were shown).
    selected = getattr(candidate.query, "select", ())
    if len(selected) >= 2:
        shown_words = {canon(word) for item in selected if isinstance(item.expression, ColumnRef)
                       for word in lexical_words(item.expression.name)}
        if any(isinstance(item.expression, Aggregate) and item.expression.function == "COUNT" for item in selected):
            shown_words |= {"number", "count"}
        shown_tables = {canon(word) for item in selected if isinstance(item.expression, ColumnRef)
                        for word in lexical_words(item.expression.table)}
        question_words = tokens(question)
        for index, word in enumerate(question_words):
            if word not in {"corresponding", "correspond"} and not _misspells(word, {"corresponding"}):
                continue
            after = [later for later in question_words[index + 1:index + 4] if later not in {"the", "its", "their"}]
            if (tuple(question_words[index + 1:index + 3]) in {("to", "each"), ("to", "it"), ("to", "them")}
                    or (after and after[0] in shown_words)
                    or (after[:1] == ["to"] and after[1:2] and after[1] in shown_tables)):
                ordinary_words.add(word)
    # The names of rows whose table holds no name but a title are their titles: "What are the names of all cartoons
    # directed by Ben Jones?" shows Cartoon.Title (Spider DEV 588, 2026-10-17: the right answer was refused for
    # "names"). A table that also holds a name field keeps "names" for it.
    from engine.sql_schema import name_words as _schema_name_words
    for column in column_refs(candidate.query):
        own = {canon(word) for word in _schema_name_words(column.name)}
        if own & {"title", "label"} and not any(
                "name" in {canon(word) for word in _schema_name_words(other.ref.name)}
                for other in graph.by_table.get(column.table, ())):
            ordinary_words.update({"name", "names"})
    # "Time" beside a date field's other words is that field's kind: "the distinct states and create time of all
    # votes" is VOTES.created (Spider DEV 693, 2026-10-17). It is read where the query reads a date field and no cell
    # says "time".
    if "time" not in observed and any(column.type == SQLType.DATE for column in column_refs(candidate.query)):
        ordinary_words.add("time")
    has_aggregate = any(isinstance(item.expression, Aggregate)
                        for item in getattr(candidate.query, 'select', ()))
    if any(isinstance(expression, Aggregate) and expression.function == 'COUNT'
           for expression in ([item.expression for item in getattr(candidate.query, 'select', ())]
                              + [term.expression for term in getattr(candidate.query, 'order_by', ())])):
        # The noun a count counts names the rows counted, on one table, whether the count is answered or orders
        # the groups ("which country has the most leads"). With several, only a field's name that
        # names none of the tables: "how many subscriptions by Status" counts the exports' rows, though a report
        # has a Subscriptions column. "How many people live in Gelderland" names no field, and a count of cities
        # is not its answer (Spider DEV 728, 2026-10-08).
        from engine.closed_class import counted_rows
        fields_named = _name_vocabulary(column.ref.name for column in graph.columns) - _name_vocabulary(graph.tables)
        ordinary_words.update(canon(word) for word in counted_rows(question)
                              if canon(word) not in observed
                              and (len(graph.tables) == 1 or canon(word) in fields_named))
    state_schema = any(set(lexical_words(c.ref.name)) & {"status", "state", "paid", "listed", "settled"}
                       for c in graph.columns)
    stateless = not state_schema and not observed & {"unpaid", "unlisted", "pending", "refunded", "settled"}
    # Where no field holds a listing or is named for a status, "listed" says the rows are in the data and asks no
    # condition: "How many transcripts are listed?", "the transcript with at least 2 courses listed", "each of the
    # listed directors" (Spider DEV 509, 552, 568, 627). An address's state is no listing: the transcripts were still
    # refused for the word beside a state_province_county field (2026-10-16).
    if not observed & {"listed", "unlisted"} and not any(
            {"listed", "unlisted", "listing", "status"} & set(lexical_words(c.ref.name)) for c in graph.columns):
        ordinary_words.add("listed")
    # So does "available" where no table or field is named for availability: "the files that are available in the
    # format of mp4", "How many available hotels are there in total?" (held-out Spider train 41, 443, 877, 2026-10-16:
    # each was refused for the word). Over a table Other_Available_Features the word names that table.
    if not observed & {"available", "unavailable"} and not any(
            {"available", "availability", "unavailable"} & set(lexical_words(name))
            for name in (*graph.tables, *(column.ref.name for column in graph.columns))):
        ordinary_words.add("available")
    # An ordering said from one end to the other is read by a query ordering its rows in that grade's direction: "Sort
    # all captain names by their ages from old to young" by an age descending, "in the order of their date of birth
    # from old to young" by a birth date ascending (held-out Spider train 924, 715; sql_extrema.span_grade). The
    # opposite direction leaves the words unread. "The results" a sort verb orders are the answer's rows: "sort the
    # results by the number of followers" (447, 842). "How old are they" and "the least number of results" still ask
    # for something.
    if _orders_rows(candidate.query):
        from engine.sql_extrema import span_grade, superlative_direction
        grade = span_grade(tokens(question))
        if grade and any(isinstance(term.expression, ColumnRef)
                         and term.direction == superlative_direction(grade, term.expression)
                         for select, _ in _select_queries(candidate.query, graph=graph) for term in select.order_by):
            ordinary_words.update({"old", "older", "oldest", "young", "younger", "youngest"})
        if re.search(r"\b(?:sort|sorted|order|ordered|rank|ranked)\s+(?:all\s+)?(?:the\s+|these\s+)?results?\b",
                     question, re.I):
            ordinary_words.add("result")
    # "Amount of" says a quantity as "number of" does, read by a query aggregating or ordering its rows where no field
    # of the tables it reads is an amount: "the total amount of bonus" is SUM(Bonus), "the smallest amount of
    # horsepower" the least Horsepower, "the greatest amount of languages" the most languages (Spider DEV 290, 96, 741,
    # 2026-10-15: each was refused for the word). "The total amount of payments" with an amount field is that field.
    if (re.search(r"\bamounts?\s+of\b", question, re.I) and (has_aggregate or _orders_rows(candidate.query))
            and not any("amount" in _name_vocabulary((column.ref.name,)) for column in graph.columns
                        if column.ref.table in read_tables)):
        ordinary_words.add("amount")
    if has_aggregate:
        ordinary_words.update(canon(word) for word in measure_participles(question, frozenset(schema_words))
                              if canon(word) not in observed)
        if stateless:
            if any(set(lexical_words(t)) & {"payments", "payment"} for t in graph.tables):
                if re.search(r"\bamount\s+paid\b", question, re.I):
                    ordinary_words.add("paid")
    if any(share_aggregate(item.expression, candidate.query) is not None
           for item in getattr(candidate.query, 'select', ())):
        ordinary_words.update({"share", "percentage", "percent"})
    # A naming participle only binds wording when its following literal is actually
    # tested by the query. It cannot stand in for an omitted state column or value.
    if re.search(r"\b(?:named|called)\s+", question, re.I) and sql_literals:
        ordinary_words.update({"named", "called"})
    # A name the question spells as two words: "high schoolers" is the Highschooler table it reads, and so is "the
    # high school student Kyle" (Spider DEV 887, 895, 896, 2026-10-12: refused for "high" and "school").
    ordinary_words.update(word for left, right in zip(words, words[1:])
                          if len(joined := canon(left + right)) >= 6 and any(name.startswith(joined) for name in read_names)
                          for word in (left, right))
    # A spelled number the query realizes in its role (_number_read): "the two oldest" keeps two rows, "at least
    # two votes" compares with 2. Every place the question says it must be realized.
    question_tokens = tokens(question)
    ranked = bool(set(question_tokens) & _RANKING_WORDS or degree_words(question))
    for word in number_words(question):
        places = [index for index, token in enumerate(question_tokens) if token == canon(word)]
        if word in WORD_NUMBERS and places and all(
                _number_read(question_tokens, index, WORD_NUMBERS[word], candidate.query, ranked) for index in places):
            ordinary_words.add(canon(word))
    # The words of an order instruction are read by a query that orders its rows: "sorted", "in descending order".
    # "Alphabetical" is read only by an order on a text field, in the direction asked: "in reverse alphabetical order"
    # is Z to A.
    # "All the information about hiring" and "all the details of employees" are read by a query showing every field
    # of a table it reads (held-out Spider train 147, 848, 2026-10-12).
    from engine.sql_expansion import complete_projection_requested
    if complete_projection_requested(question) and _shows_every_field(candidate.query, graph):
        ordinary_words.update({"information", "info", "detail", "data", "column", "field", "attribute"})
    if _orders_rows(candidate.query):
        ordinary_words.update(_ORDERING_WORDS)
        reverse = bool(set(words) & REVERSE_ORDER_WORDS)
        if set(words) & ALPHABETICAL_WORDS and _orders_alphabetically(candidate.query, reverse):
            ordinary_words.update(ALPHABETICAL_WORDS | (REVERSE_ORDER_WORDS if reverse else frozenset()))
        # A superlative's adjective is read by the field it measures where the query orders by it: "the most expensive
        # phone" by Price (held-out Spider train 110, 355, 956, 2026-10-17: each right answer was refused for
        # "expensive"; sql_extrema.SUPERLATIVE_DIMENSIONS).
        from engine.sql_extrema import MAX_CUES, MIN_CUES, SUPERLATIVE_DIMENSIONS, superlative_direction
        from engine.sql_expansion import semantic_tokens
        from engine.sql_schema import name_words
        # The field must be one of the rows the noun after it names, where it names a table: "the most recent
        # appointment" ordered by a prescription's date is not read. It is the adjective after "most" or "least",
        # ordered in that direction: "the least expensive invoices" is no descending total. A superlative of its own
        # ("the youngest") is read in its direction below (_realizes_grade).
        ordered = _ordering_columns(candidate.query)
        for index, word in enumerate(words):
            dimensions = SUPERLATIVE_DIMENSIONS.get(word, frozenset())
            cue = words[index - 1] if index and words[index - 1] in {"most", "least"} else None
            if not dimensions or cue is None or word in MAX_CUES or word in MIN_CUES:
                continue
            noun = set(words[index + 1:index + 4])
            owners = {table for table in graph.tables if {canon(part) for part in name_words(table)} & noun}
            if any(dimensions & set(semantic_tokens(column.name)) and (not owners or column.table in owners)
                   and superlative_direction(cue, column) == direction for column, direction in ordered):
                ordinary_words.add(word)
    # A graded word is read when the query realizes it at every place the question says it (_realizes_grade): "the
    # youngest singer" by an ascending age, "the most common citizenship" by groups ordered by their count.
    from engine.sql_extrema import MAX_CUES, MIN_CUES
    from engine.closed_class import COMPARATIVES
    for word in (set(question_tokens) & (MAX_CUES | MIN_CUES | set(COMPARATIVES) | _FREQUENCY_WORDS)) - ordinary_words:
        places = [index for index, token in enumerate(question_tokens) if token == word]
        if all(_realizes_grade(question_tokens, index, candidate.query, graph) for index in places):
            ordinary_words.add(canon(word))
    # "First" and "last" before no name or number grade a date as "earliest" and "latest" do: "the last transcript
    # release date", "the last date created of votes" (Spider DEV 569, 691, 2026-10-17: right answers refused for them).
    for word, grade in (("first", "earliest"), ("last", "latest")):
        places = [index for index, token in enumerate(question_tokens) if token == word]
        if places and word not in ordinary_words and all(
                not set(question_tokens[index + 1:index + 2]) & {"name", "names"}
                and not any(token.isdigit() for token in question_tokens[index + 1:index + 2])
                and _realizes_grade(question_tokens[:index] + (grade,) + question_tokens[index + 1:], index,
                                    candidate.query, graph) for index in places):
            ordinary_words.add(word)
    # A participle relating the rows to a value the query compares says how they relate: "flights departing from
    # APG" compares the source airport with 'APG' (Spider flight_2, 2026-10-08). Its object names a compared value
    # and nothing the query leaves out: "staff working for the museums" is unread over a query that averages the
    # opening year. One the data holds as a value ("orders returned by Alice" over a status holding 'Returned')
    # or names as a field is still to be read.
    for participle, target in value_participles(question):
        objects = {canon(word) for word in target} - ordinary_words
        if (objects & sql_literals and objects <= read_names | sql_literals
                and canon(participle) not in observed and canon(participle) not in schema_words
                and _one_relationship(candidate.query, graph, objects & sql_literals, participle)):
            ordinary_words.add(canon(participle))
    ordinary_words.update(_witnessed_words(question, words, candidate.query, graph, observed, schema_words,
                                           ordinary_words))
    # A word the language knows no use of, one letter from a word the query reads, is that word misspelled: "the
    # cartools that are on the TV Channel", "the battles that lost any 'Brig' type shipes" (Spider DEV 612, 498,
    # 2026-10-16: each correct reading was refused for the misspelling). A word the language knows is itself: "the
    # country of all appelations" is no County, nor "the course names" a field one letter from "name".
    read = read_names | sql_literals | ordinary_words
    ordinary_words.update(word for said, word in zip(spelled, words)
                          if word not in read and len(said) >= 5 and said.isalpha() and _misspells(word, read)
                          and _unknown_word(said))
    unread = tuple(dict.fromkeys(said for said, word in zip(spelled, words)
                                 if word not in read_names | sql_literals | ordinary_words))
    named = tuple(dict.fromkeys(said for said, word in zip(spelled, words)
                                if word in read_names | sql_literals and word not in ordinary_words))
    # A duration the query compares names the dates its span runs between: "how many users wanted neartail for
    # greater than 6 months" names data, and is asked about its other words, not read as another language.
    named += tuple(dict.fromkeys(word for word in lexical_words(recognized_question)
                                 if canon(word) in duration_words and word not in ordinary_words))
    def has_star(query):
        if isinstance(query, SetQuery):
            return has_star(query.left) or has_star(query.right)
        if not isinstance(query, SelectQuery):
            return False
        if isinstance(query.from_table, SubquerySource) and has_star(query.from_table.query):
            return True
        return any(isinstance(item.expression, Star) for item in query.select)

    table_words = _name_vocabulary(graph.tables)
    requested_fields = set(words) & (schema_words - table_words)
    from engine.sql_expansion import complete_projection_requested
    explicitly_all_fields = complete_projection_requested(question)
    return Reading(unread, named,
                   has_star(candidate.query) and bool(requested_fields) and not explicitly_all_fields)


def _unknown_word(said):
    """Whether the English vocabulary (engine/closed_class.spacy_model) holds no use of the spelled word ``said``."""
    from engine.closed_class import spacy_model
    return spacy_model().vocab[said.lower()].is_oov


def _misspells(word, read):
    """Whether ``word`` is one edit (a letter added, dropped or changed, or two neighbours swapped) from a word of
    ``read`` of four letters or more, or from its plural."""
    def one_edit(a, b):
        if a == b or abs(len(a) - len(b)) > 1:
            return False
        if len(a) == len(b):
            diff = [index for index in range(len(a)) if a[index] != b[index]]
            return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                      and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
        short, long = (a, b) if len(a) < len(b) else (b, a)
        return any(long[:index] + long[index + 1:] == short for index in range(len(long)))
    return any(one_edit(word, known) or one_edit(word, known + "s") for known in read
               if len(known) >= 4 and known.isalpha())


def _compares_for_equality(query):
    """Whether ``query`` compares a field with "=", "!=" or "<>" anywhere."""
    from engine.sql_ast import Comparison
    from dataclasses import fields, is_dataclass

    def found(value):
        if isinstance(value, Comparison) and value.operator in {"=", "!=", "<>"}:
            return True
        if is_dataclass(value):
            return any(found(getattr(value, field.name)) for field in fields(value))
        if isinstance(value, (tuple, list)):
            return any(found(item) for item in value)
        return False

    return found(query)


def _shows_every_field(query, graph):
    """Whether ``query`` shows every field of the table it reads first: all of them, or a star."""
    from engine.sql_ast import ColumnRef, SelectQuery, Star
    if not isinstance(query, SelectQuery):
        return False
    if any(isinstance(item.expression, Star) for item in query.select):
        return True
    shown = {item.expression for item in query.select if isinstance(item.expression, ColumnRef)}
    fields = {column.ref for column in graph.columns if column.ref.table == query.from_table}
    return bool(fields) and fields <= shown


# The words that end the opening of a question, where it says which rows it asks for: "what are airlines | that have
# flights", "which airlines | have departing flights", "the flight numbers of flights | arriving at".
_OPENING_STOPS = frozenset({"that", "who", "whom", "whose", "where", "with", "having", "has", "have", "had", "in",
                            "from", "at", "by", "for", "when", "if", "than", "and", "or"})
# The command a question may open with, which names no rows: "Show the record companies shared by orchestras" asks for
# no show (Spider DEV 856, 2026-10-12).
_OPENING_COMMANDS = frozenset({"find", "give", "list", "return", "show", "what", "which", "display", "tell", "name",
                               "count"})


def _asked_tables(question_tokens, graph):
    """The tables whose whole name the question's opening says (canon() forms), after a command it opens with and up
    to its first relative, condition or preposition word; a "which" after its first two words starts a relative clause:
    "the stadiums | which some concerts happened in". The projection window (sql_expansion.projection_window) is
    another reading: what the answer shows, from a later command on ("How many countries does each continent have?
    List the continent id"), where this one is the rows the question asks about."""
    from engine.sql_schema import canon, name_words
    opening = set()
    lead = 1 if question_tokens[:1] == ("please",) else 0
    for index, token in enumerate(question_tokens):
        if index < lead or (index == lead and token in _OPENING_COMMANDS):
            continue    # "Please show the record companies ..." asks for no show (Spider DEV 846, 848, 2026-10-14)
        if index and (token in _OPENING_STOPS or (token == "which" and index > 1)):
            break
        opening.add(token)
    asked = set()
    for table in graph.tables:
        name = {canon(word) for word in name_words(table)} - {"id"}
        if name and name <= opening:
            asked.add(table)
    return asked


def _witnessed_words(question, words, query, graph, observed, schema_words, already):
    """The words of ``question`` (``words``, canon() forms in order) that ``query`` realizes in what it reads, though no
    table, field or value of it spells them (Spider DEV, 2026-10-12: 173 right answers refused for one to four such
    words, 136 for one):

    - a word of travel direction, by the column of that direction: "flights departing from APG" by SourceAirport,
      "flights arriving at AHD" by DestAirport;
    - a name's part a field abbreviates: "the first name and gender" by Fname, "last name" by LName, and "the full
      name" by a first and a last name of one table;
    - "name" by a field named as its table is: "the airline names" by airlines.Airline, "the continent name" by
      continents.Continent;
    - a word the search names a read field by: "gender" by Sex, "manufacturer" by Maker;
    - a verb relating two tables the query reads, one named before it and the other after: "templates used by
      documents", "the conductor that has conducted the most orchestras";
    - a word sharing a stem of five letters or more with the name of a table or field the query reads: "the semester
      with no students enrolled" by Student_Enrolment, "the courses they are arranged to teach" by course_arrange,
      "countries that became independent" by IndepYear, "the most populated countries" by Population; a word of an
      operation the query must perform ("ordered", "listed", "counted") is not read so;
    - "one" standing for a row after a determiner: "the department description for the one whose name has the word
      computer", "how many car makers are there in each one";
    - a word saying how rows are counted, by a query counting rows (``_counts_rows``): "times" and "records" after a
      word of a count ("the most times", "at least two records"), and a verb a count follows after "by" by one that
      groups the rows it counts ("the hometowns shared by at least two teachers", "the record company used by the
      greatest number of orchestras");
    - "record" after the name of a table or field the query reads: "from the trip record".

    Spider DEV, 2026-10-12: the stem and "one" readings, with two-word names (read_question), read 22 more right
    answers and 8 wrong ones, each wrong for another reason (a field the question asks for left out, a count read as a
    listing).

    None is read where the data holds it as a value or a field is named for it: "orders returned by Alice" over a
    status holding 'Returned' still asks for that status."""
    from engine.closed_class import relation_verbs
    from engine.sql_ast import column_refs
    from engine.sql_rank import TRAVEL_WORDS, travel_column_role
    from engine.sql_schema import canon, name_words
    used = [ref for ref in column_refs(query) if hasattr(ref, "name")]
    out = set()
    for role in {travel_column_role(ref) for ref in used} - {None}:
        out.update(canon(word) for word in TRAVEL_WORDS[role])
    from engine.sql_expansion import name_part_words
    parts = {part[0] for ref in used if (part := name_part_words(ref.name))}
    for index, word in enumerate(words):
        if word in parts and all(
                "name" in words[place + 1:place + 5] for place, said in enumerate(words) if said == word):
            out.update({word, "name"})
    # "The full name" is read by a query showing a first and a last name of one table, whatever cell says "full".
    full_name = {"full", "name"} if _full_name_read(words, used) else set()
    if any([canon(part) for part in name_words(ref.name)] == [canon(part) for part in name_words(ref.table)][-1:]
           for ref in used):
        out.add("name")
    # A word that names a field the query reads by the search's own naming of fields (sql_expansion.column_matches):
    # "the gender of each student" is Sex (Spider DEV 81, held-out train 335, 2026-10-12).
    # A word a read table or field spells is named, not witnessed: "country" by countries.country stays a name the
    # clarification of an unread word offers fields beside.
    from engine.sql_expansion import column_matches
    tables = sorted(table for table in query.referenced_tables() if isinstance(table, str))
    spelled = {canon(part) for table in tables for part in name_words(table)}
    spelled |= {canon(part) for ref in used for part in name_words(ref.name)}
    out.update(word for word in set(words) - set(already) - spelled
               if any(column_matches(ref.name, {word}) for ref in used))
    # A table whose name writes two words together is named by them: "the official language spoken in the country"
    # names countrylanguage and country around "spoken" (Spider DEV 768, 769, 770, 2026-10-17).
    from engine.closed_class import compound_parts
    named = {table: ({canon(part) for part in name_words(table)}
                     | {canon(half) for part in name_words(table) for half in compound_parts(part)}) - {"id"}
             for table in tables}
    # So is one relating a table named before it to a field the query reads named just after it: "the professionals
    # living in a city", "TV Channel using language English" (Spider DEV 603, 971, 973, 2026-10-17: right answers
    # refused for the verb).
    field_words = [{canon(part) for part in name_words(ref.name)} - {"id"} for ref in used]
    for verb in relation_verbs(question):
        word = canon(verb)
        places = [index for index, said in enumerate(words) if said == word]
        if not places or word in already or word in observed or word in schema_words:
            continue
        if all(any(named[left] & set(words[max(0, place - 8):place]) and named[right] & set(words[place + 1:place + 9])
                   for left in tables for right in tables if left != right)
               or (any(named[left] & set(words[max(0, place - 8):place]) for left in tables)
                   and any(fields & set(words[place + 1:place + 5]) for fields in field_words if fields))
               for place in places):
            out.add(word)
    read_names = {canon(part) for table in tables for part in name_words(table)}
    read_names |= {canon(part) for ref in used for part in name_words(ref.name)}
    from engine.sql_expansion import entity_words, inflects, says_in_another_form
    entities = entity_words(graph)
    for word in set(words) - set(already):
        if any(says_in_another_form(word, name, entities) or inflects(word, name) for name in read_names):
            out.add(word)
    # Spider DEV 397, 852, 853 and held-out Spider train 404, 609, 667, 850, 892, 2026-10-12: right answers were refused
    # for these words.
    if _counts_rows(query):
        for word in {"time", "record"} & set(words):
            if all(set(words[max(0, place - 3):place]) & _COUNT_WORDS
                   for place, said in enumerate(words) if said == word):
                out.add(word)
        if any(source.group_by for source, _ in _select_queries(query)):
            for verb in relation_verbs(question):
                word = canon(verb)
                places = [place for place, said in enumerate(words) if said == word]
                if places and all(list(words[place + 1:place + 2]) == ["by"]
                                  and set(words[place + 2:place + 6]) & _COUNT_WORDS for place in places):
                    out.add(word)
    # A past participle before a year the query compares says how that year relates to the rows: "cars produced in
    # 1980", "models produced after 1980" (Spider DEV 145, 103, 104, 169, 2026-10-13: right answers refused for it).
    years = {number for number in _compared_numbers(query) if 1000 <= number <= 2999 and number == int(number)}
    for index, word in enumerate(words[:-2]):
        if word.endswith("ed") and word not in already and words[index + 1] in _YEAR_PREPOSITIONS and any(
                said.isdigit() and float(said) in years for said in words[index + 2:index + 5]):
            out.add(word)
    # "Of origin" after a field the query reads says that field: "the country of origin" is Country (Spider DEV 40, 640,
    # 2026-10-17: right answers refused for "origin").
    places = [index for index, said in enumerate(words) if said == "origin"]
    if places and all(place >= 2 and words[place - 1] == "of" and any(words[place - 2] in fields for fields in field_words)
                      for place in places):
        out.add("origin")
    # "Record" after the name of a table or field the query reads names its rows: "from the trip record", "teams that do
    # not have match season record" (held-out Spider train 288, 598, 806, 2026-10-12).
    places = [index for index, said in enumerate(words) if said == "record"]
    if places and all(place and words[place - 1] in read_names for place in places):
        out.add("record")
    places = [index for index, said in enumerate(words) if said == "one"]
    determiners = {canon(word) for word in _PRONOUN_DETERMINERS}
    if places and all(place and words[place - 1] in determiners for place in places):
        out.add("one")
    # A passive participle that only says the rows are there, after a copula whose subject the query reads or compares
    # and with nothing after it but "in the world": "How many different degrees are offered?", "the continents where
    # Chinese is spoken", "the languages spoken in the world" (Spider DEV 519, 520, 732, 750, 2026-10-17: right answers
    # refused for the participle). "The world" is every row there. One the data's names spell is that name: "How many
    # regions are affected?" names affected_region (held-out Spider train 601, 2026-10-17: served as COUNT(*) FROM region).
    from engine.sql_expansion import names_operation
    compared = {canon(part) for value in _compared_literal_texts(query) for part in lexical_words(value)}
    for index, word in enumerate(words):
        if (word in already or not (word.endswith("ed") or word in _IRREGULAR_PARTICIPLES)
                or names_operation(word) or word in schema_words):
            continue
        rest = list(words[index + 1:])
        world = rest[:3] in (["in", "the", "world"], ["around", "the", "world"])
        if rest and not world:
            continue
        subject = words[max(0, index - 3):index]
        if (index >= 2 and words[index - 1] in _COPULAS and words[index - 2] in read_names | compared) or (
                world and set(subject) & read_names):
            out.add(word)
            if world:
                out.add("world")
    # A noun right after the one-word name of a numeric field the query reads is what the field measures: "the average
    # number of gold medals" by Gold, "the minimum snatch score" by Snatch, "the average enrollment size" by Enrollment
    # (held-out Spider train 649, 717, 788, 864, 2026-10-17: right answers refused for the noun).
    measured = {name[0] for ref in used if getattr(getattr(ref, "type", None), "numeric", False)
                and len(name := [canon(part) for part in name_words(ref.name)]) == 1}
    for index, word in enumerate(words):
        if (index and words[index - 1] in measured and word.isalpha() and word not in already
                and word not in schema_words and word not in read_names):
            out.add(word)
    # "Single" is one: "not a single car maker" by an exclusion, "used by a single country" by a count of one (Spider DEV
    # 173, 759, 2026-10-17).
    places = [index for index, said in enumerate(words) if said == "single"]
    if places and all((list(words[max(0, place - 2):place]) == ["not", "a"] or list(words[place - 1:place]) == ["no"])
                      and _excludes(query) or list(words[place - 1:place]) == ["a"] and _counts_one(query)
                      for place in places):
        out.add("single")
    return (out - observed) | full_name


_COPULAS = frozenset({"is", "are", "was", "were", "be", "been"})
_IRREGULAR_PARTICIPLES = frozenset({"spoken", "written", "given", "taken", "known", "held", "made", "sold", "built",
                                    "shown", "seen", "taught", "won", "drawn", "grown", "run", "paid", "kept", "left"})


def _compared_literal_texts(node):
    """The text values ``node``'s comparisons test a column against, subqueries included."""
    from dataclasses import fields, is_dataclass
    from engine.sql_ast import Literal
    if isinstance(node, Literal):
        return {node.value} if isinstance(node.value, str) and node.value.strip() else set()
    found = set()
    if is_dataclass(node):
        for part in fields(node):
            found |= _compared_literal_texts(getattr(node, part.name))
    elif isinstance(node, (tuple, list)):
        for item in node:
            found |= _compared_literal_texts(item)
    return found


def _excludes(node):
    """Whether ``node`` removes rows: an EXCEPT, or a negated membership or existence test, anywhere in it."""
    from dataclasses import fields, is_dataclass
    from engine.sql_ast import ExistsPredicate, InPredicate
    if _subtracts(node) or (isinstance(node, (ExistsPredicate, InPredicate)) and node.negated):
        return True
    if is_dataclass(node):
        return any(_excludes(getattr(node, part.name)) for part in fields(node))
    if isinstance(node, (tuple, list)):
        return any(_excludes(item) for item in node)
    return False


def _counts_one(node):
    """Whether ``node`` compares a count of rows with one: HAVING COUNT(*) = 1."""
    from dataclasses import fields, is_dataclass
    from engine.sql_ast import Aggregate, Comparison, Literal
    if (isinstance(node, Comparison) and node.operator == "=" and isinstance(node.left, Aggregate)
            and node.left.function == "COUNT" and isinstance(node.right, Literal) and node.right.value == 1):
        return True
    if is_dataclass(node):
        return any(_counts_one(getattr(node, part.name)) for part in fields(node))
    if isinstance(node, (tuple, list)):
        return any(_counts_one(item) for item in node)
    return False


def _base_refs(query):
    """The fields ``query`` shows from an aliased table, as its table's: a self-join's "related"."name" shows
    Highschooler.name. A field an alias only joins on or compares is not counted: "owner"."ID" joining Kyle to his
    friends answers no "id" (Spider DEV 872, 2026-10-12)."""
    from dataclasses import replace
    from engine.sql_ast import ColumnRef, SelectQuery, column_refs
    out = set()
    for select, _ in _select_queries(query):
        if not isinstance(select, SelectQuery):
            continue
        aliases = {join.alias: join.table for join in select.joins if join.alias}
        if select.from_alias and isinstance(select.from_table, str):
            aliases[select.from_alias] = select.from_table
        out.update(replace(ref, table=aliases[ref.table]) for item in select.select
                   for ref in column_refs(item.expression) if isinstance(ref, ColumnRef) and ref.table in aliases)
    return out


def _owner_tables(label, question_tokens, graph):
    """The tables the question names as the owner of the field ``label`` (canon() words) names, after the "of" that
    follows it, past the fields coordinated with it: "the name of the breed", "the names and ids of all countries"."""
    from engine.sql_schema import canon, name_words
    tokens = list(question_tokens)
    size = len(label)
    stops = {"that", "which", "who", "whose", "where", "with", "for", "in", "by", "from", "than"}
    determiners = {"the", "all", "each", "every", "a", "an", "its", "their", "his", "her", "these", "those"}
    owners = set()
    for start in range(len(tokens) - size + 1):
        if tokens[start:start + size] != list(label):
            continue
        after = start + size
        of = next((index for index in range(after, min(len(tokens), after + 6)) if tokens[index] == "of"), None)
        if of is None or stops & set(tokens[after:of]):
            continue
        rest = [token for token in tokens[of + 1:of + 6] if token not in determiners]
        for table in graph.tables:
            words = [canon(word) for word in name_words(table)]
            if words and (rest[:len(words)] == words or rest[:1] == words[-1:]):
                owners.add(table)
    return owners


def _full_name_read(words, used):
    """Whether every "full" of the question (canon() forms) says "full name", and the fields ``used`` hold a first
    and a last name of one table: "the full names of employees" read by FirstName and LastName (held-out Spider train
    474, 2026-10-12)."""
    from engine.sql_expansion import name_part_words
    places = [index for index, word in enumerate(words) if word == "full"]
    if not places or not all(list(words[place + 1:place + 2]) == ["name"] for place in places):
        return False
    held: dict[str, set[str]] = {}
    for ref in used:
        if (part := name_part_words(ref.name)):
            held.setdefault(ref.table, set()).add(part[0])
    return any({"first", "last"} <= parts for parts in held.values())


# The words that put a year after a participle: "produced in 1980", "built before 1990".
_YEAR_PREPOSITIONS = frozenset({"in", "before", "after", "since", "until", "from"})
# The words before a "one" that make it a pronoun for a row: "the one whose", "each one".
_PRONOUN_DETERMINERS = frozenset({"the", "each", "this", "that", "which", "every"})
# Words of a count before the noun or after the "by" they count (canon() forms): "the most times", "at least two
# records", "shared by the greatest number of".
_COUNT_WORDS = frozenset({"most", "fewest", "least", "many", "number", "greatest", "largest", "smallest", "more", "less",
                          "once", "twice", "two", "three", "four", "five", "2", "3", "4", "5"})


def _counts_rows(query):
    """Whether ``query`` counts rows in what it shows, orders or keeps: a COUNT(*) or a count of a field's values,
    not of its distinct ones."""
    from engine.sql_ast import Aggregate
    from dataclasses import fields, is_dataclass

    def counts(value):
        if isinstance(value, Aggregate) and value.function == "COUNT" and not value.distinct:
            return True
        if is_dataclass(value):
            return any(counts(getattr(value, field.name)) for field in fields(value))
        if isinstance(value, (tuple, list)):
            return any(counts(item) for item in value)
        return False

    return any(counts((source.select, source.order_by, source.having)) for source, _ in _select_queries(query))


def _comparisons(predicate, alternatives=False):
    """The comparisons ``predicate`` makes every returned row meet, and those of its OR branches when the question
    offers ``alternatives``."""
    from engine.sql_ast import BooleanExpr, Comparison
    if isinstance(predicate, Comparison):
        return [predicate]
    if isinstance(predicate, BooleanExpr) and (predicate.operator == "AND" or alternatives):
        return [item for term in predicate.terms for item in _comparisons(term, alternatives)]
    return []


@dataclass(frozen=True)
class _Scope:
    """Where a select query sits in the query it is part of. ``alternative``: in a UNION branch, so its rows are one
    alternative of the answer; ``negated``: subtracted (the right of an EXCEPT, a NOT IN or NOT EXISTS subquery, an
    odd number of times), so its rows are the ones excluded; ``scalar``: in a scalar subquery, which computes a value
    and keeps no row; ``unbound``: in a membership test that ties no outer row to its rows (``_binds``), which keeps
    every row or none whatever each row holds."""
    alternative: bool = False
    negated: bool = False
    scalar: bool = False
    unbound: bool = False


def _tested_subqueries(predicate):
    """(query, scalar, negated, operand) for each query a predicate tests rows against: IN and EXISTS test membership,
    negated as NOT IN and NOT EXISTS, IN of its ``operand`` (None for EXISTS and a scalar subquery); a scalar subquery
    computes a value."""
    from engine.sql_ast import BooleanExpr, Comparison, ExistsPredicate, InPredicate, ScalarSubquery
    if isinstance(predicate, BooleanExpr):
        for term in predicate.terms:
            yield from _tested_subqueries(term)
    elif isinstance(predicate, InPredicate) and not isinstance(predicate.source, tuple):
        yield predicate.source, False, predicate.negated, predicate.left
    elif isinstance(predicate, ExistsPredicate):
        yield predicate.query, False, predicate.negated, None
    elif isinstance(predicate, Comparison):
        for side in (predicate.left, predicate.right):
            if isinstance(side, ScalarSubquery):
                yield side.query, True, False, None


def _binds(operand, inner, graph, extremum=False):
    """Whether a membership test ties the outer row to ``inner``'s rows: an IN compares its ``operand`` with values of
    its own kind, and an EXISTS (``operand`` None) refers to the outer row. "People whose ID is IN the ages over 35" and
    "people such that someone is over 35" keep rows whatever their own age, and both were read as "people whose age is
    above 35" (review F3, 2026-10-11). Values of the operand's kind are its own column, one a foreign key pairs with
    it, one of the same name, or one named for the operand's table and the operand ("visitor_ID" for
    "visitor"."ID"); a shared word alone is not ("Manager_ID" for "Person_ID"), nor a computed value ("Age - 39"). A
    set of queries binds when each of its branches does (the review of 2026-10-11, R2). A largest or smallest value
    keeps one row of each group: it binds the rows it keeps only where the question asks for an extremum
    (``extremum``) or each group is one row; "people whose age is above 35" kept only the one with the largest id
    (the review of 6263502, S4)."""
    from engine.sql_ast import Aggregate, ColumnRef, SelectQuery, SetQuery, column_refs
    from engine.sql_schema import canon, name_words
    if isinstance(inner, SetQuery):
        return _binds(operand, inner.left, graph, extremum) and _binds(operand, inner.right, graph, extremum)
    if not isinstance(inner, SelectQuery):
        return False
    if operand is None:
        own = {inner.from_alias, inner.from_table if isinstance(inner.from_table, str) else None} | {
            name for join in inner.joins for name in (join.alias, join.table)}
        return any(ref.table not in own for ref in column_refs((inner.where, inner.having)))
    projected = inner.select[0].expression if len(inner.select) == 1 else None
    if isinstance(projected, Aggregate) and projected.function in {"MAX", "MIN"}:
        if not extremum and not (inner.group_by and all(isinstance(group, ColumnRef) for group in inner.group_by)
                                 and graph is not None and graph.identifies(inner.group_by)):
            return False
        projected = projected.operand
    if not isinstance(operand, ColumnRef) or not isinstance(projected, ColumnRef):
        return False

    def words(name):
        return [canon(word) for word in name_words(name)]

    pair = {(operand.table, operand.name), (projected.table, projected.name)}
    return (len(pair) == 1 or operand.name.casefold() == projected.name.casefold()
            or any(pair == {(left.table, left.name), (right.table, right.name)}
                   for key in getattr(graph, "foreign_keys", ()) for left, right in key.column_pairs)
            or any(words(mine.name) == words(other.table) + words(other.name) and words(other.name)
                   for mine, other in ((projected, operand), (operand, projected))))


def _select_queries(query, scope=_Scope(), graph=None, extremum=False):
    """(select query, ``_Scope``) for every select query in ``query``: set branches, FROM subqueries and the
    subqueries its predicates test, so "visitors who did not visit any museum opened after 2010" is realized inside
    its NOT IN. Each keeps where it sits: a comparison anywhere in the query once realized the question's, so "above
    35 and below 50" was met by a UNION and by an EXCEPT of the two, and "people above 35" by a scalar subquery
    comparing their ids (review of 2026-10-10). ``extremum``: the question asks for one (``_binds``)."""
    from dataclasses import replace
    from engine.sql_ast import SelectQuery, SetQuery, SubquerySource
    if isinstance(query, SetQuery):
        branch = replace(scope, alternative=scope.alternative or query.operator == "UNION")
        yield from _select_queries(query.left, branch, graph, extremum)
        yield from _select_queries(query.right, replace(branch, negated=branch.negated != (query.operator == "EXCEPT")),
                                   graph, extremum)
    elif isinstance(query, SelectQuery):
        yield query, scope
        if isinstance(query.from_table, SubquerySource):
            yield from _select_queries(query.from_table.query, scope, graph, extremum)
        for predicate in (query.where, query.having):
            for nested, scalar, negated, operand in _tested_subqueries(predicate):
                unbound = not scalar and not _binds(operand, nested, graph, extremum)
                yield from _select_queries(nested, replace(scope, negated=scope.negated != negated,
                                                           scalar=scope.scalar or scalar,
                                                           unbound=scope.unbound or unbound), graph, extremum)


def _dangling_unnamed_join(query, question_words):
    """A table ``query`` joins only to filter that the question does not name: no column of it is read outside the
    joins, it is joined to one other table (a leaf, connecting nothing), and no word of its name is in
    ``question_words``. None when every joined table is named, read or a bridge."""
    from engine.sql_ast import column_refs
    from engine.sql_schema import canon, name_words
    joins = getattr(query, "joins", ())
    if not joins:
        return None
    pairs = [(join.left, join.right) for join in joins] + [pair for join in joins for pair in join.additional]
    outside = {ref.table for ref in column_refs((query.select, query.where, query.group_by, query.having,
                                                 query.order_by))}
    for join in joins:
        table = join.table
        neighbours = {side.table for left, right in pairs if table in {left.table, right.table}
                      for side in (left, right)} - {table}
        if (table not in outside and len(neighbours) == 1 and isinstance(table, str)
                and not {canon(word) for word in name_words(table)} & question_words):
            return table
    return None


def _scoped_match(scope, operator, wanted, *, alternatives, negated):
    """Whether a comparison with ``operator`` where ``scope`` says realizes the question's ``wanted`` operator on the
    rows returned. A scalar subquery filters none of them; a UNION branch realizes only an alternative the question
    offers; a subtracted comparison realizes its complement ("people not older than 35" are everyone EXCEPT those over
    35), or the operator itself when the question ``negated`` it before ("visitors who did not visit any museum opened
    after 2010" are the visitors NOT IN those visits)."""
    from engine.sql_expansion import COMPLEMENTS
    if scope.scalar or scope.unbound or (scope.alternative and not alternatives):
        return False
    if scope.negated:
        return COMPLEMENTS.get(operator) == wanted or (negated and operator == wanted)
    return operator == wanted


# The words after "most" or "least" that grade by how often: "the most common citizenship".
_FREQUENCY_WORDS = frozenset({"common", "frequent", "frequently"})


def _realizes_grade(question_tokens, index, query, graph=None):
    """Whether ``query`` realizes the graded word at ``question_tokens[index]`` in its direction, on a field of the
    kind it grades. A comparative before "than" is a comparison with its operator (sql_expansion.comparative_operator,
    so "not higher than 4" is at most 4), on the measure it describes when it names one ("pets older than 1" compares
    an age with >). A frequency superlative orders groups by their count
    ("the most common citizenship", "the fewest paragraphs"). Any other superlative orders by, or takes the maximum or
    minimum of, a field in its direction (sql_extrema.superlative_direction): "the youngest singer" is the least
    age or the latest birth date, "the latest date" a date. The end of a range ("from the oldest to the youngest") is
    read with its start. A rule that read graded words without their direction once served 27 wrong answers for 19
    right (spider/results/RESULTS.md, 2026-10-08)."""
    from engine.sql_ast import Aggregate, ColumnRef, SQLType
    from engine.sql_expansion import semantic_tokens
    from engine.sql_extrema import MAX_CUES, MIN_CUES, frequency_cue, superlative_direction
    from engine.closed_class import COMPARATIVE_COLUMNS, COMPARATIVES
    from engine.sql_expansion import comparative_operator

    word = question_tokens[index]
    graded = MAX_CUES | MIN_CUES
    if word in graded and "to" in question_tokens[max(0, index - 2):index]:
        start = next((position for position in range(index - 1, -1, -1) if question_tokens[position] in graded
                      and "from" in question_tokens[max(0, position - 2):position]), None)
        if start is not None:
            return _realizes_grade(question_tokens, start, query, graph)
    from engine.closed_class import EXCLUSION_CUES
    alternatives = bool({"or", "either"} & set(question_tokens))
    # A negation before the word's own ("did not have any pet older than 3") makes a subtracted scope the one asked;
    # one beside it ("not older than 35") is the comparative's own complement (comparative_operator).
    negated = bool(EXCLUSION_CUES.search(" ".join(question_tokens[:max(0, index - 2)])))
    scoped = tuple(_select_queries(query, graph=graph, extremum=bool(set(question_tokens) & graded)))
    if word in COMPARATIVES and "than" in question_tokens[index + 1:index + 4]:
        operator = comparative_operator(question_tokens, index)
        described = COMPARATIVE_COLUMNS.get(word, frozenset())
        return any(
            _scoped_match(scope, comparison.operator, operator, alternatives=alternatives, negated=negated)
            and isinstance(column := (comparison.left.operand if isinstance(comparison.left, Aggregate)
                                      else comparison.left), ColumnRef)
            and (not described or bool(described & set(semantic_tokens(column.name))))
            for select, scope in scoped for comparison in (*_comparisons(select.where, alternatives),
                                                           *_comparisons(select.having, alternatives)))
    # A superlative may be computed in a scalar subquery ("the car with the largest horsepower"), but not in rows the
    # query excludes or offers as one alternative, unless the question does, nor in a membership test that binds no
    # returned row.
    selects = tuple(select for select, scope in scoped
                    if (not scope.negated or negated) and (not scope.alternative or alternatives)
                    and not scope.unbound)
    cue = frequency_cue(question_tokens)
    if cue is not None and (index == cue.position or (index == cue.position + 1 and word in _FREQUENCY_WORDS)):
        return any(select.order_by and isinstance(select.order_by[0].expression, Aggregate)
                   and select.order_by[0].expression.function == "COUNT"
                   and select.order_by[0].direction == cue.direction for select in selects)
    if word not in graded:
        return False

    def fits(column):
        semantic = set(semantic_tokens(column.name))
        if word in {"youngest", "oldest"}:
            return bool(semantic & {"age", "birth", "birthday", "date", "year"})
        if word in {"latest", "earliest"}:
            return column.type == SQLType.DATE or bool(semantic & {"date", "year", "time"})
        return True

    for select in selects:
        first = select.order_by[0] if select.order_by else None
        if (first is not None and isinstance(first.expression, ColumnRef) and fits(first.expression)
                and first.direction == superlative_direction(word, first.expression)):
            return True
        if any(isinstance(item.expression, Aggregate) and item.expression.function in {"MAX", "MIN"}
               and isinstance(item.expression.operand, ColumnRef) and fits(item.expression.operand)
               and ("DESC" if item.expression.function == "MAX" else "ASC")
               == superlative_direction(word, item.expression.operand) for item in select.select):
            return True
    return False


def _groups_the_counted_rows(query, counted_here, table, graph):
    """Whether ``query``'s groups include the key of the rows it counts, or a key referring to the counted ``table``'s:
    each group is then about one row. "Which countries have at least 3 cities" grouped by each city's id, and
    "employees who had two or more jobs" by each job's (held-out Spider train 296, 220, 2026-10-11)."""
    keys = {graph.identifying_column(read) for read in counted_here | {table} if read in graph.by_table} - {None}
    referring = {(mine.table, mine.name) for key in graph.foreign_keys for left, right in key.column_pairs
                 for mine, other in ((left, right), (right, left))
                 if any((other.table, other.name) == (ref.table, ref.name) for ref in keys)}
    return any(getattr(group, "table", None) in counted_here | {table}
               and ((group.table, group.name) in referring or any(
                   (group.table, group.name) == (ref.table, ref.name) for ref in keys))
               for group in query.group_by)


def _multiplies(query, counted, question_words, graph):
    """Whether ``query`` counts the ``counted`` tables' rows over a join that repeats them: it reads a table the
    question does not name and the tables it names do not need to meet. "Patients who made more than one appointment"
    counted the appointments joined to nurses, prescriptions, stays, rooms and procedures (held-out Spider train 47,
    2026-10-11)."""
    from engine.sql_ast import column_refs
    from engine.sql_schema import canon, name_words
    tables = {table for table in query.referenced_tables() if isinstance(table, str)}
    # Named in the question, two words of it spelling one name ("high schoolers"), or read as the answer's own rows.
    shown = {ref.table for ref in column_refs((query.select, query.group_by))}
    named = {table for table in tables if {canon(word) for word in name_words(table)} <= question_words} | shown | set(
        counted)
    trees = graph.join_trees(named, limit=1) if len(named) > 1 else ()
    needed = ({trees[0].root, *(join.table for join in trees[0].joins)} if trees else set()) | named
    return bool(tables - needed)


def realizes_numeric_comparisons(question, candidate, graph, *, exclude_positions=frozenset()):
    """Whether a typed query realizes the question's numeric threshold operators and targets.

    The ambiguous ``over``/``under`` wording counts as read only when the selected AST contains the
    same operator/value against a matching column (including an aggregate over that column). This
    prevents ``over`` from forcing Gemini while still rejecting a query that reverses it to ``under``.
    """
    import math
    from engine.sql_ast import (
        Aggregate, BinaryExpr, BooleanExpr, ColumnRef, Comparison, DatePart, SelectQuery, SetQuery, Star, SubquerySource,
    )
    from engine.sql_expansion import (
        ExpansionSupport, count_admits, count_requirement_operator, counted_thresholds, name_tokens, tokens,
    )
    from engine.sql_extrema import MAX_CUES, MIN_CUES
    from engine.sql_search import _number

    question_tokens = tokens(question)
    requested = ExpansionSupport(graph).numeric_comparisons(question_tokens, exclude_positions)
    counts_asked = counted_thresholds(question_tokens, graph.tables)
    if not requested and not counts_asked:
        return False
    # Comparisons the question offers as alternatives ("opened after 2013 or before 2008") are realized by an OR of
    # them; only a conjunction realizes comparisons it asks for together.
    alternatives = bool(re.search(r"\b(?:or|either)\b", str(question), re.I))

    def refs(expression):
        if isinstance(expression, ColumnRef):
            return {expression}
        if isinstance(expression, Aggregate):
            return refs(expression.operand)
        if isinstance(expression, BinaryExpr):
            return refs(expression.left) | refs(expression.right)
        if isinstance(expression, DatePart):
            return refs(expression.operand)
        return set()

    actual = []     # (comparison, the tables its query reads, whether it tests groups, where it sits)
    scoped = tuple(_select_queries(candidate.query, graph=graph,
                                   extremum=bool(set(question_tokens) & (MAX_CUES | MIN_CUES))))
    for query, scope in scoped:
        tables = query.referenced_tables()
        actual.extend((item, tables, False, scope) for item in _comparisons(query.where, alternatives))
        actual.extend((item, tables, True, scope) for item in _comparisons(query.having, alternatives))

    def counted(item, tables, grouped, targets):
        """A threshold on a number of rows ("countries with more than 2 car makers") is realized by a HAVING COUNT
        over groups of a query that reads the counted rows' table. A field holding the number itself is compared,
        not counted: "markets with number of shops greater than 50" is Num_of_shops > 50 (held out 832, 2026-10-17:
        a count of phone rows over 50 was accepted)."""
        return (grouped and isinstance(item.left, Aggregate) and item.left.function == "COUNT"
                and any(target.table in tables and name_tokens(target.name)[:1] not in (("number",), ("num",), ("no",))
                        for target in targets))

    def number(value):
        try:
            return _number(str(value))
        except (TypeError, ValueError):
            return str(value) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)) else None

    # A number the name of a table follows counts that table's rows (``counted_thresholds``): "countries with more than
    # 1 car maker" is met by a HAVING COUNT, not by a car maker's Country key compared with 1, and "exactly 0 car
    # makers" not by the key equal to 0 (the review of 2026-10-11).
    counted_numbers = {(operator, value) for threshold, _ in counts_asked for operator, value in threshold.predicates}
    grouped = {}
    for comparison in requested:
        value = number(comparison.right.value)
        if comparison.operator == '=' or value is None or (comparison.operator, value) in counted_numbers:
            continue
        grouped.setdefault((comparison.operator, value), set()).add(comparison.left)
    if not grouped and not counts_asked:
        return False
    from engine.closed_class import EXCLUSION_CUES
    from engine.sql_schema import canon
    question_words = set(question_tokens) | {canon(left + right) for left, right in zip(question_tokens,
                                                                                      question_tokens[1:])}
    # A threshold stated on a total or an average ("customers with a total amount over 10") compares that aggregate,
    # not each row's field: a WHERE amount > 10 once answered it (the review of 2026-10-11).
    aggregated = {}
    for stated in ExpansionSupport(graph).aggregate_thresholds(question_tokens):
        aggregated.setdefault((stated.operator, number(stated.value)), set()).add((stated.function, stated.column))

    def compares_as_asked(item, operator, value):
        wanted = aggregated.get((operator, value))
        return wanted is None or (isinstance(item.left, Aggregate) and (item.left.function, item.left.operand) in wanted)

    def count_formula(predicate, witnesses):
        """(the numbers of rows ``predicate`` keeps a group with, as a test; the bounds it compares them with; the
        counts it reads) for a HAVING read on the counts ``witnesses`` accepts: their comparisons with a number, and AND
        and OR of those. A conjunct on anything else constrains something else and is left out. None when it says
        nothing of such a count, or an OR joins one to anything else: "fewer than 0 or more than 2", which subtracts
        what "between 0 and 2" leaves out, was dropped, so the subtraction was refused (the review of 6263502, S1)."""
        if isinstance(predicate, Comparison):
            value = number(getattr(predicate.right, "value", None))
            if not witnesses(predicate.left) or value is None or isinstance(value, str):
                return None
            return (lambda rows: count_admits(predicate.operator, value, rows)), [value], [predicate.left]
        if isinstance(predicate, BooleanExpr):
            parts = [count_formula(term, witnesses) for term in predicate.terms]
            if predicate.operator == "OR" and None in parts:
                return None
            parts = [part for part in parts if part is not None]
            if not parts:
                return None
            combine = all if predicate.operator == "AND" else any
            return ((lambda rows: combine(test(rows) for test, _, _ in parts)),
                    [bound for _, bounds, _ in parts for bound in bounds], [read for _, _, reads in parts for read in reads])
        return None

    def boundary_counts(bounds):
        """The numbers of rows at which a comparison with one of ``bounds`` can change, and one past them all: between
        them the comparisons keep the same groups, so these are the counts to try however large a number the question
        says. Every count up to it was tried, two seconds for a million (the review of 6263502, S5)."""
        edges = {0}
        for bound in bounds:
            low, high = math.floor(bound), math.ceil(bound)
            edges |= {low - 1, low, low + 1, high, high + 1}
        return sorted(edge for edge in edges | {max(edges) + 1} if edge >= 0)

    def counts_as_asked(predicates, table, query, scope):
        """Whether the select ``query`` where ``scope`` says counts ``table``'s rows per group as ``predicates`` ask: its
        HAVING admits the same numbers of rows ("fewer than 2" is COUNT < 2 or COUNT <= 1), or, subtracted (NOT IN),
        admits the others and leaves out only groups with rows ("fewer than 2" is NOT IN the groups with at least 2,
        which keeps those with none). A count that admits none kept groups only when the groups keep rows without one:
        the counted table grouped alone where no other table holds what it groups by, or joined LEFT with its own
        column counted; an inner join leaves out the countries without car makers (the review of 2026-10-11, R1), and
        the car makers grouped alone beside a countries table enumerate only the countries that have one (the review
        of 6263502, S1)."""
        if scope.scalar or scope.unbound or (scope.alternative and not alternatives):
            return False
        read = {name for name in query.referenced_tables() if isinstance(name, str)}
        # The rows counted are the table's, or, where the query does not read it, a table's whose name extends it:
        # "employees who had two or more jobs" counts their job_history rows (held-out Spider train 220). Beside the
        # templates, Ref_Template_Types holds the types they are grouped by, not templates (Spider DEV 333).
        counted_here = {table} & read or {name for name in read
                                          if _name_vocabulary((table,)) <= _name_vocabulary((name,))}
        if not counted_here or _multiplies(query, counted_here, question_words, graph) \
                or _groups_the_counted_rows(query, counted_here, table, graph):
            return False
        # The count is of the counted rows: COUNT(*), or of a column of theirs, a distinct one telling them apart. A
        # count of distinct countries is no count of car makers (S2), and COUNT(*) over car makers joined to the
        # airports another threshold counts counts pairs of them (S3).
        others = {other for _, other in counts_asked if other != table} & read

        def witnesses(expression):
            if not isinstance(expression, Aggregate) or expression.function != "COUNT":
                return False
            if isinstance(expression.operand, Star):
                return not others
            return (isinstance(expression.operand, ColumnRef) and expression.operand.table in counted_here
                    and (not expression.distinct or graph.identifies((expression.operand,))))

        formula = count_formula(query.having, witnesses)
        if formula is None:
            return False
        kept_by, bounds, counts = formula
        for rows in boundary_counts([value for _, value in predicates] + bounds):
            kept = kept_by(rows)
            if scope.negated:
                kept = rows == 0 or not kept
            if kept != all(count_admits(operator, value, rows) for operator, value in predicates):
                return False
        if scope.negated or not all(count_admits(operator, value, 0) for operator, value in predicates):
            return True
        if read == {table}:
            return not any((group.table, group.name) == (mine.table, mine.name) and other.table != table
                           for group in query.group_by if isinstance(group, ColumnRef)
                           for key in graph.foreign_keys for left, right in key.column_pairs
                           for mine, other in ((left, right), (right, left)))
        return (query.from_table != table and all(join.kind == "LEFT" for join in query.joins)
                and all(isinstance(count.operand, ColumnRef) and count.operand.table == table for count in counts))

    def negated_before(value):
        """Whether the question negates before it says ``value`` ("did not visit any museum opened after 2010")."""
        place = next((index for index, token in enumerate(question_tokens) if number(token) == value), None)
        return place is not None and bool(EXCLUSION_CUES.search(" ".join(question_tokens[:place])))

    for (operator, value), targets in grouped.items():
        negated = negated_before(value)
        if not any(
            _scoped_match(scope, item.operator, operator, alternatives=alternatives, negated=negated)
            and number(getattr(item.right, "value", None)) == value
            and (bool(refs(item.left) & targets) or counted(item, tables, grouped_rows, targets))
            and compares_as_asked(item, operator, value)
            for item, tables, grouped_rows, scope in actual
        ):
            return False
    for threshold, table in counts_asked:
        if not any(counts_as_asked(threshold.predicates, table, query, scope) for query, scope in scoped):
            return False
    if len({table for _, table in counts_asked}) > 1:
        operator = count_requirement_operator(question_tokens, counts_asked)
        if operator is None:
            return False
        extremum = bool(set(question_tokens) & (MAX_CUES | MIN_CUES))

        def combine_tests(parts, connective):
            if connective == "OR" and any(part is None for part in parts):
                return None
            parts = [part for part in parts if part is not None]
            if not parts:
                return None
            combine = all if connective == "AND" else any
            return lambda truths: combine(part(truths) for part in parts)

        def matching_counts(queries):
            matched = tuple(index for index, (threshold, table) in enumerate(counts_asked)
                            if any(counts_as_asked(threshold.predicates, table, query, scope)
                                   for query, scope in queries))
            return (lambda truths: all(truths[index] for index in matched)) if matched else None

        def predicate_test(predicate):
            if isinstance(predicate, BooleanExpr):
                return combine_tests([predicate_test(term) for term in predicate.terms], predicate.operator)
            queries = []
            for inner, scalar, negated, operand in _tested_subqueries(predicate):
                if not scalar:
                    scope = _Scope(negated=negated, unbound=not _binds(operand, inner, graph, extremum))
                    queries.extend(_select_queries(inner, scope, graph, extremum))
            return matching_counts(queries)

        def query_test(query):
            if isinstance(query, SetQuery):
                if query.operator not in {"UNION", "INTERSECT"}:
                    return None
                return combine_tests([query_test(query.left), query_test(query.right)],
                                     "OR" if query.operator == "UNION" else "AND")
            if not isinstance(query, SelectQuery):
                return None
            parts = [matching_counts(((query, _Scope()),)), predicate_test(query.where)]
            if isinstance(query.from_table, SubquerySource):
                parts.append(query_test(query.from_table.query))
            return combine_tests(parts, "AND")

        actual_test = query_test(candidate.query)
        if actual_test is None:
            return False
        wanted = all if operator == "AND" else any
        # Count extraction retains at most four occurrences: compare their Boolean relationship, not their numeric
        # magnitude. Independent witnesses alone once accepted A AND B as the requested A OR B (2026-10-12).
        for mask in range(1 << len(counts_asked)):
            truths = tuple(bool(mask & (1 << index)) for index in range(len(counts_asked)))
            if actual_test(truths) != wanted(truths):
                return False
    return True

