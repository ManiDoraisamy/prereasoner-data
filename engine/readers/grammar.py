"""Schema-driven named readout into the engine's one typed SQL AST.

The JSON grammar, tree codec, validator and renderer remain engine.sql_ast.
This controller offers finite input-grounded choices; it never generates SQL.
Training and inference use the same traversal and candidate descriptions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import re
from functools import lru_cache
from collections.abc import Mapping

from engine import sql_ast as ast
from engine.readers.decisions import (
    InvalidDecision,
    named_operation_meanings,
    named_propositions,
    request,
)

_REFERENCE_ROW_BOUND = 128
_REFERENCE_TEXT_BOUND = 12000


def _key(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _source_value(value):
    from engine.numeric import wire_document
    return value.isoformat() if isinstance(value, (date, datetime)) else wire_document(value)


def source_schema(graph):
    """Physical input evidence shared by corpus parents and the encoder bank.

    The schema is derived only from the supplied graph. It contains neither
    question-dependent anchors nor target/program annotations.
    """
    return [
        {
            "table": column.ref.table,
            "column": column.ref.name,
            "type": column.ref.type.value,
            "samples": [
                _source_value(value)
                for value in column.values[:9]
            ],
            **({"annotations": dict(column.annotations)} if column.annotations else {}),
        }
        for column in graph.columns
    ]


class _DecisionInput(Mapping):
    """Materialize only the encoder input of a captured decision."""

    def __init__(self, metadata, materialize):
        self.metadata, self.materialize, self.full = metadata, materialize, None

    def __getitem__(self, key):
        if key in self.metadata:
            return self.metadata[key]
        if self.full is None:
            self.full = self.materialize()
        return self.full[key]

    def __iter__(self):
        return iter((*self.metadata, "request", "input", "items"))

    def __len__(self):
        return len(self.metadata) + 3


@lru_cache(maxsize=4)
def _literal_observations(graph):
    """Original values and observed date formats; never stripped source bytes."""
    columns, styles, domains, durations = [], set(), set(), set()
    for column in graph.columns:
        strings = {}
        for value in column.values:
            if isinstance(value, str):
                strings.setdefault(
                    value, frozenset(re.findall(r"[^\W_]+", value.casefold()))
                )
                match = re.match(r"^\d{4}([/-])\d{1,2}\1\d{1,2}", value)
                if match:
                    styles.add(("ymd", match[1]))
                match = re.match(r"^(\d{1,2})([/-])(\d{1,2})\2\d{4}", value)
                if match:
                    first, second = int(match[1]), int(match[3])
                    if second <= 12:
                        styles.add(("dmy", match[2]))
                    if first <= 12:
                        styles.add(("mdy", match[2]))
                match = re.match(r"^(\d{1,2})([/-])(\d{1,2})\2\d{2}(?!\d)", value)
                if match:
                    if int(match[3]) <= 12:
                        styles.add(("dmy2", match[2]))
                    if int(match[1]) <= 12:
                        styles.add(("mdy2", match[2]))
                match = re.match(r"^\d{1,2}([/-])([A-Za-z]{3})\1\d{4}", value)
                if match:
                    case = "upper" if match[2].isupper() else "lower" if match[2].islower() else "title"
                    styles.add(("dmony_" + case, match[1]))
                match = re.fullmatch(r"[^@\s]+@([^@\s]+\.[A-Za-z]{2,})", value)
                if match:
                    domains.add("@" + match[1])
                if re.fullmatch(r"\d{1,3}:[0-5]\d:[0-5]\d", value):
                    durations.add('hms')
                elif re.fullmatch(r"\d{1,3}:[0-5]\d", value):
                    durations.add('ms')
        from collections import defaultdict
        items = tuple(strings.items())
        index = defaultdict(list)
        for i, (_, tokens) in enumerate(items):
            for token in tokens:
                index[token].append(i)
            value = items[i][0]
            compact = re.sub(r"\W+", "", value.casefold())
            if len(value) <= 64 and len(compact) >= 3 and compact not in tokens:
                index[compact].append(i)
        columns.append((items, dict(index)))
    return tuple(columns), tuple(sorted(styles)), tuple(sorted(domains)), frozenset(durations)


@lru_cache(maxsize=4)
def _row_observations(graph):
    """Keep aligned source-row cells so an observed name can ground its code."""
    from collections import Counter
    rows, frequencies = [], Counter()
    for table, columns in graph.by_table.items():
        for row in range(max((len(c.values) for c in columns), default=0)):
            cells = tuple((c.ref.name, c.values[row]) for c in columns if row < len(c.values))
            tokens = frozenset(word for _, value in cells if isinstance(value, (str, date, datetime))
                               for word in re.findall(r"[^\W_]+", _source_value(value).casefold()))
            rows.append((table, row, cells, tokens))
            frequencies.update(tokens)
    from collections import defaultdict
    index = defaultdict(list)
    for i, (_, _, _, tokens) in enumerate(rows):
        for token in tokens:
            index[token].append(i)
    return tuple(rows), frequencies, dict(index)


def canonical_query(query):
    """Alpha-normalize arbitrary SQL aliases, including derived output names.

    Names of physical tables/columns and literal values remain unchanged.
    Projection aliases are display identifiers; their bijection is recorded by
    the caller's source/target trees, never supplied as an input hint.
    """
    counter = 0

    def visit(value, scope, derived=False):
        nonlocal counter
        if isinstance(value, ast.SelectQuery):
            own = dict(scope)

            def source(table, alias):
                nonlocal counter
                names = {}
                if isinstance(table, ast.SubquerySource):
                    inner, names = visit(table.query, scope, True)
                    alias = table.alias
                if alias is not None:
                    new = f"a{counter}"
                    counter += 1
                    own[alias] = (new, names)
                else:
                    new = None
                    own[table] = (table, {})
                return (
                    ast.SubquerySource(inner, new)
                    if isinstance(table, ast.SubquerySource)
                    else table
                ), new

            table, alias = source(value.from_table, value.from_alias)
            joins = []
            for join in value.joins:
                target, joined_alias = source(join.table, join.alias)
                joins.append(replace(join, table=target, alias=joined_alias))
            changes = {
                field.name: visit(getattr(value, field.name), own)
                for field in fields(value)
                if field.name not in {"from_table", "from_alias", "joins", "select"}
            }
            changes.update(
                from_table=table,
                from_alias=None if isinstance(table, ast.SubquerySource) else alias,
                joins=tuple(visit(join, own) for join in joins),
            )
            output, names = [], {}
            for i, item in enumerate(value.select):
                expression = visit(item.expression, own)
                old_name = item.alias or (
                    item.expression.name
                    if isinstance(item.expression, ast.ColumnRef)
                    else None
                )
                new_name = f"c{i}" if derived or item.alias is not None else None
                output.append(ast.SelectItem(expression, new_name))
                if old_name is not None:
                    names[old_name] = new_name or old_name
            changes["select"] = tuple(output)
            result = ast.SelectQuery(**changes)
            return (result, names) if derived else result
        if isinstance(value, ast.SetQuery):
            if derived:
                left, names = visit(value.left, scope, True)
                right, _ = visit(value.right, scope, True)
                return ast.SetQuery(left, value.operator, right), names
            return ast.SetQuery(
                visit(value.left, scope), value.operator, visit(value.right, scope)
            )
        if isinstance(value, ast.ColumnRef):
            table, columns = scope.get(value.table, (value.table, {}))
            return replace(value, table=table, name=columns.get(value.name, value.name))
        if is_dataclass(value):
            return replace(
                value,
                **{
                    field.name: visit(getattr(value, field.name), scope)
                    for field in fields(value)
                },
            )
        if isinstance(value, tuple):
            return tuple(visit(child, scope) for child in value)
        return value

    result = visit(query, {})
    ast.validate_query(result)
    return result


def validate_grounding(query, graph):
    """Validate every physical/derived/correlated reference against its scope."""
    ast.validate_query(query)
    _ground_query(query, graph, retype=False)


def retype_grounded_query(query, graph):
    """Rebind offline source-label types to a checked input schema.

    Physical column identities, operations and values never change. Serving
    validation remains strict and never calls this source-label repair helper.
    """
    result = _ground_query(query, graph, retype=True)
    ast.validate_query(result)
    return result


def _ground_query(query, graph, *, retype):

    def output(query):
        selected = query.left if isinstance(query, ast.SetQuery) else query
        result = {}
        for item in selected.select:
            name = item.alias or (
                item.expression.name
                if isinstance(item.expression, ast.ColumnRef)
                else None
            )
            if name is not None:
                if name in result:
                    raise InvalidDecision("ambiguous derived output column")
                result[name] = ast.value_type(item.expression)
        return result

    def visit(value, outer):
        if isinstance(value, ast.SelectQuery):
            own = dict(outer)
            local = set()
            bound_source = value.from_table
            if isinstance(bound_source, ast.SubquerySource):
                bound_source = replace(bound_source, query=visit(bound_source.query, outer))
            sources = [
                (bound_source, value.from_alias),
                *((join.table, join.alias) for join in value.joins),
            ]
            for source, alias in sources:
                if isinstance(source, ast.SubquerySource):
                    name, columns = source.alias, output(source.query)
                else:
                    if source not in graph.by_table:
                        raise InvalidDecision(
                            "AST source is absent from the input schema"
                        )
                    name = alias or source
                    columns = {
                        column.ref.name: column.ref.type
                        for column in graph.by_table[source]
                    }
                if name in local:
                    raise InvalidDecision("duplicate source alias")
                local.add(name)
                own[name] = columns
            changed = {field.name: visit(getattr(value, field.name), own)
                       for field in fields(value) if field.name not in {"from_table", "from_alias"}}
            return replace(value, from_table=bound_source, **changed)
        elif isinstance(value, ast.ColumnRef):
            expected = outer.get(value.table, {}).get(value.name)
            if expected is None or not retype and expected != value.type:
                raise InvalidDecision(
                    "AST column binding/type disagrees with the input scope"
                )
            return replace(value, type=expected) if retype else value
        elif is_dataclass(value):
            return replace(value, **{field.name: visit(getattr(value, field.name), outer) for field in fields(value)})
        elif isinstance(value, tuple):
            return tuple(visit(child, outer) for child in value)
        return value

    return visit(query, {})


class ASTGrammar:
    """Complete traversal of query_tree_schema, within explicit resource bounds.

    `choose` receives only current input, legal candidates and the already built
    prefix. The target is available solely to teacher(), outside this interface.
    Each encoder invocation is a prefill, without a language-generation head.
    """

    def __init__(self, turns, graph, *, max_decisions=1024, max_depth=64):
        base, bindings = request(turns, graph)
        self.graph, self.base = graph, base
        self.schema = ast.query_tree_schema()
        self.max_decisions, self.max_depth = max_decisions, max_depth
        values = list(bindings["b0.where0.value"].values())
        values = [v for v in values if not isinstance(v, dict)]
        # Grammar/measurement constants, not facts inferred from a gold query.
        values.extend((False, True, -1, -1.0, 0.0, 1.0))
        self._question_spans = {}
        from engine.sql_schema import canon
        comparable_spans = set()
        for turn_index, turn in enumerate(base["state"]["conversation"]):
            content = turn["content"]
            tokens = list(re.finditer(r"\w+|[^\w\s]", content))
            if len(tokens) > 512:
                raise InvalidDecision(
                    "question span bank exceeds its explicit 512-token resource bound"
                )
            for i, start in enumerate(tokens):
                for stop in tokens[i:]:
                    if stop.end() - start.start() > 4096:
                        break
                    value = content[start.start() : stop.end()]
                    self._question_spans[_key(value)] = (
                        turn_index,
                        start.start(),
                        stop.end(),
                        value,
                    )
            words = list(re.finditer(r"[^\W_]+(?:'[^\W_]+)?", content))
            for i in range(len(words)):
                for stop in range(i + 1, min(i + 8, len(words)) + 1):
                    parts = [word[0] for word in words[i:stop]]
                    stems = [canon(part) for part in parts]
                    comparable_spans.add(" ".join(stems))
                    comparable_spans.add(" ".join(
                        stem.upper() if part.isupper() else
                        stem[:1].upper() + stem[1:] if part[:1].isupper() else stem
                        for part, stem in zip(parts, stems)))
        values.extend(span[3] for span in self._question_spans.values())
        # Word-form alternatives are choices, never replacements for source
        # values or a decision that two distinct names mean the same entity.
        values.extend(sorted(comparable_spans))
        text = "\n".join(turn["content"] for turn in base["state"]["conversation"])
        # Named record identifiers may separate their label and digits in
        # conversation while the source stores one token ("member 313").
        # Offer the written components together; this never selects a record.
        values.extend(match[1] + match[2] for match in
                      re.finditer(r"\b([A-Za-z_]+)\s+(\d+)\b", text))
        words = frozenset(re.findall(r"[^\W_]+", text.casefold()))
        if words & {"percent", "percentage", "percentages"} or "%" in text:
            values.extend((100, 100.0))
        observed, styles, domains, durations = _literal_observations(graph)
        values.extend(domain for domain in domains
                      if domain[1:].casefold() in text.casefold()
                      or domain[1:].split('.')[0].casefold() in words)
        rows, frequencies, row_index = _row_observations(graph)
        candidates, cutoff = set(), None
        for frequency, word in sorted((frequencies[word], word) for word in words if frequencies[word]):
            if cutoff is not None and frequency > cutoff:
                break
            candidates.update(row_index[word])
            if len(candidates) >= 16:
                cutoff = frequency
        ranked_rows = []
        for i in sorted(candidates):
            table, row, cells, tokens = rows[i]
            matched = tokens & words
            if matched:
                ranked_rows.append((min(frequencies[word] for word in matched), -len(matched), table, row, cells))
        self._row_evidence = sorted(ranked_rows, key=lambda item: item[:4])[:16]
        # Completed input lookups identify actual rows, even when a code such
        # as an affirmative answer shares no words with the user's question.
        # Source cells remain unchanged; this metadata cannot invent values.
        explicit = set(graph.source_rows)
        if explicit:
            ordered = {(table, row): (table, row, cells) for table, row, cells, _ in rows}
            selected, characters = [], 0
            for key in graph.source_rows:
                table, row, cells = ordered[key]
                visible = {name: _source_value(value) for name, value in cells
                           if not isinstance(value, str) or len(value) <= 256}
                size = len(_key({"table": table, "cells": visible}))
                if len(selected) >= _REFERENCE_ROW_BOUND or characters + size > _REFERENCE_TEXT_BOUND:
                    continue
                selected.append((0, 0, table, row, cells)); characters += size
            self._row_evidence = selected + [item for item in self._row_evidence if item[2:4] not in explicit]
        # Small reference/code tables must expose the relation between an ID
        # and its description, not just the first nine values of each column.
        # Append bounded, target-independent row facts after relevant rows.
        selected_rows = {(table, row) for _, _, table, row, _ in self._row_evidence}
        reference_rows, reference_characters = 0, 0
        small_tables = {table for table, columns in graph.by_table.items()
                        if max((len(column.values) for column in columns), default=0) <= _REFERENCE_ROW_BOUND}
        for table, row, cells, _ in rows:
            if reference_rows >= _REFERENCE_ROW_BOUND:
                break
            if (table, row) in selected_rows:
                continue
            if table not in small_tables:
                continue
            visible = {name: _source_value(value) for name, value in cells
                       if not isinstance(value, str) or len(value) <= 256}
            size = len(_key({"table": table, "cells": visible}))
            if reference_characters + size > _REFERENCE_TEXT_BOUND:
                continue
            self._row_evidence.append((0, 0, table, row, cells))
            selected_rows.add((table, row))
            reference_rows += 1; reference_characters += size
        for _, _, _, _, cells in self._row_evidence:
            values.extend(value for _, value in cells if isinstance(value, (str, int, float, Decimal, bool, type(None)))
                          and (not isinstance(value, str) or len(value) <= 256))
        for strings, index in observed:
            if (
                len(strings) <= 32
                or len(strings) <= 1024
                and all(len(value) <= 8 for value, _ in strings)
            ):
                values.extend(value for value, _ in strings)
            from collections import Counter
            overlaps = Counter(i for word in words for i in index.get(word, ()))
            matching = sorted(
                (
                    (
                        -count / max(1, len(strings[i][1])),
                        -count,
                        strings[i][0],
                    )
                    for i, count in overlaps.items()
                )
            )
            values.extend(value for _, _, value in matching[:9])
            # Repeated long labels must not crowd a compact stored spelling
            # out of the input choices. Keep the relevance choices above and
            # offer a bounded second view of actual, input-overlapping cells.
            # This neither invents an abbreviation nor chooses its meaning.
            compact_words = sorted((len(index[word]), word) for word in words
                                   if len(word) >= 4 and word in index)[:9]
            for _, word in compact_words:
                compact = sorted((len(strings[i][1]), len(strings[i][0]), strings[i][0])
                                 for i in index[word]
                                 if len(strings[i][0]) <= 128 and len(strings[i][1]) <= 3)
                values.extend(value for _, _, value in compact[:3])
        from engine.readers.numerals import mentions, intervals
        from engine.sql_dates import MONTHS

        numbers = list(mentions(text))
        implied_numbers = set()
        for low, high, _, _ in intervals(text):
            if type(low) is int and type(high) is int and 0 <= high - low <= 50:
                implied_numbers.update(range(low, high + 1))
        values.extend(sorted(implied_numbers))
        # Supplied column dictionaries are input facts, not target literals.
        # Offer their explicit numbers and unit arithmetic as choices. A named
        # decision must still select the column, operation and actual unit.
        from fractions import Fraction
        from engine.numeric import DECIMAL_PRECISION, parse_decimal
        from decimal import localcontext
        definition_numbers = set()
        def offer_number(value):
            values.append(int(value) if value == value.to_integral_value() else value)
            if value != value.to_integral_value() and Decimal(str(float(value))) == value:
                values.append(float(value))
        for column in graph.columns:
            for _, definition in column.annotations:
                for value, _, _ in mentions(definition):
                    try:
                        definition_numbers.add(parse_decimal(value))
                    except (ValueError, TypeError):
                        continue
                values.extend(next(part for part in groups if part) for groups in
                              re.findall(r'"([^"\n]{1,256})"|\x27([^\x27\n]{1,256})\x27', definition))
        for value in sorted(definition_numbers):
            offer_number(value)
        for factor in sorted(definition_numbers):
            if factor in (0, 1, -1):
                continue
            for number, _, _ in numbers:
                try:
                    rational = Fraction(parse_decimal(number))
                except (ValueError, TypeError):
                    continue
                for converted in (rational * Fraction(factor), rational / Fraction(factor)):
                    denominator = converted.denominator
                    for prime in (2, 5):
                        while denominator % prime == 0:
                            denominator //= prime
                    if denominator != 1:
                        continue
                    try:
                        with localcontext() as context:
                            context.prec = DECIMAL_PRECISION
                            value = parse_decimal(Decimal(converted.numerator) / converted.denominator)
                    except (ValueError, TypeError):
                        continue
                    offer_number(value)
        for match in re.finditer(r"(?<!\d)([12]\d{2}0)['’]?s\b", text):
            decade = int(match[1]); values.extend((decade, decade + 9, decade + 10))
        for word, decade in (("twenties", 20), ("thirties", 30), ("forties", 40), ("fifties", 50),
                             ("sixties", 60), ("seventies", 70), ("eighties", 80), ("nineties", 90)):
            if word in words:
                values.extend((decade, decade + 9, decade + 10))
        # Offer unit conversions as candidates; the named readout still has to
        # select the source column's actual unit. Never infer it from a target.
        for number, _, end in numbers:
            unit = re.match(r"\s*(seconds?|minutes?|hours?|days?|weeks?)\b", text[end:], re.IGNORECASE)
            if unit:
                from fractions import Fraction
                seconds = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}
                scale = seconds[unit[1].lower().rstrip("s")]
                for target_scale in seconds.values():
                    fraction = Fraction(Decimal(str(number))) * scale / target_scale
                    denominator = fraction.denominator
                    for factor in (2, 5):
                        while denominator % factor == 0:
                            denominator //= factor
                    if denominator != 1:
                        continue
                    converted = Decimal(fraction.numerator) / fraction.denominator
                    values.append(int(converted) if converted == converted.to_integral_value() else converted)
                values.append(scale)
                duration = Fraction(Decimal(str(number))) * scale
                if duration.denominator == 1 and 0 <= duration <= 999 * 3600:
                    total_seconds = int(duration)
                    hour, remainder = divmod(total_seconds, 3600)
                    minute, second = divmod(remainder, 60)
                    # These are alternatives only when the supplied source has
                    # the corresponding colon-separated duration representation.
                    if 'hms' in durations:
                        values.extend((f"{hour:02d}:{minute:02d}:{second:02d}",
                                       f"{hour}:{minute:02d}:{second:02d}"))
                    if 'ms' in durations:
                        values.extend((f"{total_seconds // 60}:{second:02d}",
                                       f"{total_seconds // 60}:"))
        if words & {"celsius", "fahrenheit"}:
            values.extend((32, 1.8, Decimal("1.8"), 5, 9))
        if words & {"teenage", "teenager", "teenagers", "teens"}:
            values.extend((13, 19, 20))
        for number, _, end in numbers:
            if re.match(r"\s*(?:%|percent\b)", text[end:], re.IGNORECASE):
                fraction = Decimal(str(number)) / 100
                values.extend(
                    (float(fraction), fraction)
                )
                if words & {"above", "below", "higher", "lower", "more", "less"}:
                    for factor in (1 + fraction, 1 - fraction):
                        values.extend((factor, float(factor)))
        years = {
            number
            for number, _, _ in numbers
            if type(number) is int and 1000 <= number <= 9999
        }
        years.update(number for number in implied_numbers if 1000 <= number <= 9999)
        months = {MONTHS[word] for word in words if word in MONTHS}
        months.update(
            number
            for number, _, _ in numbers
            if type(number) is int and 1 <= number <= 12
        )
        from calendar import monthrange

        # Calendar boundaries have one owner. Read half-years, quarters and
        # ordinal months through it instead of guessing numeric constants.
        from engine.sql_dates import date_phrases
        from engine.sql_expansion import tokens
        calendar_column = ast.ColumnRef(graph.tables[0], "calendar", ast.SQLType.DATE)
        for phrase in date_phrases(text, tokens(text)):
            for comparison in phrase.comparisons(calendar_column):
                if comparison.right.type is ast.SQLType.DATE:
                    values.append(comparison.right.value)
            if phrase.year is not None:
                years.add(phrase.year)
                months.add(phrase.month)
                if phrase.until is not None:
                    end_year, end_month, _ = phrase.until
                    if end_year is not None:
                        years.add(end_year)
                    months.add(end_month)

        if any(column.ref.type is ast.SQLType.DATE for column in graph.columns):
            styles = tuple(sorted(set(styles) | {("ymd", "-")}))
        # A question can name March without a year. The source's written-month
        # representation still licenses its standard abbreviation and casing.
        if any(order.startswith('dmony_') for order, _ in styles):
            from calendar import month_abbr
            for month in sorted(months):
                name = month_abbr[month]
                values.extend((name, name.upper(), name.lower()))
        days = {
            number
            for number, _, _ in numbers
            if type(number) is int and 1 <= number <= 31
        }
        if {"from", "to"} <= words and len(days) >= 2 and months and years:
            days.update(range(1, 32))
        for order, separator in styles:
            for year in sorted(years):
                if order.startswith("dmony_"):
                    from calendar import month_abbr
                    for month in sorted(months):
                        name = getattr(month_abbr[month], order.split('_')[1])()
                        values.extend((name, f"{separator}{name}{separator}{year}"))
                        for day in sorted(days | {1, monthrange(year, month)[1]}):
                            if day <= monthrange(year, month)[1]:
                                values.extend((f"{day}{separator}{name}{separator}{year}",
                                               f"{day:02d}{separator}{name}{separator}{year}"))
                    continue
                source_year = str(year)[-2:] if order.endswith('2') else str(year)
                source_order = order.rstrip('2')
                if order == "ymd":
                    values.extend(
                        (f"{year}-01-01", f"{year}-12-31", f"{year + 1}-01-01",
                         f"{year}{separator}")
                    )
                else:
                    values.append(f"%{separator}%{separator}{source_year}")
                for month in sorted(months):
                    last = monthrange(year, month)[1]
                    for m in {str(month), f"{month:02d}"}:
                        if order == "ymd":
                            values.extend((f"{year}{separator}{m}", f"{year}{separator}{m}{separator}"))
                        elif source_order == "mdy":
                            values.append(f"{m}{separator}%{separator}{source_year}")
                        else:
                            values.append(f"%{separator}{m}{separator}{source_year}")
                        for day in sorted(days | {1, last}):
                            if day > last:
                                continue
                            for d in {str(day), f"{day:02d}"}:
                                parts = (
                                    (str(year), m, d)
                                    if order == "ymd"
                                    else (m, d, source_year)
                                    if source_order == "mdy"
                                    else (d, m, source_year)
                                )
                                values.append(separator.join(parts))
        timestamp = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})\s+(?:at\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\b", re.IGNORECASE)
        clocks = [(int(m[3]), int(m[1]), int(m[2]), int(m[4]), int(m[5]), int(m[6] or 0),
                   (m[7] or "").upper(), True) for m in timestamp.finditer(text)]
        ymd = r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"
        time_of_day = r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\b"
        for match in re.finditer(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(AM|PM)\b", text, re.IGNORECASE):
            hour, minute, second = int(match[1]), int(match[2] or 0), int(match[3] or 0)
            if not 1 <= hour <= 12 or minute > 59 or second > 59:
                continue
            hour = hour % 12 + (12 if match[4].upper() == 'PM' else 0)
            values.extend((f"{hour:02d}:{minute:02d}:{second:02d}", f"{hour:02d}:{minute:02d}"))
        for match in re.finditer(ymd + r"\s+(?:at\s+)?" + time_of_day, text, re.IGNORECASE):
            clocks.append((*map(int, match.group(1, 2, 3, 4, 5)), int(match[6] or 0),
                           (match[7] or "").upper(), False))
        for match in re.finditer(time_of_day + r"\s+on\s+" + ymd, text, re.IGNORECASE):
            clocks.append((*map(int, match.group(5, 6, 7, 1, 2)), int(match[3] or 0),
                           (match[4] or "").upper(), False))
        for year, first, second, hour, minute, second_of_minute, meridiem, reversible in clocks:
            if minute > 59 or hour > (12 if meridiem else 23) or meridiem and hour < 1:
                continue
            hour = hour % 12 + (12 if meridiem == "PM" else 0) if meridiem else hour
            dates = []
            for month, day in ((first, second), (second, first)) if reversible else ((first, second),):
                try:
                    dates.append(datetime(year, month, day, hour, minute, second_of_minute))
                except ValueError:
                    continue
            for value in dates:
                for order, separator in styles:
                    if order not in {'ymd', 'mdy', 'dmy'}:
                        continue
                    fmt = {"ymd": "%Y{0}%m{0}%d", "mdy": "%m{0}%d{0}%Y", "dmy": "%d{0}%m{0}%Y"}[order].format(separator)
                    values.append(value.strftime(fmt + " %H:%M:%S"))
        for column in graph.columns:
            for value in column.values[:9]:
                if isinstance(value, (date, datetime)):
                    value = value.isoformat()
                if isinstance(value, (str, int, float, Decimal, bool, type(None))):
                    values.append(value)
        self.literals = {}
        for value in values:
            tree = ast.query_to_tree(
                ast.SelectQuery((ast.SelectItem(ast.Literal(value)),), graph.tables[0])
            )
            encoded = tree["select"][0]["expression"]["value"]
            self.literals[_key(encoded)] = encoded
        self.literals = [value for _, value in sorted(self.literals.items())]
        self._literal_keys = {_key(value): value for value in self.literals}
        self._lower_literals, self._upper_literals = {}, {}
        for value in self.literals:
            if isinstance(value, str):
                self._lower_literals.setdefault(value.lower(), value)
                self._upper_literals.setdefault(value.upper(), value)
        self.history, self.decisions = [], []
        self._column_cache = {}
        self._aliases = {}

    def role(self, path, owner, field):
        if path == "$":
            return "role.scope_parent"
        if owner == "Join":
            return (
                "role.join_left"
                if field == "left"
                else "role.join_right"
                if field == "right"
                else "role.relation_target"
            )
        if owner == "Aggregate" and field == "operand":
            return "role.measure"
        if owner == "DateSpan":
            return "role." + {
                "start": "temporal_start",
                "end": "temporal_end",
                "until": "temporal_end",
                "unit": "unit",
            }.get(field, "modifier")
        if owner == "SelectQuery":
            direct = {
                "where": "sql.filter",
                "having": "sql.having",
                "group_by": "intent_group",
                "order_by": "ace.frame.ordering",
                "limit": "intent_limit",
                "distinct": "sql.distinct_rows",
                "with_ties": "sql.with_ties",
            }
            if field in direct:
                return direct[field]
        if field in {"from_table", "table"}:
            return "role.record_grain"
        if field in {
            "alias",
            "from_alias",
            "type",
            "function",
            "operator",
            "direction",
            "part",
            "negated",
            "distinct",
        }:
            return "role.modifier"
        if field in {"query", "source"} and owner in {
            "ScalarSubquery",
            "ExistsPredicate",
            "InPredicate",
            "SubquerySource",
        }:
            return "role.scope_child"
        clauses = re.findall(
            r"\.(select|where|having|order_by|group_by)(?:\b|\[)", path
        )
        if clauses:
            clause = clauses[-1]
            if clause == "order_by":
                return "role.sort_key"
            if clause == "group_by":
                return "role.group_key"
            if clause == "select":
                return "role.projection"
            return (
                "role.filter_value"
                if field in {"right", "value", "source"}
                else "role.filter_attribute"
            )
        return "role.modifier"

    def choose(self, path, kind, options, role, *, owner=None, field=None, absent=()):
        if len(self.history) >= self.max_decisions:
            raise InvalidDecision(
                "recursive readout exceeds its decision resource bound"
            )
        validating = getattr(self, "_validation_only", False)
        if validating and options is self.literals:
            options = self._validation_literals
        else:
            options = {_key(value): value for value in options}
            options = {f"o{i}": value for i, value in enumerate(options.values())}
        if not options:
            raise InvalidDecision("grammar field has no input-grounded candidate")
        if len(options) == 1:
            value = next(iter(options.values()))
            self.history.append(
                {"path": path, "kind": kind, "value": deepcopy(value), "forced": True}
            )
            return value
        if validating:
            choice = self.callback({"path": path, "kind": kind, "options": options})
            if choice not in options:
                raise InvalidDecision("missing, refused or illegal named AST choice")
            value = deepcopy(options[choice])
            self.history.append({"path": path, "kind": kind, "value": value})
            return value
        coordinates = {key: role for key in options}
        if owner and field:
            try:
                meanings = named_operation_meanings(owner, field)
            except InvalidDecision:
                meanings = {}
            for key, value in options.items():
                if isinstance(value, str) and value in meanings:
                    coordinates[key] = meanings[value]
        identity = path + "/" + kind

        def materialize():
            propositions = named_propositions(
                identity,
                options,
                coordinates,
                absent=[
                    key
                    for key, value in options.items()
                    if any(_key(value) == _key(a) for a in absent)
                ],
            )
            context = {
                "field": path,
                "kind": kind,
                "owner": owner,
                "attribute": field,
                "prefix": deepcopy(self.history),
                "instruction": "Choose exactly the input-grounded candidate that occupies this field of the complete requested query. "
                "A presence proposition is false when the field/list is absent or finished. No invented constraints.",
            }
            state = {
                "schema": [
                    *self.base["state"]["schema"],
                    {"ast_readout": context},
                    {"foreign_keys": self.base["state"]["foreign_keys"]},
                ],
                "conversation": self.base["state"]["conversation"],
            }
            return {
                "request": propositions,
                "input": state,
                "items": [d["item"] for d in propositions.values()],
            }

        record = _DecisionInput(
            {
                "path": path,
                "kind": kind,
                "options": options,
                "coordinates": coordinates,
                "absent": [
                    key
                    for key, value in options.items()
                    if any(_key(value) == _key(a) for a in absent)
                ],
                "position": len(self.history),
            },
            materialize,
        )
        choice = self.callback(record)
        if choice not in options:
            raise InvalidDecision("missing, refused or illegal named AST choice")
        value = deepcopy(options[choice])
        self.history.append({"path": path, "kind": kind, "value": value})
        return value

    def bank(self, positions=256):
        """Target-independent options and positions for one encoder prefill.

        Source columns precede the conversation. Candidate literals/control
        symbols and position queries follow it. Aliased references compose the
        existing physical-column state with an explicit alias state; they do
        not require a separate encoder invocation or opaque pointer head.
        """
        if type(positions) is not int or not 1 <= positions <= 1024:
            raise InvalidDecision("parallel grammar position bound must be explicit")
        values = {
            _key(value): value
            for value in [
                False,
                True,
                None,
                "answer",
                "clarify",
                *self.literals,
                *self.graph.tables,
                *(f"a{i}" for i in range(32)),
                *(f"c{i}" for i in range(64)),
                "preserve",
                "lower",
                "upper",
                "exact",
                "contains",
                "prefix",
                "suffix",
            ]
        }

        def collect(spec):
            if isinstance(spec, dict):
                for value in spec.get("enum", []):
                    values[_key(value)] = value
                for branch in spec.get("oneOf", []):
                    value = {"form": branch.get("$ref", branch.get("type"))}
                    values[_key(value)] = value
                for value in spec.values():
                    collect(value)
            elif isinstance(spec, list):
                for value in spec:
                    collect(value)

        collect(self.schema)
        items, self._bank_index = [], {}
        for column in self.graph.columns:
            value = self._column_tree(column.ref)
            self._bank_index[_key(value)] = len(items)
            items.append(
                {
                    "identity": "bank/" + str(len(items)),
                    "kind": "column",
                    "text": "Source column: " + _key(value),
                }
            )
        for _, _, table, row, cells in self._row_evidence:
            visible = {name: _source_value(value) for name, value in cells
                       if (not isinstance(value, str) or len(value) <= 256)}
            items.append({"identity": f"source-row/{table}/{row}", "kind": "frame",
                          "text": "Observed source row: " + _key({"table": table, "cells": visible})})
        for i in range(64):
            value = {"derived_column": f"c{i}"}
            values[_key(value)] = value
        for key, value in sorted(values.items()):
            if key in self._bank_index:
                continue
            self._bank_index[key] = len(items)
            if key in self._question_spans:
                turn, start, stop, text = self._question_spans[key]
                items.append(
                    {
                        "identity": "bank/" + str(len(items)),
                        "kind": "span",
                        "text": text,
                        "turn": turn,
                        "start": start,
                        "stop": stop,
                    }
                )
            else:
                items.append(
                    {
                        "identity": "bank/" + str(len(items)),
                        "kind": "frame",
                        "text": "Candidate: " + key,
                    }
                )
        self._position_indices = []
        for i in range(positions):
            self._position_indices.append(len(items))
            items.append(
                {
                    "identity": f"position/{i}",
                    "kind": "control",
                    "text": f"Requested AST decision number {i}.",
                }
            )
        self._action_index = len(items)
        items.append(
            {
                "identity": "response-action",
                "kind": "control",
                "text": "Is the user's intended question complete: answer or clarify?",
            }
        )
        state = {
            "schema": [
                *source_schema(self.graph),
                {"foreign_keys": self.base["state"]["foreign_keys"]},
                {
                    "grammar": {
                        "schema_sha256": self.schema_sha256,
                        "positions": positions,
                        "traversal": "All choices, including forced choices, in preorder; query sources and all join sources precede expressions/predicates; lists choose more/end.",
                        "nodes": {
                            name: list(spec.get("properties", {}))
                            for name, spec in self.schema["$defs"].items()
                        },
                    }
                },
            ],
            "conversation": self.base["state"]["conversation"],
        }
        return state, items

    def components(self, record, value):
        """Physical identities/scope mappings only; no semantic selection here."""
        if record["kind"] != "column":
            try:
                return [self._bank_index[_key(value)]]
            except KeyError as exc:
                raise InvalidDecision(
                    "input option is missing from the shared bank"
                ) from exc
        table, name = value["table"], value["name"]
        physical = self._aliases.get(table, table)
        if physical is None:
            parts = [
                self._bank_index[_key({"derived_column": name})],
                self._bank_index[_key(value.get("type", "unknown"))],
            ]
        else:
            ref = self.graph.column_map.get((physical, name))
            if ref is None:
                raise InvalidDecision("shared-bank column has no physical origin")
            parts = [self._bank_index[_key(self._column_tree(ref.ref))]]
        if table != physical:
            parts.append(self._bank_index[_key(table)])
        return parts

    def training_example(self, query=None, *, action="answer", positions=256):
        """One target-independent prefill, with all scoped decisions as labels."""
        if action not in {"answer", "clarify"} or (action == "answer") != (
            query is not None
        ):
            raise InvalidDecision(
                "a joint readout requires its source-annotated action"
            )
        state, items = self.bank(positions)
        pairs, targets, outputs, groups = [], [], [], []

        def group(
            identity, query_index, options, coordinates, absent, selected, components
        ):
            start = len(pairs)
            polarities = []
            for key in options:
                polarity = -1 if key in absent else 1
                pairs.append(
                    {
                        "query": query_index,
                        "components": components[key],
                        "interaction": True,
                    }
                )
                targets.append(
                    {coordinates[key]: int((key == selected) == (polarity == 1))}
                )
                outputs.append({"identity": identity + "/" + key, "kind": "frame"})
                polarities.append(polarity)
            groups.append(
                {
                    "start": start,
                    "stop": len(pairs),
                    "chosen": start + list(options).index(selected),
                    "polarities": polarities,
                }
            )

        actions = {key: key for key in ("answer", "clarify")}
        group(
            "action",
            self._action_index,
            actions,
            {key: "dialogue." + key for key in actions},
            (),
            action,
            {key: [self._bank_index[_key(key)]] for key in actions},
        )
        realized, decisions = None, []
        if query is not None:
            decisions, realized = self.teacher(query, capture=False)
            if len(self.history) > positions:
                raise InvalidDecision(
                    "checked target exceeds the parallel decision bound"
                )
            for decision in decisions:
                group(
                    decision["path"] + "/" + decision["kind"],
                    self._position_indices[decision["position"]],
                    decision["options"],
                    decision["coordinates"],
                    decision["absent"],
                    decision["selected"],
                    decision["components"],
                )
        for column in self.graph.columns:
            if column.ref.type is ast.SQLType.UNKNOWN:
                continue
            index = self._bank_index[_key(self._column_tree(column.ref))]
            pairs.append({"query": index, "components": [index], "interaction": False})
            targets.append(
                {
                    "is_num": int(column.ref.type.numeric),
                    "is_str": int(column.ref.type is ast.SQLType.TEXT),
                    "is_time": int(column.ref.type is ast.SQLType.DATE),
                    "is_bool": int(column.ref.type is ast.SQLType.BOOLEAN),
                }
            )
            outputs.append({"identity": items[index]["identity"], "kind": "column"})
        return (
            {
                "input": state,
                "items": items,
                "pairs": pairs,
                "targets": targets,
                "outputs": outputs,
                "choice_groups": groups,
                "target_ast": None,
                "parallel_positions": positions,
                "grammar_sha256": self.schema_sha256,
                "annotation_scope": "All choices supervise named axes after one target-independent prefill; no AST prefix enters encoder input.",
            },
            realized,
            decisions,
        )

    def _strings(self, field):
        if field in {"table", "from_table"}:
            return list(self.graph.tables)
        if field in {"alias", "from_alias"}:
            return [f"a{i}" for i in range(32)] + [f"c{i}" for i in range(64)]
        return [value for value in self.literals if isinstance(value, str)]

    def _column_tree(self, ref):
        if ref in self._column_cache:
            return self._column_cache[ref]
        physical = (
            ref.table if ref.table in self.graph.by_table else self.graph.tables[0]
        )
        wrapper = ast.SelectQuery(
            (ast.SelectItem(ref),),
            physical,
            from_alias=ref.table if ref.table not in self.graph.by_table else None,
        )
        result = ast.query_to_tree(wrapper)["select"][0]["expression"]
        self._column_cache[ref] = result
        return result

    def walk(
        self, spec, path, scope, *, owner=None, field=None, depth=0, role_hint=None
    ):
        if depth > self.max_depth:
            raise InvalidDecision("recursive readout exceeds its depth resource bound")
        if "$ref" in spec:
            name = spec["$ref"].rsplit("/", 1)[-1]
            return self.walk(
                self.schema["$defs"][name],
                path,
                scope,
                owner=name,
                field=field,
                depth=depth,
                role_hint=role_hint or self.role(path, owner, field),
            )
        role = role_hint or self.role(path, owner, field)
        if "oneOf" in spec:
            branches = spec["oneOf"]
            forms = [
                {"form": branch.get("$ref", branch.get("type"))} for branch in branches
            ]
            chosen = self.choose(path, "form", forms, role, owner=owner, field=field)
            return self.walk(
                branches[forms.index(chosen)],
                path,
                scope,
                owner=owner,
                field=field,
                depth=depth + 1,
                role_hint=role,
            )
        if "const" in spec:
            return spec["const"]
        if "enum" in spec:
            return self.choose(
                path, "enum", spec["enum"], role, owner=owner, field=field
            )
        kind = spec.get("type")
        if kind == "object":
            node = spec.get("properties", {}).get("node", {}).get("const")
            if node == "ColumnRef":
                options = [self._column_tree(ref) for ref in scope.values()]
                return self.choose(path, "column", options, role)
            if node == "Literal":
                return self.literal(spec, path, scope, depth, role)
            value, own = {}, dict(scope)
            names = list(spec["properties"])
            if node == "SelectQuery":
                first = ["node", "from_table", "from_alias", "joins"]
                names = first + [name for name in names if name not in first]
            elif node == "Join":
                first = ["node", "table", "alias"]
                names = first + [name for name in names if name not in first]
            for name in names:
                prop = spec["properties"][name]
                if name not in spec.get("required", []):
                    present = self.choose(
                        path + "." + name,
                        "present",
                        [False, True],
                        self.role(path, node, name),
                        owner=node,
                        field=name,
                        absent=(False,),
                    )
                    if not present:
                        continue
                value[name] = self.walk(
                    prop,
                    path + "." + name,
                    own,
                    owner=node,
                    field=name,
                    depth=depth + 1,
                )
                if node == "SelectQuery" and name in {
                    "from_table",
                    "from_alias",
                    "joins",
                }:
                    own = dict(scope)
                    own.update(self.source_columns(value))
                elif node == "Join" and name in {"table", "alias"}:
                    own = dict(scope)
                    own.update(
                        self.source_columns(
                            {
                                "from_table": value["table"],
                                "from_alias": value.get("alias"),
                            }
                        )
                    )
            return value
        if kind == "array":
            if "prefixItems" in spec:
                return [
                    self.walk(
                        part,
                        path + f"[{i}]",
                        scope,
                        owner=owner,
                        field=field,
                        depth=depth + 1,
                    )
                    for i, part in enumerate(spec["prefixItems"])
                ]
            if owner == "SelectQuery" and field == "joins":
                return self.joins(spec, path, scope, depth)
            value = []
            while True:
                if len(value) >= spec.get("minItems", 0):
                    more = self.choose(
                        path + f"[{len(value)}]",
                        "more",
                        [False, True],
                        role,
                        absent=(False,),
                    )
                    if not more:
                        break
                if len(value) >= 256:
                    raise InvalidDecision("AST list exceeds its resource bound")
                value.append(
                    self.walk(
                        spec["items"],
                        path + f"[{len(value)}]",
                        scope,
                        owner=owner,
                        field=field,
                        depth=depth + 1,
                    )
                )
            return value
        if kind == "null":
            return None
        if kind == "boolean":
            return self.choose(
                path,
                "boolean",
                [False, True],
                role,
                owner=owner,
                field=field,
                absent=(False,),
            )
        if kind == "integer":
            return self.choose(
                path, "integer", [v for v in self.literals if type(v) is int], role
            )
        if kind == "string":
            return self.choose(path, "text", self._strings(field), role)
        raise InvalidDecision("unhandled SQL grammar schema: " + _key(spec))

    def joins(self, spec, path, scope, depth):
        """Read every join source before any predicate, preserving full scope."""
        definition = self.schema["$defs"]["Join"]
        values = []
        while self.choose(
            path + f"[{len(values)}]",
            "more",
            [False, True],
            "role.relation_target",
            absent=(False,),
        ):
            if len(values) >= 256:
                raise InvalidDecision("join list exceeds its resource bound")
            stem = path + f"[{len(values)}]"
            value = {
                "node": "Join",
                "table": self.walk(
                    definition["properties"]["table"],
                    stem + ".table",
                    scope,
                    owner="Join",
                    field="table",
                    depth=depth + 1,
                ),
            }
            if self.choose(
                stem + ".alias",
                "present",
                [False, True],
                "role.relation_target",
                absent=(False,),
            ):
                value["alias"] = self.walk(
                    definition["properties"]["alias"],
                    stem + ".alias",
                    scope,
                    owner="Join",
                    field="alias",
                    depth=depth + 1,
                )
            values.append(value)
        own = dict(scope)
        own.update(self.source_columns({"joins": values}))
        for i, value in enumerate(values):
            for name, prop in definition["properties"].items():
                if name in {"node", "table", "alias"}:
                    continue
                stem = path + f"[{i}]." + name
                if name not in definition["required"] and not self.choose(
                    stem,
                    "present",
                    [False, True],
                    self.role(stem, "Join", name),
                    absent=(False,),
                ):
                    continue
                value[name] = self.walk(
                    prop, stem, own, owner="Join", field=name, depth=depth + 1
                )
        return values

    def source_columns(self, partial):
        result = {}
        for source, alias in [
            (partial.get("from_table"), partial.get("from_alias")),
            *((j["table"], j.get("alias")) for j in partial.get("joins", [])),
        ]:
            if source is None:
                continue
            if isinstance(source, str):
                if source not in self.graph.by_table:
                    raise InvalidDecision("unknown input source")
                for column in self.graph.by_table[source]:
                    ref = replace(column.ref, table=alias or source)
                    result[(ref.table, ref.name)] = ref
                self._aliases[alias or source] = source
            else:
                self._aliases[source["alias"]] = None
                query = ast.query_from_tree(source["query"])
                selected = query.left if isinstance(query, ast.SetQuery) else query
                for item in selected.select:
                    name = item.alias or (
                        item.expression.name
                        if isinstance(item.expression, ast.ColumnRef)
                        else None
                    )
                    if name is not None:
                        ref = ast.ColumnRef(
                            source["alias"], name, ast.value_type(item.expression)
                        )
                        result[(ref.table, ref.name)] = ref
        return result

    def literal(self, spec, path, scope, depth, role):
        raw = self.choose(path + ".value", "literal", self.literals, role)
        value = raw
        if isinstance(raw, str):
            case = self.choose(
                path + ".value", "case", ["preserve", "lower", "upper"], "role.modifier"
            )
            value = (
                raw.lower()
                if case == "lower"
                else raw.upper()
                if case == "upper"
                else raw
            )
            pattern = self.choose(
                path + ".value",
                "pattern",
                ["exact", "contains", "prefix", "suffix"],
                "role.modifier",
            )
            value = (
                ("%" if pattern in {"contains", "suffix"} else "")
                + value
                + ("%" if pattern in {"contains", "prefix"} else "")
            )
        result = {"node": "Literal", "value": value}
        present = self.choose(
            path + ".type", "present", [False, True], "role.modifier", absent=(False,)
        )
        if present:
            result["type"] = self.walk(
                spec["properties"]["type"],
                path + ".type",
                scope,
                owner="Literal",
                field="type",
                depth=depth + 1,
            )
        return result

    def run(self, choose):
        self.callback, self.history, self.decisions = choose, [], []
        self._aliases = {}
        tree = self.walk(self.schema, "$", {})
        query = ast.query_from_tree(tree)
        validate_grounding(query, self.graph)
        return query

    def teacher(self, query, *, capture=True, collect=True):
        """Produce masked role labels by traversing an external checked target."""
        if not collect and capture is not False:
            raise InvalidDecision("validation without records requires capture=False")
        query = canonical_query(query)
        target = ast.query_to_tree(query)
        examples = []
        origins = {}

        def lookup(path):
            import re

            value = target
            for name, index in re.findall(r"\.([A-Za-z_]+)|\[([0-9]+)\]", path):
                if name:
                    if not isinstance(value, dict) or name not in value:
                        return False, None
                    value = value[name]
                elif not isinstance(value, list) or int(index) >= len(value):
                    return False, None
                else:
                    value = value[int(index)]
            return True, value

        def oracle(record):
            exists, value = lookup(record["path"])
            kind, options = record["kind"], record["options"]
            if kind in {"present", "more"}:
                wanted = exists
            elif kind == "form":
                form = (
                    "#/$defs/" + value["node"]
                    if isinstance(value, dict) and "node" in value
                    else "array"
                    if isinstance(value, list)
                    else "null"
                    if value is None
                    else "boolean"
                    if type(value) is bool
                    else "integer"
                    if type(value) is int
                    else "number"
                    if type(value) is float
                    else "string"
                )
                wanted = {"form": form}
            elif kind in {"literal", "case", "pattern"}:
                path = record["path"]
                if path not in origins:
                    choices = []
                    candidates = []
                    if _key(value) in self._literal_keys:
                        candidates.append(self._literal_keys[_key(value)])
                    if isinstance(value, str):
                        variants = [value]
                        if value.startswith("%") and value.endswith("%"):
                            variants.append(value[1:-1])
                        if value.startswith("%"):
                            variants.append(value[1:])
                        if value.endswith("%"):
                            variants.append(value[:-1])
                        for variant in variants:
                            if _key(variant) in self._literal_keys:
                                candidates.append(self._literal_keys[_key(variant)])
                            if variant in self._lower_literals:
                                candidates.append(self._lower_literals[variant])
                            if variant in self._upper_literals:
                                candidates.append(self._upper_literals[variant])
                    for raw in candidates:
                        for case in (
                            ("preserve", "lower", "upper")
                            if isinstance(raw, str)
                            else ("preserve",)
                        ):
                            adjusted = (
                                raw.lower()
                                if case == "lower"
                                else raw.upper()
                                if case == "upper"
                                else raw
                            )
                            for pattern in (
                                ("exact", "contains", "prefix", "suffix")
                                if isinstance(raw, str)
                                else ("exact",)
                            ):
                                candidate = (
                                    (
                                        (
                                            "%"
                                            if pattern in {"contains", "suffix"}
                                            else ""
                                        )
                                        + adjusted
                                        + (
                                            "%"
                                            if pattern in {"contains", "prefix"}
                                            else ""
                                        )
                                    )
                                    if isinstance(raw, str)
                                    else adjusted
                                )
                                if _key(candidate) == _key(value):
                                    choices.append(
                                        (
                                            case != "preserve",
                                            pattern != "exact",
                                            len(_key(raw)),
                                            _key(raw),
                                            raw,
                                            case,
                                            pattern,
                                        )
                                    )
                    if not choices:
                        error = InvalidDecision(
                            "target literal absent from input-derived values"
                        )
                        error.missing_binding = {"path": path, "value": value}
                        raise error
                    best = min(choices, key=lambda x: x[:4])
                    origins[path] = {
                        "literal": best[4],
                        "case": best[5],
                        "pattern": best[6],
                    }
                wanted = origins[path][kind]
            else:
                wanted = value
            if not collect and kind == "literal":
                selected = self._validation_literal_ids.get(_key(wanted))
                matches = [selected] if selected is not None else []
            else:
                matches = [key for key, item in options.items() if _key(item) == _key(wanted)]
            if len(matches) != 1:
                raise InvalidDecision(
                    "checked target absent from recursive grammar field "
                    + record["path"]
                )
            selected = matches[0]
            if not collect:
                return selected
            if capture is False:
                examples.append(
                    {
                        "path": record["path"],
                        "kind": record["kind"],
                        "selected": selected,
                        "position": record["position"],
                        "options": record["options"],
                        "coordinates": record["coordinates"],
                        "absent": record["absent"],
                        **(
                            {
                                "components": {
                                    key: self.components(record, value)
                                    for key, value in options.items()
                                }
                            }
                            if hasattr(self, "_bank_index")
                            else {}
                        ),
                    }
                )
                return selected
            if (
                isinstance(capture, set)
                and (record["path"], record["kind"]) not in capture
            ):
                return selected
            labels = [
                {
                    descriptor["coordinate"]: int(
                        (key == selected) == (descriptor["polarity"] == 1)
                    )
                }
                for key, descriptor in record["request"].items()
            ]
            examples.append(
                {
                    **record,
                    "targets": labels,
                    "selected": selected,
                    "annotation_scope": "typed target argument/operation; not an English span or static property label",
                }
            )
            return selected

        self._validation_only = not collect
        literals = {_key(v): v for v in self.literals} if not collect else {}
        self._validation_literals = {f"o{i}": value for i, value in enumerate(literals.values())}
        self._validation_literal_ids = {key: f"o{i}" for i, key in enumerate(literals)}
        try:
            recovered = self.run(oracle)
        finally:
            self._validation_only = False
            self._validation_literals = {}
            self._validation_literal_ids = {}
        if ast.query_to_tree(recovered) != target:
            raise InvalidDecision("recursive grammar supervision changed the target")
        return examples, recovered

    @property
    def schema_sha256(self):
        return hashlib.sha256(_key(self.schema).encode()).hexdigest()


def replay_query_trace(turns, graph, trace):
    """Rebuild the AST from recorded named values, without any encoder states."""
    from engine.readers.decisions import replay_named_field

    controller = ASTGrammar(turns, graph)
    if (
        trace["grammar_sha256"] != controller.schema_sha256
        or trace["hidden_choice_channels"] != 0
    ):
        raise InvalidDecision("trace grammar/interface differs from the active owner")
    state, items = controller.bank(trace["positions"])
    fingerprint = hashlib.sha256(
        _key({"input": state, "items": items}).encode()
    ).hexdigest()
    if trace["prefill_calls"] != 1 or fingerprint != trace["encoder_input_sha256"]:
        raise InvalidDecision("trace differs from its one input-only prefill")
    action_options = {key: key for key in ("answer", "clarify")}
    action_request = named_propositions(
        "action", action_options, {key: "dialogue." + key for key in action_options}
    )
    if (
        trace["action"]["request"] != action_request
        or trace["action"]["basis"] != trace["basis"]
    ):
        raise InvalidDecision(
            "action trace does not describe the active named response domain"
        )
    action = replay_named_field(trace["action"])
    if action["choice"] != "answer":
        if trace["fields"]:
            raise InvalidDecision("declined action has unexpected AST field choices")
        return None
    position = 0

    def choose(record):
        nonlocal position
        if position >= len(trace["fields"]):
            raise InvalidDecision("incomplete named AST trace")
        stored = trace["fields"][position]
        position += 1
        components = {
            key: controller.components(record, value)
            for key, value in record["options"].items()
        }
        if (
            stored["path"] != record["path"]
            or stored["kind"] != record["kind"]
            or stored["options"] != record["options"]
            or stored["components"] != components
            or stored["position"] != record["position"]
            or stored["decision"]["request"] != record["request"]
            or stored["decision"]["basis"] != trace["basis"]
        ):
            raise InvalidDecision(
                "named field trace differs from input-derived grammar candidates"
            )
        return replay_named_field(stored["decision"])["choice"]

    try:
        query = controller.run(choose)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        if (trace.get("final_action") == "clarify"
                and trace.get("readout_failure") == {"type": type(exc).__name__, "message": str(exc)}
                and position == len(trace["fields"]) and "model_ast" not in trace):
            return None
        raise
    if (
        position != len(trace["fields"])
        or ast.query_to_tree(query) != trace["model_ast"]
    ):
        raise InvalidDecision("named trace has unused choices or changed output AST")
    from engine.readers.ace import encode_query

    _, tree = encode_query(query)
    digest = hashlib.sha256(
        json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != trace["ace_tree_sha256"]:
        raise InvalidDecision("named trace ACE differs from its constructed AST")
    return query
