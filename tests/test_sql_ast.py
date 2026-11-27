"""Hermetic tests for the typed SQL AST (validation, rendering, execution), SQL import, the lexical readings the
planner's helpers keep, and the plumbing of the one own-data selection (engine/tables.py:select_query): the request's
grids, the Decisions path's candidates as the pool, the per-answer decision as the served, warned or asked reply, the
length rule, the chosen program on both deterministic emitters, and the one request database.

The Decisions path's stock reader is a fake whose vectors come from a text's hash and its API a fake transport that
answers each closed question by the test's rule (tests/test_answer.py): nothing reaches a network or a model file.

Run: python -m tests.test_sql_ast
"""
from __future__ import annotations

from collections import Counter
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
    LongestRun,
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
from engine.sql_candidate import ScoredQuery
from engine.sql_rank import PoolSelection, analyze_question
from engine.sql_schema import SchemaGraph
from regress.sql_import import import_sql, normalize_decoded_sql
from spider.probe.evalutil import run_with_budget
from engine.tables import PROGRAM_MODEL, TableQuery
from spider.probe.spider_eval import (
    compare as compare_spider_rows,
    record_integrated_result,
    recursive_gold_table_names,
    spider_foreign_keys,
)
from spider.probe.evalutil import _score as score_spider_candidates


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


def test_star_is_only_a_projection_or_count_operand():
    age = ColumnRef('people', 'Age', SQLType.INTEGER)
    projection = (SelectItem(age),)
    invalid = (
        SelectQuery(projection, 'people', where=Comparison(Star(), '=', Literal(1))),
        SelectQuery(projection, 'people', where=Comparison(age, '=', Star())),
        SelectQuery(projection, 'people', where=InPredicate(Star(), (Literal(1),))),
        SelectQuery(projection, 'people', where=InPredicate(age, (Star(),))),
        SelectQuery(projection, 'people', order_by=(OrderTerm(Star(), 'ASC'),)),
        SelectQuery((SelectItem(Star(), 'all_columns'),), 'people'),
        SelectQuery((SelectItem(Aggregate('COUNT', Star())),), 'people',
                    having=Comparison(Star(), '>', Literal(0))),
    )
    for query in invalid:
        try:
            render_query(query)
        except ASTValidationError as exc:
            assert 'star' in str(exc), exc
        else:
            raise AssertionError(f'bare star reached invalid SQL context: {query!r}')
    assert execute([PEOPLE], render_query(SelectQuery((SelectItem(Star()),), 'people'))) == [
        tuple(row) for row in PEOPLE['rows']]
    assert execute([PEOPLE], render_query(SelectQuery((SelectItem(Aggregate('COUNT', Star())),), 'people'))) == [(3,)]


def test_evaluator_shards_are_disjoint_complete_and_merge_only_matching_checkpoints():
    import copy
    from spider.probe.full_eval import _dependency_fingerprints, _merge_checkpoints, _shard_indices
    picked = [2, 4, 7, 9, 12, 15, 19]
    contract = {'picked': picked, 'shard': None, 'selection': 'served', 'dependencies': {'reader': 'a' * 64},
                'artifacts': {'dev': 'questions', 'weight_bundle': 'weights', 'source': 'code'}}
    shards = []
    for i in range(3):
        shard = f'{i}/3'; indices = _shard_indices(picked, shard)
        shards.append({'contract': dict(contract, picked=indices, shard=shard),
                       'records': [{'idx': index, 'ok': index % 2 == 0} for index in indices]})
    merged = _merge_checkpoints(contract, shards[::-1])
    assert sorted(merged) == picked
    assert sum(r['ok'] for r in merged.values()) == sum(i % 2 == 0 for i in picked)
    assert _shard_indices(picked, None) == picked
    invalid = [shards[:2], shards + [shards[0]]]
    for change in ['weights', 'denominator', 'gap', 'duplicate', 'selection', 'injected_model']:
        broken = copy.deepcopy(shards)
        if change == 'weights': broken[0]['contract']['artifacts']['weight_bundle'] = 'other'
        elif change == 'denominator': broken[0]['contract']['picked'] = picked
        elif change == 'gap': broken[0]['records'].pop()
        elif change == 'duplicate': broken[0]['records'].append(broken[0]['records'][0])
        elif change == 'injected_model': broken[0]['contract']['dependencies']['reader'] = 'b' * 64
        else: broken[0]['contract']['selection'] = 'pool_oracle'
        invalid.append(broken)
    for checkpoints in invalid:
        try: _merge_checkpoints(contract, checkpoints)
        except ValueError: pass
        else: raise AssertionError('incomplete or mismatched shard merge accepted')
    for shard in ['1/0', '3/3', '-1/3', 'abc', '1/2/3']:
        try: _shard_indices(picked, shard)
        except ValueError: pass
        else: raise AssertionError('invalid shard accepted')
    assert _dependency_fingerprints(['reader=' + 'a' * 64]) == {'reader': 'a' * 64}
    for items in [['bad/name=' + 'a' * 64], ['reader=short'], ['x=' + 'a' * 64] * 2]:
        try: _dependency_fingerprints(items)
        except ValueError: pass
        else: raise AssertionError('invalid dependency fingerprint accepted')


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


def test_a_longest_run_renders_alone_and_counts_unbroken_runs():
    """The longest run (engine/program LONGEST_RUN, "how many consecutive wins") renders as gaps and islands over the
    rows its WHERE reads, ordered by a number column; it is selected alone, and a row its predicate does not keep, or
    reads as unknown, ends a run."""
    import sqlite3

    line, result = ColumnRef("games", "line", SQLType.INTEGER), ColumnRef("games", "result", SQLType.TEXT)
    won = Comparison(result, "=", Literal("w", SQLType.TEXT))
    query = SelectQuery((SelectItem(LongestRun(won, line), "value"),), "games",
                        where=Comparison(ColumnRef("games", "kind", SQLType.TEXT), "=", Literal("game", SQLType.TEXT)))
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE games (line INTEGER, kind TEXT, result TEXT)")
    played = [("game", "w"), ("game", "w"), ("game", "l"), ("note", "w"), ("game", "w"), ("game", "w"), ("game", "w"), ("game", None), ("game", "w")]
    connection.executemany("INSERT INTO games VALUES (?, ?, ?)", [(at, kind, outcome) for at, (kind, outcome) in enumerate(played)])
    assert connection.execute(render_query(query)).fetchall() == [(3,)]
    connection.execute("DELETE FROM games")
    assert connection.execute(render_query(query)).fetchall() == [(0,)]
    for wrong in (SelectQuery((SelectItem(LongestRun(won, line), "value"), SelectItem(line)), "games"),
                  SelectQuery((SelectItem(LongestRun(won, line), "value"),), "games", limit=1, order_by=(OrderTerm(line),)),
                  SelectQuery((SelectItem(LongestRun(won, result), "value"),), "games")):
        try:
            validate_query(wrong)
        except ASTValidationError:
            continue
        raise AssertionError(f"accepted {wrong}")


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


def _hermetic_planner():
    """The production TableQuery without its encoder: ``schema`` types columns from their values. Its selection is
    production's, the Decisions path: a test reads it with the fake reader and transport (``_decisions``)."""
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

    return HermeticPlanner()


def _request(planner, tables=None):
    """(norm, fks, sch, tablemap) for one request's tables, prepared the way serve() prepares them."""
    norm, fks = planner.ingest(tables or [PEOPLE])
    sch, _, tablemap = planner.schema(norm, fks)
    return norm, fks, sch, tablemap


SALES = {"name": "sales", "columns": ["region", "year", "amount", "note"], "rows": [
    ["East", 2023, 100, ""], ["East", 2024, 250, ""], ["West", 2024, 300, ""], ["West", 2025, 50, ""]]}


def test_a_duration_is_how_long_rows_lasted_not_a_quantity_and_an_interval():
    """"how many users wanted neartail for greater than 6 months" read "6 months" as Interval = 'month' and
    Quantity > 6 (2026-10-05). A comparison word, a number and a unit compare how long each row lasted: the days
    from its start date to its end date, or to the question's date while it runs (engine/sql_durations.py,
    sql_ast.DateSpan)."""
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

    def phrase(question):
        return [(found.operator, found.amount, found.unit, found.ago)
                for found in duration_phrases(question, tokens(question))]

    assert phrase("how many subscriptions lasted more than 6 months") == [(">", 6, "month", False)]
    assert phrase("how many subscriptions started more than a year ago") == [(">", 1, "year", True)]
    # Same profile, no unit: a quantity is still a quantity; and a window is no duration.
    assert phrase("how many subscriptions with quantity more than 6") == []
    assert duration_phrases("orders in the last 6 months", tokens("orders in the last 6 months")) == ()

    _, fks, sch, tablemap = _request(planner, [subscriptions])
    column = SchemaGraph.from_planner(sch, fks).column_map

    def counted(end, unit, amount):
        span = DateSpan(column[("subscriptions", "Start Date (UTC)")].ref,
                        column[("subscriptions", end)].ref if end else None, unit, today.isoformat())
        query = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "subscriptions",
                            where=Comparison(span, ">", Literal(amount, SQLType.INTEGER)))
        validate_query(query)
        return planner.execute(tablemap, sch, render_query(query), query=query)[1]

    assert counted("Ended At (UTC)", "month", 6) == [(sum(lasted(row) / 30.4375 > 6
                                                          for row in subscriptions["rows"]),)] == [(3,)]
    # "ago" runs from the date to the question's date, whatever the row's end.
    assert counted(None, "year", 1) == [(sum((today - date.fromisoformat(row[5][:10])).days / 365.25 > 1
                                             for row in subscriptions["rows"]),)] == [(3,)]


# complex-unsold-products: 'Lyon' occurs only in purchases.city.
PURCHASES = {
    "name": "purchases",
    "columns": ["purchase_id", "customer_name", "city", "product_name"],
    "rows": [[1, "Alice", "Paris", "Alpha"], [2, "Alice", "Paris", "Beta"],
             [3, "Bob", "Lyon", "Gamma"], [4, "Cara", "Paris", "Gamma"],
             [5, "Dan", "Berlin", "Delta"], [6, "Eve", "Paris", "Alpha"]],
}


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


# A customer's keyword-planner tab (2026-10-06), cut down: 'inspection checklist' is one keyword, and three others
# hold it inside them (10,550 searches in all).
KEYWORDS = {"name": "Checklist", "columns": ["Keyword", "Avg. monthly searches", "Competition"], "rows": [
    ["inspection checklist", 500, "Low"], ["home inspection checklist", 5000, "Low"],
    ["house inspection checklist", 5000, "High"], ["roof inspection checklist", 50, "Low"],
    ["fire extinguisher inspection list", 5000, "High"], ["forklift training", 500, "Medium"]]}


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


def test_a_decimal_comma_amount_is_not_totalled_as_hundreds():
    """A release review (2026-10-07): ingestion dropped every comma, so amounts written "1,50", "12,34" and
    "2,00" totalled 1584 instead of 15.84. A comma is read only where it groups digits, so such a column stays
    text and is not summed (the planner types a column numeric only when its cells are stored as numbers,
    engine/numeric.stored_as_numbers); amounts grouped in thousands, or in Indian lakhs, still total."""
    from engine.numeric import stored_as_numbers
    from engine.tables import csv_table

    def total(csv):
        amounts = [row[1] for row in csv_table(csv, "sales")["rows"]]
        return sum(amounts) if stored_as_numbers(amounts) else None

    assert total('item,amount\nA,"1,50"\nB,"12,34"\nC,"2,00"\n') is None
    assert total('item,amount\nA,"1,500"\nB,"12,340"\nC,"2,000"\n') == 15840
    assert total('item,amount\nA,"1,23,456"\nB,"12,34,567"\n') == 1358023
    # A column mixing the formats is not totalled from its grouped cells alone.
    assert total('item,amount\nA,"1,234"\nB,"1,50"\n') is None


def test_join_keys_are_read_as_the_executed_join_compares_them():
    """A release review (2026-10-07). Foreign-key discovery compared keys without case, while the join it planned
    compares them exactly: orders keyed ABC, ABC, DEF were linked to customers keyed abc and def, and "total
    amount by customer name" was served as no rows. Customers keyed ABC and abc, two keys to the join, were no
    key at all. Discovery now reads a key as the join does."""
    from decimal import Decimal

    from engine.relations import discover_fks, join_keys
    from engine.tables import csv_table
    planner = _hermetic_planner()
    orders = {"name": "orders", "columns": ["order_id", "customer_id", "amount"],
              "rows": [[1, "ABC", 10], [2, "ABC", 20], [3, "DEF", 30]]}
    by_name = ("SELECT customers.name, SUM(orders.amount) FROM orders JOIN customers "
               "ON orders.customer_id = customers.customer_id GROUP BY customers.name")

    def joined(tables):
        """The total by customer name over the discovered key, run by the production executor; None unlinked."""
        if not discover_fks(tables):
            return None
        _, fks, sch, tablemap = _request(planner, tables)
        query = import_sql(by_name, SchemaGraph.from_planner(sch, fks))
        return sorted(map(tuple, planner.execute(tablemap, sch, render_query(query), query=query)[1]))

    def customers(*rows):
        return {"name": "customers", "columns": ["customer_id", "name"], "rows": [list(row) for row in rows]}

    def answer(table):
        edges = [(e["from_table"], e["from_col"], e["to_table"], e["to_col"]) for e in discover_fks([orders, table])]
        return edges, joined([orders, table])

    linked = [("orders", "customer_id", "customers", "customer_id")]
    assert answer(customers(("abc", "Ann"), ("def", "Bo"))) == ([], None)
    assert answer(customers(("ABC", "Ann"), ("DEF", "Bo"))) == (linked, [("Ann", 30), ("Bo", 30)])
    assert answer(customers(("ABC", "Ann"), ("abc", "Cy"), ("DEF", "Bo"))) == (linked, [("Ann", 30), ("Bo", 30)])
    # In a column of numbers a key is its exact magnitude whatever its type, distinct however large or precise.
    assert len(set(join_keys([1001, "1001", 1001.0, Decimal("1001.00")]))) == 1
    assert len(set(join_keys([2**53, 2**53 + 1, 10**37, 10**37 + 1, Decimal("0.1"),
                              Decimal("0.10000000000000001")]))) == 6
    # In a column that holds text, every cell is stored as its text and joins on it (revision 2 of the review):
    # orders keyed 1.0, 1.0, 2.0 and ABC were linked to customers keyed 1, 2 and ABC, and the join kept only ABC.
    assert join_keys(["1.0", "1", "Abc", "abc", "  ", None]) == ["1.0", "1", "Abc", "abc", None, None]
    assert join_keys([1, "ABC"]) == ["1", "ABC"] and join_keys([1, 2]) != join_keys(["1", "2", "x"])[:2]
    mixed_orders = csv_table("customer_id,amount\n1.0,10\n1.0,20\n2.0,30\nABC,40\n", "orders")
    for keys, expected in (("1,Alice\n2,Bob\nABC,Chris", None),
                           ("1.0,Alice\n2.0,Bob\nABC,Chris", [("Alice", 30), ("Bob", 30), ("Chris", 40)])):
        mixed_customers = csv_table("customer_id,name\n" + keys + "\n", "customers")
        edges = discover_fks([mixed_orders, mixed_customers])
        result = joined([mixed_orders, mixed_customers])
        assert bool(edges) == bool(expected) and result == expected, (keys, edges, result)


