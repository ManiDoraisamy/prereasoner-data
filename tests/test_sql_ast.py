"""Hermetic execution tests for deterministic SQL AST search and ranking.

Run: python -m tests.test_sql_ast
"""
from __future__ import annotations

from collections import Counter
import inspect
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from unittest.mock import patch

import numpy as np

from engine.sql_ast import (
    ASTValidationError,
    Aggregate,
    BinaryExpr,
    ColumnRef,
    Comparison,
    ExistsPredicate,
    InPredicate,
    Join,
    Literal,
    OrderTerm,
    SQLType,
    ScalarSubquery,
    SelectItem,
    SelectQuery,
    SetQuery,
    Star,
    SubquerySource,
    render_query,
    validate_query,
)
from engine import llm
from engine.artifact_provenance import sha256_file, validate_weight_bundle
from engine.sql_grounding import double_counted, grounded_members, join_pairs, literal_bindings
from engine.sql_rank import FallbackRecord, SemanticSignals, analyze_question
from regress.sql_import import import_sql, normalize_decoded_sql
from engine.sql_search import SQLSearcher, SchemaGraph, ScoredQuery
from engine.sql_profile_expansion import ProfileQueryExpander, ProfileSearchConfig
from spider.probe.evalutil import run_with_budget
from engine.tables import TableQuery
from spider.probe.spider_eval import (
    compare as compare_spider_rows,
    record_integrated_result,
    recursive_gold_table_names,
    spider_foreign_keys,
)
from spider.probe.evalutil import _score as score_spider_candidates
from spider.probe.ast_profile import (
    CandidateAssessment,
    SQLProfile,
    diagnose_pool,
    profile_query,
    profile_spider_sql,
)


PEOPLE = {
    "name": "people",
    "columns": ["Person_ID", "Name", "Country", "Age"],
    "rows": [[1, "Alice", "France", 30], [2, "Bob", "France", 20], [3, "Cara", "Spain", 40]],
}
CUSTOMERS = {
    "name": "customers",
    "columns": ["Customer_ID", "Name"],
    "rows": [[1, "Alice"], [2, "Bob"]],
}
ORDERS = {
    "name": "orders",
    "columns": ["Order_ID", "Customer_ID"],
    "rows": [[10, 1], [11, 2]],
}
ITEMS = {
    "name": "items",
    "columns": ["Item_ID", "Order_ID", "Price"],
    "rows": [[100, 10, 8], [101, 10, 12], [102, 11, 30]],
}
COMMERCE_FKS = [
    {"from_table": "orders", "from_col": "Customer_ID", "to_table": "customers", "to_col": "Customer_ID"},
    {"from_table": "items", "from_col": "Order_ID", "to_table": "orders", "to_col": "Order_ID"},
]
# Retail fact-table fixture for transactional measure phrases ("units sold",
# "revenue", "total spend"). Rows are chosen so the summed ranking, the raw
# row ranking, and each candidate measure column all produce DIFFERENT answers:
# quantity sums Beta 9 > Alpha 8 > Gamma 6, revenue sums Beta 180 > Gamma 90 >
# Alpha 80, raw quantity rows rank Beta then Gamma, and shopper spend ranks
# Bob 180 > Alice 170 while shopper quantity ranks Alice first.
SHOPPERS = {
    "name": "shoppers",
    "columns": ["shopper_id", "name"],
    "rows": [[1, "Alice"], [2, "Bob"]],
}
PURCHASES = {
    "name": "purchases",
    "columns": ["purchase_id", "shopper_id"],
    "rows": [[1001, 1], [1002, 2]],
}
RETAIL_PRODUCTS = {
    "name": "products",
    "columns": ["product_id", "product_name"],
    "rows": [[101, "Alpha"], [102, "Beta"], [103, "Gamma"]],
}
PURCHASE_ITEMS = {
    "name": "purchase_items",
    "columns": ["purchase_item_id", "purchase_id", "product_id", "quantity", "unit_price", "line_total"],
    "rows": [
        [1, 1001, 101, 5, 10, 50],
        [2, 1001, 101, 3, 10, 30],
        [3, 1002, 102, 9, 20, 180],
        [4, 1001, 103, 6, 15, 90],
    ],
}
RETAIL = [SHOPPERS, PURCHASES, RETAIL_PRODUCTS, PURCHASE_ITEMS]
RETAIL_FKS = [
    {"from_table": "purchases", "from_col": "shopper_id", "to_table": "shoppers", "to_col": "shopper_id"},
    {"from_table": "purchase_items", "from_col": "purchase_id", "to_table": "purchases", "to_col": "purchase_id"},
    {"from_table": "purchase_items", "from_col": "product_id", "to_table": "products", "to_col": "product_id"},
]
STADIUM = {
    "name": "stadium",
    "columns": ["Stadium_ID", "Name", "Capacity"],
    "rows": [[1, "Alpha", 6000], [2, "Beta", 9000], [3, "Gamma", 12000]],
}
CONCERT = {
    "name": "concert",
    "columns": ["Concert_ID", "Stadium_ID"],
    "rows": [[1, 1], [2, 1], [3, 2]],
}
STADIUM_FKS = [
    {"from_table": "concert", "from_col": "Stadium_ID", "to_table": "stadium", "to_col": "Stadium_ID"},
]
PETS = {
    "name": "Pets",
    "columns": ["PetID", "PetType", "pet_age"],
    "rows": [[1, "dog", 3], [2, "cat", 5], [3, "dog", 7]],
}
AIRPORTS = {
    "name": "airports",
    "columns": ["AirportCode", "City"],
    "rows": [["ABZ", "Aberdeen"], ["ASY", "Ashley"], ["LAX", "Los Angeles"]],
}
FLIGHTS = {
    "name": "flights",
    "columns": ["Flight_ID", "SourceAirport", "DestAirport"],
    "rows": [[1, "ABZ", "ASY"], [2, "ASY", "ABZ"], [3, "ABZ", "LAX"]],
}
FLIGHT_FKS = [
    {"from_table": "flights", "from_col": "SourceAirport", "to_table": "airports", "to_col": "AirportCode"},
    {"from_table": "flights", "from_col": "DestAirport", "to_table": "airports", "to_col": "AirportCode"},
]


def _qident(value):
    return '"' + str(value).replace('"', '""') + '"'


def execute(tables, sql):
    con = sqlite3.connect(":memory:")
    for table in tables:
        columns = table["columns"]
        con.execute(f"CREATE TABLE {_qident(table['name'])} (" + ", ".join(_qident(c) for c in columns) + ")")
        placeholders = ",".join("?" for _ in columns)
        con.executemany(f"INSERT INTO {_qident(table['name'])} VALUES ({placeholders})", table["rows"])
    rows = con.execute(sql).fetchall()
    con.close()
    return rows


def best(question, tables, fks=()):
    candidates = SQLSearcher.from_tables(tables, fks).search(question)
    assert candidates, f"no candidates for {question!r}"
    return candidates[0]


def test_typed_ast_rejects_invalid_aggregate():
    name = ColumnRef("people", "Name", SQLType.TEXT)
    query = SelectQuery((SelectItem(Aggregate("SUM", name)),), "people")
    try:
        validate_query(query)
    except ASTValidationError:
        return
    raise AssertionError("SUM(text) passed AST validation")


def test_grouped_ast_rejects_ungrouped_ordering():
    country = ColumnRef("people", "Country", SQLType.TEXT)
    age = ColumnRef("people", "Age", SQLType.INTEGER)
    query = SelectQuery(
        (SelectItem(country), SelectItem(Aggregate("AVG", age))),
        "people",
        group_by=(country,),
        order_by=(OrderTerm(age, "DESC"),),
    )
    try:
        validate_query(query)
    except ASTValidationError:
        return
    raise AssertionError("aggregate query ordered by an ungrouped column")


def test_typed_ast_rejects_mismatched_literal_payloads():
    code = ColumnRef("items", "code", SQLType.INTEGER)
    query = SelectQuery(
        (SelectItem(code),),
        "items",
        where=Comparison(code, "=", Literal("0 OR 1=1", SQLType.INTEGER)),
    )
    try:
        render_query(query)
    except ASTValidationError:
        pass
    else:
        raise AssertionError("numeric-typed string literal reached SQL rendering")

    graph = SchemaGraph.from_planner([{
        "table": "items",
        "name": "code",
        "affinity": "INTEGER",
        "values": ["1,000", "0 OR 1=1"],
    }], [])
    assert graph.columns[0].values == (1000, None)


def test_ast_rejects_indeterminate_set_and_aggregate_shapes():
    star_query = SelectQuery((SelectItem(Star()),), "left_table")
    compound = SetQuery(star_query, "UNION", SelectQuery((SelectItem(Star()),), "right_table"))
    invalid = (
        compound,
        SelectQuery((SelectItem(Aggregate("COUNT", Star(), distinct=True)),), "items"),
    )
    for query in invalid:
        try:
            validate_query(query)
        except ASTValidationError:
            continue
        raise AssertionError(f"invalid AST shape passed validation: {query!r}")


def test_grouping_validation_sees_ordered_aggregates():
    item_id = ColumnRef("items", "id", SQLType.INTEGER)
    name = ColumnRef("items", "name", SQLType.TEXT)
    query = SelectQuery(
        (SelectItem(item_id), SelectItem(name)),
        "items",
        group_by=(item_id,),
        order_by=(OrderTerm(Aggregate("COUNT", Star()), "DESC"),),
    )
    try:
        validate_query(query)
    except ASTValidationError:
        return
    raise AssertionError("ORDER BY aggregate did not activate grouped projection validation")


def test_composite_foreign_key_renders_and_executes_as_one_join():
    shipments = {
        "name": "shipments",
        "columns": ["country", "postal_code", "parcel"],
        "rows": [["US", "10001", "A"], ["CA", "10001", "B"]],
    }
    postal = {
        "name": "postal",
        "columns": ["country_code", "postal_code", "place_name"],
        "rows": [["US", "10001", "New York"], ["CA", "10001", "Toronto"]],
    }
    graph = SchemaGraph.from_tables([shipments, postal], [{
        "from_table": "shipments", "from_cols": ("country", "postal_code"),
        "to_table": "postal", "to_cols": ("country_code", "postal_code"),
        "confidence": 1.0,
    }])
    assert len(graph.foreign_keys) == 1 and graph.foreign_keys[0].is_composite
    tree = graph.join_trees({"shipments", "postal"}, preferred_root="shipments")[0]
    assert len(tree.joins[0].predicates) == 2
    place = graph.column_map[("postal", "place_name")].ref
    query = SelectQuery((SelectItem(place),), "shipments", joins=tree.joins)
    sql = render_query(query)
    assert " AND " in sql
    assert execute([shipments, postal], sql) == [("New York",), ("Toronto",)]


def test_composite_join_validation_rejects_disconnected_predicate():
    query = SelectQuery(
        (SelectItem(ColumnRef("postal", "place", SQLType.TEXT)),),
        "shipments",
        joins=(Join(
            "postal",
            ColumnRef("shipments", "postal", SQLType.TEXT),
            ColumnRef("postal", "postal", SQLType.TEXT),
            additional=((
                ColumnRef("unseen", "country", SQLType.TEXT),
                ColumnRef("postal", "country", SQLType.TEXT),
            ),),
        ),),
    )
    try:
        validate_query(query)
    except ASTValidationError:
        return
    raise AssertionError("disconnected composite predicate passed AST validation")


def test_composite_self_join_helpers_render_complete_predicates():
    # The is_composite guard in _self_join_candidates keeps composite FKs out of the self-join
    # helpers today, so this is unreachable via search. It pins the OWNER invariant (cleanup #1
    # from the 349a4ae review): if a composite FK ever reaches these helpers they must emit a
    # COMPLETE join (ON a=b AND c=d), never a silent partial join.
    from engine.sql_schema import ForeignKey
    from engine.sql_recursive import _route_self_join, _relationship_self_join

    def _fk(child_code, child_region):
        return ForeignKey(
            ColumnRef("flights", child_code, SQLType.TEXT),
            ColumnRef("airports", "code", SQLType.TEXT),
            additional_columns=((ColumnRef("flights", child_region, SQLType.TEXT),
                                 ColumnRef("airports", "region", SQLType.TEXT)),),
        )

    source_fk, destination_fk = _fk("source_code", "source_region"), _fk("dest_code", "dest_region")
    assert source_fk.is_composite and destination_fk.is_composite
    name = ColumnRef("airports", "name", SQLType.TEXT)

    route = render_query(_route_self_join(
        "flights", "airports", source_fk, destination_fk,
        (0, name, "Aberdeen"), (1, name, "Ashley"), True, None))
    assert '"base"."source_region" = "source"."region"' in route, route
    assert '"base"."dest_region" = "destination"."region"' in route, route

    relation = render_query(_relationship_self_join(
        "flights", "airports", source_fk, destination_fk, (0, name, "Aberdeen"), name))
    assert '"relation"."source_region" = "owner"."region"' in relation, relation
    assert '"relation"."dest_region" = "related"."region"' in relation, relation


