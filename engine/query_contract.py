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


def constraint_violations(question, query, graph):
    from engine.sql_ast import Aggregate, Comparison, DatePart, ExistsPredicate, InPredicate, SelectQuery, SetQuery
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
    violations = []
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
    explicitly_all_fields = bool(re.search(r'\ball\s+(?:columns|fields)\b', question, re.I))
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


