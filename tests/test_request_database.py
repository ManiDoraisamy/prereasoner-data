"""test_request_database.py — every question runs on its own SQLite database (engine/prepared.py).

Registered in tests/run_all.py. Hermetic: no PostgreSQL. A small base image stands in for the knowledgebase's,
and a stub for the instance's knowledge where a test reads exchange rates. Pins what the database must hold
exactly (decimal text, integers, NULLs, the knowledgebase rows a table's preparation reaches), what it must not
do again (reload an unchanged table), and how a question picks among a cell's prepared candidates.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

from engine import prepared
from engine.prepared import BaseImage, RequestDatabase, TablePreparation, table_key


def _fixture(n_rows):
    """One sheet: a text key, an integer, and a fractional decimal that must stay exact."""
    sch = [
        {"table": "sales", "name": "city", "affinity": "TEXT"},
        {"table": "sales", "name": "units", "affinity": "INTEGER"},
        {"table": "sales", "name": "revenue", "affinity": "REAL"},
    ]
    rows = [[f"city{i}", i, Decimal(f"{i}.25")] for i in range(n_rows)]
    if rows:
        rows[0][0] = None                               # a NULL must survive the load unchanged
    tablemap = {"sales": {"name": "sales", "columns": ["city", "units", "revenue"], "rows": rows}}
    return sch, tablemap


def _base(tables):
    """A BaseImage holding ``tables`` {name: (((column, PostgreSQL type), ...), rows)} and their labels table."""
    database = sqlite3.connect(":memory:")
    prepared.register_functions(database)
    catalog = {}
    for name, (columns, rows) in tables.items():
        catalog[name] = tuple(columns)
        prepared._create(database, "main", name, columns)
        prepared._insert(database, "main", name, [column for column, _type in columns], rows)
    database.execute('CREATE TABLE "_labels" ("qid" TEXT PRIMARY KEY, "label" TEXT)')
    database.execute("INSERT INTO \"_labels\" VALUES ('Q142', 'France'), ('Q30', 'United States')")
    database.commit()
    image = database.serialize()
    database.close()
    return BaseImage("test", image, catalog, {})


@contextmanager
def _serving(database):
    token = prepared._DATABASE.set(database)
    try:
        yield database
    finally:
        prepared._DATABASE.reset(token)
        database.close()


_CITY = (("qid", "text"), ("name", "text"), ("country", "text"), ("population", "bigint"))


def test_every_value_is_held_exactly_as_the_planner_types_it():
    """A REAL column is canonical decimal text (SQLite rounds a fraction it stores as a float), an INTEGER
    column integers, a NULL stays NULL; the decimal collation orders the text by value."""
    sch, tablemap = _fixture(12)
    database = RequestDatabase()
    try:
        connection = database.load(tablemap, sch)
        rows = connection.execute('SELECT city, units, revenue, typeof(revenue) FROM sales ORDER BY units').fetchall()
        assert rows[0] == (None, 0, "0.25", "text"), rows[0]
        assert rows[3] == ("city3", 3, "3.25", "text"), rows[3]
        ordered = [row[0] for row in connection.execute("SELECT revenue FROM sales ORDER BY revenue DESC LIMIT 3")]
        assert ordered == ["11.25", "10.25", "9.25"], ordered      # by value, not as text ('9.25' > '11.25')
        assert connection.execute("SELECT decimal_sum(revenue) FROM sales").fetchone() == ("69",)
    finally:
        database.close()


def test_large_identifier_and_scientific_decimal_use_exact_storage():
    from engine.numeric import observed_numeric_affinity
    rows = [[str(10**30), "1e-3"], [str(10**30 + 1), "2e-3"]]
    sch = [{"table": "ledger", "name": name, "affinity": observed_numeric_affinity([row[i] for row in rows])}
           for i, name in enumerate(("id", "amount"))]
    database = RequestDatabase()
    try:
        held = database.load({"ledger": {"columns": ["id", "amount"], "rows": rows}}, sch).execute(
            "SELECT id, amount FROM ledger ORDER BY id").fetchall()
        assert held == [(str(10**30), "0.001"), (str(10**30 + 1), "0.002")], held
    finally:
        database.close()


def test_an_unchanged_table_is_loaded_once_and_a_changed_one_replaced():
    """Every executor of a question loads its tables (the pool, the plan, the world checks); only the first load
    copies them, and a table whose rows changed is replaced, never appended to."""
    sch, tablemap = _fixture(3)
    database = RequestDatabase()
    created = []
    database.connection.set_trace_callback(
        lambda statement: created.append(statement) if statement.startswith("CREATE TABLE main.") else None)
    try:
        database.load(tablemap, sch)
        database.load(tablemap, sch)
        database.load({"sales": dict(tablemap["sales"], rows=list(tablemap["sales"]["rows"]))}, sch)
        assert len(created) == 1, created
        changed = {"sales": dict(tablemap["sales"], rows=tablemap["sales"]["rows"][:2])}
        assert database.load(changed, sch).execute("SELECT COUNT(*) FROM sales").fetchone() == (2,)
        assert len(created) == 2, created
        empty = {"sales": dict(tablemap["sales"], rows=[])}
        assert database.load(empty, sch).execute("SELECT COUNT(*) FROM sales").fetchone() == (0,)
    finally:
        database.close()


def test_text_folds_case_in_every_alphabet():
    """SQLite's own lower() folds only ASCII: 'ÉCOLE' never matched 'école' (PostgreSQL's did)."""
    database = RequestDatabase()
    try:
        assert database.connection.execute("SELECT lower('ÉCOLE'), upper('straße')").fetchone() == (
            "école", "STRASSE")
        assert database.connection.execute("SELECT lower(NULL)").fetchone() == (None,)
    finally:
        database.close()


def test_a_statement_ends_at_the_requests_deadline():
    """The PostgreSQL statement timeout ended a runaway statement at the request's deadline; the database's
    progress handler does now."""
    from engine import request_deadline
    database = RequestDatabase()
    token = request_deadline.begin(0.05)
    try:
        time.sleep(0.1)
        try:
            database.connection.execute(
                "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n").fetchone()
        except sqlite3.OperationalError as exc:
            assert "interrupt" in str(exc), exc
        else:
            raise AssertionError("a statement outlived the request's deadline")
    finally:
        request_deadline.end(token)
        database.close()


def test_place_ambiguity_is_one_lookup_for_every_value():
    """Ambiguities ran one query per distinct uploaded place (seven ~150 ms scans of the 200k-row city table on
    every world request, 2026-09-30). One lookup flags the same values in the upload's own order and spelling."""
    from engine.knowledge_tables import KnowledgeTableQuery

    rows = [("Q90", "Paris", "Q142", 1), ("Q830149", "Paris", "Q30", 2), ("Q84", "London", "Q145", 3),
            ("Q1001", "London", "Q30", 4), ("Q456", "Lyon", "Q142", 5)]
    query = object.__new__(KnowledgeTableQuery)
    query.words = {"city": {"key": "qid", "columns": ["qid", "name", "country"]}}
    table = {"columns": ["customer", "city"],
             "rows": [["a", "Paris"], ["b", "Lyon"], ["c", "PARIS"], ["d", "London"], ["e", None]]}
    with _serving(RequestDatabase(_base({"city": (_CITY, rows)}))) as database:
        lookups = []
        database.connection.set_trace_callback(
            lambda statement: lookups.append(statement) if "knowledgebase" in statement else None)
        warnings = query.ambiguities(table, "city", "city")
    assert len(lookups) == 1, lookups
    assert warnings == ["'Paris' is ambiguous in city: Q142, Q30", "'London' is ambiguous in city: Q145, Q30"], warnings