def test_arithmetic_expression_sum_renders_and_executes():
    # M3a: SUM(amount * rate) — the prerequisite for currency conversion. The AST must render valid
    # SQL and execute to the per-row-weighted sum, NOT the currency-blind raw SUM(amount).
    orders = {"name": "orders", "columns": ["ccy", "amount", "rate"],
              "rows": [["EUR", 100, 1.5], ["EUR", 200, 1.5], ["GBP", 50, 2.0]]}  # 150 + 300 + 100 = 550
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    rate = ColumnRef("orders", "rate", SQLType.REAL)
    query = SelectQuery((SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", rate)), alias="usd"),), "orders")
    validate_query(query)
    sql = render_query(query)
    assert 'SUM(("orders"."amount" * "orders"."rate"))' in sql, sql
    assert execute([orders], sql) == [(550.0,)]


def test_arithmetic_division_preserves_real_semantics():
    values = {"name": "values", "columns": ["numerator", "denominator"], "rows": [[5, 2], [5, 0]]}
    numerator = ColumnRef("values", "numerator", SQLType.INTEGER)
    denominator = ColumnRef("values", "denominator", SQLType.INTEGER)
    query = SelectQuery((SelectItem(BinaryExpr(numerator, "/", denominator)),), "values")
    sql = render_query(query)
    assert "CAST(" in sql and " AS REAL) / NULLIF(" in sql, sql
    assert execute([values], sql) == [(2.5,), (None,)]


def test_currency_conversion_query_executes_end_to_end():
    # M3: the COMPLETE conversion the engine must eventually build — join orders to an FX-rate table
    # and SUM(amount * rate). Proves the typed AST already expresses conversion end to end (join +
    # arithmetic + aggregate); the planner *producing* this is M3c. Rates are exact in float on
    # purpose so the assertion is precise.
    orders = {"name": "orders", "columns": ["currency", "amount"],
              "rows": [["EUR", 310], ["EUR", 210], ["GBP", 100]]}
    fx = {"name": "fx", "columns": ["currency_code", "rate_to_usd"],
          "rows": [["EUR", 1.5], ["GBP", 2.0], ["USD", 1.0]]}          # 310*1.5 + 210*1.5 + 100*2.0 = 980
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    rate = ColumnRef("fx", "rate_to_usd", SQLType.REAL)
    query = SelectQuery(
        (SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", rate)), alias="usd_total"),),
        "orders",
        joins=(Join("fx", ColumnRef("orders", "currency", SQLType.TEXT),
                    ColumnRef("fx", "currency_code", SQLType.TEXT)),),
    )
    validate_query(query)
    assert execute([orders, fx], render_query(query)) == [(980.0,)]


def test_planner_emits_currency_conversion_when_requested():
    # M3c: "... in US dollars" + a joinable FX-rate table -> the planner OFFERS SUM(amount * rate)
    # over the discovered orders.currency -> fx.currency_code join. Proves the planner produces the
    # conversion the M3a AST made representable, and that it does NOT fire without a currency cue.
    orders = {"name": "orders", "columns": ["currency", "amount"],
              "rows": [["EUR", 310], ["EUR", 210], ["GBP", 100], ["EUR", 95]]}
    fx = {"name": "fx", "columns": ["currency_code", "rate_to_usd"],
          "rows": [["EUR", 1.5], ["GBP", 2.0], ["USD", 1.0]]}
    fks = [{"from_table": "orders", "from_col": "currency", "to_table": "fx", "to_col": "currency_code"}]
    top = best("total order amount in US dollars", [orders, fx], fks)          # ranker must PREFER conversion
    assert top.sql.startswith('SELECT SUM(("orders"."amount" * "fx"."rate_to_usd"))'), top.sql
    assert 'JOIN "fx"' in top.sql, top.sql
    # 310*1.5 + 210*1.5 + 100*2.0 + 95*1.5 = 465 + 315 + 200 + 142.5 = 1122.5
    assert execute([orders, fx], top.sql) == [(1122.5,)], top.sql
    # no currency/convert cue -> raw sum wins; the arithmetic conversion must NOT appear (no false positive)
    plain = best("total order amount", [orders, fx], fks)
    assert "rate_to_usd" not in plain.sql and "*" not in plain.sql, plain.sql


def test_planner_discovers_and_executes_same_name_currency_edge():
    orders = {"name": "orders", "columns": ["currency", "amount"],
              "rows": [["EUR", 520], ["EUR", 450], ["GBP", 100]]}
    rates = {"name": "illustrative fx rates", "columns": ["currency", "rate_to_usd"],
             "rows": [["USD", 1.0], ["EUR", 1.08], ["GBP", 1.27], ["INR", 0.012]]}
    tables, fks = TableQuery().ingest([orders, rates])
    assert any(
        fk["from_table"] == "orders" and fk["from_col"] == "currency"
        and fk["to_table"] == "illustrative_fx_rates" and fk["to_col"] == "currency"
        for fk in fks
    ), fks

    top = SQLSearcher.from_tables(tables, fks).search("total order amount in US dollars")[0]
    assert 'SUM(("orders"."amount" * "illustrative_fx_rates"."rate_to_usd"))' in top.sql, top.sql
    rows = execute(tables, top.sql)
    assert len(rows) == 1 and abs(rows[0][0] - 1174.6) < 1e-9, (top.sql, rows)


def test_currency_conversion_survives_uploaded_column_case():
    # REGRESSION: engine.tables normalizes TABLE names but preserves COLUMN case, while the rate column is
    # synthesized lowercase ("rate_to_usd"). An exact-case membership test therefore bound nothing for an FX
    # sheet headed "Rate_To_USD", and the planner silently answered with the UNCONVERTED SUM — a wrong
    # number, no clarify. Same rows as the lowercase test above, headers cased the way a spreadsheet export
    # actually arrives.
    orders = {"name": "orders", "columns": ["Currency", "Amount"],
              "rows": [["EUR", 520], ["EUR", 450], ["GBP", 100]]}
    rates = {"name": "illustrative fx rates", "columns": ["Currency", "Rate_To_USD"],
             "rows": [["USD", 1.0], ["EUR", 1.08], ["GBP", 1.27], ["INR", 0.012]]}
    tables, fks = TableQuery().ingest([orders, rates])
    top = SQLSearcher.from_tables(tables, fks).search("total order amount in US dollars")[0]
    # the identifier must be rendered as the schema spells it, or the SQL will not execute
    assert 'SUM(("orders"."Amount" * "illustrative_fx_rates"."Rate_To_USD"))' in top.sql, top.sql
    rows = execute(tables, top.sql)
    assert len(rows) == 1 and abs(rows[0][0] - 1174.6) < 1e-9, (top.sql, rows)


def test_world_aggregate_currency_binding_is_typed_and_unambiguous():
    from engine.knowledge_tables import KnowledgeTableQuery

    schema = [
        {"table": "orders", "name": "amount", "affinity": "REAL"},
        {"table": "rates", "name": "rate_to_usd", "affinity": "REAL"},
    ]
    edge = {"from_table": "orders", "from_col": "currency",
            "to_table": "rates", "to_col": "currency"}
    aggregate = ("SUM", "orders", "amount")
    assert KnowledgeTableQuery._currency_conversion_binding(
        "total amount in US dollars", aggregate, schema, [edge]
    ) == ("rates", "rate_to_usd")
    assert KnowledgeTableQuery._currency_conversion_binding(
        "total amount", aggregate, schema, [edge]
    ) is None

    duplicate_schema = schema + [
        {"table": "other_rates", "name": "rate_to_usd", "affinity": "REAL"},
    ]
    duplicate_edge = {**edge, "to_table": "other_rates"}
    assert KnowledgeTableQuery._currency_conversion_binding(
        "total amount in US dollars", aggregate, duplicate_schema, [edge, duplicate_edge]
    ) is None


def test_planner_requires_exact_currency_target_and_key_shape():
    orders = {"name": "orders", "columns": ["currency", "amount"],
              "rows": [["USD", 100], ["EUR", 100]]}
    fx = {"name": "fx", "columns": ["currency_code", "rate_to_usd", "rate_to_eur"],
          "rows": [["USD", 1.0, 0.9], ["EUR", 1.1, 1.0]]}
    edge = [{"from_table": "orders", "from_col": "currency",
             "to_table": "fx", "to_col": "currency_code"}]
    eur = best("convert total order amount to EUR", [orders, fx], edge)
    assert "rate_to_eur" in eur.sql and "rate_to_usd" not in eur.sql, eur.sql

    only_usd = {"name": "fx", "columns": ["currency_code", "rate_to_usd"],
                "rows": [["USD", 1.0], ["EUR", 1.1]]}
    unsupported = best("total order amount in EUR", [orders, only_usd], edge)
    assert "rate_to_usd" not in unsupported.sql and "*" not in unsupported.sql, unsupported.sql
    ambiguous = best("total order amount in dollars", [orders, only_usd], edge)
    assert "rate_to_usd" not in ambiguous.sql and "*" not in ambiguous.sql, ambiguous.sql
    untargeted = best("convert total order amount", [orders, only_usd], edge)
    assert "rate_to_usd" not in untargeted.sql and "*" not in untargeted.sql, untargeted.sql

    tax = {"name": "tax_rates", "columns": ["tax_code", "tax_rate"],
           "rows": [["USD", 0.2], ["EUR", 0.1]]}
    tax_edge = [{"from_table": "orders", "from_col": "currency",
                 "to_table": "tax_rates", "to_col": "tax_code"}]
    not_fx = best("total order amount in US dollars", [orders, tax], tax_edge)
    assert "tax_rate" not in not_fx.sql and "*" not in not_fx.sql, not_fx.sql

    other_fx = {"name": "other_fx", "columns": ["currency", "rate_to_usd"],
                "rows": [["USD", 1.0], ["EUR", 1.2]]}
    other_edge = [{"from_table": "orders", "from_col": "currency",
                   "to_table": "other_fx", "to_col": "currency"}]
    ambiguous_rates = best(
        "total order amount in US dollars", [orders, only_usd, other_fx], edge + other_edge
    )
    assert "rate_to_usd" not in ambiguous_rates.sql and "*" not in ambiguous_rates.sql, ambiguous_rates.sql


def test_arithmetic_expression_validation():
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    name = ColumnRef("orders", "name", SQLType.TEXT)
    # a valid bare arithmetic projection passes
    validate_query(SelectQuery((SelectItem(BinaryExpr(amount, "+", Literal(1, SQLType.INTEGER))),), "orders"))
    for bad in (
        BinaryExpr(amount, "%", amount),                       # unsupported operator
        BinaryExpr(amount, "*", Star()),                       # arithmetic on * is invalid
        BinaryExpr(amount, "+", Literal("not a number")),      # UNKNOWN text literal is invalid
    ):
        try:
            validate_query(SelectQuery((SelectItem(bad),), "orders")); raise AssertionError(bad)
        except ASTValidationError:
            pass
    # a non-numeric operand inside an aggregate is rejected
    try:
        validate_query(SelectQuery((SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", name))),), "orders"))
        raise AssertionError("non-numeric arithmetic operand accepted")
    except ASTValidationError:
        pass
    # aggregate arithmetic is row-scoped; aggregate-over-aggregate stays outside SUM/AVG.
    for nested in (
        Aggregate("SUM", Aggregate("SUM", amount)),
        Aggregate("SUM", BinaryExpr(Aggregate("SUM", amount), "/", amount)),
    ):
        try:
            validate_query(SelectQuery((SelectItem(nested),), "orders"))
            raise AssertionError("nested aggregate accepted")
        except ASTValidationError:
            pass


def test_projection_filter_and_order():
    candidate = best(
        "Show name, country and age for people from France ordered by age descending",
        [PEOPLE],
    )
    assert execute([PEOPLE], candidate.sql) == [("Alice", "France", 30), ("Bob", "France", 20)]
    assert candidate.sql.endswith('ORDER BY "people"."Age" DESC')


def test_scalar_aggregate_does_not_group_by_recipient_mention():
    payments = {"name": "payments", "columns": ["Supplier", "Amount"],
                "rows": [["A", 10], ["A", 20], ["B", 40]]}
    for question in ("What is the total amount paid to suppliers?",
                     "What is the average amount paid to suppliers?"):
        candidate = best(question, [payments])
        assert not candidate.query.group_by, candidate.sql
        want = 70 if "total" in question else 70 / 3
        assert execute([payments], candidate.sql) == [(want,)], candidate.sql
    for question in ("Show suppliers and total amount", "Total amount by supplier",
                     "Total amount for each supplier"):
        candidate = best(question, [payments])
        assert candidate.query.group_by, (question, candidate.sql)
        assert execute([payments], candidate.sql) == [("A", 30), ("B", 40)]


def test_mentioned_table_join_keeps_minimal_variant_in_pool():
    """A table the question merely NAMES must not be force-joined into every candidate: the
    parsimonious variant touching only tables whose columns the query uses has to exist in the
    pool alongside the mention-widened join (Spider over-join family, 215/669 strict misses)."""
    dogs = {"name": "dogs", "columns": ["dog_id", "name"],
            "rows": [[1, "Rex"], [2, "Ace"], [3, "Ivy"]]}
    treatments = {"name": "treatments", "columns": ["treatment_id", "dog_id", "cost"],
                  "rows": [[1, 1, 50], [2, 1, 30], [3, 2, 20]]}
    fks = [{"from_table": "treatments", "from_col": "dog_id",
            "to_table": "dogs", "to_col": "dog_id"}]
    candidates = SQLSearcher.from_tables([dogs, treatments], fks).search(
        "What is the total cost of treatments for dogs?")
    assert candidates
    minimal = [c for c in candidates if "tables:minimal" in c.evidence]
    assert minimal, "parsimonious variant missing from the pool"
    assert all('"dogs"' not in c.sql for c in minimal), [c.sql for c in minimal]
    assert any("SUM" in c.sql.upper() for c in minimal), [c.sql for c in minimal]
    top_minimal = next(c for c in minimal if "SUM" in c.sql.upper())
    assert execute([dogs, treatments], top_minimal.sql) == [(100,)], top_minimal.sql
    joined = [c for c in candidates if '"dogs"' in c.sql and "JOIN" in c.sql.upper()]
    assert joined, "mention-widened join reading must remain pooled"


def test_duplicate_named_projection_keeps_single_binding_variant_in_pool():
    """When one canonical column name is projected from several tables, the expander must emit
    each single-binding reduction, pruning tables the reduction stops using (Spider over-join
    family: the distractor is bound into the projection, so pruning tables alone cannot reach
    the gold reading)."""
    from engine.sql_ast import ColumnRef, Join, SelectItem, SelectQuery, render_query
    from engine.sql_parsimony import ParsimonyQueryExpander

    templates = {"name": "templates", "columns": ["template_id", "type_code"],
                 "rows": [[1, "PPT"], [2, "CV"]]}
    ref_types = {"name": "ref_types", "columns": ["type_code", "type_name"],
                 "rows": [["PPT", "Presentation"], ["CV", "Resume"]]}
    fks = [{"from_table": "templates", "from_col": "type_code",
            "to_table": "ref_types", "to_col": "type_code"}]
    graph = SchemaGraph.from_tables([templates, ref_types], fks)
    t_id = ColumnRef("templates", "template_id")
    t_code = ColumnRef("templates", "type_code")
    r_code = ColumnRef("ref_types", "type_code")
    double = SelectQuery(
        select=(SelectItem(t_id), SelectItem(t_code), SelectItem(r_code)),
        from_table="templates",
        joins=(Join("ref_types", t_code, r_code),),
    )
    base = ScoredQuery(double, render_query(double), 10.0, ())
    variants = ParsimonyQueryExpander(graph).expand("show template type codes", [base])
    assert variants, "no parsimony variants emitted"
    assert all(v.score < base.score for v in variants)
    kept_templates = [v for v in variants
                     if "projection:binding:templates.type_code" in v.evidence]
    kept_ref = [v for v in variants
                if "projection:binding:ref_types.type_code" in v.evidence]
    assert kept_templates and kept_ref, [v.sql for v in variants]
    minimal = kept_templates[0]
    assert "tables:minimal" in minimal.evidence
    assert minimal.sql == 'SELECT "templates"."template_id", "templates"."type_code" FROM "templates"'
    assert execute([templates, ref_types], minimal.sql) == [(1, "PPT"), (2, "CV")]
    assert 'JOIN "ref_types"' in kept_ref[0].sql, kept_ref[0].sql
    dropped = [v for v in variants
               if "projection:drop:templates.template_id" in v.evidence]
    assert any(
        v.sql == 'SELECT "templates"."type_code", "ref_types"."type_code" FROM "templates" '
                 'JOIN "ref_types" ON "templates"."type_code" = "ref_types"."type_code"'
        for v in dropped
    ), [v.sql for v in dropped]
    assert any(any(tag.startswith("projection:drop2:") for tag in v.evidence)
               for v in variants), "drop-two reductions missing"
    assert any("projection:distinct-toggle" in v.evidence for v in variants)

    from engine.sql_ast import Aggregate
    from engine.sql_parsimony import _projection_variants

    cost = ColumnRef("templates", "template_id")
    weight = ColumnRef("templates", "type_code")
    total = SelectQuery(select=(SelectItem(Aggregate("SUM", cost)),), from_table="templates")
    stub_linked = lambda table: (weight,)  # noqa: E731 - question-linking stubbed out
    tags = [evidence for _, evidence in _projection_variants(total, stub_linked)]
    assert any("aggregate:operand:templates.type_code" in evidence for evidence in tags), tags
    plain = SelectQuery(select=(SelectItem(cost),), from_table="templates")
    tags = [evidence for _, evidence in _projection_variants(plain, stub_linked)]
    assert any("projection:add:templates.type_code" in evidence for evidence in tags), tags


def _hermetic_planner(fallback=None):
    """The production TableQuery without its encoder: ``schema`` types columns from their values and
    the search reads no encoder signals. ``fallback`` is selection's labelled Gemini fallback
    (engine/question_rewrite.py); a planner without one serves the search alone."""
    class HermeticPlanner(TableQuery):
        def schema(self, tables, fks):
            columns, index = [], 0
            for table in tables:
                for name in table["columns"]:
                    values = [row[table["columns"].index(name)] for row in table["rows"]]
                    numeric = values and all(isinstance(value, (int, float)) for value in values)
                    columns.append({
                        "table": table["name"], "name": name, "idx": index, "struct": set(),
                        "affinity": "INTEGER" if numeric else "TEXT", "ace": [],
                        "is_date": bool(values) and all(
                            re.match(r"\d{4}-\d{2}-\d{2}", str(value)) for value in values),
                        "qvec": np.zeros(2, dtype=np.float32), "values": values,
                    })
                    index += 1
            return columns, {}, {table["name"]: table for table in tables}

        def ast_semantic_signals(self, question, sch):
            return SemanticSignals.empty()

    planner = HermeticPlanner()
    planner.question_rewriter = fallback
    return planner


def _request(planner, tables=None):
    """(norm, fks, sch, tablemap) for one request's tables, prepared the way serve() prepares them."""
    norm, fks = planner.ingest(tables or [PEOPLE])
    sch, _, tablemap = planner.schema(norm, fks)
    return norm, fks, sch, tablemap


def _select(planner, question, tables=None, searched=None):
    """The production own-data selection. ``searched`` is the pool select_query ranks, runs and
    grounds; None runs the deterministic search for it, as serving does."""
    return planner.select_query(question, *_request(planner, tables), searched=searched)


def _searched(planner, question, tables=None):
    """The deterministic search's own pool for ``question`` (select_query's first stage)."""
    norm, fks, sch, _ = _request(planner, tables)
    return planner.search_pool(question, norm, fks, sch)


def _model_query(planner, sql, tables=None):
    """A query a SQL model wrote, imported into the typed AST against the request's schema and
    re-rendered (regress/sql_import.py): a pool member select_query can rank, run and ground."""
    norm, fks, sch, _ = _request(planner, tables)
    query = import_sql(sql, SchemaGraph.from_planner(sch, fks))
    validate_query(query)
    return ScoredQuery(query, render_query(query), 0.0, ("model:query",))


class FakeGemini:
    """engine/llm.py's surface as selection's labelled fallback uses it (engine/question_rewrite.py),
    with scripted replies and every call recorded. It never touches the network.

    ``question`` is the rewording Gemini returns; None is a reply without the field. ``enabled`` is
    ``available()`` and ``outage`` makes every call raise LLMUnavailable."""

    LLMUnavailable = llm.LLMUnavailable

    def __init__(self, question=None, *, enabled=True, outage=False):
        self.replies = {"question": question}
        self.enabled = enabled
        self.outage = outage
        self.calls = []
        self.requests = []

    def available(self):
        return self.enabled

    def model_id(self):
        return "gemini-test"

    def generate_text(self, **request):
        inspect.signature(llm.generate_text).bind(**request)   # the call engine/llm.py accepts
        field = next(iter(request["json_schema"]["properties"]))
        self.calls.append((field, request["prompt"]))
        self.requests.append(request)
        if self.outage:
            raise self.LLMUnavailable("Gemini call failed (TimeoutError)")
        reply = self.replies[field]
        return json.dumps({field: reply} if reply is not None else {}, ensure_ascii=False)


def _gemini_planner(question=None, **state):
    """A hermetic planner whose selection falls back to a FakeGemini; returns (planner, client)."""
    from engine.question_rewrite import QuestionRewriter

    client = FakeGemini(question, **state)
    return _hermetic_planner(QuestionRewriter(client=client)), client


# The search reads words; a question in a script it has no words for gives it an empty pool.
JAPANESE_FRANCE = "フランス出身の人は何人ですか？"   # "How many people are from France?"
RUSSIAN_FRANCE = "Сколько людей из Франции?"          # the same, in Russian
FRANCE_COUNT = "SELECT COUNT(*) FROM \"people\" WHERE \"people\".\"Country\" = 'France'"


def test_select_query_serves_the_reading_that_keeps_the_named_date():
    """Cloud product suite, 2026-10-02: with an undated reading ranked first, "How many contracts
    were signed before July 10, 2026?" was served without its date and declined. The served
    selection keeps to the readings that realize the date when one does."""
    contracts = {"name": "contracts", "columns": ["contract", "value", "signed"], "rows": [
        ["NDA", 5000, "2026-07-06"], ["License", 900000, "2026-07-05"], ["Lease", 30000, "2026-07-12"]]}
    planner = _hermetic_planner()
    question = "How many contracts were signed before July 10, 2026?"
    undated = _model_query(planner, "SELECT COUNT(*) FROM contracts", [contracts])
    selection = _select(planner, question, [contracts],
                        searched=[undated, *_searched(planner, question, [contracts])])
    assert 0 not in selection.ranking, "a dropped date is ineligible even when scored first"
    served = selection.candidate
    assert "'2026-07-10'" in served.sql and selection.date_satisfied[selection.selected], served.sql
    assert selection.record()["date_satisfied"] is True
    # Contrast: without a date in the question the ranking's choice stands.
    plain_question = "How many contracts are there?"
    plain = _select(planner, plain_question, [contracts],
                    searched=[undated, *_searched(planner, plain_question, [contracts])])
    assert plain.candidate.sql == undated.sql and not any(plain.date_satisfied)


def test_select_query_never_chooses_a_query_that_does_not_run():
    """A pooled query that does not finish within the SQLite step budget (EXECUTION_OP_LIMIT) is
    never chosen, and a question whose pool holds none that runs is not answered."""
    planner = _hermetic_planner()
    with patch("engine.sql_rank.EXECUTION_OP_LIMIT", 1):
        selection = _select(planner, "list person names")
        response = planner.serve([PEOPLE], "list person names")
    assert selection.pool and not any(selection.executable)
    assert selection.ranking == () and selection.selected is None and selection.candidate is None
    assert selection.record()["executable"] == 0 and selection.served_by == "search"
    assert response["valid"] is False and response["fallback"] is None
    assert response["error"] == "planner: no executable AST candidate"


def test_a_join_that_multiplies_two_tables_rows_stops_at_their_cells_budget():
    """A pooled query may take EXECUTION_OPS_PER_CELL SQLite steps for each cell of the tables it reads
    (engine.sql_rank.execution_op_limit). Two subscription exports joined on their currency, a value
    most of their rows share, ran to the flat 100M-step budget: about 2.4 s each, 110 s of one question
    in production (2026-10-05). Such a join now stops at the budget its tables set and is not eligible,
    while every row of the largest tab the add-on reads, listed in order, stays eligible."""
    from engine.sql_rank import (EXECUTION_OP_FLOOR, EXECUTION_OP_LIMIT, EXECUTION_OPS_PER_CELL,
                                 execution_op_limit)

    planner = _hermetic_planner()
    exports = [{"name": name, "columns": ["id", "Currency", "Amount"],
                "rows": [[f"{name}_{i}", "usd", 10] for i in range(3000)]} for name in ("nt", "si")]
    joined = _model_query(planner, 'SELECT COUNT(*) FROM "nt" JOIN "si" ON "nt"."Currency" = "si"."Currency"',
                          exports)
    counted = _model_query(planner, 'SELECT COUNT(*) FROM "nt"', exports)
    tablemap = _request(planner, exports)[3]
    assert execution_op_limit(joined.query, tablemap) == EXECUTION_OP_FLOOR        # 18,000 cells
    selection = _select(planner, "how many nt rows are there", exports, searched=[joined, counted])
    assert selection.executable == (False, True), selection.executable
    assert selection.candidate.sql == counted.sql

    # Every row of the largest tab the add-on reads (50,000), listed in order, the heaviest normal shape
    # measured (2.3 steps a cell on a 22-column export), takes under a quarter of its budget.
    columns = ["id", "Plan", "Status", "Amount", "Currency", "Interval", "Quantity", "Customer", "Email", "Domain",
               "Product", "Coupon"]
    tablemap = {"subscriptions": {"columns": columns, "rows": [
        [f"sub_{i:06}", f"plan_{i % 40}", "active", i % 100, "usd", "month", 1, f"cus_{i % 9000}",
         f"user{i}@example.invalid", "example.invalid", "Startup", ""] for i in range(50000)]}}
    listed = SelectQuery(select=tuple(SelectItem(ColumnRef("subscriptions", name, SQLType.TEXT)) for name in columns),
                         from_table="subscriptions",
                         order_by=(OrderTerm(ColumnRef("subscriptions", "id", SQLType.TEXT)),))
    assert execution_op_limit(listed, tablemap) == EXECUTION_OPS_PER_CELL * 600000   # above the floor
    connection = TableQuery._sqlite_tables(None, tablemap, [
        {"table": "subscriptions", "name": name, "affinity": "INTEGER" if name in ("Amount", "Quantity") else "TEXT"}
        for name in columns])
    steps = 0

    def count():
        nonlocal steps
        steps += 1000
    connection.set_progress_handler(count, 1000)
    assert len(connection.execute(render_query(listed)).fetchall()) == 50000
    connection.close()
    assert 4 * steps < execution_op_limit(listed, tablemap), steps

    wide = {"wide": {"columns": list(range(50)), "rows": range(200000)}}           # 10M cells
    assert execution_op_limit(listed, wide) == EXECUTION_OP_LIMIT                    # a table it does not hold
    wide_listed = SelectQuery(select=(SelectItem(ColumnRef("wide", "0", SQLType.TEXT)),), from_table="wide")
    assert execution_op_limit(wide_listed, wide) == EXECUTION_OP_LIMIT               # the ceiling


def test_the_rewording_pool_shares_the_tables_copy_and_reruns_no_query():
    """select_query copies the request's tables into SQLite once and runs each distinct query once:
    the rewording's pool (step 4) copied every table again and reran every query the first pool had
    run, a second full copy of a large workbook on every question Gemini rewords (2026-10-05)."""
    planner, _ = _gemini_planner(question="what is total Amount")
    amounts = {"name": "orders", "columns": ["Amount"], "rows": [[100], [200]]}
    candidate = _model_query(planner, 'SELECT SUM("orders"."Amount") FROM "orders"', [amounts])
    planner.search_pool = lambda *_args, **_kwargs: [candidate]
    copies, guarded = [], []
    copy, guard = planner._sqlite_tables, planner.guard
    planner._sqlite_tables = lambda *args: copies.append(args) or copy(*args)
    planner.guard = lambda sql: guarded.append(sql) or guard(sql)
    selection = _select(planner, "what is total turnover", [amounts], searched=[candidate])
    assert selection.served_by == "gemini-rewrite" and selection.candidate.sql == candidate.sql
    assert len(copies) == 1, f"{len(copies)} copies of the request's tables"
    assert guarded == [candidate.sql], guarded


def test_a_duration_is_how_long_rows_lasted_not_a_quantity_and_an_interval():
    """"how many users wanted neartail for greater than 6 months" read "6 months" as Interval = 'month' and
    Quantity > 6 (2026-10-05). A comparison word, a number and a unit compare how long each row lasted: the days
    from its start date to its end date, or to the question's date while it runs (engine/sql_durations.py)."""
    from datetime import date
    from engine.sql_ast import DateSpan
    from engine.sql_durations import duration_phrases
    from engine.sql_expansion import tokens

    subscriptions = {"name": "subscriptions", "columns": [
        "id", "Customer ID", "Product", "Interval", "Quantity", "Start Date (UTC)", "Canceled At (UTC)",
        "Ended At (UTC)"], "rows": [
        [1, "cus_a", "Neartail - Startup", "month", 1, "2025-01-10 09:00", "2025-09-01 10:00", "2025-09-01 10:00"],
        [2, "cus_b", "Neartail - Business", "month", 1, "2025-03-01 12:00", "2025-05-15 08:00", "2025-05-15 08:00"],
        [3, "cus_c", "Neartail - Startup", "year", 1, "2025-11-20 00:00", "", ""],
        [4, "cus_d", "Order Form - Basic", "month", 7, "2026-08-01 00:00", "", ""],
        [5, "cus_e", "Neartail - Enterprise", "month", 1, "2024-12-01 00:00", "", ""],
        [6, "cus_f", "Order Form - Premium", "month", 1, "2026-01-01 00:00", "2026-03-01 00:00", "2026-03-01 00:00"]]}
    today = date(2026, 10, 5)

    def lasted(row, end=7):
        stop = date.fromisoformat(row[end][:10]) if row[end] else today
        return (stop - date.fromisoformat(row[5][:10])).days

    planner = _hermetic_planner()
    typed = planner.schema

    def schema(tables, fks):                       # blank cells are no evidence against a date, as in production
        columns, extra, tablemap = typed(tables, fks)
        for column in columns:
            filled = [value for value in column["values"] if value not in ("", None)]
            column["is_date"] = bool(filled) and all(re.match(r"\d{4}-\d{2}-\d{2}", str(v)) for v in filled)
        return columns, extra, tablemap
    planner.schema = schema

    with patch("engine.sql_durations.question_date", return_value=today.isoformat()):
        longer = planner.serve([subscriptions], "how many subscriptions lasted more than 6 months")
        assert longer["result"]["rows"] == [[sum(lasted(row) / 30.4375 > 6 for row in subscriptions["rows"])]] == [[3]]
        selection = _select(planner, "how many subscriptions lasted more than 6 months", [subscriptions])
        span = selection.candidate.query.where.left
        assert isinstance(span, DateSpan) and (span.start.name, span.end.name, span.unit) == (
            "Start Date (UTC)", "Ended At (UTC)", "month"), selection.candidate.sql
        # "ago" runs from the date to the question's date, whatever the row's end.
        older = planner.serve([subscriptions], "how many subscriptions started more than a year ago")
        assert older["result"]["rows"] == [[sum((today - date.fromisoformat(row[5][:10])).days / 365.25 > 1
                                                 for row in subscriptions["rows"])]] == [[3]]
        # Same profile, no unit: a quantity is still a quantity.
        quantity = _select(planner, "how many subscriptions with quantity more than 6", [subscriptions])
        assert quantity.candidate.sql.endswith('WHERE "subscriptions"."Quantity" > 6'), quantity.candidate.sql
        # The customer's question reads its duration and asks about the words it cannot read.
        asked = planner.serve([subscriptions], "how many users wanted neartail for Greater than 6 months")
        assert asked.get("clarify") and asked["dropped"] == ["users", "neartail"], asked
        # A window is no duration, and a table without dates answers no duration with a quantity.
        assert duration_phrases("orders in the last 6 months", tokens("orders in the last 6 months")) == ()
        undated = {"name": "plans", "columns": ["id", "Interval", "Quantity"],
                   "rows": [[1, "month", 7], [2, "year", 1]]}
        assert _select(planner, "how many plans lasted more than 6 months", [undated]).candidate is None


# complex-unsold-products: 'Lyon' occurs only in purchases.city.
PURCHASES = {
    "name": "purchases",
    "columns": ["purchase_id", "customer_name", "city", "product_name"],
    "rows": [[1, "Alice", "Paris", "Alpha"], [2, "Alice", "Paris", "Beta"],
             [3, "Bob", "Lyon", "Gamma"], [4, "Cara", "Paris", "Gamma"],
             [5, "Dan", "Berlin", "Delta"], [6, "Eve", "Paris", "Alpha"]],
}


def test_literal_grounding_names_the_column_a_value_actually_occupies():
    text = SQLType.TEXT
    customer = ColumnRef("purchases", "customer_name", text)
    city = ColumnRef("purchases", "city", text)
    product = ColumnRef("purchases", "product_name", text)
    tables = {"purchases": PURCHASES}

    def query(where, **scope):
        return SelectQuery((SelectItem(product),), "purchases", where=where, **scope)

    def grounded(q):
        validate_query(q)
        graph = SchemaGraph.from_tables(list(tables.values()), ())
        return grounded_members([ScoredQuery(q, render_query(q), 0.0, ())], tables, graph)[0]

    lyon = Literal("Lyon", text)
    assert not grounded(query(Comparison(customer, "=", lyon))), "a city bound to a name column"
    assert not grounded(query(Comparison(lyon, "=", customer))), (
        "literal-on-left equality must receive the same grounding check")
    assert grounded(query(Comparison(city, "=", lyon)))
    assert grounded(query(Comparison(city, "=", Literal("  lyon ", text)))), "case and spacing fold"
    assert grounded(query(Comparison(customer, "=", Literal("Tokyo", text)))), (
        "a value no column holds is left alone: the honest answer is empty")
    # Exclusions are checked like equality: excluding a city from a name column excludes nothing.
    alice = Literal("Alice", text)
    for exclusion in (Comparison(customer, "!=", lyon), Comparison(customer, "<>", lyon),
                      Comparison(lyon, "!=", customer),
                      InPredicate(customer, (lyon,), negated=True)):
        assert not grounded(query(exclusion)), exclusion
    assert grounded(query(Comparison(customer, "!=", alice))), "a held value is a real exclusion"
    assert grounded(query(Comparison(city, "<>", lyon)))
    assert grounded(query(Comparison(customer, "<>", Literal("Tokyo", text)))), "held nowhere: left alone"
    assert grounded(query(InPredicate(customer, (alice,), negated=True)))
    assert not grounded(query(InPredicate(customer, (alice, lyon))))
    assert grounded(query(InPredicate(customer, (alice, Literal("Bob", text)))))
    assert grounded(query(Comparison(customer, "LIKE", Literal("%Lyon%", text)))), "patterns are out of scope"
    assert grounded(query(Comparison(ColumnRef("purchases", "purchase_id", SQLType.INTEGER), "=",
                                     Literal(3, SQLType.INTEGER)))), "numbers are out of scope"

    # Aliases resolve to their table; nested subqueries are checked in their own scope.
    aliased = SelectQuery((SelectItem(ColumnRef("p", "product_name", text)),), "purchases",
                          from_alias="p",
                          where=Comparison(ColumnRef("p", "customer_name", text), "=", lyon))
    assert literal_bindings(aliased) == (("purchases", "customer_name", "Lyon"),)
    assert not grounded(aliased)
    products = ColumnRef("products", "product_name", text)
    for column, sound in ((customer, False), (city, True)):
        inner = query(Comparison(column, "=", lyon))
        unsold = SelectQuery((SelectItem(products),), "products",
                             where=InPredicate(products, inner, negated=True))
        assert grounded(unsold) is sound, column

    # The accepted cost, pinned: with two columns of one domain, a value only the sibling column
    # holds makes the named column's test ineligible, even where the question meant that column.
    orders = {"name": "orders", "columns": ["order_id", "ship_city", "billing_city"],
              "rows": [[1, "Lyon", "Paris"], [2, "Berlin", "Paris"]]}
    ship = ColumnRef("orders", "ship_city", text)
    shipped = SelectQuery((SelectItem(ship),), "orders",
                          where=Comparison(ship, "=", Literal("Paris", text)))
    validate_query(shipped)
    assert not grounded_members([ScoredQuery(shipped, render_query(shipped), 0.0, ())],
                                {"orders": orders}, SchemaGraph.from_tables([orders], ()))[0]


def test_select_query_never_serves_a_misgrounded_query():
    """A SQL model reads the schema and a few example values, never the data its filter tests. For
    "Lyon customers" a model's query filtered customer_name = 'Lyon', which matches no row because
    'Lyon' is a city, and it was ranked first. Ranked first, it is still never served: the search's
    own reading, which binds 'Lyon' to the column that holds it, is."""
    misbound, bound = "\"customer_name\" = 'Lyon'", "\"city\" = 'Lyon'"
    planner = _hermetic_planner()
    question = "product names bought by Lyon customers"
    model = _model_query(planner, "SELECT product_name FROM purchases WHERE customer_name = 'Lyon'",
                         [PURCHASES])
    selection = _select(planner, question, [PURCHASES],
                        searched=[model, *_searched(planner, question, [PURCHASES])])
    assert misbound in selection.pool[0].sql
    assert selection.executable[0] and not selection.grounded[0] and 0 not in selection.ranking
    assert selection.selected == 1 and bound in selection.candidate.sql, selection.candidate.sql
    assert selection.record()["misgrounded"] == 1

    # Same profile, a value the column does hold: the model's query is served as before.
    alice = "\"customer_name\" = 'Alice'"
    question = "product names bought by Alice"
    model = _model_query(planner, "SELECT product_name FROM purchases WHERE customer_name = 'Alice'",
                         [PURCHASES])
    selection = _select(planner, question, [PURCHASES],
                        searched=[model, *_searched(planner, question, [PURCHASES])])
    assert selection.selected == 0 and alice in selection.candidate.sql and all(selection.grounded)


def _promotions():
    """complex-promotions, in its production upload order (web/public/index.html DATASETS)."""
    from tests.test_datasets import DATASET_DIR, _tables

    tables = {table["name"]: table for table in _tables(DATASET_DIR / "complex-promotions")}
    return [tables[name] for name in ("customers", "products", "orders", "order_items")]


def test_select_query_never_serves_a_join_the_foreign_keys_contradict():
    """Production 2026-10-01, complex-promotions leaf "For each customer, list every product name
    they have ever bought": a SQL model joined products ON orders.order_id = products.product_id,
    skipping order_items. The query ran and matched no row, it was ranked first, and the anti-join
    it fed removed nothing, so six customer-product pairs were listed instead of three."""
    bypass = '"orders"."order_id" = "products"."product_id"'
    bridge = '"order_items"."product_id" = "products"."product_id"'
    planner = _hermetic_planner()
    tables = _promotions()
    question = "For each customer, list every product name they have ever bought"
    model = _model_query(planner, (
        "SELECT T1.customer_name , T3.product_name FROM customers AS T1 JOIN orders AS T2 "
        "ON T1.customer_id = T2.customer_id JOIN products AS T3 ON T2.order_id = T3.product_id"), tables)
    selection = _select(planner, question, tables,
                        searched=[model, *_searched(planner, question, tables)])
    assert bypass in selection.pool[0].sql
    assert selection.executable[0], "it runs: execution is not evidence"
    assert not selection.grounded[0], "two keys the foreign keys tell apart"
    assert 0 not in selection.ranking
    assert bridge in selection.candidate.sql, selection.candidate.sql
    assert selection.record()["misgrounded"] == 1

    # The same bypass inside a subquery is the same defect.
    nested = _model_query(planner, (
        "SELECT customer_name FROM customers WHERE customer_id IN (SELECT orders.customer_id "
        "FROM orders JOIN products ON orders.order_id = products.product_id "
        "WHERE products.product_name = 'Alpha')"), tables)
    selection = _select(planner, "customers who bought Alpha", tables, searched=[nested])
    assert bypass in nested.sql and not selection.grounded[0] and selection.ranking == ()

    # A key equated with a column whose values it never holds (Spider car_1: model_list.ModelId
    # joined to car_names.Model, a name) can never match either.
    models = {"name": "model_list", "columns": ["model_id", "maker", "model"],
              "rows": [[1, 1, "amc"], [2, 2, "audi"], [3, 3, "bmw"]]}
    names = {"name": "car_names", "columns": ["make_id", "model", "make"],
             "rows": [[1, "amc", "amc hornet"], [2, "amc", "amc gremlin"], [3, "audi", "audi 100ls"],
                      [4, "bmw", "bmw 2002"]]}
    keyed = _model_query(
        planner, "SELECT car_names.make FROM model_list JOIN car_names ON model_list.model_id = car_names.model",
        [models, names])
    selection = _select(planner, "which makes does each model have", [models, names], searched=[keyed])
    assert '"model_list"."model_id" = "car_names"."model"' in keyed.sql
    assert not selection.grounded[0] and selection.ranking == ()


def test_select_query_serves_the_bridge_join_the_foreign_keys_state():
    """Same profile: a model's join through order_items is eligible and, ranked first, served."""
    bridge = '"order_items"."product_id" = "products"."product_id"'
    planner = _hermetic_planner()
    tables = _promotions()
    question = "customer name and product name for each order"
    model = _model_query(planner, (
        "SELECT T1.customer_name , T4.product_name FROM customers AS T1 JOIN orders AS T2 ON "
        "T1.customer_id = T2.customer_id JOIN order_items AS T3 ON T2.order_id = T3.order_id "
        "JOIN products AS T4 ON T3.product_id = T4.product_id"), tables)
    selection = _select(planner, question, tables, searched=[model, *_searched(planner, question, tables)])
    assert all(selection.grounded)
    assert selection.selected == 0 and bridge in selection.candidate.sql


def test_join_grounding_leaves_joins_the_foreign_keys_do_not_contradict():
    planner = _hermetic_planner()

    def eligible(question, tables, line):
        member = _model_query(planner, line, tables)
        selection = _select(planner, question, tables, searched=[member])
        assert selection.grounded[0] and selection.ranking == (0,), member.sql

    # A shortcut through a shared parent key (world_1: city.CountryCode = countrylanguage.CountryCode).
    country = {"name": "country", "columns": ["code", "country_name"],
               "rows": [["FR", "France"], ["ES", "Spain"], ["MX", "Mexico"]]}
    city = {"name": "city", "columns": ["city_id", "city_name", "country_code"],
            "rows": [[1, "Paris", "FR"], [2, "Lyon", "FR"], [3, "Madrid", "ES"], [4, "Puebla", "MX"],
                     [5, "Seville", "ES"]]}
    language = {"name": "language", "columns": ["country_code", "language"],
                "rows": [["FR", "French"], ["ES", "Spanish"], ["MX", "Spanish"], ["ES", "Catalan"]]}
    eligible("cities in countries that speak Spanish", [country, city, language],
             "SELECT city.city_name FROM city JOIN language ON city.country_code = "
             "language.country_code WHERE language.language = 'Spanish'")

    # An attribute join between tables the foreign keys connect.
    promotions = _promotions()
    customers = dict(promotions[0], columns=promotions[0]["columns"] + ["signup_date"],
                     rows=[row + [date] for row, date in zip(promotions[0]["rows"], [
                         "2026-01-10", "2026-01-01", "2026-02-20", "2025-12-31"])])
    eligible("customers who ordered on their signup date", [customers] + promotions[1:],
             "SELECT customers.customer_name FROM customers JOIN orders ON orders.customer_id = "
             "customers.customer_id AND orders.order_date = customers.signup_date")

    # A key join between tables no foreign key connects (flight_2's undeclared airline key).
    airlines = {"name": "airlines", "columns": ["uid", "airline"],
                "rows": [[1, "United"], [2, "JetBlue"], [3, "Delta"]]}
    flights = {"name": "flights", "columns": ["flight_no", "operator"],
               "rows": [[10, 1], [11, 1], [12, 2], [13, 3], [14, 2]]}
    eligible("how many JetBlue flights", [airlines, flights],
             "SELECT COUNT(*) FROM flights JOIN airlines ON airlines.uid = flights.operator "
             "WHERE airlines.airline = 'JetBlue'")

    # A role key the discovery did not resolve (two of its four values are not customers) whose
    # values overlap the key it is joined to.
    shoppers = {"name": "customers", "columns": ["customer_id", "customer_name"],
                "rows": [[1, "Alice"], [2, "Bob"], [3, "Cara"]]}
    shipments = {"name": "orders", "columns": ["order_id", "customer_id", "ship_to_id", "amount"],
                 "rows": [[10, 1, 2, 5.0], [11, 1, 9, 7.5], [12, 2, 1, 3.0], [13, 3, 8, 4.0],
                          [14, 3, 2, 6.0]]}
    eligible("names of the customers orders were shipped to", [shoppers, shipments],
             "SELECT customers.customer_name FROM orders JOIN customers "
             "ON orders.ship_to_id = customers.customer_id")


def test_join_grounding_reads_every_equated_column_pair():
    """JOIN ... ON pairs and column equalities in WHERE, in every scope, with aliases resolved."""
    integer, text = SQLType.INTEGER, SQLType.TEXT
    correlated = SelectQuery(
        (SelectItem(ColumnRef("o", "customer_id", integer)),), "orders", from_alias="o",
        where=Comparison(ColumnRef("o", "order_id", integer), "=",
                         ColumnRef("products", "product_id", integer)))
    query = SelectQuery(
        (SelectItem(ColumnRef("products", "product_name", text)),), "products",
        joins=(Join("order_items", ColumnRef("order_items", "product_id", integer),
                    ColumnRef("products", "product_id", integer)),),
        where=ExistsPredicate(correlated))
    assert join_pairs(query) == (
        (("order_items", "product_id"), ("products", "product_id")),
        (("orders", "order_id"), ("products", "product_id")),
    )
    # A self join equates nothing across tables.
    boss = SelectQuery(
        (SelectItem(ColumnRef("e", "name", text)),), "employees", from_alias="e",
        joins=(Join("employees", ColumnRef("e", "manager_id", integer),
                    ColumnRef("m", "employee_id", integer), alias="m"),))
    assert join_pairs(boss) == ()


def test_double_counting_reads_the_rows_each_join_repeats():
    """engine/sql_grounding.double_counted, per SELECT: a SUM or AVG whose operand reads only rows its joins
    repeat. A customer joined to her orders appears once per order; an order joined to its customer, once."""
    integer, real, text = SQLType.INTEGER, SQLType.REAL, SQLType.TEXT
    customers = {"name": "customers", "columns": ["customer_id", "country", "credit"],
                 "rows": [[1, "France", 100], [2, "Japan", 50], [3, "Japan", 70]]}
    orders = {"name": "orders", "columns": ["order_id", "customer_id", "amount"],
              "rows": [[10, 1, 5], [11, 1, 7], [12, 2, 3]]}
    items = {"name": "items", "columns": ["item_id", "order_id", "product_id", "quantity"],
             "rows": [[100, 10, 1, 1], [101, 10, 2, 2], [102, 12, 1, 4]]}
    products = {"name": "products", "columns": ["product_id", "price"], "rows": [[1, 2.5], [2, 4.0]]}
    profiles = {"name": "profiles", "columns": ["customer_id", "segment"],
                "rows": [[1, "retail"], [2, "trade"], [3, "trade"]]}
    addresses = {"name": "addresses", "columns": ["address_id", "customer_id", "city"],
                 "rows": [[1, 1, "Lyon"], [2, 1, "Paris"], [3, 2, "Osaka"]]}
    employees = {"name": "employees", "columns": ["employee_id", "manager_id", "salary"],
                 "rows": [[1, None, 90], [2, 1, 60], [3, 1, 50]]}
    tables = {table["name"]: table for table in (customers, orders, items, products, profiles, addresses, employees)}
    graph = SchemaGraph.from_tables(list(tables.values()), [
        ("orders", "customer_id", "customers", "customer_id"), ("items", "order_id", "orders", "order_id"),
        ("items", "product_id", "products", "product_id"), ("profiles", "customer_id", "customers", "customer_id"),
        ("addresses", "customer_id", "customers", "customer_id")])

    def counted(query, data=tables):
        if isinstance(query, str):
            query = import_sql(query, graph)
        validate_query(query)
        return [f"{aggregate.function}({aggregate.operand.table}.{aggregate.operand.name})"
                for aggregate in double_counted(query, data)]

    join = "FROM orders JOIN customers ON orders.customer_id = customers.customer_id"
    # Contrast: a child's measure totalled by its parent's attribute reads each order once.
    assert counted(f"SELECT customers.country, SUM(orders.amount) {join} GROUP BY customers.country") == []
    # The parent's own measure is read once per order, wherever the aggregate stands.
    assert counted(f"SELECT customers.country, SUM(customers.credit) {join} GROUP BY customers.country") == [
        "SUM(customers.credit)"]
    assert counted(f"SELECT AVG(customers.credit) {join}") == ["AVG(customers.credit)"]
    assert counted(f"SELECT customers.country {join} GROUP BY customers.country "
                   "ORDER BY SUM(customers.credit) DESC") == ["SUM(customers.credit)"]
    assert counted(f"SELECT customers.country {join} GROUP BY customers.country "
                   "HAVING AVG(customers.credit) > 60") == ["AVG(customers.credit)"]
    # Left alone: COUNT counts the joined rows whichever column it names; MIN, MAX and DISTINCT read values.
    assert counted(f"SELECT COUNT(customers.country), COUNT(*), MIN(customers.credit), MAX(customers.credit), "
                   f"COUNT(DISTINCT customers.customer_id) {join}") == []
    # A table that holds each key once repeats nothing.
    assert counted("SELECT SUM(customers.credit) FROM customers JOIN profiles "
                   "ON profiles.customer_id = customers.customer_id") == []
    # Repeats carry across joins: an order of a customer with two addresses appears twice.
    assert counted(f"SELECT SUM(orders.amount) {join} JOIN addresses "
                   "ON addresses.customer_id = customers.customer_id") == ["SUM(orders.amount)"]
    # An operand that reads a table the joins do not repeat is computed once per row of that table.
    lines = "FROM items JOIN products ON items.product_id = products.product_id"
    assert counted(f"SELECT SUM(items.quantity * products.price) {lines}") == []
    assert counted(f"SELECT SUM(products.price) {lines}") == ["SUM(products.price)"]
    # A self join: a manager appears once per report, a report once.
    bosses = "FROM employees AS e JOIN employees AS m ON e.manager_id = m.employee_id"
    assert counted(f"SELECT SUM(m.salary) {bosses}") == ["SUM(m.salary)"]
    assert counted(f"SELECT SUM(e.salary) {bosses}") == []
    # Each SELECT is read against its own joins.
    assert counted(f"SELECT customers.country FROM customers WHERE customers.credit > "
                   f"(SELECT AVG(customers.credit) {join})") == ["AVG(customers.credit)"]

    # A composite key repeats a row only when the combination repeats: each currency and each month
    # repeats in rates, but each pair once, while payments holds ("usd", 1) twice.
    payments = {"name": "payments", "columns": ["currency", "month", "amount"],
                "rows": [["usd", 1, 10], ["usd", 2, 20], ["eur", 1, 30], ["usd", 1, 5]]}
    rates = {"name": "rates", "columns": ["currency", "month", "rate"],
             "rows": [["usd", 1, 1.0], ["usd", 2, 1.1], ["eur", 1, 0.9]]}

    def converted(column):
        return SelectQuery((SelectItem(Aggregate("SUM", column)),), "payments", joins=(Join(
            "rates", ColumnRef("rates", "currency", text), ColumnRef("payments", "currency", text),
            additional=((ColumnRef("rates", "month", integer), ColumnRef("payments", "month", integer)),)),))

    keyed = {"payments": payments, "rates": rates}
    assert counted(converted(ColumnRef("payments", "amount", integer)), keyed) == []
    assert counted(converted(ColumnRef("rates", "rate", real)), keyed) == ["SUM(rates.rate)"]

    # A derived table's values are computed, not read; the uploaded table joined to it still repeats it.
    credit = ColumnRef("t", "credit", integer)
    derived = SubquerySource(SelectQuery((SelectItem(ColumnRef("customers", "customer_id", integer)),
                                          SelectItem(ColumnRef("customers", "credit", integer))), "customers"), "t")
    on = Join("orders", ColumnRef("orders", "customer_id", integer), ColumnRef("t", "customer_id", integer))
    assert counted(SelectQuery((SelectItem(Aggregate("SUM", credit)),), derived, joins=(on,))) == ["SUM(t.credit)"]
    assert counted(SelectQuery((SelectItem(Aggregate("SUM", ColumnRef("orders", "amount", integer))),),
                               derived, joins=(on,))) == []


def _ranked_first(planner, member):
    """select_query over the search's own pool with ``member`` ranked ahead of it: the production
    selection, given a pool in which a model's query outranks the search's readings."""
    select = planner.select_query

    def selection(question, norm, fks, sch, tablemap, searched=None, graph=None):
        if searched is None:
            searched = planner.search_pool(question, norm, fks, sch, graph=graph)
        return select(question, norm, fks, sch, tablemap, searched=[member, *searched], graph=graph)

    return selection


def test_named_request_decomposes_a_compound_question_a_single_query_answers_in_part():
    """The search reads "products no Paris customer bought" as a set difference; a SQL model offered
    one join that lists what Paris DID buy, and it was ranked first. Evaluation (no analysis context)
    serves selection's choice. A named product request must instead ask for decomposition and
    execute nothing: a single query cannot answer a compound question."""

    from engine.decomposition import search_probe
    from engine.deterministic.context import analysis_execution_context
    from tests.test_datasets import DATASET_DIR, _tables

    tables = _tables(DATASET_DIR / "complex-unsold-products")
    question = "List the product names that no customer from Paris has bought, ordered by product name."
    planner = _hermetic_planner()
    assert isinstance(_searched(planner, question, tables)[0].query, SetQuery)
    one_join = _model_query(planner, (
        "SELECT products.product_name FROM products JOIN purchases "
        "ON products.product_name = purchases.product_name "
        "WHERE purchases.city = 'Paris' ORDER BY products.product_name"), tables)

    with patch.object(planner, "select_query", side_effect=_ranked_first(planner, one_join)):
        evaluated = planner.serve(tables, question)
        assert evaluated["valid"] and "NOT IN" in evaluated["sql"], "the positive-only candidate must not satisfy an exclusion"
        assert evaluated["result"]["rows"]
        with analysis_execution_context({"slug": "unsold", "revision": 1}, "c_" + "9" * 32), \
                patch.object(planner, "execute", side_effect=AssertionError("partial answer executed")):
            named = planner.serve(tables, question)
    assert named.get("decomposition_required"), named
    assert named["result"] is None and named["error"] is None

    # The compose path's probe asks the same question of the search alone: no selection runs.
    with patch.object(planner, "select_query", side_effect=AssertionError("the probe ran selection")):
        assert search_probe(planner, tables, question).decomposition_required


def test_a_compound_named_request_asks_for_decomposition_before_selection_runs():
    """tests.test_complex_datasets failed on the CPU 7B SQL model (2026-09-30): a named compound
    request decoded the whole prompt before asking whether the search read it as compound, the
    decode ran past its CPU budget, and the request failed instead of asking for a decomposition.
    The search's top alone decides compound structure, so a named compound request runs no
    selection: no pool execution and no fallback call."""

    from engine.deterministic.context import analysis_execution_context
    from tests.test_datasets import DATASET_DIR, _tables

    tables = _tables(DATASET_DIR / "complex-unsold-products")
    question = "List the product names that no customer from Paris has bought, ordered by product name."
    planner, gemini = _gemini_planner()
    with patch.object(planner, "select_query", wraps=planner.select_query) as select:
        with analysis_execution_context({"slug": "unsold", "revision": 1}, "c_" + "9" * 32), \
                patch.object(planner, "execute", side_effect=AssertionError("partial answer executed")):
            named = planner.serve(tables, question)
        assert named.get("decomposition_required"), named
        assert named["result"] is None and named["error"] is None
        assert select.call_count == 0, f"a compound named request ran selection {select.call_count} time(s)"
        # Contrast: evaluation (no analysis context) serves selection's choice, so it runs it.
        assert planner.serve(tables, question)["valid"]
        assert select.call_count == 1
    assert gemini.calls == [], "the search's runnable readings never reach the fallback"


def test_named_request_never_serves_or_decomposes_a_model_only_set_operation():
    """The live demo gate caught this after a SQL model's queries could be served. "total amount for
    restaurants in United States" is a world question; the catering sheet has no country column. A
    model read it as an INTERSECT over invented values and it was ranked first, so every named
    request asked for a decomposition and the world join never ran. Compound structure is the
    search's reading: a named request neither decomposes nor serves that set operation, it serves
    the best-ranked single query. Evaluation still serves selection's choice."""

    from engine.decomposition import search_probe
    from engine.deterministic.context import analysis_execution_context
    from tests.test_datasets import DATASET_DIR, _tables

    (sheet,) = _tables(DATASET_DIR / "neartail-catering")
    tables = [dict(sheet, rows=[[name, event, int(amount)] for name, event, amount in sheet["rows"]])]
    question = "total amount for restaurants in United States"
    planner = _hermetic_planner()
    invented = _model_query(planner, ("SELECT SUM(amount) FROM catering WHERE event = 'breakfast' "
                                      "INTERSECT SELECT SUM(amount) FROM catering WHERE event = 'lunch'"),
                            tables)
    searched = _searched(planner, question, tables)
    assert isinstance(searched[0].query, SelectQuery), "the search reads one goal"
    selection = _select(planner, question, tables, searched=[invented, *searched])
    assert selection.candidate is None, "invented values do not realize the requested country"
    assert not search_probe(planner, tables, question).decomposition_required
    with analysis_execution_context({"slug": "catering", "revision": 1}, "c_" + "8" * 32):
        named = planner.serve(tables, question)
    assert not named.get("decomposition_required"), named
    assert named["result"] is None and not named["valid"], "the world route must supply the missing country"


def test_proposal_import_rejects_malformed_model_text():
    from regress.sql_import import Unsupported, import_sql

    graph = SchemaGraph.from_tables([PEOPLE], [])
    for text in ("SELECT * FROM", "EXISTS", "SELECT Name FROM people LIMIT NULL",
                 "SELECT -'x' FROM people", "SELECT Name FROM people LIMIT 'a'",
                 "SELECT " + "(" * 400 + "1" + ")" * 400 + " FROM people",
                 "SELECT Name FROM people; DROP TABLE people", "-- a comment"):
        try:
            import_sql(text, graph)
        except Unsupported:
            continue
        raise AssertionError(f"malformed model text imported: {text[:40]}")


def test_decoded_sql_normalizer_preserves_multiline_statements_and_strips_fences():

    assert normalize_decoded_sql("SELECT name\nFROM people;") == "SELECT name\nFROM people;"
    assert normalize_decoded_sql("```sql\nSELECT name\nFROM people;\n```") == (
        "SELECT name\nFROM people;"
    )


def test_the_fallback_is_not_asked_when_the_search_covers_the_question():
    """A runnable, grounded, lexically covered question stays on deterministic search."""
    planner, gemini = _gemini_planner(question="How many people are from France?")
    selection = _select(planner, "How many people are from France?")
    assert selection.selected == 0 and selection.served_by == "search" and selection.fallback is None
    served = planner.serve([PEOPLE], "How many people are from France?")
    assert served["sql"] == FRANCE_COUNT and served["result"]["rows"] == [[2]]
    assert served["fallback"] is None
    assert served["model"] == "engine - typed SQL AST planner (deterministic search)"
    assert gemini.calls == []


def test_a_question_the_search_cannot_read_is_answered_through_geminis_rewording():
    """The search reads words, and a question in Japanese gives it none: its pool is empty. The
    fallback asks Gemini once to reword the question in the tables' own words and the search answers
    the rewording, so the SQL is still the search's. The answer names the question it answered and
    says Gemini reworded it."""
    rewording = "How many people are from France?"
    planner, gemini = _gemini_planner(question=rewording)
    assert not _select(_hermetic_planner(), JAPANESE_FRANCE).candidate
    selection = _select(planner, JAPANESE_FRANCE)
    assert selection.served_by == "gemini-rewrite"
    assert selection.fallback == FallbackRecord("rewrite", "gemini-test", question=rewording)
    assert selection.candidate.sql == _searched(planner, rewording)[0].sql == FRANCE_COUNT
    served = planner.serve([PEOPLE], JAPANESE_FRANCE)
    assert served["valid"] and served["question"] == JAPANESE_FRANCE
    assert served["sql"] == FRANCE_COUNT and served["result"]["rows"] == [[2]]
    assert served["fallback"] == {"kind": "rewrite", "model": "gemini-test", "question": rewording}
    assert served["selection"]["served_by"] == "gemini-rewrite"
    assert served["selection"]["fallback"] == served["fallback"]
    assert served["model"] == ("engine - typed SQL AST planner; the search read gemini-test's "
                               "rewording of the question")
    assert [step for step, _ in gemini.calls] == ["question", "question"]


def test_rewriter_recovers_when_a_runnable_plan_ignores_part_of_the_question():
    """A runnable projection of Keyword is not accepted when the question also asks for volume."""
    sheet = {"name": "Checklist", "columns": [
        "Keyword", "shortlist", "Currency", "Avg. monthly searches",
    ], "rows": [
        ["home inspection checklist", "", "USD", 5000],
        ["fire extinguisher audit checklist", "", "USD", 5000],
    ]}
    rewording = "show the column Avg. monthly searches for the keyword home inspection checklist"
    for question in (
        "keyword volume for home inspection checklist",
        "Suchvolumen für home inspection checklist",
    ):
        planner, gemini = _gemini_planner(question=rewording)
        baseline = _select(_hermetic_planner(), question, [sheet])
        assert baseline.candidate is None, "an incomplete baseline cannot answer without rewriting"
        selection = _select(planner, question, [sheet])
        assert selection.served_by == "gemini-rewrite"
        assert "Avg. monthly searches" in selection.candidate.sql
        assert "AVG(" not in selection.candidate.sql
        assert selection.candidate.sql.endswith("WHERE \"Checklist\".\"Keyword\" = 'home inspection checklist'")
        assert selection.fallback.question == rewording
        assert [step for step, _ in gemini.calls] == ["question"]
        prompt = gemini.calls[0][1]
        assert "5000" not in prompt and "home inspection checklist" in prompt
        served = planner.serve([sheet], question)
        assert served["valid"] and served["result"]["rows"] == [[5000]], served


def test_schema_rewrite_can_resolve_a_synonym_without_changing_the_sql_reading():
    """A safe rewrite that maps an unknown measure phrase onto a schema column can confirm the
    same correct AST; it need not manufacture a different SQL string to count as a better reading."""
    planner, gemini = _gemini_planner(question="what is total Amount")
    amounts = {"name": "orders", "columns": ["Amount"], "rows": [[100], [200]]}
    candidate = _model_query(planner, 'SELECT SUM("orders"."Amount") FROM "orders"', [amounts])
    # The typed search is stubbed at its canonical-column result so this test isolates the
    # selection rule; the real search and schema rewrite are exercised in neighboring tests.
    planner.search_pool = lambda *_args, **_kwargs: [candidate]
    searched = [candidate]
    original = _select(_hermetic_planner(), "what is total turnover", [amounts], searched=searched)
    assert original.candidate is None
    selection = _select(planner, "what is total turnover", [amounts], searched=searched)
    assert selection.served_by == "gemini-rewrite"
    assert selection.candidate.sql == candidate.sql
    assert selection.candidate.sql == 'SELECT SUM("orders"."Amount") FROM "orders"'
    assert selection.fallback.question == "what is total Amount"
    assert [step for step, _ in gemini.calls] == ["question"]


# A customer's keyword-planner tab (2026-10-06), cut down: 'inspection checklist' is one keyword, and three others
# hold it inside them (10,550 searches in all).
KEYWORDS = {"name": "Checklist", "columns": ["Keyword", "Avg. monthly searches", "Competition"], "rows": [
    ["inspection checklist", 500, "Low"], ["home inspection checklist", 5000, "Low"],
    ["house inspection checklist", 5000, "High"], ["roof inspection checklist", 50, "Low"],
    ["fire extinguisher inspection list", 5000, "High"], ["forklift training", 500, "Medium"]]}
HELD_INSIDE = "WHERE LOWER(\"Checklist\".\"Keyword\") LIKE '%inspection checklist%'"


def test_a_text_column_is_an_aggregate_operand_only_where_it_ends_the_aggregate_phrase():
    """A customer's keyword sheet (2026-10-06): "total keyword volume for all inspection checklist keywords" made
    the Keyword column what "total" totals, so every reading, and Gemini's right rewording, was refused with "The
    requested total for 'Keyword' could not be computed from that field". "keyword" only says whose volume, and
    "keywords" after "for" names the rows. A text column is an aggregate's operand only where its name ends the
    phrase the aggregate word begins ("the total of the Amount", where error cells make Amount text and the
    message asks to check them); a numeric column keeps the nearest-name reading."""
    planner = _hermetic_planner()
    people = {"name": "students", "columns": ["Student", "Age"], "rows": [["Ann", 20], ["Bob", 30]]}
    orders = {"name": "orders", "columns": ["City", "Amount"], "rows": [["Paris", 10], ["Lyon", 5]]}

    def targets(question, table):
        norm, fks, sch, _ = _request(planner, [table])
        roles = analyze_question(question, SchemaGraph.from_planner(sch, fks))
        return {function: sorted(ref.name for ref in refs) for function, refs in roles.aggregate_targets.items()}

    assert targets("total keyword volume for all inspection checklist", KEYWORDS) == {}
    assert targets("total keyword volume for all inspection checklist keywords", KEYWORDS) == {}
    assert targets("total keyword count", KEYWORDS) == {}
    assert targets("average student age", people) == {"AVG": ["Age"]}
    # Contrast, the same sheets: the column that ends the aggregate's phrase is the operand.
    assert targets("total Avg. monthly searches for the Keyword home inspection checklist", KEYWORDS) == {
        "SUM": ["Avg. monthly searches"]}
    assert targets("average Age of students", people) == {"AVG": ["Age"]}
    assert targets("maximum Keyword for Low competition", KEYWORDS) == {"MAX": ["Keyword"]}
    assert targets("the maximum of the Keyword column", KEYWORDS) == {"MAX": ["Keyword"]}
    errors = {"name": "orders", "columns": ["City", "Amount"], "rows": [["Paris", "10"], ["Lyon", "#N/A"]]}
    assert targets("total of the Amount for Paris", errors) == {"SUM": ["Amount"]}
    # Negative: a numeric column keeps its place before a quantity word ("the Amount value" is the Amount).
    assert targets("total Amount value for Paris", orders) == {"SUM": ["Amount"]}


def test_a_text_right_after_contain_or_all_is_compared_inside_values():
    """A customer's keyword sheet (2026-10-06): "...for all inspection checklist" and "...containing
    'inspection checklist'" both meant every keyword holding the phrase, but 'inspection checklist' is also one
    keyword, so both compared that one value (500 of 10,550 here). Right after "contain" or "include", a text is
    one that values hold, whole value or not; after "all" or "every", a value only one row holds while others
    hold it inside them is too. The words that ask are read once the query compares the text: "containing"
    had left every substring reading unread, so Spider's "names contain the substring 'Al'" went unanswered."""
    planner = _hermetic_planner()
    for question in ("What is the sum of Avg. monthly searches for all Keyword containing 'inspection checklist'?",
                     "total Avg. monthly searches for keywords containing inspection checklist",
                     "total Avg. monthly searches for all inspection checklist",
                     "total Avg. monthly searches for all inspection checklists",
                     "total Avg. monthly searches for every inspection checklist"):
        served = planner.serve([KEYWORDS], question)
        assert served["valid"] and served["sql"].endswith(HELD_INSIDE), (question, served)
        assert served["result"]["rows"] == [[10550]], (question, served)
    # Contrast, the same sheet: the phrase named as the one keyword, without "all", is that keyword.
    for question in ("total Avg. monthly searches for the Keyword 'inspection checklist'",
                     "total Avg. monthly searches for inspection checklist"):
        served = planner.serve([KEYWORDS], question)
        assert served["result"]["rows"] == [[500]] and "LIKE" not in served["sql"], (question, served)
    # Negative: "all" over a value many rows hold is those rows ('Paris Nord' is another city), and a column's
    # words between "contain" and a whole value keep it one value (Spider's "contain the paragraph text").
    orders = {"name": "orders", "columns": ["City", "Amount"],
              "rows": [["Paris", 10], ["Paris", 20], ["Lyon", 5], ["Paris Nord", 7]]}
    served = planner.serve([orders], "total Amount for all Paris orders")
    assert served["result"]["rows"] == [[30]] and "LIKE" not in served["sql"], served
    paragraphs = {"name": "paragraphs", "columns": ["document_id", "paragraph_text"],
                  "rows": [[1, "Brazil"], [2, "Brazil and Chile"], [3, "Ireland"]]}
    served = planner.serve([paragraphs], "What are the ids of documents that contain the paragraph text 'Brazil'?")
    assert served["result"]["rows"] == [[1]] and "LIKE" not in served["sql"], served
    contestants = {"name": "contestants", "columns": ["contestant_name", "votes"],
                   "rows": [["Alice", 3], ["Bob", 4], ["Kaleb", 5]]}
    for question in ("Return the names of the contestants whose names contain the substring 'Al'.",
                     "Which contestants have names containing 'Al'?"):
        served = planner.serve([contestants], question)
        assert sorted(served["result"]["rows"]) == [["Alice"], ["Kaleb"]], (question, served)


def test_an_average_a_column_name_spells_is_the_column():
    """A customer's keyword sheet (2026-10-06): Gemini reworded "keyword volume for all inspection checklist" as
    "What is the Avg. monthly searches for all Keyword containing 'inspection checklist'?", and "Avg." in the
    column's name averaged the 38 keywords' searches: 1,115.79, an average of averages nobody asked for. An
    average a spelled column name holds never asks for one by itself; another word that asks still does, and a
    spelled total still totals ("the total amount in Paris"), in every reading the search builds."""
    planner = _hermetic_planner()
    served = planner.serve([KEYWORDS], "What is the Avg. monthly searches for Low competition?")
    assert "AVG(" not in served["sql"] and sorted(served["result"]["rows"]) == [[50], [500], [5000]], served
    served = planner.serve([KEYWORDS], "What is the Avg. monthly searches for all Keyword containing 'inspection checklist'?")
    assert "AVG(" not in served["sql"] and served["sql"].endswith(HELD_INSIDE), served
    assert sorted(row[-1] for row in served["result"]["rows"]) == [50, 500, 5000, 5000], served
    served = planner.serve([KEYWORDS], "What is the Avg. monthly searches for the Keyword 'home inspection checklist'?")
    assert served["result"]["rows"] == [[5000]], served
    # Two keywords joined by "or" are read by the constraint expansion, which averaged them too.
    either = "home inspection checklist or roof inspection checklist"
    served = planner.serve([KEYWORDS], f"Avg. monthly searches for {either}")
    assert "AVG(" not in served["sql"] and sorted(row[-1] for row in served["result"]["rows"]) == [50, 5000], served
    # Contrast: a word outside the name asks for the average.
    served = planner.serve(
        [KEYWORDS], "What is the average Avg. monthly searches for all Keyword containing 'inspection checklist'?")
    assert "AVG(" in served["sql"] and float(served["result"]["rows"][0][0]) == 2637.5, served
    assert planner.serve([KEYWORDS], f"average Avg. monthly searches for {either}")["result"]["rows"] == [[2525]]
    assert planner.serve([KEYWORDS], f"total Avg. monthly searches for {either}")["result"]["rows"] == [[5050]]
    # Negative: a spelled total still totals.
    orders = {"name": "orders", "columns": ["City", "Total Amount"],
              "rows": [["Paris", 10], ["Paris", 20], ["Lyon", 5]]}
    assert planner.serve([orders], "What is the total amount in Paris?")["result"]["rows"] == [[30]]


def test_the_rewording_of_a_keyword_volume_keeps_every_keyword_holding_the_phrase():
    """The conversation that failed (2026-10-06). The search cannot read "volume" without Gemini, which sees
    names, not cells: with tabs named Inspection and Checklist it reworded "keyword volume for all inspection
    checklist" into keywords of the Inspection tab containing 'checklist', every time; the literal check
    refused that, and the user was asked which column "volume" means. Production's rewording dropped "all"
    ("the Keyword 'inspection checklist'") and answered 500 for one keyword; and the right rewording of "total
    keyword volume for all inspection checklist" was refused against the question's Keyword total. Gemini now
    sees the question's own value quoted, and one the search reads as held inside a column's values spelled as
    that column containing it; a rewording that compares one keyword is refused, and the right one is served."""
    listed = "What is the Avg. monthly searches for each Keyword containing 'inspection checklist'?"
    planner, gemini = _gemini_planner(question=listed)
    selection = _select(planner, "keyword volume for all inspection checklist", [KEYWORDS])
    assert selection.served_by == "gemini-rewrite" and HELD_INSIDE in selection.candidate.sql, selection.record()
    prompt = gemini.calls[0][1]
    assert prompt.endswith("Question:\nkeyword volume for all Keyword containing 'inspection checklist'"), prompt
    assert "home inspection checklist" not in prompt and "5000" not in prompt
    # The prompt keeps the user's own words; a plural names the keyword all the same, and is compared as the
    # sheet writes it (LIKE '%inspection checklists%' would hold no keyword).
    plural = "What is the Avg. monthly searches for all Keyword containing 'inspection checklists'?"
    planner, gemini = _gemini_planner(question=plural)
    selection = _select(planner, "keyword volume for all inspection checklists", [KEYWORDS])
    assert gemini.calls[0][1].endswith(
        "Question:\nkeyword volume for all Keyword containing 'inspection checklists'"), gemini.calls
    assert selection.served_by == "gemini-rewrite" and HELD_INSIDE in selection.candidate.sql, selection.record()
    # Contrast: the one keyword, asked without "all", is only quoted.
    planner, gemini = _gemini_planner(question=listed)
    _select(planner, "keyword volume for inspection checklist", [KEYWORDS])
    assert gemini.calls[0][1].endswith("Question:\nkeyword volume for 'inspection checklist'"), gemini.calls
    served = planner.serve([KEYWORDS], "keyword volume for all inspection checklist")
    assert sorted(row[-1] for row in served["result"]["rows"]) == [50, 500, 5000, 5000], served
    # The rewording production served compares the one keyword: not all of them.
    planner, _ = _gemini_planner(question="What is the Avg. monthly searches for the Keyword 'inspection checklist'?")
    selection = _select(planner, "keyword volume for all inspection checklist", [KEYWORDS])
    assert selection.candidate is None and selection.fallback.kind == "none", selection.record()
    # "total" after it: the right rewording is served, and totals every keyword holding the phrase.
    total = "What is the sum of Avg. monthly searches for all Keyword containing 'inspection checklist'?"
    planner, _ = _gemini_planner(question=total)
    for question in ("total keyword volume for all inspection checklist",
                     "total keyword volume for all inspection checklist keywords"):
        served = planner.serve([KEYWORDS], question)
        assert served["valid"] and served["result"]["rows"] == [[10550]], (question, served)
        assert served["sql"].endswith(HELD_INSIDE) and served["fallback"]["question"] == total


def test_a_listing_of_numbers_names_the_rows_its_text_filter_picked():
    """A customer's keyword sheet (2026-10-06): "All inspection checklist?" was served as the Avg. monthly searches
    of every keyword containing 'inspection checklist', and the reply listed 38 numbers ("- 5000", "- 5000", ...)
    with nothing to say which keyword each was. A listing of numbers alone whose rows a text filter picked with more
    than one value (a pattern, values joined by "or" or "and", a value excluded) shows that column first; a filter
    on one value, a listing that already names its rows, a total, DISTINCT and a single row stay as they are."""
    from engine.answer_presentation import terminal_reply

    planner = _hermetic_planner()
    holding = [["inspection checklist", 500], ["home inspection checklist", 5000],
               ["house inspection checklist", 5000], ["roof inspection checklist", 50]]
    for question in ("What is the Avg. monthly searches for all Keyword containing 'inspection checklist'?",
                     "Avg. monthly searches for keywords containing inspection checklist"):
        served = planner.serve([KEYWORDS], question)
        assert served["sql"].endswith(HELD_INSIDE), (question, served)
        assert served["result"]["columns"] == ["Keyword", "Avg. monthly searches"], (question, served)
        assert served["result"]["rows"] == holding, (question, served)
    reply = terminal_reply({"status": "answered", "answer": served["result"]})
    assert reply.splitlines()[1] == "- Keyword: home inspection checklist; Avg. monthly searches: 5000", reply
    for joined in ("and", "or"):
        served = planner.serve(
            [KEYWORDS], f"Avg. monthly searches for home inspection checklist {joined} roof inspection checklist")
        assert served["result"]["rows"] == [
            ["home inspection checklist", 5000], ["roof inspection checklist", 50]], (joined, served)
    served = planner.serve([KEYWORDS], "Avg. monthly searches for keywords except inspection checklist")
    assert served["result"]["columns"][0] == "Keyword" and len(served["result"]["rows"]) == 5, served
    served = planner.serve([KEYWORDS], "top 2 Avg. monthly searches for keywords containing 'inspection checklist'")
    assert served["result"]["rows"] == [
        ["home inspection checklist", 5000], ["house inspection checklist", 5000]], served
    orders = {"name": "orders", "columns": ["City", "Amount", "Year"],
              "rows": [["Paris", 10, 2024], ["Paris Nord", 20, 2025], ["Lyon", 5, 2024], ["Nice", 7, 2025]]}
    assert planner.serve([orders], "amounts for cities containing 'Paris'")["result"]["rows"] == [
        ["Paris", 10], ["Paris Nord", 20]]
    assert planner.serve([orders], "amounts for orders not from Paris")["result"]["rows"] == [
        ["Paris Nord", 20], ["Lyon", 5], ["Nice", 7]]
    # Contrast, the same sheets: one value filtered on names no row, and a listing that names its rows keeps them.
    for question, columns in (
            ("Avg. monthly searches for the Keyword 'home inspection checklist'", ["Avg. monthly searches"]),
            ("Avg. monthly searches for Low competition", ["Avg. monthly searches"]),
            ("keywords containing 'inspection checklist'", ["Keyword"]),
            ("Keyword and Avg. monthly searches for keywords containing 'inspection checklist'",
             ["Keyword", "Avg. monthly searches"]),
            ("the different Avg. monthly searches for keywords containing 'inspection checklist'",
             ["Avg. monthly searches"])):
        served = planner.serve([KEYWORDS], question)
        assert served["result"]["columns"] == columns, (question, served)
    # Negative: a total, the one highest value and a numeric filter ask for values, not rows.
    assert planner.serve([KEYWORDS], "total Avg. monthly searches for all inspection checklist")["result"]["rows"] == [
        [10550]]
    served = planner.serve(
        [KEYWORDS], "the highest Avg. monthly searches for keywords containing 'inspection checklist'")
    assert served["result"]["columns"] == ["Avg. monthly searches"], served
    assert planner.serve([orders], "Amount for orders with Amount above 6")["result"]["columns"] == ["Amount"]


def test_unread_check_accepts_a_threshold_only_when_the_ast_realizes_it():
    from engine.tables import _query_has_unread_terms

    sales = {"name": "s", "columns": ["city", "sales"],
             "rows": [["Tokyo", 100], ["Osaka", 200], ["Nagoya", 50]]}
    planner = _hermetic_planner()
    question = "cities with total sales over 100"
    norm, fks, sch, tablemap = _request(planner, [sales])
    graph = SchemaGraph.from_planner(sch, fks)
    selected = planner.select_query(question, norm, fks, sch, tablemap)
    assert selected.candidate is not None
    assert not _query_has_unread_terms(question, selected.candidate, graph)

    reversed_comparison = _model_query(
        planner, "SELECT city, SUM(sales) FROM s GROUP BY city HAVING SUM(sales) < 100", [sales],
    )
    assert _query_has_unread_terms(question, reversed_comparison, graph)


def test_unread_check_treats_sheet_scope_words_as_context_not_filters():
    from engine.tables import _query_has_unread_terms

    sheet = {"name": "customers", "columns": ["order ID", "customer", "city", "amount"],
             "rows": [[101, "Poirot", "Brussels", 38], [102, "Lupin", "Paris", 180]]}
    planner = _hermetic_planner()
    norm, fks, sch, _tablemap = _request(planner, [sheet])
    graph = SchemaGraph.from_planner(sch, fks)
    candidate = _model_query(planner, 'SELECT COUNT(*) FROM "customers"', [sheet])
    for question in (
        "How many orders are in the current sheet?",
        "Count all non-empty Order ID rows below the header in the Customers sheet",
    ):
        assert not _query_has_unread_terms(question, candidate, graph), question


def test_gemini_rewording_cannot_drop_a_user_constraint_from_coverage():
    """The rewrite is only a retrieval aid: coverage checks the original question too."""
    from unittest.mock import patch
    from engine.knowledge_query import KnowledgeQuery, _coverage_questions

    original = "total amount for Germany"
    response = {"fallback": {"kind": "rewrite", "question": "total amount"}}
    questions = _coverage_questions(response, original)
    assert questions == [original, "total amount"]
    schema = [{"table": "orders", "name": "amount", "values": ["100"]}]
    sql = "SELECT SUM(amount) FROM orders WHERE country = 'France'"
    checked = []
    def uncovered(_self, question, _schema, _sql):
        checked.append(question)
        return ["germany"] if question == original else []
    owner = object.__new__(KnowledgeQuery)
    with patch.object(KnowledgeQuery, "_uncovered", uncovered):
        dropped = list(dict.fromkeys(
            word for question in questions for word in owner._uncovered(question, schema, sql)
        ))
    assert checked == [original, "total amount"]
    assert "germany" in dropped


def test_gemini_cannot_write_sql_when_its_rewrite_is_not_searchable():
    """When neither the typed search nor the reworded search can read a question, no SQL is served."""
    planner, gemini = _gemini_planner(question=RUSSIAN_FRANCE)
    selection = _select(planner, JAPANESE_FRANCE)
    assert selection.selected is None and selection.served_by == "search"
    assert selection.fallback == FallbackRecord(
        "none", "gemini-test", question=RUSSIAN_FRANCE,
        note="the rewritten question still has unread terms")
    assert [step for step, _ in gemini.calls] == ["question"]
    served = planner.serve([PEOPLE], JAPANESE_FRANCE)
    assert served["valid"] is False and served["sql"] is None


def test_a_disabled_fallback_is_never_asked():
    """With external models off (EXTERNAL_LLM_ENABLED unset, or Gemini unconfigured) selection is the
    search alone: no call and no fallback record, and a question it cannot read is not answered."""
    planner, gemini = _gemini_planner(question="How many people are from France?", enabled=False)
    selection = _select(planner, JAPANESE_FRANCE)
    assert selection.selected is None and selection.fallback.kind == "none"
    served = planner.serve([PEOPLE], JAPANESE_FRANCE)
    assert served["error"] == "planner: no executable AST candidate" and served["fallback"]["kind"] == "none"
    assert served["model"] == "engine - typed SQL AST planner (deterministic search)"
    assert gemini.calls == []


def test_a_gemini_outage_serves_nothing_and_is_not_cached():
    """engine.llm raises LLMUnavailable for a failed call or a timeout. That is not the prompt's
    answer: selection serves nothing and says Gemini was unavailable, and once Gemini answers again
    the same question is asked afresh."""
    planner, gemini = _gemini_planner(question="How many people are from France?", outage=True)
    selection = _select(planner, JAPANESE_FRANCE)
    assert selection.selected is None and selection.served_by == "search"
    assert selection.fallback.kind == "none"
    gemini.outage = False
    recovered = _select(planner, JAPANESE_FRANCE)
    assert recovered.served_by == "gemini-rewrite" and recovered.candidate.sql == FRANCE_COUNT
    assert selection.fallback == FallbackRecord("none", "gemini-test", note="Gemini unavailable"), (
        selection.fallback)


def test_unreadable_runnable_baseline_is_not_served_when_rewriting_fails():
    sheet = {"name": "Checklist", "columns": ["Keyword", "Avg. monthly searches"],
             "rows": [["home inspection checklist", 5000]]}
    planner, _gemini = _gemini_planner(outage=True)
    selection = _select(planner, "keyword volume for home inspection checklist", [sheet])
    assert selection.selected is None
    assert selection.fallback == FallbackRecord("none", "gemini-test", note="Gemini unavailable")


def test_the_fallback_is_stateless_across_repeated_requests():
    """Every request is rewritten independently; process-local answers are never reused."""
    planner, gemini = _gemini_planner(question="How many people are from France?")
    first = _select(planner, RUSSIAN_FRANCE)
    again = _select(planner, RUSSIAN_FRANCE)
    assert first.served_by == again.served_by == "gemini-rewrite"
    assert again.candidate.sql == first.candidate.sql == FRANCE_COUNT
    assert [step for step, _ in gemini.calls] == ["question", "question"]
    _select(planner, RUSSIAN_FRANCE, [PEOPLE, PURCHASES])
    assert [step for step, _ in gemini.calls] == ["question"] * 3


def test_gemini_reads_schema_names_but_not_cell_values():
    """The fallback sends names and types, never workbook cell values or conversation history."""
    question = "リヨンの客が買った商品は？"
    planner, gemini = _gemini_planner()
    _select(planner, question, [PURCHASES])
    (rewrite_step, rewrite), = gemini.calls
    assert rewrite_step == "question"
    assert rewrite.startswith("Tables:\n【DB_ID】 SQLite database\n【Schema】\n# Table: purchases\n[")
    assert rewrite.endswith("\n\nQuestion:\n" + question)
    assert "(purchase_id:INTEGER)" in rewrite
    assert "(customer_name:TEXT)" in rewrite
    for unsent in ("Alice", "Cara", "Dan", "Eve", "Berlin", "Delta", "Examples:"):
        assert unsent not in rewrite, unsent

    planner, gemini = _gemini_planner()
    _select(planner, question, [CUSTOMERS, ORDERS, ITEMS])
    prompt = gemini.calls[0][1]
    assert "]\n【Foreign keys】\n" in prompt
    assert "orders.Customer_ID=customers.Customer_ID" in prompt and "items.Order_ID=orders.Order_ID" in prompt
    assert not [char for char in prompt if 0xE000 <= ord(char) <= 0xF8FF], "private-use characters"


def test_gemini_rewrite_must_preserve_data_values_and_numbers():
    question = "top 5 customers in France"
    planner, gemini = _gemini_planner(question="top 3 customers")
    _norm, fks, sch, _tablemap = _request(planner, [PEOPLE])
    graph = SchemaGraph.from_planner(sch, fks)
    rewritten, note = planner.question_rewriter.rewrite(question, graph)
    assert rewritten is None
    assert note == "rewording changed a stated value or number"
    assert [step for step, _ in gemini.calls] == ["question"]


def test_evaluator_grades_the_served_selection():
    from spider.probe.full_eval import ast_predict

    planner = _hermetic_planner()
    served = _select(planner, "list person names")
    record = ast_predict(planner, [PEOPLE], "list person names")
    assert record["ok"] and record["sql"] == served.candidate.sql
    assert record["selected_candidate_rank"] == served.selected
    assert record["selection"] == served.record()
    assert record["served_by"] == served.served_by == "search"
    oracle = ast_predict(planner, [PEOPLE], "list person names", selection="pool_oracle")
    assert [entry["sql"] for entry in oracle["pool_execution"]] == [c.sql for c in served.pool]
    for rank, entry in enumerate(oracle["pool_execution"]):
        assert entry["score"] == served.pool[rank].score
        assert entry["executable"] == served.executable[rank]
        assert entry["grounded"] == served.grounded[rank]
        assert entry["eligible"] == (served.executable[rank] and served.grounded[rank])

    # An answer the fallback produced is graded as served and labelled as Gemini's, so a run with
    # the fallback enabled can never pass for the engine alone.
    planner, _ = _gemini_planner(question="How many people are from France?")
    record = ast_predict(planner, [PEOPLE], JAPANESE_FRANCE)
    assert record["ok"] and record["rows"] == [[2]] and record["served_by"] == "gemini-rewrite"
    assert record["selection"]["fallback"] == {
        "kind": "rewrite", "model": "gemini-test", "question": "How many people are from France?"}


def test_pool_oracle_counts_only_eligible_denotation_hits_and_validates_checkpoints():
    from spider.probe.full_eval import _load_checkpoint_records, _score_pool_oracle

    record = {
        "ok": True,
        "selected_candidate_rank": 1,
        "pool_execution": [
            {"rank": 0, "sql": "SELECT 1", "rows": [["gold"]], "score": 0.0,
             "features": {}, "evidence": [], "eligible": False, "executable": True,
             "grounded": False, "calculation_satisfied": False, "money_total": False},
            {"rank": 1, "sql": "SELECT 2", "rows": [["gold"]], "score": -5.0,
             "features": {"projection": 1.0}, "evidence": ["extrema:projection"], "eligible": True,
             "executable": True, "grounded": True, "calculation_satisfied": False,
             "money_total": True},
            {"rank": 2, "sql": "SELECT 3", "score": -6.0,
             "features": {}, "evidence": [], "eligible": True, "executable": True,
             "grounded": True, "calculation_satisfied": False, "money_total": False,
             "error": "OperationalError: interrupted"},
        ],
    }
    oracle, top1 = _score_pool_oracle(record, [["gold"]])
    assert oracle["strict"] and record["oracle_rank"]["strict"] == 1
    assert top1["strict"] is True
    assert record["pool"][0]["strict"] and not record["pool"][0]["eligible"]
    assert record["pool"][1]["eligible"]
    assert record["pool"][1]["features"] == {"projection": 1.0}
    assert record["pool"][1]["evidence"] == ["extrema:projection"]
    assert record["pool"][1]["calculation_satisfied"] is False
    assert record["pool"][1]["money_total"] is True
    assert record["pool"][1]["oracle_executed"] is True
    assert record["pool"][2]["eligible"] is True and not record["pool"][2]["oracle_executed"]

    selected = {"idx": 4, "stage": "ok"}
    assert _load_checkpoint_records([selected], [3, 4]) == {4: selected}
    assert _load_checkpoint_records([{"idx": 4, "stage": "timeout"}], [4], True) == {}
    for rows in ([selected, selected], [{"idx": 9}], None):
        try:
            _load_checkpoint_records(rows, [3, 4])
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate, out-of-denominator, or malformed checkpoint accepted")


def test_shared_ranking_rule_preserves_calculation_then_money_precedence():
    from engine.sql_rank import select_ranked_candidate
    assert select_ranked_candidate((), (), ()) is None
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (False,) * 3) == 1
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (True, False, True)) == 2
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (False, True, True)) == 1
    # A query that keeps the named dates comes first; the calculation and money rules apply among them.
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (False,) * 3, (True, False, False)) == 0
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (False,) * 3, (True, True, False)) == 1
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (False,) * 3, (False,) * 3) == 1
    # Among those, a query whose SUM or AVG reads only rows its joins repeat gives way to one that does not, even
    # to a satisfied calculation; when every one does, the order stands.
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (False,) * 3, (), (False, True, True)) == 0
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (False,) * 3, (), (True,) * 3) == 2
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (False,) * 3, (False, False, True),
                                   (False, False, True)) == 2
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (False,) * 3, (), (False, True, False)) == 2


