"""Shared request completeness policy for the deterministic planner.

Operator and literal evidence comes from the typed AST. Schema normalization is
separate from execution; unresolved meaning cannot certify a partial answer.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


def lexical_words(text):
    """Keep Unicode names; punctuation in schema labels separates words."""
    return tuple(word.removesuffix("'s") for word in
                 re.findall(r"[^\W_]+(?:'[^\W_]+)?", str(text).casefold(), re.UNICODE))


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
                  for word in lexical_words(column.ref.name)}
    data_words.update(canon(word) for column in graph.columns for value in column.values
                      if value is not None for word in lexical_words(value))
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


def constraint_violations(question, query, graph):
    from engine.sql_ast import Aggregate, ColumnRef, Comparison, DatePart, ExistsPredicate, InPredicate, SelectQuery, SetQuery, Star
    from engine.sql_dates import realizes_dates, served_date_phrases
    from engine.sql_expansion import ExpansionSupport, tokens
    from engine.sql_candidate import ScoredQuery

    if isinstance(query, SetQuery):
        return tuple(dict.fromkeys(constraint_violations(question, query.left, graph)
                                   + constraint_violations(question, query.right, graph)))
    if not isinstance(query, SelectQuery):
        return ("unsupported interpretation scope",)
    question_tokens = tokens(question)
    phrases = served_date_phrases(question, question_tokens, graph)
    violations = list(relational_operator_evidence(question, query, graph)[1])
    # The spreadsheet importer preserves duplicate headers with their original
    # column letters. Displaying both is safe; choosing one for a calculation or
    # filter needs the user to distinguish it, rather than an arbitrary model pick.
    from dataclasses import fields, is_dataclass

    def references(value):
        if isinstance(value, ColumnRef):
            yield value
        elif is_dataclass(value):
            for field in fields(value):
                yield from references(getattr(value, field.name))
        elif isinstance(value, (tuple, list)):
            for item in value:
                yield from references(item)

    used = set(references(query))
    projected = {item.expression for item in query.select if isinstance(item.expression, ColumnRef)}
    constrained = set(references((query.where, query.having, query.group_by, query.order_by)))
    constrained.update(ref for item in query.select if not isinstance(item.expression, ColumnRef)
                       for ref in references(item.expression))
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
    from engine.sql_rank import analyze_question
    roles = analyze_question(question, graph)
    count_requested = roles.count_requested
    from engine.closed_class import recipient_classes
    recipients = {canon(word) for word in recipient_classes(question)}
    data_literals = {canon(word) for column in graph.columns for value in column.values
                     if value is not None for word in lexical_words(value)} if recipients else set()
    def aggregate_functions(value):
        found = {value.function} if isinstance(value, Aggregate) else set()
        if is_dataclass(value):
            for field in fields(value):
                found.update(aggregate_functions(getattr(value, field.name)))
        elif isinstance(value, (tuple, list)):
            for item in value:
                found.update(aggregate_functions(item))
        return found
    if roles.aggregate_positions.get('SUM') and not count_requested and 'SUM' not in aggregate_functions(query.select):
        violations.append('A requested total needs a sum of values, not a row count or projection')
    counts_rows = count_requested and any(isinstance(item.expression, Aggregate)
        and item.expression.function == 'COUNT' and isinstance(item.expression.operand, Star)
        for item in query.select)
    for label, refs in named_fields.items():
        if ' '+label+' ' not in question_text or refs & used:
            continue
        if roles.aggregate_positions and label in recipients and label not in data_literals:
            continue
        # Counting a populated field in the base table is exactly COUNT(*).
        # This evidence is local data, not a learned guess about missing values.
        if counts_rows and any(ref.table == query.from_table and graph.column_map[(ref.table, ref.name)].values
            and all(value is not None and str(value).strip() for value in graph.column_map[(ref.table, ref.name)].values)
            for ref in refs):
            continue
        if not any(isinstance(item.expression, Star) for item in query.select):
            violations.append('The requested field is not used: '+sorted(ref.name for ref in refs)[0])
    for ref in used:
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
    if phrases and not realizes_dates(query, phrases):
        violations.append("requested calendar constraint is missing or changed")
    elif phrases:
        named_dates = {column for _, column in ExpansionSupport(graph).mentioned_columns(question_tokens, False)
                       if column.type.value == 'date'}
        filtered_dates = {p.left.operand if isinstance(p.left, DatePart) else p.left
                          for p in mandatory_predicates(query.where) if isinstance(p, Comparison)}
        if named_dates and not named_dates & filtered_dates:
            violations.append("the calendar filter uses a different date column")
    date_positions = frozenset(i for phrase in phrases for i in range(phrase.start, phrase.end))
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
    if EXCLUSION_CUES.search(question) and not re.search(r"\bnon[- ]empty\b", question, re.I):
        if not any((isinstance(p, Comparison) and p.operator in {"!=", "<>", "NOT LIKE", "IS NOT"})
                   or (isinstance(p, (ExistsPredicate, InPredicate)) and p.negated) for p in actual):
            violations.append("requested exclusion is missing")
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
    # somewhere in the schema. Unknown grain remains the wording assistant's job.
    for match in re.finditer(r"\b(?:by|per|each)\s+([^?!.;]+)", question.casefold()):
        tail = match[1]
        named = {column.ref for column in graph.columns
                 if " ".join(lexical_words(column.ref.name)) in " ".join(lexical_words(tail))}
        if named and aggregates and not named <= set(query.group_by):
            violations.append("requested output grain is missing")
    return tuple(violations)


def has_unread_terms(question, candidate, graph, *, calculation_satisfied=False):
    """Ask the stateless rewriter when a runnable plan leaves wording out or returns every field.

    Matching one named column can produce executable SQL while ignoring another part of the request.
    Unmatched wording and a wildcard projection over a named field are bounded rewrite signals.
    """
    if candidate is None:
        return True
    from engine.sql_ast import Aggregate, SelectQuery, SetQuery, Star, SubquerySource, share_aggregate
    from engine.sql_schema import canon
    from engine.closed_class import action_words, closed_class_words, measure_participles
    from engine.sql_dates import served_date_phrases, realizes_dates
    from engine.sql_expansion import tokens

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
    words = tuple(canon(word) for word in lexical_words(recognized_question) if word not in date_words)
    schema_words = {
        canon(word)
        for column in graph.columns
        for word in lexical_words(f"{column.ref.table} {column.ref.name}")
    }
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
    observed = {canon(word) for column in graph.columns for value in column.values
                if value is not None for word in lexical_words(value)}
    ordinary_words.update(canon(word) for word in closed_class_words(question))
    ordinary_words.update(relational_operator_evidence(question, candidate.query, graph)[0])
    ordinary_words.update(canon(word) for word in action_words(question) if canon(word) not in observed)
    has_aggregate = any(isinstance(item.expression, Aggregate)
                        for item in getattr(candidate.query, 'select', ()))
    if len(graph.tables) == 1 and any(isinstance(item.expression, Aggregate) and item.expression.function == 'COUNT'
                                    for item in getattr(candidate.query, 'select', ())):
        from engine.closed_class import counted_rows
        ordinary_words.update(canon(word) for word in counted_rows(question)
                              if canon(word) not in observed)
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
    if any(word not in schema_words | sql_literals | ordinary_words for word in words):
        return True
    def has_star(query):
        if isinstance(query, SetQuery):
            return has_star(query.left) or has_star(query.right)
        if not isinstance(query, SelectQuery):
            return False
        if isinstance(query.from_table, SubquerySource) and has_star(query.from_table.query):
            return True
        return any(isinstance(item.expression, Star) for item in query.select)

    table_words = {canon(word) for table in graph.tables for word in lexical_words(table)}
    requested_fields = set(words) & (schema_words - table_words)
    from engine.sql_expansion import complete_projection_requested
    explicitly_all_fields = complete_projection_requested(question)
    return has_star(candidate.query) and bool(requested_fields) and not explicitly_all_fields


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