def test_a_tables_identity_ignores_the_columns_a_dataset_op_synthesizes():
    """A currency claim adds a constant code column the routes never read: the table it was added to keeps its
    preparation."""
    from engine.dataset_semantics import synthetic_currency_column
    plain = {"name": "orders", "columns": ["city", "amount"], "rows": [["Paris", 5], ["Lyon", 7]]}
    claimed = {"name": "orders", "columns": ["city", "amount", synthetic_currency_column("amount")],
               "rows": [["Paris", 5, "EUR"], ["Lyon", 7, "EUR"]]}
    assert table_key(plain) == table_key(claimed)
    assert table_key(plain) != table_key(dict(plain, rows=[["Paris", 5], ["Lyon", 8]]))


def test_a_bridge_replaces_one_columns_rows():
    database = RequestDatabase()
    try:
        name = "orders connected to knowledgebase"
        database.replace_bridge(name, "city", [("city", "Paris", "city", "Q90", "France", "Q515", "Q90", "")])
        database.replace_bridge(name, "country", [("country", "FR", "country", "France", "France", "Q6256",
                                                   "Q142", "")])
        database.replace_bridge(name, "city", [("city", "Lyon", "city", "Q456", "France", "Q515", "Q456", "")])
        held = database.connection.execute(f'SELECT "column", "value" FROM main."{name}" ORDER BY 1').fetchall()
        assert held == [("city", "Lyon"), ("country", "FR")], held
    finally:
        database.close()