def test_sql_import_maps_numeric_arithmetic_but_refuses_nonnumeric():
    """The importer every model-written query passes (regress/sql_import.py) maps row/order
    arithmetic over numeric columns into BinaryExpr (`max_f - min_f`, as Spider's gold SQL writes
    it), while the validator still refuses arithmetic over non-numeric operands — coverage without
    weakening type semantics."""
    from engine.sql_ast import render_query, validate_query
    from regress.sql_import import Unsupported

    weather = {"name": "weather", "columns": ["day", "max_f", "min_f"],
               "rows": [["2019-01-01", 60, 40], ["2019-01-02", 55, 50]]}
    graph = SchemaGraph.from_tables([weather], [])
    query = import_sql(
        "SELECT day, max_f - min_f FROM weather ORDER BY max_f - min_f LIMIT 1", graph)
    validate_query(query)
    assert execute([weather], render_query(query)) == [("2019-01-02", 5)]
    # non-numeric arithmetic (date columns) must remain refused, not silently coerced
    events = {"name": "events", "columns": ["name", "starts", "ends"],
              "rows": [["a", "2020-01-01", "2020-01-05"]]}
    dgraph = SchemaGraph.from_tables([events], [])
    try:
        validate_query(import_sql("SELECT avg(ends - starts) FROM events", dgraph))
        raise AssertionError("non-numeric arithmetic was accepted")
    except (Unsupported, ValueError):
        pass


