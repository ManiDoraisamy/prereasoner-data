"""Shared request completeness policy for the deterministic planner.

Operator and literal evidence comes from the typed AST. Schema normalization is
separate from execution; unresolved meaning cannot certify a partial answer.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import weakref

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


def unreadable_measure_reason(column, cells, function="SUM"):
    """The reply when the measure a question names has cells that are not numbers (``unreadable_cells``, one entry
    per cell). Compose, the named-field check below and the currency check all say it in these words: how many cells,
    and the first distinct ones."""
    distinct = list(dict.fromkeys(cells))
    shown = ", ".join(repr(cell) for cell in distinct[:3]) + (f" and {len(distinct) - 3} more"
                                                              if len(distinct) > 3 else "")
    held = "a value that isn't a number" if len(cells) == 1 else f"{len(cells)} values that aren't numbers"
    done = "averaged" if function == "AVG" else "totaled"
    return f"The {column} column has {held} ({shown}), so it can't be {done}."


def unreadable_measure_violation(question, graph):
    """The refusal for a question that totals or averages a column it names whose cells are not all numbers
    (``unreadable_cells``), or None. A sheet whose only amounts are text has no query to total them at all, and the
    reply said "no valid AST candidate" (planted-text test, 2026-10-08)."""
    from engine.sql_rank import aggregate_operand

    text_columns = {column.ref.name: column for column in graph.columns if not column.ref.type.numeric}
    for function in ("SUM", "AVG"):
        operand = aggregate_operand(question, sorted(text_columns), function)
        cells = unreadable_cells(text_columns[operand].values) if operand else None
        if cells:
            return unreadable_measure_reason(operand, cells, function)
    return None


_UNREADABLE_MEASURE = re.compile(r"The .+ column has .+, so it can't be (?:totaled|averaged)\.")


def explains_refusal(violation):
    """Whether a ``constraint_violations`` entry is written for the user, so a refusal can be its reply: a repeated
    field to choose, a field that cannot be computed, or a measure with cells that are not numbers."""
    return (violation.startswith(("Which repeated field", "The field ", "The requested total for "))
            or bool(_UNREADABLE_MEASURE.fullmatch(violation)))


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


def constraint_violations(question, query, graph):
    from engine.sql_ast import Aggregate, ColumnRef, Comparison, DatePart, ExistsPredicate, InPredicate, SelectQuery, SetQuery, Star
    from engine.sql_dates import realizes_dates, served_date_phrases
    from engine.sql_durations import duration_phrases, realizes_durations
    from engine.sql_expansion import ExpansionSupport, tokens
    from engine.sql_candidate import ScoredQuery

    if isinstance(query, SetQuery):
        return tuple(dict.fromkeys(constraint_violations(question, query.left, graph)
                                   + constraint_violations(question, query.right, graph)))
    if not isinstance(query, SelectQuery):
        return ("unsupported interpretation scope",)
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
    question_tokens = tokens(question)
    phrases = served_date_phrases(question, question_tokens, graph)
    violations.extend(relational_operator_evidence(question, query, graph)[1])
    # The spreadsheet importer preserves duplicate headers with their original
    # column letters. Displaying both is safe; choosing one for a calculation or
    # filter needs the user to distinguish it, rather than an arbitrary model pick.
    used = set(column_refs(query))
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
    for label, refs in named_fields.items():
        if ' '+label+' ' not in question_text:
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
        if not realizes_substring(query, request.text, request.subject):
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
        member = ScoredQuery(query, render_query(query), 0.0, ())
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
    # A value the query compares is data, not an instruction: "Newsletter No" and "tasks that
    # are Not Started" filter on the cells 'No' and 'Not Started'. A cue outside those values
    # still needs its exclusion ("tasks that are not Done").
    cue_text = question
    for literal in sorted(_compared_texts(actual), key=len, reverse=True):
        cue_text = re.sub(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", " ", cue_text, flags=re.I)
    if EXCLUSION_CUES.search(cue_text) and not re.search(r"\bnon[- ]empty\b", question, re.I):
        if not any((isinstance(p, Comparison) and p.operator in {"!=", "<>", "NOT LIKE", "IS NOT"})
                   or (isinstance(p, (ExistsPredicate, InPredicate)) and p.negated) for p in actual):
            violations.append("requested exclusion is missing")
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
    for column, value, folded, excludes in _value_comparisons(actual):
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
                      for p in actual if isinstance(p, (ExistsPredicate, InPredicate)))
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
    # name shared by two tables (a join key) is met by grouping either one.
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
            if label and " " + label + " " in tail_words:
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


def realizes_numeric_comparisons(question, candidate, graph, *, exclude_positions=frozenset()):
    """Whether a typed query realizes the question's numeric threshold operators and targets.

    The ambiguous ``over``/``under`` wording counts as read only when the selected AST contains the
    same operator/value against a matching column (including an aggregate over that column). This
    prevents ``over`` from forcing Gemini while still rejecting a query that reverses it to ``under``.
    """
    from engine.sql_ast import Aggregate, BinaryExpr, BooleanExpr, ColumnRef, Comparison, DatePart
    from engine.sql_ast import SelectQuery, SetQuery, SubquerySource
    from engine.sql_expansion import ExpansionSupport, tokens
    from engine.sql_search import _number

    requested = ExpansionSupport(graph).numeric_comparisons(tokens(question), exclude_positions)
    if not requested:
        return False

    def comparisons(predicate):
        if isinstance(predicate, Comparison):
            return [predicate]
        if isinstance(predicate, BooleanExpr) and predicate.operator == "AND":
            return [item for term in predicate.terms for item in comparisons(term)]
        return []

    def queries(query):
        if isinstance(query, SetQuery):
            yield from queries(query.left)
            yield from queries(query.right)
        elif isinstance(query, SelectQuery):
            yield query
            if isinstance(query.from_table, SubquerySource):
                yield from queries(query.from_table.query)

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

    actual = []
    for query in queries(candidate.query):
        for predicate in (query.where, query.having):
            actual.extend(comparisons(predicate))

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
    for (operator, value), targets in grouped.items():
        if not any(
            item.operator == operator and number(item.right.value) == value
            and bool(refs(item.left) & targets)
            for item in actual
        ):
            return False
    return True


