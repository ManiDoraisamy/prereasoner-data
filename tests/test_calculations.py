"""Hermetic adversarial tests for the registered typed-calculation engine."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from engine.currency_intent import (
    CurrencyIntentKind, currency_conversion_target, currency_intent, currency_rate_target,
)
from engine.knowledge import KnowledgeReasoner
from engine.knowledge_tables import KnowledgeTableQuery
from engine.calculations import (
    ComputationEvidence,
    assess_calculations,
    calculation_clarify,
    describe_computation,
    detect_calculations,
    select_calculation_candidate,
)
from engine.sql_ast import (
    Aggregate, BinaryExpr, BooleanExpr, ColumnRef, Comparison, Join, Literal, SQLType,
    SelectItem, SelectQuery, SetQuery,
)
from engine.sql_schema import SchemaGraph
from engine.sql_rank import SemanticSignals
from engine.sql_search import SQLSearcher
from engine.trace import stream_final
from mcp_server.engine_client import shape_reason_response
from training.props.calculation_contrastive import build_rows, write_rows
from training.props.eval_intent import read_op_mirror, thresholds_from_score_rows
from training.props.promote import promote


P = 0
F = 0


def ok(condition, message):
    global P, F
    if condition:
        P += 1
        print(f"  PASS  {message}")
    else:
        F += 1
        print(f"  FAIL  {message}")


ORDERS = {
    "name": "orders",
    "columns": ["currency", "amount"],
    "rows": [["EUR", 310], ["GBP", 100], ["USD", 95]],
}
USD_RATES = {
    "name": "fx",
    "columns": ["currency_code", "rate_to_usd"],
    "rows": [["EUR", 1.5], ["GBP", 2.0], ["USD", 1.0]],
}
EDGE = {
    "from_table": "orders", "from_col": "currency",
    "to_table": "fx", "to_col": "currency_code",
}


def _assessment(question, tables=(ORDERS, USD_RATES), fks=(EDGE,)):
    graph = SchemaGraph.from_tables(tables, fks)
    candidate, assessments, _ = select_calculation_candidate(
        question, tables, graph, SQLSearcher(graph).search(question),
    )
    assessment = next(row for row in assessments if row["specification"] == "currency")
    return candidate, assessment


def _currency_assessment(question, tables, graph, computation):
    return next(
        row for row in assess_calculations(question, tables, graph, computation)
        if row["specification"] == "currency"
    )


def test_intent_is_not_a_bare_currency_phrase():
    count = currency_intent("how many orders in USD")
    output = currency_intent("total order amount in euros")
    explicit = currency_intent("convert USD to EUR")
    ok(count is not None and count.kind == CurrencyIntentKind.FILTER,
       "COUNT + in USD is a row filter")
    ok(output is not None and output.kind == CurrencyIntentKind.OUTPUT,
       "aggregate + in euros is an output-unit request")
    ok(explicit is not None and explicit.kind == CurrencyIntentKind.OUTPUT and explicit.explicit,
       "explicit convert-to phrase is an output conversion")
    ok(currency_conversion_target("how many orders in USD") is None,
       "filter intent cannot activate FX enrichment or conversion ranking")
    ok(currency_conversion_target("total order amount in JPY") == "JPY",
       "pinned ISO/CLDR codes are recognized beyond three hand-written aliases")
    ok(currency_intent("show orders in top category") is None,
       "ordinary lowercase words that collide with ISO codes do not become currency intent")
    ok(currency_rate_target("rate_to_abc") is None,
       "malformed rate columns cannot enter typed FX availability")


def test_filter_conversion_and_annotation_matrix():
    count, count_result = _assessment("how many orders in USD")
    ok(count_result["status"] == "satisfied"
       and count_result["realization"] == "currency_filter",
       "typed WHERE currency=USD satisfies the count filter")
    ok("WHERE" in count.sql and "'USD'" in count.sql,
       "filter evidence agrees with rendered SQL")

    excluded, excluded_result = _assessment("how many orders not in USD")
    ok(excluded_result["status"] == "satisfied"
       and excluded_result["realization"] == "currency_exclusion",
       "negated currency filters preserve their polarity")
    ok(excluded.sql.startswith("SELECT COUNT") and "!=" in excluded.sql and "'USD'" in excluded.sql,
       "post-ranking admissibility skips the invalid EXCEPT candidate for the typed exclusion")

    _, ambiguous = _assessment("average amount in GBP")
    ok(ambiguous["status"] == "ambiguous"
       and ambiguous["proposal"] == "average amount where currency is GBP",
       "a scalable aggregate resolved as a filter asks instead of silently choosing a reading")

    _, missing = _assessment("total order amount in euros")
    ok(missing["status"] == "unmet" and missing["available_targets"] == ["USD"],
       "mixed-currency raw SUM cannot masquerade as an EUR total")
    ok(missing["proposal"] == "total order amount in US dollars",
       "proposal names a target genuinely joinable from the measure table")

    converted, satisfied = _assessment("total order amount in US dollars")
    ok(satisfied["status"] == "satisfied" and satisfied["realization"] == "converted",
       "typed SUM(amount*rate_to_usd) satisfies conversion")
    ok("rate_to_usd" in converted.sql and " * " in converted.sql,
       "conversion evidence agrees with rendered arithmetic")

    products = {"name": "products", "columns": ["product", "revenue"],
                "rows": [["A", 100], ["B", 250]]}
    _, annotation = _assessment("total revenue in euros", (products,), ())
    ok(annotation["status"] == "satisfied" and annotation["realization"] == "unit_annotation",
       "a measure with no currency dimension accepts the user's stated unit")

    eur_orders = {**ORDERS, "rows": [["EUR", 10], ["EUR", 20]]}
    _, identity = _assessment("total order amount in euros", (eur_orders,), ())
    ok(identity["status"] == "satisfied" and identity["realization"] == "identity",
       "an all-EUR measure needs no rate multiplication")

    incomplete_orders = {**ORDERS, "rows": [["EUR", 10], ["", 20], ["unknown", 30]]}
    _, incomplete = _assessment("total order amount in euros", (incomplete_orders,), ())
    ok(incomplete["status"] == "unmet"
       and incomplete["source_currency"]["state"] == "incomplete",
       "blank or non-ISO source values cannot falsely certify an identity conversion")

    jpy_rates = {"name": "jpy_fx", "columns": ["currency_code", "rate_to_jpy"],
                 "rows": [["EUR", 160.0], ["GBP", 190.0], ["USD", 150.0]]}
    jpy_edge = {**EDGE, "to_table": "jpy_fx"}
    _, jpy = _assessment("total order amount in JPY", (ORDERS, jpy_rates), (jpy_edge,))
    ok(jpy["status"] == "satisfied" and jpy["realization"] == "converted",
       "typed direct-rate conversion works for any pinned ISO currency code")


def test_set_query_requires_every_numeric_branch_to_convert():
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    rate = ColumnRef("fx", "rate_to_usd", SQLType.REAL)
    join = Join("fx", ColumnRef("orders", "currency", SQLType.TEXT),
                ColumnRef("fx", "currency_code", SQLType.TEXT))
    converted = SelectQuery(
        (SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", rate))),),
        "orders", joins=(join,),
    )
    raw = SelectQuery((SelectItem(Aggregate("SUM", amount)),), "orders")
    computation = describe_computation(SetQuery(converted, "UNION", raw))
    result = _currency_assessment(
        "total order amount in US dollars", (ORDERS, USD_RATES),
        SchemaGraph.from_tables((ORDERS, USD_RATES), (EDGE,)), computation,
    )
    ok(len(computation.branches) == 2
       and sum("rate_to_usd" in repr(output.expression)
               for branch in computation.branches for output in branch.outputs) == 1,
       "set-operation evidence counts both operands")
    ok(result["status"] == "unmet",
       "one converted branch cannot bless an unconverted set operand")

    eur_rate = ColumnRef("fx", "rate_to_eur", SQLType.REAL)
    wrong_target = SelectQuery(
        (SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", eur_rate))),),
        "orders", joins=(join,),
    )
    dual_rates = {**USD_RATES, "columns": ["currency_code", "rate_to_usd", "rate_to_eur"],
                  "rows": [["EUR", 1.5, 1.0], ["GBP", 2.0, 1.2], ["USD", 1.0, 0.8]]}
    mixed_targets = describe_computation(SetQuery(converted, "UNION", wrong_target))
    mixed_result = _currency_assessment(
        "total order amount in US dollars", (ORDERS, dual_rates),
        SchemaGraph.from_tables((ORDERS, dual_rates), (EDGE,)), mixed_targets,
    )
    rate_columns = {
        column.name
        for branch in mixed_targets.branches for output in branch.outputs for column in output.columns
        if column.name.startswith("rate_to_")
    }
    ok(rate_columns == {"rate_to_eur", "rate_to_usd"},
       "set-operation evidence retains every converted target")
    ok(mixed_result["status"] == "unmet",
       "fully converted branches cannot certify a request when one uses the wrong target")

    projection = SelectQuery((SelectItem(amount),), "orders")
    mixed_shape = describe_computation(SetQuery(converted, "UNION", projection))
    mixed_shape_result = _currency_assessment(
        "total order amount in US dollars", (ORDERS, USD_RATES),
        SchemaGraph.from_tables((ORDERS, USD_RATES), (EDGE,)), mixed_shape,
    )
    numeric_aggregate_branches = sum(any(output.aggregate_functions for output in branch.outputs)
                                     for branch in mixed_shape.branches)
    ok(numeric_aggregate_branches == 1 and len(mixed_shape.branches) == 2
       and mixed_shape_result["status"] == "unmet",
       "a converted aggregate cannot bless a projection-only set operand")


def test_filter_evidence_is_guaranteed_on_every_path():
    currency = ColumnRef("orders", "currency", SQLType.TEXT)
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    usd = Comparison(currency, "=", Literal("USD", SQLType.TEXT))
    positive = Comparison(amount, ">", Literal(0, SQLType.INTEGER))
    filtered = SelectQuery((SelectItem(amount),), "orders", where=usd)
    unfiltered = SelectQuery((SelectItem(amount),), "orders")
    graph = SchemaGraph.from_tables((ORDERS,), ())

    set_result = _currency_assessment(
        "show orders in USD", (ORDERS,), graph,
        describe_computation(SetQuery(filtered, "UNION", unfiltered)),
    )
    ok(set_result["status"] == "unmet",
       "a currency filter on only one set operand does not certify the whole query")

    disjoined = SelectQuery(
        (SelectItem(amount),), "orders", where=BooleanExpr("OR", (usd, positive)),
    )
    or_result = _currency_assessment(
        "show orders in USD", (ORDERS,), graph,
        describe_computation(disjoined),
    )
    ok(or_result["status"] == "unmet",
       "a currency comparison on only one OR arm is not treated as a guaranteed filter")


def test_unjoinable_rate_is_not_advertised():
    archive = {"name": "legacy_archive", "columns": ["code", "rate_to_eur"],
               "rows": [["EUR", 1.0], ["USD", 0.9]]}
    _, result = _assessment("total order amount in British pounds",
                            (ORDERS, USD_RATES, archive), (EDGE,))
    ok(result["available_targets"] == ["USD"],
       "availability uses the same typed edge as planner binding")
    ok("euros" not in result["proposal"].lower(),
       "an unjoinable rate column cannot produce a dead-end proposal")

    malformed = {"name": "bad_fx", "columns": ["currency_code", "rate_to_abc"],
                 "rows": [["EUR", 1.0], ["USD", 1.1]]}
    malformed_edge = {**EDGE, "to_table": "bad_fx"}
    _, malformed_result = _assessment(
        "total order amount in British pounds",
        (ORDERS, USD_RATES, malformed), (EDGE, malformed_edge),
    )
    ok(malformed_result["available_targets"] == ["USD"],
       "an invalid rate_to suffix is ignored rather than crashing availability")


def test_only_monetary_measures_can_convert():
    inventory = {"name": "inventory", "columns": ["currency", "quantity"],
                 "rows": [["EUR", 10], ["USD", 20]]}
    edge = {**EDGE, "from_table": "inventory"}
    graph = SchemaGraph.from_tables((inventory, USD_RATES), (edge,))
    candidates = SQLSearcher(graph).search("total quantity in USD")
    ok(candidates and all("rate_to_usd" not in candidate.sql for candidate in candidates),
       "FX generation cannot multiply a physical quantity by an exchange rate")
    assessment = _currency_assessment(
        "total quantity in USD", (inventory, USD_RATES), graph,
        describe_computation(candidates[0].query),
    )
    ok(assessment["status"] in {"ambiguous", "unmet"},
       "a non-monetary aggregate cannot be certified as converted or currency-denominated")


def test_missing_typed_evidence_fails_closed():
    eur_orders = {**ORDERS, "rows": [["EUR", 10], ["EUR", 20]]}
    reasoner = object.__new__(KnowledgeReasoner)
    response = reasoner._verify_calculations(
        {"question": "total order amount in euros", "sql": "SELECT 30",
         "result": {"columns": ["total"], "rows": [[30]]}},
        (eur_orders,), "total order amount in euros", (),
    )
    ok(response["clarify"] is True and response["result"] is None
       and response["currency"]["computation"]["verified"] is False,
       "a route without typed computation evidence cannot release a numeric answer")


def test_decline_contract_reaches_stream_and_mcp():
    _, assessment = _assessment("total order amount in euros")
    response = calculation_clarify(
        "total order amount in euros",
        {"sql": 'SELECT SUM("orders"."amount") FROM "orders"'},
        (assessment,),
    )
    ok(response["clarify"] and response["result"] is None and response["reason"],
       "wrong number is removed from the clarify envelope")

    emitted = {}
    stream_final(lambda key, value: emitted.__setitem__(key, value), response)
    ok(emitted["clarify"]["reason"] == response["reason"]
       and emitted["clarify"]["unmet"],
       "RTDB terminal event preserves reason and unmet evidence")

    shaped = shape_reason_response(response, "job-currency")
    ok(shaped["status"] == "clarify"
       and shaped["clarify"]["reason"] == response["reason"],
       "MCP output preserves the same reason")


def _run_calculation(question, tables, fks):
    graph = SchemaGraph.from_tables(tables, fks)
    candidates = SQLSearcher(graph).search(question)
    candidate, assessments, index = select_calculation_candidate(
        question, tables, graph, candidates,
    )
    return graph, candidate, assessments, index


def test_ratio_uses_composite_keys_and_derives_units():
    economy = {"name": "economy", "columns": ["country", "year", "gdp"],
               "rows": [["FR", 2024, 3000], ["DE", 2024, 4000]]}
    population = {"name": "population", "columns": ["country", "year", "population"],
                  "rows": [["FR", 2024, 60], ["DE", 2024, 80]]}
    edge = {"from_table": "economy", "from_cols": ["country", "year"],
            "to_table": "population", "to_cols": ["country", "year"]}
    _, candidate, assessments, index = _run_calculation(
        "GDP per capita", (economy, population), (edge,),
    )
    ratio = next(row for row in assessments if row["specification"] == "ratio")
    ok(index == 0 and ratio["status"] == "satisfied" and ratio["output_unit"] == "currency/person",
       "ratio verification derives a named output unit from bound operands")
    ok("SUM(\"economy\".\"gdp\")" in candidate.sql
       and "SUM(\"population\".\"population\")" in candidate.sql
       and " AND " in candidate.sql,
       "per-capita search emits ratio-of-sums over the complete composite join")
    paraphrase = detect_calculations("national output per inhabitant")
    ok(paraphrase and paraphrase[0].specification == "ratio"
       and paraphrase[0].target == "per_capita",
       "per-capita intent recognizes heldout person-denominator paraphrases")
    ok(not detect_calculations("number of concerts for each person"),
       "ordinary for-each grouping is not a division request")


def test_learned_operand_signal_orders_only_typed_eligible_plans():
    economy = {
        "name": "economy",
        "columns": ["gdp", "revenue", "population", "country_id"],
        "rows": [[3000, 2000, 60, 1]],
    }
    graph = SchemaGraph.from_tables((economy,), ())
    signals = SemanticSignals(
        {},
        {},
        calculation_operands={
            "ratio:numerator": {
                ("economy", "gdp"): 0.95,
                ("economy", "revenue"): 0.10,
                ("economy", "country_id"): 1.0,
            },
            "ratio:denominator": {("economy", "population"): 0.95},
        },
    )
    candidates = SQLSearcher(graph).search("economic output per capita", semantic_signals=signals)
    candidate, assessments, _ = select_calculation_candidate(
        "economic output per capita", (economy,), graph, candidates,
    )
    ok(assessments[0]["status"] == "satisfied" and '"economy"."gdp"' in candidate.sql,
       "learned operand similarity orders typed ratio bindings")
    ok('"economy"."country_id"' not in candidate.sql,
       "learned similarity cannot make an identifier an eligible operand")
    adversarial = SemanticSignals(
        {},
        {},
        calculation_operands={
            "ratio:numerator": {
                ("economy", "gdp"): -1.0,
                ("economy", "revenue"): 1.0,
            },
            "ratio:denominator": {("economy", "population"): 1.0},
        },
    )
    explicit = SQLSearcher(graph).search("GDP per capita", semantic_signals=adversarial)[0]
    ok('"economy"."gdp"' in explicit.sql and '"economy"."revenue"' not in explicit.sql,
       "learned similarity cannot override an exact lexical operand binding")


def test_rate_application_supports_percent_and_fraction_units():
    payments = {"name": "payments", "columns": ["instrument", "amount"],
                "rows": [["card", 100], ["bank", 200]]}
    commissions = {"name": "commission_rates", "columns": ["instrument", "commission_fraction"],
                   "rows": [["card", 0.03], ["bank", 0.01]]}
    edge = {"from_table": "payments", "from_col": "instrument",
            "to_table": "commission_rates", "to_col": "instrument"}
    _, commission_query, commission_rows, _ = _run_calculation(
        "total commission amount", (payments, commissions), (edge,),
    )
    commission = commission_rows[0]
    ok(commission["status"] == "satisfied" and commission["rule"] == "commission_fraction"
       and "/ 100" not in commission_query.sql,
       "fractional commission rates multiply directly")

    transactions = {"name": "transactions", "columns": ["country", "amount"],
                    "rows": [["FR", 100], ["DE", 200]]}
    taxes = {"name": "tax_rates", "columns": ["country", "tax_percent"],
             "rows": [["FR", 20], ["DE", 19]]}
    tax_edge = {"from_table": "transactions", "from_col": "country",
                "to_table": "tax_rates", "to_col": "country"}
    _, tax_query, tax_rows, _ = _run_calculation(
        "total tax amount", (transactions, taxes), (tax_edge,),
    )
    tax = tax_rows[0]
    ok(tax["status"] == "satisfied" and tax["rule"] == "tax_percent"
       and "/ 100.0" in tax_query.sql,
       "percent tax rates are normalized before monetary multiplication")

    loans = {"name": "loans", "columns": ["loan_id", "principal_amount", "interest_percent"],
             "rows": [[1, 1000, 5], [2, 2000, 4]]}
    _, interest_query, interest_rows, _ = _run_calculation(
        "total annual simple interest amount", (loans,), (),
    )
    interest = interest_rows[0]
    ok(interest["status"] == "satisfied" and interest["rule"] == "interest_percent"
       and "principal_amount" in interest_query.sql,
       "an explicit annual one-year simple-interest policy uses the same typed rate primitive")

    fees = {
        "name": "merchant_fees",
        "columns": ["payment_amount", "merchant_fee_percent"],
        "rows": [[100, 2.5], [200, 1.5]],
    }
    _, fee_query, fee_rows, _ = _run_calculation(
        "compute merchant fee amount", (fees,), (),
    )
    ok(fee_rows[0]["status"] == "satisfied" and "merchant_fee_percent" in fee_query.sql,
       "commission intent and bindings recognize explicit merchant-fee terminology")

    financing = {
        "name": "financing",
        "columns": ["principal_amount", "financing_percent"],
        "rows": [[1000, 5]],
    }
    _, financing_query, financing_rows, _ = _run_calculation(
        "compute yearly simple financing charge", (financing,), (),
    )
    ok(financing_rows[0]["status"] == "satisfied" and "financing_percent" in financing_query.sql,
       "explicit yearly simple-financing terminology maps to the interest rule")


def test_temporal_rate_requires_and_accepts_composite_alignment():
    sales = {"name": "sales", "columns": ["country", "effective_date", "amount"],
             "rows": [["FR", "2024-01-01", 100], ["FR", "2025-01-01", 200]]}
    rates = {"name": "tax_rates", "columns": ["country", "effective_date", "tax_percent"],
             "rows": [["FR", "2024-01-01", 20], ["FR", "2025-01-01", 21]]}
    edge = {"from_table": "sales", "from_cols": ["country", "effective_date"],
            "to_table": "tax_rates", "to_cols": ["country", "effective_date"]}
    _, candidate, assessments, _ = _run_calculation(
        "total tax amount", (sales, rates), (edge,),
    )
    ok(assessments[0]["status"] == "satisfied"
       and '"sales"."effective_date" = "tax_rates"."effective_date"' in candidate.sql,
       "dated rates are admissible only when the complete temporal key is present in the join")


def test_verifier_rejects_arithmetic_over_an_incomplete_join():
    sales = {"name": "sales", "columns": ["country", "effective_date", "amount"],
             "rows": [["FR", "2024-01-01", 100]]}
    rates = {"name": "tax_rates", "columns": ["country", "effective_date", "tax_percent"],
             "rows": [["FR", "2024-01-01", 20]]}
    edge = {"from_table": "sales", "from_cols": ["country", "effective_date"],
            "to_table": "tax_rates", "to_cols": ["country", "effective_date"]}
    graph = SchemaGraph.from_tables((sales, rates), (edge,))
    def column(table, name):
        return graph.column_map[(table, name)].ref
    expression = Aggregate("SUM", BinaryExpr(
        column("sales", "amount"),
        "*",
        BinaryExpr(column("tax_rates", "tax_percent"), "/", Literal(100.0, SQLType.REAL)),
    ))
    incomplete = SelectQuery(
        (SelectItem(expression, alias="tax_amount"),),
        "sales",
        joins=(Join(
            "tax_rates",
            column("sales", "country"),
            column("tax_rates", "country"),
        ),),
    )
    assessment = assess_calculations(
        "total tax amount", (sales, rates), graph, describe_computation(incomplete),
    )[0]
    ok(assessment["status"] == "unmet",
       "matching arithmetic cannot certify a rate joined on only part of its composite key")
    adapter = KnowledgeTableQuery.__new__(KnowledgeTableQuery)
    from_sql, descriptions, joined, selected = adapter._uploaded_from(
        ["sales", "tax_rates"], (edge,),
    )
    ok(" AND " in from_sql and "effective_date" in descriptions[0]
       and joined == ["sales", "tax_rates"] and selected == [edge],
       "the world-route adapter preserves the selected composite FK for shared evidence")


def test_complex_and_temporally_unbound_rates_abstain():
    sales = {"name": "sales", "columns": ["country", "amount"], "rows": [["FR", 100]]}
    tiers = {"name": "tax_rates", "columns": ["country", "tax_percent"], "rows": [["FR", 20]]}
    edge = {"from_table": "sales", "from_col": "country", "to_table": "tax_rates", "to_col": "country"}
    graph = SchemaGraph.from_tables((sales, tiers), (edge,))
    evidence = describe_computation(SQLSearcher(graph).search("total tiered tax amount")[0].query)
    tiered = assess_calculations("total tiered tax amount", (sales, tiers), graph, evidence)[0]
    ok(tiered["status"] == "unmet" and "piecewise" in tiered["reason"],
       "tiered statutory schedules abstain instead of applying one flat rate")

    dated = {"name": "dated_tax", "columns": ["country", "effective_date", "tax_percent"],
             "rows": [["FR", "2024-01-01", 20]]}
    dated_graph = SchemaGraph.from_tables((sales, dated), (
        {**edge, "to_table": "dated_tax"},
    ))
    candidates = SQLSearcher(dated_graph).search("total tax amount")
    _, assessments, _ = select_calculation_candidate(
        "total tax amount", (sales, dated), dated_graph, candidates,
    )
    tax = assessments[0]
    ok(tax["status"] == "unmet" and not tax["available"],
       "a dated rate table without a typed temporal key cannot be applied")

    ok(not detect_calculations("show the total tax rate by country"),
       "projecting or aggregating a rate does not request rate application")
    gross_intent = detect_calculations("calculate the total amount including tax")
    ok(bool(gross_intent) and bool(gross_intent[0].attributes.get("unsupported")),
       "gross and net totals abstain instead of returning only the rate component")


def test_unverified_non_currency_calculation_fails_closed():
    economy = {"name": "economy", "columns": ["gdp", "population"], "rows": [[3000, 60]]}
    graph = SchemaGraph.from_tables((economy,), ())
    assessment = assess_calculations(
        "GDP per capita", (economy,), graph, ComputationEvidence.unverified(),
    )[0]
    ok(assessment["status"] == "unmet" and assessment["computation"]["verified"] is False,
       "the shared fail-closed contract applies to non-currency calculations")


def test_typed_calculation_clarify_supersedes_generic_coverage_clarify():
    generic = {
        "question": "total order amount in euros",
        "clarify": True,
        "original_sql": 'SELECT SUM("orders"."amount") FROM "orders"',
        "dropped": ["euros"],
        "model": "engine - clarify (the query dropped part of the question)",
    }
    reasoner = KnowledgeReasoner.__new__(KnowledgeReasoner)
    result = reasoner._verify_calculations(
        generic,
        (ORDERS, USD_RATES),
        "total order amount in euros",
        (EDGE,),
    )
    ok(result["model"] == "engine - clarify (typed calculation semantics not satisfied)"
       and result["result"] is None and result["calculations"][0]["status"] == "unmet",
       "typed calculation evidence supersedes a generic dropped-phrase clarification")
    ok(result["original_sql"] == generic["original_sql"]
       and result["prior_clarification"]["dropped"] == ["euros"],
       "calculation clarification preserves prior SQL and coverage evidence")


def test_calculation_training_corpus_is_split_safe_and_rebuildable():
    train, evaluation = build_rows()
    ok(bool(train) and bool(evaluation)
       and not ({row["query"] for row in train} & {row["query"] for row in evaluation}),
       "structured calculation supervision has a query-disjoint heldout split")
    ok({row["kind"] for row in train} == {"intent", "operand"}
       and {row["label"] for row in train if row["kind"] == "intent"}
       == {"currency", "ratio", "rate_application"},
       "training covers named operation families and operand bindings")
    with TemporaryDirectory() as directory:
        train_path, eval_path = write_rows(Path(directory))
        ok(train_path.exists() and eval_path.exists()
           and train_path.read_text(encoding="utf-8").count("\n") == len(train),
           "the calculation corpus is regenerated by deterministic code")


def test_intent_thresholds_are_checkpoint_calibrated():
    rows = [
        ("COUNT", None, {"COUNT": 0.20, "SUM": 0.01, "AVG": 0.00}),
        (None, None, {"COUNT": 0.10, "SUM": 0.01, "AVG": 0.00}),
        ("SUM", None, {"COUNT": 0.01, "SUM": 0.80, "AVG": 0.00}),
        (None, None, {"COUNT": 0.01, "SUM": 0.30, "AVG": 0.00}),
        ("AVG", None, {"COUNT": 0.01, "SUM": 0.02, "AVG": 0.70}),
        (None, None, {"COUNT": 0.01, "SUM": 0.02, "AVG": 0.20}),
    ]
    thresholds = thresholds_from_score_rows(rows)
    ok(read_op_mirror(rows[0][2], thresholds) == "COUNT"
       and read_op_mirror(rows[1][2], thresholds) is None
       and read_op_mirror(rows[2][2], thresholds) == "SUM"
       and read_op_mirror(rows[5][2], thresholds) is None,
       "operator gates are learned from independent score distributions, not fixed constants")


def test_model_promotion_is_atomic_and_marks_unpublished_candidates():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source, destination = root / "source", root / "engine"
        (source / "qwen_lora_props").mkdir(parents=True)
        destination.mkdir()
        payloads = {
            "encoder_props.pt": b"encoder",
            "encoder_props_meta.pt": b"meta",
            "qwen_lora_props/adapter_config.json": b"{}",
            "qwen_lora_props/adapter_model.safetensors": b"adapter",
        }
        for relative, payload in payloads.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (source / "props_thr.json").write_text('{"name": 0.5}', encoding="utf-8")
        files = {
            "encoder.pt": "",
            "encoder_meta.pt": "",
            "qwen_lora/adapter_config.json": "",
            "qwen_lora/adapter_model.safetensors": "",
        }
        (destination / "weights_manifest.json").write_text(
            json.dumps({"version": 1, "files": files}), encoding="utf-8"
        )
        manifest = promote(source, destination, revision=None, local_only=True)
        ok(manifest["unpublished_local"] is True and manifest["revision"] is None,
           "local model promotion cannot masquerade as a published revision")
        ok(all(manifest["files"].values()) and (destination / "props_thr.json").is_file(),
           "promotion installs and hashes the complete runtime checkpoint")


TESTS = [
    test_intent_is_not_a_bare_currency_phrase,
    test_filter_conversion_and_annotation_matrix,
    test_set_query_requires_every_numeric_branch_to_convert,
    test_filter_evidence_is_guaranteed_on_every_path,
    test_unjoinable_rate_is_not_advertised,
    test_only_monetary_measures_can_convert,
    test_missing_typed_evidence_fails_closed,
    test_decline_contract_reaches_stream_and_mcp,
    test_ratio_uses_composite_keys_and_derives_units,
    test_learned_operand_signal_orders_only_typed_eligible_plans,
    test_rate_application_supports_percent_and_fraction_units,
    test_temporal_rate_requires_and_accepts_composite_alignment,
    test_verifier_rejects_arithmetic_over_an_incomplete_join,
    test_complex_and_temporally_unbound_rates_abstain,
    test_unverified_non_currency_calculation_fails_closed,
    test_typed_calculation_clarify_supersedes_generic_coverage_clarify,
    test_calculation_training_corpus_is_split_safe_and_rebuildable,
    test_intent_thresholds_are_checkpoint_calibrated,
    test_model_promotion_is_atomic_and_marks_unpublished_candidates,
]


def main():
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # keep the repository's aggregated P/F test convention
            ok(False, f"{test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\ntest_calculations: {P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