def test_sql_import_preserves_distinct_self_join_roles():
    from regress.sql_import import Unsupported

    employees = {"name": "employees", "columns": ["id", "name"],
                 "rows": [[1, "Approver"], [2, "Operator"]]}
    documents = {"name": "documents", "columns": ["id", "approved_by", "destroyed_by"],
                 "rows": [[10, 1, 2], [11, 2, 1]]}
    tables = [employees, documents]
    graph = SchemaGraph.from_tables(tables, [])
    sql = ("SELECT a.name, b.name FROM documents AS d "
           "JOIN employees AS a ON d.approved_by=a.id "
           "JOIN employees AS b ON d.destroyed_by=b.id ORDER BY d.id")
    query = import_sql(sql, graph)
    validate_query(query)
    assert execute(tables, render_query(query)) == [("Approver", "Operator"), ("Operator", "Approver")]
    assert query.select[0].expression.table == "a" and query.select[1].expression.table == "b"
    for bad in (
        sql.replace("SELECT a.name, b.name", "SELECT name"),
        sql.replace("SELECT a.name, b.name", 'SELECT "name"'),
        sql.replace("employees AS b", "employees AS a"),
        sql.replace("d.destroyed_by=b.id", "a.id=a.id"),
        sql.replace("b.name", "missing.name"),
    ):
        try:
            validate_query(import_sql(bad, graph))
        except (Unsupported, ValueError):
            pass
        else:
            raise AssertionError(f"invalid/ambiguous self join accepted: {bad}")
    movies = {"name": "movie", "columns": ["title", "director"],
              "rows": [["A", "X"], ["B", "X"], ["C", "Y"]]}
    mgraph = SchemaGraph.from_tables([movies], [])
    gold = ("SELECT a.title, a.director FROM movie a JOIN movie b ON a.director=b.director "
            "WHERE a.title != b.title ORDER BY a.title")
    assert execute([movies], render_query(import_sql(gold, mgraph))) == [("A", "X"), ("B", "X")]


def test_sql_import_round_trip_executes_and_matches():
    """The importer must map alias-heavy, double-quoted-literal SQL, as models and Spider's gold
    queries write it, into the typed AST such that the engine's own rendering reproduces the
    original denotation."""
    city ={"name": "city", "columns": ["city_id", "cname", "status", "population"],
            "rows": [[1, "Aa", "Village", 100], [2, "Bb", "City", 5000], [3, "Cc", "Town", 900]]}
    mayor = {"name": "mayor", "columns": ["mayor_id", "city_id", "mname"],
             "rows": [[7, 2, "Kim"], [8, 3, "Lee"]]}
    fks = [{"from_table": "mayor", "from_col": "city_id", "to_table": "city", "to_col": "city_id"}]
    graph = SchemaGraph.from_tables([city, mayor], fks)
    for gold in (
        'SELECT count(*) FROM city WHERE Status != "Village"',
        "SELECT T2.mname FROM city AS T1 JOIN mayor AS T2 ON T1.city_id = T2.city_id "
        "WHERE T1.population > 800 ORDER BY T1.population DESC LIMIT 1",
        "SELECT status, sum(population) FROM city GROUP BY status HAVING sum(population) > 500",
    ):
        query = import_sql(gold, graph)
        from engine.sql_ast import render_query, validate_query
        validate_query(query)
        assert execute([city, mayor], render_query(query)) == execute([city, mayor], gold), gold



def test_order_noun_does_not_request_sort_or_group():
    tables = [{"name": "purchase_orders_over_5000",
               "columns": ["Purchase order", "Supplier name", "Net PO Value"],
               "rows": [[4001, "A", 7], [4002, "B", 9]]},
              {"name": "contracts_register", "columns": ["Number", "Supplier", "Est Value"],
               "rows": [["C1", "A", 5]]}]
    for question in ('How many purchase orders are listed?', 'Count purchase orders'):
        candidate = best(question, tables)
        assert execute(tables, candidate.sql) == [(2,)], candidate.sql
        assert not candidate.query.group_by and not candidate.query.order_by, candidate.sql
    from engine.sql_expansion import ordering_requested
    # `order`, `ordered` and `rank` are business nouns at least as often as they
    # are instructions. Spider's "highest rank points" and "the money rank of the
    # player" regressed when bare `rank` counted as a sort request: it suppressed
    # the row-superlative candidates and left an unscoped MAX subquery to win.
    for question in ('What is the value of the purchase order?',
                     'Find the name of the winner who has the highest rank points.',
                     'Return the money rank of the player with the greatest earnings.',
                     'What is the rank of the player?',
                     'top 3 products by quantity ordered',
                     'How many units were ordered by each customer?'):
        assert not ordering_requested(question), question
    for question in ('Order purchase orders by value', 'List purchase orders ordered by value',
                     'List purchases in descending order', 'Sort the orders',
                     'Rank the players by earnings', 'List the players ranked by earnings',
                     'Can you rank people by age?',
                     'Show people ranked according to age',
                     'Show people ordered alphabetically by name'):
        assert ordering_requested(question), question

    unsorted_people = {
        **PEOPLE,
        "rows": [[3, "Cara", "Spain", 40], [1, "Alice", "France", 30],
                 [2, "Bob", "France", 20]],
    }
    ranked = best("Can you rank people by age?", [unsorted_people])
    assert ranked.query.order_by, ranked.sql
    assert execute([unsorted_people], ranked.sql) == [(20,), (30,), (40,)]
    alphabetical = best("Show names for people ordered alphabetically", [unsorted_people])
    assert alphabetical.query.order_by, alphabetical.sql
    assert execute([unsorted_people], alphabetical.sql) == [("Alice",), ("Bob",), ("Cara",)]


def test_shared_table_words_do_not_collapse_distinct_projection_mentions():
    documents = {
        "name": "Documents",
        "columns": ["Document_ID", "Document_Name", "Document_Description"],
        "rows": [[1, "Plan", "Annual plan"]],
    }
    searcher = SQLSearcher.from_tables([documents], [], max_candidates=180)
    candidates = searcher.search(
        "What are the ids, names, and descriptions for all documents?",
        expand_recursive=False,
        expand_constraints=False,
        expand_extrema=False,
    )
    expected = (
        'SELECT "Documents"."Document_ID", "Documents"."Document_Name", '
        '"Documents"."Document_Description" FROM "Documents"'
    )
    assert any(candidate.sql == expected for candidate in candidates)


def test_generic_projection_respects_entity_qualifier():
    countries = {
        "name": "countries",
        "columns": ["CountryId", "CountryName", "Continent"],
        "rows": [[1, "France", 10], [2, "Germany", 10]],
    }
    continents = {
        "name": "continents",
        "columns": ["ContId", "Continent"],
        "rows": [[10, "Europe"]],
    }
    foreign_keys = [{
        "from_table": "countries",
        "from_col": "Continent",
        "to_table": "continents",
        "to_col": "ContId",
    }]
    candidate = SQLSearcher.from_tables(
        [countries, continents], foreign_keys, max_candidates=180,
    ).search("For each continent, list its id, name, and how many countries it has?")[0]
    assert '"countries"."Continent"' in candidate.sql
    assert '"continents"."Continent"' in candidate.sql
    assert '"countries"."CountryId"' not in candidate.sql
    assert '"countries"."CountryName"' not in candidate.sql


def test_entity_projection_follows_owner_foreign_key():
    poker_players = {
        "name": "poker_player",
        "columns": ["People_ID", "Final_Table_Made"],
        "rows": [[1, 3]],
    }
    people = {
        "name": "people",
        "columns": ["People_ID", "Name"],
        "rows": [[1, "Alice"]],
    }
    foreign_keys = [{
        "from_table": "poker_player",
        "from_col": "People_ID",
        "to_table": "people",
        "to_col": "People_ID",
    }]
    searcher = SQLSearcher.from_tables([poker_players, people], foreign_keys)
    candidate = searcher.search("What are the names of poker players?")[0]
    assert candidate.sql == (
        'SELECT "people"."Name" FROM "poker_player" JOIN "people" '
        'ON "poker_player"."People_ID" = "people"."People_ID"'
    )


def test_fk_attribute_phrase_does_not_project_the_source_qualifier():
    registrations = {
        "name": "registrations", "columns": ["registration_id", "country"],
        "rows": [[1, "FR"], [2, "US"]],
    }
    countries = {
        "name": "iana_country", "columns": ["alpha2", "name"],
        "rows": [["FR", "France"], ["US", "United States"]],
    }
    foreign_keys = [{
        "from_table": "registrations", "from_col": "country",
        "to_table": "iana_country", "to_col": "alpha2",
    }]
    candidate = SQLSearcher.from_tables([registrations, countries], foreign_keys).search(
        "Show the country name for each registration"
    )[0]
    assert candidate.sql == (
        'SELECT "iana_country"."name" FROM "registrations" JOIN "iana_country" '
        'ON "registrations"."country" = "iana_country"."alpha2"'
    )


def test_entity_id_does_not_follow_owner_foreign_key():
    paragraphs = {
        "name": "Paragraphs",
        "columns": ["Paragraph_ID", "Document_ID", "Paragraph_Text"],
        "rows": [[1, 10, "Hello"]],
    }
    documents = {
        "name": "Documents",
        "columns": ["Document_ID", "Document_Name"],
        "rows": [[10, "Welcome"]],
    }
    foreign_keys = [{
        "from_table": "Paragraphs",
        "from_col": "Document_ID",
        "to_table": "Documents",
        "to_col": "Document_ID",
    }]
    candidate = SQLSearcher.from_tables(
        [paragraphs, documents], foreign_keys, max_candidates=180,
    ).search(
        "Show all paragraph ids and texts for the document with name 'Welcome'."
    )[0]
    assert candidate.sql.startswith(
        'SELECT "Paragraphs"."Paragraph_ID", "Paragraphs"."Paragraph_Text" FROM '
    )


def test_duplicate_property_projection_respects_entity_qualifier():
    documents = {
        "name": "Documents",
        "columns": ["Document_ID", "Document_Description", "Template_ID"],
        "rows": [[1, "Annual memo", 1]],
    }
    templates = {
        "name": "Templates",
        "columns": ["Template_ID", "Template_Type_Code"],
        "rows": [[1, "A"]],
    }
    template_types = {
        "name": "Ref_Template_Types",
        "columns": ["Template_Type_Code", "Template_Type_Description"],
        "rows": [["A", "Type A"]],
    }
    foreign_keys = [
        {
            "from_table": "Documents",
            "from_col": "Template_ID",
            "to_table": "Templates",
            "to_col": "Template_ID",
        },
        {
            "from_table": "Templates",
            "from_col": "Template_Type_Code",
            "to_table": "Ref_Template_Types",
            "to_col": "Template_Type_Code",
        },
    ]
    candidate = SQLSearcher.from_tables(
        [documents, templates, template_types], foreign_keys, max_candidates=180,
    ).search(
        "What are the distinct template type descriptions for the templates "
        "ever used by any document?"
    )[0]
    assert '"Ref_Template_Types"."Template_Type_Description"' in candidate.sql
    assert '"Documents"."Document_Description"' not in candidate.sql


def test_directional_year_filter_targets_date_column():
    employees = {
        "name": "employees",
        "columns": ["Name", "Hired_Date", "Salary"],
        "rows": [["Ada", "2014-06-01", 10], ["Lin", "2016-03-01", 20]],
    }
    candidate = best("List employee names hired after 2015", [employees])
    assert '"employees"."Hired_Date" >= \'2016-01-01\'' in candidate.sql
    assert '"employees"."Salary"' not in candidate.sql
    assert execute([employees], candidate.sql) == [("Lin",)]


def test_month_and_dated_phrases_filter_the_date_column():
    """Chrome exploration, 2026-10-02: "How many transfers were signed in August?", "How many leads
    were submitted after August 10, 2026?" and "How many contracts were signed before July 10, 2026?"
    were planned without their date; the coverage gate declined them over the month name. A month
    compares the month of each date, and a dated phrase compares the date (engine/sql_dates.py)."""
    leads = {"name": "leads", "columns": ["submitted", "name", "budget"], "rows": [
        ["2026-08-03", "Elena", 15000], ["2026-08-04", "Pierre", 8000], ["2026-08-05", "Sara", 25000],
        ["2026-08-07", "Akira", 12000], ["2026-08-09", "Maria", 5000], ["2026-08-11", "Tom", 18000],
        ["2026-08-12", "Lucia", 6000], ["2026-08-14", "Raj", 9000], ["2026-08-15", "Emma", 4000],
        ["2026-08-18", "Hans", 11000]]}
    contracts = {"name": "contracts", "columns": ["contract", "value", "signed"], "rows": [
        ["Service Agreement", 12000, "2026-07-02"], ["License", 900000, "2026-07-05"], ["NDA", 5000, "2026-07-06"],
        ["Maintenance", 400000, "2026-07-08"], ["License", 3000000, "2026-07-10"],
        ["Service Agreement", 30000, "2026-07-12"], ["NDA", 4000, "2026-07-15"], ["Maintenance", 80000, "2026-07-18"]]}
    transfers = {"name": "transfers", "columns": ["hospital", "signed", "transfers"], "rows": [
        ["Mayo Clinic", "2026-08-04", 14], ["Massachusetts General Hospital", "2026-08-06", 11],
        ["Johns Hopkins Hospital", "2026-08-08", 9], ["Cleveland Clinic", "2026-08-11", 12],
        ["Charite", "2026-07-13", 7], ["Toronto General Hospital", "2026-09-15", 8]]}
    for question, tables, fragment, expected in (
            ("How many leads were submitted after August 10, 2026?", [leads], "'2026-08-11'", 5),
            ("How many contracts were signed before July 10, 2026?", [contracts], "'2026-07-10'", 4),
            ("How many transfers were signed in August?", [transfers], "AS TEXT), 6, 2) AS INTEGER) = 8", 4),
            # Same profile, other cues: "since" keeps the named day, "until" keeps it too, "in <month> <year>"
            # is that month, and a day-month-year or ISO date reads the same.
            ("How many leads were submitted since August 11, 2026?", [leads], "'2026-08-11'", 5),
            ("How many leads were submitted until August 11, 2026?", [leads], "'2026-08-12'", 6),
            ("total transfers in August 2026", [transfers], "'2026-09-01'", 46),
            ("How many transfers were signed on 6 August 2026?", [transfers], "'2026-08-07'", 1),
            ("how many transfers since 2026-08-06", [transfers], "'2026-08-06'", 4),
            ("How many transfers were signed in July?", [transfers], "AS TEXT), 6, 2) AS INTEGER) = 7", 1)):
        candidate = best(question, tables)
        assert fragment in candidate.sql, (question, candidate.sql)
        assert execute(tables, candidate.sql)[0][0] == expected, (question, candidate.sql)
    # Negative: "may" as a verb is no month, and a day with no year names no date to compare with, so
    # neither is a date filter (the coverage gate asks about the unread month); "after 2015" stays a year.
    for question in ("how many transfers may have been signed", "How many leads were submitted after August 10?"):
        candidate = best(question, [transfers if "transfers" in question else leads])
        assert "WHERE" not in candidate.sql, (question, candidate.sql)
    # Ordinal days and a "between" range read as dates; a month that is a value of the data stays it.
    staff = {"name": "staff", "columns": ["first_name", "hired"], "rows": [
        ["April", "2007-10-01"], ["Ben", "2008-03-04"], ["Cy", "2009-08-09"], ["April", "2010-01-01"]]}
    between = best("How many staff were hired between November 5th, 2007 and July 5th, 2009?", [staff])
    assert "'2007-11-05'" in between.sql and "'2009-07-06'" in between.sql, between.sql
    assert execute([staff], between.sql) == [(1,)], between.sql
    named = best("How many staff are named April?", [staff])
    assert "'April'" in named.sql and "SUBSTR" not in named.sql, named.sql
    # Without a date column a month is a value: "in may" filters a text month column.
    contacts = {"name": "contacts", "columns": ["age", "job", "month", "balance"], "rows": [
        [30, "admin", "may", 100], [40, "tech", "jun", 50], [35, "admin", "may", 70]]}
    contacted = best("number of contacts reached in may", [contacts])
    assert "\"contacts\".\"month\" = 'may'" in contacted.sql, contacted.sql
    assert execute([contacts], contacted.sql) == [(2,)], contacted.sql


def test_date_ranges_lists_and_quarters_filter_one_span():
    """Probe of the 2026-10-02 date reader: "from March to May 2026" and "between March and May 2026"
    filtered May alone, "between July 1 and July 10, 2026" July 10 alone, "in August and September 2026"
    both months at once (no row), and "after the 10th of August 2026" all of August. A range's ends, a
    list of consecutive months and a quarter are one span; a year one end names is the other's too."""
    dates = ["2025-11-20", "2025-12-15", "2026-01-10", "2026-02-14", "2026-03-03", "2026-04-18", "2026-05-30",
             "2026-06-02", "2026-07-01", "2026-07-05", "2026-07-10", "2026-07-11", "2026-08-05", "2026-08-10",
             "2026-08-12", "2026-09-09", "2026-10-01"]
    orders = {"name": "orders", "columns": ["order_id", "placed", "amount"],
              "rows": [[index + 1, placed, 100] for index, placed in enumerate(dates)]}
    for question, expected in (
            ("How many orders were placed from March to May 2026?", 3),
            ("How many orders were placed from March 2026 to May 2026?", 3),
            ("How many orders were placed between March and May 2026?", 3),
            ("How many orders were placed between July 1 and July 10, 2026?", 3),
            ("How many orders were placed in August and September 2026?", 4),
            ("How many orders were placed in June, July and August 2026?", 8),
            ("How many orders were placed after the 10th of August 2026?", 3),
            ("How many orders were placed in Q3 2026?", 8),
            ("How many orders were placed from November to February 2026?", 4),
            ("How many orders were placed from 5th to 10th August 2026?", 2),
            ("How many orders were placed March-May 2026?", 3)):
        candidate = best(question, [orders])
        assert execute([orders], candidate.sql)[0][0] == expected, (question, candidate.sql)

    from engine.sql_ast import ColumnRef, DatePart, SQLType
    from engine.sql_dates import date_phrases
    from engine.sql_expansion import tokens

    def read(question):
        column = ColumnRef("orders", "placed", SQLType.DATE)
        return [tuple((type(c.left) is DatePart, c.operator, c.right.value) for c in phrase.comparisons(column))
                for phrase in date_phrases(question, tokens(question))]

    # Same profile, other spans: a lone "from" is the period, "onwards" makes it "since", a yearless
    # range compares months, and a yearless quarter its three months.
    assert read("orders from August 2026") == [((False, ">=", "2026-08-01"), (False, "<", "2026-09-01"))]
    assert read("orders from August 2026 onwards") == [((False, ">=", "2026-08-01"),)]
    assert read("orders in March and April") == [((True, ">=", 3), (True, "<=", 4))]
    assert read("orders in Q4") == [((True, ">=", 10), (True, "<=", 12))]
    # Negative: a list with a gap, a range with an impossible day and a yearless day range compare
    # nothing (the coverage gate asks), "before August" names no year, and "may" as a verb is no month.
    assert read("orders in January and March 2026") == [()]
    assert read("orders between February 30 and March 3, 2026") == [()]
    assert read("orders from 5th to 10th August") == [()]
    assert read("orders before August") == []
    assert read("how many staff may join in June 2026") == [
        ((False, ">=", "2026-06-01"), (False, "<", "2026-07-01"))]


def test_the_coverage_gate_reads_the_months_a_query_compares():
    from engine.sql_dates import realized_month_words

    question = "How many transfers were signed in August?"
    month = 'SELECT COUNT(*) FROM t WHERE CAST(SUBSTR(CAST("t"."signed" AS TEXT), 6, 2) AS INTEGER) = 8'
    assert realized_month_words(question, month) == {"august"}
    dated = "How many leads were submitted after August 10, 2026?"
    assert realized_month_words(dated, "SELECT COUNT(*) FROM x WHERE submitted >= '2026-08-11'") == {"august"}
    # Contrast: another month's comparison, or none, realizes nothing.
    assert realized_month_words(question, month.replace("= 8", "= 7")) == frozenset()
    assert realized_month_words(dated, "SELECT COUNT(*) FROM x") == frozenset()
    # A range realizes the months and quarters it names only when the query keeps the whole span.
    spring = "How many orders from March to May 2026?"
    kept = "SELECT COUNT(*) FROM o WHERE placed >= '2026-03-01' AND placed < '2026-06-01'"
    assert realized_month_words(spring, kept) == {"march", "may"}
    assert realized_month_words(spring, "SELECT COUNT(*) FROM o WHERE placed >= '2026-05-01'") == frozenset()
    months = ('SELECT COUNT(*) FROM o WHERE CAST(SUBSTR(CAST("o"."placed" AS TEXT), 6, 2) AS INTEGER) >= 10 '
              'AND CAST(SUBSTR(CAST("o"."placed" AS TEXT), 6, 2) AS INTEGER) <= 12')
    assert realized_month_words("How many orders in Q4?", months) == {"q4"}


def test_a_share_divides_the_kept_rows_aggregate_by_the_whole():
    """Chrome exploration, 2026-10-02 (neartail-orders): "what share of the total amount comes from
    Paris?" served the Paris total, and "share of total amount by city" each city's total; the coverage
    gate declined both over the dropped word and proposed "total unit price". A share is the kept rows'
    aggregate over the same aggregate of every row the query reads (sql_ast.share_of)."""
    orders = {"name": "orders", "columns": ["order ID", "customer", "amount", "city"], "rows": [
        [1, "Ada", 100, "Paris"], [2, "Ben", 29, "Paris"], [3, "Cy", 60, "Lyon"], [4, "Dee", 12, "Marseille"],
        [5, "Eve", 6, "Nice"]]}
    paris = best("what share of the total amount comes from Paris?", [orders])
    assert "/ NULLIF((SELECT SUM(\"orders\".\"amount\") FROM \"orders\"), 0)" in paris.sql, paris.sql
    assert abs(execute([orders], paris.sql)[0][0] - 129 / 207) < 1e-12
    by_city = best("share of total amount by city", [orders])
    shares = dict(execute([orders], by_city.sql))
    assert set(shares) == {"Paris", "Lyon", "Marseille", "Nice"} and abs(shares["Lyon"] - 60 / 207) < 1e-12
    lyon = best("what percentage of orders are from Lyon?", [orders])
    assert "COUNT(*)" in lyon.sql and execute([orders], lyon.sql) == [(0.2,)], lyon.sql
    # Contrast, same sheet: a total asked without a share word stays the total.
    total = best("total amount from Paris", [orders])
    assert "/" not in total.sql and execute([orders], total.sql) == [(129,)], total.sql
    # Negative: a share word that names a column is that column (Spider tvshow and world_1).
    series = {"name": "TV_series", "columns": ["id", "Episode", "Share", "Rating"], "rows": [
        [1, "A", 5.5, 9], [2, "B", 7.0, 8], [3, "C", 4.5, 7]]}
    shares = best("What is minimum and maximum share of TV series?", [series])
    assert "/" not in shares.sql and "MAX(" in shares.sql and "MIN(" in shares.sql, shares.sql
    languages = {"name": "countrylanguage", "columns": ["CountryCode", "Language", "Percentage"], "rows": [
        ["ABW", "Dutch", 5.3], ["ABW", "English", 9.5], ["AFG", "Pashto", 52.4]]}
    spoken = best("What is the total number of countries where English is spoken by the largest percentage of people?",
                  [languages])
    assert "/" not in spoken.sql, spoken.sql
    # Negative: an average is never shared out.
    average = best("what share of orders have the highest average amount", [orders])
    assert "share" not in average.sql.lower() or "AVG" not in average.sql, average.sql


def test_a_share_word_is_the_aggregate_of_the_rows_it_divides():
    """Probe, 2026-10-02: "what % of total amount comes from Paris" served the Paris total (the
    tokenizers dropped "%"), "what fraction of the amount comes from Lyon" divided order counts,
    "percentage of amount by city" and "share of orders by city" were listings that divided nothing
    ("orders by" also read as a sort), and "what percentage of customers are in Paris" a share per
    customer. A share word asks for the sum of the measure it names, else the count of the rows."""
    orders = {"name": "orders", "columns": ["order ID", "customer", "amount", "city"], "rows": [
        [1, "Ada", 100, "Paris"], [2, "Ben", 29, "Paris"], [3, "Cy", 60, "Lyon"], [4, "Dee", 12, "Marseille"],
        [5, "Eve", 6, "Nice"]]}
    for question, expected in (
            ("What % of total amount comes from Paris?", 129 / 207),
            ("What fraction of the amount comes from Lyon?", 60 / 207),
            ("What percentage of customers are in Paris?", 2 / 5),
            ("What share of the amount do Paris and Lyon account for?", 189 / 207),
            ("What percentage of the amount comes from orders over 50?", 160 / 207)):
        candidate = best(question, [orders])
        assert abs(execute([orders], candidate.sql)[0][0] - expected) < 1e-12, (question, candidate.sql)
    for question, expected in (("Percentage of amount by city", {"Paris": 129 / 207, "Lyon": 60 / 207}),
                               ("share of orders by city", {"Paris": 2 / 5, "Lyon": 1 / 5})):
        candidate = best(question, [orders])
        shares = dict(execute([orders], candidate.sql))
        assert set(shares) == {"Paris", "Lyon", "Marseille", "Nice"}, (question, candidate.sql)
        assert all(abs(shares[city] - value) < 1e-12 for city, value in expected.items()), (question, candidate.sql)
    # Contrast: a "%" after a number is its unit, not a share.
    from engine.sql_expansion import tokens

    assert tokens("What % of orders") == ("what", "percent", "of", "order")
    assert "percent" not in tokens("orders with a discount over 50%") + tokens("a discount over 50 %")
    discounted = {"name": "orders", "columns": ["order ID", "discount", "amount"], "rows": [
        [1, 60, 100], [2, 10, 29], [3, 55, 60]]}
    over = best("how many orders have a discount over 50%", [discounted])
    assert "/" not in over.sql and execute([discounted], over.sql) == [(2,)], over.sql


def test_two_values_of_one_column_are_either():
    """Probe, 2026-10-02: "total amount from Paris and Lyon" filtered city = 'Paris' AND city =
    'Lyon' and answered nothing. No row holds both, and no Spider gold query conjoins two such values
    (Spider writes "the continents Asia and Europe" as OR): the search reads either value, and the
    served selection grounds no such conjunction, from the search or from a proposer."""
    orders = {"name": "orders", "columns": ["order ID", "customer", "amount", "city"], "rows": [
        [1, "Ada", 100, "Paris"], [2, "Ben", 29, "Paris"], [3, "Cy", 60, "Lyon"], [4, "Dee", 12, "Marseille"],
        [5, "Eve", 6, "Nice"]]}
    for question, expected in (("total amount from Paris and Lyon", [(189,)]),
                               ("how many orders are from Paris and Lyon", [(3,)]),
                               ("total amount from Paris or Lyon", [(189,)])):
        candidate = best(question, [orders])
        assert execute([orders], candidate.sql) == expected, (question, candidate.sql)
    # Contrast: excluding both values keeps both exclusions.
    excluded = best("total amount not from Paris and not from Lyon", [orders])
    assert execute([orders], excluded.sql) == [(18,)], excluded.sql
    # Negative: the conjunction is never searched, and never grounded for the served selection.
    from engine.sql_ast import BooleanExpr, conjunction, contradictory
    from engine.sql_candidate import ScoredQuery
    from engine.sql_grounding import grounded_members

    city = ColumnRef("orders", "city", SQLType.TEXT)
    both = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "orders",
                       where=BooleanExpr("AND", (Comparison(city, "=", Literal("Paris", SQLType.TEXT)),
                                                 Comparison(city, "=", Literal("Lyon", SQLType.TEXT)))))
    assert contradictory(both)
    searcher = SQLSearcher.from_tables([orders], [])
    assert not any(contradictory(candidate.query) for candidate in searcher.search("total amount from Paris and Lyon"))
    either = SelectQuery(both.select, "orders", where=conjunction(both.where.terms))
    assert isinstance(either.where, BooleanExpr) and either.where.operator == "OR", either.where
    pool = [ScoredQuery(both, render_query(both), 0.0, ()), ScoredQuery(either, render_query(either), 0.0, ())]
    assert grounded_members(pool, {"orders": orders}, searcher.schema) == (False, True)
    # Contrast: a self-join compares two aliases, not one column.
    first, second = ColumnRef("T2", "city", SQLType.TEXT), ColumnRef("T3", "city", SQLType.TEXT)
    route = BooleanExpr("AND", (Comparison(first, "=", Literal("Ashley", SQLType.TEXT)),
                                Comparison(second, "=", Literal("Aberdeen", SQLType.TEXT))))
    assert conjunction(route.terms) == route


def test_a_noun_over_a_number_compares_each_row():
    """Probe, 2026-10-02: "total amount of orders over 50" was read as "more than 50 orders" and lost
    its total. A plural noun before "over <n>" compares each row, as in every Spider question of that
    shape ("students over 20 years old"); "customers with over 1 orders" still counts."""
    orders = {"name": "orders", "columns": ["order ID", "customer", "amount", "city"], "rows": [
        [1, "Ada", 100, "Paris"], [2, "Ada", 29, "Paris"], [3, "Cy", 60, "Lyon"], [4, "Dee", 12, "Marseille"]]}
    total = best("total amount of orders over 50", [orders])
    assert execute([orders], total.sql) == [(160,)], total.sql
    counted = best("how many orders over 50", [orders])
    assert execute([orders], counted.sql) == [(2,)], counted.sql
    repeat = best("customers with over 1 orders", [orders])
    assert "HAVING COUNT(*) > 1" in repeat.sql and execute([orders], repeat.sql) == [("Ada",)], repeat.sql


def test_a_month_word_joins_a_date_phrase_only_as_a_date():
    """Review of the 2026-10-02 date reader: "hired in April may retire" read April-May, "hired in March
    may get a raise" dropped March, "after January 15, 2026 through March 31, 2026" kept the 15th, "from
    June, July and August 2026" paired June with July and answered 0, "between July 1 and 10, 2026"
    compared an amount, and "valid_to on 9999-12-31" crashed. Each now reads as its dates."""
    employees = {"name": "employees", "columns": ["employee_id", "name", "hired", "salary"], "rows": [
        [1, "Ann", "2024-04-03", 10], [2, "Bo", "2024-05-09", 20], [3, "Cy", "2025-04-20", 30],
        [4, "Di", "2025-03-11", 40], [5, "Ed", "2025-05-15", 50]]}
    orders = {"name": "orders", "columns": ["order_id", "placed", "amount"], "rows": [
        [1, "2026-01-15", 10], [2, "2026-01-16", 10], [3, "2026-02-20", 10], [4, "2026-03-31", 10],
        [5, "2026-04-01", 10], [6, "2026-06-02", 10], [7, "2026-07-01", 10], [8, "2026-07-05", 10],
        [9, "2026-07-10", 10], [10, "2026-07-11", 10], [11, "2026-08-10", 10], [12, "2026-08-12", 10],
        [13, "2026-08-16", 10]]}
    contracts = {"name": "contracts", "columns": ["contract_id", "client", "valid_to"], "rows": [
        [1, "A", "9999-12-31"], [2, "B", "2026-05-01"], [3, "C", "9999-12-31"]]}
    for question, tables, expected in (
            ("How many employees hired in April may retire?", [employees], 2),
            ("How many employees hired in March may get a raise?", [employees], 1),
            ("How many orders were placed after January 15, 2026 through March 31, 2026?", [orders], 3),
            ("How many orders were placed from June, July and August 2026?", [orders], 8),
            ("How many orders were placed between July 1 and 10, 2026?", [orders], 3),
            ("How many orders were placed from August 10 to 15, 2026?", [orders], 2),
            ("How many orders were placed in July or August 2026?", [orders], 7),
            ("How many contracts have valid_to on 9999-12-31?", [contracts], 2),
            ("How many contracts are valid until December 31, 9999?", [contracts], 3)):
        candidate = best(question, tables)
        assert execute(tables, candidate.sql) == [(expected,)], (question, candidate.sql)
    # A quarter column keeps its quarters: "Q3" is its value, and a game's "first quarter" is no month.
    sales = {"name": "sales", "columns": ["sale id", "sold on", "quarter", "amount"], "rows": [
        [1, "2026-01-10", "Q1", 20], [2, "2026-04-10", "Q2", 25], [3, "2026-08-02", "Q3", 30],
        [4, "2025-08-02", "Q3", 15]]}
    third = best("total amount in Q3 2026", [sales])
    assert "'Q3'" in third.sql and execute([sales], third.sql) == [(30,)], third.sql
    scores = {"name": "scores", "columns": ["score id", "game date", "team", "quarter", "points"], "rows": [
        [1, "2026-01-10", "Bulls", 1, 20], [2, "2026-05-02", "Bulls", 1, 30], [3, "2026-05-02", "Bulls", 3, 15]]}
    game = best("total points scored in the first quarter", [scores])
    assert "SUBSTR" not in game.sql and "2026-01-01" not in game.sql, game.sql


def test_either_value_reads_only_listed_values():
    """Review of the either-value rewrite (2026-10-02): it outranked "destination Paris and origin Lyon"
    bound to two columns, and a count over INTERSECT for "both English and Dutch". It reads values the
    question lists with "and" or "or", never after "both", at its conjunction's score."""
    shipments = {"name": "shipments", "columns": ["shipment id", "origin", "destination", "weight"], "rows": [
        [1, "Lyon", "Paris", 10], [2, "Paris", "Lyon", 20], [3, "Lyon", "Nice", 30], [4, "Nice", "Paris", 40],
        [5, "Lyon", "Paris", 50]]}
    routed = best("List the weight of shipments with destination Paris and origin Lyon", [shipments])
    assert execute([shipments], routed.sql) == [(10,), (50,)], routed.sql
    country = {"name": "country", "columns": ["Code", "Name"], "rows": [
        ["ABW", "Aruba"], ["NLD", "Netherlands"], ["GBR", "United Kingdom"], ["AND", "Andorra"]]}
    languages = {"name": "countrylanguage", "columns": ["CountryCode", "Language"], "rows": [
        ["ABW", "Dutch"], ["ABW", "English"], ["NLD", "Dutch"], ["NLD", "English"], ["GBR", "English"],
        ["AND", "Catalan"]]}
    fks = [{"from_table": "countrylanguage", "from_col": "CountryCode", "to_table": "country", "to_col": "Code"}]
    both = best("How many countries speak both English and Dutch?", [country, languages], fks)
    assert "INTERSECT" in both.sql and execute([country, languages], both.sql) == [(2,)], both.sql
    # Contrast: listed values of one column are either value.
    orders = {"name": "orders", "columns": ["order ID", "customer", "amount", "city"], "rows": [
        [1, "Ada", 100, "Paris"], [2, "Ben", 29, "Paris"], [3, "Cy", 60, "Lyon"], [4, "Dee", 12, "Nice"]]}
    listed = best("total amount from Paris and Lyon", [orders])
    assert execute([orders], listed.sql) == [(189,)], listed.sql
    # Negative: a value compared as text and as a number is one value, not a conflict.
    from engine.sql_ast import BooleanExpr, contradictory

    code = ColumnRef("orders", "order ID", SQLType.TEXT)
    same = SelectQuery((SelectItem(Star()),), "orders", where=BooleanExpr("AND", (
        Comparison(code, "=", Literal("1", SQLType.TEXT)), Comparison(code, "=", Literal(1, SQLType.INTEGER)))))
    assert not contradictory(same)