def test_an_explicit_zero_foreign_key_confidence_stays_zero():
    """`conf or 1.0` read an explicit confidence of 0.0 as full confidence (release review, 2026-10-07).
    Only an absent confidence defaults to 1.0."""
    tables = [{"name": "a", "columns": ["id", "b_id"], "rows": [[1, 1], [2, 1]]},
              {"name": "b", "columns": ["id"], "rows": [[1]]}]

    def confidence(**stated):
        edge = {"from_table": "a", "from_col": "b_id", "to_table": "b", "to_col": "id", **stated}
        return SchemaGraph.from_tables(tables, [edge]).foreign_keys[0].confidence

    assert confidence(conf=0.0) == 0.0 and confidence(confidence=0.4) == 0.4 and confidence() == 1.0


def test_coordinated_aggregate_words_share_the_field_after_them():
    """Spider DEV 4, 17, 2026-10-13: "the average, minimum, and maximum age of all singers from France" was refused as no
    average of stadium.Average, a field named for the word, though the query averaged Age: the words coordinated after
    "average" share the field after them (sql_rank.aggregate_operand). "The highest average attendance" names no
    Average operand."""
    from engine.sql_rank import aggregate_operand
    names = ["Stadium_ID", "Name", "Capacity", "Average", "Singer_ID", "Country", "Age"]
    assert aggregate_operand("What is the average, minimum, and maximum age of all singers from France?", names,
                             "AVG") == "Age"
    assert aggregate_operand("the stadium with the highest average attendance", ["Average", "Capacity"], "AVG") is None


def test_a_field_is_named_by_another_form_of_its_word():
    """Spider DEV 958, 861, held-out Spider train 920, 2026-10-13: "the arriving date and the departing date of the dogs"
    showed no date_arrived or date_departed, and "years of founding" no Year_of_Founded: a word of a field's name said in
    another form names the field (sql_expansion.says_in_another_form). A word naming an operation names no field
    ("ordered" by an Orders field), and a short shared start is no shared stem ("named" is not "name")."""
    from engine.sql_expansion import says_in_another_form
    assert says_in_another_form("arriving", "arrived") and says_in_another_form("departure", "departed")
    assert says_in_another_form("directors", "directed") and says_in_another_form("settlement", "settled")
    assert not says_in_another_form("ordered", "order") and not says_in_another_form("listed", "listing")
    assert not says_in_another_form("named", "name") and not says_in_another_form("country", "count")
    # A "d" alone ends only a past form: "amenity" says no amenid (held-out 531).
    assert not says_in_another_form("amenity", "amenid") and says_in_another_form("created", "create")


def test_a_field_named_with_its_tables_abbreviation():
    """Held-out Spider train, 2026-10-13: 19 questions asked for cust_name, stu_gpa, crs_description, dept_name or
    apt_number by their other words ("the name of customers", "their apartment numbers") and none was shown: a field's
    first word abbreviating its own table's name is that table's word, as "customer" of customer_name is
    (sql_expansion.abbreviates). A word that abbreviates no word of its table still has to be said."""
    from engine.sql_expansion import abbreviates
    assert abbreviates("cust", "customer") and abbreviates("crs", "course") and abbreviates("dept", "department")
    assert not abbreviates("acc", "customer") and not abbreviates("cs", "course") and not abbreviates("stu", "course")


def test_an_explicit_list_names_the_outputs_their_owner_and_order():
    """Spider DEV 89, 149, 2026-10-14: "How many countries does each continent have? List the continent id, continent
    name and the number of countries." showed a car maker's Country beside the continent and the count first, and "List
    full name and the id" of makers showed a model's ModelId. An explicit list instruction names the outputs, in its
    order (sql_expansion.output_requests): one owner's fields unless an item names another (request_fields). A
    question with no such sentence lists nothing."""
    from engine.sql_expansion import output_requests, request_fields, tokens
    question = ("How many countries does each continent have? List the continent id, continent name and the number "
                "of countries.")
    said = tokens(question)
    assert [(" ".join(said[r.start:r.end]), r.count) for r in output_requests(question)] == [
        ("continent id", False), ("continent name", False), ("number of country", True)]
    assert output_requests("What are the names of the teachers?") == ()
    assert [" ".join(tokens(q)[r.start:r.end]) for q in ("Find the program which most students are enrolled in. List "
                                                         "both the id and the summary.",)
            for r in output_requests(q)] == ["id", "summary"]
    continents = {"name": "continents", "columns": ["ContId", "Continent"], "rows": [[1, "america"], [2, "europe"]]}
    countries = {"name": "countries", "columns": ["CountryId", "CountryName", "Continent"],
                 "rows": [[1, "usa", 1], [2, "germany", 2], [3, "france", 2]]}
    makers = {"name": "car_makers", "columns": ["Id", "Maker", "FullName", "Country"],
              "rows": [[1, "amc", "American Motor Company", 1], [2, "vw", "Volkswagen", 2]]}
    models = {"name": "model_list", "columns": ["ModelId", "Maker", "Model"], "rows": [[1, 1, "amc"], [2, 2, "golf"]]}
    graph = SchemaGraph.from_tables([continents, countries, makers, models], ())
    ref = {(column.ref.table, column.ref.name): column.ref for column in graph.columns}
    # The continent id is ContId, not the countries' CountryId; the list's owner is the continents table.
    assert request_fields(question, graph) == ((ref["continents", "ContId"],), (ref["continents", "Continent"],))
    assert request_fields("Which makers designed more than 3 car models? List full name and the id.", graph) == (
        (ref["car_makers", "FullName"],), (ref["car_makers", "Id"],))
    # A "number" naming a field asks for the field (held-out Spider train 47), and a foreign key gives way to the key it
    # references: the club id of a count of coaches per club is the club's (fresh holdout 119, 2026-10-14).
    assert [r.count for r in output_requests("Which patients made more than one appointment? Tell me the name and "
                                             "phone number of these patients.")] == [False, False]
    clubs = {"name": "club", "columns": ["Club_ID", "Club_name"], "rows": [[1, "Ajax"], [2, "Benfica"]]}
    coaches = {"name": "coach", "columns": ["Coach_ID", "Coach_name", "Club_ID"],
               "rows": [[1, "Ann", 1], [2, "Bo", 1], [3, "Cy", 2]]}
    clubs_graph = SchemaGraph.from_tables([clubs, coaches], [("coach", "Club_ID", "club", "Club_ID")])
    club = {(column.ref.table, column.ref.name): column.ref for column in clubs_graph.columns}
    assert request_fields("How many coaches does each club has? List the club id, name and the number of coaches.",
                          clubs_graph) == ((club["club", "Club_ID"],), (club["club", "Club_name"],))


def test_a_list_item_that_is_only_the_count_asks_for_the_count():
    """Spider DEV 105 (2026-10-14): "How many car makers are there in each continents? List the continent name and the
    count." showed both the countries' and the continents' Continent: "the count" asked no field, so no list was
    read. An item that is only "count" asks for the count (sql_expansion.output_requests); "the count of cars" too."""
    from engine.sql_expansion import output_requests, tokens
    question = "How many car makers are there in each continents? List the continent name and the count."
    said = tokens(question)
    assert [(" ".join(said[r.start:r.end]), r.count) for r in output_requests(question)] == [
        ("continent name", False), ("count", True)]
    assert [r.count for r in output_requests("Which shops sell shoes? List the shop name and the count of shoes.")] == [
        False, True]


def test_the_opening_sentence_lists_its_coordinated_outputs():
    """Spider DEV 33, 34, 800, 950 (2026-10-14): "What are the names, themes, and number of singers for every concert?"
    showed a theme and a count without the concert's name; "the names and areas of countries" no area. A correct-binding
    experiment that supplied the gold outputs through the list reading served 48 more DEV answers. The opening sentence's
    two or more coordinated outputs are read like a later list instruction's (sql_expansion.output_requests): the list
    ends where its rows begin ("of countries"), the owner is the table realizing most items, and within it an item is
    the field named exactly by it (Name, not LocalName). A single output, or none, lists nothing."""
    from engine.sql_expansion import output_requests, request_fields, tokens
    question = "What are the names, themes, and number of singers for every concert?"
    said = tokens(question)
    assert [(" ".join(said[r.start:r.end]), r.count) for r in output_requests(question)] == [
        ("name", False), ("theme", False), ("number of singer", True)]
    assert output_requests("What are the names of the singers?") == ()
    assert output_requests("What is the average age of all singers?") == ()
    said = tokens("List each owner's first name, last name, and the size of their dog.")
    assert [" ".join(said[r.start:r.end]) for r in output_requests(
        "List each owner's first name, last name, and the size of their dog.")] == ["owner first name", "last name", "size"]
    concerts = {"name": "concert", "columns": ["concert_ID", "concert_Name", "Theme"],
                "rows": [[1, "Auditions", "Free choice"], [2, "Super bootcamp", "Bleeding Love"]]}
    singers = {"name": "singer", "columns": ["Singer_ID", "Name"], "rows": [[1, "Joe"], [2, "Tribal King"]]}
    roster = {"name": "singer_in_concert", "columns": ["concert_ID", "Singer_ID"], "rows": [[1, 1], [1, 2], [2, 2]]}
    graph = SchemaGraph.from_tables([concerts, singers, roster], [("singer_in_concert", "concert_ID", "concert",
                                                                   "concert_ID"),
                                                                  ("singer_in_concert", "Singer_ID", "singer",
                                                                   "Singer_ID")])
    ref = {(column.ref.table, column.ref.name): column.ref for column in graph.columns}
    # The owner is the concert, which realizes both items; its name is concert_Name, not a singer's Name.
    assert request_fields("Show the name and theme for all concerts and the number of singers in each concert.",
                          graph) == ((ref["concert", "concert_Name"],), (ref["concert", "Theme"],))
    countries = {"name": "country", "columns": ["Code", "Name", "LocalName", "SurfaceArea"],
                 "rows": [["AGO", "Angola", "Angola", 1246700.0], ["CHN", "China", "Zhongquo", 9572900.0]]}
    cities = {"name": "city", "columns": ["ID", "Name", "CountryCode"], "rows": [[1, "Luanda", "AGO"]]}
    world = SchemaGraph.from_tables([countries, cities], [("city", "CountryCode", "country", "Code")])
    place = {(column.ref.table, column.ref.name): column.ref for column in world.columns}
    assert request_fields("What are the names and areas of countries with the top 5 largest area?", world) == (
        (place["country", "Name"],), (place["country", "SurfaceArea"],))
    # Within the owner its own foreign key is its field (DEV 319: Templates.Template_Type_Code, not only the referenced
    # type table's key, which holds the same values), and the table named right after the items owns them where they
    # tie (DEV 523: the sections).
    templates = {"name": "Templates", "columns": ["Template_ID", "Version_Number", "Template_Type_Code"],
                 "rows": [[1, 5, "PP"], [2, 7, "BK"]]}
    types = {"name": "Ref_Template_Types", "columns": ["Template_Type_Code", "Template_Type_Description"],
             "rows": [["PP", "Paper"], ["BK", "Book"]]}
    docs = SchemaGraph.from_tables([templates, types], [("Templates", "Template_Type_Code", "Ref_Template_Types",
                                                         "Template_Type_Code")])
    doc = {(column.ref.table, column.ref.name): column.ref for column in docs.columns}
    assert request_fields("Show template ids, version numbers, and template type codes for all templates.", docs) == (
        (doc["Templates", "Template_ID"],), (doc["Templates", "Version_Number"],),
        (doc["Templates", "Template_Type_Code"], doc["Ref_Template_Types", "Template_Type_Code"]))
    courses = {"name": "Courses", "columns": ["course_id", "course_name", "course_description"],
               "rows": [[1, "math", "numbers"], [2, "art", "paint"]]}
    sections = {"name": "Sections", "columns": ["section_id", "course_id", "section_name", "section_description"],
                "rows": [[1, 1, "a", "first"], [2, 2, "b", "second"]]}
    school = SchemaGraph.from_tables([courses, sections], [("Sections", "course_id", "Courses", "course_id")])
    room = {(column.ref.table, column.ref.name): column.ref for column in school.columns}
    assert request_fields("What are the names and descriptions of all the sections?", school) == (
        (room["Sections", "section_name"],), (room["Sections", "section_description"],))
    # "of" inside an item that other items follow does not end the list (held-out Spider train 113), the owner's own
    # key is its "id" before a foreign key, and items two tables realize alike with neither named read no list (529).
    said = tokens("Show the id, the date of account opened, the account name, and other account detail for all "
                  "accounts.")
    assert [" ".join(said[r.start:r.end]) for r in output_requests(
        "Show the id, the date of account opened, the account name, and other account detail for all accounts.")] == [
        "id", "date of account opened", "account name", "other account detail"]
    accounts = {"name": "Accounts", "columns": ["account_id", "customer_id", "account_name"],
                "rows": [[1, 7, "main"], [2, 8, "spare"]]}
    customers = {"name": "Customers", "columns": ["customer_id", "customer_name"], "rows": [[7, "Ann"], [8, "Bo"]]}
    bank = SchemaGraph.from_tables([accounts, customers], [("Accounts", "customer_id", "Customers", "customer_id")])
    vault = {(column.ref.table, column.ref.name): column.ref for column in bank.columns}
    assert request_fields("Show the id and the account name for all accounts.", bank) == (
        (vault["Accounts", "account_id"],), (vault["Accounts", "account_name"],))
    students = {"name": "Student", "columns": ["StuID", "Fname", "LName"], "rows": [[1, "Ann", "Lee"]]}
    faculty = {"name": "Faculty", "columns": ["FacID", "Fname", "Lname"], "rows": [[1, "Bo", "Ray"]]}
    campus = SchemaGraph.from_tables([students, faculty], [])
    assert request_fields("Show the first name and last name for all the instructors.", campus) == ()
    # Of tied tables, the one the question names owns the items (held-out Spider train 903).
    person = {(column.ref.table, column.ref.name): column.ref for column in campus.columns}
    assert request_fields("Which student has the longest name? Show the first name and last name.", campus) == (
        (person["Student", "Fname"],), (person["Student", "LName"],))
    # One named only after the list, in its condition, does not (fresh holdout 654).
    assert request_fields("What are the first name and last name of those who advise a student?", campus) == ()