def _preparation(key, cities, reached_rows, labels=None):
    return TablePreparation(key, "v", {
        "columns": {"city": {"type": "city", "cells": cities}},
        "reached": {"city": {"columns": [column for column, _type in _CITY], "rows": reached_rows}},
        "labels": labels or {},
        "cities": [],
    })


def test_a_preparations_entities_are_held_once_with_every_row():
    """Two tables naming one city hold its rows once; an entity with two rows keeps both, as the knowledgebase
    join read them."""
    first = _preparation("a" * 64, {}, [["Q90", "Paris", "Q142", 1], ["Q90", "Paris", "Q142", 2]], {"Q90": "Paris"})
    second = _preparation("b" * 64, {}, [["Q90", "Paris", "Q142", 1], ["Q456", "Lyon", "Q142", 5]])
    database = RequestDatabase(_base({"city": (_CITY, [])}))
    try:
        database.add_preparation(first)
        database.add_preparation(second)
        database.add_preparation(first)
        held = database.connection.execute(
            'SELECT qid, population FROM knowledgebase."city" ORDER BY qid, population').fetchall()
        assert held == [("Q456", 5), ("Q90", 1), ("Q90", 2)], held
        assert database.labels(["Q90", "Q142", "Q999"]) == {"Q90": "Paris", "Q142": "France"}
    finally:
        database.close()


def test_a_questions_country_never_chooses_a_cells_city():
    """A cell's city is the name's primary city, then the most populous; a typo's is its nearest. The country a
    question names filters rows and never chooses among a cell's namesakes: choosing Paris, Texas under 'in the
    United States' let every namesake pass the filter ("total amount in the United States in euros" counted the
    London and Paris orders, tests.test_question_families, 2026-10-27)."""
    from engine.knowledge_query import KnowledgeQuery

    table = {"name": "orders", "columns": ["city"], "rows": [["Paris"], ["Pariss"]]}
    cities = {"Paris": [["Q90", "France", True, 2100000, 1.0], ["Q830149", "United States", False, 25000, 1.0]],
              "Pariss": [["Q90", "France", True, 2100000, 0.93], ["Q830149", "United States", False, 25000, 0.91]]}
    reached = [["Q90", "Paris", "Q142", 2100000], ["Q830149", "Paris", "Q30", 25000]]
    query = object.__new__(KnowledgeQuery)
    with _serving(RequestDatabase(_base({"city": (_CITY, [])}))) as database:
        database.add_preparation(_preparation("c" * 64, cities, reached), table)
        assert query._city_pairs([table], "orders", "city") == [("Paris", "Q90"), ("Pariss", "Q90")]