def test_a_count_over_times_and_a_share_word_that_names_a_thing():
    """Review, 2026-10-02: "orders over 2 times" lost its count once "orders over 50" compared rows, and the
    share cue summed "the highest share price" and "the highest percentage discount" and shared out "the %
    discount on the Desk". A share is asked by "share of", "percentage of", "% of" or "as a percentage",
    and it sums the measure the phrase after "of" names in full ("the share of the order amount")."""
    customers = {"name": "customers", "columns": ["customer_id", "name"], "rows": [[1, "Ann"], [2, "Bob"], [3, "Cy"]]}
    orders = {"name": "orders", "columns": ["order_id", "customer_id", "amount"], "rows": [
        [10, 1, 5], [11, 1, 7], [12, 1, 70], [13, 2, 60], [14, 2, 3], [15, 3, 1]]}
    fks = [{"from_table": "orders", "from_col": "customer_id", "to_table": "customers", "to_col": "customer_id"}]
    repeat = best("Which customers have placed orders over 2 times?", [customers, orders], fks)
    assert "HAVING COUNT(*) > 2" in repeat.sql and execute([customers, orders], repeat.sql) == [("Ann",)], repeat.sql
    companies = {"name": "companies", "columns": ["company id", "name", "sector", "price"], "rows": [
        [1, "Acme", "Tech", 10.0], [2, "Bolt", "Tech", 30.0], [3, "Crux", "Energy", 60.0]]}
    products = {"name": "products", "columns": ["product id", "name", "category", "discount", "price"], "rows": [
        [1, "Pen", "Office", 5, 2.0], [2, "Desk", "Furniture", 30, 200.0], [3, "Lamp", "Furniture", 15, 40.0]]}
    for question, tables, expected in (
            ("Which company has the highest share price?", [companies], 60.0),
            ("Which product has the highest percentage discount?", [products], 30),
            ("What is the % discount on the Desk?", [products], 30)):
        candidate = best(question, tables)
        assert "/" not in candidate.sql and execute(tables, candidate.sql)[0][-1] == expected, (question, candidate.sql)
    sales = {"name": "orders", "columns": ["order ID", "customer", "amount", "city"], "rows": [
        [1, "Ada", 100, "Paris"], [2, "Ben", 29, "Paris"], [3, "Cy", 60, "Lyon"], [4, "Dee", 18, "Nice"]]}
    order_amount = best("What share of the order amount comes from Paris?", [sales])
    assert abs(execute([sales], order_amount.sql)[0][0] - 129 / 207) < 1e-12, order_amount.sql
    revenue = {"name": "revenue", "columns": ["month", "region", "revenue"], "rows": [
        ["Jan", "North", 100], ["Feb", "North", 50], ["Jan", "South", 10], ["Feb", "South", 40]]}
    regions = dict(execute([revenue], best("percentage of revenue by region", [revenue]).sql))
    assert regions == {"North": 0.75, "South": 0.25}, regions
    # Negative: "share" as a verb divides nothing, and a pattern's "%" is no percent.
    shared = best("which customers share a city", [sales])
    assert "/" not in shared.sql, shared.sql
    from engine.sql_expansion import tokens

    assert "percent" not in tokens("names like 'A%'")


def test_a_lowercase_grammar_word_links_to_no_value():
    """Spider world_1, 2026-10-02: "in", "and", "is" and "are" linked to the codes 'IN', 'AND', 'IS' and
    'ARE' (India, Andorra, Iceland, the Emirates), so a question about countries in Asia filtered
    Code = 'ARE', and one with two such words had no satisfiable reading at all. A grammar word in lower
    case links to no value; one the question writes in capitals still does ("the division AS")."""
    country = {"name": "country", "columns": ["Code", "Code2", "Name", "Continent", "Population"], "rows": [
        ["ARE", "AE", "United Arab Emirates", "Asia", 2441000], ["AND", "AD", "Andorra", "Europe", 78000],
        ["IND", "IN", "India", "Asia", 1013662000], ["ISL", "IS", "Iceland", "Europe", 279000],
        ["CHN", "CN", "China", "Asia", 1277558000]]}
    asia = best("how many countries are in Asia?", [country])
    assert execute([country], asia.sql) == [(3,)], asia.sql
    europe = best("What are the names of the countries that are in Europe and have a population of 78000?",
                  [country])
    assert not {"'AND'", "'ARE'", "'IN'", "'IS'"} & set(re.findall(r"'[A-Z]+'", europe.sql)), europe.sql
    # Contrast: a value the question writes in capitals is that value.
    departments = {"name": "department", "columns": ["DName", "Division", "Building"], "rows": [
        ["History", "AS", "NEB"], ["Physics", "AS", "OLS"], ["Civil", "EN", "NEB"]]}
    division = best("How many departments are in the division AS?", [departments])
    assert execute([departments], division.sql) == [(2,)], division.sql
    # Negative: a grammar word inside a longer value still belongs to it.
    documents = {"name": "documents", "columns": ["Document_ID", "Document_Name"], "rows": [
        [1, "Welcome to NY"], [2, "Robbin CV"]]}
    named = best("Show the id of the document with name 'Welcome to NY'.", [documents])
    assert execute([documents], named.sql) == [(1,)], named.sql


def test_a_stated_comparison_on_an_aggregate_needs_no_where():
    """Spider world_1 #796 (2026-10-02): "each government form whose average life expectancy is longer
    than 72" reached HAVING AVG(...) > 72 only through a WHERE a bogus code link had built; the rewrite
    reads the comparison from the question. A bare number is no such comparison ("top 2 shoppers by
    total spend" keeps its ranking)."""
    country = {"name": "country", "columns": ["Code", "GovernmentForm", "Population", "LifeExpectancy"], "rows": [
        ["A", "Republic", 100, 80.0], ["B", "Republic", 50, 70.0], ["C", "Monarchy", 30, 60.0],
        ["D", "Monarchy", 20, 66.0]]}
    question = ("Find the government form name and total population for each government form whose average "
                "life expectancy is longer than 72.")
    candidate = best(question, [country])
    rows = execute([country], candidate.sql)
    assert "HAVING AVG(" in candidate.sql and rows in ([(150, "Republic")], [("Republic", 150)]), candidate.sql
    spend = best("top 2 shoppers by total spend", RETAIL, RETAIL_FKS)
    assert "HAVING" not in spend.sql, spend.sql


def test_a_word_of_time_orders_by_a_date_and_a_name_part_places_nothing():
    """Probe without generated SQL, 2026-10-02: "what are the first names of all students" returned one
    row ordered by age (every "first" was a top-1 cue), "who is the first student to register" ordered by
    age, and "what is the last transcript release date" kept no order. A word of time orders by the date
    the question names, in its direction, as every Spider DEV question of that shape does; the "first" or
    "last" of a name or an address line places nothing."""
    students = {"name": "student", "columns": ["StuID", "first_name", "last_name", "age", "date_first_registered"],
                "rows": [[1, "Ann", "Lee", 20, "2020-09-01"], [2, "Bo", "Kim", 22, "2019-09-01"],
                         [3, "Cy", "Ng", 21, "2021-09-01"]]}
    names = best("What are the first names of all students?", [students])
    assert execute([students], names.sql) == [("Ann",), ("Bo",), ("Cy",)], names.sql
    both = best("List the first and last name of all students", [students])
    assert "LIMIT" not in both.sql and len(execute([students], both.sql)) == 3, both.sql
    first = best("Who is the first student to register? List the first name.", [students])
    assert execute([students], first.sql) == [("Bo",)], first.sql
    transcripts = {"name": "transcripts", "columns": ["transcript_id", "transcript_date", "other_details"], "rows": [
        [1, "2018-03-01", "a"], [2, "2019-07-01", "b"], [3, "2017-01-01", "c"]]}
    last = best("What is the last transcript release date?", [transcripts])
    assert execute([transcripts], last.sql) == [("2019-07-01",)], last.sql
    # Contrast: a "by" target orders by itself ("the first 2 students by age").
    by_age = best("List the first 2 students by age", [students])
    assert 'ORDER BY "student"."age" ASC LIMIT 2' in by_age.sql, by_age.sql


def test_a_table_joins_every_reading_only_when_named_together():
    """Spider car_1, 2026-10-02: "how many car makers are there in each continent? List the continent
    name and the count" named car_names by "car" and "name" far apart, every reading joined it through
    model_list, and each maker counted once per car name. A several-word table joins every reading only
    when its words come together ("car makers"), turned around with "of", or as its first word before one
    of its columns ("the car makeid")."""
    continents = {"name": "continents", "columns": ["ContId", "Continent"], "rows": [[1, "america"], [2, "europe"]]}
    countries = {"name": "countries", "columns": ["CountryId", "CountryName", "Continent"], "rows": [
        [1, "usa", 1], [2, "germany", 2], [3, "france", 2]]}
    makers = {"name": "car_makers", "columns": ["Id", "Maker", "Country"], "rows": [
        [1, "ford", 1], [2, "bmw", 2], [3, "renault", 3], [4, "audi", 2]]}
    models = {"name": "model_list", "columns": ["ModelId", "Maker", "Model"], "rows": [
        [1, 1, "mustang"], [2, 1, "focus"], [3, 2, "x5"]]}
    names = {"name": "car_names", "columns": ["MakeId", "Model", "Make"], "rows": [
        [1, "mustang", "ford mustang"], [2, "focus", "ford focus"], [3, "x5", "bmw x5"]]}
    fks = [{"from_table": "countries", "from_col": "Continent", "to_table": "continents", "to_col": "ContId"},
           {"from_table": "car_makers", "from_col": "Country", "to_table": "countries", "to_col": "CountryId"},
           {"from_table": "model_list", "from_col": "Maker", "to_table": "car_makers", "to_col": "Id"},
           {"from_table": "car_names", "from_col": "Model", "to_table": "model_list", "to_col": "Model"}]
    tables = [continents, countries, makers, models, names]
    candidate = best("How many car makers are there in each continents? List the continent name and the count.",
                     tables, fks)
    assert '"car_names"' not in candidate.sql and '"model_list"' not in candidate.sql, candidate.sql
    counts = {next(value for value in row if isinstance(value, str)): row[-1] for row in execute(tables, candidate.sql)}
    assert counts == {"america": 1, "europe": 3}, candidate.sql


def test_an_order_of_names_its_target_as_by_does():
    """Spider concert_singer, 2026-10-02: "the names, countries, and ages for every singer in descending
    order of age" ordered by the stadium's Average and joined three tables for it. "Order of" names the
    ordering column as "by" does."""
    singer = {"name": "singer", "columns": ["Singer_ID", "Name", "Country", "Age"], "rows": [
        [1, "Joe", "Netherlands", 52], [2, "Tribal", "United States", 29], [3, "Rose", "France", 41]]}
    stadium = {"name": "stadium", "columns": ["Stadium_ID", "Location", "Capacity", "Highest", "Lowest", "Average"],
               "rows": [[1, "Raith", 10104, 4812, 1294, 2106], [2, "Ayr", 4125, 1057, 331, 638]]}
    concert = {"name": "concert", "columns": ["concert_ID", "Stadium_ID"], "rows": [[1, 1], [2, 2]]}
    sung = {"name": "singer_in_concert", "columns": ["concert_ID", "Singer_ID"], "rows": [[1, 1], [2, 2], [2, 3]]}
    fks = [{"from_table": "concert", "from_col": "Stadium_ID", "to_table": "stadium", "to_col": "Stadium_ID"},
           {"from_table": "singer_in_concert", "from_col": "concert_ID", "to_table": "concert", "to_col": "concert_ID"},
           {"from_table": "singer_in_concert", "from_col": "Singer_ID", "to_table": "singer", "to_col": "Singer_ID"}]
    tables = [stadium, singer, concert, sung]
    candidate = best("What are the names, countries, and ages for every singer in descending order of age?",
                     tables, fks)
    assert 'ORDER BY "singer"."Age" DESC' in candidate.sql and "JOIN" not in candidate.sql, candidate.sql
    assert [row[-1] for row in execute(tables, candidate.sql)] == [52, 41, 29], candidate.sql


def test_a_candidate_drops_a_key_echo_and_an_unread_join():
    """Spider car_1 and cre_Doc_Template_Mgt, 2026-10-02: "the model of the car with the smallest amount of
    horsepower" projected car_names.Model and model_list.Model, one value through a foreign key, and joined
    model_list for it; "the template ids" projected Templates.Template_ID and Documents.Template_ID. Every
    candidate projects a key once and keeps no joined table that nothing reads and the question does not
    name; COUNT(*) keeps its joins, which make the rows it counts."""
    names = {"name": "car_names", "columns": ["MakeId", "Model", "Make"], "rows": [
        [1, "chevrolet", "chevrolet chevelle"], [2, "buick", "buick skylark"], [3, "chevrolet", "chevrolet impala"]]}
    models = {"name": "model_list", "columns": ["ModelId", "Maker", "Model"], "rows": [
        [1, 1, "chevrolet"], [2, 2, "buick"]]}
    cars = {"name": "cars_data", "columns": ["Id", "Horsepower"], "rows": [[1, 130], [2, 95], [3, 220]]}
    fks = [{"from_table": "car_names", "from_col": "Model", "to_table": "model_list", "to_col": "Model"},
           {"from_table": "cars_data", "from_col": "Id", "to_table": "car_names", "to_col": "MakeId"}]
    tables = [names, models, cars]
    weakest = best("What is the model of the car with the smallest amount of horsepower?", tables, fks)
    assert '"model_list"' not in weakest.sql and execute(tables, weakest.sql) == [("buick",)], weakest.sql
    # Contrast: COUNT(*) counts the rows its joins make, and a named table stays joined.
    from engine.sql_ast import Aggregate, Join, OrderTerm, SelectItem, SelectQuery, Star
    from engine.sql_candidate import ScoredQuery

    searcher = SQLSearcher.from_tables(tables, fks)
    model = ColumnRef("model_list", "Model", SQLType.TEXT)
    versions = SelectQuery((SelectItem(model),), "model_list",
                           joins=(Join("car_names", ColumnRef("car_names", "Model", SQLType.TEXT), model),),
                           group_by=(model,), order_by=(OrderTerm(Aggregate("COUNT", Star()), "DESC"),), limit=1)
    counted = ScoredQuery(versions, render_query(versions), 1.0, ())
    assert searcher._simplified(counted, set()).sql == counted.sql
    echo = SelectQuery((SelectItem(ColumnRef("car_names", "Model", SQLType.TEXT)), SelectItem(model)), "car_names",
                       joins=(Join("model_list", model, ColumnRef("car_names", "Model", SQLType.TEXT)),))
    once = searcher._simplified(ScoredQuery(echo, render_query(echo), 1.0, ()), set())
    assert once.sql == 'SELECT "car_names"."Model" FROM "car_names"', once.sql
    named = searcher._simplified(ScoredQuery(echo, render_query(echo), 1.0, ()), {"model_list"})
    assert '"model_list"' in named.sql and named.sql.count('"Model"') == 3, named.sql


def test_a_listing_drops_a_key_that_repeats_a_read_table():
    """Spider tvshow, 2026-10-02: "what is the content of TV Channel with serial name Sky Radio" projected
    TV_series.Channel, which only repeats TV_Channel's id through the join, and joined TV_series for it;
    "TV Channel" also counted as TV_series' first word before its Channel column. A listing drops a key
    that repeats a table it reads for something else, and a word that names another table names that
    table."""
    channel = {"name": "TV_Channel", "columns": ["id", "series_name", "Content", "Language"], "rows": [
        ["700", "Sky Radio", "music", "Italian"], ["701", "Sky Music", "music", "English"]]}
    series = {"name": "TV_series", "columns": ["id", "Episode", "Channel"], "rows": [
        [1, "A Love of a Lifetime", "700"], [2, "Friendly Skies", "700"], [3, "Blowback", "701"]]}
    cartoon = {"name": "Cartoon", "columns": ["id", "Title", "Channel"], "rows": [[1, "The Rise of the Blue Beetle!", "701"]]}
    fks = [{"from_table": "TV_series", "from_col": "Channel", "to_table": "TV_Channel", "to_col": "id"},
           {"from_table": "Cartoon", "from_col": "Channel", "to_table": "TV_Channel", "to_col": "id"}]
    tables = [channel, series, cartoon]
    candidate = best('What is the content of TV Channel with serial name "Sky Radio"?', tables, fks)
    assert "JOIN" not in candidate.sql and execute(tables, candidate.sql) == [("music",)], candidate.sql
    # Contrast: the question's own rows keep their key. "The currency symbol for every order" lists each
    # order's currency beside its symbol (tests.test_enrichment serving benchmark, 2026-10-02: ccab4dc
    # dropped orders.currency as a repeat of the joined currency's code).
    orders = {"name": "orders", "columns": ["order_id", "currency", "amount"], "rows": [
        [1, "USD", 20], [2, "EUR", 30], [3, "GBP", 40]]}
    currencies = {"name": "currency_iso4217", "columns": ["alphabetic_code", "symbol"], "rows": [
        ["USD", "$"], ["EUR", "EUR"], ["GBP", "GBP"]]}
    keyed = [{"from_table": "orders", "from_col": "currency", "to_table": "currency_iso4217",
              "to_col": "alphabetic_code"}]
    symbols = SQLSearcher.from_tables([orders, currencies], keyed).search("Show the currency symbol for every order")
    gold = [("USD", "$"), ("EUR", "EUR"), ("GBP", "GBP")]
    assert any(execute([orders, currencies], symbol.sql) == gold for symbol in symbols), [c.sql for c in symbols[:3]]


def test_a_comparative_than_a_number_compares_the_measure_it_describes():
    """Spider pets_1, 2026-10-02: "how many pets have a greater weight than 10", "the number of pets whose
    weight is heavier than 10" and "the id and weight of every pet who is older than 1" matched no
    comparison cue and dropped their filter. A comparative before "than" and a number compares the
    column it describes, preferring the measure the word implies (older: age)."""
    pets = {"name": "Pets", "columns": ["PetID", "PetType", "pet_age", "weight"], "rows": [
        [2001, "cat", 3, 12.0], [2002, "dog", 2, 13.4], [2003, "dog", 1, 9.3]]}
    for question, expected in (("How many pets have a greater weight than 10?", [(2,)]),
                               ("Find the number of pets whose weight is heavier than 10.", [(2,)]),
                               ("What is the id and weight of every pet who is older than 1?",
                                [(2001, 12.0), (2002, 13.4)])):
        candidate = best(question, [pets])
        assert execute([pets], candidate.sql) == expected, (question, candidate.sql)
    older = best("What is the id and weight of every pet who is older than 1?", [pets])
    assert '"Pets"."pet_age" > 1' in older.sql, older.sql


def test_a_contained_text_compares_the_lowered_values():
    """Spider DEV, 2026-10-02: no reading had a substring filter, so "the contestants whose names contain
    the substring 'Al'", "a song having 'Hey' in its name" and "the department whose name has the word
    computer" dropped it ("'Al'" also became a state code 'AL'). A contained text compares
    LOWER(column) LIKE '%al%', on the column that holds it; "includes the text 'Korea'" stays a whole
    value."""
    contestants = {"name": "CONTESTANTS", "columns": ["contestant_number", "contestant_name"], "rows": [
        [1, "Alana"], [2, "Bob"], [3, "Kendall"], [4, "Jessie"]]}
    states = {"name": "AREA_CODE_STATE", "columns": ["area_code", "state"], "rows": [[205, "AL"], [907, "AK"]]}
    named = best("Return the names of the contestants whose names contain the substring 'Al' .", [contestants, states])
    assert "LIKE '%al%'" in named.sql and execute([contestants, states], named.sql) == [("Alana",), ("Kendall",)], named.sql
    singer = {"name": "singer", "columns": ["Singer_ID", "Name", "Country", "Song_Name"], "rows": [
        [1, "Joe", "Netherlands", "You"], [2, "Timbaland", "US", "Dangerous"], [3, "Justin", "France", "Hey Oh"]]}
    song = best("what is the name and nation of the singer who have a song having 'Hey' in its name?", [singer])
    assert 'LOWER("singer"."Song_Name") LIKE \'%hey%\'' in song.sql, song.sql
    departments = {"name": "Departments", "columns": ["department_id", "department_name", "department_description"],
                   "rows": [[1, "computer science", "error"], [2, "history", "nihil"], [3, "art", "et"]]}
    computing = best("What is the department description for the one whose name has the word computer?", [departments])
    assert execute([departments], computing.sql) == [("error",)], computing.sql
    # Contrast: "includes the text" names a whole value.
    paragraphs = {"name": "Paragraphs", "columns": ["Paragraph_ID", "Paragraph_Text", "Other_Details"], "rows": [
        [7, "Korea", "a"], [9, "North Korea", "b"]]}
    korea = best("What are the details for the paragraph that includes the text 'Korea' ?", [paragraphs])
    assert "LIKE" not in korea.sql and "\"Paragraph_Text\" = 'Korea'" in korea.sql, korea.sql


def test_a_distinct_counted_noun_counts_the_column_it_names():
    """Spider wta_1, 2026-10-02: "how many distinct countries do players come from" counted rows, since no
    column was a mention of "countries". A noun after "distinct", "different" or "unique" counts, once
    each, the column a word of whose name it is (players.country_code)."""
    players = {"name": "players", "columns": ["player_id", "first_name", "country_code"], "rows": [
        [1, "Serena", "USA"], [2, "Venus", "USA"], [3, "Angelique", "GER"], [4, "Simona", "ROU"]]}
    countries = best("How many distinct countries do players come from?", [players])
    assert "COUNT(DISTINCT" in countries.sql and execute([players], countries.sql) == [(3,)], countries.sql
    # Contrast: without "distinct" a count counts the rows.
    everyone = best("How many players are there?", [players])
    assert execute([players], everyone.sql) == [(4,)], everyone.sql


def test_a_having_reading_lists_in_the_question_order():
    """Live suite without generated SQL, 2026-10-02: "cities with total sales over 100" served
    [200, 'Osaka']: the sanitized HAVING reading put the total before the city it groups. It lists them in
    the order the question names them (Spider writes some golds total-first against such wording: an
    order-only difference)."""
    sales = {"name": "s", "columns": ["city", "sales"], "rows": [
        ["Osaka", 120], ["Osaka", 80], ["Tokyo", 50], ["Kyoto", 90]]}
    cities = best("cities with total sales over 100", [sales])
    assert execute([sales], cities.sql) == [("Osaka", 200)], cities.sql


def test_a_superlative_of_only_its_measure_is_the_extreme_value():
    """Live suite without generated SQL, 2026-10-02 (eval-formesign-termination-xlsx): "maximum notice_days"
    served the first row ordered by notice_days, and the served ties turned it into a table. A row
    superlative that lists only the column it orders, of a table the question does not name, is MAX or
    MIN of it; one that names its entity keeps the row."""
    terminations = {"name": "terminations", "columns": ["contract", "notice_days", "reason"], "rows": [
        ["A", 30, "x"], ["B", 90, "y"], ["C", 90, "z"], ["D", 60, "x"]]}
    longest = best("maximum notice_days", [terminations])
    assert "MAX(" in longest.sql and execute([terminations], longest.sql) == [(90,)], longest.sql
    shortest = best("minimum notice_days", [terminations])
    assert "MIN(" in shortest.sql and execute([terminations], shortest.sql) == [(30,)], shortest.sql
    # Contrast: the entity a question names stays its answer.
    contract = best("Which contract has the longest notice_days?", [terminations])
    assert "LIMIT 1" in contract.sql and execute([terminations], contract.sql)[0][0] in {"B", "C"}, contract.sql


def test_a_year_column_compares_the_year_itself():
    """Spider DEV, 2026-10-02: "the countries that became independent after 1950" and "the cars produced
    after 1980" compared an integer year column with a date ('1951-01-01'), which validated nowhere, so the
    search returned no reading at all (15 such questions). A column holding the year compares the year;
    a date column still compares the year's first and last day."""
    country = {"name": "country", "columns": ["Code", "Name", "IndepYear"], "rows": [
        ["ARE", "United Arab Emirates", 1971], ["AFG", "Afghanistan", 1919], ["AGO", "Angola", 1975],
        ["ALB", "Albania", 1912]]}
    independent = best("What are the names of all the countries that became independent after 1950?", [country])
    assert '"country"."IndepYear" > 1950' in independent.sql, independent.sql
    assert sorted(execute([country], independent.sql)) == [("Angola",), ("United Arab Emirates",)], independent.sql
    # Contrast: a date column compares the date.
    orders = {"name": "orders", "columns": ["order_id", "placed", "amount"], "rows": [
        [1, "2015-03-01", 10], [2, "2016-01-01", 20], [3, "2017-06-30", 30]]}
    later = best("How many orders were placed after 2015?", [orders])
    assert "'2016-01-01'" in later.sql and execute([orders], later.sql) == [(2,)], later.sql


def test_an_unjoinable_projection_drops_out_of_the_reading():
    """Spider dog_kennels, 2026-10-02: "the cost of each treatment and the corresponding treatment type
    description" also read "type" as Charges.charge_type, a table no foreign key reaches, so no join
    tree held the reading and the search returned nothing. A projected column or a named table that the
    foreign keys do not reach from what the reading filters, aggregates, groups or orders drops out."""
    treatments = {"name": "Treatments", "columns": ["treatment_id", "treatment_type_code", "cost_of_treatment"],
                  "rows": [[1, "WALK", 567], [2, "VAC", 147], [3, "EXAM", 429]]}
    types = {"name": "Treatment_Types", "columns": ["treatment_type_code", "treatment_type_description"],
             "rows": [["EXAM", "Physical examination"], ["VAC", "Vaccination"], ["WALK", "Take for a Walk"]]}
    charges = {"name": "Charges", "columns": ["charge_id", "charge_type", "charge_amount"],
               "rows": [[1, "Daily Accommodation", 98], [2, "Drugs", 322]]}
    fks = [{"from_table": "Treatments", "from_col": "treatment_type_code",
            "to_table": "Treatment_Types", "to_col": "treatment_type_code"}]
    tables = [treatments, types, charges]
    candidate = best("List the cost of each treatment and the corresponding treatment type description.", tables, fks)
    assert '"Charges"' not in candidate.sql, candidate.sql
    assert sorted(execute(tables, candidate.sql)) == [(147, "Vaccination"), (429, "Physical examination"),
                                                      (567, "Take for a Walk")], candidate.sql


def test_two_values_a_child_table_holds_are_both():
    """Spider world_1, 2026-10-02: "the number of nations that use English and Dutch" was read as either
    language. Two values joined by "and" in a table that holds several rows per listed entity (the languages
    of a country) are both, an INTERSECT; a column of the listed rows themselves stays either value (see
    test_two_values_of_one_column_are_either). "Contain the paragraph text 'Brazil'" names a whole value."""
    country = {"name": "country", "columns": ["Code", "Name"], "rows": [
        ["CAN", "Canada"], ["FRA", "France"], ["GBR", "United Kingdom"]]}
    languages = {"name": "countrylanguage", "columns": ["CountryCode", "Language"], "rows": [
        ["CAN", "English"], ["CAN", "French"], ["FRA", "French"], ["GBR", "English"]]}
    fks = [{"from_table": "countrylanguage", "from_col": "CountryCode", "to_table": "country", "to_col": "Code"}]
    tables = [country, languages]
    both = best("What is the number of nations that use English and French?", tables, fks)
    assert "INTERSECT" in both.sql and execute(tables, both.sql) == [(1,)], both.sql
    paragraphs = {"name": "Paragraphs", "columns": ["Paragraph_ID", "Document_ID", "Paragraph_Text"], "rows": [
        [1, 10, "Brazil"], [2, 10, "Ireland"], [3, 20, "Brazil"]]}
    contained = best("What are the ids of documents that contain the paragraph text 'Brazil'?", [paragraphs])
    assert "LIKE" not in contained.sql and "= 'Brazil'" in contained.sql, contained.sql


def test_a_column_word_that_introduces_a_value_is_not_listed():
    """Spider DEV, 2026-10-02: "which airline has abbreviation 'UAL'" listed the abbreviation beside the
    airline, and "the names of cities that have a population between 160000 and 900000" the population
    beside each name (30 readings listed the column they filtered). A column word before the value it
    introduces, across "of", "the" or a comparison, or right after a data value ("'Brig' type ships"),
    names the compared column; a second mention of the column still lists it."""
    airlines = {"name": "airlines", "columns": ["uid", "Airline", "Abbreviation", "Country"], "rows": [
        [1, "United Airlines", "UAL", "USA"], [2, "JetBlue Airways", "JetBlue", "USA"],
        [3, "Air Canada", "ACA", "Canada"]]}
    airline = best("Which airline has abbreviation 'UAL'?", [airlines])
    assert execute([airlines], airline.sql) == [("United Airlines",)], airline.sql
    abbreviation = best('What is the abbreviation of Airline "JetBlue Airways"?', [airlines])
    assert execute([airlines], abbreviation.sql) == [("JetBlue",)], abbreviation.sql
    city = {"name": "city", "columns": ["ID", "Name", "Population"], "rows": [
        [1, "Kabul", 1780000], [2, "Qandahar", 237500], [3, "Herat", 186800], [4, "Amsterdam", 731200]]}
    names = best("Return the names of cities that have a population between 160000 and 900000.", [city])
    assert sorted(execute([city], names.sql)) == [("Amsterdam",), ("Herat",), ("Qandahar",)], names.sql
    ship = {"name": "ship", "columns": ["id", "name", "ship_type", "tonnage"], "rows": [
        [1, "Lettice", "Brig", 249], [2, "Bon Accord", "Brig", 300], [3, "Mary", "Schooner", 120]]}
    brigs = best("What are the names of 'Brig' type ships?", [ship])
    assert sorted(execute([ship], brigs.sql)) == [("Bon Accord",), ("Lettice",)], brigs.sql
    # Contrast: the column named again in the list is listed.
    both = best("Show the name and population of cities that have a population between 160000 and 900000.",
                [city])
    assert sorted(execute([city], both.sql)) == [("Amsterdam", 731200), ("Herat", 186800),
                                                 ("Qandahar", 237500)], both.sql


def test_the_words_of_a_column_name_are_no_value():
    """Spider wta_1 and tvshow, 2026-10-02: "list the first and last name of all players" filtered on a
    player whose last name is 'Last', and "the Package Option of TV Channel ..." on a package option
    'Option' (8 readings filtered on the words of a column's name). Words that spell a several-word column
    name name that column; the same words quoted are a value."""
    players = {"name": "players", "columns": ["player_id", "first_name", "last_name", "hand"], "rows": [
        [1, "Martina", "Hingis", "R"], [2, "Mirjana", "Last", "L"], [3, "Ana", "Jones", "R"]]}
    names = best("List the last name of all players.", [players])
    assert sorted(execute([players], names.sql)) == [("Hingis",), ("Jones",), ("Last",)], names.sql
    channels = {"name": "TV_Channel", "columns": ["id", "series_name", "Package_Option"], "rows": [
        [1, "Sky Radio", "Option"], [2, "Sky Music", "Sky Famiglia"]]}
    option = best('What is the Package Option of TV Channel with serial name "Sky Radio"?', [channels])
    assert "WHERE" in option.sql and '"Package_Option" =' not in option.sql, option.sql
    assert execute([channels], option.sql) == [("Option",)], option.sql
    # Contrast: the quoted value is compared, once.
    quoted = best("Which channels have the package option 'Option'?", [channels])
    assert quoted.sql.count("'Option'") == 1 and execute([channels], quoted.sql) == [("Option",)], quoted.sql


def test_a_several_word_column_name_is_a_mention_where_it_is_said():
    """Spider student_transcripts_tracking and wta_1, 2026-10-02: "list the first name, middle name, last
    name" and "the first, middle, and last name" listed one of the names. Each column was a mention at the
    last "name", so the three were one mention read three ways. A several-word name is a mention where the
    question says it, and so is each modifier coordinated before a shared last word."""
    students = {"name": "Students", "columns": ["student_id", "first_name", "middle_name", "last_name",
                                                 "date_first_registered"], "rows": [
        [1, "Timmothy", "Anna", "Ward", "1971-02-05"], [2, "Hobart", "Lorenz", "Bergnaum", "1976-10-26"],
        [3, "Warren", "Violet", "Gibson", "1990-03-01"]]}
    listed = best("List the first name, middle name, last name of all students.", [students])
    assert sorted(execute([students], listed.sql)) == [
        ("Hobart", "Lorenz", "Bergnaum"), ("Timmothy", "Anna", "Ward"), ("Warren", "Violet", "Gibson")], listed.sql
    first = best("What is the first, middle, and last name of the first student to register?", [students])
    assert execute([students], first.sql) == [("Timmothy", "Anna", "Ward")], first.sql
    # Contrast: one modifier before the shared word is one column.
    last = best("List the last name of all students.", [students])
    assert sorted(execute([students], last.sql)) == [("Bergnaum",), ("Gibson",), ("Ward",)], last.sql


def test_the_article_a_is_no_value():
    """Spider student_transcripts_tracking, 2026-10-02: "who is enrolled in a Bachelor degree program"
    also filtered on the section named 'a' (4 readings compared the article with a value). A lowercase "a"
    is the article; a capital "A" after the first word is the value."""
    classes = {"name": "classes", "columns": ["class_id", "student", "class_name", "program"], "rows": [
        [1, "Ann", "a", "Bachelor"], [2, "Bob", "b", "Bachelor"], [3, "Cy", "a", "Master"]]}
    enrolled = best("List the students enrolled in a Bachelor program.", [classes])
    assert sorted(execute([classes], enrolled.sql)) == [("Ann",), ("Bob",)], enrolled.sql
    # Contrast: the capital is the class.
    in_a = best("List the students in class A.", [classes])
    assert sorted(execute([classes], in_a.sql)) == [("Ann",), ("Cy",)], in_a.sql


def test_a_value_compares_the_column_whose_words_introduce_it():
    """Spider wta_1, 2026-10-02: "the players who are left / L hand" compared first_name = 'L', the first of
    two columns holding 'L' by name, not the hand the question names beside it. The column whose words
    introduce the value is the one compared."""
    players = {"name": "players", "columns": ["player_id", "first_name", "last_name", "hand"], "rows": [
        [1, "Martina", "Hingis", "R"], [2, "Mirjana", "Lucic", "L"], [3, "L", "Jones", "R"]]}
    left = best("List the first and last name of all players who are left / L hand.", [players])
    assert execute([players], left.sql) == [("Mirjana", "Lucic")], left.sql
    # Contrast: the first name that introduces 'L' is compared.
    named = best("List the last name of the players whose first name is L.", [players])
    assert execute([players], named.sql) == [("Jones",)], named.sql


def test_a_value_stated_once_is_compared_once():
    """Spider concert_singer, 2026-10-02: "how many concerts are there in year 2014 or 2015" counted the
    concerts whose year and whose singers' song release year were both 2014 or 2015, joining the singers
    (10 readings compared one stated value on two columns). A value is compared in as many AND-ed terms
    as the question states it; one term may still compare it on two columns."""
    concert = {"name": "concert", "columns": ["concert_ID", "concert_Name", "Year"], "rows": [
        [1, "Auditions", 2014], [2, "Bootcamp", 2015], [3, "Home Visits", 2016], [4, "Week 1", 2014]]}
    singer = {"name": "singer", "columns": ["Singer_ID", "Name", "Song_release_year"], "rows": [
        [1, "Joe Sharp", 2014], [2, "Timbaland", 2008]]}
    links = {"name": "singer_in_concert", "columns": ["concert_ID", "Singer_ID"], "rows": [[1, 1], [2, 2], [4, 2]]}
    fks = [{"from_table": "singer_in_concert", "from_col": "concert_ID", "to_table": "concert",
            "to_col": "concert_ID"},
           {"from_table": "singer_in_concert", "from_col": "Singer_ID", "to_table": "singer", "to_col": "Singer_ID"}]
    tables = [concert, singer, links]
    counted = best("How many concerts are there in year 2014 or 2015?", tables, fks)
    assert "JOIN" not in counted.sql and execute(tables, counted.sql) == [(3,)], counted.sql
    # Contrast: one OR term compares the value on either column.
    either = best("What are the names of singers whose song release year is 2014 or whose concert year is 2014?",
                  tables, fks)
    assert sorted(execute(tables, either.sql)) == [("Joe Sharp",), ("Timbaland",)], either.sql