def test_a_list_verb_after_a_comma_lists_the_outputs():
    """Spider DEV 274, 970 (2026-10-15): "Sort all the shops by number products in descending order, and return the
    name, location and district of each shop." was refused, every reading showing every field, and "List his or her
    role, street, city and state." listed the fields in no order asked. A clause a list verb opens after a comma lists
    the outputs as a sentence does (sql_expansion.instruction_starts), and a possessive before an item asks nothing.
    Negative: a comma before no list verb opens no list."""
    from engine.sql_expansion import instruction_starts, output_requests, request_fields, tokens

    def items(question):
        said = tokens(question)
        return [(" ".join(said[r.start:r.end]), r.count) for r in output_requests(question)]

    sort = "Sort all the shops by number products in descending order, and return the name, location and district of "
    assert items(sort + "each shop.") == [("name", False), ("location", False), ("district", False)]
    assert items("For each language, list the number of TV Channels that use it.") == [("number of tv channel", True)]
    assert items("Which professionals live in a city containing the substring 'West'? List his or her role, street, "
                 "city and state.") == [("role", False), ("street", False), ("city", False), ("state", False)]
    assert items("Which dogs are of the rarest breed? Show their names and treatment dates.") == [
        ("name", False), ("treatment date", False)]
    # The possessive opening the items names their owner, the continent named before them (DEV 90), not the
    # countries whose fields say "id" and "name" more nearly. Without a possessive the countries own them.
    question = "For each continent, list its id, name, and how many countries it has?"
    assert instruction_starts(question) == (0, 3)
    countries = {"name": "countries", "columns": ["CountryId", "CountryName", "Continent"],
                 "rows": [[1, "France", 10], [2, "Germany", 10]]}
    continents = {"name": "continents", "columns": ["ContId", "Continent"], "rows": [[10, "Europe"]]}
    world = SchemaGraph.from_tables([countries, continents], [("countries", "Continent", "continents", "ContId")])
    place = {(column.ref.table, column.ref.name): column.ref for column in world.columns}
    assert request_fields(question, world) == ((place["continents", "ContId"],), (place["continents", "Continent"],))
    assert request_fields("For each continent, list the id and name of the countries.", world) == (
        (place["countries", "CountryId"],), (place["countries", "CountryName"],))
    # Negative: a comma that no list verb follows opens nothing.
    assert items("For each shop, how many employees are there?") == []
    assert instruction_starts("For each shop, how many employees are there?") == (0,)


def test_along_with_adds_an_item_to_a_list():
    """Spider DEV 94, 534, 946 (2026-10-15): "What is the full name of each car maker, along with its id and how many
    models it produces?" read no list: "along with" joined nothing, and "of each car maker" ended the items. "Along
    with", "together with" and "as well as" add an item as "and" does, even past the rows the items before them name,
    and "different" before an item asks no field. Negative: a list that nothing continues past its rows still ends
    there."""
    from engine.sql_expansion import output_requests, request_fields, tokens

    def items(question):
        said = tokens(question)
        return [(" ".join(said[r.start:r.end]), r.count) for r in output_requests(question)]

    question = "What is the full name of each car maker, along with its id and how many models it produces?"
    assert items(question) == [("full name", False), ("id", False), ("how many model it produce", True)]
    assert items("What are the first, middle, and last names, along with the ids, of all students who enrolled in 2 "
                 "degree programs in one semester?") == [
        ("first", False), ("middle", False), ("last name", False), ("id", False)]
    assert items("List the date of each treatment, together with the first name of the professional who operated "
                 "it.") == [("date", False), ("first name", False)]
    assert items("Show different occupations along with the number of players in each occupation.") == [
        ("occupation", False), ("number of player", True)]
    # "And" before a possessive adds an item past the rows too (fresh holdout 745: no track name was shown).
    assert items("Show the name and date for each race and its track name.") == [
        ("name", False), ("date", False), ("track name", False)]
    # Negative: "and" after the rows the items name adds nothing; the list ends at "for all concerts".
    assert items("Show the name and theme for all concerts and the number of singers in each concert.") == [
        ("name", False), ("theme", False)]
    makers = {"name": "car_makers", "columns": ["Id", "Maker", "FullName", "Country"],
              "rows": [[1, "amc", "American Motor Company", 1], [2, "volkswagen", "Volkswagen", 2]]}
    models = {"name": "model_list", "columns": ["ModelId", "Maker", "Model"],
              "rows": [[1, 1, "amc"], [2, 2, "audi"], [3, 2, "bmw"]]}
    cars = SchemaGraph.from_tables([makers, models], [("model_list", "Maker", "car_makers", "Id")])
    maker = {(column.ref.table, column.ref.name): column.ref for column in cars.columns}
    assert request_fields(question, cars) == ((maker["car_makers", "FullName"],), (maker["car_makers", "Id"],))


def test_a_noun_joined_to_a_count_by_and_is_counted_too():
    """Spider DEV 597: "List the number of different series names and contents in the TV Channel table." counts both:
    a bare noun added to a count by "and" is counted too (sql_expansion.output_requests), so nothing is grouped."""
    from engine.sql_expansion import output_requests
    question = "List the number of different series names and contents in the TV Channel table."
    assert [request.count for request in output_requests(question)] == [True, True]


def test_find_opens_a_list_of_outputs():
    """Spider DEV 419, 637 (2026-10-15): "find the id, name and age for visitors who visited some museums more than
    once." showed a museum's id and name beside the visitor's age: "find" opened no list. "Find" is a list verb like
    "list" and "show" (sql_expansion.output_requests). Negative: an opening "find" with one item lists nothing."""
    from engine.sql_expansion import output_requests, request_fields, tokens

    def items(question):
        said = tokens(question)
        return [(" ".join(said[r.start:r.end]), r.count) for r in output_requests(question)]

    assert items("find the id, name and age for visitors who visited some museums more than once.") == [
        ("id", False), ("name", False), ("age", False)]
    assert items("For each journal_committee, find the editor name and the journal theme.") == [
        ("editor name", False), ("journal theme", False)]
    assert items("Find the number of concerts happened in the stadium with the highest capacity.") == []
    visitors = {"name": "visitor", "columns": ["ID", "Name", "Level_of_membership", "Age"],
                "rows": [[1, "Gonzalo Higuain", 8, 35], [2, "Guti Midfielder", 5, 28]]}
    museums = {"name": "museum", "columns": ["Museum_ID", "Name", "Num_of_Staff"], "rows": [[1, "Plaza Museum", 62]]}
    visits = {"name": "visit", "columns": ["Museum_ID", "visitor_ID", "Num_of_Ticket"], "rows": [[1, 1, 20], [1, 1, 4]]}
    graph = SchemaGraph.from_tables([visitors, museums, visits], [("visit", "Museum_ID", "museum", "Museum_ID"),
                                                                   ("visit", "visitor_ID", "visitor", "ID")])
    ref = {(column.ref.table, column.ref.name): column.ref for column in graph.columns}
    assert request_fields("find the id, name and age for visitors who visited some museums more than once.", graph) == (
        (ref["visitor", "ID"],), (ref["visitor", "Name"],), (ref["visitor", "Age"],))


def test_an_item_followed_by_of_and_its_rows_is_theirs():
    """Fresh holdout 509, Spider DEV 946 (2026-10-15): "Find the customer name and date of the orders that have the
    status 'Delivered'." showed each customer's date_became_customer: the customers, owning more items, owned the date
    too. An item followed by "of" and the rows of one table that realizes it is that table's (request_fields), and
    still counts in deciding the list's owner (DEV 370: "the ids and names of each document" showed a template's id).
    Contrast: where no table holds the rows named after "of", the owner of the items decides it."""
    from engine.sql_expansion import request_fields
    customers = {"name": "Customers", "columns": ["customer_id", "customer_name", "date_became_customer"],
                 "rows": [[1, "Ann", "2017-03-01"], [2, "Bo", "2016-11-07"]]}
    orders = {"name": "Customer_Orders", "columns": ["order_id", "customer_id", "order_status", "order_date"],
              "rows": [[1, 1, "Delivered", "2018-03-08"], [2, 2, "Cancelled", "2018-02-01"]]}
    graph = SchemaGraph.from_tables([customers, orders], [("Customer_Orders", "customer_id", "Customers",
                                                           "customer_id")])
    ref = {(column.ref.table, column.ref.name): column.ref for column in graph.columns}
    question = "Find the customer name and date of the orders that have the status \"Delivered\"."
    assert request_fields(question, graph) == (
        (ref["Customers", "customer_name"],), (ref["Customer_Orders", "order_date"],))
    # Contrast: rows no table holds own nothing, and the customers, owning more items, own the date.
    assert request_fields("Find the customer name and date of the shipments.", graph) == (
        (ref["Customers", "customer_name"],), (ref["Customers", "date_became_customer"],))
    templates = {"name": "Templates", "columns": ["Template_ID", "Template_Type_Code"], "rows": [[1, "PP"], [2, "BK"]]}
    documents = {"name": "Documents", "columns": ["Document_ID", "Template_ID", "Document_Name"],
                 "rows": [[10, 1, "Robbin CV"], [11, 2, "Data base"]]}
    paragraphs = {"name": "Paragraphs", "columns": ["Paragraph_ID", "Document_ID", "Paragraph_Text"],
                  "rows": [[100, 10, "Korea"], [101, 10, "Ireland"], [102, 11, "Brazil"]]}
    docs = SchemaGraph.from_tables([templates, documents, paragraphs], [
        ("Documents", "Template_ID", "Templates", "Template_ID"), ("Paragraphs", "Document_ID", "Documents",
                                                                   "Document_ID")])
    doc = {(column.ref.table, column.ref.name): column.ref for column in docs.columns}
    assert request_fields("What are the ids and names of each document, as well as the number of paragraphs in "
                          "each?", docs)[0] == (doc["Documents", "Document_ID"],)


def test_an_items_last_word_may_stand_for_its_fields_kind():
    """Spider DEV 994, 995 (2026-10-15): "List the email, cell phone and home phone of all the professionals." read no
    list: no field's name says "cell phone", and the professionals' cell_number came first, then an owner's home_phone.
    An item whose last word stands where its field's name says only the kind of its values ("number"), the item saying
    the rest, is that field (sql_expansion.realizes_request). Contrast: a field whose own name says the item is it."""
    from engine.sql_expansion import request_fields
    columns = ["professional_id", "email_address", "home_phone", "cell_number"]
    professionals = {"name": "Professionals", "columns": columns,
                     "rows": [[1, "a@x.com", "555-0100", "555-0101"], [2, "b@x.com", "555-0200", "555-0201"]]}
    owners = {"name": "Owners", "columns": ["owner_id", "email_address", "home_phone", "cell_number"],
              "rows": [[1, "c@x.com", "555-0300", "555-0301"]]}
    graph = SchemaGraph.from_tables([professionals, owners], [])
    ref = {(column.ref.table, column.ref.name): column.ref for column in graph.columns}
    question = "List the email, cell phone and home phone of all the professionals."
    assert request_fields(question, graph) == (
        (ref["Professionals", "email_address"],), (ref["Professionals", "cell_number"],),
        (ref["Professionals", "home_phone"],))
    # Contrast: a field named by the item's own words is it, before the field its kind stands for.
    phones = {"name": "Professionals", "columns": columns + ["cell_phone"],
              "rows": [row + ["x"] for row in professionals["rows"]]}
    named = SchemaGraph.from_tables([phones], [])
    assert request_fields(question, named)[1] == (named.by_table["Professionals"][4].ref,)


def test_a_country_holds_its_irregular_demonym():
    """Spider DEV 5, 1007 (2026-10-18): "What is the average, minimum, and maximum age for all French singers?" and "the
    singers who are not French citizens" were refused: no value says "French", the word for what is of 'France' that its
    name does not begin (closed_class.COUNTRY_DEMONYMS, read into SchemaGraph.value_index). Contrast: where a field
    holds the word itself (a language 'French'), the word is that value."""
    singer = {"name": "singer", "columns": ["Singer_ID", "Name", "Country", "Citizenship", "Age"],
              "rows": [[1, "Joe Sharp", "Netherlands", "Netherlands", 52],
                       [2, "Timbaland", "United States", "United States", 32],
                       [3, "Justin Brown", "France", "France", 29], [4, "Rose White", "France", "France", 41]]}
    graph = SchemaGraph.from_tables([singer], [])
    assert {value for _, value in graph.value_index.get("french", ())} == {"France"}
    assert {value for _, value in graph.value_index.get("dutch", ())} == {"Netherlands"}
    language = {"name": "countrylanguage", "columns": ["CountryCode", "Language", "Country"],
                "rows": [["FRA", "French", "France"], ["BEL", "Dutch", "Belgium"]]}
    spoken = SchemaGraph.from_tables([language], [])
    assert {value for _, value in spoken.value_index.get("french", ())} == {"French"}
    assert {value for _, value in spoken.value_index.get("dutch", ())} == {"Dutch"}


