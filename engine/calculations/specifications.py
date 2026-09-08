"""Registered domain adapters for the generic calculation engine.

Specifications recognize intent and bind typed columns.  They do not render SQL and they do not
execute arithmetic.  The shared searcher materializes their plans as ASTs and the shared verifier
checks those ASTs after ranking.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from engine.calculations.core import (
    CalculationIntent,
    CalculationPlan,
    ComputationEvidence,
    branch_realizes_plan,
)
from engine.currency_intent import (
    CurrencyIntentKind,
    currency_intent,
    currency_rate_bindings,
    currency_rate_target,
    is_currency_measure_column,
    is_currency_reference_key,
    is_currency_source_column,
    substitute_currency_filter,
    substitute_currency_target,
)
from engine.numeric import parse_decimal
from engine.dataset_semantics import is_synthetic_currency_column, synthetic_currency_column
from engine.enrichment.value_types import ISO4217_CODES
from engine.sql_ast import Aggregate, BinaryExpr, ColumnRef, Literal, SQLType
from engine.sql_schema import SchemaGraph


_ID_WORDS = frozenset({"id", "identifier", "key", "code"})
_MONEY_WORDS = frozenset({
    "amount", "budget", "charge", "cost", "expense", "fee", "gdp", "income",
    "paid", "payment", "price", "principal", "profit", "revenue", "sale", "sales",
    "spend", "subtotal", "turnover", "value",
})
_PERSON_WORDS = frozenset({"capita", "inhabitant", "inhabitants", "people", "person", "persons", "population"})
_RATE_WORDS = frozenset({"fraction", "percent", "percentage", "pct", "rate"})
OperandScores = Mapping[str, Mapping[tuple[str, str], float]]


def _words(value: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    return tuple(word.lower() for word in re.findall(r"[A-Za-z0-9]+", spaced))


def _column_label(column: ColumnRef) -> str:
    return f"{column.table}.{column.name}"


def _role_score(operand_scores: OperandScores | None, role: str, column: ColumnRef) -> float:
    if not operand_scores:
        return 0.0
    return float(operand_scores.get(role, {}).get((column.table, column.name), 0.0))


def _literal_code(value: Any) -> str | None:
    text = str(value).strip().upper()
    return text if text in ISO4217_CODES else None


def _table_map(tables) -> dict[str, dict]:
    return {str(table.get("name") or ""): table for table in (tables or ())}


def _source_profile(tables, measure_columns: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    by_name = _table_map(tables)
    candidates = []
    for table_name, measure_name in measure_columns:
        table = by_name.get(table_name)
        if table is None:
            continue
        columns = [str(column) for column in (table.get("columns") or ())]
        private_column = synthetic_currency_column(measure_name)
        source_columns = ([private_column] if private_column in columns else [
            column for column in columns
            if is_currency_source_column(column) and not is_synthetic_currency_column(column)
        ])
        for column in source_columns:
            index = columns.index(column)
            values, unknown, missing = [], [], 0
            for row in table.get("rows") or ():
                value = row.get(column) if isinstance(row, dict) else row[index] if index < len(row) else None
                if value is None or not str(value).strip():
                    missing += 1
                    continue
                code = _literal_code(value)
                if code and code not in values:
                    values.append(code)
                elif code is None and str(value).strip() not in unknown:
                    unknown.append(str(value).strip())
            candidates.append((table_name, column, tuple(values), tuple(unknown), missing))
    if not candidates:
        return {"state": "absent", "table": None, "column": None, "values": []}
    populated = [item for item in candidates if item[2] or item[3] or item[4]]
    if len(populated) != 1:
        return {
            "state": "ambiguous",
            "table": None,
            "column": None,
            "values": sorted({value for _, _, values, _, _ in populated for value in values}),
            "unknown_values": sorted({value for _, _, _, unknown, _ in populated for value in unknown}),
            "missing_count": sum(missing for _, _, _, _, missing in populated),
        }
    table, column, values, unknown, missing = populated[0]
    return {
        "state": "observed" if not unknown and missing == 0 else "incomplete",
        "table": table,
        "column": column,
        "values": list(values),
        "unknown_values": list(unknown),
        "missing_count": missing,
    }


def _graph_edge_rows(graph: SchemaGraph) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (foreign_key.from_column.table, foreign_key.from_column.name,
         foreign_key.to_column.table, foreign_key.to_column.name)
        for foreign_key in graph.foreign_keys
    )


def _graph_numeric_rows(graph: SchemaGraph) -> tuple[tuple[str, str], ...]:
    return tuple(
        (column.ref.table, column.ref.name)
        for column in graph.columns if column.ref.type.numeric
    )


def _measure_currency_edges(measure: ColumnRef, edge_rows):
    """Keep public currency edges plus only this measure's private asserted-currency edge."""
    private_column = synthetic_currency_column(measure.name)
    return tuple(
        edge for edge in edge_rows
        if not (edge[0] == measure.table and is_synthetic_currency_column(edge[1])
                and edge[1] != private_column)
    )