def test_a_column_is_named_without_the_word_of_its_kind():
    """Spider dog_kennels, 2026-10-02: "the emails of the professionals who live in the state of Hawaii or
    the state of Wisconsin" and "their role, street, city and state" listed no email_address or role_code:
    a several-word column was a mention only with every word said. The words of a column's kind (code,
    number, address, date, ...) may go unsaid when the others say what its values are; "of" or a table's
    word says nothing ("the cost of each treatment" names no date_of_treatment)."""
    professionals = {"name": "Professionals", "columns": ["professional_id", "role_code", "email_address",
                                                          "city", "state"], "rows": [
        [1, "Employee", "deanna@example.com", "West Heidi", "Indiana"],
        [2, "Employee", "lucile@example.com", "North Odellfurt", "Hawaii"],
        [3, "Veterenarian", "uboehm@example.org", "Domenickton", "Wisconsin"]]}
    emails = best("List the emails of the professionals who live in the state of Hawaii or the state of "
                  "Wisconsin.", [professionals])
    assert sorted(execute([professionals], emails.sql)) == [("lucile@example.com",), ("uboehm@example.org",)], \
        emails.sql
    roles = best("Find the role, city and state of the professionals.", [professionals])
    assert sorted(execute([professionals], roles.sql))[0] == ("Employee", "North Odellfurt", "Hawaii"), roles.sql
    # Contrast: a column named by "date" and "of" is not named by "of".
    treatments = {"name": "Treatments", "columns": ["treatment_id", "date_of_treatment", "cost_of_treatment"],
                  "rows": [[1, "2018-03-19", 567], [2, "2018-03-15", 147]]}
    costs = best("List the cost of each treatment.", [treatments])
    assert sorted(execute([treatments], costs.sql)) == [(147,), (567,)], costs.sql
    # Contrast: a word two columns of a table share names neither ("the name of tourney" is no date).
    matches = {"name": "matches", "columns": ["match_id", "tourney_name", "tourney_date"], "rows": [
        [1, "Auckland", "2013-01-01"], [2, "Auckland", "2013-01-02"], [3, "Brisbane", "2013-01-03"]]}
    tourneys = best("Find the name of tourney that has more than 1 matches.", [matches])
    assert execute([matches], tourneys.sql) == [("Auckland",)], tourneys.sql


def test_a_plural_reads_as_its_singular_everywhere():
    """Spider DEV, 2026-10-02: five modules each kept a copy of one plural rule, which read "courses" as
    "cours", "matches" as "matche" and "finishes" as "finishe", so "the course", "the match" and "the best
    finishes" named no Courses, matches or Best_Finish. sql_schema.canon is the one rule the search, its
    expansions, the ranker and the value index read with; "-ss" and short words keep their "s"."""
    from engine.sql_schema import canon

    for singular, plural in (("course", "courses"), ("match", "matches"), ("branch", "branches"),
                             ("finish", "finishes"), ("tax", "taxes"), ("class", "classes"),
                             ("address", "addresses"), ("city", "cities"), ("house", "houses")):
        assert canon(singular) == canon(plural) == singular, (singular, plural, canon(plural))
    # Contrast: a word that ends in "ss", or a short one, keeps its "s".
    assert (canon("bus"), canon("class"), canon("business")) == ("bus", "class", "business")
    players = {"name": "poker_player", "columns": ["Poker_Player_ID", "Name", "Best_Finish", "Earnings"],
               "rows": [[1, "Aleksey", 1, 476000], [2, "Maksim", 2, 133833], [3, "Yevgeni", 2, 104871]]}
    finishes = best("List the best finishes of all poker players.", [players])
    assert sorted(execute([players], finishes.sql)) == [(1,), (2,), (2,)], finishes.sql


def test_a_key_named_by_the_table_it_references_needs_its_whole_name():
    """Spider cre_Doc_Template_Mgt, 2026-10-02: "show all document names using templates with template type
    code BK" listed Documents.Template_ID beside the names: "templates" was a mention of the foreign key
    named by the table it references, and "code" counted as asking for keys. That table's word names the
    table, which the join reaches; the key is a mention only where the question says its whole name ("the
    template ids", "the ids of the documents")."""
    templates = {"name": "Templates", "columns": ["Template_ID", "Version_Number", "Template_Type_Code"],
                 "rows": [[1, 5, "BK"], [4, 4, "PP"], [6, 2, "BK"]]}
    documents = {"name": "Documents", "columns": ["Document_ID", "Template_ID", "Document_Name"], "rows": [
        [0, 6, "Introduction of OS"], [1, 1, "Understanding DB"], [3, 4, "Summer Show"]]}
    fks = [{"from_table": "Documents", "from_col": "Template_ID", "to_table": "Templates", "to_col": "Template_ID"}]
    tables = [templates, documents]
    names = best("Show all document names using templates with template type code BK.", tables, fks)
    assert sorted(execute(tables, names.sql)) == [("Introduction of OS",), ("Understanding DB",)], names.sql
    ids = best("What are the ids of the documents using templates with template type code BK?", tables, fks)
    assert sorted(execute(tables, ids.sql)) == [(0,), (1,)], ids.sql
    # Contrast: the whole name lists the key.
    keyed = best("Show the document names and their template ids.", tables, fks)
    assert sorted(execute(tables, keyed.sql)) == [("Introduction of OS", 6), ("Summer Show", 4),
                                                  ("Understanding DB", 1)], keyed.sql


def test_a_denial_is_read_by_what_it_denies():
    """Spider DEV, 2026-10-02: all 40 of the search's set-operation misfires were the extrema difference
    (an EXCEPT on a fixed 51-point base). "The average age of students who do not have any pet" listed raw
    ages, since an EXCEPT of values cannot carry the asked aggregate; "the teachers whose hometown is not
    Little Lever Urban District" was a difference of grades, not the row's own value unequal; and "students
    who do not have a cat pet" excluded the students whose pet is NOT a cat. An aggregate or a count leaves
    the anti-join that keeps it, a denied value of the listed rows is "!=", and the excluded rows are those
    the denial names."""
    student = {"name": "Student", "columns": ["StuID", "LName", "Major", "Age"], "rows": [
        [1001, "Smith", 600, 18], [1002, "Kim", 600, 19], [1003, "Jones", 600, 21], [1004, "Kumar", 600, 20],
        [1005, "Gompers", 520, 26]]}
    has_pet = {"name": "Has_Pet", "columns": ["StuID", "PetID"], "rows": [[1001, 2001], [1002, 2002],
                                                                         [1002, 2003]]}
    pets = {"name": "Pets", "columns": ["PetID", "PetType", "pet_age", "weight"], "rows": [
        [2001, "cat", 3, 12.0], [2002, "dog", 2, 13.4], [2003, "dog", 1, 9.3]]}
    fks = [{"from_table": "Has_Pet", "from_col": "StuID", "to_table": "Student", "to_col": "StuID"},
           {"from_table": "Has_Pet", "from_col": "PetID", "to_table": "Pets", "to_col": "PetID"}]
    tables = [student, has_pet, pets]
    average = best("Find the average age of students who do not have any pet.", tables, fks)
    assert [round(value, 6) for (value,) in execute(tables, average.sql)] == [22.333333], average.sql
    no_cat = best("Find the last names of students who do not have a cat pet.", tables, fks)
    assert sorted(execute(tables, no_cat.sql)) == [("Gompers",), ("Jones",), ("Kim",), ("Kumar",)], no_cat.sql
    teacher = {"name": "teacher", "columns": ["Teacher_ID", "Name", "Age", "Hometown"], "rows": [
        [1, "Joseph Huts", 32, "Blackrod Urban District"], [2, "Gustaaf Deloor", 29, "Bolton County Borough"],
        [3, "Vicente Carretero", 26, "Little Lever Urban District"]]}
    arrange = {"name": "course_arrange", "columns": ["Course_ID", "Teacher_ID", "Grade"], "rows": [
        [2, 1, 1], [3, 2, 3], [4, 3, 2]]}
    course = {"name": "course", "columns": ["Course_ID", "Staring_Date", "Course"], "rows": [
        [2, "6 May", "Science"], [3, "7 May", "English"], [4, "9 May", "Art"]]}
    teacher_fks = [
        {"from_table": "course_arrange", "from_col": "Teacher_ID", "to_table": "teacher", "to_col": "Teacher_ID"},
        {"from_table": "course_arrange", "from_col": "Course_ID", "to_table": "course", "to_col": "Course_ID"}]
    teachers = [teacher, arrange, course]
    elsewhere = best("List the name of teachers whose hometown is not Little Lever Urban District.", teachers,
                     teacher_fks)
    assert sorted(execute(teachers, elsewhere.sql)) == [("Gustaaf Deloor",), ("Joseph Huts",)], elsewhere.sql


def test_a_ranking_measure_is_not_an_asked_aggregate():
    """tests.test_complex_datasets, 2026-10-02: "find the top 3 products by units sold and the top 2 customers by
    total spend, then list each pair where that customer has never bought that product" stopped asking for a
    decomposition. "Total" in "by total spend" read as an asked aggregate, which leaves a denial no difference,
    and the compound reading went with it (engine/decomposition.compound_candidate). An aggregate word after
    "by" names the measure the rows are ranked by; an aggregate the question asks of the rows left still
    takes the anti-join that keeps it."""
    from engine.decomposition import compound_candidate

    student = {"name": "Student", "columns": ["StuID", "LName", "Major", "Age"], "rows": [
        [1001, "Smith", 600, 18], [1002, "Kim", 600, 19], [1003, "Jones", 600, 21], [1004, "Kumar", 600, 20],
        [1005, "Gompers", 520, 26]]}
    has_pet = {"name": "Has_Pet", "columns": ["StuID", "PetID"], "rows": [[1001, 2001], [1002, 2002],
                                                                         [1002, 2003]]}
    pets = {"name": "Pets", "columns": ["PetID", "PetType", "pet_age", "weight"], "rows": [
        [2001, "cat", 3, 12.0], [2002, "dog", 2, 13.4], [2003, "dog", 1, 9.3]]}
    fks = [{"from_table": "Has_Pet", "from_col": "StuID", "to_table": "Student", "to_col": "StuID"},
           {"from_table": "Has_Pet", "from_col": "PetID", "to_table": "Pets", "to_col": "PetID"}]
    tables = [student, has_pet, pets]
    ranked = SQLSearcher.from_tables(tables, fks).search(
        "Find the top 2 majors by total age, then list the last names of students who do not have any pet.")
    assert compound_candidate(ranked) is not None, ranked[0].sql
    total = best("Find the total age of students who do not have any pet.", tables, fks)
    assert execute(tables, total.sql) == [(67,)], total.sql


def test_a_total_by_month_groups_by_the_year_month():
    """2026-10-02: "total amount by month" and "monthly total amount" were one ungrouped total, "how many orders
    per month" was grouped by customer, and "which month had the highest total amount" was the overall total. A
    month groups by the year-month of the date column, so August 2025 and August 2026 stay two rows, read in
    calendar order. A month the question names still filters, another grouping is unchanged, a text column
    named month is that column, and a spelled column name holding "monthly" asks for no month."""
    orders = {"name": "orders", "columns": ["order_id", "customer", "city", "date", "amount"], "rows": [
        [1, "Ada", "Paris", "2026-07-03", 40], [2, "Bo", "Lyon", "2026-07-15", 25],
        [3, "Ada", "Paris", "2026-08-01", 60], [4, "Cy", "Nice", "2026-08-20", 30],
        [5, "Bo", "Lyon", "2026-08-28", 45], [6, "Cy", "Nice", "2025-08-09", 67]]}
    tables = [orders]
    for question in ("total amount by month", "monthly total amount", "What is the total amount for each month?"):
        answer = best(question, tables)
        assert execute(tables, answer.sql) == [("2025-08", 67), ("2026-07", 65), ("2026-08", 135)], (question,
                                                                                                    answer.sql)
    per_month = best("how many orders per month", tables)
    assert execute(tables, per_month.sql) == [("2025-08", 1), ("2026-07", 2), ("2026-08", 3)], per_month.sql
    top = best("which month had the highest total amount", tables)
    assert execute(tables, top.sql) == [("2026-08", 135)], top.sql
    # The coverage gate reads "month" as realized only where the query groups by the year-month.
    from engine.sql_dates import realized_month_words
    assert {"month", "monthly"} <= realized_month_words("monthly total amount", top.sql)
    assert not realized_month_words("total amount by month", 'SELECT SUM("orders"."amount") FROM "orders"')
    # Contrastive: a named month filters, and a grouping by city stays one.
    august = best("total amount in August", tables)
    assert execute(tables, august.sql) == [(202,)], august.sql
    by_city = best("total amount by city", tables)
    assert sorted(execute(tables, by_city.sql)) == [("Lyon", 70), ("Nice", 97), ("Paris", 100)], by_city.sql
    # Negative: a text column named month groups as that column; a spelled "Avg. monthly searches" beside a
    # date column asks for no month.
    leads = {"name": "leads", "columns": ["lead_id", "month", "contacted", "balance"], "rows": [
        [1, "may", "2026-05-03", 100], [2, "jun", "2026-06-11", 50], [3, "may", "2026-05-20", 30]]}
    by_text_month = best("total balance by month", [leads])
    assert sorted(execute([leads], by_text_month.sql)) == [("jun", 50), ("may", 130)], by_text_month.sql
    keywords = {"name": "keywords", "columns": ["Keyword", "Avg. monthly searches", "Checked"], "rows": [
        ["forklift checklist", 5000, "2026-08-01"], ["forklift inspection", 500, "2026-09-01"]]}
    searches = best("total of the avg. monthly searches", [keywords])
    assert execute([keywords], searches.sql) == [(5500,)], searches.sql


def test_same_shaped_tabs_answer_a_stated_keyword():
    """A customer's keyword-planner workbook, 2026-10-02: three tabs shaped alike (Forklift, Checklist and
    Inspection, each with Keyword, Avg. monthly searches, Top of page bid (low range), ...) gave "total of the
    avg. monthly searches for forklift inspection" no reading at all ("planner: no valid AST candidate"), and the
    Forklift tab alone summed it and averaged it too. The keyword 'forklift inspection' named the Forklift and
    Inspection tabs, which no key joins; "of" in "Top of page bid" qualified "the avg. monthly searches" as
    another entity's, so the column was never named; and "avg" in its name read as an average beside the asked
    total. A spelled column name's aggregate word still asks the aggregate when no other word does."""
    columns = ["Keyword", "Avg. monthly searches", "Competition", "Top of page bid (low range)"]

    def tab(name, rows):
        return {"name": name, "columns": columns, "rows": [list(row) for row in rows]}

    forklift = tab("Forklift", [("forklift inspection", 500, "Medium", 3.47),
                                ("forklift inspection checklist", 5000, "High", 1.37),
                                ("forklift checklist", 5000, "High", 1.37)])
    checklist = tab("Checklist", [("forklift inspection checklist", 5000, "High", 1.37),
                                  ("daily checklist", 500, "Low", 0.80)])
    inspection = tab("Inspection", [("forklift inspection", 500, "Medium", 3.47),
                                    ("home inspection", 50000, "High", 4.20)])
    workbook = [forklift, checklist, inspection]
    for tables in ([forklift], workbook):
        answer = best("total of the avg. monthly searches for forklift inspection", tables)
        assert execute(tables, answer.sql) == [(500,)], answer.sql
    volume = best("What is the total search volume for forklift inspection checklist", workbook)
    assert execute(workbook, volume.sql) == [(5000,)], volume.sql
    # The screenshot's follow-up paraphrase after the assistant asked whether to total "average
    # monthly searches" must survive the same multi-tab workbook and preserve the exact keyword.
    planner = _hermetic_planner()
    for question in (
        "total of the avg. monthly searches for forklift inspection checklist",
        "total avg monthly searches for forklift inspection checklist",
        "sum of average monthly searches for forklift inspection checklist",
    ):
        served = planner.serve(workbook, question)
        assert served["valid"], (question, served)
        assert served["result"]["rows"] == [[5000]], (question, served["sql"])
    orders = {"name": "orders", "columns": ["City", "Total Amount"],
              "rows": [["Paris", 10], ["Paris", 20], ["Lyon", 5]]}
    paris = best("What is the total amount in Paris?", [orders])
    assert execute([orders], paris.sql) == [(30,)], paris.sql


def _subscription_workbook(extra_si_column=False, exports=None):
    """Three subscription exports with one layout (NT, SI and FF) and a report of each, small enough to total
    by hand: a customer's Stripe workbook, 2026-10-04. ``extra_si_column`` gives SI one more column, and
    ``exports`` (name -> rows) replaces the exports."""
    # A report lists each product once per currency, so no column of it is a key the exports reference.
    exports = exports or {
        "NT": [("price_a", "Neartail - Startup", "usd", 10, "active"),
               ("price_a", "Neartail - Startup", "usd", 10, "canceled"),
               ("price_a", "Neartail - Startup", "eur", 9, "active"),
               ("price_b", "Order Form - Basic", "eur", 7, "active"),
               ("price_b", "Order Form - Basic", "usd", 8, "active")],
        "SI": [("price_c", "Formesign - Pro", "usd", 20, "active"),
               ("price_c", "Formesign - Pro", "usd", 20, "canceled"),
               ("price_c", "Formesign - Pro", "inr", 5, "active"),
               ("price_g", "Formesign - Team", "usd", 40, "active")],
        "FF": [("price_d", "Payment Form - Monthly", "usd", 3, "active"),
               ("price_d", "Payment Form - Monthly", "eur", 4, "active"),
               ("price_e", "Payment Form - Yearly", "usd", 30, "canceled"),
               ("price_e", "Payment Form - Yearly", "usd", 30, "active")],
    }
    tables = []
    for name, rows in exports.items():
        extra = extra_si_column and name == "SI"
        tables.append({"name": name, "columns": ["Plan", "Product", "Currency", "Amount", "Status"] + (["Tax"] if extra else []),
                       "rows": [list(row) + ([1] if extra else []) for row in rows]})
        totals = {}
        for _plan, product, currency, amount, _status in rows:
            count, total = totals.get((product, currency), (0, 0))
            totals[(product, currency)] = (count + 1, total + amount)
        tables.append({"name": f"{name} Report", "columns": ["Product", "Currency", "Subscriptions", "Total Amount"],
                       "rows": [[product, currency, count, total]
                                for (product, currency), (count, total) in sorted(totals.items())]})
    return tables


def test_tabs_of_one_layout_are_read_as_one():
    """A customer's Stripe workbook, 2026-10-04: three subscription exports with one layout and a report of each
    left "What is the total Amount broken down by Plan and Currency?" no reading at all. "total" read as the first
    word of the reports' Total Amount, so the exports' Amount was no mention, and each word's options were cut to
    four across six tabs that no key joins. Copies of one layout are searched as one: the tab the question names,
    else the first holding a value it states, else the first sent (the Sheets add-on sends the active tab first),
    and the answer says which it read."""
    workbook = _subscription_workbook()
    question = "What is the total Amount broken down by Plan and Currency?"
    answer = best(question, workbook)
    assert answer.query.referenced_tables() == {"NT"}, answer.sql
    assert sorted(execute(workbook, answer.sql)) == [("price_a", "eur", 9), ("price_a", "usd", 20), ("price_b", "eur", 7), ("price_b", "usd", 8)], answer.sql
    # Same profile, another copy: the tab the question names, or the one holding the value it states.
    named = best("What is the total Amount in SI by Plan?", workbook)
    assert sorted(execute(workbook, named.sql)) == [("price_c", 45), ("price_g", 40)], named.sql
    held = best("total Amount for Payment Form - Monthly by Currency", workbook)
    assert sorted(execute(workbook, held.sql)) == [("eur", 4), ("usd", 3)], held.sql
    # The served answer names the tab it read, and the others that could answer; not when the question names it.
    planner = _hermetic_planner()
    served = planner.serve(workbook, question)
    assert sorted(map(tuple, served["result"]["rows"])) == [("price_a", "eur", 9), ("price_a", "usd", 20), ("price_b", "eur", 7), ("price_b", "usd", 8)], served["sql"]
    assert served["layout_copies"] == {"read": ["NT"], "others": ["SI", "FF"]}, served.get("layout_copies")
    assert "layout_copies" not in planner.serve(workbook, "What is the total Amount in SI by Plan?")
    # A near copy (SI with one more column) is named too; another table that merely has the column read is not.
    near = planner.serve(_subscription_workbook(extra_si_column=True), question)
    assert set(near["layout_copies"]["others"]) == {"NT", "SI", "FF"} - set(near["layout_copies"]["read"]), near.get("layout_copies")
    customers = {"name": "customers", "columns": ["customer_id", "name", "country"],
                 "rows": [["c1", "Ada", "France"], ["c2", "Lin", "Japan"]]}
    suppliers = {"name": "suppliers", "columns": ["supplier_id", "name", "country"],
                 "rows": [["s1", "Acme", "Germany"], ["s2", "Bolt", "France"]]}
    listed = planner.serve([customers, suppliers], "list the countries")
    assert listed["valid"] and listed["sql"] == 'SELECT "customers"."country" FROM "customers"', listed["sql"]
    assert "layout_copies" not in listed, listed.get("layout_copies")
    # The narrowed graph keeps its parent's value index for the tables it keeps.
    graph = SchemaGraph.from_tables(workbook, [])
    kept = [column for column in graph.columns if column.ref.table not in {"SI", "FF"}]
    assert graph.without(frozenset({"SI", "FF"})).value_index == SchemaGraph(kept, graph.foreign_keys).value_index
    # Negative: tables with one layout that foreign keys reference are different things (two code lookups),
    # and so are lists of one or two columns (test_encoder_role_signal_breaks_ambiguous_column_tie).
    main = {"name": "main", "columns": ["id", "a_code", "b_code"], "rows": [[1, "x", "p"], [2, "y", "p"]]}
    lookups = [{"name": name, "columns": ["code", "description", "active"], "rows": rows}
               for name, rows in (("ref_a", [["x", "Ex", 1], ["y", "Why", 1]]),
                                  ("ref_b", [["p", "Pea", 1], ["q", "Queue", 0]]))]
    assert SchemaGraph.from_tables([main, *lookups], []).layout_copies == (("ref_a", "ref_b"),)
    keyed = SchemaGraph.from_tables([main, *lookups], [("main", "a_code", "ref_a", "code"),
                                                       ("main", "b_code", "ref_b", "code")])
    assert keyed.layout_copies == ()
    lists = [{"name": name, "columns": ["code", "description"], "rows": [["x", "Ex"]]} for name in ("ref_a", "ref_b")]
    assert SchemaGraph.from_tables(lists, []).layout_copies == ()


def test_a_counted_noun_naming_an_unjoined_column_counts_the_rows():
    """The owner's six-tab Stripe workbook (2026-10-05): "How many subscriptions are there by Status?" named the
    report tabs' Subscriptions column, which no key joins to the exports' Status. Every reading grouped by both
    and none could run, so the question had no reading. A projected column the group's table cannot reach is not
    grouped with it and drops out; the completeness check reads the counted noun as the rows counted."""
    workbook = _subscription_workbook()
    question = "How many subscriptions are there by Status?"
    answer = best(question, workbook)
    assert sorted(execute(workbook, answer.sql)) == [("active", 4), ("canceled", 1)], answer.sql
    served = _hermetic_planner().serve(workbook, question)
    assert served["valid"] and sorted(map(tuple, served["result"]["rows"])) == [("active", 4), ("canceled", 1)], served


def test_a_sum_over_rows_its_joins_repeat_is_served_only_when_every_reading_is_one():
    """Subscription exports beside reports that list each product once, in its one currency (2026-10-05):
    discovery keys the exports' Product to a report's, and the best-ranked reading of "What is the total Amount
    broken down by Plan and Currency?" summed the report's Total Amount over that join, once per subscription the
    report row matched: 40 for a product whose subscriptions total 20. A SUM or AVG that reads only rows its joins
    repeat is served only when every eligible reading does, so the exports' own Amount is served."""
    workbook = _subscription_workbook(exports={
        "NT": [("price_a", "Neartail - Startup", "usd", 10, "active"),
               ("price_a", "Neartail - Startup", "usd", 10, "canceled"),
               ("price_b", "Order Form - Basic", "eur", 7, "active")],
        "SI": [("price_c", "Formesign - Pro", "usd", 20, "active"),
               ("price_c", "Formesign - Pro", "usd", 20, "canceled")]})
    question = "What is the total Amount broken down by Plan and Currency?"
    planner = _hermetic_planner()
    selection = _select(planner, question, workbook)
    first = selection.ranking[0]
    assert 'SUM("NT_Report"."Total Amount")' in selection.pool[first].sql and selection.double_counted[first], (
        "the best-ranked eligible reading totals the report once per subscription", selection.pool[first].sql)
    assert not selection.double_counted[selection.selected] and not selection.record()["double_counted"]
    served = planner.serve(workbook, question)
    assert {(row[0], row[1], row[-1]) for row in served["result"]["rows"]} == {
        ("price_a", "usd", 20), ("price_b", "eur", 7)}, served["sql"]

    # Negative: when every eligible reading reads repeats, the best-ranked is served. A course's credits
    # summed over the classes that offer it read the repeats on purpose (Spider train, college_1).
    course = {"name": "course", "columns": ["crs_code", "dept_code", "crs_credit"],
              "rows": [["ACCT-211", "ACCT", 3], ["ACCT-212", "ACCT", 3], ["CIS-220", "CIS", 4]]}
    classes = {"name": "class", "columns": ["class_code", "crs_code"],
               "rows": [[1, "ACCT-211"], [2, "ACCT-211"], [3, "CIS-220"], [4, "ACCT-212"]]}
    member = _model_query(planner, "SELECT course.dept_code, SUM(course.crs_credit) FROM course JOIN class "
                                   "ON course.crs_code = class.crs_code GROUP BY course.dept_code", [course, classes])
    only = _select(planner, "What is the total crs credit by dept code?", [course, classes], searched=[member])
    assert only.double_counted == (True,) and only.selected == 0 and only.record()["double_counted"]


def _keyword_planner_tabs():
    """Three Google Ads Keyword Planner exports, the first with a column its owner added: a customer's Keyword Stats
    workbook (2026-10-05). The values are made up."""
    columns = ["Keyword", "Currency", "Avg. monthly searches", "Three month change", "Competition",
               "Competition (indexed value)", "Top of page bid (low range)", "Top of page bid (high range)",
               "Searches: Sep 2026"]

    def rows(keywords):
        return [[keyword, "USD", volume, "0%", "Low", index, 0.5 + index, 2.0 + index, volume]
                for index, (keyword, volume) in enumerate(keywords)]

    checklist = rows([("fire extinguisher audit checklist", 5000), ("home inspection checklist", 5000),
                      ("balcony inspection checklist", 500)])
    return [{"name": "Checklist", "columns": [columns[0], "shortlist", *columns[1:]],
             "rows": [[row[0], "Yes" if index == 1 else None, *row[1:]] for index, row in enumerate(checklist)]},
            {"name": "Forklift", "columns": columns,
             "rows": rows([("forklift inspection checklist", 500), ("forklift daily checklist", 50)])},
            {"name": "Inspection", "columns": columns,
             "rows": rows([("home inspection", 50000), ("home inspection checklist", 5000), ("roof inspection", 5000)])}]


def test_a_question_no_reading_reads_whole_is_asked_about_the_word_it_could_not_read():
    """A customer's Keyword Stats workbook (2026-10-05): "keyword volume for home inspection checklist" read Keyword and
    the keyword but not "volume". Nothing was served, and the reply asked "Which interpretation should I use?" with
    nothing to choose. The reply names the word and offers the columns of the reading's table it could mean: numeric
    ones for a quantity word, undated before dated copies of a measure."""
    tabs = _keyword_planner_tabs()
    planner = _hermetic_planner()
    served = planner.serve(tabs, "keyword volume for home inspection checklist")
    assert served["clarify"] is True and served["error"] is None and served["dropped"] == ["volume"], served
    assert served["reason"] == ("I couldn't tell which column “volume” means. Did you mean Avg. monthly searches, "
                                "Competition (indexed value), Top of page bid (low range) or Top of page bid (high "
                                "range)?"), served["reason"]
    # Contrast: the column's own name answers.
    named = planner.serve(tabs, "avg. monthly searches for home inspection checklist")
    assert named["result"]["rows"] == [[5000]] and "clarify" not in named, named
    # A word that asks for no number is offered every column the reading does not read.
    trend = planner.serve(tabs, "keyword trend for home inspection checklist")
    assert trend["reason"] == ("I couldn't tell which column “trend” means. Did you mean shortlist, Currency, Avg. "
                               "monthly searches or Three month change?"), trend["reason"]
    # Negative: a reading that reads none of the question's tables, columns or values is not asked about.
    assert planner.serve([PEOPLE], JAPANESE_FRANCE)["error"] == "planner: no executable AST candidate"


def test_the_rewrite_asks_for_low_thinking_and_stops_at_twenty_seconds():
    """The same question with Gemini on (2026-10-05): at the model's default thinking the rewording took 12 to 30 s,
    and in production the 30 s client timeout passed with no answer, a minute after the question. The rewrite asks
    for LOW thinking (6 to 14 s measured) and stops at 20 s; the search reads the rewording."""
    rewording = "What is the Avg. monthly searches for the Keyword 'home inspection checklist'?"
    planner, gemini = _gemini_planner(question=rewording)
    served = planner.serve(_keyword_planner_tabs(), "keyword volume for home inspection checklist")
    assert served["result"]["rows"] == [[5000]] and served["fallback"]["kind"] == "rewrite", served
    (request,) = gemini.requests
    assert request["thinking"] == "LOW" and request["timeout_seconds"] == 20.0, request


def test_total_before_a_measure_reads_the_measure_or_the_whole_name():
    """Near copies of a subscriptions export (SI with one more column) leave SI and NT their own layouts, both with
    Amount, beside reports with Total Amount: "the total Amount" must still read the total of Amount, as it
    totals the reports' Total Amount (2026-10-04: Amount was no mention, and the question had no reading). Where
    the column the question spells whole can answer, it stays the reading."""
    near = _subscription_workbook(extra_si_column=True)
    answer = best("What is the total Amount broken down by Plan and Currency?", near)
    expected = {"NT": [("price_a", "eur", 9), ("price_a", "usd", 20), ("price_b", "eur", 7), ("price_b", "usd", 8)],
                "SI": [("price_c", "inr", 5), ("price_c", "usd", 40), ("price_g", "usd", 40)]}
    (table,) = answer.query.referenced_tables()
    assert table in expected and sorted(execute(near, answer.sql)) == expected[table], answer.sql
    # Contrast: the whole name the question spells answers when it can, and the measure stays in the pool.
    orders = {"name": "orders", "columns": ["Region", "Amount", "Customer"], "rows": [["North", 10, "a"], ["South", 5, "b"]]}
    refunds = {"name": "refunds", "columns": ["Region", "Amount", "Reason"], "rows": [["North", 2, "late"]]}
    summary = {"name": "summary", "columns": ["Region", "Total Amount"], "rows": [["North", 100], ["South", 50]]}
    tables = [orders, refunds, summary]
    pool = SQLSearcher.from_tables(tables, []).search("total amount by region")
    assert sorted(execute(tables, pool[0].sql)) == [("North", 100), ("South", 50)], pool[0].sql
    assert any(candidate.query.referenced_tables() == {"orders"} and 'SUM("orders"."Amount")' in candidate.sql
               for candidate in pool), [candidate.sql for candidate in pool]


def test_serving_preserves_repeated_source_rows_in_aggregates():
    """Repeated identical transactions are separate source observations; ingestion must not turn
    a $300 source total into $100 by dropping two identical-looking payment rows."""
    from engine.tables import normalize_tables

    payments = {"name": "payments", "columns": ["customer", "amount"],
                "rows": [["Ada", 100], ["Ada", 100], ["ada", 100]]}
    normalized = normalize_tables([payments])
    assert len(normalized[0]["rows"]) == 3
    served = _hermetic_planner().serve([payments], "total amount")
    assert served["valid"], served
    assert served["result"]["rows"] == [[300]], served


def test_a_listing_follows_the_order_the_question_names():
    """Spider DEV, 2026-10-02: "the airline names and abbreviations for airlines in the USA" listed the
    abbreviation first, and "the names and birth dates of people" the dates: a column was placed at its last
    mention ("airlines in the USA"), not where the question asks for it (8 listings matched gold but for
    the order). A listing follows the question: a column where its whole name is said, a modifier
    coordinated before a shared word at its own word, another word of its name ("the role")."""
    airlines = {"name": "airlines", "columns": ["uid", "Airline", "Abbreviation", "Country"], "rows": [
        [1, "United Airlines", "UAL", "USA"], [2, "US Airways", "USAir", "USA"], [3, "Air Canada", "ACA", "Canada"]]}
    names = best("What are the airline names and abbreviations for airlines in the USA?", [airlines])
    assert sorted(execute([airlines], names.sql)) == [("US Airways", "USAir"), ("United Airlines", "UAL")], names.sql
    players = {"name": "players", "columns": ["player_id", "first_name", "last_name", "hand"], "rows": [
        [1, "Martina", "Hingis", "R"], [2, "Mirjana", "Lucic", "L"]]}
    full = best("List the first and last name of all players.", [players])
    assert sorted(execute([players], full.sql)) == [("Martina", "Hingis"), ("Mirjana", "Lucic")], full.sql
    # Contrast: a grouped answer lists its group before the aggregate, as a table shows it.
    pets = {"name": "Pets", "columns": ["PetID", "PetType", "pet_age", "weight"], "rows": [
        [2001, "cat", 3, 12.0], [2002, "dog", 2, 13.4], [2003, "dog", 1, 9.3]]}
    average = best("Find the average weight for each pet type.", [pets])
    assert sorted(execute([pets], average.sql)) == [("cat", 12.0), ("dog", 11.350000000000001)], average.sql


def test_by_after_a_participle_names_who_acted():
    """Spider DEV, 2026-10-02: "how many cartoons were written by Joseph Kuhr" counted one group per
    writer and "the number of pets owned by students who are older than 20" one per student: "by" read as a
    grouping cue wherever it stood. After a participle it names who acted ("written by", "owned by"); after
    "grouped" or "broken down" it still names the groups, and "ordered by" sorts."""
    cartoon = {"name": "Cartoon", "columns": ["id", "Title", "Written_by", "Channel"], "rows": [
        [1, "The Rise of the Blue Beetle!", "Michael Jelenic", "700"], [2, "Terror on Dinosaur Island!",
                                                                       "Joseph Kuhr", "701"],
        [3, "Evil Under the Sea!", "Joseph Kuhr", "701"]]}
    written = best("How many cartoons were written by Joseph Kuhr?", [cartoon])
    assert not written.query.group_by and execute([cartoon], written.sql) == [(2,)], written.sql
    # Contrast: "grouped by" names the groups.
    grouped = best("Count the cartoons grouped by channel.", [cartoon])
    channels = sorted(execute([cartoon], grouped.sql))
    assert grouped.query.group_by and channels == [("700", 1), ("701", 2)], grouped.sql


def test_multiple_aggregates_share_a_typed_operand():
    candidate = best("What are the average, minimum and maximum age of people from France?", [PEOPLE])
    assert execute([PEOPLE], candidate.sql) == [(25.0, 20, 30)]
    assert 'AVG("people"."Age")' in candidate.sql
    assert 'MIN("people"."Age")' in candidate.sql
    assert 'MAX("people"."Age")' in candidate.sql


