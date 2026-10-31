"""Hermetic tests for the typed SQL AST (validation, rendering, execution), grounding, SQL import, the lexical readings
the planner's helpers keep, and the plumbing of the one own-data selection (engine/tables.py:select_query).

The readers (engine/readers) build every own-data candidate; their weights never load here. A test's pool is the
queries the readers would build for its question, written as SQL and imported into the typed AST
(regress/sql_import.py), and the readers' re-scorer and refusal are a stub (``StubReaders``).

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
from engine.sql_grounding import double_counted, grounded_members, join_pairs, literal_bindings
from engine.sql_rank import analyze_question
from engine.sql_schema import SchemaGraph
from regress.sql_import import import_sql, normalize_decoded_sql
from spider.probe.evalutil import run_with_budget
from engine.tables import READERS_MODEL, TableQuery
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


def test_planner_discovers_and_executes_same_name_currency_edge():
    """Ingestion keys an FX sheet's same-named currency column to the orders' currency, and the conversion the
    registered calculations build over that edge (engine/calculations/search.py, the expansions search_pool adds to
    the readers' plain total) executes to the converted total."""
    from engine.calculations.search import CalculationQueryExpander

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

    graph = SchemaGraph.from_tables(tables, fks)
    plain = import_sql('SELECT SUM("orders"."amount") FROM "orders"', graph)
    converted = CalculationQueryExpander(graph).expand(
        "total order amount in US dollars", [ScoredQuery(plain, render_query(plain), 0.0, ())])
    top = next(candidate for candidate in converted if "rate_to_usd" in candidate.sql)
    assert 'SUM(("orders"."amount" * "illustrative_fx_rates"."rate_to_usd"))' in top.sql, top.sql
    rows = execute(tables, top.sql)
    assert len(rows) == 1 and abs(rows[0][0] - 1174.6) < 1e-9, (top.sql, rows)


def test_currency_conversion_survives_uploaded_column_case():
    # REGRESSION: engine.tables normalizes TABLE names but preserves COLUMN case, while the rate column is
    # synthesized lowercase ("rate_to_usd"). An exact-case membership test therefore bound nothing for an FX
    # sheet headed "Rate_To_USD", and the planner silently answered with the UNCONVERTED SUM — a wrong
    # number, no clarify. Same rows as the lowercase test above, headers cased the way a spreadsheet export
    # actually arrives; the conversion is the calculations' expansion of the readers' plain total.
    from engine.calculations.search import CalculationQueryExpander

    orders = {"name": "orders", "columns": ["Currency", "Amount"],
              "rows": [["EUR", 520], ["EUR", 450], ["GBP", 100]]}
    rates = {"name": "illustrative fx rates", "columns": ["Currency", "Rate_To_USD"],
             "rows": [["USD", 1.0], ["EUR", 1.08], ["GBP", 1.27], ["INR", 0.012]]}
    tables, fks = TableQuery().ingest([orders, rates])
    graph = SchemaGraph.from_tables(tables, fks)
    plain = import_sql('SELECT SUM("orders"."Amount") FROM "orders"', graph)
    converted = CalculationQueryExpander(graph).expand(
        "total order amount in US dollars", [ScoredQuery(plain, render_query(plain), 0.0, ())])
    top = next((candidate for candidate in converted if "Rate_To_USD" in candidate.sql), None)
    assert top is not None, [candidate.sql for candidate in converted]
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


class StubReaders:
    """engine/readers/search.py:ReadingSearch as search_pool and select_query read it, without its weights.

    ``pool`` returns the planner's ``pool``: the queries its readers would build, a list for every question or a dict
    by question (a planner without one reads no question). ``rescore`` gives each candidate that ran its pool
    ``score`` (0.0 for a query written for a test, so ties keep the pool's order), and the learned refusal
    (``confidence``, ``confident``) accepts every choice unless built with ``confident=False``."""

    def __init__(self, planner, confident=True):
        self.planner = planner
        self.accepts = confident

    def pool(self, question, graph):
        readings = self.planner.pool
        if readings is None:
            raise AssertionError("the hermetic planner has no readers: give it the pool its readers would build")
        return list(readings.get(question, ()) if isinstance(readings, dict) else readings)

    def rescore(self, question, pool, shapes):
        return [member.score if shape is not None else None for member, shape in zip(pool, shapes)]

    def confidence(self, pool, scores, shapes, chosen):
        return 0.9 if self.accepts else 0.1

    def confident(self, confidence):
        return confidence is None or confidence >= 0.5


def _hermetic_planner(confident=True):
    """The production TableQuery without its encoder or the readers' weights: ``schema`` types columns from their
    values, and search_pool and select_query read ``StubReaders`` (``planner.pool``, ``planner.readers``) where
    serving reads the readers; the calculations' expansions, execution, grounding and choice are production's."""
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

        def search_pool(self, *args, **kwargs):
            with patch("engine.readers.search.reading_search", return_value=self.readers):
                return super().search_pool(*args, **kwargs)

        def select_query(self, *args, **kwargs):
            with patch("engine.readers.search.reading_search", return_value=self.readers):
                return super().select_query(*args, **kwargs)

    planner = HermeticPlanner()
    planner.pool = None
    planner.readers = StubReaders(planner, confident)
    return planner


def _request(planner, tables=None):
    """(norm, fks, sch, tablemap) for one request's tables, prepared the way serve() prepares them."""
    norm, fks = planner.ingest(tables or [PEOPLE])
    sch, _, tablemap = planner.schema(norm, fks)
    return norm, fks, sch, tablemap


def _select(planner, question, tables=None, searched=None):
    """The production own-data selection. ``searched`` is the pool select_query runs, grounds and chooses from;
    None reads the planner's readers for it, as serving does."""
    return planner.select_query(question, *_request(planner, tables), searched=searched)


def _model_query(planner, sql, tables=None, score=0.0, read_as="model:query"):
    """A query the readers could build, written as SQL and imported into the typed AST against the request's schema
    (regress/sql_import.py): a pool member select_query can run, ground and choose. ``score`` is the re-scorer's
    logit for it (``StubReaders.rescore``) and ``read_as`` its read-as line, the first item of its evidence."""
    norm, fks, sch, _ = _request(planner, tables)
    query = import_sql(sql, SchemaGraph.from_planner(sch, fks))
    validate_query(query)
    return ScoredQuery(query, render_query(query), score, (read_as,))


def test_select_query_serves_the_reading_that_keeps_the_named_date():
    """Cloud product suite, 2026-10-02: with an undated reading ranked first, "How many contracts
    were signed before July 10, 2026?" was served without its date and declined. The served
    selection keeps to the readings that realize the date when one does."""
    contracts = {"name": "contracts", "columns": ["contract", "value", "signed"], "rows": [
        ["NDA", 5000, "2026-07-06"], ["License", 900000, "2026-07-05"], ["Lease", 30000, "2026-07-12"]]}
    planner = _hermetic_planner()
    question = "How many contracts were signed before July 10, 2026?"
    undated = _model_query(planner, "SELECT COUNT(*) FROM contracts", [contracts], score=1.0)
    dated = _model_query(planner, "SELECT COUNT(*) FROM contracts WHERE signed < '2026-07-10'", [contracts])
    selection = _select(planner, question, [contracts], searched=[undated, dated])
    assert selection.ranking[0] == 0, "the undated reading is ranked first"
    served = selection.candidate
    assert selection.selected == 1 and "'2026-07-10'" in served.sql, served.sql
    assert selection.date_satisfied[selection.selected] and not selection.date_satisfied[0]
    assert selection.record()["date_satisfied"] is True
    # Contrast: without a date in the question the ranking's choice stands.
    plain = _select(planner, "How many contracts are there?", [contracts], searched=[undated, dated])
    assert plain.candidate.sql == undated.sql and not any(plain.date_satisfied)


def test_select_query_never_chooses_a_query_that_does_not_run():
    """A pooled query that does not finish within the SQLite step budget (EXECUTION_OP_LIMIT) is
    never chosen, and a question whose pool holds none that runs is not answered."""
    planner = _hermetic_planner()
    planner.pool = [_model_query(planner, 'SELECT "Name" FROM "people"')]
    with patch("engine.sql_rank.EXECUTION_OP_LIMIT", 1):
        selection = _select(planner, "list person names")
        response = planner.serve([PEOPLE], "list person names")
    assert selection.pool and not any(selection.executable)
    assert selection.ranking == () and selection.selected is None and selection.candidate is None
    assert selection.record()["executable"] == 0
    assert response["valid"] is False and response["read_as"] is None and "clarify" not in response
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
    from engine.prepared import RequestDatabase
    database = RequestDatabase()
    connection = database.load(tablemap, [
        {"table": "subscriptions", "name": name, "affinity": "INTEGER" if name in ("Amount", "Quantity") else "TEXT"}
        for name in columns])
    steps = 0

    def count():
        nonlocal steps
        steps += 1000
    connection.set_progress_handler(count, 1000)
    assert len(connection.execute(render_query(listed)).fetchall()) == 50000
    database.close()
    assert 4 * steps < execution_op_limit(listed, tablemap), steps

    wide = {"wide": {"columns": list(range(50)), "rows": range(200000)}}           # 10M cells
    assert execution_op_limit(listed, wide) == EXECUTION_OP_LIMIT                    # a table it does not hold
    wide_listed = SelectQuery(select=(SelectItem(ColumnRef("wide", "0", SQLType.TEXT)),), from_table="wide")
    assert execution_op_limit(wide_listed, wide) == EXECUTION_OP_LIMIT               # the ceiling


def test_select_query_copies_the_tables_once_and_runs_each_query_once():
    """select_query copies the request's tables into its database once and runs each distinct query once
    (engine/tables.py _PoolRuns, engine/prepared.py): a second copy of a large workbook per pool member, or a
    query run again, is time no answer needs (2026-10-05)."""
    from engine.prepared import scratch_database
    planner = _hermetic_planner()
    amounts = {"name": "orders", "columns": ["Amount"], "rows": [[100], [200]]}
    total = _model_query(planner, 'SELECT SUM("orders"."Amount") FROM "orders"', [amounts])
    listed = _model_query(planner, 'SELECT "orders"."Amount" FROM "orders"', [amounts])
    copies, guarded = [], []
    guard = planner.guard
    planner.guard = lambda sql: guarded.append(sql) or guard(sql)
    with scratch_database() as database:
        database.connection.set_trace_callback(
            lambda statement: copies.append(statement) if statement.startswith("CREATE TABLE main.") else None)
        selection = _select(planner, "what is total Amount", [amounts], searched=[total, listed, total])
    assert selection.candidate.sql == total.sql and selection.executable == (True, True, True)
    assert len(copies) == 1, f"{len(copies)} copies of the request's tables"
    assert guarded == [total.sql, listed.sql], guarded


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
    'Lyon' is a city, and it was ranked first. Ranked first, it is still never served: the reading
    that binds 'Lyon' to the column that holds it is."""
    misbound, bound = "\"customer_name\" = 'Lyon'", "\"city\" = 'Lyon'"
    planner = _hermetic_planner()
    question = "product names bought by Lyon customers"
    model = _model_query(planner, "SELECT product_name FROM purchases WHERE customer_name = 'Lyon'",
                         [PURCHASES])
    held = _model_query(planner, "SELECT product_name FROM purchases WHERE city = 'Lyon'", [PURCHASES])
    selection = _select(planner, question, [PURCHASES], searched=[model, held])
    assert misbound in selection.pool[0].sql
    assert selection.executable[0] and not selection.grounded[0] and 0 not in selection.ranking
    assert selection.selected == 1 and bound in selection.candidate.sql, selection.candidate.sql
    assert selection.record()["misgrounded"] == 1

    # Same profile, a value the column does hold: the model's query is served as before.
    alice = "\"customer_name\" = 'Alice'"
    question = "product names bought by Alice"
    model = _model_query(planner, "SELECT product_name FROM purchases WHERE customer_name = 'Alice'",
                         [PURCHASES])
    selection = _select(planner, question, [PURCHASES], searched=[model, held])
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
    bridged = _model_query(planner, (
        "SELECT T1.customer_name , T4.product_name FROM customers AS T1 JOIN orders AS T2 ON "
        "T1.customer_id = T2.customer_id JOIN order_items AS T3 ON T2.order_id = T3.order_id "
        "JOIN products AS T4 ON T3.product_id = T4.product_id"), tables)
    selection = _select(planner, question, tables, searched=[model, bridged])
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
    names = _model_query(planner, "SELECT customer_name FROM customers", tables)
    selection = _select(planner, question, tables, searched=[model, names])
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


def test_named_request_decomposes_a_compound_question_a_single_query_answers_in_part():
    """The readers read "products no Paris customer bought" as a set difference, and a single join listing
    what Paris DID buy is ranked first. A named product request must ask for decomposition and execute
    nothing: a single query cannot answer a compound question. Evaluation (no analysis context) serves
    selection's choice."""

    from engine.decomposition import search_probe
    from engine.deterministic.context import analysis_execution_context
    from tests.test_datasets import DATASET_DIR, _tables

    tables = _tables(DATASET_DIR / "complex-unsold-products")
    question = "List the product names that no customer from Paris has bought, ordered by product name."
    planner = _hermetic_planner()
    unsold = _model_query(planner, ("SELECT product_name FROM products EXCEPT "
                                    "SELECT product_name FROM purchases WHERE city = 'Paris'"), tables)
    one_join = _model_query(planner, (
        "SELECT products.product_name FROM products JOIN purchases "
        "ON products.product_name = purchases.product_name "
        "WHERE purchases.city = 'Paris' ORDER BY products.product_name"), tables, score=1.0)
    planner.pool = [unsold, one_join]
    assert isinstance(unsold.query, SetQuery)

    evaluated = planner.serve(tables, question)
    assert evaluated["valid"] and evaluated["sql"] == one_join.sql, evaluated
    assert evaluated["result"]["rows"]
    with analysis_execution_context({"slug": "unsold", "revision": 1}, "c_" + "9" * 32), \
            patch.object(planner, "execute", side_effect=AssertionError("partial answer executed")):
        named = planner.serve(tables, question)
    assert named.get("decomposition_required"), named
    assert named["result"] is None and named["error"] is None

    # The compose path's probe asks the same question of the readers' reading alone: no selection runs.
    with patch.object(planner, "select_query", side_effect=AssertionError("the probe ran selection")):
        probe = search_probe(planner, tables, question)
    assert probe.decomposition_required and probe.selection is None


def test_a_compound_named_request_asks_for_decomposition_before_selection_runs():
    """tests.test_complex_datasets failed on the CPU 7B SQL model (2026-09-30): a named compound
    request decoded the whole prompt before asking whether it was read as compound, the decode ran
    past its CPU budget, and the request failed instead of asking for a decomposition. The readers'
    most probable reading alone decides compound structure, so a named compound request runs no
    selection: no pool execution."""

    from engine.deterministic.context import analysis_execution_context
    from tests.test_datasets import DATASET_DIR, _tables

    tables = _tables(DATASET_DIR / "complex-unsold-products")
    question = "List the product names that no customer from Paris has bought, ordered by product name."
    planner = _hermetic_planner()
    planner.pool = [_model_query(planner, ("SELECT product_name FROM products EXCEPT "
                                           "SELECT product_name FROM purchases WHERE city = 'Paris'"), tables)]
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


def test_named_request_never_serves_or_decomposes_a_lower_read_set_operation():
    """The live demo gate caught this after a SQL model's queries could be served. "total amount for
    restaurants in United States" read as an INTERSECT over invented values, ranked first, made every
    named request ask for a decomposition. Compound structure is the readers' most probable reading: when it
    is one goal, a named request neither decomposes nor serves a set operation the re-scorer ranks first, it
    serves the best-ranked single query. Evaluation still serves selection's choice."""

    from engine.decomposition import search_probe
    from engine.deterministic.context import analysis_execution_context
    from tests.test_datasets import DATASET_DIR, _tables

    (sheet,) = _tables(DATASET_DIR / "neartail-catering")
    tables = [dict(sheet, rows=[[name, event, int(amount)] for name, event, amount in sheet["rows"]])]
    question = "total amount for restaurants in United States"
    planner = _hermetic_planner()
    total = _model_query(planner, "SELECT SUM(amount) FROM catering", tables)
    invented = _model_query(planner, ("SELECT SUM(amount) FROM catering WHERE event = 'breakfast' "
                                      "INTERSECT SELECT SUM(amount) FROM catering WHERE event = 'lunch'"),
                            tables, score=1.0)
    planner.pool = [total, invented]
    selection = _select(planner, question, tables)
    assert selection.candidate.sql == invented.sql, "evaluation serves the re-scorer's choice"
    probe = search_probe(planner, tables, question)
    assert not probe.decomposition_required and probe.selection.selected == 1
    with analysis_execution_context({"slug": "catering", "revision": 1}, "c_" + "8" * 32):
        named = planner.serve(tables, question)
    assert not named.get("decomposition_required"), named
    # A named request runs the served query's deterministic program; its SQL is the program's, its AST the total.
    assert named["valid"] and named["ast"] == repr(total.query), named
    assert named["deterministic"]["mode"] == "python" and named["result"]["rows"] == [[26000]], named
    assert named["selection"]["selected"] == 0


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
    key at all. Join grounding read keys through float, so accounts 2**53 and 2**53 + 1 were one key and a
    total over their payments was read as double counted. Both now read a key as the join does."""
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
    accounts = {"name": "accounts", "columns": ["account_id", "owner"], "rows": [[2**53, "Ann"], [2**53 + 1, "Bo"]]}
    payments = {"name": "payments", "columns": ["payment_id", "account_id", "amount"],
                "rows": [[1, 2**53, 5], [2, 2**53 + 1, 7], [3, 2**53, 1]]}
    graph = SchemaGraph.from_tables([accounts, payments], [("payments", "account_id", "accounts", "account_id")])
    total = import_sql("SELECT accounts.owner, SUM(payments.amount) FROM payments JOIN accounts "
                       "ON payments.account_id = accounts.account_id GROUP BY accounts.owner", graph)
    assert double_counted(total, {"accounts": accounts, "payments": payments}) == ()
    # Contrast: an account listed twice does repeat each payment the join matches to it.
    twice = {**accounts, "rows": accounts["rows"] + [[2**53, "Ann"]]}
    assert len(double_counted(total, {"accounts": twice, "payments": payments})) == 1


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


def test_a_superlative_over_the_shown_field_is_one_value():
    """Spider DEV 988, 989 (2026-10-14): "What is the age of the oldest dog?" ranked the dogs by birth date and served
    every dog tied at the earliest one. A ranking showing only what it is ordered by serves one row, its ties showing
    the same value (sql_ast.keep_ties). Contrast: a ranking showing another field keeps every tied row."""
    from engine.sql_ast import keep_ties
    age = ColumnRef("Dogs", "age", SQLType.INTEGER)
    name = ColumnRef("Dogs", "name", SQLType.TEXT)
    oldest = SelectQuery((SelectItem(age),), "Dogs", order_by=(OrderTerm(age, "DESC"),), limit=1)
    assert keep_ties(oldest) == oldest
    assert keep_ties(SelectQuery((SelectItem(name),), "Dogs", order_by=(OrderTerm(age, "DESC"),), limit=1)).with_ties


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
    planner.pool = [_model_query(planner, ("SELECT COUNT(*) FROM shipments JOIN carriers "
                                           "ON shipments.carrier = carriers.id WHERE carriers.name = 'Alpha'"), tables)]
    cache = {}
    with patch.object(planner, "schema", wraps=planner.schema) as schema:
        actual = ast_predict(planner, tables, "How many shipments use carrier Alpha?", declared, cache)
        merged = schema.call_args.args[1]
        pairs = {(edge["from_table"], edge["from_col"], edge["to_table"], edge["to_col"]) for edge in merged}
        assert ("shipments", "carrier", "carriers", "id") in pairs, merged
        assert ("shipments", "destination_id", "ports", "id") in pairs, merged
        assert not any(edge["from_col"] == "weight" for edge in merged), merged
        assert actual.get("ok") and actual["rows"] == [[2]], actual
        # A repeated question reuses that one schema; a changed declaration is
        # a different ingestion input even for the same table object identities.
        again = ast_predict(planner, tables, "How many shipments use carrier Alpha?", declared, cache)
        assert again.get("rows") == actual["rows"] and schema.call_count == 1
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
    planner.pool = {question: [_model_query(planner, sql, tables)] for question, sql in (
        ("List artist names", 'SELECT "Name" FROM "artists"'),
        ("List employee names", 'SELECT "Name" FROM "employees"'),
        ("How many employees are there?", 'SELECT COUNT(*) FROM "employees"'))}
    unrelated = ast_predict(planner, tables, "List artist names", declared)
    assert unrelated["ok"] and Counter(map(tuple, unrelated["rows"])) == Counter({("Nora",): 1, ("Nico",): 1}), unrelated
    listed = ast_predict(planner, tables, "List employee names", declared)
    assert listed["ok"] and Counter(map(tuple, listed["rows"])) == Counter({("A",): 2, ("B",): 1}), listed
    counted = ast_predict(planner, tables, "How many employees are there?", declared)
    assert counted["ok"] and counted["rows"] == [[3]], counted
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


def test_one_value_bounded_both_ways_is_contradictory():
    """Spider DEV 856, 857, 2026-10-17: "Show the record companies shared by orchestras founded before 2003 and after
    2003." was served as Year_of_Founded > 2003 AND Year_of_Founded < 2003, which matches no row. A conjunction
    bounding a column below where it bounds it above is contradictory (sql_ast.contradictory). Contrast: a range is
    no contradiction."""
    from engine.sql_ast import and_predicates, contradictory
    founded = ColumnRef("orchestra", "Year_of_Founded", SQLType.INTEGER)
    after, before = Comparison(founded, ">", Literal(2003)), Comparison(founded, "<", Literal(2003))
    company = (SelectItem(ColumnRef("orchestra", "Record_Company", SQLType.TEXT)),)
    assert contradictory(SelectQuery(company, "orchestra", where=and_predicates([after, before])))
    assert not contradictory(SelectQuery(company, "orchestra", where=and_predicates([
        after, Comparison(founded, "<", Literal(2010))])))


def test_a_value_bounded_away_from_its_equality_is_contradictory():
    """Fresh holdout 189, 2026-10-17: "Provide the full names of employees earning more than the employee with id 163."
    was served as EMPLOYEE_ID = 163 AND EMPLOYEE_ID > 163, which matches no row: an equality bounds its column from both
    sides, and the bound away from it passed the contradiction filter. Contrast: an equality inside the bound is no
    contradiction, nor one of another column."""
    from engine.sql_ast import and_predicates, contradictory
    employee = ColumnRef("employees", "EMPLOYEE_ID", SQLType.INTEGER)
    salary = ColumnRef("employees", "SALARY", SQLType.INTEGER)
    names = (SelectItem(ColumnRef("employees", "FIRST_NAME", SQLType.TEXT)),)
    equal = Comparison(employee, "=", Literal(163))

    def where(*terms):
        return SelectQuery(names, "employees", where=and_predicates(list(terms)))

    assert contradictory(where(equal, Comparison(employee, ">", Literal(163))))
    assert contradictory(where(Comparison(employee, "<", Literal(100)), equal))
    assert not contradictory(where(equal, Comparison(employee, ">=", Literal(163))))
    assert not contradictory(where(equal, Comparison(employee, ">", Literal(100))))
    assert not contradictory(where(equal, Comparison(salary, ">", Literal(163))))


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


def test_evaluator_grades_the_served_selection():
    from spider.probe.full_eval import ast_predict

    planner = _hermetic_planner()
    planner.pool = [_model_query(planner, 'SELECT "Name" FROM "people"'),
                    _model_query(planner, 'SELECT COUNT(*) FROM "people"')]
    served = _select(planner, "list person names")
    record = ast_predict(planner, [PEOPLE], "list person names")
    assert record["ok"] and record["sql"] == served.candidate.sql
    assert record["selected_candidate_rank"] == served.selected
    assert record["selection"] == served.record()
    assert "served_by" not in record
    oracle = ast_predict(planner, [PEOPLE], "list person names", selection="pool_oracle")
    assert [entry["sql"] for entry in oracle["pool_execution"]] == [c.sql for c in served.pool]
    for rank, entry in enumerate(oracle["pool_execution"]):
        assert entry["score"] == served.pool[rank].score
        assert entry["executable"] == served.executable[rank]
        assert entry["grounded"] == served.grounded[rank]
        assert entry["eligible"] == (served.executable[rank] and served.grounded[rank])


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
    assert record["pool"][1]["features"] == {"projection": 1.0}
    assert record["pool"][1]["evidence"] == ["extrema:projection"]
    assert record["pool"][1]["calculation_satisfied"] is False
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


def test_shared_ranking_rule_preserves_date_then_double_count_then_calculation_precedence():
    from engine.sql_rank import select_ranked_candidate
    assert select_ranked_candidate((), ()) is None
    # A total of money over several currencies is served per currency, unless the question already says which.
    from engine.sql_rank import mixed_currencies, per_currency
    from engine.sql_ast import Aggregate, Comparison, Literal, SelectItem, SelectQuery
    from engine.sql_schema import SchemaGraph
    money = SchemaGraph.from_tables([{"name": "t", "columns": ["id", "Currency", "Amount", "Quantity"],
                                      "rows": [[1, "usd", 10, 1], [2, "inr", 800, 2], [3, "usd", 5, 1]]},
                                     {"name": "one", "columns": ["Currency", "Amount"], "rows": [["usd", 1], ["usd", 2]]}], [])
    currencies = mixed_currencies(money)
    assert set(currencies) == {"t"}, currencies
    amount, quantity = money.column_map[("t", "Amount")].ref, money.column_map[("t", "Quantity")].ref
    currency = money.column_map[("t", "Currency")].ref
    total = SelectQuery((SelectItem(Aggregate("SUM", amount)),), "t")
    split = per_currency(total, currencies)
    assert split.group_by == (currency,) and split.select[0].expression == currency
    # Already filtered or grouped by currency, not money, a row list, or a sheet of one currency: as it was.
    filtered = SelectQuery(total.select, "t", where=Comparison(currency, "=", Literal("usd")))
    assert per_currency(filtered, currencies) is filtered
    grouped = SelectQuery((SelectItem(currency), SelectItem(Aggregate("SUM", amount))), "t", group_by=(currency,))
    assert per_currency(grouped, currencies) is grouped
    units = SelectQuery((SelectItem(Aggregate("SUM", quantity)),), "t")
    assert per_currency(units, currencies) is units
    assert per_currency(total, {}) is total
    # Within a group too: a plan's total of mixed money is a sum of nothing ("by Plan and Currency", read without it).
    identity = money.column_map[("t", "id")].ref
    by_id = SelectQuery((SelectItem(identity), SelectItem(Aggregate("SUM", amount))), "t", group_by=(identity,))
    split = per_currency(by_id, currencies)
    assert split.group_by == (identity, currency), split.group_by
    assert [item.expression for item in split.select] == [identity, currency, Aggregate("SUM", amount)], split.select
    first = SelectQuery((SelectItem(Aggregate("SUM", amount)), SelectItem(identity)), "t", group_by=(identity,))
    assert [item.expression for item in per_currency(first, currencies).select] == [
        Aggregate("SUM", amount), identity, currency]
    ranked = SelectQuery(by_id.select, "t", group_by=(identity,), limit=1)
    assert per_currency(ranked, currencies) is ranked, "the top group stays one group"
    # A test of a column of few values against a value it does not hold is named; one of many values, or a held one, not.
    from engine.sql_grounding import absent_value
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import (Aggregate, ColumnRef, Comparison, Literal, OrderTerm, SelectItem, SelectQuery, SQLType,
                                named_groups, render_query)
    graph = SchemaGraph.from_tables([{"name": "t", "columns": ["id", "status", "note"],
                                      "rows": [[i, ["active", "canceled"][i % 2], f"n{i}"] for i in range(40)]}], [])
    status = graph.column_map[("t", "status")].ref
    note = graph.column_map[("t", "note")].ref
    star = (SelectItem(status),)
    churned = SelectQuery(star, "t", where=Comparison(status, "=", Literal("churned")))
    assert absent_value(churned, graph) == ("status", "churned", ("active", "canceled"))
    assert absent_value(SelectQuery(star, "t", where=Comparison(status, "=", Literal("Canceled"))), graph) is None
    assert absent_value(SelectQuery(star, "t", where=Comparison(note, "=", Literal("gone"))), graph) is None
    # A ranking of groups ranks the groups that have a name; a listing of groups keeps its blank.
    industry, amount = ColumnRef("t", "industry", SQLType.TEXT), ColumnRef("t", "amount", SQLType.INTEGER)
    total = Aggregate("SUM", amount)
    ranking = SelectQuery((SelectItem(industry),), "t", group_by=(industry,),
                          order_by=(OrderTerm(total, "DESC"),), limit=1)
    assert "\"t\".\"industry\" <> ''" in render_query(named_groups(ranking))
    listing = SelectQuery((SelectItem(industry), SelectItem(total)), "t", group_by=(industry,))
    assert named_groups(listing) is listing
    plain = SelectQuery((SelectItem(industry),), "t", order_by=(OrderTerm(amount, "DESC"),), limit=1)
    assert named_groups(plain) is plain
    year = ColumnRef("t", "year", SQLType.INTEGER)
    by_year = SelectQuery((SelectItem(year),), "t", group_by=(year,), order_by=(OrderTerm(total, "DESC"),), limit=1)
    assert "\"t\".\"year\" IS NOT NULL" in render_query(named_groups(by_year))
    # A reading that matches nothing does not beat a more probable reading that finds rows, whatever the re-scorer
    # thinks of its words; the readers' most probable reading keeps an empty answer ("orders from Mars").
    from types import SimpleNamespace
    from engine.sql_rank import outread_by_rows
    pool = [SimpleNamespace(score=-0.4), SimpleNamespace(score=-1.0), SimpleNamespace(score=-2.0)]
    outread = outread_by_rows(pool, (1, 2, 0), [False, True, False])
    assert outread == (False, True, False)
    assert select_ranked_candidate((1, 2, 0), [False] * 3, outread=outread) == 2
    assert outread_by_rows(pool, (0, 1, 2), [True, False, False]) == (False, False, False)
    assert select_ranked_candidate((1, 0), [False] * 3, outread=outread_by_rows(pool, (1, 0), [True] * 3)) == 1
    # A more probable reading that is not eligible (it did not run, or misreads a column) outreads nothing.
    assert outread_by_rows(pool, (1,), [False, True, False]) == (False, False, False)
    assert select_ranked_candidate((2, 1, 0), (False, True, False)) == 1
    # A query that keeps the named dates comes first; the calculation rule applies among them.
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (True, False, False)) == 0
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (True, True, False)) == 1
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (False,) * 3) == 1
    # Among those, a query whose SUM or AVG reads only rows its joins repeat gives way to one that does not, even
    # to a satisfied calculation; when every one does, the order stands.
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (), (False, True, True)) == 0
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (), (True,) * 3) == 2
    assert select_ranked_candidate((2, 1, 0), (False,) * 3, (False, False, True), (False, False, True)) == 2
    assert select_ranked_candidate((2, 1, 0), (False, True, False), (), (False, True, False)) == 2


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


def test_two_values_of_one_column_are_either():
    """Probe, 2026-10-02: "total amount from Paris and Lyon" filtered city = 'Paris' AND city =
    'Lyon' and answered nothing. No row holds both, and no Spider gold query conjoins two such values
    (Spider writes "the continents Asia and Europe" as OR): the conjunction is contradictory, its
    disjunction is what either value means (sql_ast.conjunction), and the served selection grounds no
    such conjunction (engine/sql_grounding.py)."""
    from engine.sql_ast import BooleanExpr, conjunction, contradictory

    orders = {"name": "orders", "columns": ["order ID", "customer", "amount", "city"], "rows": [
        [1, "Ada", 100, "Paris"], [2, "Ben", 29, "Paris"], [3, "Cy", 60, "Lyon"], [4, "Dee", 12, "Marseille"],
        [5, "Eve", 6, "Nice"]]}
    city = ColumnRef("orders", "city", SQLType.TEXT)
    both = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "orders",
                       where=BooleanExpr("AND", (Comparison(city, "=", Literal("Paris", SQLType.TEXT)),
                                                 Comparison(city, "=", Literal("Lyon", SQLType.TEXT)))))
    assert contradictory(both)
    either = SelectQuery(both.select, "orders", where=conjunction(both.where.terms))
    assert isinstance(either.where, BooleanExpr) and either.where.operator == "OR", either.where
    assert execute([orders], render_query(either)) == [(3,)]
    pool = [ScoredQuery(both, render_query(both), 0.0, ()), ScoredQuery(either, render_query(either), 0.0, ())]
    assert grounded_members(pool, {"orders": orders}, SchemaGraph.from_tables([orders], [])) == (False, True)
    # Contrast: a self-join compares two aliases, not one column.
    first, second = ColumnRef("T2", "city", SQLType.TEXT), ColumnRef("T3", "city", SQLType.TEXT)
    route = BooleanExpr("AND", (Comparison(first, "=", Literal("Ashley", SQLType.TEXT)),
                                Comparison(second, "=", Literal("Aberdeen", SQLType.TEXT))))
    assert conjunction(route.terms) == route


def test_a_value_compared_as_text_and_as_a_number_is_one_value():
    """Review of the either-value rewrite (2026-10-02): a value compared as text and as a number is one value, not a
    conflict (sql_ast.contradictory)."""
    from engine.sql_ast import BooleanExpr, contradictory

    code = ColumnRef("orders", "order ID", SQLType.TEXT)
    same = SelectQuery((SelectItem(Star()),), "orders", where=BooleanExpr("AND", (
        Comparison(code, "=", Literal("1", SQLType.TEXT)), Comparison(code, "=", Literal(1, SQLType.INTEGER)))))
    assert not contradictory(same)


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
    """A customer's Stripe workbook, 2026-10-04: three subscription exports with one layout and a report of each.
    An answer from one of the copies says which it read, and the others that could answer, so it is not taken for
    the workbook's total; not when the question names the tab (engine/tables.py _unread_copies,
    SchemaGraph.layout_copies)."""
    workbook = _subscription_workbook()
    question = "What is the total Amount broken down by Plan and Currency?"
    by_plan = ('SELECT "{t}"."Plan", "{t}"."Currency", SUM("{t}"."Amount") FROM "{t}" '
               'GROUP BY "{t}"."Plan", "{t}"."Currency"')
    in_si = "What is the total Amount in SI by Plan?"
    planner = _hermetic_planner()
    planner.pool = {question: [_model_query(planner, by_plan.format(t="NT"), workbook)],
                    in_si: [_model_query(planner, 'SELECT "SI"."Plan", SUM("SI"."Amount") FROM "SI" GROUP BY "SI"."Plan"',
                                         workbook)]}
    served = planner.serve(workbook, question)
    assert sorted(map(tuple, served["result"]["rows"])) == [("price_a", "eur", 9), ("price_a", "usd", 20), ("price_b", "eur", 7), ("price_b", "usd", 8)], served["sql"]
    assert served["layout_copies"] == {"read": ["NT"], "others": ["SI", "FF"]}, served.get("layout_copies")
    named = planner.serve(workbook, in_si)
    # SI's money is in two currencies, so each plan's total is per currency (sql_rank.per_currency): 40 usd + 5 inr is no 45.
    assert sorted(map(tuple, named["result"]["rows"])) == [("price_c", "inr", 5), ("price_c", "usd", 40),
                                                           ("price_g", "usd", 40)], named["sql"]
    assert "layout_copies" not in named
    # A near copy (SI with one more column) is named too; another table that merely has the column read is not.
    near_workbook = _subscription_workbook(extra_si_column=True)
    planner.pool = [_model_query(planner, by_plan.format(t="NT"), near_workbook)]
    near = planner.serve(near_workbook, question)
    assert set(near["layout_copies"]["others"]) == {"NT", "SI", "FF"} - set(near["layout_copies"]["read"]), near.get("layout_copies")
    customers = {"name": "customers", "columns": ["customer_id", "name", "country"],
                 "rows": [["c1", "Ada", "France"], ["c2", "Lin", "Japan"]]}
    suppliers = {"name": "suppliers", "columns": ["supplier_id", "name", "country"],
                 "rows": [["s1", "Acme", "Germany"], ["s2", "Bolt", "France"]]}
    planner.pool = [_model_query(planner, 'SELECT "customers"."country" FROM "customers"', [customers, suppliers])]
    listed = planner.serve([customers, suppliers], "list the countries")
    assert listed["valid"] and "layout_copies" not in listed, listed.get("layout_copies")
    # The narrowed graph keeps its parent's value index for the tables it keeps.
    graph = SchemaGraph.from_tables(workbook, [])
    kept = [column for column in graph.columns if column.ref.table not in {"SI", "FF"}]
    assert graph.without(frozenset({"SI", "FF"})).value_index == SchemaGraph(kept, graph.foreign_keys).value_index
    # Negative: tables with one layout that foreign keys reference are different things (two code lookups),
    # and so are lists of one or two columns.
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
    reported = _model_query(planner, (
        'SELECT "NT"."Plan", "NT"."Currency", SUM("NT_Report"."Total Amount") FROM "NT" JOIN "NT_Report" '
        'ON "NT"."Product" = "NT_Report"."Product" GROUP BY "NT"."Plan", "NT"."Currency"'), workbook, score=1.0)
    own = _model_query(planner, ('SELECT "NT"."Plan", "NT"."Currency", SUM("NT"."Amount") FROM "NT" '
                                 'GROUP BY "NT"."Plan", "NT"."Currency"'), workbook)
    planner.pool = [reported, own]
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


def test_one_of_two_columns_that_share_a_header_is_asked_about():
    """A sheet headed "id, Amount, Amount, Units" names its fields "Amount [column B]" and "Amount [column C]", and
    "total Amount" summed column B alone (tests.test_scale, 2026-10-23): a reading of one of two columns that share a
    header, the question not saying which, is not eligible, and the reply asks which. Contrast: the question that
    names the column letter is answered, and so is one about another column."""
    from engine.tables import csv_table

    messy = csv_table("id,Amount,Amount,Units\n1,10,20,2\n2,30,40,3", "orders")
    planner = _hermetic_planner()
    planner.pool = {
        "total Amount": [_model_query(planner, 'SELECT SUM("Amount [column B]") FROM "orders"', [messy])],
        "total Amount in column C": [_model_query(planner, 'SELECT SUM("Amount [column C]") FROM "orders"', [messy])],
        "total Units": [_model_query(planner, 'SELECT SUM("Units") FROM "orders"', [messy])]}
    asked = planner.serve([messy], "total Amount")
    assert asked["clarify"] is True and not (asked.get("result") or {}).get("rows"), asked
    assert "headed “Amount”" in asked["reason"] and asked["selection"]["eligible"] == 0, asked
    assert planner.serve([messy], "total Amount in column C")["result"]["rows"] == [[60]]
    assert planner.serve([messy], "total Units")["result"]["rows"] == [[5]]


def test_the_readers_read_one_table_of_each_layout():
    """A workbook of three subscription exports (NT, SI, FF: one layout) and their three reports: read against all six
    tabs, every reading of "What is the total Amount broken down by Plan and Currency?" joined copies no key connects
    (tests.test_scale, 2026-10-23). The readers read one copy of each layout, the one the question names or else the
    first, and the answer names the others (``layout_copies``)."""
    def export(name):
        return {"name": name, "columns": ["id", "Plan", "Currency", "Amount"],
                "rows": [[1, "basic", "usd", 10], [2, "pro", "eur", 30]]}

    def report(name):
        return {"name": name, "columns": ["Plan", "Subscriptions", "Total Amount"],
                "rows": [["basic", 1, 10], ["pro", 1, 30]]}
    workbook = [export("NT"), report("NT Report"), export("SI"), report("SI Report"), export("FF"), report("FF Report")]
    planner = _hermetic_planner()
    read = []

    def pool(question, graph):
        read.append(set(graph.tables))
        table = "SI" if "SI" in question else "NT"
        return [_model_query(planner, f'SELECT "Plan", SUM("Amount") FROM "{table}" GROUP BY "Plan"', workbook)]
    planner.readers.pool = pool
    served = planner.serve(workbook, "What is the total Amount broken down by Plan?")
    assert read[-1] == {"NT", "NT_Report"}, read[-1]
    assert served["layout_copies"] == {"read": ["NT"], "others": ["SI", "FF"]}, served.get("layout_copies")
    planner.serve(workbook, "total Amount by Plan in SI")
    assert "SI" in read[-1] and not read[-1] & {"NT", "FF"}, read[-1]


def test_a_ranking_whose_order_ranks_its_rows_is_preferred():
    """ "top 3 cities" (one customer per city) ranked first the cities by their number of rows: every city has one,
    so its first three were three of four by name, Berlin among them (tests.test_geo, 2026-10-23). A ranking whose
    order ranks none of the rows it keeps above the first it leaves out is served only when every eligible one is
    (sql_rank.select_ranked_candidate, ``undecided``): the ranking by amount ranks them. Contrast: a ranking that ties
    only at its cut ranks its first rows and stays first ("the five oldest people", calibration); when no ranking
    ranks anything, the first is served."""
    customers = {"name": "customers", "columns": ["name", "city", "amount"],
                 "rows": [["Ada", "Paris", 100], ["Bob", "Lyon", 80], ["Eve", "Berlin", 40], ["Sam", "Tokyo", 50]]}
    by_rows = 'SELECT "city" FROM "customers" GROUP BY "city" ORDER BY COUNT(*) DESC LIMIT 3'
    by_amount = 'SELECT "city" FROM "customers" GROUP BY "city" ORDER BY SUM("amount") DESC LIMIT 3'
    planner = _hermetic_planner()
    planner.pool = [_model_query(planner, by_rows, [customers], score=1.0),
                    _model_query(planner, by_amount, [customers])]
    served = planner.serve([customers], "top 3 cities")
    assert [row[0] for row in served["result"]["rows"]] == ["Paris", "Lyon", "Tokyo"], served["result"]
    assert served["selection"]["selected"] == 1 and served["selection"]["undecided"] is False, served["selection"]
    decided = _hermetic_planner()
    decided.pool = [_model_query(decided, by_amount, [customers], score=1.0),
                    _model_query(decided, by_rows, [customers])]
    assert decided.serve([customers], "top 3 cities")["selection"]["selected"] == 0
    cut_tie = {**customers, "rows": customers["rows"] + [["Max", "Madrid", 50]]}   # Tokyo and Madrid tie at the cut
    tied_cut = _hermetic_planner()
    tied_cut.pool = [_model_query(tied_cut, by_amount, [cut_tie], score=1.0),
                     _model_query(tied_cut, by_amount.replace('SUM("amount")', 'MIN("name")'), [cut_tie])]
    assert tied_cut.serve([cut_tie], "top 3 cities")["selection"]["selected"] == 0
    tied = _hermetic_planner()
    tied.pool = [_model_query(tied, by_rows, [customers], score=1.0),
                 _model_query(tied, by_rows.replace("COUNT(*)", 'COUNT("name")'), [customers])]
    assert tied.serve([customers], "top 3 cities")["selection"]["selected"] == 0


def test_a_long_question_the_readers_decline_is_decomposed():
    """In an analysis, a question longer than decomposition.ONE_QUERY_WORDS words that the readers decline asks for a
    decomposition proposal (the complex demos' two-ranking prompts, which the readers read as one query they were not
    confident of, 2026-10-20). Contrast: the same decline of a short question is asked about, and outside an analysis
    a long one is too."""
    from engine.decomposition import ONE_QUERY_WORDS
    from engine.deterministic.context import analysis_execution_context

    sql = "SELECT COUNT(*) FROM people WHERE Country = 'France'"
    long_question = " ".join(["how many people come from France"] * (ONE_QUERY_WORDS // 6 + 1))
    unsure = _hermetic_planner(confident=False)
    unsure.pool = [_model_query(unsure, sql, read_as="the number of people whose Country is France")]
    with analysis_execution_context({"slug": "people", "revision": 1}, "c_" + "7" * 32):
        decomposed = unsure.serve([PEOPLE], long_question)
        short = unsure.serve([PEOPLE], "how many people come from France?")
    assert decomposed["decomposition_required"]["reason"].count("compound") == 1, decomposed
    assert "clarify" not in decomposed and decomposed["result"] is None, decomposed
    assert short["clarify"] is True and "decomposition_required" not in short, short
    assert unsure.serve([PEOPLE], long_question)["clarify"] is True


def test_a_question_past_the_calibrated_length_is_declined_however_confident():
    """The choice's threshold was matched on calibration questions of at most 29 words, so its confidence says
    nothing of a longer one: "Find the top 2 product categories by revenue and the top 2 customers by total spend,
    then list each pair where ..." (44 words) was read as one ranking at 0.19, above the 0.1434 threshold
    (2026-10-23). A confident choice of a question longer than decomposition.ONE_QUERY_WORDS words is declined: in
    an analysis it asks for the decomposition proposal, outside one it is asked about. Contrast: the same confident
    reading of the short question is served."""
    from engine.decomposition import ONE_QUERY_WORDS
    from engine.deterministic.context import analysis_execution_context

    sql = "SELECT COUNT(*) FROM people WHERE Country = 'France'"
    long_question = " ".join(["how many people come from France"] * (ONE_QUERY_WORDS // 6 + 1))
    sure = _hermetic_planner(confident=True)
    sure.pool = [_model_query(sure, sql, read_as="the number of people whose Country is France")]
    with analysis_execution_context({"slug": "people", "revision": 1}, "c_" + "7" * 32):
        decomposed = sure.serve([PEOPLE], long_question)
        short = sure.serve([PEOPLE], "how many people come from France?")
    assert decomposed["decomposition_required"]["reason"].count("compound") == 1, decomposed
    assert decomposed["result"] is None and decomposed["selection"]["declined"] == 0, decomposed
    assert decomposed["selection"]["confidence"] == 0.9, decomposed["selection"]
    assert short["result"] is not None and short["selection"]["selected"] == 0, short
    assert sure.serve([PEOPLE], long_question)["clarify"] is True


def test_a_reading_the_readers_are_not_confident_of_is_asked_about():
    """When the readers' learned refusal (ReadingSearch.confident) is not confident of the reading selection
    chose, nothing is served, the selection records its confidence and the reading it declined, and the reply says
    how that reading read the question so the person can ask again in the sheet's own terms (engine/tables.py
    unsure_clarification). Contrast: a confident reading is served with the same read-as line and its confidence;
    a reading with none is asked about without one."""
    question = "how many people come from France?"
    line = "the number of people whose Country is France"
    unsure = _hermetic_planner(confident=False)
    unsure.pool = [_model_query(unsure, "SELECT COUNT(*) FROM people WHERE Country = 'France'", read_as=line)]
    served = unsure.serve([PEOPLE], question)
    assert served["clarify"] is True and served["valid"] is False, served
    assert served["error"] is None and served["result"] is None and served["sql"] is None
    assert served["read_as"] == line and served["dropped"] == []
    assert served["selection"]["selected"] is None and served["selection"]["declined"] == 0, served["selection"]
    assert served["selection"]["confidence"] == 0.1, served["selection"]
    assert served["reason"] == (f"I read this as “{line}”, but I'm not confident that is what you asked. "
                                "Ask again with the column names as your sheet spells them."), served["reason"]
    sure = _hermetic_planner()
    sure.pool = [_model_query(sure, "SELECT COUNT(*) FROM people WHERE Country = 'France'", read_as=line)]
    answered = sure.serve([PEOPLE], question)
    assert answered["valid"] and answered["result"]["rows"] == [[2]] and answered["read_as"] == line
    assert "clarify" not in answered and answered["model"] == READERS_MODEL
    assert answered["selection"]["confidence"] == 0.9 and answered["selection"]["declined"] is None
    unsure.pool = [ScoredQuery(unsure.pool[0].query, unsure.pool[0].sql, 0.0, ())]
    unread = unsure.serve([PEOPLE], question)
    assert unread["clarify"] is True and unread["read_as"] is None
    assert unread["reason"] == ("I'm not confident I read this question right. "
                                "Ask again with the column names as your sheet spells them."), unread["reason"]


def test_serving_preserves_repeated_source_rows_in_aggregates():
    """Repeated identical transactions are separate source observations; ingestion must not turn
    a $300 source total into $100 by dropping two identical-looking payment rows."""
    from engine.tables import normalize_tables

    payments = {"name": "payments", "columns": ["customer", "amount"],
                "rows": [["Ada", 100], ["Ada", 100], ["ada", 100]]}
    normalized = normalize_tables([payments])
    assert len(normalized[0]["rows"]) == 3
    planner = _hermetic_planner()
    planner.pool = [_model_query(planner, 'SELECT SUM("amount") FROM "payments"', [payments])]
    served = planner.serve([payments], "total amount")
    assert served["valid"], served
    assert served["result"]["rows"] == [[300]], served


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
    for module in ("engine.sql_expansion", "engine.sql_grounding", "engine.compose", "engine.encoder_overlay",
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
    planner = _hermetic_planner()
    line = "the Name of every person"
    planner.pool = [_model_query(planner, 'SELECT "Name" FROM "people"', read_as=line)]
    response = planner.serve([PEOPLE], "list person names")
    assert response["valid"] is True
    assert response["error"] is None
    assert response["result"]["rows"] == [["Alice"], ["Bob"], ["Cara"]]
    assert response["candidate_count"] == 1
    assert response["ast"].startswith("SelectQuery(")
    assert response["model"] == READERS_MODEL == "engine - learned readers + typed SQL AST builder"
    assert response["read_as"] == line and "fallback" not in response
    selection = response["selection"]
    assert selection["executable"] == selection["pool_size"]
    assert selection["eligible"] == selection["pool_size"] and selection["rank"] == 0
    assert "served_by" not in selection and "fallback" not in selection
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


TESTS = [
    test_shared_ranking_rule_preserves_date_then_double_count_then_calculation_precedence,
    test_pool_oracle_counts_only_eligible_denotation_hits_and_validates_checkpoints,
    test_select_query_serves_the_reading_that_keeps_the_named_date,
    test_select_query_never_chooses_a_query_that_does_not_run,
    test_a_join_that_multiplies_two_tables_rows_stops_at_their_cells_budget,
    test_select_query_copies_the_tables_once_and_runs_each_query_once,
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
    test_named_request_never_serves_or_decomposes_a_lower_read_set_operation,
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
    test_a_superlative_over_the_shown_field_is_one_value,
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
    test_one_value_bounded_both_ways_is_contradictory,
    test_a_value_bounded_away_from_its_equality_is_contradictory,
    test_a_count_counts_its_nouns_head,
    test_a_closed_opening_request_for_one_field_is_the_answer,
    test_and_past_a_condition_joins_conditions,
    test_declared_self_relationship_keeps_unrelated_queries_and_source_rows,
    test_spider_ingestion_keeps_discovered_and_declared_relationships,
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
    test_arithmetic_expression_sum_renders_and_executes,
    test_arithmetic_division_preserves_real_semantics,
    test_currency_conversion_query_executes_end_to_end,
    test_planner_discovers_and_executes_same_name_currency_edge,
    test_currency_conversion_survives_uploaded_column_case,
    test_world_aggregate_currency_binding_is_typed_and_unambiguous,
    test_arithmetic_expression_validation,
    test_date_ranges_lists_and_quarters_filter_one_span,
    test_the_coverage_gate_reads_the_months_a_query_compares,
    test_two_values_of_one_column_are_either,
    test_a_value_compared_as_text_and_as_a_number_is_one_value,
    test_a_plural_reads_as_its_singular_everywhere,
    test_tabs_of_one_layout_are_read_as_one,
    test_a_sum_over_rows_its_joins_repeat_is_served_only_when_every_reading_is_one,
    test_one_of_two_columns_that_share_a_header_is_asked_about,
    test_the_readers_read_one_table_of_each_layout,
    test_a_ranking_whose_order_ranks_its_rows_is_preferred,
    test_a_long_question_the_readers_decline_is_decomposed,
    test_a_question_past_the_calibrated_length_is_declined_however_confident,
    test_a_reading_the_readers_are_not_confident_of_is_asked_about,
    test_serving_preserves_repeated_source_rows_in_aggregates,
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
    test_shared_spider_evaluation_contract,
    test_live_table_query_ast_mode_executes_typed_candidate,
    test_weight_manifest_detects_tampered_bundle,
    test_world_own_data_route_preserves_ast_observability,
    test_world_target_uses_the_synced_iso_currency_column,
    test_schema_graph_resolves_normalized_foreign_key_names,
    test_spider_evaluator_does_not_count_all_errors_as_answered,
    test_the_table_a_list_names_after_its_items_by_its_last_word_owns_them,
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