def test_a_capital_initial_before_a_capitalized_word_is_a_word():
    """Held-out Spider train 737, 2026-10-18: "Give the names of the courses with at least five enrollments." was
    refused over Course.CName, read as the one word "cname": a capital initial before a capitalized word is a word of
    its own (sql_schema.spaced_name, the split every reader of names makes), so CName is a course's name. Contrast:
    GNPOld is "GNP Old"."""
    from engine.sql_schema import name_words
    assert name_words("CName") == ("c", "name") and name_words("GNPOld") == ("gnp", "old")


def test_the_row_with_the_maximum_is_the_row_holding_it():
    """Spider DEV 95, fresh holdout 896 (2026-10-15): "What is the name of the country with the maximum GNP?" listed
    every country beside its own GNP. An extreme after "with/has/have the" selects the row holding it, as "the highest"
    does (sql_expansion.selects_extreme). Contrast: "the maximum GNP of countries" asks for the value."""
    from engine.sql_expansion import selects_extreme, tokens
    said = tokens("What is the maximum GNP of countries that have the minimum population?")
    assert not selects_extreme(said, said.index("maximum")) and selects_extreme(said, said.index("minimum"))


def test_typographic_quotes_read_as_the_ascii_ones():
    """Held-out Spider train 202, Spider DEV 715 (2026-10-16): "the customer whose name includes the letter “a”" quoted
    nothing, and "Give me Brazil’s population and life expectancies." left a lone "s" unread: the readers parse the
    ASCII apostrophe and quotes only. Selection reads a question with its typographic marks written as the ASCII ones
    (sql_expansion.plain_marks)."""
    from engine.sql_expansion import plain_marks, tokens
    assert tokens(plain_marks("Give me Brazil’s population")) == tokens("Give me Brazil's population")
    assert plain_marks("hometown is not `` Little Lever '' .") == 'hometown is not " Little Lever " .'
    assert plain_marks("the letter “a”.") == 'the letter "a".'


def test_a_possessive_is_its_word():
    """Spider DEV 715, 873, 952, 2026-10-16: "Brazil's" was read as "brazil'", so "Give me Brazil's population" stated
    no country and "the owner's first name" named no owner. A possessive is its word (sql_schema.canon)."""
    from engine.sql_schema import canon
    assert canon("Brazil's") == "brazil" and canon("dogs's") == "dog" and canon("owners") == "owner"


def test_a_word_spelling_out_a_fields_abbreviation_names_it():
    """Held-out Spider train 202, 2026-10-16: "Find the name and account balance of the customer whose name includes the
    letter 'a'." was refused, "account balance" naming no acc_bal; 35 held-out questions saying an abbreviated field's
    words in full (enr "enrollment", lat "latitude", instID "institution") were refused. A word of seven letters or
    more that a field's three- or four-letter name word begins spells it out (sql_expansion.spells_out). Contrast: a
    form of the short word ("longest"), an ordering word ("descending") and a stop word ("per") spell out nothing."""
    from engine.sql_expansion import says_in_another_form, spells_out
    assert spells_out("account", "acc") and spells_out("longitude", "long") and spells_out("institution", "inst")
    assert says_in_another_form("balance", "bal") and says_in_another_form("enrollment", "enr")
    assert not spells_out("longest", "long") and not spells_out("descending", "desc")
    assert not spells_out("percentage", "per") and not spells_out("report", "rep")
    # A word naming a table spells out no field's abbreviation of it: "the first name of students" shows no
    # ENROLL.STU_NUM (held-out Spider train 58, 2026-10-16); "institution" still spells out the table Inst.
    assert not says_in_another_form("student", "stu", frozenset({"student", "enroll"}))
    assert says_in_another_form("institution", "inst", frozenset({"inst", "paper"}))


def test_spider_ingestion_keeps_discovered_and_declared_relationships():
    """A partial declared schema must not replace production's data-derived graph."""
    from spider.probe.full_eval import ast_predict
    carriers = {"name": "carriers", "columns": ["id", "name"], "rows": [[1, "Alpha"], [2, "Beta"]]}
    ports = {"name": "ports", "columns": ["id", "name"], "rows": [["X", "East"], ["Y", "West"]]}
    shipments = {"name": "shipments", "columns": ["id", "carrier", "destination_id", "weight"],
                 "rows": [[10, 1, "X", 1], [11, 1, "Y", 2], [12, 2, "X", 1]]}
    tables = [carriers, ports, shipments]
    declared = [{"from_table": "shipments", "from_col": "destination_id", "to_table": "ports", "to_col": "id"}]
    planner = _hermetic_planner()
    # The evaluator's own selection is production's (TableQuery.select_query); this test reads its ingestion.
    planner.select_query = lambda question, tables, marked=True: PoolSelection((), (), None)
    cache = {}
    with patch.object(planner, "schema", wraps=planner.schema) as schema:
        actual = ast_predict(planner, tables, "How many shipments use carrier Alpha?", declared, cache)
        merged = schema.call_args.args[1]
        pairs = {(edge["from_table"], edge["from_col"], edge["to_table"], edge["to_col"]) for edge in merged}
        assert ("shipments", "carrier", "carriers", "id") in pairs, merged
        assert ("shipments", "destination_id", "ports", "id") in pairs, merged
        assert not any(edge["from_col"] == "weight" for edge in merged), merged
        assert not actual["ok"] and actual["stage"] == "ast_search", actual
        # A repeated question reuses that one schema; a changed declaration is
        # a different ingestion input even for the same table object identities.
        ast_predict(planner, tables, "How many shipments use carrier Alpha?", declared, cache)
        assert schema.call_count == 1
        ast_predict(planner, tables, "How many shipments are there?", [], cache)
        assert schema.call_count == 2
        discovered = schema.call_args.args[1]
        assert any(edge["from_col"] == "carrier" for edge in discovered), discovered


def test_declared_self_relationship_keeps_unrelated_queries_and_source_rows():
    from spider.probe.full_eval import ast_predict
    employees = {"name": "employees", "columns": ["Employee_ID", "Name", "ReportsTo"],
                 "rows": [[1, "A", None], [2, "A", 1], [3, "B", 1]]}
    artists = {"name": "artists", "columns": ["Artist_ID", "Name"],
               "rows": [[10, "Nora"], [11, "Nico"]]}
    tables = [employees, artists]
    before = json.dumps(tables, sort_keys=True)
    declared = [{"from_table": "employees", "from_col": "ReportsTo",
                 "to_table": "employees", "to_col": "Employee_ID"}]
    planner = _hermetic_planner()
    norm, fks = planner.ingest(tables, explicit_fks=declared)
    assert any(edge["from_table"] == edge["to_table"] == "employees"
               and edge["from_col"] == "ReportsTo" and edge["to_col"] == "Employee_ID"
               and edge["explicit"] for edge in fks), fks
    units, _ = planner._schema_name_units(norm, fks)
    assert next(unit for unit in units if unit["table"] == "employees"
                and unit["colname"] == "ReportsTo")["ref"] == ("employees", "Employee_ID")
    sch, _, _ = planner.schema(norm, fks)
    graph = SchemaGraph.from_planner(sch, fks)
    assert any(edge.from_column.table == edge.to_column.table == "employees"
               for edge in graph.foreign_keys)
    assert graph.reachable({"employees"}) == {"employees"}
    assert graph.join_trees({"employees", "artists"}) == ()
    assert graph.join_trees({"employees"})[0].joins == ()
    planner.select_query = lambda question, tables, marked=True: PoolSelection((), (), None)
    ast_predict(planner, tables, "List employee names", declared)
    assert json.dumps(tables, sort_keys=True) == before
    for malformed in ({**declared[0], "to_table": "absent"},
                      {**declared[0], "to_col": "absent"},
                      {"from_table": "employees", "to_table": "employees", "from_cols": (), "to_cols": ()},
                      {"from_table": "employees", "to_table": "employees", "from_cols": ("ReportsTo",),
                       "to_cols": ("Employee_ID", "Name")}):
        try:
            planner.ingest(tables, explicit_fks=[malformed])
        except ValueError:
            pass
        else:
            raise AssertionError("self-reference support must not admit malformed declarations")


def test_a_closed_opening_request_for_one_field_is_the_answer():
    """Spider DEV 625, 2026-10-17: "List the Episode of all TV series showed on TV Channel with series name 'Sky
    Radio'." also showed TV_series.Channel, the key its join repeats. An opening instruction naming exactly one field,
    bound unambiguously, whose owner phrase names that field's table and ends there, closes the answer's outputs
    (sql_expansion._one_closed_field, list_closed). Negative: a list that goes on after its owner ("and the number of
    ...") is not closed."""
    from engine.sql_expansion import list_closed, output_requests
    channels = {"name": "TV_Channel", "columns": ["id", "series_name", "Language"],
                "rows": [["700", "Sky Radio", "Italian"], ["701", "Sky Music", "English"]]}
    series = {"name": "TV_series", "columns": ["id", "Episode", "Channel"],
              "rows": [[1, "A Love of a Lifetime", "700"], [2, "Friendly Skies", "700"], [3, "Blowback", "701"]]}
    fks = [{"from_table": "TV_series", "from_col": "Channel", "to_table": "TV_Channel", "to_col": "id"}]
    graph = SchemaGraph.from_tables([channels, series], fks)
    closed = 'List the Episode of all TV series showed on TV Channel with series name "Sky Radio".'
    assert list_closed(closed, output_requests(closed, graph))
    question = "List the Episode of all TV series and the number of channels."
    assert not list_closed(question, output_requests(question, graph))


def test_a_word_a_fields_name_abbreviates_spells_that_field_and_states_no_value():
    """Spider DEV 788, 789, 2026-10-17: "What are the name, independence year, and surface area of the country with the
    smallest population?" filtered on a city named 'Independence'. A several-word field's name is spelled by words its
    own words abbreviate ("independence year" for IndepYear), so they state no stored value
    (sql_expansion.spelled_names). Contrast: "the cities named Independence" spells no field."""
    from engine.sql_expansion import spelled_names, tokens
    countries = {"name": "country", "columns": ["Code", "Name", "IndepYear", "SurfaceArea"],
                 "rows": [["USA", "United States", 1776, 9363520.0]]}
    cities = {"name": "city", "columns": ["ID", "Name", "CountryCode"], "rows": [[1, "Independence", "USA"]]}
    graph = SchemaGraph.from_tables([countries, cities], [])
    said = tokens("What are the name, independence year, and surface area of the country?")
    assert {said[index] for index in spelled_names(said, graph)} >= {"independence", "year"}
    said = tokens("Which cities are named Independence?")
    assert not spelled_names(said, graph)


def test_most_before_a_tables_name_counts_its_rows():
    """Spider DEV 27, 462, 2026-10-17: "What is the year that had the most concerts?" ranked the shown year itself.
    "Most" or "fewest" before a table's name counts that table's rows: the concerts, not the singers of each
    (sql_expansion.counted_noun_tables)."""
    from engine.sql_expansion import counted_noun_tables, tokens
    concert = {"name": "concert", "columns": ["concert_ID", "concert_Name", "Year"],
               "rows": [[1, "Auditions", 2014], [2, "Super bootcamp", 2014], [3, "Home Visits", 2015]]}
    singers = {"name": "singer_in_concert", "columns": ["concert_ID", "Singer_ID"],
               "rows": [[3, 1], [3, 2], [3, 3], [3, 4], [1, 1], [2, 2]]}
    fks = [{"from_table": "singer_in_concert", "from_col": "concert_ID", "to_table": "concert", "to_col": "concert_ID"}]
    assert counted_noun_tables(tokens("What is the year that had the most concerts?"),
                               SchemaGraph.from_tables([concert, singers], fks)) == {"concert"}


def test_a_comparison_symbol_is_its_word():
    """Fresh holdout of Spider train 103, 2026-10-17: "Find the names of all instructors in Comp. Sci. department with
    salary > 80000." compared salary = 80000: the question's words hold no ">". Selection reads a comparison symbol
    before a number as its words (sql_expansion.plain_marks): ">" above, ">=" at least, "<" below, "<=" at most, and
    "prior to" is "before" (fresh holdout 273)."""
    from engine.sql_expansion import plain_marks
    assert plain_marks("rated <5 or >= 8") == "rated  below 5 or  at least  8"
    assert plain_marks("the A<B ratio") == "the A<B ratio"
    assert plain_marks("the priority list") == "the priority list"


def test_a_count_counts_its_nouns_head():
    """Held-out Spider train 989, 2026-10-17: "How many movie ratings have more than 3 stars?" counted the different
    movies: the counted noun was read as Movie, its first word. A table named right after the one a count's noun
    starts with is the noun's head, which counts (sql_expansion.counted_noun_tables). Contrast: a noun naming one
    table counts it, and a verb is no head."""
    from engine.sql_expansion import counted_noun_tables, tokens
    movie = {"name": "Movie", "columns": ["mID", "title", "year"],
             "rows": [[101, "Gone with the Wind", 1939], [102, "Star Wars", 1977], [103, "Avatar", 2009]]}
    rating = {"name": "Rating", "columns": ["rID", "mID", "stars"],
              "rows": [[201, 101, 2], [201, 101, 4], [202, 102, 5], [203, 102, 4], [204, 103, 3]]}
    fks = [{"from_table": "Rating", "from_col": "mID", "to_table": "Movie", "to_col": "mID"}]
    graph = SchemaGraph.from_tables([movie, rating], fks)
    assert counted_noun_tables(tokens("How many ratings are there?"), graph) == {"Rating"}
    assert counted_noun_tables(tokens("How many movie ratings are there?"), graph) == {"Rating"}
    # A table named by a verb that takes an object is no head: "How many instructors teach a course" counts the
    # instructors (fresh holdout of Spider train 648, 2026-10-17).
    instructor = {"name": "instructor", "columns": ["ID", "name"], "rows": [[1, "Srinivasan"]]}
    teaches = {"name": "teaches", "columns": ["ID", "course_id"], "rows": [[1, "CS-101"]]}
    taught = SchemaGraph.from_tables([instructor, teaches], [])
    assert counted_noun_tables(tokens("How many instructors teach a course in the Spring of 2010?"), taught) == {
        "instructor"}