def _available_currency_targets(
    graph: SchemaGraph, measure_columns: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    edge_rows = _graph_edge_rows(graph)
    numeric_rows = _graph_numeric_rows(graph)
    available = set()
    for table, column in measure_columns:
        measure = graph.column_map.get((table, column))
        if measure is None:
            continue
        available.update(currency_rate_bindings(
            table, _measure_currency_edges(measure.ref, edge_rows), numeric_rows,
        ))
    return tuple(sorted(available))


def _multiplication_terms(expression):
    if isinstance(expression, BinaryExpr) and expression.operator == "*":
        return _multiplication_terms(expression.left) + _multiplication_terms(expression.right)
    return [expression]


def _currency_expression(expression) -> tuple[ColumnRef, str] | None:
    if (
        not isinstance(expression, Aggregate)
        or expression.function != "SUM"
    ):
        return None
    columns = [term for term in _multiplication_terms(expression.operand)
               if isinstance(term, ColumnRef)]
    rates = [(column, currency_rate_target(column.name)) for column in columns
             if currency_rate_target(column.name) is not None]
    measures = [column for column in columns if is_currency_measure_column(column.name)]
    if len(rates) != 1 or len(measures) != 1:
        return None
    return measures[0], rates[0][1]


@dataclass(frozen=True)
class CurrencySpecification:
    name: str = "currency"

    def detect(self, question: str) -> CalculationIntent | None:
        detected = currency_intent(question)
        if detected is None:
            return None
        return CalculationIntent(
            specification=self.name,
            operation="filter" if detected.kind == CurrencyIntentKind.FILTER else "convert",
            phrase=detected.phrase,
            target=detected.target,
            explicit=detected.explicit,
            attributes={
                "intent_rule": detected.rule,
                "negated": "true" if detected.negated else "false",
                "_question": question,
            },
            operands={"measure": "monetary amount"},
        )

    def plans(
        self,
        intent: CalculationIntent,
        graph: SchemaGraph,
        operand_scores: OperandScores | None = None,
    ) -> tuple[CalculationPlan, ...]:
        if intent.operation != "convert" or not intent.target:
            return ()
        edge_rows, numeric_rows = _graph_edge_rows(graph), _graph_numeric_rows(graph)
        eligible_measures = set(_numeric_candidates(
            graph,
            intent.attributes.get("_question", intent.phrase),
            preferred=_MONEY_WORDS,
            learned=(operand_scores or {}).get("measure"),
        ))
        out = []
        for column in graph.columns:
            measure = column.ref
            if not measure.type.numeric or not is_currency_measure_column(measure.name):
                continue
            if eligible_measures and measure not in eligible_measures:
                continue
            measure_edges = _measure_currency_edges(measure, edge_rows)
            binding = currency_rate_bindings(
                measure.table, measure_edges, numeric_rows,
            ).get(intent.target)
            if binding is None or binding not in graph.column_map:
                continue
            source_names = {
                from_column
                for from_table, from_column, to_table, to_column in measure_edges
                if from_table == measure.table and to_table == binding[0]
                and is_currency_source_column(from_column)
                and is_currency_reference_key(to_column)
            }
            if len(source_names) != 1:
                continue
            source_key = (measure.table, next(iter(source_names)))
            if source_key not in graph.column_map:
                continue
            source_currency = graph.column_map[source_key].ref
            rate = graph.column_map[binding].ref
            out.append(CalculationPlan(
                self.name,
                Aggregate("SUM", BinaryExpr(measure, "*", rate)),
                f"total_{intent.target.lower()}",
                intent.target,
                "direct_rate_multiplication",
                (("measure", measure), ("source_currency", source_currency), ("rate", rate)),
                measure.table,
                2.0 + _role_score(operand_scores, "measure", measure),
                measure,
                rate,
            ))
        return tuple(sorted(
            out,
            key=lambda plan: (-plan.score, tuple(_column_label(c) for c in plan.required_columns)),
        ))

    def assess(self, intent: CalculationIntent, evidence: ComputationEvidence, tables,
               graph: SchemaGraph) -> dict[str, Any]:
        target = intent.target or ""
        base = {
            **intent.record(),
            "computation": evidence.record(),
            "status": "unmet",
            "realization": None,
        }
        if not evidence.verified:
            return {**base, "available_targets": [], "proposal": "",
                    "reason": "the selected planner supplied no typed calculation evidence"}

        negated = intent.attributes.get("negated") == "true"
        filter_satisfied = bool(evidence.branches) and all(any(
            is_currency_source_column(fact.column)
            and _literal_code(fact.value) == target
            and fact.operator in ({"!=", "<>"} if negated else {"="})
            for fact in branch.predicates
        ) for branch in evidence.branches)
        numeric_outputs = [
            output
            for branch in evidence.branches
            for output in branch.outputs
            if output.numeric
            and output.aggregate_functions
            and "COUNT" not in output.aggregate_functions
        ]
        has_value_output = bool(numeric_outputs)
        effective_filter = intent.operation == "filter" or not has_value_output
        if effective_filter:
            return {
                **base,
                "status": "satisfied" if filter_satisfied else "unmet",
                "realization": ("currency_exclusion" if negated else "currency_filter")
                               if filter_satisfied else None,
                "available_targets": [],
                "proposal": "",
                "reason": (
                    f"every query branch {'excludes' if negated else 'filters rows to'} {target}"
                    if filter_satisfied else
                    f"the selected query did not realize the requested {target} row predicate"
                ),
            }

        converted = [_currency_expression(output.expression) for output in numeric_outputs]
        converted_measures = {match[0] for match in converted if match is not None}
        # A composed output can also contain another numeric factor such as discount_percent.
        # Once the typed FX factor identifies its monetary measure, do not misclassify every
        # non-FX factor as a second measure.
        measure_columns = tuple(sorted(
            {(column.table, column.name) for column in converted_measures}
            or {
                (column.table, column.name)
                for output in numeric_outputs
                for column in output.columns
                if currency_rate_target(column.name) is None
            }
        ))
        source = _source_profile(tables, measure_columns)
        available = _available_currency_targets(graph, measure_columns)
        proposal_target = next((code for code in available if code != target), "")
        original_question = intent.attributes.get("_question", intent.phrase)
        proposal = substitute_currency_target(original_question, proposal_target) if proposal_target else ""
        common = {
            **base,
            "source_currency": source,
            "available_targets": list(available),
            "proposal": proposal or "",
            "bindings": [
                {"role": "measure", "table": table, "column": column}
                for table, column in measure_columns
            ],
        }
        every_branch_numeric = bool(evidence.branches) and all(any(
            output.numeric and output.aggregate_functions and "COUNT" not in output.aggregate_functions
            for output in branch.outputs
        ) for branch in evidence.branches)
        monetary = bool(measure_columns) and all(
            is_currency_measure_column(column) for _, column in measure_columns
        )
        exact_conversion = (
            every_branch_numeric
            and monetary
            and bool(converted)
            and all(match is not None and match[1] == target for match in converted)
            and any(
                all(branch_realizes_plan(branch, plan, graph) for branch in evidence.branches)
                for plan in self.plans(intent, graph)
            )
        )
        if exact_conversion:
            return {**common, "status": "satisfied", "realization": "converted",
                    "reason": f"every numeric branch applies a typed direct rate to {target}"}
        if monetary and every_branch_numeric and not any(converted) and source["state"] == "absent" and not intent.explicit:
            return {**common, "status": "satisfied", "realization": "unit_annotation",
                    "reason": f"no source-currency dimension exists; {target} is the stated measure unit"}
        if (monetary and every_branch_numeric and not any(converted)
                and source["state"] == "observed" and source["values"] == [target]):
            return {**common, "status": "satisfied", "realization": "identity",
                    "reason": f"all observed source values are already {target}"}
        if filter_satisfied:
            return {
                **common,
                "status": "ambiguous",
                "realization": "currency_filter",
                "reason": (
                    f"{intent.phrase!r} can mean convert the aggregate to {target} or filter {target} rows; "
                    "the selected query used the filter reading"
                ),
                "proposal": substitute_currency_filter(original_question, target) or "",
            }
        if not every_branch_numeric:
            reason = "not every set-operation branch produces a scalable numeric aggregate"
        elif measure_columns and not monetary:
            reason = "the selected measure is not monetary"
        elif source["state"] == "ambiguous":
            reason = "the measure tables do not expose one unambiguous source-currency dimension"
        elif source["state"] == "incomplete":
            reason = "the source-currency dimension contains missing or non-ISO values"
        elif source["state"] == "absent":
            reason = "an explicit conversion needs a source-currency dimension"
        else:
            reason = f"the selected computation does not convert {source['values']} to {target}"
        return {**common, "reason": reason}


def _numeric_candidates(
    graph: SchemaGraph,
    phrase: str,
    preferred: frozenset[str] = frozenset(),
    learned: Mapping[tuple[str, str], float] | None = None,
) -> tuple[ColumnRef, ...]:
    phrase_words = set(_words(phrase))
    scored = []
    for schema_column in graph.columns:
        column = schema_column.ref
        words = set(_words(column.name))
        if not column.type.numeric or words & _ID_WORDS:
            continue
        overlap = len(words & phrase_words)
        preferred_hit = bool(words & preferred)
        exact = bool(words) and words <= phrase_words
        score = 4 * exact + 2 * overlap + preferred_hit
        if score:
            semantic = float((learned or {}).get((column.table, column.name), 0.0))
            scored.append((score, semantic, column))
    if not scored:
        return ()
    best_lexical = max(score for score, _, _ in scored)
    return tuple(column for lexical, _semantic, column in sorted(
        scored, key=lambda item: (-item[0], -item[1], item[2].table, item[2].name)
    ) if lexical == best_lexical)


def _unit_for(column: ColumnRef) -> str:
    words = set(_words(column.name))
    if words & _MONEY_WORDS:
        return "currency"
    if words & _PERSON_WORDS:
        return "person"
    return re.sub(r"\W+", "_", column.name.lower()).strip("_") or "quantity"


@dataclass(frozen=True)
class RatioSpecification:
    name: str = "ratio"

    def detect(self, question: str) -> CalculationIntent | None:
        text = " ".join(question.split())
        match = re.search(
            r"\b(?P<numerator>[A-Za-z0-9 _-]+?)\s+per\s+"
            r"(?P<denominator>capita|person|people|residents?|inhabitants?)\b",
            text,
            re.I,
        )
        if match:
            numerator = re.sub(r"\b(?:what|is|the|show|list|total|sum|average)\b", " ", match.group("numerator"), flags=re.I)
            numerator_phrase = " ".join(numerator.split())
            return CalculationIntent(
                specification=self.name,
                operation="divide",
                phrase=match.group(0),
                target="per_capita",
                attributes={"ratio_kind": "per_capita"},
                operands={"numerator": numerator_phrase, "denominator": "population"},
            )
        match = re.search(r"\bratio\s+of\s+(?P<numerator>.+?)\s+to\s+(?P<denominator>.+?)(?:\s+(?:by|where|for)\b|$)", text, re.I)
        if not match:
            match = re.search(r"\b(?P<numerator>.+?)\s+divided\s+by\s+(?P<denominator>.+?)(?:\s+(?:where|for)\b|$)", text, re.I)
        if not match:
            return None
        return CalculationIntent(
            specification=self.name,
            operation="divide",
            phrase=match.group(0),
            target="ratio",
            attributes={"ratio_kind": "explicit"},
            operands={
                "numerator": match.group("numerator").strip(),
                "denominator": match.group("denominator").strip(),
            },
        )

    def plans(
        self,
        intent: CalculationIntent,
        graph: SchemaGraph,
        operand_scores: OperandScores | None = None,
    ) -> tuple[CalculationPlan, ...]:
        numerators = _numeric_candidates(
            graph,
            intent.operands.get("numerator", ""),
            _MONEY_WORDS,
            (operand_scores or {}).get("numerator"),
        )
        denominators = _numeric_candidates(
            graph,
            intent.operands.get("denominator", ""),
            _PERSON_WORDS,
            (operand_scores or {}).get("denominator"),
        )
        out = []
        for numerator in numerators:
            for denominator in denominators:
                if numerator == denominator:
                    continue
                output_unit = f"{_unit_for(numerator)}/{_unit_for(denominator)}"
                out.append(CalculationPlan(
                    self.name,
                    BinaryExpr(Aggregate("SUM", numerator), "/", Aggregate("SUM", denominator)),
                    intent.target or "ratio",
                    output_unit,
                    "ratio_of_sums",
                    (("numerator", numerator), ("denominator", denominator)),
                    numerator.table,
                    2.5
                    + _role_score(operand_scores, "numerator", numerator)
                    + _role_score(operand_scores, "denominator", denominator),
                ))
        return tuple(sorted(
            out,
            key=lambda plan: (-plan.score, tuple(_column_label(c) for c in plan.required_columns)),
        ))

    def assess(self, intent: CalculationIntent, evidence: ComputationEvidence, tables,
               graph: SchemaGraph) -> dict[str, Any]:
        base = {**intent.record(), "computation": evidence.record(), "status": "unmet", "realization": None}
        if not evidence.verified:
            return {**base, "reason": "the selected planner supplied no typed calculation evidence",
                    "available": []}
        plans = self.plans(intent, graph)
        if not plans:
            return {**base, "reason": "the numerator and denominator could not be bound unambiguously",
                    "available": []}
        matched = []
        for branch in evidence.branches:
            branch_matches = [
                plan for plan in plans
                if branch_realizes_plan(branch, plan, graph)
            ]
            if len(branch_matches) != 1:
                return {**base, "reason": "every query branch must compute the same typed ratio",
                        "available": [plan.record() for plan in plans]}
            matched.append(branch_matches[0])
        if not matched or len({plan.expression for plan in matched}) != 1:
            return {**base, "reason": "set-operation branches disagree on ratio operands",
                    "available": [plan.record() for plan in plans]}
        plan = matched[0]
        return {
            **base,
            "status": "satisfied",
            "realization": "ratio",
            "reason": "every query branch divides the bound aggregate numerator by the bound aggregate denominator",
            "output_unit": plan.output_unit,
            "bindings": plan.record()["bindings"],
            "rule": plan.rule,
            "available": [plan.record() for plan in plans],
        }


def _rate_scale(graph: SchemaGraph, column: ColumnRef) -> tuple[float, str] | None:
    words = set(_words(column.name))
    if words & {"percent", "percentage", "pct"}:
        return 100.0, "percent"
    if words & {"fraction", "decimal"}:
        return 1.0, "fraction"
    schema_column = graph.column_map.get((column.table, column.name))
    values = []
    for value in () if schema_column is None else schema_column.values:
        try:
            values.append(parse_decimal(value))
        except ValueError:
            continue
    if values and all(0 <= value <= 1 for value in values):
        return 1.0, "observed_fraction"
    return None


def _has_temporal_alignment(graph: SchemaGraph, measure: ColumnRef, rate: ColumnRef) -> bool:
    if measure.table == rate.table:
        return True
    temporal_words = {"date", "effective", "from", "to", "until", "valid", "year"}
    rate_temporal = {
        column.ref for column in graph.by_table.get(rate.table, ())
        if set(_words(column.ref.name)) & temporal_words
    }
    if not rate_temporal:
        return True
    return any(
        foreign_key.tables == frozenset((measure.table, rate.table))
        and any(
            left in rate_temporal or right in rate_temporal
            for left, right in foreign_key.column_pairs
        )
        for foreign_key in graph.foreign_keys
    )


@dataclass(frozen=True)
class RateApplicationSpecification:
    name: str = "rate_application"

    def detect(self, question: str) -> CalculationIntent | None:
        text = " ".join(question.split())
        low = text.lower()
        kind = "tax" if re.search(r"\b(?:tax|vat|levy)\b|\bfiscal\s+charge\b", low) else (
            "commission" if re.search(r"\bcommission\b|\b(?:merchant|processing)\s+fee\b", low) else
            "interest" if re.search(r"\binterest\b|\bfinancing\s+charge\b", low) else
            "discount" if re.search(r"\bdiscount\b", low) else None
        )
        if kind is None:
            return None
        amount_output = bool(re.search(r"\b(?:amount|charge|cost|due|payable)\b", low))
        aggregate_output = bool(re.search(r"\b(?:total|sum)\b", low))
        action = bool(re.search(r"\b(?:apply|calculate|compute)\b", low))
        names_rate = bool(re.search(
            rf"\b(?:{kind}|tax|vat|levy|commission|interest|discount|merchant fee|processing fee)\s+"
            r"(?:percent(?:age)?|pct|fraction|rate)\b",
            low,
        ))
        if names_rate and not amount_output:
            return None
        asks_amount = amount_output or aggregate_output or action
        if not asks_amount:
            return None
        unsupported = ""
        if re.search(r"\b(?:bracket|marginal|progressive|tiered)\b", low):
            unsupported = "piecewise rate schedules are not represented"
        if kind == "interest" and not (
            re.search(r"\b(?:annual|yearly|one[ -]year)\b", low)
            and re.search(r"\bsimple\b", low)
        ):
            unsupported = "interest requires an explicit annual one-year simple-interest policy"
        kind_pattern = re.escape(kind)
        subtract = bool(re.search(
            rf"\b(?:subtract(?:ing)?|deduct(?:ing|ed)?|reduce(?:d|s|ing)?)\b[^.;,]*\b{kind_pattern}\b"
            rf"|\b(?:net of|after)\b(?:\s+\w+){{0,3}}\s+\b{kind_pattern}\b"
            rf"|\b{kind_pattern}\b(?:\s+\w+){{0,3}}\s+\b(?:deducted|subtracted)\b",
            low,
        ))
        add_words = r"add(?:ing|ed)?|plus|including" + (r"|with" if kind != "discount" else "")
        add = bool(re.search(
            rf"\b(?:{add_words})\b(?:\s+\w+){{0,2}}\s+\b{kind_pattern}\b"
            rf"|\binclusive of\b(?:\s+\w+){{0,2}}\s+\b{kind_pattern}\b",
            low,
        ))
        asks_rate_amount = bool(re.search(
            rf"\b(?:total|sum|calculate|compute)\s+(?:the\s+)?{kind}\s+(?:amount|charge|cost|due)\b",
            low,
        ))
        if kind == "discount" and not asks_rate_amount:
            subtract = True
        if subtract and add:
            unsupported = "the requested add-or-subtract direction is ambiguous"
        if re.search(r"\b(?:gross|net)\b", low) and not (subtract or add):
            unsupported = "gross or net requests require an explicit add-or-subtract direction"
        operation = "subtract_rate" if subtract and not add else (
            "add_rate" if add and not subtract else "apply_rate"
        )
        return CalculationIntent(
            specification=self.name,
            operation=operation,
            phrase=kind,
            target=kind,
            attributes={"rate_kind": kind, "unsupported": unsupported},
            operands={"measure": "monetary amount", "rate": f"{kind} percentage"},
        )

    def plans(
        self,
        intent: CalculationIntent,
        graph: SchemaGraph,
        operand_scores: OperandScores | None = None,
    ) -> tuple[CalculationPlan, ...]:
        if intent.attributes.get("unsupported"):
            return ()
        kind = intent.attributes.get("rate_kind", intent.target or "")
        rate_synonyms = {
            "tax": {"tax", "vat", "levy"},
            "commission": {"commission", "merchant", "processing", "fee"},
            "interest": {"interest", "financing"},
            "discount": {"discount"},
        }.get(kind, {kind})
        rates = []
        for schema_column in graph.columns:
            column = schema_column.ref
            words = set(_words(column.name))
            if column.type.numeric and words & rate_synonyms and words & _RATE_WORDS:
                scale = _rate_scale(graph, column)
                if scale is not None:
                    rates.append((column, scale))
        measure_preference = {"principal"} if kind == "interest" else _MONEY_WORDS
        measures = [
            column.ref for column in graph.columns
            if column.ref.type.numeric
            and set(_words(column.ref.name)) & measure_preference
            and not set(_words(column.ref.name)) & (_RATE_WORDS | rate_synonyms)
            and not set(_words(column.ref.name)) & _ID_WORDS
        ]
        out = []
        for measure in measures:
            for rate, (divisor, scale_rule) in rates:
                if not _has_temporal_alignment(graph, measure, rate):
                    continue
                normalized_rate = (rate if divisor == 1.0 else
                                   BinaryExpr(rate, "/", Literal(divisor, SQLType.REAL)))
                if intent.operation == "subtract_rate":
                    factor = BinaryExpr(Literal(1, SQLType.INTEGER), "-", normalized_rate)
                    alias = "net_amount"
                    mode = "subtract"
                elif intent.operation == "add_rate":
                    factor = BinaryExpr(Literal(1, SQLType.INTEGER), "+", normalized_rate)
                    alias = "gross_amount"
                    mode = "add"
                else:
                    factor = normalized_rate
                    alias = f"{kind}_amount"
                    mode = "amount"
                out.append(CalculationPlan(
                    self.name,
                    Aggregate("SUM", BinaryExpr(measure, "*", factor)),
                    alias,
                    "currency",
                    f"{kind}_{mode}_{scale_rule}" if mode != "amount" else f"{kind}_{scale_rule}",
                    (("measure", measure), ("rate", rate)),
                    measure.table,
                    2.0
                    + _role_score(operand_scores, "measure", measure)
                    + _role_score(operand_scores, "rate", rate),
                    measure,
                    factor,
                ))
        return tuple(sorted(
            out,
            key=lambda plan: (-plan.score, tuple(_column_label(c) for c in plan.required_columns)),
        ))

    def assess(self, intent: CalculationIntent, evidence: ComputationEvidence, tables,
               graph: SchemaGraph) -> dict[str, Any]:
        base = {**intent.record(), "computation": evidence.record(), "status": "unmet", "realization": None}
        if not evidence.verified:
            return {**base, "reason": "the selected planner supplied no typed calculation evidence",
                    "available": []}
        if intent.attributes.get("unsupported"):
            return {**base, "reason": intent.attributes["unsupported"], "available": []}
        plans = self.plans(intent, graph)
        if not plans:
            return {
                **base,
                "reason": "no unambiguous monetary measure and dimensionless rate are connected by typed keys",
                "available": [],
            }
        matched = []
        for branch in evidence.branches:
            branch_matches = [
                plan for plan in plans
                if branch_realizes_plan(branch, plan, graph)
            ]
            if len(branch_matches) != 1:
                return {**base, "reason": "every query branch must apply the same bound rate expression",
                        "available": [plan.record() for plan in plans]}
            matched.append(branch_matches[0])
        if not matched or len({plan.expression for plan in matched}) != 1:
            return {**base, "reason": "set-operation branches disagree on rate bindings",
                    "available": [plan.record() for plan in plans]}
        plan = matched[0]
        realization = {
            "subtract_rate": "rate_subtraction",
            "add_rate": "rate_addition",
        }.get(intent.operation, "rate_application")
        return {
            **base,
            "status": "satisfied",
            "realization": realization,
            "reason": "every query branch applies the requested typed rate expression to the bound monetary measure",
            "output_unit": plan.output_unit,
            "bindings": plan.record()["bindings"],
            "rule": plan.rule,
            "available": [plan.record() for plan in plans],
        }


SPECIFICATIONS = (
    CurrencySpecification(),
    RatioSpecification(),
    RateApplicationSpecification(),
)