def test_the_world_paths_check_queries_run_on_the_request_database():
    """The world path's freshness, coverage and unmatched-row checks run its own FROM clause, which joins the
    bridge. The bridge's subquery named its columns PostgreSQL's way (`AS b(cell, qid)`), which SQLite rejects, so
    every world answer ended in an error once it ran on the request's database (found 2026-10-27, before release)."""
    from engine.knowledge_query import KnowledgeQuery
    from engine.knowledge_tables import load_word_tables

    query = object.__new__(KnowledgeQuery)
    query.words = load_word_tables()
    table = {"name": "orders", "columns": ["city", "amount"], "rows": [["Paris", 5], ["Lyon", 7], ["Tokyo", 9]]}
    cities = {"Paris": [["Q90", "France", True, 2100000, 1.0]], "Lyon": [["Q456", "France", True, 500000, 1.0]],
              "Tokyo": [["Q1490", "Japan", True, 14000000, 1.0]]}
    reached = [["Q90", "Paris", "Q142", 2100000], ["Q456", "Lyon", "Q142", 500000], ["Q1490", "Tokyo", "Q17", 1]]
    sch = [{"table": "orders", "name": "city", "affinity": "TEXT", "values": ["Paris", "Lyon", "Tokyo"]},
           {"table": "orders", "name": "amount", "affinity": "INTEGER", "values": [5, 7, 9]}]
    joins = [{"left_table": "orders", "left_col": "city", "right_table": "city", "right_col": "qid"}]
    with _serving(RequestDatabase(_base({"city": (_CITY, [])}))) as database:
        database.add_preparation(_preparation("e" * 64, cities, reached), table)
        database.load({"orders": table}, sch)
        fw, _disambiguated, warnings = query._world_joins(
            'FROM "orders"', joins, sch, [table], "orders", "city", "2026-10-27",
            {"filter_table": "city", "attr": "country", "value": "Q142"})
        total = database.connection.execute(
            f"SELECT SUM(\"orders\".\"amount\") {fw} WHERE \"city\".\"country\" = 'Q142'").fetchone()
    assert total == (12,), (total, fw)
    assert warnings == [], warnings


def test_a_conversion_holds_one_rate_row_per_currency_and_day():
    """A second conversion in the same question (another target currency) fills its column on the rows the first
    held: a second row for a currency and day would double every converted total."""
    columns = (("currency_code", "text"), ("date", "date"), ("rate_to_usd", "numeric"), ("rate_to_gbp", "numeric"),
               ("updated_at", "text"), ("source", "text"), ("source_release_id", "text"))
    table = {("2026-10-01",): [("EUR", "2026-10-01", Decimal("1.1"), Decimal("0.86"), "u", "ecb", "r1")],
             ("2026-10-02",): [("EUR", "2026-10-02", Decimal("1.2"), Decimal("0.87"), "u", "ecb", "r1")]}

    def rates(_knowledge, wanted, days):
        names = ("currency_code", "date") + tuple(sorted(wanted))
        index = {name: i for i, (name, _type) in enumerate(columns)}
        rows = [tuple(row[index[name]] for name in names) for day in days for row in table[(day,)]]
        return names, rows

    database = RequestDatabase(_base({"exchange_rate": (columns, [])}))
    try:
        with patch.object(prepared.Knowledge, "exchange_rates", rates):
            database.load_rates(["rate_to_usd"], ["2026-10-01"])
            database.load_rates(["rate_to_gbp"], ["2026-10-01", "2026-10-02"])
        held = database.connection.execute(
            'SELECT currency_code, date, rate_to_usd, rate_to_gbp FROM knowledgebase."exchange_rate" '
            'ORDER BY date').fetchall()
        assert held == [("EUR", "2026-10-01", "1.1", "0.86"), ("EUR", "2026-10-02", "1.2", "0.87")], held
    finally:
        database.close()


def test_a_preparation_round_trips_through_its_stored_form():
    record = {"routes": [["orders", "city", "city"]], "typing": [{"table": "orders", "column": "city"}],
              "columns": {"city": {"type": "city", "cells": {"Paris": [["Q90", "France", True, 1, 1.0]]}}},
              "nongeo": {}, "reached": {}, "labels": {"Q90": "Paris"},
              "cities": [["paris", "Q90", prepared.encode_city_vector([0.5, 0.25, 0.0])]]}
    stored = TablePreparation("d" * 64, "v", record)
    loaded = TablePreparation.decode("d" * 64, "v", stored.encode())
    assert loaded.routes == {("orders", "city"): "city"} and loaded.labels == {"Q90": "Paris"}
    assert loaded.columns == record["columns"] and loaded.typing == record["typing"]
    assert [list(vector) for _norm, _qid, vector in loaded.cities] == [[0.5, 0.25, 0.0]]


