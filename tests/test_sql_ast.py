"""Hermetic execution tests for deterministic SQL AST search and ranking.

Run: python -m tests.test_sql_ast
"""
from __future__ import annotations

from collections import Counter
import sqlite3
import sys
import tempfile

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
)
from engine.sql_search import SQLSearcher, SchemaGraph, ScoredQuery
from spider.probe.spider_eval import (
    compare as compare_spider_rows,
    recursive_gold_table_names,
    spider_foreign_keys,
)
from spider.probe.ast_eval import _score as score_spider_candidates


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
    assert sql.SelectQuery is SelectQuery
    assert sql.load_ranker_model is load_ranker_model
    assert callable(sql.execute_and_rerank)
    candidates = sql.SQLSearcher.from_tables([PEOPLE], []).search("list person names")
    assert candidates
    assert isinstance(candidates[0].query, sql.SelectQuery)


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
    assert compare_spider_rows([["1"], [None]], [[None], [1.0]])["strict"]
    assert not compare_spider_rows([[1, 2]], [[2, 1]])["strict"]
    assert not compare_spider_rows([[1, 1]], [[1]])["strict"]


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
    test_execution_rerank_penalizes_empty_candidate,
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
    test_phase5_searches_set_difference,
    test_phase5_guards_multi_aggregate_and_can_be_disabled,
    test_phase6_features_are_schema_independent,
    test_phase6_model_round_trip_and_deterministic_rerank,
    test_phase6_is_opt_in_at_search_boundary,
    test_phase6_tree_artifact_has_dependency_free_inference,
    test_public_sql_facade_exposes_stable_planner_contract,
    test_shared_spider_evaluation_contract,
    test_spider_evaluator_does_not_count_all_errors_as_answered,
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