def test_repeated_count_paraphrase_is_one_aggregate():
    candidate = best("Count the number of people from France", [PEOPLE])
    assert candidate.sql.count("COUNT(") == 1
    assert execute([PEOPLE], candidate.sql) == [(2,)]


def test_total_number_of_entities_is_a_scalar_count():
    candidate = best("What is the total number of people?", [PEOPLE])
    assert candidate.sql == 'SELECT COUNT(*) FROM "people"'
    assert execute([PEOPLE], candidate.sql) == [(3,)]


def test_number_used_as_a_column_label_is_not_a_count_request():
    pit_stops = {
        "name": "pitStops",
        "columns": ["driverId", "stop", "duration"],
        "rows": [[1, 1, 20], [1, 2, 18]],
    }
    searcher = SQLSearcher.from_tables([pit_stops], [])
    question = "Find the driver id and stop number of all drivers."
    assert analyze_question(question, searcher.schema).count_requested is False
    ranked = searcher.search(
        question, rank_candidates=False, expand_recursive=False,
        expand_constraints=False, expand_extrema=False,
    )
    assert ranked
    assert "COUNT(" not in ranked[0].sql


def test_abbreviated_number_column_is_not_a_count_request():
    flights = {
        "name": "flights",
        "columns": ["FlightNo", "SourceAirport"],
        "rows": [[101, "APG"], [202, "LAX"]],
    }
    candidate = best("Give the flight numbers of flights leaving from APG", [flights])
    assert candidate.sql == (
        'SELECT "flights"."FlightNo" FROM "flights" '
        'WHERE "flights"."SourceAirport" = \'APG\''
    )
    assert execute([flights], candidate.sql) == [(101,)]


def test_travel_direction_disambiguates_parallel_airport_foreign_keys():
    airports = {
        "name": "airports",
        "columns": ["AirportCode"],
        "rows": [["APG"], ["LAX"]],
    }
    flights = {
        "name": "flights",
        "columns": ["FlightNo", "SourceAirport", "DestAirport"],
        "rows": [[101, "APG", "LAX"], [202, "LAX", "APG"]],
    }
    fks = [
        {"from_table": "flights", "from_col": "SourceAirport",
         "to_table": "airports", "to_col": "AirportCode"},
        {"from_table": "flights", "from_col": "DestAirport",
         "to_table": "airports", "to_col": "AirportCode"},
    ]
    leaving = best("Give the flight numbers of flights leaving from APG", [airports, flights], fks)
    landing = best("Give the flight numbers of flights landing at APG", [airports, flights], fks)
    count = best("Return the number of flights", [airports, flights], fks)
    airport_count = best("Return the number of airports", [airports, flights], fks)
    assert '"flights"."SourceAirport"' in leaving.sql
    assert '"flights"."DestAirport"' not in leaving.sql
    assert '"flights"."DestAirport"' in landing.sql
    assert '"flights"."SourceAirport"' not in landing.sql
    assert execute([airports, flights], leaving.sql) == [(101,)]
    assert execute([airports, flights], landing.sql) == [(202,)]
    assert count.sql == 'SELECT COUNT(*) FROM "flights"'
    assert execute([airports, flights], count.sql) == [(2,)]
    assert airport_count.sql == 'SELECT COUNT(*) FROM "airports"'
    assert execute([airports, flights], airport_count.sql) == [(2,)]


def test_scalar_count_keeps_qualified_one_letter_category_filter():
    matches = {
        "name": "matches",
        "columns": ["winner_name", "winner_hand", "tourney_name"],
        "rows": [
            ["Alice", "L", "WTA Championships"],
            ["Alice", "L", "WTA Championships"],
            ["Beth", "R", "WTA Championships"],
            ["Cara", "L", "Other"],
        ],
    }
    candidate = best(
        "Find the number of left handed winners who participated in the WTA Championships",
        [matches],
    )
    assert '"matches"."winner_hand" = \'L\'' in candidate.sql
    assert 'COUNT(DISTINCT "matches"."winner_name")' in candidate.sql
    assert "GROUP BY" not in candidate.sql
    assert execute([matches], candidate.sql) == [(1,)]


def test_counted_table_beats_related_column_with_same_entity_word():
    documents = {
        "name": "Documents",
        "columns": ["Document_ID", "Template_ID"],
        "rows": [[1, 10], [2, 10], [3, 20]],
    }
    templates = {
        "name": "Templates",
        "columns": ["Template_ID", "Template_Type_Code"],
        "rows": [[10, "PPT"], [20, "PDF"]],
    }
    paragraphs = {
        "name": "Paragraphs",
        "columns": ["Paragraph_ID", "Document_ID"],
        "rows": [[100, 1], [101, 1], [102, 2]],
    }
    fks = [
        {"from_table": "Documents", "from_col": "Template_ID",
         "to_table": "Templates", "to_col": "Template_ID"},
        {"from_table": "Paragraphs", "from_col": "Document_ID",
         "to_table": "Documents", "to_col": "Document_ID"},
    ]
    candidate = best(
        "How many documents are using the template with type code PPT?",
        [documents, templates, paragraphs],
        fks,
    )
    # The documents are counted: their rows, or a column of theirs (Documents.Template_ID is a mention
    # only where "template id" is said, test_a_key_named_by_the_table_it_references_needs_its_whole_name).
    assert ('COUNT("Documents".' in candidate.sql
            or candidate.sql.startswith('SELECT COUNT(*) FROM "Documents" JOIN "Templates"')), candidate.sql
    assert 'JOIN "Paragraphs"' not in candidate.sql
    assert execute([documents, templates, paragraphs], candidate.sql) == [(2,)]


def test_arbitrary_word_does_not_become_a_category_initial_filter():
    records = {
        "name": "records",
        "columns": ["status"],
        "rows": [["A"], ["I"]],
    }
    candidate = best("List all statuses", [records])
    assert "WHERE" not in candidate.sql
    assert set(execute([records], candidate.sql)) == {("A",), ("I",)}


def test_ranker_prefers_count_distinct_over_grouped_count():
    candidate = best("Find the number of distinct type of pets", [PETS])
    assert candidate.sql == 'SELECT COUNT(DISTINCT "Pets"."PetType") FROM "Pets"'
    assert execute([PETS], candidate.sql) == [(2,)]


def test_ranker_coordinates_multiple_aggregate_operands():
    candidate = best("Find the average and maximum age for each type of pet", [PETS])
    assert 'AVG("Pets"."pet_age")' in candidate.sql
    assert 'MAX("Pets"."pet_age")' in candidate.sql
    assert 'MAX("Pets"."PetType")' not in candidate.sql


def test_multi_hop_join_uses_bridge_table():
    candidate = best("show customer names and item prices", [CUSTOMERS, ORDERS, ITEMS], COMMERCE_FKS)
    assert candidate.sql.count(" JOIN ") == 2
    assert 'JOIN "orders"' in candidate.sql
    assert execute([CUSTOMERS, ORDERS, ITEMS], candidate.sql) == [
        ("Alice", 8), ("Alice", 12), ("Bob", 30),
    ]


def test_grouped_count_uses_entity_display_column():
    candidate = best("For each stadium, how many concerts play there?", [STADIUM, CONCERT], STADIUM_FKS)
    assert 'GROUP BY "stadium"."Name"' in candidate.sql
    assert "COUNT(*)" in candidate.sql
    assert execute([STADIUM, CONCERT], candidate.sql) == [("Alpha", 2), ("Beta", 1)]


def test_filter_column_does_not_leak_into_projection():
    candidate = best("show stadium names with capacity between 5000 and 10000", [STADIUM])
    assert candidate.sql.startswith('SELECT "stadium"."Name" FROM')
    assert execute([STADIUM], candidate.sql) == [("Alpha",), ("Beta",)]


def test_order_column_does_not_leak_into_projection():
    candidate = best("show people names ordered by age descending", [PEOPLE])
    assert candidate.sql.startswith('SELECT "people"."Name" FROM')
    assert execute([PEOPLE], candidate.sql) == [("Cara",), ("Alice",), ("Bob",)]


def test_literals_are_escaped_by_renderer():
    authors = {"name": "authors", "columns": ["Name"], "rows": [["O'Reilly"], ["Elsevier"]]}
    candidate = best("show names for O'Reilly", [authors])
    assert "'O''Reilly'" in candidate.sql
    assert execute([authors], candidate.sql) == [("O'Reilly",)]


def test_grouped_topn_orders_by_aggregate_across_bridge():
    candidate = best("show top 2 customer names by total item price", [CUSTOMERS, ORDERS, ITEMS], COMMERCE_FKS)
    assert 'GROUP BY "customers"."Name"' in candidate.sql
    assert 'ORDER BY SUM("items"."Price") DESC LIMIT 2' in candidate.sql
    assert execute([CUSTOMERS, ORDERS, ITEMS], candidate.sql) == [("Bob", 30), ("Alice", 20)]


def test_ranked_units_sold_sums_the_quantity_measure():
    # "units sold" is an implicit SUM over the quantity column, never a raw
    # ORDER BY over whichever numeric column shares a token ("unit_price").
    candidate = best("top 2 products by units sold", RETAIL, RETAIL_FKS)
    assert 'SUM("purchase_items"."quantity")' in candidate.sql
    assert 'GROUP BY "products"."product_name"' in candidate.sql
    assert 'ORDER BY SUM("purchase_items"."quantity") DESC LIMIT 2' in candidate.sql
    assert execute(RETAIL, candidate.sql) == [("Beta", 9), ("Alpha", 8)]


def test_ranked_revenue_sums_the_amount_measure():
    candidate = best("top 2 products by revenue", RETAIL, RETAIL_FKS)
    assert 'SUM("purchase_items"."line_total")' in candidate.sql
    assert 'GROUP BY "products"."product_name"' in candidate.sql
    assert execute(RETAIL, candidate.sql) == [("Beta", 180), ("Gamma", 90)]


def test_unit_price_ranking_stays_a_raw_rate_order():
    # Same profile as the units-sold family, but "unit price" is a rate
    # qualifier: ranking stays a raw ORDER BY without any aggregation.
    candidate = best("top 2 products by unit price", RETAIL, RETAIL_FKS)
    assert "SUM(" not in candidate.sql
    assert 'ORDER BY "purchase_items"."unit_price" DESC LIMIT 2' in candidate.sql


def test_total_spend_binds_the_amount_measure_deterministically():
    # Without measure vocabulary the SUM target fell back to an arbitrary
    # numeric-column tie; "spend" must bind the extended-amount column.
    candidate = best("top 2 shoppers by total spend", RETAIL, RETAIL_FKS)
    assert 'SUM("purchase_items"."line_total")' in candidate.sql
    assert execute(RETAIL, candidate.sql) == [("Bob", 180), ("Alice", 170)]


def test_explicit_total_quantity_sold_phrasing_is_unchanged():
    candidate = best("top 2 product names by total quantity sold", RETAIL, RETAIL_FKS)
    assert 'SUM("purchase_items"."quantity")' in candidate.sql
    assert 'ORDER BY SUM("purchase_items"."quantity") DESC LIMIT 2' in candidate.sql
    assert execute(RETAIL, candidate.sql) == [("Beta", 9), ("Alpha", 8)]


SALES_LEDGER = {
    "name": "sales",
    "columns": ["order_id", "customer", "city", "currency", "amount"],
    "rows": [
        [101, "Sherlock Holmes", "London", "GBP", 118],
        [102, "Sherlock Holmes", "London", "GBP", 95],
        [104, "Dr. John Watson", "London", "GBP", 340],
        [109, "Inspector Clouseau", "Paris", "EUR", 310],
        [121, "Arsene Lupin", "Paris", "EUR", 180],
    ],
}


def test_money_noun_naming_the_table_reads_as_its_money_total():
    # "sales" names the table AND is a money noun. Asked as a quantity it is the money total, the
    # way "total amount" is: the Sheets add-on's "Whats the sales in france" counted rows (5)
    # instead of summing amount (970). The count and listing phrasings below keep the entity.
    candidate = best("What's the sales in London", [SALES_LEDGER])
    assert 'SUM("sales"."amount")' in candidate.sql, candidate.sql
    assert execute([SALES_LEDGER], candidate.sql) == [(553,)]


def test_counting_or_listing_a_money_named_table_keeps_the_entity():
    counted = best("how many sales in London", [SALES_LEDGER])
    assert "COUNT(" in counted.sql and "SUM(" not in counted.sql, counted.sql
    assert execute([SALES_LEDGER], counted.sql) == [(3,)]
    listed = best("list the sales in London", [SALES_LEDGER])
    assert "SUM(" not in listed.sql and "COUNT(" not in listed.sql, listed.sql


def test_money_named_table_without_a_money_column_keeps_the_entity():
    # Same profile, but nothing in the table carries money vocabulary: there is no money total to
    # read, so the question keeps its current interpretation instead of summing a quantity.
    units = {"name": "sales", "columns": ["sale_id", "city", "units"],
             "rows": [[1, "London", 3], [2, "Paris", 4]]}
    candidate = best("What's the sales in London", [units])
    assert "SUM(" not in candidate.sql, candidate.sql


def test_served_selection_contract_keeps_money_totals_and_converted_totals():
    """select_query serves the best-ranked member that aggregates the money column when the rule
    fires, so a ranking that puts a listing first cannot turn "what's the sales in London" back
    into rows. A converted total (SUM(amount * rate)) satisfies the contract, so a currency
    intent's choice is never displaced."""
    from engine.sql_ast import Aggregate, BinaryExpr, ColumnRef, SelectItem, SelectQuery
    from engine.sql_expansion import aggregates_money_column, money_total_columns

    sch = [{"table": "sales", "name": name, "affinity": affinity}
           for name, affinity in (("order_id", "INTEGER"), ("city", "TEXT"), ("amount", "REAL"))]
    assert money_total_columns("What's the sales in London", sch) == ("sales", [sch[2]])
    assert money_total_columns("how many sales in London", sch) is None
    assert money_total_columns("list the sales in London", sch) is None
    assert money_total_columns("What's the sales in London",
                               [dict(entry, table="orders") for entry in sch]) is None
    summed = best("What's the sales in London", [SALES_LEDGER]).query
    listed = best("list the sales in London", [SALES_LEDGER]).query
    assert aggregates_money_column(summed, "sales", ["amount"])
    assert not aggregates_money_column(listed, "sales", ["amount"])
    converted = SelectQuery(select=(SelectItem(Aggregate("SUM", BinaryExpr(
        ColumnRef("sales", "amount"), "*", ColumnRef("rates", "rate")))),), from_table="sales")
    assert aggregates_money_column(converted, "sales", ["amount"])

    # At the selection owner: the listing ranked first gives way to the money total ranked second.
    planner = _hermetic_planner()
    pool = [ScoredQuery(query, render_query(query), 0.0, ()) for query in (listed, summed)]
    selection = _select(planner, "What's the sales in London", [SALES_LEDGER], searched=pool)
    assert selection.ranking == (0, 1) and selection.money_total == (False, True)
    assert selection.selected == 1 and selection.record()["money_total"] is True
    listing = _select(planner, "list the sales in London", [SALES_LEDGER], searched=pool)
    assert listing.selected == 0 and listing.money_total == (False, False), "the rule did not fire"


def test_world_path_money_noun_naming_the_table_sums_its_money_column():
    """The world-join operand choice (EncoderQuery.read_op_all) follows the same rule. The intent
    head's operator reading is stubbed: it read COUNT for "Whats the sales in france" and SUM for
    "total sales in France", and both used to return COUNT(sales) through the table-noun rule."""
    import numpy as np

    from engine.encoder_overlay import EncoderQuery

    def reader(op):
        query = EncoderQuery.__new__(EncoderQuery)
        query.ingest = lambda tables: (tables, [])
        query.read_op_model = lambda norm, question, fks: (op, None)
        query._encode = lambda texts: np.eye(len(texts), 8, dtype=np.float32)
        return query

    ledger = [{"table": "sales", "name": name, "affinity": affinity} for name, affinity in (
        ("order ID", "INTEGER"), ("customer", "TEXT"), ("city", "TEXT"), ("currency", "TEXT"),
        ("amount", "REAL"))]
    assert reader("COUNT").read_op_all("Whats the sales in france", ledger) == ("SUM", "sales", "amount")
    assert reader("SUM").read_op_all("total sales in France", ledger) == ("SUM", "sales", "amount")
    assert reader("AVG").read_op_all("average sales in France", ledger) == ("AVG", "sales", "amount")
    # The head reads no aggregate for "What's the sales in London"; the money noun still asks for the
    # total, while a listing command keeps the rows.
    assert reader(None).read_op_all("What's the sales in London", ledger) == ("SUM", "sales", "amount")
    assert reader(None).read_op_all("show the sales in London", ledger) is None
    # Contrasts: a count cue keeps the count; an entity noun still counts its rows; a money-named
    # table with nothing monetary to sum keeps the table-noun count.
    assert reader("COUNT").read_op_all("how many sales in France", ledger) == ("COUNT", "sales", None)
    assert reader("COUNT").read_op_all("number of sales in France", ledger) == ("COUNT", "sales", None)
    customers = [{"table": "customers", "name": "name", "affinity": "TEXT"},
                 {"table": "customers", "name": "amount", "affinity": "REAL"}]
    assert reader("SUM").read_op_all("total customers in France", customers) == ("COUNT", "customers", None)
    units = [{"table": "sales", "name": "city", "affinity": "TEXT"},
             {"table": "sales", "name": "units", "affinity": "INTEGER"}]
    assert reader("SUM").read_op_all("total sales in France", units) == ("COUNT", "sales", None)


def test_world_path_reads_quantity_superlatives_and_counted_measures():
    """Chrome exploration, 2026-10-01/02: "which country has the most deposits" read no aggregate, so the
    bank sheet's world path never ran and the question was declined; "how many transfers in Canada" counted
    the hospital rows (1) where the transfers column holds the 8 transfers. The world operand reader
    (EncoderQuery.read_op_all) reads a quantity superlative as the top total of the column it names, or as
    the row count of the sheet it names, and a count of what a numeric column already counts as that
    column's total. The intent head's operator reading is stubbed, as the production head read it."""
    import numpy as np

    from engine.encoder_overlay import EncoderQuery

    def reader(op):
        query = EncoderQuery.__new__(EncoderQuery)
        query.ingest = lambda tables: (tables, [])
        query.read_op_model = lambda norm, question, fks: (op, None)
        query._encode = lambda texts: np.eye(len(texts), 8, dtype=np.float32)
        return query

    deposits = [{"table": "deposits", "name": name, "affinity": affinity} for name, affinity in (
        ("bank", "TEXT"), ("account manager", "TEXT"), ("deposits", "INTEGER"))]
    catering = [{"table": "catering", "name": name, "affinity": affinity} for name, affinity in (
        ("restaurant", "TEXT"), ("event", "TEXT"), ("amount", "INTEGER"))]
    transfers = [{"table": "transfers", "name": name, "affinity": affinity} for name, affinity in (
        ("hospital", "TEXT"), ("signed", "TEXT"), ("transfers", "INTEGER"))]
    # Positive: the column the superlative names, whichever operator the head read ("fewest" reads a count).
    assert reader(None).read_op_all("which country has the most deposits?", deposits) == (
        "SUM", "deposits", "deposits")
    assert reader("COUNT").read_op_all("which country has the fewest deposits?", deposits) == (
        "SUM", "deposits", "deposits")
    # A money verb names the money column; the rows the superlative names are counted.
    assert reader(None).read_op_all("which country spent the most on catering?", catering) == (
        "SUM", "catering", "amount")
    assert reader(None).read_op_all("which country has the most banks?", deposits) == ("COUNT", "deposits", None)
    # The transfers column already counts transfers.
    assert reader("COUNT").read_op_all("how many transfers in Canada?", transfers) == (
        "SUM", "transfers", "transfers")
    assert reader("COUNT").read_op_all("number of transfers in Canada", transfers) == (
        "SUM", "transfers", "transfers")
    # Contrastive, same sheet: counting the hospitals counts rows; an explicit total is unchanged.
    assert reader("COUNT").read_op_all("how many hospitals in Canada?", transfers) == ("COUNT", "transfers", None)
    assert reader("SUM").read_op_all("total transfers in Canada", transfers) == ("SUM", "transfers", "transfers")
    # Negative: "the highest deposits" may be the largest single value, and a superlative of a quality
    # names no measure, so neither is read as a total.
    assert reader(None).read_op_all("which country has the highest deposits?", deposits) is None
    assert reader(None).read_op_all("which bank has the most recent deposits?", deposits) is None


def test_operator_readout_never_reads_an_aggregate_off_a_closed_class_word():
    """Production, 2026-09-27: 'who ordered a trench coat in France' was answered with COUNT = 5. The
    COUNT threshold is 0.05, and the article 'a' read 0.15 (the real cue 'many' reads 0.92), so a listing
    question became a count. Closed-class words carry grammar; the readout skips them as it skips column
    names ('ordered' read 0.22) and cell values. The activations are the production readout's."""

    import numpy as np

    from engine.encoder_overlay import EncoderQuery

    activation = {"ordered": 0.22, "a": 0.15, "many": 0.92}
    query = EncoderQuery.__new__(EncoderQuery)
    query.sid = {"intent_agg_count": 0, "intent_agg_sum": 1, "intent_agg_avg": 2}
    query.thr = {"intent_agg_count": 0.05, "intent_agg_sum": 0.3, "intent_agg_avg": 0.3}

    def question_readout(_tables, _fks, text):
        toks = text.split()
        final = np.zeros((len(toks), 3), np.float32)
        for index, token in enumerate(toks):
            final[index][0] = activation.get(token.lower(), 0.0)
        return final, 0, toks, [token.lower() for token in toks]

    query._question_readout = question_readout
    sales = {"name": "sales", "columns": ["customer", "ordered"], "rows": [["Clouseau", "Gabardine Trench Coat"]]}
    with patch("engine.closed_class.closed_class_words", return_value=frozenset({"who", "a", "in", "how"})):
        assert query.read_op_model([sales], "who ordered a trench coat in France", [])[0] is None
        assert query.read_op_model([sales], "how many orders in France", [])[0] == "COUNT"


def test_one_surrogate_key_rule_names_keys_not_measures_or_codes():
    # Eleven copies of the surrogate-key test disagreed (2026-09-27): compose missed 'order ID' and summed
    # order numbers, the ranker's regex called 'paid' a key, and only some copies counted codes. The planner,
    # the ranker's features, compose, the world path and the deterministic lowering now read one rule.
    import importlib

    from engine.sql_schema import is_surrogate_key

    for name in ("order ID", "customer_id", "OrderID", "PetID", "uid", "customer_key", "Identifier", "index"):
        assert is_surrogate_key(name), name
    # Contrastive: 'orders' and 'idea' share letters with 'id', 'paid' ends in them, 'price index' is a
    # measure, and a code is a natural attribute people ask for by name.
    for name in ("orders", "idea", "paid", "valid", "price index", "country_code", "Code", "keyboard"):
        assert not is_surrogate_key(name), name
    assert not is_surrogate_key("amount")                       # a measure
    for module in ("engine.sql_search", "engine.sql_rank", "engine.sql_recursive", "engine.sql_expansion",
                   "engine.sql_extrema", "engine.compose", "engine.encoder_overlay", "engine.knowledge_query",
                   "engine.knowledge_tables", "engine.deterministic.lower"):
        assert importlib.import_module(module).is_surrogate_key is is_surrogate_key, module


def test_literal_measure_column_keeps_raw_interpretation():
    # A schema that names its own "revenue" column keeps the raw reading; the
    # implicit measure fires only when the vocabulary has no direct column.
    stores = {
        "name": "stores",
        "columns": ["store_id", "store_name", "revenue"],
        "rows": [[1, "North", 100], [2, "South", 200]],
    }
    candidate = best("store with the highest revenue", [stores])
    assert "SUM(" not in candidate.sql
    assert 'ORDER BY "stores"."revenue" DESC LIMIT 1' in candidate.sql


def test_search_is_deterministic():
    searcher = SQLSearcher.from_tables([CUSTOMERS, ORDERS, ITEMS], COMMERCE_FKS)
    first = [(c.sql, c.score) for c in searcher.search("show customer names and item prices")]
    second = [(c.sql, c.score) for c in searcher.search("show customer names and item prices")]
    assert first == second


def test_encoder_role_signal_breaks_ambiguous_column_tie():
    customers = {"name": "customers", "columns": ["Name"], "rows": [["Alice"]]}
    orders = {"name": "orders", "columns": ["Name"], "rows": [["First order"]]}
    signals = SemanticSignals(
        {"projection": {("customers", "Name"): 0.1, ("orders", "Name"): 0.9}},
        {},
    )
    candidates = SQLSearcher.from_tables([customers, orders], []).search(
        "show names", semantic_signals=signals,
    )
    assert candidates[0].sql == 'SELECT "orders"."Name" FROM "orders"'
    assert any(name == "model_projection" for name, _ in candidates[0].features)


def test_profile_beam_expands_missing_projection_binding():
    schema = SQLSearcher.from_tables([PEOPLE], []).schema
    age = next(column.ref for column in schema.columns if column.ref.name == "Age")
    target = SelectQuery((SelectItem(age),), "people")
    signals = SemanticSignals(
        {"projection": {("people", "Age"): 1.0, ("people", "Name"): 0.1}},
        {"people": 1.0},
        (profile_query(target).sketch_map,),
    )
    candidates = SQLSearcher(schema, max_candidates=25).search(
        "show people details", semantic_signals=signals, expand_recursive=False,
        expand_constraints=False, expand_extrema=False,
        profile_config=ProfileSearchConfig(),
    )
    assert any(candidate.query == target for candidate in candidates)
    assert any("profile-expand:1" in candidate.evidence for candidate in candidates)


def test_profile_beam_instantiates_grouped_frequency_shape():
    schema = SQLSearcher.from_tables([PEOPLE], []).schema
    name = next(column.ref for column in schema.columns if column.ref.name == "Name")
    desired = SelectQuery(
        (SelectItem(name), SelectItem(Aggregate("COUNT", Star()))),
        "people",
        group_by=(name,),
        order_by=(OrderTerm(Aggregate("COUNT", Star()), "DESC"),),
        limit=1,
    )
    profile = profile_query(desired).sketch_map
    signals = SemanticSignals(
        {
            "projection": {("people", "Name"): 1.0},
            "aggregate": {("people", "Age"): 0.8},
            "group": {("people", "Name"): 1.0},
            "order": {("people", "Age"): 0.7},
        },
        {"people": 1.0},
        (profile,),
    )
    candidates = SQLSearcher(schema, max_candidates=40).search(
        "show people details", semantic_signals=signals, expand_recursive=False,
        expand_constraints=False, expand_extrema=False,
        profile_config=ProfileSearchConfig(),
    )
    expanded = [candidate for candidate in candidates if "profile-expand:1" in candidate.evidence]
    assert expanded
    assert all(profile_query(candidate.query).sketch_map == profile for candidate in expanded)
    assert any(candidate.query == desired for candidate in expanded)


def test_profile_expansion_caps_variants_and_penalizes_transformation():
    schema = SQLSearcher.from_tables([PEOPLE], []).schema
    name = next(column.ref for column in schema.columns if column.ref.name == "Name")
    age = next(column.ref for column in schema.columns if column.ref.name == "Age")
    base_query = SelectQuery((SelectItem(name),), "people")
    scaffold = ScoredQuery(base_query, render_query(base_query), 10.0, ("base",))
    signals = SemanticSignals(
        {"projection": {("people", "Age"): 1.0, ("people", "Name"): 0.5}},
        {"people": 1.0},
        (profile_query(SelectQuery((SelectItem(age),), "people")).sketch_map,),
    )
    expanded = ProfileQueryExpander(
        schema, signals, max_candidates=2, per_profile=2, generation_penalty=4.0,
        binding_quality_weight=2.0,
    ).expand("show people details", [scaffold])
    assert 0 < len(expanded) <= 2
    assert all(candidate.score <= scaffold.score - 2.0 for candidate in expanded)
    assert all("profile_binding_quality" in dict(candidate.features) for candidate in expanded)
    best_quality = max(dict(candidate.features)["profile_binding_quality"] for candidate in expanded)
    best_score = max(candidate.score for candidate in expanded)
    assert best_score == scaffold.score - 4.0 + 2.0 * best_quality


def test_profile_expansion_preserves_hand_ranked_fallback_top():
    searcher = SQLSearcher.from_tables([PEOPLE], [], max_candidates=25)
    baseline = searcher.search("show people details")
    age = next(column.ref for column in searcher.schema.columns if column.ref.name == "Age")
    signals = SemanticSignals(
        {"projection": {("people", "Age"): 1.0}},
        {"people": 1.0},
        (profile_query(SelectQuery((SelectItem(age),), "people")).sketch_map,),
    )
    expanded = searcher.search(
        "show people details", semantic_signals=signals,
        profile_config=ProfileSearchConfig(),
    )
    assert expanded[0].sql == baseline[0].sql
    assert "profile:fallback-top" in expanded[0].evidence


def test_profile_fallback_applies_when_no_compatible_variant_exists():
    searcher = SQLSearcher.from_tables([PEOPLE], [], max_candidates=25)
    baseline = searcher.search("show people details")
    impossible = profile_query(SetQuery(
        SelectQuery((SelectItem(next(iter(searcher.schema.columns)).ref),), "people"),
        "UNION",
        SelectQuery((SelectItem(next(iter(searcher.schema.columns)).ref),), "people"),
    )).sketch_map
    signals = SemanticSignals({"projection": {}}, {"people": 1.0}, (impossible,))
    expanded = searcher.search(
        "show people details", semantic_signals=signals,
        profile_config=ProfileSearchConfig(),
    )
    assert expanded[0].sql == baseline[0].sql
    assert "profile:fallback-top" in expanded[0].evidence



def test_profile_generation_requires_explicit_configuration():
    searcher = SQLSearcher.from_tables([PEOPLE], [], max_candidates=25)
    age = next(column.ref for column in searcher.schema.columns if column.ref.name == "Age")
    signals = SemanticSignals(
        {"projection": {("people", "Age"): 1.0}},
        {"people": 1.0},
        (profile_query(SelectQuery((SelectItem(age),), "people")).sketch_map,),
    )
    roles_only = searcher.search("show people details", semantic_signals=signals)
    expanded = searcher.search(
        "show people details",
        semantic_signals=signals,
        profile_config=ProfileSearchConfig(),
    )
    assert not any("profile-expand:" in evidence for candidate in roles_only
                   for evidence in candidate.evidence)
    assert any("profile-expand:" in evidence for candidate in expanded
               for evidence in candidate.evidence)



def test_soft_prediction_budget_does_not_abandon_work():
    import time

    before = threading.active_count()
    value, error, elapsed, over_budget = run_with_budget(
        lambda: (time.sleep(0.01), "done")[1], budget=0.001
    )
    assert value == "done" and error is None and elapsed >= 0.01 and over_budget
    assert threading.active_count() == before


def test_recursive_ast_scalar_subquery_executes():
    age = ColumnRef("people", "Age", SQLType.INTEGER)
    name = ColumnRef("people", "Name", SQLType.TEXT)
    average = SelectQuery((SelectItem(Aggregate("AVG", age)),), "people")
    query = SelectQuery(
        (SelectItem(name),),
        "people",
        where=Comparison(age, ">", ScalarSubquery(average)),
    )
    assert execute([PEOPLE], render_query(query)) == [("Cara",)]


def test_recursive_ast_correlated_exists_executes():
    customer_id = ColumnRef("customers", "Customer_ID", SQLType.INTEGER)
    order_customer_id = ColumnRef("orders", "Customer_ID", SQLType.INTEGER)
    subquery = SelectQuery(
        (SelectItem(Star()),),
        "orders",
        where=Comparison(order_customer_id, "=", customer_id),
    )
    query = SelectQuery(
        (SelectItem(ColumnRef("customers", "Name", SQLType.TEXT)),),
        "customers",
        where=ExistsPredicate(subquery),
    )
    assert execute([CUSTOMERS, ORDERS], render_query(query)) == [("Alice",), ("Bob",)]


def test_recursive_ast_set_query_in_derived_table_executes():
    country = ColumnRef("people", "Country", SQLType.TEXT)
    age = ColumnRef("people", "Age", SQLType.INTEGER)
    older = SelectQuery(
        (SelectItem(country),), "people", where=Comparison(age, ">", Literal(25, SQLType.INTEGER))
    )
    younger = SelectQuery(
        (SelectItem(country),), "people", where=Comparison(age, "<", Literal(35, SQLType.INTEGER))
    )
    query = SelectQuery(
        (SelectItem(Aggregate("COUNT", Star())),),
        SubquerySource(SetQuery(older, "INTERSECT", younger), "matches"),
    )
    assert execute([PEOPLE], render_query(query)) == [(1,)]


def test_recursive_expansion_searches_scalar_average():
    candidate = best("Show names of people older than the average age", [PEOPLE])
    assert "(SELECT AVG(" in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [("Cara",)]


def test_recursive_expansion_searches_anti_membership():
    candidate = SQLSearcher.from_tables([STADIUM, CONCERT], STADIUM_FKS).search(
        "Show the stadium names without any concert", expand_extrema=False
    )[0]
    assert " NOT IN (SELECT " in candidate.sql
    assert execute([STADIUM, CONCERT], candidate.sql) == [("Gamma",)]


def test_recursive_expansion_searches_route_self_join():
    candidate = best(
        "How many flights depart from City Aberdeen and have destination City Ashley?",
        [AIRPORTS, FLIGHTS],
        FLIGHT_FKS,
    )
    assert candidate.sql.count('JOIN "airports"') == 2
    assert 'AS "source"' in candidate.sql and 'AS "destination"' in candidate.sql
    assert execute([AIRPORTS, FLIGHTS], candidate.sql) == [(1,)]


def test_recursive_expansion_searches_nested_count_aggregate():
    students = {
        "name": "students",
        "columns": ["Student_ID", "Name"],
        "rows": [[1, "Alice"], [2, "Bob"], [3, "Cara"]],
    }
    pets = {
        "name": "pets",
        "columns": ["Pet_ID", "Student_ID"],
        "rows": [[1, 1], [2, 1], [3, 2]],
    }
    fks = [
        {"from_table": "pets", "from_col": "Student_ID", "to_table": "students", "to_col": "Student_ID"},
    ]
    candidate = best("What is the average number of pets per student?", [students, pets], fks)
    assert 'AVG("counts"."value_count")' in candidate.sql
    assert "FROM (SELECT" in candidate.sql
    assert "LEFT JOIN" in candidate.sql
    assert 'COUNT("pets"."Student_ID")' in candidate.sql
    assert execute([students, pets], candidate.sql) == [(1.0,)]


def test_recursive_set_expansion_keeps_every_categorical_alternative():
    people = {
        "name": "people",
        "columns": ["Name", "Country"],
        "rows": [["A", "France"], ["B", "Spain"], ["C", "Italy"]],
    }
    candidates = SQLSearcher.from_tables([people], [], max_candidates=80).search(
        "List people in France, Spain, or Italy",
        expand_constraints=False,
        expand_extrema=False,
    )
    candidate = next(candidate for candidate in candidates if " UNION " in candidate.sql)
    assert candidate.sql.count("SELECT") == 3
    assert '"people"."Country" = \'Italy\'' in candidate.sql
    assert " AND " not in candidate.sql
    assert execute([people], candidate.sql) == [("A",), ("B",), ("C",)]