def test_a_decimal_result_compares_with_an_untyped_number():
    """A SUM, an AVG and a calculation read as decimal text on the request database, and SQLite orders any text
    above any number: "cities with total sales over 100" kept every city when its HAVING compared decimal_sum(...)
    with the readers' untyped Literal(100) (2026-10-27). Each comparison of a decimal value with a number is exact,
    whichever side says what."""
    from engine.sql_ast import (Aggregate, ColumnRef, Comparison, InPredicate, Literal, ScalarSubquery, SelectItem,
                                SelectQuery, SQLType, render_query)
    from engine.tables import TableQuery

    sch = [{"table": "s", "name": "city", "affinity": "TEXT"},
           {"table": "s", "name": "sales", "affinity": "INTEGER"},
           {"table": "s", "name": "revenue", "affinity": "REAL"}]
    tablemap = {"s": {"name": "s", "columns": ["city", "sales", "revenue"],
                      "rows": [["Tokyo", 100, Decimal("99.5")], ["Osaka", 200, Decimal("100.25")],
                               ["Nagoya", 50, Decimal("7")], ["Lyon", 30, Decimal("0.5")]]}}
    city = ColumnRef("s", "city", SQLType.TEXT)
    sales = ColumnRef("s", "sales", SQLType.INTEGER)
    revenue = ColumnRef("s", "revenue", SQLType.REAL)

    def cities(**clauses):
        query = SelectQuery((SelectItem(city),), "s", **clauses)
        _columns, rows = TableQuery().execute(tablemap, sch, render_query(query), query=query)
        return sorted(row[0] for row in rows)

    assert cities(group_by=(city,), having=Comparison(Aggregate("SUM", sales), ">", Literal(100))) == ["Osaka"]
    assert cities(group_by=(city,), having=Comparison(Aggregate("SUM", revenue), ">", Literal(100))) == ["Osaka"]
    assert cities(group_by=(city,), having=Comparison(Aggregate("MAX", sales), "<", Literal(Decimal("50.5")))) == [
        "Lyon", "Nagoya"]
    average = ScalarSubquery(SelectQuery((SelectItem(Aggregate("AVG", sales)),), "s"))
    assert cities(where=Comparison(sales, ">", average)) == ["Osaka", "Tokyo"]
    assert cities(group_by=(city,), having=InPredicate(Aggregate("SUM", revenue), (Literal(7), Literal(100.25)))) == [
        "Nagoya", "Osaka"]
    assert cities(group_by=(city,), having=InPredicate(Aggregate("SUM", revenue), (Literal(7), Literal(100.25)),
                                                       negated=True)) == ["Lyon", "Tokyo"]
    # An integer compares with a number as itself: no decimal function where nothing reads as text.
    assert "decimal_cmp" not in render_query(SelectQuery((SelectItem(city),), "s",
                                                         where=Comparison(sales, ">", Literal(100))),
                                             dialect="sqlite_decimal")


TESTS = [
    test_a_decimal_result_compares_with_an_untyped_number,
    test_every_value_is_held_exactly_as_the_planner_types_it,
    test_large_identifier_and_scientific_decimal_use_exact_storage,
    test_an_unchanged_table_is_loaded_once_and_a_changed_one_replaced,
    test_text_folds_case_in_every_alphabet,
    test_a_statement_ends_at_the_requests_deadline,
    test_place_ambiguity_is_one_lookup_for_every_value,
    test_a_tables_identity_ignores_the_columns_a_dataset_op_synthesizes,
    test_a_bridge_replaces_one_columns_rows,
    test_a_preparations_entities_are_held_once_with_every_row,
    test_a_questions_country_never_chooses_a_cells_city,
    test_the_world_paths_check_queries_run_on_the_request_database,
    test_a_conversion_holds_one_rate_row_per_currency_and_day,
    test_a_preparation_round_trips_through_its_stored_form,
]


def main():
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as error:                         # noqa: BLE001 - report every test
            failures += 1
            print(f"  FAIL {test.__name__}: {type(error).__name__}: {error}")
    print(f"{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
