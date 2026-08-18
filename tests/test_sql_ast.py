"""Hermetic execution tests for deterministic SQL AST search and ranking.

Run: python -m tests.test_sql_ast
"""
from __future__ import annotations

from collections import Counter
import os
import sqlite3
import sys
import tempfile
import threading

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
from engine.artifact_provenance import sha256_file, validate_weight_bundle
from engine.sql_rank import (
    CandidateRanker,
    ExecutedCandidate,
    SemanticSignals,
    analyze_question,
    execute_and_rerank,
)
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


def test_arithmetic_expression_validation():
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    name = ColumnRef("orders", "name", SQLType.TEXT)
    # a valid bare arithmetic projection passes
    validate_query(SelectQuery((SelectItem(BinaryExpr(amount, "+", Literal(1, SQLType.INTEGER))),), "orders"))
    for bad in (
        BinaryExpr(amount, "%", amount),                       # unsupported operator
        BinaryExpr(amount, "*", Star()),                       # arithmetic on * is invalid
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


def test_projection_filter_and_order():
    candidate = best(
        "Show name, country and age for people from France ordered by age descending",
        [PEOPLE],
    )
    assert execute([PEOPLE], candidate.sql) == [("Alice", "France", 30), ("Bob", "France", 20)]
    assert candidate.sql.endswith('ORDER BY "people"."Age" DESC')


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
    assert 'COUNT("Documents".' in candidate.sql
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


def test_execution_rerank_penalizes_empty_candidate():
    query_a = SelectQuery((SelectItem(Star()),), "empty_table")
    query_b = SelectQuery((SelectItem(Star()),), "nonempty_table")
    first = ScoredQuery(query_a, render_query(query_a), 10.0, ())
    second = ScoredQuery(query_b, render_query(query_b), 9.0, ())
    ranked = CandidateRanker(SQLSearcher.from_tables(
        [{"name": "empty_table", "columns": ["x"], "rows": []},
         {"name": "nonempty_table", "columns": ["x"], "rows": [[1]]}],
        [],
    ).schema).rank_executions("show rows", [
        ExecutedCandidate(first, ("x",), ()),
        ExecutedCandidate(second, ("x",), ((1,),)),
    ])
    assert ranked[0].candidate.sql == second.sql


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


def test_execution_checks_preserve_successful_semantic_winner():
    schema = SQLSearcher.from_tables(
        [{"name": "empty_table", "columns": ["x"], "rows": []},
         {"name": "nonempty_table", "columns": ["x"], "rows": [[1]]}],
        [],
    ).schema
    query_a = SelectQuery((SelectItem(Star()),), "empty_table")
    query_b = SelectQuery((SelectItem(Star()),), "nonempty_table")
    first = ScoredQuery(query_a, render_query(query_a), 10.0, ())
    second = ScoredQuery(query_b, render_query(query_b), 9.0, ())

    def execute(sql):
        return (("x",), ()) if "empty_table" in sql else (("x",), ((1,),))

    ranked = execute_and_rerank("show rows", [first, second], schema, execute)
    assert ranked[0].candidate.sql == first.sql


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

    response = HermeticTableQuery().serve([PEOPLE], "list person names")
    assert response["valid"] is True
    assert response["error"] is None
    assert response["result"]["rows"] == [["Alice"], ["Bob"], ["Cara"]]
    assert response["candidate_count"] > 0
    assert response["ast"].startswith("SelectQuery(")
    assert "AST planner" in response["model"]
    assert compare_spider_rows([["1"], [None]], [[None], [1.0]])["strict"]
    assert not compare_spider_rows([[1, 2]], [[2, 1]])["strict"]
    assert not compare_spider_rows([[1, 1]], [[1]])["strict"]


def test_weight_manifest_detects_tampered_bundle():
    with tempfile.TemporaryDirectory() as directory:
        artifact = os.path.join(directory, "model.bin")
        with open(artifact, "wb") as handle:
            handle.write(b"correct")
        manifest = {"version": 1, "files": {"model.bin": sha256_file(artifact)}}
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
    }
    assert response["model"] == "typed planner"


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
    test_typed_ast_rejects_invalid_aggregate,
    test_grouped_ast_rejects_ungrouped_ordering,
    test_typed_ast_rejects_mismatched_literal_payloads,
    test_ast_rejects_indeterminate_set_and_aggregate_shapes,
    test_grouping_validation_sees_ordered_aggregates,
    test_composite_foreign_key_renders_and_executes_as_one_join,
    test_composite_join_validation_rejects_disconnected_predicate,
    test_composite_self_join_helpers_render_complete_predicates,
    test_arithmetic_expression_sum_renders_and_executes,
    test_currency_conversion_query_executes_end_to_end,
    test_planner_emits_currency_conversion_when_requested,
    test_arithmetic_expression_validation,
    test_projection_filter_and_order,
    test_shared_table_words_do_not_collapse_distinct_projection_mentions,
    test_generic_projection_respects_entity_qualifier,
    test_entity_projection_follows_owner_foreign_key,
    test_fk_attribute_phrase_does_not_project_the_source_qualifier,
    test_entity_id_does_not_follow_owner_foreign_key,
    test_duplicate_property_projection_respects_entity_qualifier,
    test_directional_year_filter_targets_date_column,
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
    test_search_is_deterministic,
    test_encoder_role_signal_breaks_ambiguous_column_tie,
    test_profile_beam_expands_missing_projection_binding,
    test_profile_beam_instantiates_grouped_frequency_shape,
    test_profile_expansion_caps_variants_and_penalizes_transformation,
    test_profile_expansion_preserves_hand_ranked_fallback_top,
    test_profile_fallback_applies_when_no_compatible_variant_exists,
    test_execution_rerank_penalizes_empty_candidate,
    test_profile_generation_requires_explicit_configuration,
    test_execution_checks_preserve_successful_semantic_winner,
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
