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
    from engine.sql_ast import Aggregate, ColumnRef, SelectQuery
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
        projected = [item.expression for item in query.select]
        grouped_projection = (bool(query.group_by) and bool(projected)
                              and all(isinstance(item, ColumnRef) and item in query.group_by
                                      for item in projected))
        if query.distinct or (counted and all(item.distinct for item in counted)) or grouped_projection:
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
    branches = tuple(_select_branches(query))
    if not all(isinstance(branch, SelectQuery) for branch in branches):
        return ("unsupported interpretation scope",)
    return tuple(dict.fromkeys(violation for branch in branches
                               for violation in _select_violations(question, branch, graph, query)))


def _select_violations(question, query, graph, whole):
    """``constraint_violations`` for the select ``query``, one branch of the query ``whole`` (``query`` itself when
    it is not a set query)."""
    from engine.sql_ast import Aggregate, ColumnRef, Comparison, DatePart, ExistsPredicate, InPredicate, Star
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
    # Nor does a query answer a total of a named column that cannot be totaled by totaling another column or counting
    # the rows: beside a saved reference, "which city has the highest total amount" ranked the reference's estimated
    # amounts (unreadable_operand).
    unreadable = unreadable_measure_violation(question, graph)
    if unreadable:
        violations.append(unreadable)
    question_tokens = tokens(question)
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
    for column in graph.columns:
        label = ' '.join(canon(word) for word in lexical_words(column.ref.name))
        if not label or label in table_labels or (len(label.split()) == 1 and label in noise):
            continue
        named_fields.setdefault(label, set()).add(column.ref)
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
    measures = aggregate_columns(query.select)
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
    from engine.sql_rank import aggregate_operand
    for label, refs in named_fields.items():
        if ' '+label+' ' not in table_masked:
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
            violations.append(unreadable_measure_reason(named.name, cells, mismatched[0]) if cells else
                              'The requested total for ' + repr(sorted(ref.name for ref in refs)[0])
                              + ' could not be computed from that field; check for nonnumeric/error cells or choose a numeric field')
            continue
        if refs & used:
            continue
        # A field's name is also part of a longer field's name the query uses when the question says that longer
        # name's other words: "the name and the release year of the song" asks for Song_Name and Song_release_year,
        # "pets whose age" for pet_age (Spider DEV 6 and 69). Not when a table the question names and the query
        # reads holds the named field itself, unless the longer field is in that table: "the name and location of
        # the stadiums which some concerts happened" asks for the stadiums' Name, not concert_Name (DEV 41,
        # 2026-10-10: any of the question's words once made it the longer field).
        label_words = set(label.split())
        named_holders = {named.table for named in refs if named.table in whole.referenced_tables()
                         and ' ' + ' '.join(canon(word) for word in lexical_words(named.table)) + ' ' in question_text}
        if any(label_words < (words := _name_vocabulary((ref.name,))) and words <= question_words
               and (ref.table in {named.table for named in refs} or not named_holders)
               for ref in used):
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
        if query.limit is not None:
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
    if any(item.operator != "=" for item in requested):
        from engine.sql_ast import render_query
        member = ScoredQuery(whole, render_query(whole), 0.0, ())
        if not realizes_numeric_comparisons(question, member, graph, exclude_positions=date_positions):
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
    for branch, scope in _select_queries(whole):
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
    for pattern, function in ((r"\b(?:maximum|max)\b", "MAX"), (r"\b(?:minimum|min)\b", "MIN"),
                              (r"\b(?:average|avg|mean)\b", "AVG")):
        if re.search(pattern, text) and function not in aggregates:
            violations.append("requested aggregate is missing: " + function)
    for pattern, function, direction in ((r"\b(?:highest|largest)\b", "MAX", "DESC"),
                                         (r"\b(?:lowest|smallest)\b", "MIN", "ASC")):
        if re.search(pattern, text) and function not in aggregates:
            if not query.order_by or query.order_by[0].direction != direction:
                violations.append("requested extremum or ordering is missing")
    cutoff = re.search(r"\b(top|bottom)\s+(\d+)\b", text)
    if cutoff and (query.limit != int(cutoff[2]) or not query.order_by):
        violations.append("requested ranking cutoff is missing")
    # Explicitly named output grain must be represented, rather than just appearing
    # somewhere in the schema: a field named in full after "by/per/each", up to the clause's
    # end. "sorted by average Price" orders the groups and asks for no grain of its own, and a
    # name shared by two tables (a join key) is met by grouping either one. A field an aggregate
    # word takes is the measure the groups are ranked by, not a grain: "top 2 cities by total
    # amount" groups the cities, and only the queries also grouped by amount met it (an owner's
    # replay, 2026-10-10: "Lyon 200, Lyon 160" for Lyon 470, Paris 270).
    lowered = question.casefold()
    grouped = set(query.group_by)
    for match in re.finditer(r"\b(?:by|per|each)\s+([^?!.;,]+)", lowered):
        if re.search(r"\b(?:sort|sorted|order|ordered|rank|ranked)\s+$", lowered[:match.start()]):
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
_ORDERING_WORDS = frozenset({"order", "ordered", "sort", "sorted", "ascending", "descending"})


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
    from engine.sql_expansion import nearby_operator
    before = tuple(question_tokens[max(0, index - 2):index])
    threshold = (nearby_operator(question_tokens, index) != "="
                 or before in {("at", "least"), ("at", "most")} or before[-1:] == ("exactly",))
    compared = float(value) in _compared_numbers(query)
    limited = getattr(query, "limit", None) == value
    if threshold:
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
    from engine.sql_ast import Aggregate, SelectQuery, SetQuery, Star, SubquerySource, column_refs, share_aggregate
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
        "find", "list", "get", "return", "display", "calculate", "bought", "ordered",
    }
    if realizes_numeric_comparisons(question, candidate, graph):
        ordinary_words.update({
            "over", "under", "above", "below", "greater", "less", "more", "fewer",
            "than", "least", "most", "at",
        })
    observed = cell_words(graph)
    ordinary_words.update(canon(word) for word in closed_class_words(question))
    ordinary_words.update(relational_operator_evidence(question, candidate.query, graph)[0])
    # "contain", "substring" or "all" asked for the text a LIKE compares: read once the query compares it.
    from engine.sql_search import realizes_substring, substring_requests
    ordinary_words.update(cue for request in substring_requests(question, graph)
                          if realizes_substring(candidate.query, request.text, request.subject)
                          for cue in request.cues)
    ordinary_words.update(canon(word) for word in action_words(question) if canon(word) not in observed)
    has_aggregate = any(isinstance(item.expression, Aggregate)
                        for item in getattr(candidate.query, 'select', ()))
    if any(isinstance(item.expression, Aggregate) and item.expression.function == 'COUNT'
           for item in getattr(candidate.query, 'select', ())):
        # The noun a count counts names the rows counted, on one table. With several, only a field's name that
        # names none of the tables: "how many subscriptions by Status" counts the exports' rows, though a report
        # has a Subscriptions column. "How many people live in Gelderland" names no field, and a count of cities
        # is not its answer (Spider DEV 728, 2026-10-08).
        from engine.closed_class import counted_rows
        fields_named = _name_vocabulary(column.ref.name for column in graph.columns) - _name_vocabulary(graph.tables)
        ordinary_words.update(canon(word) for word in counted_rows(question)
                              if canon(word) not in observed
                              and (len(graph.tables) == 1 or canon(word) in fields_named))
    if has_aggregate:
        ordinary_words.update(canon(word) for word in measure_participles(question, frozenset(schema_words))
                              if canon(word) not in observed)
        state_schema = any(set(lexical_words(c.ref.name)) & {"status", "state", "paid", "listed", "settled"}
                           for c in graph.columns)
        if not state_schema and not observed & {"unpaid", "unlisted", "pending", "refunded", "settled"}:
            if re.search(r"\b(?:is|are|was|were)\s+listed\b", question, re.I):
                ordinary_words.add("listed")
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
    # A name the question spells as two words: "high schoolers" is the Highschooler table it reads.
    ordinary_words.update(word for left, right in zip(words, words[1:]) if canon(left + right) in read_names
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
    if _orders_rows(candidate.query):
        ordinary_words.update(_ORDERING_WORDS)
        reverse = bool(set(words) & REVERSE_ORDER_WORDS)
        if set(words) & ALPHABETICAL_WORDS and _orders_alphabetically(candidate.query, reverse):
            ordinary_words.update(ALPHABETICAL_WORDS | (REVERSE_ORDER_WORDS if reverse else frozenset()))
    # A graded word is read when the query realizes it at every place the question says it (_realizes_grade): "the
    # youngest singer" by an ascending age, "the most common citizenship" by groups ordered by their count.
    from engine.sql_extrema import MAX_CUES, MIN_CUES
    from engine.closed_class import COMPARATIVES
    for word in (set(question_tokens) & (MAX_CUES | MIN_CUES | set(COMPARATIVES) | _FREQUENCY_WORDS)) - ordinary_words:
        places = [index for index, token in enumerate(question_tokens) if token == word]
        if all(_realizes_grade(question_tokens, index, candidate.query) for index in places):
            ordinary_words.add(canon(word))
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
    and keeps no row."""
    alternative: bool = False
    negated: bool = False
    scalar: bool = False


def _tested_subqueries(predicate):
    """(query, scalar, negated) for each query a predicate tests rows against: IN and EXISTS test membership, negated
    as NOT IN and NOT EXISTS; a scalar subquery computes a value."""
    from engine.sql_ast import BooleanExpr, Comparison, ExistsPredicate, InPredicate, ScalarSubquery
    if isinstance(predicate, BooleanExpr):
        for term in predicate.terms:
            yield from _tested_subqueries(term)
    elif isinstance(predicate, InPredicate) and not isinstance(predicate.source, tuple):
        yield predicate.source, False, predicate.negated
    elif isinstance(predicate, ExistsPredicate):
        yield predicate.query, False, predicate.negated
    elif isinstance(predicate, Comparison):
        for side in (predicate.left, predicate.right):
            if isinstance(side, ScalarSubquery):
                yield side.query, True, False


def _select_queries(query, scope=_Scope()):
    """(select query, ``_Scope``) for every select query in ``query``: set branches, FROM subqueries and the
    subqueries its predicates test, so "visitors who did not visit any museum opened after 2010" is realized inside
    its NOT IN. Each keeps where it sits: a comparison anywhere in the query once realized the question's, so "above
    35 and below 50" was met by a UNION and by an EXCEPT of the two, and "people above 35" by a scalar subquery
    comparing their ids (review of 2026-10-10)."""
    from dataclasses import replace
    from engine.sql_ast import SelectQuery, SetQuery, SubquerySource
    if isinstance(query, SetQuery):
        branch = replace(scope, alternative=scope.alternative or query.operator == "UNION")
        yield from _select_queries(query.left, branch)
        yield from _select_queries(query.right, replace(branch, negated=branch.negated != (query.operator == "EXCEPT")))
    elif isinstance(query, SelectQuery):
        yield query, scope
        if isinstance(query.from_table, SubquerySource):
            yield from _select_queries(query.from_table.query, scope)
        for predicate in (query.where, query.having):
            for nested, scalar, negated in _tested_subqueries(predicate):
                yield from _select_queries(nested, replace(scope, negated=scope.negated != negated,
                                                           scalar=scope.scalar or scalar))


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
    if scope.scalar or (scope.alternative and not alternatives):
        return False
    if scope.negated:
        return COMPLEMENTS.get(operator) == wanted or (negated and operator == wanted)
    return operator == wanted


# The words after "most" or "least" that grade by how often: "the most common citizenship".
_FREQUENCY_WORDS = frozenset({"common", "frequent", "frequently"})


def _realizes_grade(question_tokens, index, query):
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
            return _realizes_grade(question_tokens, start, query)
    from engine.closed_class import EXCLUSION_CUES
    alternatives = bool({"or", "either"} & set(question_tokens))
    # A negation before the word's own ("did not have any pet older than 3") makes a subtracted scope the one asked;
    # one beside it ("not older than 35") is the comparative's own complement (comparative_operator).
    negated = bool(EXCLUSION_CUES.search(" ".join(question_tokens[:max(0, index - 2)])))
    scoped = tuple(_select_queries(query))
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
    # query excludes or offers as one alternative, unless the question does.
    selects = tuple(select for select, scope in scoped
                    if (not scope.negated or negated) and (not scope.alternative or alternatives))
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


def realizes_numeric_comparisons(question, candidate, graph, *, exclude_positions=frozenset()):
    """Whether a typed query realizes the question's numeric threshold operators and targets.

    The ambiguous ``over``/``under`` wording counts as read only when the selected AST contains the
    same operator/value against a matching column (including an aggregate over that column). This
    prevents ``over`` from forcing Gemini while still rejecting a query that reverses it to ``under``.
    """
    from engine.sql_ast import Aggregate, BinaryExpr, ColumnRef, DatePart
    from engine.sql_expansion import ExpansionSupport, tokens
    from engine.sql_search import _number

    requested = ExpansionSupport(graph).numeric_comparisons(tokens(question), exclude_positions)
    if not requested:
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
    for query, scope in _select_queries(candidate.query):
        tables = query.referenced_tables()
        actual.extend((item, tables, False, scope) for item in _comparisons(query.where, alternatives))
        actual.extend((item, tables, True, scope) for item in _comparisons(query.having, alternatives))

    def counted(item, tables, grouped, targets):
        """A threshold on a number of rows ("countries with more than 2 car makers") is realized by a HAVING COUNT
        over groups of a query that reads the counted rows' table."""
        return (grouped and isinstance(item.left, Aggregate) and item.left.function == "COUNT"
                and any(target.table in tables for target in targets))

    def number(value):
        try:
            return _number(str(value))
        except (TypeError, ValueError):
            return str(value) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)) else None

    grouped = {}
    for comparison in requested:
        if comparison.operator == '=':
            continue
        value = number(comparison.right.value)
        if value is None:
            continue
        grouped.setdefault((comparison.operator, value), set()).add(comparison.left)
    if not grouped:
        return False
    from engine.closed_class import EXCLUSION_CUES
    question_tokens = tokens(question)

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
            for item, tables, grouped_rows, scope in actual
        ):
            return False
    return True