def test_pool_oracle_counts_only_eligible_denotation_hits_and_validates_checkpoints():
    from spider.probe.full_eval import _load_checkpoint_records, _score_pool_oracle

    record = {
        "ok": True,
        "selected_candidate_rank": 1,
        "pool_execution": [
            {"rank": 0, "sql": "SELECT 1", "rows": [["gold"]], "score": 0.0,
             "features": {}, "evidence": [], "eligible": False, "executable": True,
             "grounded": False, "calculation_satisfied": False},
            {"rank": 1, "sql": "SELECT 2", "rows": [["gold"]], "score": -5.0,
             "features": {"projection": 1.0}, "evidence": ["extrema:projection"], "eligible": True,
             "executable": True, "grounded": True, "calculation_satisfied": False},
            {"rank": 2, "sql": "SELECT 3", "score": -6.0,
             "features": {}, "evidence": [], "eligible": True, "executable": True,
             "grounded": True, "calculation_satisfied": False,
             "error": "OperationalError: interrupted"},
        ],
    }
    oracle, top1 = _score_pool_oracle(record, [["gold"]])
    assert oracle["strict"] and record["oracle_rank"]["strict"] == 1
    assert top1["strict"] is True
    assert record["pool"][0]["strict"] and not record["pool"][0]["eligible"]
    assert record["pool"][1]["eligible"]
    assert record["pool"][1]["evidence"] == ["extrema:projection"]
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


def test_sql_import_preserves_arithmetic_over_aggregates():
    from typing import get_args, get_type_hints

    sales = {'name': 'sales', 'columns': ['amount', 'units', 'cost', 'label'],
             'rows': [[5, 2, 1, 'a'], [11, 3, 4, 'b']]}
    graph = SchemaGraph.from_tables([sales], [])
    cases = [
        ('SELECT SUM(amount * units) * 1.0 / SUM(units) FROM sales', 8.6),
        ('SELECT SUM(amount) - SUM(cost) FROM sales', 11),
        ('SELECT SUM(amount) * 1.0 / COUNT(*) FROM sales', 8),
    ]
    for sql, expected in cases:
        query = import_sql(sql, graph)
        validate_query(query)
        assert execute([sales], render_query(query)) == [(expected,)]
    hints = get_type_hints(Comparison)
    assert BinaryExpr in get_args(hints['left']) and BinaryExpr in get_args(hints['right'])
    assert Literal in get_args(hints['left'])
    for sql, expected in [
        ('SELECT amount FROM sales WHERE amount * units > 10', [(11,)]),
        ('SELECT amount FROM sales WHERE amount > 0.97 * (SELECT AVG(amount) FROM sales)', [(11,)]),
        ('SELECT amount FROM sales WHERE 1 < units', [(5,), (11,)]),
    ]:
        query = import_sql(sql, graph)
        validate_query(query)
        assert execute([sales], render_query(query)) == expected
    for sql in ('SELECT SUM(SUM(amount)) FROM sales',
                'SELECT SUM(SUM(amount) * 1.0 / units) FROM sales',
                'SELECT SUM(amount) + SUM(label) FROM sales'):
        try:
            validate_query(import_sql(sql, graph))
        except ASTValidationError:
            pass
        else:
            raise AssertionError('invalid aggregate arithmetic accepted: ' + sql)


def test_sql_import_refuses_semantics_the_ast_cannot_preserve():
    from regress.sql_import import Unsupported

    data = {'name': 'pay', 'columns': ['rate', 'count'], 'rows': [[5, 2], [3, 2], [1, 1]]}
    graph = SchemaGraph.from_tables([data], [])
    # These independently specified source results differ from the former,
    # silently altered ASTs: real division and an omitted OFFSET, respectively.
    assert execute([data], 'SELECT rate / count FROM pay ORDER BY rate DESC') == [(2,), (1,), (1,)]
    assert execute([data], 'SELECT rate FROM pay ORDER BY rate DESC LIMIT 1, 1') == [(3,)]
    for sql, reason in [
        ('SELECT rate / count FROM pay', 'arithmetic:integer-division'),
        ('SELECT SUM(rate) / COUNT(*) FROM pay', 'arithmetic:integer-division'),
        ('SELECT rate FROM pay ORDER BY rate DESC LIMIT 1, 1', 'query:offset'),
        ('SELECT rate FROM pay ORDER BY rate DESC LIMIT 1 OFFSET 1', 'query:offset'),
        ('SELECT rate FROM pay WHERE rate > (SELECT rate FROM pay LIMIT 1 OFFSET 1)', 'query:offset'),
        ('SELECT rate FROM pay UNION SELECT rate FROM pay ORDER BY rate LIMIT 1', 'query:compound-order-or-limit'),
    ]:
        try:
            import_sql(sql, graph)
        except Unsupported as exc:
            assert str(exc) == reason, (sql, exc)
        else:
            raise AssertionError('source semantics were silently dropped: ' + sql)
    for sql, expected in [
        ('SELECT rate * 1.0 / count FROM pay ORDER BY rate DESC', [(2.5,), (1.5,), (1.0,)]),
        ('SELECT SUM(rate) * 1.0 / COUNT(*) FROM pay', [(3.0,)]),
        ('SELECT rate FROM pay ORDER BY rate DESC LIMIT 1', [(5,)]),
        ('SELECT rate FROM pay UNION SELECT rate FROM pay', [(1,), (3,), (5,)]),
    ]:
        query = import_sql(sql, graph)
        validate_query(query)
        assert execute([data], render_query(query)) == expected


def test_sql_import_preserves_like_negation():
    data = {'name': 'items', 'columns': ['id', 'label'],
            'rows': [[1, None], [2, 'ALPHA'], [3, 'beta'], [4, ''], [5, 'alphabet']]}
    graph = SchemaGraph.from_tables([data], [])
    for predicate, operator, expected in [
        ("label LIKE 'alpha%'", 'LIKE', [(2,), (5,)]),
        ("label NOT LIKE 'alpha%'", 'NOT LIKE', [(3,), (4,)]),
        ("NOT (label LIKE 'alpha%')", 'NOT LIKE', [(3,), (4,)]),
        ("NOT (label NOT LIKE 'alpha%')", 'LIKE', [(2,), (5,)]),
    ]:
        sql = 'SELECT id FROM items WHERE ' + predicate
        query = import_sql(sql, graph)
        assert query.where.operator == operator
        assert execute([data], sql) == expected
        assert execute([data], render_query(query)) == expected


def test_sql_import_preserves_null_tests_and_lowercase():
    from regress.sql_import import Unsupported

    data = {'name': 'items', 'columns': ['id', 'label', 'score'],
            'rows': [[1, None, None], [2, 'ALPHA', 0], [3, 'beta', 2], [4, '', -1]]}
    graph = SchemaGraph.from_tables([data], [])
    for sql, expected in [
        ('SELECT id FROM items WHERE label IS NULL', [(1,)]),
        ('SELECT id FROM items WHERE label IS NOT NULL', [(2,), (3,), (4,)]),
        ('SELECT id FROM items WHERE NOT (label IS NULL)', [(2,), (3,), (4,)]),
        ("SELECT id FROM items WHERE label IS NULL OR label = 'ALPHA'", [(1,), (2,)]),
        ("SELECT LOWER(label) FROM items WHERE id = 2", [('alpha',)]),
        ("SELECT id FROM items WHERE LOWER(label) = 'alpha'", [(2,)]),
        ("SELECT id FROM items WHERE label = 'null'", []),
        ("SELECT id FROM items WHERE label = ''", [(4,)]),
    ]:
        query = import_sql(sql, graph)
        validate_query(query)
        assert execute([data], render_query(query)) == expected, sql
    for sql in ('SELECT id FROM items WHERE score IS TRUE',
                'SELECT id FROM items WHERE score IS NOT FALSE', "SELECT LOWER('ALPHA') FROM items"):
        try:
            validate_query(import_sql(sql, graph))
        except (Unsupported, ASTValidationError):
            pass
        else:
            raise AssertionError('unsupported IS truthiness or non-column LOWER admitted: ' + sql)


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


def test_date_ranges_lists_and_quarters_filter_one_span():
    """Probe of the 2026-10-02 date reader: "from March to May 2026" and "between March and May 2026"
    filtered May alone, "between July 1 and July 10, 2026" July 10 alone, "in August and September 2026"
    both months at once (no row), and "after the 10th of August 2026" all of August. A range's ends, a
    list of consecutive months and a quarter are one span; a year one end names is the other's too
    (engine/sql_dates.date_phrases, each phrase's comparisons run here as the typed AST renders them)."""
    from engine.sql_ast import DatePart, and_predicates
    from engine.sql_dates import date_phrases
    from engine.sql_expansion import tokens

    dates = ["2025-11-20", "2025-12-15", "2026-01-10", "2026-02-14", "2026-03-03", "2026-04-18", "2026-05-30",
             "2026-06-02", "2026-07-01", "2026-07-05", "2026-07-10", "2026-07-11", "2026-08-05", "2026-08-10",
             "2026-08-12", "2026-09-09", "2026-10-01"]
    orders = {"name": "orders", "columns": ["order_id", "placed", "amount"],
              "rows": [[index + 1, placed, 100] for index, placed in enumerate(dates)]}
    placed = ColumnRef("orders", "placed", SQLType.DATE)

    def counted(question):
        (phrase,) = date_phrases(question, tokens(question))
        comparisons = list(phrase.comparisons(placed))
        assert comparisons, question
        query = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "orders", where=and_predicates(comparisons))
        validate_query(query)
        return execute([orders], render_query(query))[0][0]

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
        assert counted(question) == expected, question

    def read(question):
        return [tuple((type(c.left) is DatePart, c.operator, c.right.value) for c in phrase.comparisons(placed))
                for phrase in date_phrases(question, tokens(question))]

    # Same profile, other spans: a lone "from" is the period, "onwards" makes it "since", a yearless
    # range compares months, and a yearless quarter its three months.
    assert read("orders from August 2026") == [((False, ">=", "2026-08-01"), (False, "<", "2026-09-01"))]
    assert read("orders from August 2026 onwards") == [((False, ">=", "2026-08-01"),)]
    assert read("orders in March and April") == [((True, ">=", 3), (True, "<=", 4))]
    assert read("orders in Q4") == [((True, ">=", 10), (True, "<=", 12))]
    assert read("orders in the first half of 2026") == [
        ((False, ">=", "2026-01-01"), (False, "<", "2026-07-01"))]
    assert read("orders in the second half of the year 2025") == [
        ((False, ">=", "2025-07-01"), (False, "<", "2026-01-01"))]
    assert read("orders in the last month of the year 2025") == [
        ((False, ">=", "2025-12-01"), (False, "<", "2026-01-01"))]
    assert read("orders in the first month of 2026") == [
        ((False, ">=", "2026-01-01"), (False, "<", "2026-02-01"))]
    assert read("orders in the second half") == [((True, ">=", 7), (True, "<=", 12))]
    assert read("orders in the last month") == []  # Needs a year or an explicit reference date.
    assert read("orders in the last six months of 2026") == [
        ((False, ">=", "2026-07-01"), (False, "<", "2027-01-01"))]
    assert read("orders in the first three months of 2026") == [
        ((False, ">=", "2026-01-01"), (False, "<", "2026-04-01"))]
    assert read("orders in the last six months") == []  # No reference date supplied.
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


def test_a_plural_reads_as_its_singular_everywhere():
    """Spider DEV, 2026-10-02: five modules each kept a copy of one plural rule, which read "courses" as
    "cours", "matches" as "matche" and "finishes" as "finishe", so "the course", "the match" and "the best
    finishes" named no Courses, matches or Best_Finish. sql_schema.canon is the one rule every reader of
    words and the value index read with; "-ss" and short words keep their "s"."""
    from engine.sql_schema import canon

    for singular, plural in (("course", "courses"), ("match", "matches"), ("branch", "branches"),
                             ("finish", "finishes"), ("tax", "taxes"), ("class", "classes"),
                             ("address", "addresses"), ("city", "cities"), ("house", "houses")):
        assert canon(singular) == canon(plural) == singular, (singular, plural, canon(plural))
    # Contrast: a word that ends in "ss", or a short one, keeps its "s".
    assert (canon("bus"), canon("class"), canon("business")) == ("bus", "class", "business")


def test_a_number_of_a_numeric_field_is_the_field():
    """Held-out Spider train 633, 30, 2026-10-12: "the average, maximum, and minimum number of floors for all
    buildings" counted the buildings beside the aggregates of Floors, and "the crime rates of counties in ascending
    order of number of police officers" counted counties per crime rate: a "number of" before a numeric field's whole
    name is that field (sql_expansion.field_numbers). A text field's name ("the number of countries" beside Country)
    and a table's ("the number of visits") are still counts."""
    from engine.sql_expansion import count_requested, field_numbers
    county = {"name": "county_public_safety", "columns": ["County_ID", "Name", "Police_officers", "Crime_rate"],
              "rows": [[1, "A", 90, 50.0], [2, "B", 10, 80.0], [3, "C", 40, 70.0]]}
    graph = SchemaGraph.from_tables([county], ())
    fields = [column.ref for column in graph.columns]
    assert field_numbers("the order of number of police officers", fields) == {3}
    assert not count_requested("in ascending order of number of police officers", fields)
    # Contrast: a text field's name and a table's are counted; a name alone says no type.
    country = {"name": "people", "columns": ["id", "Name", "Country"], "rows": [[1, "Ann", "France"]]}
    people = SchemaGraph.from_tables([country], ())
    assert count_requested("the number of countries", [column.ref for column in people.columns])
    assert count_requested("the number of visits", [column.ref for column in graph.columns])
    assert count_requested("the number of police officers", ["Police_officers"])
    # A table's name counts its rows though an integer key shares it (Spider DEV 88).
    continents = {"name": "continents", "columns": ["ContId", "Continent"], "rows": [[1, "asia"]]}
    countries = {"name": "countries", "columns": ["CountryId", "CountryName", "Continent"], "rows": [[1, "japan", 1]]}
    keyed = SchemaGraph.from_tables([continents, countries], [("countries", "Continent", "continents", "ContId")])
    assert count_requested("What is the number of continents?", [column.ref for column in keyed.columns])


