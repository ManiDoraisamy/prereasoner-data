"""Hermetic execution tests for deterministic SQL AST search and ranking.

Run: python -m tests.test_sql_ast
"""
from __future__ import annotations

from collections import Counter
import os
import sqlite3
import sys
import tempfile

import numpy as np

from engine.sql_ast import (
    ASTValidationError,
    Aggregate,
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
from engine.sql_rank import CandidateRanker, ExecutedCandidate, SemanticSignals
from engine.sql_learned_rank import (
    DecisionTree,
    LinearRankerModel,
    TreeEnsembleRankerModel,
    TreeNode,
    learned_feature_vector,
    load_ranker_model,
    rerank_with_promotion_gate,
)
from engine.sql_search import SQLSearcher, SchemaGraph, ScoredQuery
from engine.sql_proposal import (
    PAIR_EXTRA_FEATURES,
    SQLProposalModel,
    pair_features,
    semantic_signals_from_schema,
)
from engine.sql_profile_expansion import ProfileQueryExpander
from engine.sql_proposal_runtime import schema_descriptors
from engine.tables import TableQuery
from spider.probe.spider_eval import (
    compare as compare_spider_rows,
    record_integrated_result,
    recursive_gold_table_names,
    spider_foreign_keys,
)
from spider.probe.ast_eval import _score as score_spider_candidates
from spider.probe.ast_profile import (
    CandidateAssessment,
    SQLProfile,
    diagnose_pool,
    profile_query,
    profile_spider_sql,
)
from spider.probe.build_ast_proposal_data import (
    contrast_record,
    extract_literal_targets,
    split_database_ids,
)
from spider.probe.train_ast_ranker import (
    calibrate_promotion_gate,
    load_or_encode_question_vectors,
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


def test_projection_filter_and_order():
    candidate = best(
        "Show name, country and age for people from France ordered by age descending",
        [PEOPLE],
    )
    assert execute([PEOPLE], candidate.sql) == [("Alice", "France", 30), ("Bob", "France", 20)]
    assert candidate.sql.endswith('ORDER BY "people"."Age" DESC')


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


def test_phase2_ranks_count_distinct_above_grouped_count():
    candidate = best("Find the number of distinct type of pets", [PETS])
    assert candidate.sql == 'SELECT COUNT(DISTINCT "Pets"."PetType") FROM "Pets"'
    assert execute([PETS], candidate.sql) == [(2,)]


def test_phase2_coordinates_multiple_aggregate_operands():
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


def test_proposed_sketch_profile_promotes_matching_typed_candidate():
    plain_query = SelectQuery((SelectItem(Star()),), "customers")
    limited_query = SelectQuery((SelectItem(Star()),), "customers", limit=1)
    signals = SemanticSignals({}, {}, (profile_query(limited_query).sketch_map,))
    ranked = CandidateRanker(
        SQLSearcher.from_tables([CUSTOMERS], []).schema,
        signals,
    ).rank("show customers", [
        ScoredQuery(plain_query, render_query(plain_query), 0.0, ()),
        ScoredQuery(limited_query, render_query(limited_query), 0.0, ()),
    ])
    assert ranked[0].query == limited_query
    assert ("model_sketch_profile:1", 4.0) in ranked[0].features


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
        "show people details", semantic_signals=signals, phase3=False, phase4=False, phase5=False
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
        "show people details", semantic_signals=signals, phase3=False, phase4=False, phase5=False
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
    expanded = searcher.search("show people details", semantic_signals=signals)
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
    expanded = searcher.search("show people details", semantic_signals=signals)
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


def test_phase3_ranks_set_query_by_both_operands():
    # Both branches contribute without doubling the ordinary semantic-feature scale.
    stadium = {"name": "stadium", "columns": ["Name", "Location"],
               "rows": [["Alpha", "North"], ["Beta", "South"]]}
    schema = SQLSearcher.from_tables([stadium], []).schema
    name = ColumnRef("stadium", "Name", SQLType.TEXT)
    location = ColumnRef("stadium", "Location", SQLType.TEXT)
    left = SelectQuery((SelectItem(name),), "stadium",
                       where=Comparison(location, "=", Literal("North", SQLType.TEXT)))
    right_name = SelectQuery((SelectItem(name),), "stadium",
                             where=Comparison(location, "=", Literal("South", SQLType.TEXT)))
    right_location = SelectQuery((SelectItem(location),), "stadium",
                                 where=Comparison(location, "=", Literal("South", SQLType.TEXT)))
    aligned_set = SetQuery(left, "EXCEPT", right_name)
    misaligned_set = SetQuery(left, "EXCEPT", right_location)
    aligned = ScoredQuery(aligned_set, render_query(aligned_set), 0.0, ())
    misaligned = ScoredQuery(misaligned_set, render_query(misaligned_set), 0.0, ())
    ranked = CandidateRanker(schema).rank("stadium names except southern ones", [misaligned, aligned])
    scores = {candidate.sql: candidate.score for candidate in ranked}
    assert scores[aligned.sql] != scores[misaligned.sql]
    assert ranked[0].sql == aligned.sql
    learned = learned_feature_vector(
        "stadium names except southern ones", ranked[0]
    )
    assert "heuristic_value.left" not in learned
    assert "heuristic_value.right" not in learned
    assert learned["heuristic_count.projection_role"] == 1.0


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


def test_phase3_searches_scalar_average():
    candidate = best("Show names of people older than the average age", [PEOPLE])
    assert "(SELECT AVG(" in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [("Cara",)]


def test_phase3_searches_anti_membership():
    candidate = SQLSearcher.from_tables([STADIUM, CONCERT], STADIUM_FKS).search(
        "Show the stadium names without any concert", phase5=False
    )[0]
    assert " NOT IN (SELECT " in candidate.sql
    assert execute([STADIUM, CONCERT], candidate.sql) == [("Gamma",)]


def test_phase3_searches_route_self_join():
    candidate = best(
        "How many flights depart from City Aberdeen and have destination City Ashley?",
        [AIRPORTS, FLIGHTS],
        FLIGHT_FKS,
    )
    assert candidate.sql.count('JOIN "airports"') == 2
    assert 'AS "source"' in candidate.sql and 'AS "destination"' in candidate.sql
    assert execute([AIRPORTS, FLIGHTS], candidate.sql) == [(1,)]


def test_phase3_searches_nested_count_aggregate():
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


def test_phase3_set_expansion_keeps_every_categorical_alternative():
    people = {
        "name": "people",
        "columns": ["Name", "Country"],
        "rows": [["A", "France"], ["B", "Spain"], ["C", "Italy"]],
    }
    candidates = SQLSearcher.from_tables([people], [], max_candidates=80).search(
        "List people in France, Spain, or Italy",
        phase4=False,
        phase5=False,
    )
    candidate = next(candidate for candidate in candidates if " UNION " in candidate.sql)
    assert candidate.sql.count("SELECT") == 3
    assert '"people"."Country" = \'Italy\'' in candidate.sql
    assert " AND " not in candidate.sql
    assert execute([people], candidate.sql) == [("A",), ("B",), ("C",)]


def test_phase4_searches_cross_table_count_having():
    candidate = best(
        "Show stadium names that have more than one concert",
        [STADIUM, CONCERT],
        STADIUM_FKS,
    )
    assert 'GROUP BY "stadium"."Name"' in candidate.sql
    assert "HAVING COUNT(*) > 1" in candidate.sql
    assert execute([STADIUM, CONCERT], candidate.sql) == [("Alpha",)]


def test_phase4_searches_single_table_count_having():
    candidate = best("List countries having at least two people", [PEOPLE])
    assert 'GROUP BY "people"."Country"' in candidate.sql
    assert "HAVING COUNT(*) >= 2" in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [("France",)]


def test_phase4_searches_disjunction():
    candidate = best(
        "How many people are from France or have age greater than 35?",
        [PEOPLE],
    )
    assert '"people"."Country" = \'France\' OR "people"."Age" > 35' in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [(3,)]


def test_phase4_disjoins_every_repeated_column_group():
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


def test_phase4_disjunction_deduplicates_entities_across_relation():
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


def test_phase4_disjunction_preserves_shared_official_filter():
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


def test_phase4_searches_filtered_scalar_minimum():
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


def test_phase4_keeps_grouped_superlative_as_aggregate():
    candidate = best("What is the maximum age for all the different countries?", [PEOPLE])
    assert 'MAX("people"."Age")' in candidate.sql
    assert 'GROUP BY "people"."Country"' in candidate.sql
    assert "(SELECT MAX(" not in candidate.sql
    assert execute([PEOPLE], candidate.sql) == [("France", 30), ("Spain", 40)]


def test_phase4_infers_high_confidence_missing_entity_fk():
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


def test_phase4_can_be_disabled_without_contaminating_phase3():
    searcher = SQLSearcher.from_tables([STADIUM, CONCERT], STADIUM_FKS)
    question = "Show stadium names that have more than one concert"
    phase3 = searcher.search(question, phase3=True, phase4=False, phase5=False)
    phase4 = searcher.search(question, phase3=True, phase4=True, phase5=False)
    assert all("phase4:" not in evidence for candidate in phase3 for evidence in candidate.evidence)
    assert " HAVING " not in phase3[0].sql
    assert " HAVING " in phase4[0].sql


def test_phase5_searches_row_superlative():
    candidate = best("Show the name and country of the youngest person", [PEOPLE])
    assert candidate.sql.endswith('ORDER BY "people"."Age" ASC LIMIT 1')
    assert execute([PEOPLE], candidate.sql) == [("Bob", "France")]


def test_phase5_preserves_filter_on_row_superlative():
    candidate = best(
        "For people from France, show the name of the oldest person",
        [PEOPLE],
    )
    assert 'WHERE "people"."Country" = \'France\'' in candidate.sql
    assert candidate.sql.endswith('ORDER BY "people"."Age" DESC LIMIT 1')
    assert execute([PEOPLE], candidate.sql) == [("Alice",)]


def test_phase5_searches_explicit_top_n():
    candidate = best("Show the 2 youngest people names", [PEOPLE])
    assert candidate.sql.endswith('ORDER BY "people"."Age" ASC LIMIT 2')
    assert execute([PEOPLE], candidate.sql) == [("Bob",), ("Alice",)]


def test_phase5_distinguishes_limit_token_from_equal_filter_value():
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


def test_phase5_searches_frequency_superlative():
    candidate = best("Which country has the most people?", [PEOPLE])
    assert 'GROUP BY "people"."Country"' in candidate.sql
    assert candidate.sql.endswith("ORDER BY COUNT(*) DESC LIMIT 1")
    assert execute([PEOPLE], candidate.sql) == [("France",)]


def test_phase5_frequency_superlative_can_return_count():
    candidate = best(
        "List the country with the most people and how many people it has",
        [PEOPLE],
    )
    assert candidate.sql.startswith('SELECT "people"."Country", COUNT(*)')
    assert execute([PEOPLE], candidate.sql) == [("France", 2)]


def test_phase5_frequency_argmin_includes_zero_related_entities():
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


def test_phase5_returns_dual_lexical_extrema():
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


def test_phase5_searches_set_difference():
    candidate = best(
        "Show the stadium names without any concert",
        [STADIUM, CONCERT],
        STADIUM_FKS,
    )
    assert " EXCEPT SELECT " in candidate.sql
    assert execute([STADIUM, CONCERT], candidate.sql) == [("Gamma",)]


def test_phase5_guards_multi_aggregate_and_can_be_disabled():
    aggregate = best("What are the minimum and maximum age of people?", [PEOPLE])
    assert aggregate.sql == 'SELECT MIN("people"."Age"), MAX("people"."Age") FROM "people"'

    searcher = SQLSearcher.from_tables([PEOPLE], [])
    question = "Show the name and country of the youngest person"
    phase4 = searcher.search(question, phase5=False)
    phase5 = searcher.search(question, phase5=True)
    assert all("phase5:" not in evidence for candidate in phase4 for evidence in candidate.evidence)
    assert " LIMIT 1" not in phase4[0].sql
    assert " LIMIT 1" in phase5[0].sql


def test_phase6_features_are_schema_independent():
    age = ColumnRef("private_people", "SecretAge", SQLType.INTEGER)
    query = SelectQuery((SelectItem(Aggregate("MAX", age)),), "private_people")
    candidate = ScoredQuery(
        query,
        render_query(query),
        7.0,
        ("phase5:row-superlative",),
        (("aggregate_target:MAX:private_people.SecretAge", 3.0),),
    )
    features = learned_feature_vector("What is the maximum secret age?", candidate)
    assert "private_people" not in " ".join(features)
    assert "SecretAge" not in " ".join(features)
    assert features["ast.aggregate.MAX"] == 1.0
    assert features["heuristic_value.aggregate_target.max"] == 3.0


def test_phase6_model_round_trip_and_deterministic_rerank():
    count_query = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "people")
    rows_query = SelectQuery((SelectItem(Star()),), "people")
    candidates = [
        ScoredQuery(rows_query, render_query(rows_query), 10.0, ()),
        ScoredQuery(count_query, render_query(count_query), 9.0, ()),
    ]
    model = LinearRankerModel(
        {"match.count": 5.0, "baseline_score": 0.01},
        metadata={"fixture": True},
    )
    first = model.rerank("How many people are there?", candidates)
    second = model.rerank("How many people are there?", candidates)
    assert first[0].sql == render_query(count_query)
    assert [(candidate.sql, candidate.score) for candidate in first] == [
        (candidate.sql, candidate.score) for candidate in second
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = directory + "/ranker.json"
        model.save(path)
        loaded = LinearRankerModel.load(path)
    assert loaded.to_dict() == model.to_dict()
    assert loaded.rerank("How many people are there?", candidates)[0].sql == render_query(count_query)


def test_phase6_is_opt_in_at_search_boundary():
    searcher = SQLSearcher.from_tables([PEOPLE], [], max_candidates=20)
    baseline = searcher.search("How many people are there?")
    model = LinearRankerModel({"match.count": 1.0, "baseline_score": 1.0})
    learned = searcher.search("How many people are there?", rank_model=model)
    learned_again = searcher.search("How many people are there?", rank_model=model)
    assert all("phase6:" not in item for candidate in baseline for item in candidate.evidence)
    assert any("phase6:" in item for item in learned[0].evidence)
    assert learned[0].sql == model.rerank("How many people are there?", baseline)[0].sql
    assert [(candidate.sql, candidate.score) for candidate in learned] == [
        (candidate.sql, candidate.score) for candidate in learned_again
    ]


def test_phase6_tree_artifact_has_dependency_free_inference():
    tree = DecisionTree((
        TreeNode("match.count", 0.5, 1, 2),
        TreeNode(value=-1.0),
        TreeNode(value=2.0),
    ))
    model = TreeEnsembleRankerModel((tree,), learning_rate=0.25, base_score=0.5)
    assert model.score({"match.count": 0.0}) == 0.25
    assert model.score({"match.count": 1.0}) == 1.0
    with tempfile.TemporaryDirectory() as directory:
        path = directory + "/tree-ranker.json"
        model.save(path)
        loaded = load_ranker_model(path)
    assert isinstance(loaded, TreeEnsembleRankerModel)
    assert loaded.to_dict() == model.to_dict()


def test_public_sql_facade_exposes_stable_planner_contract():
    from engine import sql

    assert sql.SQLSearcher is SQLSearcher
    assert sql.SQLProposalModel is SQLProposalModel
    assert sql.SelectQuery is SelectQuery
    assert sql.load_ranker_model is load_ranker_model
    assert sql.ProfileSearchConfig().max_candidates == 32
    assert callable(sql.execute_and_rerank)
    assert callable(sql.profile_query)
    candidates = sql.SQLSearcher.from_tables([PEOPLE], []).search("list person names")
    assert candidates
    assert isinstance(candidates[0].query, sql.SelectQuery)
    planner = sql.DeterministicSQLPlanner(sql.SQLSearcher.from_tables([PEOPLE], []))
    assert planner.search("list person names")[0].sql == candidates[0].sql
    descriptors = schema_descriptors(planner.searcher.schema)
    assert descriptors[0].keys() == {"table", "name", "affinity", "is_date"}


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

        def ast_semantic_signals(self, question, sch, proposal_model=None, proposal_question_vector=None):
            return SemanticSignals.empty()

    previous = os.environ.get("PREREASONER_SQL_PLANNER")
    os.environ["PREREASONER_SQL_PLANNER"] = "ast"
    try:
        response = HermeticTableQuery().serve([PEOPLE], "list person names")
    finally:
        if previous is None:
            os.environ.pop("PREREASONER_SQL_PLANNER", None)
        else:
            os.environ["PREREASONER_SQL_PLANNER"] = previous
    assert response["valid"] is True
    assert response["error"] is None
    assert response["result"]["rows"] == [["Alice"], ["Bob"], ["Cara"]]
    assert response["candidate_count"] > 0
    assert response["ast"].startswith("SelectQuery(")
    assert response["model"].endswith("(ast)")
    assert compare_spider_rows([["1"], [None]], [[None], [1.0]])["strict"]
    assert not compare_spider_rows([[1, 2]], [[2, 1]])["strict"]
    assert not compare_spider_rows([[1, 1]], [[1]])["strict"]


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


def test_ast_proposal_split_is_deterministic_and_database_disjoint():
    database_ids = [f"db_{index}" for index in range(10)]
    first = split_database_ids(database_ids, validation_ratio=0.2, seed=1729)
    second = split_database_ids(reversed(database_ids), validation_ratio=0.2, seed=1729)
    train, validation = first
    assert first == second
    assert len(train) == 8
    assert len(validation) == 2
    assert not (set(train) & set(validation))
    assert set(train) | set(validation) == set(database_ids)


def test_ast_proposal_targets_keep_typed_filter_literals():
    metadata = {
        "table_names_original": ["people"],
        "column_names_original": [[-1, "*"], [0, "Age"]],
    }
    sql = {
        "select": [False, [[0, [0, [0, 1, False], None]]]],
        "from": {"table_units": [["table_unit", 0]], "conds": []},
        "where": [[False, 3, [0, [0, 1, False], None], 21, None]],
        "groupBy": [],
        "having": [],
        "orderBy": [],
        "limit": 5,
        "intersect": None,
        "union": None,
        "except": None,
    }
    assert extract_literal_targets(sql, metadata) == [
        {
            "clause": "where",
            "operator": ">",
            "column": "people.age",
            "value": 21,
            "value_type": "int",
            "negated": False,
        },
        {
            "clause": "limit",
            "operator": "limit",
            "column": None,
            "value": 5,
            "value_type": "int",
            "negated": False,
        },
    ]


def test_ast_proposal_contrasts_cover_targeted_same_profile_families():
    record = {
        "example_id": "library:00001",
        "db_id": "library",
        "split": "train",
        "question": "Which author has the fewest books?",
        "target": {
            "sketch": {
                "aggregate.COUNT": 2,
                "blocks": 1,
                "from_tables": 2,
                "group_items": 1,
                "joins": 1,
                "join_predicates": 1,
                "limit": 1,
                "order.asc": 1,
                "predicate.=": 1,
                "select_items": 2,
            },
            "tables": ["authors", "books"],
            "roles": {
                "projection": ["authors.id", "authors.name"],
                "group": ["authors.name"],
                "aggregate": ["books.id"],
                "order": ["books.id"],
            },
        },
    }
    metadata = {
        "table_names_original": ["Authors", "Books"],
        "column_names_original": [
            (-1, "*"), (0, "id"), (0, "name"),
            (1, "id"), (1, "author_id"), (1, "title"),
        ],
        "column_types": ["text", "number", "text", "number", "number", "text"],
    }
    contrast = contrast_record(record, metadata)
    families = {pair["family"] for pair in contrast["pairs"]}
    assert families == {
        "projection_identity",
        "frequency_extrema",
        "zero_inclusive_counts",
        "multi_table_role_binding",
    }
    assert contrast["profile"] == record["target"]["sketch"]
    assert all(pair["positive"] != pair["negative"] for pair in contrast["pairs"])
    for pair in contrast["pairs"]:
        assert pair["negative"] not in set(record["target"]["roles"].get(pair["role"], ()))


def test_sql_proposer_artifact_round_trip_is_deterministic():
    import numpy as np

    hidden = 3
    pair_size = hidden + len(PAIR_EXTRA_FEATURES)
    model = SQLProposalModel(
        sketch_names=("limit", "aggregate.COUNT"),
        role_names=("projection", "filter"),
        sketch_presence_weight=np.asarray([[1, 0, 0], [0, 1, 0]], dtype=np.float32),
        sketch_presence_bias=np.zeros(2, dtype=np.float32),
        sketch_count_weight=np.zeros((2, 2, hidden), dtype=np.float32),
        sketch_count_bias=np.asarray([[1, 0], [1, 0]], dtype=np.float32),
        sketch_profiles=({"limit": 1}, {"aggregate.COUNT": 1}),
        sketch_profile_weight=np.asarray([[1, 0, 0], [0, 1, 0]], dtype=np.float32),
        sketch_profile_bias=np.zeros(2, dtype=np.float32),
        table_weight=np.ones(pair_size, dtype=np.float32),
        table_bias=0.0,
        role_weight=np.ones((2, pair_size), dtype=np.float32),
        role_bias=np.zeros(2, dtype=np.float32),
        sketch_thresholds=np.asarray([0.6, 0.6], dtype=np.float32),
        metadata={"fixture": True, "maximum_feature_count": 4},
    )
    question = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
    candidate = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
    assert model.predict_sketch(question) == {"limit": 1}
    assert model.propose_sketches(question, limit=2) == (
        {"limit": 1},
        {"aggregate.COUNT": 1},
    )
    signals = semantic_signals_from_schema(
        model,
        "show name",
        ({"table": "items", "name": "name", "affinity": "TEXT"},),
        lambda texts: np.repeat(question[None, :], len(texts), axis=0),
        sketch_limit=1,
    )
    assert signals.sketch_profiles == ({"limit": 1},)
    assert set(signals.column_roles["projection"]) == {("items", "name")}
    assert len(pair_features("show name", question, "name", candidate)) == pair_size
    before = model.score_column_roles("show name", question, "name", "text", candidate)
    with tempfile.TemporaryDirectory() as directory:
        path = directory + "/proposer.json"
        model.save(path)
        loaded = SQLProposalModel.load(path)
    assert loaded.to_dict() == model.to_dict()
    assert loaded.predict_sketch(question) == model.predict_sketch(question)
    assert loaded.score_column_roles("show name", question, "name", "text", candidate) == before


def test_ranker_question_vector_cache_is_reused_and_fingerprinted():
    class FakeEncoder:
        def __init__(self):
            self.calls = 0

        def _encode(self, texts):
            self.calls += 1
            return np.asarray([[len(text), self.calls] for text in texts], dtype=np.float32)

    examples = ({"question": "one"}, {"question": "three"}, {"question": "seven"})
    with tempfile.TemporaryDirectory() as directory:
        path = directory + "/questions.npz"
        first = FakeEncoder()
        expected = load_or_encode_question_vectors(first, examples, "abc", path, batch_size=2)
        assert first.calls == 2
        second = FakeEncoder()
        actual = load_or_encode_question_vectors(second, examples, "abc", path, batch_size=2)
        assert second.calls == 0
        assert np.array_equal(actual, expected)
        try:
            load_or_encode_question_vectors(second, examples, "different", path, batch_size=2)
        except ValueError as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("stale question-vector cache was accepted")


def test_profile_promotion_gate_requires_calibrated_generated_margin():
    schema = SQLSearcher.from_tables([PEOPLE], []).schema
    name = next(column.ref for column in schema.columns if column.ref.name == "Name")
    age = next(column.ref for column in schema.columns if column.ref.name == "Age")
    fallback_query = SelectQuery((SelectItem(name),), "people")
    generated_query = SelectQuery((SelectItem(age),), "people")
    fallback = ScoredQuery(fallback_query, render_query(fallback_query), 0.0, ())
    generated = ScoredQuery(
        generated_query, render_query(generated_query), 2.0, ("profile-expand:1",),
        (("profile_binding_quality", 0.8),),
    )
    model = LinearRankerModel(
        {"baseline_score": 1.0}, metadata={"promotion_gate": {"margin_threshold": 1.0}}
    )
    assert rerank_with_promotion_gate(model, "show values", [fallback, generated])[0].sql == generated.sql
    strict = LinearRankerModel(
        {"baseline_score": 1.0}, metadata={"promotion_gate": {"margin_threshold": 3.0}}
    )
    assert rerank_with_promotion_gate(strict, "show values", [fallback, generated])[0].sql == fallback.sql
    ungated = LinearRankerModel({"baseline_score": 1.0})
    assert rerank_with_promotion_gate(ungated, "show values", [fallback, generated])[0].sql == fallback.sql


def test_promotion_gate_calibration_prefers_zero_loss_margin():
    model = LinearRankerModel({"signal": 1.0})
    groups = [
        {"candidates": [
            {"rank": 0, "sql": "base", "correct": False, "features": {}},
            {"rank": 1, "sql": "good", "correct": True, "features": {
                "signal": 1.1, "heuristic_value.profile_binding_quality": 0.8,
            }},
        ]},
        {"candidates": [
            {"rank": 0, "sql": "base", "correct": True, "features": {}},
            {"rank": 1, "sql": "bad", "correct": False, "features": {
                "signal": 0.6, "heuristic_value.profile_binding_quality": 0.7,
            }},
        ]},
    ]
    gate = calibrate_promotion_gate(groups, model)
    assert gate["margin_threshold"] == 0.75
    assert gate["wins"] == 1 and gate["losses"] == 0


TESTS = [
    test_typed_ast_rejects_invalid_aggregate,
    test_grouped_ast_rejects_ungrouped_ordering,
    test_typed_ast_rejects_mismatched_literal_payloads,
    test_ast_rejects_indeterminate_set_and_aggregate_shapes,
    test_grouping_validation_sees_ordered_aggregates,
    test_projection_filter_and_order,
    test_directional_year_filter_targets_date_column,
    test_multiple_aggregates_share_a_typed_operand,
    test_repeated_count_paraphrase_is_one_aggregate,
    test_phase2_ranks_count_distinct_above_grouped_count,
    test_phase2_coordinates_multiple_aggregate_operands,
    test_multi_hop_join_uses_bridge_table,
    test_grouped_count_uses_entity_display_column,
    test_filter_column_does_not_leak_into_projection,
    test_order_column_does_not_leak_into_projection,
    test_literals_are_escaped_by_renderer,
    test_grouped_topn_orders_by_aggregate_across_bridge,
    test_search_is_deterministic,
    test_encoder_role_signal_breaks_ambiguous_column_tie,
    test_proposed_sketch_profile_promotes_matching_typed_candidate,
    test_profile_beam_expands_missing_projection_binding,
    test_profile_beam_instantiates_grouped_frequency_shape,
    test_profile_expansion_caps_variants_and_penalizes_transformation,
    test_profile_expansion_preserves_hand_ranked_fallback_top,
    test_profile_fallback_applies_when_no_compatible_variant_exists,
    test_execution_rerank_penalizes_empty_candidate,
    test_phase3_ranks_set_query_by_both_operands,
    test_recursive_ast_scalar_subquery_executes,
    test_recursive_ast_correlated_exists_executes,
    test_recursive_ast_set_query_in_derived_table_executes,
    test_phase3_searches_scalar_average,
    test_phase3_searches_anti_membership,
    test_phase3_searches_route_self_join,
    test_phase3_searches_nested_count_aggregate,
    test_phase3_set_expansion_keeps_every_categorical_alternative,
    test_phase4_searches_cross_table_count_having,
    test_phase4_searches_single_table_count_having,
    test_phase4_searches_disjunction,
    test_phase4_disjoins_every_repeated_column_group,
    test_phase4_disjunction_deduplicates_entities_across_relation,
    test_phase4_disjunction_preserves_shared_official_filter,
    test_phase4_searches_filtered_scalar_minimum,
    test_phase4_keeps_grouped_superlative_as_aggregate,
    test_phase4_infers_high_confidence_missing_entity_fk,
    test_phase4_can_be_disabled_without_contaminating_phase3,
    test_phase5_searches_row_superlative,
    test_phase5_preserves_filter_on_row_superlative,
    test_phase5_searches_explicit_top_n,
    test_phase5_distinguishes_limit_token_from_equal_filter_value,
    test_phase5_searches_frequency_superlative,
    test_phase5_frequency_superlative_can_return_count,
    test_phase5_frequency_argmin_includes_zero_related_entities,
    test_phase5_returns_dual_lexical_extrema,
    test_phase5_searches_set_difference,
    test_phase5_guards_multi_aggregate_and_can_be_disabled,
    test_phase6_features_are_schema_independent,
    test_phase6_model_round_trip_and_deterministic_rerank,
    test_phase6_is_opt_in_at_search_boundary,
    test_phase6_tree_artifact_has_dependency_free_inference,
    test_public_sql_facade_exposes_stable_planner_contract,
    test_shared_spider_evaluation_contract,
    test_live_table_query_ast_mode_executes_typed_candidate,
    test_schema_graph_resolves_normalized_foreign_key_names,
    test_spider_evaluator_does_not_count_all_errors_as_answered,
    test_ast_failure_profiles_share_structural_and_schema_vocabulary,
    test_ast_failure_profiles_align_spider_and_typed_joins,
    test_ast_failure_diagnosis_separates_recall_and_linking_bottlenecks,
    test_ast_proposal_split_is_deterministic_and_database_disjoint,
    test_ast_proposal_targets_keep_typed_filter_literals,
    test_ast_proposal_contrasts_cover_targeted_same_profile_families,
    test_sql_proposer_artifact_round_trip_is_deterministic,
    test_ranker_question_vector_cache_is_reused_and_fingerprinted,
    test_profile_promotion_gate_requires_calibrated_generated_margin,
    test_promotion_gate_calibration_prefers_zero_loss_margin,
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