def test_constraint_expansion_searches_cross_table_count_having():
    candidate = best(
        "Show stadium names that have more than one concert",
        [STADIUM, CONCERT],
        STADIUM_FKS,
    )
    assert 'GROUP BY "stadium"."Name"' in candidate.sql
    assert "HAVING COUNT(*) > 1" in candidate.sql
    assert execute([STADIUM, CONCERT], candidate.sql) == [("Alpha",)]


def test_constraint_expansion_searches_single_table_count_having():
    candidate = best("List countries having at least two people", [PEOPLE])
    assert 'GROUP BY "people"."Country"' in candidate.sql
    assert "HAVING COUNT(*) >= 2" in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [("France",)]


def test_constraint_expansion_searches_disjunction():
    candidate = best(
        "How many people are from France or have age greater than 35?",
        [PEOPLE],
    )
    assert '"people"."Country" = \'France\' OR "people"."Age" > 35' in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [(3,)]


def test_constraint_expansion_disjoins_every_repeated_column_group():
    people = {
        "name": "people",
        "columns": ["Name", "Country", "Job"],
        "rows": [
            ["A", "France", "engineer"],
            ["B", "Spain", "doctor"],
            ["C", "France", "teacher"],
            ["D", "Italy", "engineer"],
        ],
    }
    candidates = SQLSearcher.from_tables([people], [], max_candidates=80).search(
        "List people in France or Spain who are engineers or doctors"
    )
    candidate = candidates[0]
    assert "Country\" = 'France' OR \"people\".\"Country\" = 'Spain'" in candidate.sql
    assert "Job\" = 'engineer' OR \"people\".\"Job\" = 'doctor'" in candidate.sql
    assert execute([people], candidate.sql) == [("A",), ("B",)]
    assert all(" UNION " not in item.sql for item in candidates)


def test_constraint_disjunction_deduplicates_entities_across_relation():
    customers = {
        "name": "customers",
        "columns": ["Customer_ID", "Name"],
        "rows": [[1, "Alice"], [2, "Bob"]],
    }
    orders = {
        "name": "orders",
        "columns": ["Order_ID", "Customer_ID", "Type"],
        "rows": [[1, 1, "cat"], [2, 1, "dog"], [3, 2, "bird"]],
    }
    fks = [
        {"from_table": "orders", "from_col": "Customer_ID", "to_table": "customers", "to_col": "Customer_ID"},
    ]
    candidate = best("Show customer names with order type cat or dog", [customers, orders], fks)
    assert candidate.sql.startswith('SELECT DISTINCT "customers"."Name"')
    assert execute([customers, orders], candidate.sql) == [("Alice",)]


def test_constraint_disjunction_preserves_shared_official_filter():
    countries = {
        "name": "country",
        "columns": ["Code", "Name"],
        "rows": [["A", "Alpha"], ["B", "Beta"], ["C", "Gamma"]],
    }
    languages = {
        "name": "countrylanguage",
        "columns": ["CountryCode", "Language", "IsOfficial"],
        "rows": [["A", "English", "T"], ["B", "Dutch", "T"], ["C", "English", "F"]],
    }
    fks = [
        {"from_table": "countrylanguage", "from_col": "CountryCode", "to_table": "country", "to_col": "Code"},
    ]
    candidate = best(
        "What are the country names where either English or Dutch is the official language?",
        [countries, languages],
        fks,
    )
    assert '"countrylanguage"."IsOfficial" = \'T\'' in candidate.sql
    assert execute([countries, languages], candidate.sql) == [("Alpha",), ("Beta",)]


def test_constraint_expansion_searches_filtered_scalar_minimum():
    cars = {
        "name": "cars_data",
        "columns": ["Id", "Horsepower", "Cylinders"],
        "rows": [[1, 10, 2], [2, 20, 3], [3, 30, 4]],
    }
    names = {
        "name": "car_names",
        "columns": ["MakeId", "Make"],
        "rows": [[1, "A"], [2, "B"], [3, "C"]],
    }
    fks = [
        {"from_table": "car_names", "from_col": "MakeId", "to_table": "cars_data", "to_col": "Id"},
    ]
    candidate = best(
        "Among the cars with more than lowest horsepower, which ones do not have more "
        "than 3 cylinders? List the car makeid and make name.",
        [cars, names],
        fks,
    )
    assert candidate.sql.startswith('SELECT "car_names"."MakeId", "car_names"."Make"')
    assert '"cars_data"."Cylinders" <= 3' in candidate.sql
    assert '(SELECT MIN("cars_data"."Horsepower") FROM "cars_data")' in candidate.sql
    assert execute([cars, names], candidate.sql) == [(2, "B")]


def test_constraint_expansion_keeps_grouped_superlative_as_aggregate():
    candidate = best("What is the maximum age for all the different countries?", [PEOPLE])
    assert 'MAX("people"."Age")' in candidate.sql
    assert 'GROUP BY "people"."Country"' in candidate.sql
    assert "(SELECT MAX(" not in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [("France", 30), ("Spain", 40)]


def test_constraint_expansion_infers_high_confidence_missing_entity_fk():
    airlines = {
        "name": "airlines",
        "columns": ["uid", "Airline"],
        "rows": [[1, "A"], [2, "B"], [3, "C"]],
    }
    flights = {
        "name": "flights",
        "columns": ["Airline", "FlightNo"],
        "rows": [[1, 10], [1, 11], [2, 12]],
    }
    candidate = best("Find all airlines that have at least 2 flights", [airlines, flights])
    assert 'JOIN "airlines" ON "flights"."Airline" = "airlines"."uid"' in candidate.sql
    assert execute([airlines, flights], candidate.sql) == [("A",)]


def test_constraint_expansion_can_be_disabled_without_affecting_recursive_expansion():
    searcher = SQLSearcher.from_tables([STADIUM, CONCERT], STADIUM_FKS)
    question = "Show stadium names that have more than one concert"
    recursive_only = searcher.search(
        question, expand_recursive=True, expand_constraints=False, expand_extrema=False,
    )
    constrained = searcher.search(
        question, expand_recursive=True, expand_constraints=True, expand_extrema=False,
    )
    assert all("constraint:" not in evidence
               for candidate in recursive_only for evidence in candidate.evidence)
    assert " HAVING " not in recursive_only[0].sql
    assert " HAVING " in constrained[0].sql


def test_extrema_expansion_searches_row_superlative():
    candidate = best("Show the name and country of the youngest person", [PEOPLE])
    assert candidate.sql.endswith('ORDER BY "people"."Age" ASC LIMIT 1')
    assert execute([PEOPLE], candidate.sql) == [("Bob", "France")]


def test_extrema_expansion_preserves_filter_on_row_superlative():
    candidate = best(
        "For people from France, show the name of the oldest person",
        [PEOPLE],
    )
    assert 'WHERE "people"."Country" = \'France\'' in candidate.sql
    assert candidate.sql.endswith('ORDER BY "people"."Age" DESC LIMIT 1')
    assert execute([PEOPLE], candidate.sql) == [("Alice",)]


def test_extrema_expansion_searches_explicit_top_n():
    candidate = best("Show the 2 youngest people names", [PEOPLE])
    assert candidate.sql.endswith('ORDER BY "people"."Age" ASC LIMIT 2')
    assert execute([PEOPLE], candidate.sql) == [("Bob",), ("Alice",)]


def test_extrema_expansion_distinguishes_limit_token_from_equal_filter_value():
    cars = {
        "name": "cars",
        "columns": ["Name", "Doors", "Price"],
        "rows": [["A", 2, 100], ["B", 4, 200], ["C", 5, 300]],
    }
    candidates = SQLSearcher.from_tables([cars], [], max_candidates=80).search(
        "List the price of the 2 largest cars by price with more than 2 doors"
    )
    candidate = next(
        item for item in candidates
        if item.sql.endswith('ORDER BY "cars"."Price" DESC LIMIT 2')
        and '"cars"."Doors" > 2' in item.sql
    )
    assert execute([cars], candidate.sql) == [(300,), (200,)]


def test_extrema_expansion_searches_frequency_superlative():
    candidate = best("Which country has the most people?", [PEOPLE])
    assert 'GROUP BY "people"."Country"' in candidate.sql
    assert candidate.sql.endswith("ORDER BY COUNT(*) DESC LIMIT 1")
    assert execute([PEOPLE], candidate.sql) == [("France",)]


def test_extrema_frequency_superlative_can_return_count():
    candidate = best(
        "List the country with the most people and how many people it has",
        [PEOPLE],
    )
    assert candidate.sql.startswith('SELECT "people"."Country", COUNT(*)')
    assert execute([PEOPLE], candidate.sql) == [("France", 2)]


def test_extrema_frequency_argmin_includes_zero_related_entities():
    students = {
        "name": "students",
        "columns": ["Student_ID", "Name"],
        "rows": [[1, "Alex"], [2, "Alex"], [3, "Cara"]],
    }
    pets = {
        "name": "pets",
        "columns": ["Pet_ID", "Student_ID"],
        "rows": [[1, 1], [2, 1], [3, 3]],
    }
    fks = [{
        "from_table": "pets", "from_col": "Student_ID",
        "to_table": "students", "to_col": "Student_ID",
    }]
    candidate = best(
        "Which student has the smallest number of pets?",
        [students, pets],
        fks,
    )
    assert "LEFT JOIN" in candidate.sql
    assert '"students"."Student_ID"' in candidate.sql.split(" ORDER BY ")[0]
    assert 'ORDER BY COUNT("pets"."Student_ID") ASC LIMIT 1' in candidate.sql
    assert execute([students, pets], candidate.sql) == [("Alex",)]


def test_extrema_expansion_returns_dual_lexical_extrema():
    cars = {
        "name": "cars",
        "columns": ["Name", "Country", "Price", "Weight"],
        "rows": [
            ["A", "France", 100, 1000],
            ["B", "France", 300, 900],
            ["C", "Spain", 500, 700],
        ],
    }
    candidate = best("Show the highest and lowest price", [cars])
    assert candidate.sql == 'SELECT MAX("cars"."Price"), MIN("cars"."Price") FROM "cars"'
    assert execute([cars], candidate.sql) == [(500, 100)]

    filtered = best("Show the highest and lowest price for cars in France", [cars])
    assert 'WHERE "cars"."Country" = \'France\'' in filtered.sql
    assert execute([cars], filtered.sql) == [(300, 100)]

    separate = best("Show the highest price and lowest weight", [cars])
    assert separate.sql == (
        'SELECT MAX("cars"."Price"), MIN("cars"."Weight") FROM "cars"'
    )
    assert execute([cars], separate.sql) == [(500, 700)]


def test_extrema_expansion_searches_set_difference():
    candidate = best(
        "Show the stadium names without any concert",
        [STADIUM, CONCERT],
        STADIUM_FKS,
    )
    assert " EXCEPT SELECT " in candidate.sql
    assert execute([STADIUM, CONCERT], candidate.sql) == [("Gamma",)]


def test_extrema_expansion_guards_multi_aggregate_and_can_be_disabled():
    aggregate = best("What are the minimum and maximum age of people?", [PEOPLE])
    assert aggregate.sql == 'SELECT MIN("people"."Age"), MAX("people"."Age") FROM "people"'

    searcher = SQLSearcher.from_tables([PEOPLE], [])
    question = "Show the name and country of the youngest person"
    without_extrema = searcher.search(question, expand_extrema=False)
    with_extrema = searcher.search(question, expand_extrema=True)
    assert all("extrema:" not in evidence
               for candidate in without_extrema for evidence in candidate.evidence)
    assert " LIMIT 1" not in without_extrema[0].sql
    assert " LIMIT 1" in with_extrema[0].sql


def test_shared_spider_evaluation_contract():
    metadata = {
        "demo": {
            "table_names_original": ["people", "visits"],
            "column_names_original": [
                [-1, "*"], [0, "id"], [1, "person_id"],
            ],
            "foreign_keys": [[2, 1]],
        }
    }
    example = {
        "db_id": "demo",
        "sql": {
            "from": {"table_units": [["table_unit", 0]]},
            "where": ["nested", {"table_units": [["table_unit", 1]]}],
        },
    }
    assert recursive_gold_table_names(example, metadata) == ["people", "visits"]
    assert spider_foreign_keys(metadata["demo"]) == [{
        "from_table": "visits", "from_col": "person_id",
        "to_table": "people", "to_col": "id", "conf": 1.0,
    }]


def test_live_table_query_ast_mode_executes_typed_candidate():
    class HermeticTableQuery(TableQuery):
        def schema(self, tables, fks):
            columns = []
            index = 0
            for table in tables:
                for name in table["columns"]:
                    values = [row[table["columns"].index(name)] for row in table["rows"]]
                    numeric = values and all(isinstance(value, (int, float)) for value in values)
                    columns.append({
                        "table": table["name"], "name": name, "idx": index,
                        "struct": set(), "affinity": "INTEGER" if numeric else "TEXT",
                        "ace": [], "is_date": False,
                        "qvec": np.zeros(2, dtype=np.float32), "values": values,
                    })
                    index += 1
            return columns, {}, {table["name"]: table for table in tables}

        def ast_semantic_signals(self, question, sch):
            return SemanticSignals.empty()

    planner = HermeticTableQuery()
    response = planner.serve([PEOPLE], "list person names")
    assert response["valid"] is True
    assert response["error"] is None
    assert response["result"]["rows"] == [["Alice"], ["Bob"], ["Cara"]]
    assert response["candidate_count"] > 0
    assert response["ast"].startswith("SelectQuery(")
    assert response["model"] == "engine - typed SQL AST planner (deterministic search)"
    assert response["fallback"] is None
    selection = response["selection"]
    assert selection["served_by"] == "search" and selection["executable"] == selection["pool_size"]
    assert selection["eligible"] == selection["pool_size"] and selection["rank"] == 0
    assert "fallback" not in selection
    assert compare_spider_rows([["1"], [None]], [[None], [1.0]])["strict"]
    assert not compare_spider_rows([[1, 2]], [[2, 1]])["strict"]
    assert not compare_spider_rows([[1, 1]], [[1]])["strict"]


def test_weight_manifest_detects_tampered_bundle():
    with tempfile.TemporaryDirectory() as directory:
        artifact = os.path.join(directory, "model.bin")
        committed = os.path.join(directory, "model.json")
        with open(artifact, "wb") as handle:
            handle.write(b"correct")
        with open(committed, "wb") as handle:
            handle.write(b'{"correct":true}')
        manifest = {
            "version": 1,
            "files": {"model.bin": sha256_file(artifact)},
            "committed_artifacts": {
                "model.json": {"sha256": sha256_file(committed), "note": "tracked test artifact"}
            },
        }
        fingerprint = validate_weight_bundle(directory, manifest)
        assert len(fingerprint) == 64
        with open(artifact, "wb") as handle:
            handle.write(b"tampered")
        try:
            validate_weight_bundle(directory, manifest)
        except RuntimeError as exc:
            assert "model.bin" in str(exc)
        else:
            raise AssertionError("tampered model bundle was accepted")
        with open(artifact, "wb") as handle:
            handle.write(b"correct")
        with open(committed, "wb") as handle:
            handle.write(b'{"tampered":true}')
        try:
            validate_weight_bundle(directory, manifest)
        except RuntimeError as exc:
            assert "model.json" in str(exc)
        else:
            raise AssertionError("tampered committed model artifact was accepted")


def test_world_own_data_route_preserves_ast_observability():
    from engine.knowledge_tables import KnowledgeTableQuery

    class FakeOwnPlanner:
        @staticmethod
        def ingest(tables):
            return tables, []

        @staticmethod
        def schema(tables, fks):
            return [], {}, {}

        @staticmethod
        def serve(tables, question):
            return {
                "sql": 'SELECT "Name" FROM "people"',
                "result": {"columns": ["Name"], "rows": [["Alice"]]},
                "error": None,
                "ast": "SelectQuery(...)",
                "candidate_count": 7,
                "evidence": ["extrema:projection"],
                "features": {"projection": 1.0},
                "selection": {"served_by": "gemini-rewrite", "pool_size": 7},
                "fallback": {"kind": "rewrite", "model": "gemini-test",
                             "question": "list the Name of every person"},
                "calculations": [{"specification": "ratio", "status": "satisfied"}],
                "model": "typed planner",
            }

    class HermeticKnowledgeTableQuery(KnowledgeTableQuery):
        def __init__(self):
            self.q11 = FakeOwnPlanner()

        @staticmethod
        def route(table):
            return {}

        @staticmethod
        def column_dims(schema, table_name):
            return {}

        @staticmethod
        def meaning_filter(question, routes):
            return None

        @staticmethod
        def _own_value_matches(question, tables):
            return []

        @staticmethod
        def world_target(question, routes):
            return None

        @staticmethod
        def _debug_input(*args):
            return {}

    response = HermeticKnowledgeTableQuery().serve([PEOPLE], "list person names")

    assert response["planner"] == {
        "ast": "SelectQuery(...)",
        "candidate_count": 7,
        "evidence": ["extrema:projection"],
        "features": {"projection": 1.0},
        "selection": {"served_by": "gemini-rewrite", "pool_size": 7},
    }
    assert response["model"] == "typed planner"
    assert response["calculations"] == [{"specification": "ratio", "status": "satisfied"}]
    # The coverage gate reads the question the served query answers from this record.
    assert response["fallback"] == {"kind": "rewrite", "model": "gemini-test",
                                    "question": "list the Name of every person"}


def test_world_target_uses_the_synced_iso_currency_column():
    from engine.knowledge_tables import KnowledgeTableQuery

    query = KnowledgeTableQuery.__new__(KnowledgeTableQuery)
    query.words = {
        "country": {
            "key": "qid",
            "columns": ["qid", "name", "continent", "currency"],
            "links": [],
        },
    }

    target = query.world_target(
        "total amount by currency",
        {("sales", "country"): "country"},
    )

    assert target == {
        "table": "country",
        "col": "currency",
        "affinity": "TEXT",
        "path": [{
            "left_table": "sales",
            "left_col": "country",
            "right_table": "country",
            "right_col": "qid",
        }],
        "word": "currency",
    }


def test_schema_graph_resolves_normalized_foreign_key_names():
    graph = SchemaGraph.from_planner(
        [
            {"table": "Order_Items", "name": "Order_ID", "affinity": "INTEGER"},
            {"table": "Orders", "name": "ID", "affinity": "INTEGER"},
        ],
        [{
            "from_table": "Order Items", "from_col": "order_id",
            "to_table": "orders", "to_col": "id",
        }],
    )
    assert len(graph.foreign_keys) == 1
    assert graph.foreign_keys[0].signature == (
        "Order_Items", "Order_ID", "Orders", "ID",
    )


def test_spider_evaluator_does_not_count_all_errors_as_answered():
    counter = Counter()
    query = SelectQuery((SelectItem(Star()),), "missing")
    candidate = ScoredQuery(query, 'SELECT * FROM "missing"', 0.0, ())
    connection = sqlite3.connect(":memory:")
    score_spider_candidates(counter, [[1]], [candidate], connection, 1)
    connection.close()
    assert counter["answered"] == 0
    assert counter["execution_failure"] == 1
    assert counter["scalar_n"] == 1

    integrated = Counter()
    record_integrated_result(integrated, [[1]], {}, answered=False)
    assert integrated["answered"] == 0
    assert integrated["error"] == 1
    assert integrated["scalar_total"] == 1
    assert integrated["scalar_correct"] == 0


def test_ast_failure_profiles_share_structural_and_schema_vocabulary():
    metadata = {
        "table_names_original": ["people"],
        "column_names_original": [[-1, "*"], [0, "Name"]],
    }
    spider_sql = {
        "select": [False, [[3, [0, [0, 1, False], None]]]],
        "from": {"table_units": [["table_unit", 0]], "conds": []},
        "where": [],
        "groupBy": [],
        "having": [],
        "orderBy": [],
        "limit": None,
        "intersect": None,
        "union": None,
        "except": None,
    }
    name = ColumnRef("people", "Name", SQLType.TEXT)
    query = SelectQuery((SelectItem(Aggregate("COUNT", name)),), "people")
    gold = profile_spider_sql(spider_sql, metadata)
    candidate = profile_query(query)
    assert gold.sketch == candidate.sketch
    assert gold.tables == candidate.tables == ("people",)
    assert gold.role_map == candidate.role_map == {
        "projection": ("people.name",),
        "aggregate": ("people.name",),
    }


def test_ast_failure_profiles_align_spider_and_typed_joins():
    metadata = {
        "table_names_original": ["parent", "child"],
        "column_names_original": [
            [-1, "*"], [0, "id"], [1, "parent_id"],
        ],
    }
    spider_sql = {
        "select": [False, [[0, [0, [0, 1, False], None]]]],
        "from": {
            "table_units": [["table_unit", 1], ["table_unit", 0]],
            "conds": [[
                False, 2, [0, [0, 2, False], None], [0, 1, False], None,
            ]],
        },
        "where": [],
        "groupBy": [],
        "having": [],
        "orderBy": [],
        "limit": None,
        "intersect": None,
        "union": None,
        "except": None,
    }
    parent_id = ColumnRef("parent", "id", SQLType.INTEGER)
    child_parent_id = ColumnRef("child", "parent_id", SQLType.INTEGER)
    query = SelectQuery(
        (SelectItem(parent_id),),
        "child",
        joins=(Join("parent", child_parent_id, parent_id),),
    )
    assert profile_spider_sql(spider_sql, metadata) == profile_query(query)


def test_ast_failure_diagnosis_separates_recall_and_linking_bottlenecks():
    gold = SQLProfile.build(
        {"blocks": 1, "select_items": 2},
        ["items"],
        {"projection": ["items.a", "items.b"]},
    )

    def assessed(rank, profile, *, strict=False, lenient=False):
        return CandidateAssessment(
            rank, f"candidate-{rank}", profile, strict=strict, lenient=lenient
        )

    wrong_sketch = SQLProfile.build(
        {"blocks": 1, "select_items": 1},
        ["items"],
        {"projection": ["items.a"]},
    )
    assert diagnose_pool(gold, [assessed(0, wrong_sketch)])["bottleneck"] == "missing_sketch"

    wrong_column = SQLProfile.build(
        {"blocks": 1, "select_items": 2},
        ["items"],
        {"projection": ["items.a", "items.c"]},
    )
    diagnosis = diagnose_pool(gold, [assessed(0, wrong_column)])
    assert diagnosis["bottleneck"] == "missing_column_link"
    assert diagnosis["missing_role_columns"] == {"projection": ["items.b"]}

    complementary = SQLProfile.build(
        {"blocks": 1, "select_items": 2},
        ["items"],
        {"projection": ["items.b", "items.c"]},
    )
    assert diagnose_pool(
        gold, [assessed(0, wrong_column), assessed(1, complementary)]
    )["bottleneck"] == "missing_composition"

    exact = assessed(1, gold, strict=True, lenient=True)
    assert diagnose_pool(gold, [assessed(0, wrong_column), exact])["status"] == "strict_in_pool"
    assert diagnose_pool(gold, [assessed(0, gold)])["bottleneck"] == "value_or_semantic_mismatch"


TESTS = [
    test_shared_ranking_rule_preserves_calculation_then_money_precedence,
    test_pool_oracle_counts_only_eligible_denotation_hits_and_validates_checkpoints,
    test_mentioned_table_join_keeps_minimal_variant_in_pool,
    test_duplicate_named_projection_keeps_single_binding_variant_in_pool,
    test_select_query_serves_the_reading_that_keeps_the_named_date,
    test_select_query_never_chooses_a_query_that_does_not_run,
    test_a_join_that_multiplies_two_tables_rows_stops_at_their_cells_budget,
    test_the_rewording_pool_shares_the_tables_copy_and_reruns_no_query,
    test_a_duration_is_how_long_rows_lasted_not_a_quantity_and_an_interval,
    test_literal_grounding_names_the_column_a_value_actually_occupies,
    test_select_query_never_serves_a_misgrounded_query,
    test_select_query_never_serves_a_join_the_foreign_keys_contradict,
    test_select_query_serves_the_bridge_join_the_foreign_keys_state,
    test_join_grounding_leaves_joins_the_foreign_keys_do_not_contradict,
    test_join_grounding_reads_every_equated_column_pair,
    test_double_counting_reads_the_rows_each_join_repeats,
    test_named_request_decomposes_a_compound_question_a_single_query_answers_in_part,
    test_a_compound_named_request_asks_for_decomposition_before_selection_runs,
    test_named_request_never_serves_or_decomposes_a_model_only_set_operation,
    test_proposal_import_rejects_malformed_model_text,
    test_decoded_sql_normalizer_preserves_multiline_statements_and_strips_fences,
    test_the_fallback_is_not_asked_when_the_search_covers_the_question,
    test_a_question_the_search_cannot_read_is_answered_through_geminis_rewording,
    test_gemini_rewording_cannot_drop_a_user_constraint_from_coverage,
    test_gemini_cannot_write_sql_when_its_rewrite_is_not_searchable,
    test_a_disabled_fallback_is_never_asked,
    test_a_gemini_outage_serves_nothing_and_is_not_cached,
    test_unreadable_runnable_baseline_is_not_served_when_rewriting_fails,
    test_the_fallback_is_stateless_across_repeated_requests,
    test_rewriter_recovers_when_a_runnable_plan_ignores_part_of_the_question,
    test_schema_rewrite_can_resolve_a_synonym_without_changing_the_sql_reading,
    test_a_text_column_is_an_aggregate_operand_only_where_it_ends_the_aggregate_phrase,
    test_a_text_right_after_contain_or_all_is_compared_inside_values,
    test_an_average_a_column_name_spells_is_the_column,
    test_the_rewording_of_a_keyword_volume_keeps_every_keyword_holding_the_phrase,
    test_a_listing_of_numbers_names_the_rows_its_text_filter_picked,
    test_unread_check_accepts_a_threshold_only_when_the_ast_realizes_it,
    test_unread_check_treats_sheet_scope_words_as_context_not_filters,
    test_gemini_reads_schema_names_but_not_cell_values,
    test_gemini_rewrite_must_preserve_data_values_and_numbers,
    test_evaluator_grades_the_served_selection,
    test_sql_import_maps_numeric_arithmetic_but_refuses_nonnumeric,
    test_sql_import_round_trip_executes_and_matches,
    test_sql_import_preserves_distinct_self_join_roles,
    test_typed_ast_rejects_invalid_aggregate,
    test_grouped_ast_rejects_ungrouped_ordering,
    test_typed_ast_rejects_mismatched_literal_payloads,
    test_ast_rejects_indeterminate_set_and_aggregate_shapes,
    test_grouping_validation_sees_ordered_aggregates,
    test_composite_foreign_key_renders_and_executes_as_one_join,
    test_composite_join_validation_rejects_disconnected_predicate,
    test_composite_self_join_helpers_render_complete_predicates,
    test_arithmetic_expression_sum_renders_and_executes,
    test_arithmetic_division_preserves_real_semantics,
    test_currency_conversion_query_executes_end_to_end,
    test_planner_emits_currency_conversion_when_requested,
    test_planner_discovers_and_executes_same_name_currency_edge,
    test_currency_conversion_survives_uploaded_column_case,
    test_world_aggregate_currency_binding_is_typed_and_unambiguous,
    test_planner_requires_exact_currency_target_and_key_shape,
    test_arithmetic_expression_validation,
    test_projection_filter_and_order,
    test_scalar_aggregate_does_not_group_by_recipient_mention,
    test_order_noun_does_not_request_sort_or_group,
    test_shared_table_words_do_not_collapse_distinct_projection_mentions,
    test_generic_projection_respects_entity_qualifier,
    test_entity_projection_follows_owner_foreign_key,
    test_fk_attribute_phrase_does_not_project_the_source_qualifier,
    test_entity_id_does_not_follow_owner_foreign_key,
    test_duplicate_property_projection_respects_entity_qualifier,
    test_directional_year_filter_targets_date_column,
    test_month_and_dated_phrases_filter_the_date_column,
    test_date_ranges_lists_and_quarters_filter_one_span,
    test_the_coverage_gate_reads_the_months_a_query_compares,
    test_a_share_divides_the_kept_rows_aggregate_by_the_whole,
    test_a_share_word_is_the_aggregate_of_the_rows_it_divides,
    test_two_values_of_one_column_are_either,
    test_a_noun_over_a_number_compares_each_row,
    test_a_month_word_joins_a_date_phrase_only_as_a_date,
    test_either_value_reads_only_listed_values,
    test_a_count_over_times_and_a_share_word_that_names_a_thing,
    test_a_lowercase_grammar_word_links_to_no_value,
    test_a_stated_comparison_on_an_aggregate_needs_no_where,
    test_a_word_of_time_orders_by_a_date_and_a_name_part_places_nothing,
    test_a_table_joins_every_reading_only_when_named_together,
    test_an_order_of_names_its_target_as_by_does,
    test_a_candidate_drops_a_key_echo_and_an_unread_join,
    test_a_listing_drops_a_key_that_repeats_a_read_table,
    test_a_comparative_than_a_number_compares_the_measure_it_describes,
    test_a_contained_text_compares_the_lowered_values,
    test_a_distinct_counted_noun_counts_the_column_it_names,
    test_a_having_reading_lists_in_the_question_order,
    test_a_superlative_of_only_its_measure_is_the_extreme_value,
    test_a_year_column_compares_the_year_itself,
    test_an_unjoinable_projection_drops_out_of_the_reading,
    test_two_values_a_child_table_holds_are_both,
    test_a_column_word_that_introduces_a_value_is_not_listed,
    test_the_words_of_a_column_name_are_no_value,
    test_a_several_word_column_name_is_a_mention_where_it_is_said,
    test_the_article_a_is_no_value,
    test_a_value_compares_the_column_whose_words_introduce_it,
    test_a_value_stated_once_is_compared_once,
    test_a_column_is_named_without_the_word_of_its_kind,
    test_a_plural_reads_as_its_singular_everywhere,
    test_a_key_named_by_the_table_it_references_needs_its_whole_name,
    test_a_denial_is_read_by_what_it_denies,
    test_a_ranking_measure_is_not_an_asked_aggregate,
    test_a_total_by_month_groups_by_the_year_month,
    test_same_shaped_tabs_answer_a_stated_keyword,
    test_tabs_of_one_layout_are_read_as_one,
    test_total_before_a_measure_reads_the_measure_or_the_whole_name,
    test_a_counted_noun_naming_an_unjoined_column_counts_the_rows,
    test_a_sum_over_rows_its_joins_repeat_is_served_only_when_every_reading_is_one,
    test_a_question_no_reading_reads_whole_is_asked_about_the_word_it_could_not_read,
    test_the_rewrite_asks_for_low_thinking_and_stops_at_twenty_seconds,
    test_serving_preserves_repeated_source_rows_in_aggregates,
    test_a_listing_follows_the_order_the_question_names,
    test_by_after_a_participle_names_who_acted,
    test_multiple_aggregates_share_a_typed_operand,
    test_repeated_count_paraphrase_is_one_aggregate,
    test_total_number_of_entities_is_a_scalar_count,
    test_number_used_as_a_column_label_is_not_a_count_request,
    test_abbreviated_number_column_is_not_a_count_request,
    test_travel_direction_disambiguates_parallel_airport_foreign_keys,
    test_scalar_count_keeps_qualified_one_letter_category_filter,
    test_counted_table_beats_related_column_with_same_entity_word,
    test_arbitrary_word_does_not_become_a_category_initial_filter,
    test_ranker_prefers_count_distinct_over_grouped_count,
    test_ranker_coordinates_multiple_aggregate_operands,
    test_multi_hop_join_uses_bridge_table,
    test_grouped_count_uses_entity_display_column,
    test_filter_column_does_not_leak_into_projection,
    test_order_column_does_not_leak_into_projection,
    test_literals_are_escaped_by_renderer,
    test_grouped_topn_orders_by_aggregate_across_bridge,
    test_ranked_units_sold_sums_the_quantity_measure,
    test_ranked_revenue_sums_the_amount_measure,
    test_unit_price_ranking_stays_a_raw_rate_order,
    test_total_spend_binds_the_amount_measure_deterministically,
    test_explicit_total_quantity_sold_phrasing_is_unchanged,
    test_money_noun_naming_the_table_reads_as_its_money_total,
    test_counting_or_listing_a_money_named_table_keeps_the_entity,
    test_money_named_table_without_a_money_column_keeps_the_entity,
    test_served_selection_contract_keeps_money_totals_and_converted_totals,
    test_world_path_money_noun_naming_the_table_sums_its_money_column,
    test_world_path_reads_quantity_superlatives_and_counted_measures,
    test_operator_readout_never_reads_an_aggregate_off_a_closed_class_word,
    test_one_surrogate_key_rule_names_keys_not_measures_or_codes,
    test_literal_measure_column_keeps_raw_interpretation,
    test_search_is_deterministic,
    test_encoder_role_signal_breaks_ambiguous_column_tie,
    test_profile_beam_expands_missing_projection_binding,
    test_profile_beam_instantiates_grouped_frequency_shape,
    test_profile_expansion_caps_variants_and_penalizes_transformation,
    test_profile_expansion_preserves_hand_ranked_fallback_top,
    test_profile_fallback_applies_when_no_compatible_variant_exists,
    test_profile_generation_requires_explicit_configuration,
    test_soft_prediction_budget_does_not_abandon_work,
    test_recursive_ast_scalar_subquery_executes,
    test_recursive_ast_correlated_exists_executes,
    test_recursive_ast_set_query_in_derived_table_executes,
    test_recursive_expansion_searches_scalar_average,
    test_recursive_expansion_searches_anti_membership,
    test_recursive_expansion_searches_route_self_join,
    test_recursive_expansion_searches_nested_count_aggregate,
    test_recursive_set_expansion_keeps_every_categorical_alternative,
    test_constraint_expansion_searches_cross_table_count_having,
    test_constraint_expansion_searches_single_table_count_having,
    test_constraint_expansion_searches_disjunction,
    test_constraint_expansion_disjoins_every_repeated_column_group,
    test_constraint_disjunction_deduplicates_entities_across_relation,
    test_constraint_disjunction_preserves_shared_official_filter,
    test_constraint_expansion_searches_filtered_scalar_minimum,
    test_constraint_expansion_keeps_grouped_superlative_as_aggregate,
    test_constraint_expansion_infers_high_confidence_missing_entity_fk,
    test_constraint_expansion_can_be_disabled_without_affecting_recursive_expansion,
    test_extrema_expansion_searches_row_superlative,
    test_extrema_expansion_preserves_filter_on_row_superlative,
    test_extrema_expansion_searches_explicit_top_n,
    test_extrema_expansion_distinguishes_limit_token_from_equal_filter_value,
    test_extrema_expansion_searches_frequency_superlative,
    test_extrema_frequency_superlative_can_return_count,
    test_extrema_frequency_argmin_includes_zero_related_entities,
    test_extrema_expansion_returns_dual_lexical_extrema,
    test_extrema_expansion_searches_set_difference,
    test_extrema_expansion_guards_multi_aggregate_and_can_be_disabled,
    test_shared_spider_evaluation_contract,
    test_live_table_query_ast_mode_executes_typed_candidate,
    test_weight_manifest_detects_tampered_bundle,
    test_world_own_data_route_preserves_ast_observability,
    test_world_target_uses_the_synced_iso_currency_column,
    test_schema_graph_resolves_normalized_foreign_key_names,
    test_spider_evaluator_does_not_count_all_errors_as_answered,
    test_ast_failure_profiles_share_structural_and_schema_vocabulary,
    test_ast_failure_profiles_align_spider_and_typed_joins,
    test_ast_failure_diagnosis_separates_recall_and_linking_bottlenecks,
]


def main():
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nSQL AST: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