def test_number_used_as_a_column_label_is_not_a_count_request():
    pit_stops = {
        "name": "pitStops",
        "columns": ["driverId", "stop", "duration"],
        "rows": [[1, 1, 20], [1, 2, 18]],
    }
    question = "Find the driver id and stop number of all drivers."
    assert analyze_question(question, SchemaGraph.from_tables([pit_stops], [])).count_requested is False


def test_a_number_that_ends_a_fields_name_asks_for_the_field():
    """Held-out Spider train 47 (2026-10-11): "Which patients made more than one appointment? Tell me the name and phone
    number of these patients" showed each patient's count of appointments beside the name and phone, since any
    "number" asked for a count. A "number" that ends the name of a field asks for the field; an owner's ("each
    patient's number of appointments") and a quantity's ("the total number") still count."""
    from engine.sql_expansion import count_requested, field_numbers
    names = ("SSN", "Name", "Phone", "Appointment_ID", "Patient", "Day")
    assert field_numbers("the phone number of these patients", names) == {2}
    assert not count_requested("Give the name and phone numbers of the patients.", names)
    # Negative: an owner's number and a quantity's count, though Patient and Total end fields' names.
    assert count_requested("List each patient's number of appointments.", names)
    assert count_requested("What is the total number of appointments?", names + ("Total",))


def test_literals_are_escaped_by_renderer():
    authors = {"name": "authors", "columns": ["Name"], "rows": [["O'Reilly"], ["Elsevier"]]}
    name = ColumnRef("authors", "Name", SQLType.TEXT)
    query = SelectQuery((SelectItem(name),), "authors",
                        where=Comparison(name, "=", Literal("O'Reilly", SQLType.TEXT)))
    validate_query(query)
    sql = render_query(query)
    assert "'O''Reilly'" in sql
    assert execute([authors], sql) == [("O'Reilly",)]


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


def test_and_past_a_condition_joins_conditions():
    """Held out 5, 2026-10-18: "List the document ids of documents with the status done and type Paper" read no request
    for the ids: an "and" after the list was read as another output (sql_expansion.list_closed). Past a word opening
    the condition the rows meet ("with", "where", "whose", "who", "that", "which"), "and" joins conditions. Contrast:
    after an owner phrase ("for all concerts and the number of singers") it adds an output."""
    from engine.sql_expansion import list_closed, output_requests
    documents = {"name": "Documents", "columns": ["document_id", "document_status_code", "document_type_code"],
                 "rows": [[1, "working", "CD"], [2, "done", "Paper"], [3, "done", "Hard Drive"]]}
    graph = SchemaGraph.from_tables([documents], [])
    question = "List the document ids of documents with the status done and type Paper."
    requests = output_requests(question, graph)
    assert requests and list_closed(question, requests), requests
    concerts = {"name": "concert", "columns": ["concert_ID", "concert_Name", "Theme"],
                "rows": [[1, "Auditions", "Free choice"], [2, "Super bootcamp", "Free choice 2"]]}
    question = "What are the name and theme for all concerts and the number of singers?"
    assert not list_closed(question, output_requests(question, SchemaGraph.from_tables([concerts], [])))


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
    # order numbers, the ranker's regex called 'paid' a key, and only some copies counted codes. The planner's
    # helpers, compose, the world path and the deterministic lowering now read one rule.
    import importlib

    from engine.sql_schema import is_surrogate_key

    for name in ("order ID", "customer_id", "OrderID", "PetID", "uid", "customer_key", "Identifier", "index"):
        assert is_surrogate_key(name), name
    # Contrastive: 'orders' and 'idea' share letters with 'id', 'paid' ends in them, 'price index' is a
    # measure, and a code is a natural attribute people ask for by name.
    for name in ("orders", "idea", "paid", "valid", "price index", "country_code", "Code", "keyboard"):
        assert not is_surrogate_key(name), name
    assert not is_surrogate_key("amount")                       # a measure
    for module in ("engine.sql_expansion", "engine.compose", "engine.encoder_overlay",
                   "engine.knowledge_query", "engine.knowledge_tables", "engine.deterministic.lower"):
        assert importlib.import_module(module).is_surrogate_key is is_surrogate_key, module


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


def test_spider_comparison_keeps_nonfinite_spellings_as_stable_text():
    from spider.probe.spider_eval import normalize_value
    assert compare_spider_rows([["Nan"]], [["Nan"]])["strict"]
    assert compare_spider_rows([["Nan"], ["$1,234.00"]], [[1234], ["nan"]])["strict"]
    assert normalize_value("NaN") == "nan"
    assert normalize_value("Infinity") == "infinity"
    assert not compare_spider_rows([["Nan"]], [["Nantes"]])["strict"]
    assert not compare_spider_rows([["Nan"], ["Nan"]], [["Nan"]])["strict"]
    assert not compare_spider_rows([["Nan", 1]], [[1, "Nan"]])["strict"]


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
                "evidence": ["the Name of every person"],
                "features": {"rank": 0.0},
                "selection": {"pool_size": 7, "selected": 0},
                "read_as": "the Name of every person",
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
        "evidence": ["the Name of every person"],
        "features": {"rank": 0.0},
        "selection": {"pool_size": 7, "selected": 0},
    }
    assert response["model"] == "typed planner"
    assert response["calculations"] == [{"specification": "ratio", "status": "satisfied"}]
    # The reply says how the readers read the question from this record.
    assert response["read_as"] == "the Name of every person" and "fallback" not in response


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


def test_schema_graph_preserves_zero_padded_source_codes():
    tables = [{'name': 'items', 'columns': ['id', 'quantity', 'fraction', 'native'],
               'rows': [['0877', '5', '0.25', 877], ['877', '8', '0.50', 2]]}]
    graph = SchemaGraph.from_tables(tables, [])
    code, quantity, fraction, native = graph.columns
    assert code.ref.type == SQLType.TEXT and code.values == ('0877', '877')
    assert quantity.ref.type == SQLType.INTEGER and quantity.values == (5, 8)
    assert fraction.ref.type == SQLType.REAL and native.ref.type == SQLType.INTEGER
    padded = SelectQuery((SelectItem(Aggregate('COUNT', Star())),), 'items',
                         where=Comparison(code.ref, '=', Literal('0877')))
    plain = SelectQuery((SelectItem(Aggregate('COUNT', Star())),), 'items',
                        where=Comparison(code.ref, '=', Literal('877')))
    assert execute(tables, render_query(padded))[0][0] == 1
    assert execute(tables, render_query(plain))[0][0] == 1
    assert SchemaGraph.from_tables([{'name': 'codes', 'columns': ['postal'],
                                    'rows': [['02110'], ['90210']]}], []).columns[0].values == ('02110', '90210')


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


def test_the_table_a_list_names_after_its_items_by_its_last_word_owns_them():
    """Spider DEV 150, 2026-10-18: "What are the names and ids of all makers with more than 3 models?" read no list:
    countries' CountryName and CountryId realized the items as well as car_makers' FullName and Id, and the makers
    named after the items ("of all makers") did not say the table's whole name, so a maker's name alone was shown.
    The table the list names right after its items owns them, by its name or the noun that ends it. Contrast: a noun
    no table's name ends with decides nothing."""
    from engine.sql_expansion import request_fields
    countries = {"name": "countries", "columns": ["CountryId", "CountryName"], "rows": [[1, "usa"], [2, "germany"]]}
    makers = {"name": "car_makers", "columns": ["Id", "Maker", "FullName", "Country"],
              "rows": [[1, "amc", "American Motor Company", 1], [2, "volkswagen", "Volkswagen", 2]]}
    models = {"name": "model_list", "columns": ["ModelId", "Maker", "Model"],
              "rows": [[1, 1, "amc"], [2, 2, "audi"], [3, 2, "bmw"]]}
    graph = SchemaGraph.from_tables([countries, makers, models], [("car_makers", "Country", "countries", "CountryId"),
                                                                 ("model_list", "Maker", "car_makers", "Id")])
    fields = request_fields("What are the names and ids of all makers with more than 3 models?", graph)
    assert [[(ref.table, ref.name) for ref in item] for item in fields] == [[("car_makers", "FullName")],
                                                                            [("car_makers", "Id")]], fields
    assert request_fields("What are the names and ids of all entries with more than 3 models?", graph) == ()


# ----------------------------------------------------------------------------- the one own-data selection (Decisions path)
def _orders_csv(rows=None):
    from tests.test_answer import ROWS
    return "\n".join(",".join(str(cell) for cell in row) for row in (rows or ROWS))


def _orders_request(name="orders"):
    from engine.tables import csv_table
    return [csv_table(_orders_csv(), name)]


def _choice(first, none=0.0):
    """A fake API (tests/test_answer.py) that reads "total amount for East" as a person would and gives the choice's
    first listing ``first`` and its "none" option ``none``, the rest sharing what is left."""
    from tests.test_answer import NONE, _reading_rule

    def rule(name, question, state):
        if name == "reading":
            listed = sorted(option for option in question["criteria"] if option != NONE)
            return {listed[0]: first, **({NONE: none} if none else {})}
        return _reading_rule(name, question, state)
    return rule


def _decided(rule, call, judge=None):
    """``call`` with a fake API answering by ``rule`` and a second judge that picks what the rule favours (``judge``:
    another judge's rule, as a judge that picks none for a test of an answer the two do not both pick)."""
    from tests.test_answer import FakeJudge, FakeTransport, _use_fake_reader, _with_transport
    _use_fake_reader()
    transport = FakeTransport(rule)
    return _with_transport(transport, call, None if judge is None else FakeJudge(judge)), transport


def test_the_selection_is_the_decisions_paths_listed_candidates():
    """select_query's pool is every candidate the Decisions path listed (each program that returned rows), as its
    typed AST over the request's store tables with its read-as line as evidence, ranked by its answer's probability;
    the likeliest answer is selected when the choice stands behind it."""
    from engine.program.closed import T_FLAG
    from tests.test_answer import NONE

    planner = _hermetic_planner()
    norm, _fks = planner.ingest(_orders_request())
    selection, transport = _decided(_choice(0.9), lambda: planner.select_query("total amount for East", norm))
    assert selection.pool and len(selection.pool) == len(selection.rows) == len(selection.programs)
    assert selection.selected == 0 and selection.erred is None and not selection.warned, selection.record()
    scores = [member.score for member in selection.pool]
    assert list(selection.ranking) == sorted(range(len(scores)), key=lambda at: (-scores[at], at))
    assert selection.confidence >= T_FLAG and selection.probabilities[NONE] < selection.confidence
    assert all('"_t0"' in member.sql and member.evidence[0] for member in selection.pool)
    assert selection.rows[0], "a listed candidate returned rows"
    assert list(transport.bodies[-1]["questions"]) == ["reading"], "the choice is the last call"


def _neon(answers):
    """A fake retry model (engine/llm.py QUERY_TRANSPORT): each call's answer is the next of ``answers`` (a dict the
    model's JSON holds, or an exception to raise); the prompts it was sent are kept."""
    import json

    from engine import llm

    prompts = []

    def transport(payload):
        body = json.loads(payload.decode("utf-8"))
        prompts.append(body["contents"][0]["parts"][0]["text"])
        answer = answers[min(len(prompts), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return {"text": json.dumps(answer), "usage": {"promptTokenCount": 10, "candidatesTokenCount": 5}}

    held = llm.QUERY_TRANSPORT
    llm.QUERY_TRANSPORT = transport
    return prompts, lambda: setattr(llm, "QUERY_TRANSPORT", held)


EAST = 'SELECT SUM("Amount") AS "total amount" FROM "orders" WHERE "Region" = \'East\''


def test_an_answer_most_likely_wrong_is_retried_never_asked_back():
    """Owner, 2026-11-21: "never ask back unless the question is wrong". Below closed.T_ERR, or with "none of these"
    likelier, the Decisions path decides nothing (compose and the search may answer it) and the answer is owed its
    retry (engine/retry.py): Gemini 4 Neon writes one SQLite query over typed copies of the request's tables, guarded
    and run read-only, and its rows are the answer, marked retried with its reason and query. Owner, 2026-11-27: the
    answer first. Serving gives the likeliest reading at once, marked most likely wrong, with the token of the retry
    it is owed, and asks no model; the retry (``retry_answer`` with the token) replaces it. The retry's word that the
    sheet cannot answer is the one question back; a retry that cannot answer leaves the served answer marked, with
    why. A token this engine did not seal is refused. Until 2026-11-27 serving waited for the retry before it
    answered."""
    from engine import config
    from engine.llm import LLMUnavailable
    from engine.tables import RETRY_FAILED, PendingInvalid

    planner = _hermetic_planner()
    prompts, restore = _neon([{"answerable": True, "sql": EAST, "reason": "Adds Amount over the East rows."}])
    try:
        first, _ = _decided(_choice(0.05, none=0.9), lambda: planner.serve(_orders_request(), "total amount for East"))
        assert prompts == [], "serving answers before any retry is asked"
        assert "clarify" not in first and first["valid"] and first["result"]["rows"] and first["model"] == PROGRAM_MODEL, first
        owed = first["check"]
        assert owed["state"] == "erred" and "most likely wrong" in owed["reason"] and "could not answer" not in owed["reason"], owed
        assert first["selection"]["selected"] is None and first["selection"]["erred"] == 0 and first["read_as"]
        response = planner.retry_answer(_orders_request(), "total amount for East", pending=owed["pending"])
    finally:
        restore()
    assert "clarify" not in response and response["valid"] and response["error"] is None, response
    assert response["result"] == {"columns": ["total amount"], "rows": [[240.5]]}, response["result"]
    check = response["check"]
    assert check["state"] == "retried" and check["asked"] is False and check["retry"]["sql"] == EAST, check
    assert check["retry"]["model"] == config.QUERY_MODEL and "most likely wrong" in check["reason"], check
    assert first["read_as"] in check["reason"], "the retried answer's reason names the reading it replaces"
    assert response["sql"] == EAST and response["read_as"] == "Adds Amount over the East rows."
    assert 'TABLE "orders" (7 rows)' in prompts[0] and '"Amount" REAL' in prompts[0] and "'East'" in prompts[0], prompts[0]
    assert prompts[0].endswith("QUESTION: total amount for East")
    # the question is wrong: the retry says the sheet cannot answer it, and that is asked
    _p, restore = _neon([{"answerable": False, "sql": "", "reason": "The sheet has no column of costs."}])
    try:
        asked, _ = _decided(_choice(0.05, none=0.9), lambda: planner.serve(_orders_request(), "total cost for East"))
        wrong = planner.retry_answer(_orders_request(), "total cost for East", pending=asked["check"]["pending"])
    finally:
        restore()
    assert wrong["clarify"] is True and wrong["reason"] == "The sheet has no column of costs." and wrong["result"] is None
    assert wrong["read_as"] == asked["read_as"]
    # the retry cannot answer (a query the guard refuses, twice; then a failed call): the served answer stays, marked
    for answers in ([{"answerable": True, "sql": 'DELETE FROM "orders"', "reason": "r"}] * 2, [LLMUnavailable("down")]):
        _p, restore = _neon(answers)
        try:
            failed = planner.retry_answer(_orders_request(), "total amount for East", pending=owed["pending"])
        finally:
            restore()
        assert failed["error"] == RETRY_FAILED and failed["result"] is None and failed["check"]["state"] == "erred", failed
        assert "most likely wrong" in failed["check"]["reason"] and "could not answer it either" in failed["check"]["reason"]
        assert "pending" not in failed["check"] and "clarify" not in failed
    for forged in (owed["pending"][:-1] + ("0" if owed["pending"][-1] != "0" else "1"), "p1.e30.00", "", None and "x"):
        try:
            planner.retry_answer(_orders_request(), "total amount for East", pending=forged or "x")
        except PendingInvalid:
            continue
        raise AssertionError("a token this engine did not seal ran a retry")
    # the person's Retry: the retry answers whatever the path read
    _p, restore = _neon([{"answerable": True, "sql": EAST, "reason": "Adds Amount over the East rows."}])
    try:
        retried = planner.retry_answer(_orders_request(), "total amount for East")
    finally:
        restore()
    assert retried["result"]["rows"] == [[240.5]] and retried["check"]["state"] == "retried" and retried["check"]["asked"]


def test_the_retry_reads_typed_copies_of_the_tables_and_leaves_the_database_alone():
    """The retry (engine/retry.py) shows the model, and runs its query over, TEMP copies of the request's tables typed by
    their own cells: a column of plain numbers is numbers, so "Amount > 50" is a number comparison (the planner's
    decimal text would read "9" > "50"), and a zero-padded code stays text ("007"). The copies are dropped and the
    database's own tables untouched; on a workbook of several tabs the prompt names the tab in view; the one guard
    refuses a query that writes, and a refused query goes back once with the guard's reason."""
    import contextvars

    from engine import prepared
    from engine.retry import retry

    tables = [{"name": "orders", "columns": ["Code", "Amount", "Note"], "rows": [["007", "9", "x"], ["010", "100", None]]},
              {"name": "returns", "columns": ["Code", "Qty"], "rows": [["007", 1]]}]
    guard = TableQuery.guard.__get__(_hermetic_planner())
    prompts, restore = _neon([{"answerable": True, "sql": 'SELECT "Code" FROM "orders" WHERE "Amount" > 50', "reason": "r"}])

    def run():
        database = prepared.RequestDatabase(None, "orders")
        token = prepared._DATABASE.set(database)
        try:
            outcome = retry("which codes are over 50", tables, guard)
            left = database.connection.execute("SELECT name FROM temp.sqlite_master").fetchall()
            return outcome, left
        finally:
            prepared._DATABASE.reset(token)
            database.close()
    try:
        outcome, left = contextvars.copy_context().run(run)
    finally:
        restore()
    assert outcome.answered and outcome.rows == (("010",),), outcome
    assert left == [], "the TEMP copies are dropped"
    assert '"Code" TEXT' in prompts[0] and '"Amount" INTEGER' in prompts[0] and '"Qty" INTEGER' in prompts[0], prompts[0]
    assert 'TAB IN VIEW: "orders"' in prompts[0]
    prompts, restore = _neon([{"answerable": True, "sql": 'DELETE FROM "orders"', "reason": "r"}] * 2)
    try:
        with prepared.scratch_database():
            refused = retry("drop the orders", tables, guard)
    finally:
        restore()
    assert not refused.answered and refused.error.startswith("guard:") and len(prompts) == 2 and "IT FAILED: guard:" in prompts[1]


def test_the_retry_is_told_to_put_the_answer_first():
    """The public benchmark told the models it compares to put the answer itself in the first output column, and the
    retry was never told: of the 71 DataBench test answers it rewrote at release 4a3b86e5, 9 held the right values
    behind a label column and were graded wrong (2 of 102 on WikiTableQuestions), which no written model's were. The
    rule is the retry's own (engine/retry.py ANSWER_FIRST): it reaches the model in the system instruction, the
    benchmark adds nothing to the prompt, and an answer it recorded for a written model before the rule moved is found
    by the body it was recorded under."""
    import json

    import regress.run_public as public
    from engine import llm
    from engine.retry import ANSWER_FIRST, SYSTEM

    assert SYSTEM.endswith(" " + ANSWER_FIRST) and "first output column" in ANSWER_FIRST and "one row per item for a list" in ANSWER_FIRST
    sent = []

    def transport(payload):
        sent.append(payload)
        return {"text": json.dumps({"answerable": True, "sql": EAST, "reason": "Adds Amount over the East rows."}), "usage": {}}

    planner, held = _hermetic_planner(), llm.QUERY_TRANSPORT
    llm.QUERY_TRANSPORT = transport
    try:
        retried = planner.retry_answer(_orders_request(), "total amount for East")
    finally:
        llm.QUERY_TRANSPORT = held
    assert retried["check"]["state"] == "retried" and len(sent) == 1
    system = json.loads(sent[0].decode("utf-8"))["systemInstruction"]["parts"][0]["text"]
    assert system == SYSTEM, "the rule reaches the model with the prompt"
    told, _turns = public._query_parts(sent[0])
    assert told == SYSTEM, "the benchmark sends the written models the retry's prompt, nothing added"
    before = json.loads(sent[0].decode("utf-8"))
    before["systemInstruction"]["parts"][0]["text"] = SYSTEM[: -len(" " + ANSWER_FIRST)]
    assert public.recorded_key(sent[0]) == llm.decision_hash(json.dumps(before, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    assert public.recorded_key(sent[0]) != llm.decision_hash(sent[0])


def test_a_warned_answer_is_served_with_what_else_it_may_mean():
    """Between closed.T_ERR and T_FLAG, where the second judge does not pick the answer too, an answer's ``check`` is
    warned: the reason its mark opens on says how it was read, how sure the choice was and what else the question may
    mean, with the coordinates the runner-up reads differently. The reply is the answer alone (the reason moved from
    the reply to the mark, 2026-11-21). Contrast: an answer the choice stands behind is answered, and so is one in
    that band that both judges pick (2026-11-27).

    Owner, 2026-11-27: "always serve the answer before the judge has answered". Serving gives the answer with its
    mark being checked (``check`` checking, with a sealed token) and asks no judge; ``settle_check`` asks the judge
    and returns the mark its answer leaves; the selection's record is the choice's own. The stored and streamed forms
    hold no token, and a stored answer is not judged twice until it is settled. Until 2026-11-27 serving waited for
    the judge, a median 3.7 s and a p90 of 10 s of a request's 6.1 s and 13.7 s (release 95e2a7af's gate)."""
    from engine.answer_presentation import terminal_reply
    from engine.tables import PendingInvalid, settle_check, shown_check, unsettled_check
    from mcp_server.engine_client import shape_reason_response
    from tests.test_answer import NONE, FakeJudge, FakeTransport, _use_fake_reader, _with_transport

    planner = _hermetic_planner()
    _use_fake_reader()

    def served(rule, judge):
        def run():
            response = planner.serve(_orders_request(), "total amount for East")
            assert judge.bodies == [], "the answer is served before the judge is asked"
            given = response["check"]
            return response, given, settle_check(given["pending"])
        return _with_transport(FakeTransport(rule), run, judge)

    response, given, check = served(_choice(0.47, none=0.44), FakeJudge(lambda name, question, state: {NONE: 0.9}))
    assert response["valid"] and response["result"]["rows"] and response["selection"]["warned"], response["selection"]
    assert given["state"] == "checking" and set(given) == {"state", "confidence", "pending"}, sorted(given)
    assert check["state"] == "warned" and check["reason"].startswith("I read this as “") and "% sure" in check["reason"]
    assert isinstance(check["coordinates"], list) and all(":" in name for name in check["coordinates"])
    assert check["also_read_as"] is None or isinstance(check["also_read_as"], str)
    assert shown_check(given) == {"state": "checking", "confidence": given["confidence"]}
    assert unsettled_check(given)["state"] == "warned", "stored before it is settled: warned, as the choice alone left it"
    shaped = shape_reason_response({**response, "check": check}, None)
    assert shaped["status"] == "answered" and shaped["check"] == check
    assert "not sure" not in terminal_reply(shaped)
    for forged in (given["pending"][:-2] + "00", "p1.e30.00", "not a token"):
        try:
            settle_check(forged if forged != given["pending"] else forged + "0")
        except PendingInvalid:
            continue
        raise AssertionError("a token this engine did not seal was sent to the judge")
    agreeing = FakeJudge(_choice(0.47, none=0.44))
    both, both_given, both_check = served(_choice(0.47, none=0.44), agreeing)
    assert both_given["state"] == "checking" and both_check == {"state": "answered", "confidence": both_given["confidence"]}
    assert len(agreeing.bodies) == 1, "the judge is asked once, when the mark is settled"
    sure, sure_given, sure_check = served(_choice(0.9), FakeJudge(_choice(0.9)))
    assert sure["valid"] and sure_given["state"] == "checking" and sure_check["state"] == "answered" and not sure["selection"]["warned"]
    unsettled = unsettled_check(sure_given)
    assert unsettled["state"] == "warned" and unsettled["reason"].endswith("the second check of this reading did not run."),         "an answer the choice stands behind is stored as not judged twice until its mark is settled"


def test_an_analysis_runs_the_chosen_program_on_both_emitters():
    """In an analysis, as every served request is, the chosen program's one plan runs through the deterministic
    emitters on the request's database and the response carries its record; outside one the answer is the rows the
    choice read, on the candidates' floats. Both are one answer."""
    from engine.deterministic.context import analysis_execution_context

    planner = _hermetic_planner()
    plain, _ = _decided(_choice(0.9), lambda: planner.serve(_orders_request(), "total amount for East"))
    with analysis_execution_context(None, "c_" + "0" * 32):
        served, _ = _decided(_choice(0.9), lambda: planner.serve(_orders_request(), "total amount for East"))
    assert served["deterministic"]["mode"] and served["views"] and served["sql"] != plain["sql"], served.get("error")
    assert len(served["result"]["rows"]) == len(plain["result"]["rows"])
    for exact, floats in zip(served["result"]["rows"], plain["result"]["rows"]):
        assert [float(cell) if isinstance(cell, (int, float, str)) and str(cell).replace(".", "", 1).lstrip("-").isdigit()
                else cell for cell in exact] == [float(cell) if isinstance(cell, (int, float)) else cell for cell in floats], (exact, floats)
    assert served["result"]["columns"] == plain["result"]["columns"]


def test_a_long_question_the_path_asks_back_asks_for_the_decomposition():
    """A question longer than decomposition.ONE_QUERY_WORDS words whose answer the Decisions path holds most likely wrong asks, in an
    analysis, for the decomposition proposal, each subquestion short enough. Contrasts: the same long question the
    path answers is answered, and a short question whose answer is most likely wrong is retried (here the retry's
    call fails, so the likeliest reading is served marked), never asked about."""
    from engine.decomposition import ONE_QUERY_WORDS
    from engine.deterministic.context import analysis_execution_context

    long_question = " ".join(["the total amount for East"] * (ONE_QUERY_WORDS // 5 + 1))
    planner = _hermetic_planner()
    with analysis_execution_context(None, "c_" + "0" * 32):
        asked, transport = _decided(_choice(0.05, none=0.9), lambda: planner.serve(_orders_request(), long_question))
    assert transport.bodies, "a long question is read"
    assert asked["decomposition_required"] and asked["error"] is None and asked["result"] is None, asked
    with analysis_execution_context(None, "c_" + "1" * 32):
        answered, _ = _decided(_choice(0.9), lambda: planner.serve(_orders_request(), long_question))
    assert answered["valid"] and answered["result"]["rows"] and not answered.get("decomposition_required"), answered
    from engine.llm import LLMUnavailable
    _p, restore = _neon([LLMUnavailable("down")])
    try:
        with analysis_execution_context(None, "c_" + "2" * 32):
            short, _ = _decided(_choice(0.05, none=0.9), lambda: planner.serve(_orders_request(), "total amount for East"))
    finally:
        restore()
    assert short["check"]["state"] == "erred" and "clarify" not in short and not short.get("decomposition_required"), short


def test_the_decisions_paths_reading_certifies_its_calculation_but_a_conversion_it_could_not_make():
    """The calculation registry reads the request's schema and the Decisions path's programs read its store tables, so
    the registry never found a commission, a ratio or a currency filter in the path's query: every answer it detected
    one in was asked back ("how many orders in GBP", demo gate dry run 2026-11-20). The path's reading certifies its
    calculation; a currency the question wants its answer in, with no money column the path could read in another
    currency, is still assessed by the registry (by the request's own names, engine/tables.py request_named), so an
    amount that names its currencies is never served unconverted, and a sheet that names none states its amounts in
    the currency the question names (production's unit annotation, tests/test_geo.py)."""
    from engine.knowledge import KnowledgeReasoner

    planner = _hermetic_planner()
    gate = KnowledgeReasoner.__new__(KnowledgeReasoner)
    for question in ("what is the total commission amount for East", "how many orders for East in GBP",
                     "total amount for East per capita"):
        served, _ = _decided(_choice(0.9), lambda: planner.serve(_orders_request(), question))
        assert served["valid"] and served["result"]["rows"] and not served.get("calculations"), (question, served)
        kept = gate._verify_calculations(served, _orders_request(), question, ())
        assert kept is served and not kept.get("clarify"), (question, kept)
    question = "total amount for East in euros"
    from engine.tables import csv_table
    from tests.test_answer import ROWS
    codes = ["GBP", "USD"] * len(ROWS)
    priced = [ROWS[0] + ["Currency"]] + [row + [codes[at]] for at, row in enumerate(ROWS[1:])]
    request = [csv_table(_orders_csv(priced), "orders")]
    served, _ = _decided(_choice(0.9), lambda: planner.serve(request, question))
    assert [row["specification"] for row in served["calculations"]] == ["currency"], served.get("calculations")
    asked = gate._verify_calculations(served, request, question, ())
    assert asked["clarify"] is True and asked["result"] is None, asked
    # contrastive: a sheet that names no currency states its amounts in the one the question names
    served, _ = _decided(_choice(0.9), lambda: planner.serve(_orders_request(), question))
    kept = gate._verify_calculations(served, _orders_request(), question, ())
    assert not kept.get("clarify") and kept["result"]["rows"] and kept["currency"]["realization"] == "unit_annotation", kept


def test_the_store_shares_the_request_database_with_a_table_named_like_its_own():
    """The program's tables live on the request's one database beside the request's own tables, under names no request
    table takes: a sheet sent with no name is the table "t0" (request_validation.canonical_table_name), and its copy in
    main stays the upload's while the path reads catalog table t0 as "_t0"."""
    from engine.prepared import scratch_database

    planner = _hermetic_planner()
    norm, fks = planner.ingest(_orders_request("t0"))
    sch, _, tablemap = planner.schema(norm, fks)
    with scratch_database() as database:
        database.load(tablemap, sch)
        selection, _ = _decided(_choice(0.9), lambda: planner.select_query("total amount for East", norm))
        assert selection.selected is not None and selection.tables.connection is database.connection
        names = {row[0] for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"t0", "_t0", "_one"} <= names, names
        assert database.connection.execute('SELECT COUNT(*) FROM main."t0"').fetchone()[0] == len(tablemap["t0"]["rows"])
        assert [row[1] for row in database.connection.execute('PRAGMA table_info("t0")')] == sch_names(sch, "t0")


def sch_names(sch, table):
    return [column["name"] for column in sch if column["table"] == table]


def test_one_question_is_answered_once_per_request():
    """The compose probe, the delegate and the hybrid search's check ask the one selection the same question in one
    request: its Decisions calls are made once (engine/prepared.py RequestDatabase.answered). A new request asks again;
    the workbook's preparation is kept across requests (prepared_workbook)."""
    from engine.prepared import scratch_database
    from tests.test_answer import FakeTransport, _use_fake_reader, _with_transport

    _use_fake_reader()
    planner = _hermetic_planner()
    norm, _fks = planner.ingest(_orders_request())
    transport = FakeTransport(_choice(0.9))
    with scratch_database():
        first = _with_transport(transport, lambda: planner.select_query("total amount for East", norm))
        calls = len(transport.bodies)
        second = _with_transport(transport, lambda: planner.select_query("total amount for East", norm))
        assert len(transport.bodies) == calls and second.selected == first.selected
    _with_transport(transport, lambda: planner.select_query("total amount for East", norm))
    assert len(transport.bodies) > calls, "a new request asks again"
    assert not any("c000" in body["questions"] for body in transport.bodies[calls:]), "the workbook was prepared again"


def test_the_tab_in_view_is_the_requests_active_sheet():
    """On a workbook of several tabs both records name the tab the client has in view (engine/program/closed.py
    sheet_state), as the request carries it (engine/prepared.py RequestDatabase.active_sheet); without one, the first
    data table's tab."""
    from engine.prepared import scratch_database
    from engine.tables import csv_table
    from tests.test_answer import FakeTransport, _use_fake_reader, _with_transport

    _use_fake_reader()
    planner = _hermetic_planner()
    tables = [csv_table(_orders_csv(), "orders"), csv_table(_orders_csv(), "returns")]
    norm, _fks = planner.ingest(tables)

    def in_view(active):
        transport = FakeTransport(_choice(0.9))
        with scratch_database() as database:
            database.active_sheet = active
            _with_transport(transport, lambda: planner.select_query("total amount for East", norm))
        return {json.loads(body["input"][len("STATE:\n"):]).get("tab in view") for body in transport.bodies if "reading" in body["questions"]}

    assert in_view("returns") == {"returns"}
    assert in_view(None) == {"orders"}


def test_an_uploaded_sheets_grid_keeps_its_structure():
    """The own-data path reads each request table's grid (engine/tables.py table_grid): an uploaded sheet's CSV as
    plain rows, blank rows kept, cells typed as parse_rows types them and trailing blank columns trimmed, where the
    table's own rows drop blank rows and canonicalize the header. A table built from rows reads as its header and
    rows; a currency a conversation states is the grid's unit for its column, never a column the grid gains."""
    from decimal import Decimal

    from engine import dataset_semantics
    from engine.tables import csv_table, normalize_tables, table_from_rows

    text = "Report,,\n\nRegion,Amount,\n\"East\",1.50,\n,,\nWest,2,\n"
    table = csv_table(text, "sales")
    assert table["columns"][0] == "Report" and len(table["rows"]) == 3, table
    assert table["grid"] == {"name": "sales", "rows": [["Report", ""], [], ["Region", "Amount"], ["East", Decimal("1.50")], ["", ""], ["West", 2]]}, table["grid"]
    built = normalize_tables([table_from_rows("ref", ["Code", "Name"], [["A", "x"], ["B", "y"]])])[0]
    assert built["grid"] == {"name": "ref", "rows": [["Code", "Name"], ["A", "x"], ["B", "y"]]}
    assert normalize_tables([table])[0]["grid"] is table["grid"], "an uploaded sheet keeps its own grid"
    shared = table["grid"]
    upload = csv_table("Region,Amount\nEast,1\n\nWest,2\n", "orders")
    op = {"op": "set_measure_metadata", "table": "orders", "column": "Amount", "metadata": {"currency": "EUR"},
          "basis": {"source": "conversation", "text": "amounts are in euros", "attested": True}}
    records = dataset_semantics.apply([upload], [op])
    assert records[0]["currency_column"] in upload["columns"], "the table carries the private column the world rate joins on"
    assert upload["grid"]["rows"] == [["Region", "Amount"], ["East", 1], [], ["West", 2]], upload["grid"]
    assert upload["grid"]["units"] == {"Amount": "EUR"}, upload["grid"]
    assert table["grid"] is shared


TESTS = [
    test_a_longest_run_renders_alone_and_counts_unbroken_runs,
    test_the_selection_is_the_decisions_paths_listed_candidates,
    test_an_answer_most_likely_wrong_is_retried_never_asked_back,
    test_the_retry_reads_typed_copies_of_the_tables_and_leaves_the_database_alone,
    test_the_retry_is_told_to_put_the_answer_first,
    test_a_warned_answer_is_served_with_what_else_it_may_mean,
    test_an_analysis_runs_the_chosen_program_on_both_emitters,
    test_a_long_question_the_path_asks_back_asks_for_the_decomposition,
    test_the_decisions_paths_reading_certifies_its_calculation_but_a_conversion_it_could_not_make,
    test_the_store_shares_the_request_database_with_a_table_named_like_its_own,
    test_one_question_is_answered_once_per_request,
    test_the_tab_in_view_is_the_requests_active_sheet,
    test_an_uploaded_sheets_grid_keeps_its_structure,
    test_schema_graph_preserves_zero_padded_source_codes,
    test_evaluator_shards_are_disjoint_complete_and_merge_only_matching_checkpoints,
    test_star_is_only_a_projection_or_count_operand,
    test_pool_oracle_counts_only_eligible_denotation_hits_and_validates_checkpoints,
    test_a_duration_is_how_long_rows_lasted_not_a_quantity_and_an_interval,
    test_proposal_import_rejects_malformed_model_text,
    test_decoded_sql_normalizer_preserves_multiline_statements_and_strips_fences,
    test_a_text_column_is_an_aggregate_operand_only_where_it_ends_the_aggregate_phrase,
    test_a_decimal_comma_amount_is_not_totalled_as_hundreds,
    test_join_keys_are_read_as_the_executed_join_compares_them,
    test_an_explicit_zero_foreign_key_confidence_stays_zero,
    test_coordinated_aggregate_words_share_the_field_after_them,
    test_a_field_is_named_by_another_form_of_its_word,
    test_a_field_named_with_its_tables_abbreviation,
    test_an_explicit_list_names_the_outputs_their_owner_and_order,
    test_a_list_item_that_is_only_the_count_asks_for_the_count,
    test_the_opening_sentence_lists_its_coordinated_outputs,
    test_a_list_verb_after_a_comma_lists_the_outputs,
    test_along_with_adds_an_item_to_a_list,
    test_a_noun_joined_to_a_count_by_and_is_counted_too,
    test_find_opens_a_list_of_outputs,
    test_an_item_followed_by_of_and_its_rows_is_theirs,
    test_an_items_last_word_may_stand_for_its_fields_kind,
    test_a_country_holds_its_irregular_demonym,
    test_a_capital_initial_before_a_capitalized_word_is_a_word,
    test_the_row_with_the_maximum_is_the_row_holding_it,
    test_typographic_quotes_read_as_the_ascii_ones,
    test_a_possessive_is_its_word,
    test_a_word_spelling_out_a_fields_abbreviation_names_it,
    test_a_word_a_fields_name_abbreviates_spells_that_field_and_states_no_value,
    test_most_before_a_tables_name_counts_its_rows,
    test_a_comparison_symbol_is_its_word,
    test_a_count_counts_its_nouns_head,
    test_a_closed_opening_request_for_one_field_is_the_answer,
    test_and_past_a_condition_joins_conditions,
    test_declared_self_relationship_keeps_unrelated_queries_and_source_rows,
    test_spider_ingestion_keeps_discovered_and_declared_relationships,
    test_sql_import_maps_numeric_arithmetic_but_refuses_nonnumeric,
    test_sql_import_preserves_arithmetic_over_aggregates,
    test_sql_import_refuses_semantics_the_ast_cannot_preserve,
    test_sql_import_preserves_like_negation,
    test_sql_import_preserves_null_tests_and_lowercase,
    test_sql_import_round_trip_executes_and_matches,
    test_sql_import_preserves_distinct_self_join_roles,
    test_typed_ast_rejects_invalid_aggregate,
    test_grouped_ast_rejects_ungrouped_ordering,
    test_typed_ast_rejects_mismatched_literal_payloads,
    test_ast_rejects_indeterminate_set_and_aggregate_shapes,
    test_grouping_validation_sees_ordered_aggregates,
    test_composite_foreign_key_renders_and_executes_as_one_join,
    test_composite_join_validation_rejects_disconnected_predicate,
    test_arithmetic_expression_sum_renders_and_executes,
    test_arithmetic_division_preserves_real_semantics,
    test_currency_conversion_query_executes_end_to_end,
    test_world_aggregate_currency_binding_is_typed_and_unambiguous,
    test_arithmetic_expression_validation,
    test_date_ranges_lists_and_quarters_filter_one_span,
    test_the_coverage_gate_reads_the_months_a_query_compares,
    test_a_plural_reads_as_its_singular_everywhere,
    test_a_number_of_a_numeric_field_is_the_field,
    test_number_used_as_a_column_label_is_not_a_count_request,
    test_a_number_that_ends_a_fields_name_asks_for_the_field,
    test_literals_are_escaped_by_renderer,
    test_world_path_money_noun_naming_the_table_sums_its_money_column,
    test_world_path_reads_quantity_superlatives_and_counted_measures,
    test_operator_readout_never_reads_an_aggregate_off_a_closed_class_word,
    test_one_surrogate_key_rule_names_keys_not_measures_or_codes,
    test_soft_prediction_budget_does_not_abandon_work,
    test_recursive_ast_scalar_subquery_executes,
    test_recursive_ast_correlated_exists_executes,
    test_recursive_ast_set_query_in_derived_table_executes,
    test_spider_comparison_keeps_nonfinite_spellings_as_stable_text,
    test_shared_spider_evaluation_contract,
    test_weight_manifest_detects_tampered_bundle,
    test_world_own_data_route_preserves_ast_observability,
    test_world_target_uses_the_synced_iso_currency_column,
    test_schema_graph_resolves_normalized_foreign_key_names,
    test_spider_evaluator_does_not_count_all_errors_as_answered,
    test_the_table_a_list_names_after_its_items_by_its_last_word_owns_them,
]


# These serve a question through the Decisions path, whose stock reading imports torch: the engine image installs it,
# the public CI's hermetic environment does not, and there they are skipped, as skipped.
NEEDS_TORCH = (
    test_the_selection_is_the_decisions_paths_listed_candidates,
    test_an_answer_most_likely_wrong_is_retried_never_asked_back,
    test_the_retry_reads_typed_copies_of_the_tables_and_leaves_the_database_alone,
    test_the_retry_is_told_to_put_the_answer_first,
    test_a_warned_answer_is_served_with_what_else_it_may_mean,
    test_an_analysis_runs_the_chosen_program_on_both_emitters,
    test_a_long_question_the_path_asks_back_asks_for_the_decomposition,
    test_the_decisions_paths_reading_certifies_its_calculation_but_a_conversion_it_could_not_make,
    test_the_store_shares_the_request_database_with_a_table_named_like_its_own,
    test_one_question_is_answered_once_per_request,
    test_the_tab_in_view_is_the_requests_active_sheet,
)


def main():
    import importlib.util

    failed, skipped = [], 0
    for test in TESTS:
        if test in NEEDS_TORCH and importlib.util.find_spec("torch") is None:
            skipped += 1
            print(f"  SKIP {test.__name__}: needs torch (the engine image installs it)")
            continue
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nSQL AST: {len(TESTS) - len(failed) - skipped} passed, {len(failed)} failed" + (f", {skipped} skipped" if skipped else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
