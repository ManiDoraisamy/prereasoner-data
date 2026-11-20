"""Hermetic tests for the readers' deterministic half (engine/readers): the readings form and its builder, the
decoder's generic semantics, and the serialization. No model weights load: outputs are written as a model would
write them.

Run: python -m tests.test_readers
"""
from __future__ import annotations

import sqlite3
import sys

from engine.readers.decode import Choices, candidates, decode, dated, unlisted
from engine.readers.layout import AGGREGATES, COUNTS, GLOBAL, GLOBAL_WIDTH, LAYOUT, OPERATORS, PER_BLOCK, SLOTS
from engine.readers.readings import NotReadable, build, readings_of
from engine.readers.text import Schema, fit, reading_text
from engine.sql_ast import render_query
from engine.sql_schema import SchemaGraph

ORDERS = {"name": "orders", "columns": ["order_id", "city", "amount", "placed"],
          "rows": [[1, "Paris", 100, "2026-08-03"], [2, "Lyon", 30, "2026-08-04"], [3, "Paris", 29, "2026-08-10"],
                   [4, "Nice", 48, "2026-07-02"]]}
PEOPLE = {"name": "players", "columns": ["player_id", "player", "college", "position"],
          "rows": [[1, "Ann", "UCLA", "Guard"], [2, "Bo", "Duke", "Center"], [3, "Cy", "UCLA", "Forward"]]}


def graph_of(*tables, fks=()):
    return SchemaGraph.from_tables(list(tables), list(fks))


def rows_of(sql, *tables):
    connection = sqlite3.connect(":memory:")
    for t in tables:
        connection.execute(f'CREATE TABLE "{t["name"]}" ({", ".join(chr(34) + c + chr(34) for c in t["columns"])})')
        connection.executemany(f'INSERT INTO "{t["name"]}" VALUES ({", ".join("?" for _ in t["columns"])})', t["rows"])
    return connection.execute(sql).fetchall()


def block(**fields):
    base = {"tables": ["orders"], "distinct": False, "select": [], "where": [], "conj": "AND", "group": [],
            "having": [], "order": [], "dir": None, "limit": None}
    base.update(fields)
    return base


def output_for(names, tables, picks, globals_=None, values=None):
    """A model-shaped output that says ``picks``: {(column, field, index): probability} for block 0."""
    columns = {}
    for name in names:
        p = [[0.0] * PER_BLOCK for _ in range(2)]
        for (column, key, index), value in picks.items():
            if column == name:
                p[0][LAYOUT[key][0] + index] = value
        columns[name] = p
    g = [-5.0] * GLOBAL_WIDTH
    g[0] = 5.0                                                     # structure: single
    g[GLOBAL["limit0"][0]] = 5.0                                   # no limit
    for key, value in (globals_ or {}).items():
        g[GLOBAL[key][0] + (value if isinstance(value, int) else 0)] = 5.0 if not isinstance(value, float) else value
    slots = {name: [[[1.0] + [0.0] * (SLOTS - 1) for _ in AGGREGATES] for _ in range(2)] for name in names}
    return {"columns": columns, "slots": slots, "tables": {t: [1.0, 0.0] for t in tables}, "globals": g,
            "values": values or {}, "limit": [], "unweighted": True}


def counts(g, **kinds):
    for kind, top in COUNTS.items():
        g[f"count_{kind}0"] = min(kinds.get(kind, 0), top)
    return g


def test_readings_round_trip_through_the_builder():
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "SUM", "orders.amount", False]],
                                                        where=[["orders.city", "=", "Paris"]])]}
    query = build(readings, graph)
    assert rows_of(render_query(query), ORDERS) == [(129,)]
    assert readings_of(query)["blocks"][0]["where"] == [["orders.city", "=", "Paris"]]


def test_a_listed_value_cut_short_reads_as_the_stored_value():
    """The schema text cuts a value to 20 characters, and a reader pointing at the listing read "Greater than 6 month":
    compared as cut it matched no row, and a question about a real column answered that nothing matched (2026-10-30)."""
    from engine.readers.decode import literal
    from engine.readers.text import listed

    values = ["Less than 3 months", "Greater than 6 months", "Greater than 6 months (annual)", "Exactly twenty chars"]
    assert len(values[3]) == 20 and listed(values[1]) == "Greater than 6 month"
    graph = graph_of({"name": "subs", "columns": ["id", "duration"], "rows": [[i, v] for i, v in enumerate(values)]})
    # Positive: the cut form is the value, whatever the case; the first listed that way is the one the reader saw.
    assert literal("Greater than 6 month", "subs.duration", graph, "=") == "Greater than 6 months"
    assert literal("greater than 6 month", "subs.duration", graph, "=") == "Greater than 6 months"
    # Same profile: a value that fits the listing is itself, and a shorter part of a long value names no stored value.
    assert literal("Exactly twenty chars", "subs.duration", graph, "=") == "Exactly twenty chars"
    assert literal("Greater than 6", "subs.duration", graph, "=") == "Greater than 6"
    # Negative: words no value is listed as stay what the reader read, and LIKE keeps looking inside the values.
    assert literal("above 6 months", "subs.duration", graph, "=") == "above 6 months"
    assert literal("Greater than 6 month", "subs.duration", graph, "LIKE") == "%Greater than 6 month%"


def test_a_question_past_half_the_budget_is_read_as_far_as_it_fits():
    """A question of 2,700 characters raised a truncation error out of the readers, and the reply said to try again."""
    import re

    from engine.readers.text import readable

    class Words:
        def __call__(self, text, truncation=False, max_length=None, return_offsets_mapping=False):
            spans = [match.span() for match in re.finditer(r"\S+", text)]
            spans = spans[:max_length] if truncation else spans
            return {"input_ids": list(range(len(spans))), "offset_mapping": spans}

    words = Words()
    assert readable(words, "total amount", 8) == "total amount"
    assert readable(words, "one two three four", 8) == "one two three four"
    assert readable(words, "one two three four five six", 8) == "one two three four five"


def test_training_candidates_are_the_pool_serving_ranks():
    """The choice and the re-scorers were fitted on the decoded readings alone; serving ranks their variants too."""
    from unittest.mock import patch

    from engine.readers import search
    from training.readers import candidates as candidate_module

    class Anchors:
        @staticmethod
        def anchors(_question):
            return {}

    decoded = [(-0.1234, {"sql": "SELECT 1"}), (-0.2345, {"sql": "SELECT 2"})]

    def variants(reading, _named, _graph):
        yield reading
        yield {"sql": {"SELECT 1": "SELECT 2", "SELECT 2": "SELECT 3"}[reading["sql"]]}

    with (patch.object(search, "Schema", lambda _graph: Anchors()),
          patch.object(search, "reading_variants", variants),
          patch.object(candidate_module.decoding, "candidates", lambda *_args: decoded),
          patch.object(candidate_module, "build", lambda reading, _graph: reading),
          patch.object(candidate_module, "as_built", lambda reading, _query: reading),
          patch.object(candidate_module, "render_query", lambda query: query["sql"]),
          patch.object(candidate_module, "read_as", lambda reading: reading["sql"]),
          patch.object(candidate_module, "exec_sql_timed", lambda *_args, **_kwargs: ([("ok",)], None)),
          patch.object(candidate_module, "compare", lambda *_args: {"strict": True})):
        found = candidate_module.candidate_records("outputs", {"question": "q"}, object(), object(), [("ok",)])
    assert [record["sql"] for record in found] == ["SELECT 1", "SELECT 2", "SELECT 3"], found
    assert [record["rank"] for record in found] == [0, 0, 1], found


def test_reader_training_reads_no_dev_or_test_question():
    from training.readers.provenance import SPIDER_TRAIN_SETS, assert_no_evaluation_sources
    from training.readers.sources import SPIDER_SETS

    assert SPIDER_TRAIN_SETS == ("train_spider", "train_others")
    assert SPIDER_SETS == ("train_spider.json", "train_others.json")
    assert_no_evaluation_sources([{"source": "train_spider"}, {"source": "bird_train"}, {"source": "synsql"}], "fixture")
    for source in ("dev", "test", "syn_dev", "Spider_test", "unknown_source", ""):
        try:
            assert_no_evaluation_sources([{"source": source}], "fixture")
        except ValueError as exc:
            assert source in str(exc)
        else:
            raise AssertionError(f"source {source!r} was accepted")


def test_a_value_spelled_almost_as_read_is_the_stored_value():
    """"how many people cancelled" compared status with 'cancelled' where the sheet says 'canceled', and answered 0."""
    from engine.readers.decode import literal

    statuses = ["active", "canceled", "past_due"]
    graph = graph_of({"name": "subs", "columns": ["id", "status"], "rows": [[i, v] for i, v in enumerate(statuses)]})
    assert literal("cancelled", "subs.status", graph, "=") == "canceled"
    assert literal("CANCELLED", "subs.status", graph, "=") == "canceled"
    # Same profile: another word, and a name that only begins the same, stay what was read.
    assert literal("paused", "subs.status", graph, "=") == "paused"
    places = graph_of({"name": "t", "columns": ["id", "country"], "rows": [[1, "Nigeria"], [2, "Austria"]]})
    assert literal("Niger", "t.country", places, "=") == "Niger"
    assert literal("Australia", "t.country", places, "=") == "Australia"
    # Two values as near: the reading does not choose between them.
    twins = graph_of({"name": "t", "columns": ["id", "code"], "rows": [[1, "report-a"], [2, "report-b"]]})
    assert literal("report-c", "t.code", twins, "=") == "report-c"
    # A column of many values holds names, not categories.
    many = graph_of({"name": "t", "columns": ["id", "name"], "rows": [[i, f"name{i:03}"] for i in range(70)]})
    assert literal("name0001", "t.name", many, "=") == "name0001"


def test_near_copies_of_an_export_are_one_layout_and_the_first_is_read():
    """NT holds SI's columns and more, and FF misspells one: the readers chose SI with the user on NT (2026-10-30)."""
    from engine.tables import one_of_each_layout

    shared = [f"c{i}" for i in range(19)]
    def sheet(name, columns):
        return {"name": name, "columns": columns, "rows": [[f"{name}{i}" for i in range(len(columns))]] * 2}

    workbook = graph_of(sheet("NT", ["Domain", *shared, "x1", "x2", "x3", "x4", "x5"]), sheet("SI", ["Domain", *shared]),
                        sheet("FF", ["Domian", *shared]), sheet("Report", ["year", "status", "total"]))
    assert workbook.layout_copies == (("NT", "SI", "FF"),)
    assert set(one_of_each_layout(workbook, "total amount").tables) == {"NT", "Report"}
    assert set(one_of_each_layout(workbook, "total amount in SI").tables) == {"SI", "Report"}
    # Tables that only share a few columns are their own things.
    other = graph_of(sheet("orders", ["id", "customer", "amount", "date"]), sheet("people", ["id", "customer", "age", "city"]))
    assert other.layout_copies == ()
    # Each export's Year keys its own report tab, as the importer reads a pivot, and the reports are one layout too: still
    # one export, or the readers answer from whichever tab they like (the owner's sheet, 2026-10-31).
    exports = [sheet("NT", ["Domain", "Year", *shared, "x1", "x2", "x3", "x4", "x5"]), sheet("SI", ["Domain", "Year", *shared]),
               sheet("FF", ["Domian", "Year", *shared]), sheet("NT_Report", ["Year", "active", "canceled", "unpaid", "Total"]),
               sheet("SI_Report", ["Year", "active", "canceled", "Total"])]
    reported = SchemaGraph.from_tables(exports, [("NT", "Year", "NT_Report", "Year"), ("SI", "Year", "SI_Report", "Year"),
                                                 ("FF", "Year", "NT_Report", "Year")])
    assert reported.layout_copies == (("NT", "SI", "FF"),), reported.layout_copies
    assert "NT" in one_of_each_layout(reported, "total amount").tables
    assert "SI" not in one_of_each_layout(reported, "total amount").tables
    # Near in columns but keying different things: who took a course and who taught it are not one export (college_2).
    tables = [sheet("takes", ["ID", "course_id", "sec_id", "semester", "year", "grade"]),
              sheet("teaches", ["ID", "course_id", "sec_id", "semester", "year"]),
              {"name": "student", "columns": ["ID", "name", "dept"], "rows": [["takes0", "a", "b"]]},
              {"name": "instructor", "columns": ["ID", "name", "pay"], "rows": [["teaches0", "c", "d"]]}]
    keyed = SchemaGraph.from_tables(tables, [("takes", "ID", "student", "ID"), ("teaches", "ID", "instructor", "ID")])
    assert keyed.layout_copies == (), keyed.layout_copies
    assert SchemaGraph.from_tables(tables, []).layout_copies == (("takes", "teaches"),), "no key says otherwise"


def test_the_tab_on_screen_is_the_copy_a_question_reads():
    """A sheet is uploaded once, with the tab open then first: a question asked on Checklist read Forklift (2026-10-31). The
    tab open now goes first, a tab the question names still wins, and no tab or an unknown one changes nothing."""
    from engine.tables import on_screen_first, one_of_each_layout

    def tab(name):
        return {"name": name, "columns": ["Keyword", "Volume", "Bid"], "rows": [[f"{name}{i}", i, i] for i in range(3)]}

    tabs = [tab("forklift"), tab("Sheet1"), tab("checklist"), tab("inspection")]
    def read(active, question="top keywords"):
        ordered = on_screen_first(tabs, active)
        return list(one_of_each_layout(SchemaGraph.from_tables(ordered, []), question).tables)

    assert read("Checklist") == ["checklist"], read("Checklist")
    assert read("Sheet1") == ["Sheet1"]
    assert read(None) == ["forklift"] and read("") == ["forklift"] and read("Gone") == ["forklift"]
    assert read("Checklist", "top keywords in inspection") == ["inspection"]
    assert [t["name"] for t in on_screen_first(tabs, "inspection")] == ["inspection", "forklift", "Sheet1", "checklist"]
    # "NT Report" is the tab; its report's canonical name matches and the export's does not.
    named = [tab("nt"), tab("nt_report")]
    assert [t["name"] for t in on_screen_first(named, "NT Report")] == ["nt_report", "nt"]


def test_a_table_read_from_nowhere_does_not_need_a_join():
    """A reader marked the report tab beside the export and read nothing from it: every reading was refused for want of
    a join, and "total sales split by sector" had no candidate at all (2026-10-30)."""
    from engine.readers.readings import NotReadable, build

    graph = graph_of({"name": "export", "columns": ["id", "sector", "amount"], "rows": [[1, "Retail", 5], [2, "Food", 7]]},
                     {"name": "report", "columns": ["year", "active", "total"], "rows": [[2024, 1, 2], [2025, 3, 4]]})
    base = {"structure": "single", "blocks": [{
        "tables": ["export", "report"], "select": [[0, "SUM", "export.amount", False]], "where": [], "conj": "AND",
        "group": ["export.sector"], "having": [], "order": [], "dir": None, "limit": None, "distinct": False}]}
    query = build(base, graph)
    assert query.from_table == "export" and not query.joins
    from engine.readers.read_as import read_as
    from engine.readers.readings import as_built
    assert " of export," in read_as(as_built(base, query)) and "report" not in read_as(as_built(base, query))
    # Columns of both tables and no key between them: still no query, not a cross product.
    both = {"structure": "single", "blocks": [dict(base["blocks"][0], select=[[0, "SUM", "export.amount", False],
                                                                             [1, "plain", "report.year", False]])]}
    try:
        build(both, graph)
    except NotReadable as exc:
        assert "no-join-path" in str(exc)
    else:
        raise AssertionError("two tables read and no key between them was built")


def test_readings_hold_only_the_operators_the_readers_read():
    """A HAVING that matches text (SynSQL's "HAVING name LIKE ...") has no reading: the readers compare totals, and a
    gold reading outside their vocabulary stopped the v14 trainer (2026-10-25). A compared total reads."""
    from regress.sql_import import import_sql
    graph = graph_of(ORDERS)
    try:
        readings_of(import_sql("SELECT city FROM orders GROUP BY city HAVING city LIKE '%a%'", graph))
        raise AssertionError("a text-matching HAVING was read")
    except NotReadable as exc:
        assert str(exc) == "having-operator", exc
    readings = readings_of(import_sql("SELECT city FROM orders GROUP BY city HAVING COUNT(*) > 1", graph))
    assert readings["blocks"][0]["having"] == [["COUNT", "*", ">", 1]], readings


def test_a_grouped_block_groups_what_it_shows():
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "plain", "orders.city", False],
                                                                [1, "COUNT", "*", False]],
                                                        group=["orders.order_id"])]}
    sql = render_query(build(readings, graph))
    assert 'GROUP BY "orders"."order_id", "orders"."city"' in sql, sql


def test_a_group_is_ordered_by_its_total():
    """"The top 2 cities by total amount": a grouped block that sorts by a number column it neither groups nor shows
    orders each group by its total. Grouped by the column as well, Paris split into one row per amount and its
    100 order alone ranked it (the complex demos' "top 2 customers by total spend", 2026-10-20)."""
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "plain", "orders.city", False]],
                                                        group=["orders.city"], order=[["plain", "orders.amount"]],
                                                        dir="DESC", limit=2)]}
    sql = render_query(build(readings, graph))
    assert 'ORDER BY SUM("orders"."amount") DESC' in sql and 'GROUP BY "orders"."city"' in sql, sql
    assert rows_of(sql, ORDERS) == [("Paris",), ("Nice",)], rows_of(sql, ORDERS)


def test_groups_with_no_aggregate_are_distinct_rows():
    """Groups that compute nothing are the distinct rows they show: SELECT DISTINCT, which both deterministic emitters
    express ("customer and product for each purchase" failed to lower as a GROUP BY without an aggregate). A group
    key the block does not show keeps its GROUP BY: one row per key, not per shown value."""
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "plain", "orders.city", False]],
                                                        group=["orders.city"])]}
    query = build(readings, graph)
    assert query.distinct and not query.group_by, render_query(query)
    assert sorted(rows_of(render_query(query), ORDERS)) == [("Lyon",), ("Nice",), ("Paris",)]
    keyed = {"structure": "single", "blocks": [block(select=[[0, "plain", "orders.city", False]],
                                                     group=["orders.order_id"])]}
    assert build(keyed, graph).group_by, render_query(build(keyed, graph))


def test_a_share_says_its_part_and_its_whole():
    """The read-as line of a share names the share: "What share of the total amount comes from Paris?" read its share
    candidate as "total amount of orders where city is 'Paris'", the bare total's line, so neither the person nor the
    re-scorer could tell the two apart (the neartail-orders demo, 2026-10-20)."""
    from engine.readers.read_as import read_as
    share = {"structure": "single", "blocks": [block(select=[[0, "SUM", "orders.amount", False]],
                                                     where=[["orders.city", "=", "Paris"]], share=[0])]}
    plain = {"structure": "single", "blocks": [block(select=[[0, "SUM", "orders.amount", False]],
                                                     where=[["orders.city", "=", "Paris"]])]}
    assert read_as(share) == ("share of total amount of orders where city is 'Paris', out of total amount of all "
                              "orders"), read_as(share)
    assert read_as(plain) == "total amount of orders where city is 'Paris'"


def test_two_named_values_of_one_column_read_either():
    """A row holds one value of a column: when the question names two values of the column a reading compares with
    one of them, the reading's either-or variant joins the pool ("total amount from Paris and Lyon" read only Paris,
    or Paris intersected with Lyon). A reading that compares other columns, or every named value, has none."""
    from engine.readers.search import either
    paris = {"structure": "single", "blocks": [block(select=[[0, "SUM", "orders.amount", False]],
                                                     where=[["orders.city", "=", "Paris"]])]}
    named = {"orders.city": ["Paris", "Lyon"]}
    (variant,) = either(paris, named)
    assert variant["blocks"][0]["where"] == [["orders.city", "=", "Paris"], ["orders.city", "=", "Lyon"]]
    assert variant["blocks"][0]["conj"] == "OR"
    (value,), = rows_of(render_query(build(variant, graph_of(ORDERS))), ORDERS)
    assert value == 159, value
    both = {**paris, "blocks": [{**paris["blocks"][0], "where": [["orders.city", "=", "Paris"],
                                                                 ["orders.amount", ">", 20]]}]}
    assert list(either(both, named)) == [] and list(either(variant, named)) == []


def test_a_named_period_groups_by_the_period():
    """ "How many orders per month?" grouped by the order date itself, one row per day, and was declined: a period the
    question groups by (engine.sql_dates.period_grouping) groups by that period of the date column, its year-month,
    and shows it where the reading showed the date or its earliest."""
    from engine.readers.decode import periodic
    graph = graph_of(ORDERS)
    select, group, order = periodic([[0, "MIN", "orders.placed", False], [1, "COUNT", "*", False]],
                                    ["orders.placed"], [], graph, "how many orders per month?")
    assert group == ["year_month:orders.placed"] and select[0] == [0, "plain", "year_month:orders.placed", False]
    readings = {"structure": "single", "blocks": [block(select=select, group=group)]}
    query = build(readings, graph)
    assert sorted(rows_of(render_query(query), ORDERS)) == [("2026-07", 1), ("2026-08", 3)]
    assert query.select[0].alias == "month", render_query(query)          # headed by its period, not its SQL
    assert periodic([[0, "plain", "orders.placed", False]], ["orders.placed"], [], graph,
                    "how many orders on each date?")[1] == ["orders.placed"]


def test_a_set_membership_compares_values_of_one_kind():
    """ "List the product names that no customer from Paris has bought" was read as "product id is not among (the
    product names Paris customers bought)" (2026-10-22): a number never equals a name, so every product passed. The
    builder refuses a membership that compares a number with text; the same reading over the names builds."""
    products = {"name": "products", "columns": ["product_id", "product_name"],
                "rows": [[1, "Alpha"], [2, "Beta"], [3, "Delta"]]}
    purchases = {"name": "purchases", "columns": ["purchase_id", "city", "product_name"],
                 "rows": [[1, "Paris", "Alpha"], [2, "Lyon", "Beta"]]}
    graph = graph_of(products, purchases)

    def readings(column):
        return {"structure": "NESTED", "blocks": [
            block(tables=["products"], select=[[0, "plain", "products.product_name", False]],
                  where=[[column, "NOT IN", {"block": 1}]]),
            block(tables=["purchases"], select=[[0, "plain", "purchases.product_name", False]],
                  where=[["purchases.city", "=", "Paris"]])]}
    try:
        build(readings("products.product_id"), graph)
    except NotReadable as exc:
        assert "number-with-text" in str(exc), exc
    else:
        raise AssertionError("a number was compared with names")
    query = build(readings("products.product_name"), graph)
    assert sorted(rows_of(render_query(query), products, purchases)) == [("Beta",), ("Delta",)]


def test_every_column_is_the_star_shown_plain():
    """ "show all rows ordered by id" asks for every column (tests.test_scale), which the readings could not hold: a
    SELECT * gold was not readable ("bare-star", 55 Spider training questions), so no reader learned it and the
    readers showed the id alone (2026-10-23). The star shown plain is every column: SELECT * round-trips through the
    readings and the builder, reads as "every column", and the decoder reads it in a selection."""
    from engine.readers.read_as import read_as
    from regress.sql_import import import_sql
    graph = graph_of(ORDERS)
    readings = readings_of(import_sql('SELECT * FROM "orders" ORDER BY "orders"."order_id" ASC', graph))
    assert readings["blocks"][0]["select"] == [[0, "plain", "*", False]], readings
    query = build(readings, graph)
    assert render_query(query).startswith('SELECT * FROM "orders"'), render_query(query)
    assert sorted(rows_of(render_query(query), ORDERS)) == sorted(tuple(row) for row in ORDERS["rows"])
    assert read_as(readings).startswith("every column of orders"), read_as(readings)
    counted = readings_of(import_sql('SELECT COUNT(*) FROM "orders"', graph))
    assert counted["blocks"][0]["select"] == [[0, "COUNT", "*", False]], counted


def test_a_number_column_compares_with_numbers_and_never_with_its_own_values():
    """ "Count all non-empty Order ID rows below the header in the Customers sheet" was read as "order id is
    'non-empty'", as "order id is not among (order id of customers)" and as "order id is (order id of customers)"
    (2026-10-23): none counts the sheet's 4 orders. The builder refuses a number column compared with a text that is
    no number, and a column compared with its own unfiltered values. Contrast: a number, or a number written as text, compares; a text column
    compares with text; a membership in the column's values under a condition stands."""
    sheet = {"name": "orders", "columns": ["order_id", "customer", "city"],
             "rows": [[101, "Poirot", "Paris"], [102, "Lupin", "Lyon"], [103, "Holmes", "Paris"]]}
    graph = graph_of(sheet)

    def counted(column, value):
        return {"structure": "single", "blocks": [block(tables=["orders"], select=[[0, "COUNT", "*", False]],
                                                        where=[[column, "=", value]])]}
    for refused in (counted("orders.order_id", "non-empty"), counted("orders.order_id", "Count all")):
        try:
            build(refused, graph)
        except NotReadable as exc:
            assert "number-with-text" in str(exc), exc
        else:
            raise AssertionError("a number column was compared with a word")
    for column, value, rows in (("orders.order_id", 101, [(1,)]), ("orders.city", "Paris", [(2,)])):
        assert rows_of(render_query(build(counted(column, value), graph)), sheet) == rows, (column, value)
    for spelled in ("102", "null", ""):                 # a number written as text; a missing number as data spells it
        build(counted("orders.order_id", spelled), graph)

    def among_itself(where):
        return {"structure": "NESTED", "blocks": [
            block(tables=["orders"], select=[[0, "COUNT", "*", False]], where=[["orders.order_id", "NOT IN", {"block": 1}]]),
            block(tables=["orders"], select=[[0, "plain", "orders.order_id", False]], where=where)]}
    equal_itself = among_itself([])
    equal_itself["blocks"][0]["where"] = [["orders.order_id", "=", {"block": 1}]]
    for itself in (among_itself([]), equal_itself):
        try:
            build(itself, graph)
        except NotReadable as exc:
            assert "with-itself" in str(exc), exc
        else:
            raise AssertionError("a column was compared with its own values")
    filtered = build(among_itself([["orders.city", "=", "Paris"]]), graph)
    assert rows_of(render_query(filtered), sheet) == [(1,)]
    ranked = among_itself([])                           # one ranked row's own value is a condition
    ranked["blocks"][0]["where"] = [["orders.order_id", ">", {"block": 1}]]
    ranked["blocks"][1].update(order=[["plain", "orders.city"]], dir="ASC", limit=1)
    assert rows_of(render_query(build(ranked, graph)), sheet) == [(1,)]


def test_a_membership_of_two_kinds_is_read_on_the_column_its_values_reference():
    """Every reader read "List the product names that no customer from Paris has bought" as "product id is not among
    (the product names Paris customers bought)" (2026-10-23). The search reads it again on the column the listed
    names reference (purchases.product_name -> products.product_name), or on the listed column itself when the
    subquery lists the outer table's own column; a membership of one kind, or one no foreign key pairs, has no
    variant."""
    from engine.readers.search import referenced
    products = {"name": "products", "columns": ["product_id", "product_name"],
                "rows": [[1, "Alpha"], [2, "Beta"], [3, "Delta"]]}
    purchases = {"name": "purchases", "columns": ["purchase_id", "city", "product_name", "product_ref"],
                 "rows": [[1, "Paris", "Alpha", 1], [2, "Lyon", "Beta", 2]]}
    by_name = graph_of(products, purchases, fks=[("purchases", "product_name", "products", "product_name")])

    def readings(column, listed, inner_tables=("purchases",)):
        return {"structure": "NESTED", "blocks": [
            block(tables=["products"], select=[[0, "plain", "products.product_name", False]],
                  where=[[column, "NOT IN", {"block": 1}]]),
            block(tables=list(inner_tables), select=[[0, "plain", listed, False]],
                  where=[["purchases.city", "=", "Paris"]])]}
    (variant,) = referenced(readings("products.product_id", "purchases.product_name"), by_name)
    assert variant["blocks"][0]["where"] == [["products.product_name", "NOT IN", {"block": 1}]], variant
    assert sorted(rows_of(render_query(build(variant, by_name)), products, purchases)) == [("Beta",), ("Delta",)]
    # The subquery lists the outer table's own names: the membership compares them.
    (joined,) = referenced(readings("products.product_id", "products.product_name", ("products", "purchases")),
                           by_name)
    assert joined["blocks"][0]["where"][0][0] == "products.product_name", joined
    # One kind already (ids among the ids purchases reference): the reading stands as read.
    by_id = graph_of(products, purchases, fks=[("purchases", "product_ref", "products", "product_id")])
    assert list(referenced(readings("products.product_id", "purchases.product_ref"), by_id)) == []
    # Two kinds, and no foreign key pairs the listed names with a products column: nothing to read it on.
    assert list(referenced(readings("products.product_id", "purchases.city"), by_name)) == []


def test_a_named_period_groups_the_date_when_the_readers_group_by_a_row_key():
    """ "how many orders per month?" was read as "for each order id" (one group per order) by every reader of
    v11, and declined (2026-10-22): when the question asks for one row per month and the readers group only by
    surrogate keys or by nothing, the period groups the one date column of the tables they read, shown first."""
    from engine.readers.decode import periodic
    graph = graph_of(ORDERS)
    select, group, order = periodic([[0, "COUNT", "*", False]], ["orders.order_id"], [], graph,
                                    "how many orders per month?", tables=["orders"])
    assert group == ["year_month:orders.placed"], group
    assert select == [[0, "plain", "year_month:orders.placed", False], [1, "COUNT", "*", False]], select
    query = build({"structure": "single", "blocks": [block(select=select, group=group)]}, graph)
    assert sorted(rows_of(render_query(query), ORDERS)) == [("2026-07", 1), ("2026-08", 3)]
    # The key shown as its group's label gives way to the period; no grouping at all takes the period too.
    select, group, _ = periodic([[0, "plain", "orders.order_id", False], [1, "COUNT", "*", False]],
                                ["orders.order_id"], [], graph, "how many orders per month?", tables=["orders"])
    assert select == [[0, "plain", "year_month:orders.placed", False], [1, "COUNT", "*", False]], select
    assert periodic([[0, "COUNT", "*", False]], [], [], graph, "how many orders per month?",
                    tables=["orders"])[1] == ["year_month:orders.placed"]
    # Contrast: a grouping by a field that is no key stands; no period asked, or no table read, the reading stands.
    assert periodic([[0, "COUNT", "*", False]], ["orders.city"], [], graph, "how many orders per month?",
                    tables=["orders"])[1] == ["orders.city"]
    assert periodic([[0, "COUNT", "*", False]], ["orders.order_id"], [], graph, "how many orders per order?",
                    tables=["orders"])[1] == ["orders.order_id"]
    assert periodic([[0, "COUNT", "*", False]], ["orders.order_id"], [], graph, "how many orders per month?",
                    tables=[])[1] == ["orders.order_id"]


def test_the_highest_of_the_one_ranked_column_is_its_max():
    """A block that shows only the column it ranks, first row, is that column's MAX (or MIN): the same value, and an
    aggregate both deterministic emitters express ("What is the highest purchase cost?" ran only as SQL)."""
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "plain", "orders.amount", False]],
                                                        order=[["plain", "orders.amount"]], dir="DESC", limit=1)]}
    sql = render_query(build(readings, graph))
    assert sql == 'SELECT MAX("orders"."amount") FROM "orders"', sql
    other = {"structure": "single", "blocks": [block(select=[[0, "plain", "orders.city", False]],
                                                     order=[["plain", "orders.amount"]], dir="DESC", limit=1)]}
    assert "LIMIT 1" in render_query(build(other, graph))


def test_a_share_divides_the_part_by_the_whole():
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "SUM", "orders.amount", False]],
                                                        where=[["orders.city", "=", "Paris"]], share=[0])]}
    (value,), = rows_of(render_query(build(readings, graph)), ORDERS)
    assert abs(value - 129 / 207) < 1e-9, value


def test_a_share_keeps_the_whole_conditions_in_both():
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "COUNT", "*", False]],
                                                        where=[["orders.city", "=", "Paris"],
                                                               ["orders.amount", ">", 40]], share=[1])]}
    (value,), = rows_of(render_query(build(readings, graph)), ORDERS)
    assert value == 0.5, value                                   # of the 2 Paris orders, 1 is above 40


def test_a_share_needs_one_aggregate():
    graph = graph_of(ORDERS)
    readings = {"structure": "single", "blocks": [block(select=[[0, "plain", "orders.city", False]],
                                                        where=[["orders.city", "=", "Paris"]], share=[0])]}
    try:
        build(readings, graph)
    except NotReadable:
        return
    raise AssertionError("a share of a plain column was built")


def test_a_contained_text_matches_in_any_case():
    """A LIKE reading compares LOWER(column) with the lower-cased text: SQLite's LIKE ignores ASCII case and
    PostgreSQL's does not, so "%West%" built as plain LIKE ran on the pool's SQLite copy and missed rows served
    from PostgreSQL."""
    shops = {"name": "shops", "columns": ["shop_id", "name"], "rows": [[1, "West Side"], [2, "east end"]]}
    graph = graph_of(shops)
    readings = {"structure": "single", "blocks": [block(tables=["shops"], select=[[0, "plain", "shops.name", False]],
                                                        where=[["shops.name", "LIKE", "%WEST%"]])]}
    sql = render_query(build(readings, graph))
    assert 'LOWER("shops"."name") LIKE \'%west%\'' in sql, sql
    assert rows_of(sql, shops) == [("West Side",)]
    assert readings_of(build(readings, graph))["blocks"][0]["where"] == [["shops.name", "LIKE", "%west%"]]


def test_a_pinned_column_is_not_shown_again():
    """"Show the players from college UCLA" shows the players: the college the condition fixes says nothing more."""
    graph = graph_of(PEOPLE)
    names = ["*", "players.player_id", "players.player", "players.college", "players.position"]
    question = "Show the players from college UCLA."
    picks = {("players.college", "select", 0): 0.9, ("players.player", "select", 0): 0.6,
             ("players.college", "where", OPERATORS.index("=")): 0.95}
    g = counts({}, tables=1, select=1, where=1)
    start = question.index("UCLA")
    output = output_for(names, ["players"], picks, g,
                        values={"0|players.college": [[0, start, start + len("UCLA"), 9.0]]})
    example = {"question": question, "text": ""}
    readings = decode(output, example, graph)
    assert [s[2] for s in readings["blocks"][0]["select"]] == ["players.player"], readings
    assert readings["blocks"][0]["where"] == [["players.college", "=", "UCLA"]]


def test_a_matched_column_is_not_counted_distinct():
    """"How many players are from college UCLA?" counts the UCLA rows: the distinct colleges of rows pinned to UCLA
    are always one, and of those whose college contains it, few. A column the conditions leave free keeps its
    distinct count."""
    graph = graph_of(PEOPLE)
    names = ["*", "players.player_id", "players.player", "players.college", "players.position"]
    question = "How many players are from college UCLA?"
    start = question.index("UCLA")
    values = {"0|players.college": [[0, start, start + len("UCLA"), 9.0]]}
    g = counts({}, tables=1, select=1, where=1)

    def read(counted):
        picks = {(counted, "select", AGGREGATES.index("COUNT")): 0.9, (counted, "distinct_aggregate", 0): 0.9,
                 ("players.college", "where", OPERATORS.index("=")): 0.95}
        readings = decode(output_for(names, ["players"], picks, g, values=values), {"question": question, "text": ""},
                          graph)
        return readings, render_query(build(readings, graph))

    readings, sql = read("players.college")
    assert readings["blocks"][0]["select"] == [[0, "COUNT", "players.college", False]], readings
    assert rows_of(sql, PEOPLE) == [(2,)], sql
    readings, sql = read("players.position")
    assert readings["blocks"][0]["select"] == [[0, "COUNT", "players.position", True]], readings
    assert rows_of(sql, PEOPLE) == [(2,)], sql                       # Guard and Forward
    contained = output_for(names, ["players"], {
        ("players.college", "select", AGGREGATES.index("COUNT")): 0.9, ("players.college", "distinct_aggregate", 0): 0.9,
        ("players.college", "where", OPERATORS.index("LIKE")): 0.95}, g, values=values)
    readings = decode(contained, {"question": question, "text": ""}, graph)
    assert readings["blocks"][0]["select"] == [[0, "COUNT", "players.college", False]], readings


def test_a_count_of_a_referencing_column_counts_each_row_it_names_once():
    """"How many dogs have had a treatment?" was read as the count of treatments.dog_id and answered 3 (three
    treatments of two dogs): ungrouped, a count of a column that references another table's key counts each row it
    names once. A grouped count, and a count of a column that references nothing, keep their repeats."""
    dogs = {"name": "dogs", "columns": ["dog_id", "name"], "rows": [[1, "Rex"], [2, "Fido"], [3, "Max"]]}
    treatments = {"name": "treatments", "columns": ["treatment_id", "dog_id", "cost"],
                  "rows": [[1, 1, 50], [2, 1, 20], [3, 2, 30]]}
    graph = graph_of(dogs, treatments, fks=[("treatments", "dog_id", "dogs", "dog_id")])
    names = ["*", "treatments.treatment_id", "treatments.dog_id", "treatments.cost"]
    question = "How many dogs have had a treatment?"

    def read(counted, grouped=False):
        picks = {(counted, "select", AGGREGATES.index("COUNT")): 0.9, (counted, "distinct_aggregate", 0): 0.05}
        g = counts({}, tables=1, select=1, group=int(grouped))
        if grouped:
            picks[("treatments.cost", "group", 0)] = 0.9
        readings = decode(output_for(names, ["treatments"], picks, g), {"question": question, "text": ""}, graph)
        return readings, render_query(build(readings, graph))

    readings, sql = read("treatments.dog_id")
    assert readings["blocks"][0]["select"] == [[0, "COUNT", "treatments.dog_id", True]], readings
    assert rows_of(sql, dogs, treatments) == [(2,)], sql
    readings, _ = read("treatments.cost")
    assert readings["blocks"][0]["select"] == [[0, "COUNT", "treatments.cost", False]], readings
    readings, _ = read("treatments.dog_id", grouped=True)
    assert readings["blocks"][0]["select"][0][3] is False, readings


def test_a_date_condition_reads_the_days_the_question_names():
    graph = graph_of(ORDERS)
    where = dated([["orders.placed", "<", "July 10"]], graph, "How many orders were placed before July 10, 2026?")
    assert where == [["orders.placed", "<", "2026-07-10"]], where
    where = dated([["orders.placed", "=", "August"]], graph, "total amount placed in August")
    assert where == [["month:orders.placed", "=", 8]], where


def test_a_stored_date_keeps_its_exact_value():
    """A literal the question gives in stored form ("before 2018-03-17 07:13:53") is exact to the second."""
    graph = graph_of(ORDERS)
    where = [["orders.placed", "<", "2026-08-04 10:00:00"]]
    assert dated(where, graph, "orders placed before 2026-08-04 10:00:00") == where


def test_a_listed_date_is_not_the_question_s_stored_form():
    """A reader that points into the schema's listed dates reads a stored-form literal the question never says
    ("between August 4 and August 9, 2026" read as '2026-08-04' and '26-08-04', 9 rows for 4, 2026-10-25): the
    question's own date phrase says the days. A literal the question states keeps its exact value."""
    graph = graph_of(ORDERS)
    question = "How many orders were placed between August 4 and August 9, 2026?"
    where = dated([["orders.placed", ">=", "2026-08-04"], ["orders.placed", "<=", "26-08-04"]], graph, question)
    assert where == [["orders.placed", ">=", "2026-08-04"], ["orders.placed", "<", "2026-08-10"]], where
    stated = [["orders.placed", "<", "2026-08-04"]]
    assert dated(stated, graph, "orders placed before 2026-08-04") == stated


def test_a_date_range_is_not_read_under_or():
    graph = graph_of(ORDERS)
    where = [["orders.city", "=", "Paris"], ["orders.placed", "=", "August 4"]]
    question = "orders from Paris or placed between August 4 and August 9, 2026"
    assert dated(where, graph, question, ranges=False) == where


def test_a_pointed_value_drops_the_marks_of_its_list():
    assert unlisted("2026-08-04)") == "2026-08-04"
    assert unlisted("(2026-08-03") == "2026-08-03"
    assert unlisted("Comp. Sci. (Hons)") == "Comp. Sci. (Hons)"
    assert unlisted(" Paris, ") == "Paris"


def test_the_question_values_are_listed_beside_their_columns():
    graph = graph_of(PEOPLE)
    schema = Schema(graph)
    anchors = schema.anchors("Which positions do players from Duke play?")
    assert anchors == {("players", "college"): ["Duke"]}, anchors
    text, _, _, spans = reading_text(schema, ["players"], {"players": schema.columns["players"]}, True, anchors)
    assert "college [text] (Duke" in text, text


def test_the_samples_go_before_a_table():
    """The token budget drops a table's samples before it drops a table (engine.readers.text.fit): a reading of a
    dropped table can never be right. College_1's seven tables kept three or four with samples (2026-10-25)."""
    events = {"name": "events", "columns": ["event_id", "title"],
              "rows": [[i, f"Event number {i} title"] for i in range(10)]}
    schema = Schema(graph_of(ORDERS, PEOPLE, events))
    tables = ["orders", "players", "events"]
    columns_of = {t: schema.columns[t] for t in tables}

    def tokenizer(question, text):
        return {"input_ids": (question + " " + text).split()}

    full = len(tokenizer("q", reading_text(schema, tables, columns_of, True)[0])["input_ids"])
    bare = len(tokenizer("q", reading_text(schema, tables, columns_of, False)[0])["input_ids"])
    assert bare < full, (bare, full)
    kept, (text, *_) = fit(tokenizer, bare, "q", schema, tables, columns_of, ["events", "players"], anchored=False)
    assert kept == tables and "Event number 0" not in text, (kept, text)
    kept, (text, *_) = fit(tokenizer, full, "q", schema, tables, columns_of, ["events", "players"], anchored=False)
    assert kept == tables and "Event number 0" in text, (kept, text)
    kept, _ = fit(tokenizer, bare - 1, "q", schema, tables, columns_of, ["events", "players"], anchored=False)
    assert kept == ["orders", "players"], kept


def test_the_choice_ranks_by_agreement_and_refuses_below_its_threshold():
    """The readers' choice (choice.json) weighs the re-scorer's logit with what the pool says of a candidate; of two
    candidates the re-scorer scores alike, the one more of the readers' readings build ranks first (calibration,
    four readers: 642 of 1,028 chosen right against 628 for the re-scorer alone). Its probability is the confidence,
    and a choice under the threshold is refused."""
    import math

    from engine.readers import search
    from engine.sql_candidate import ScoredQuery

    readers = search.ReadingSearch.__new__(search.ReadingSearch)      # no weights: the logits are memoized below
    readers.choice = {"features": list(search.choice_feature_names(["rescorer_a"])), "coef": [0.4, -0.3, -0.2, 1.5, 0.4],
                      "intercept": -1.5, "threshold": 0.3}
    pool = [ScoredQuery(None, "SELECT 1", -1.0, ("one",), (("rank", 0.0), ("readers", 0.25))),
            ScoredQuery(None, "SELECT 2", -1.0, ("two",), (("rank", 0.0), ("readers", 1.0))),
            ScoredQuery(None, "SELECT 3", -2.0, ("three",), (("rank", 3.0), ("readers", 0.25)))]
    shapes = [(1, 1), (1, 1), None]
    search._keep(search._SCORES, ("q", ("SELECT 1", "SELECT 2"), ((1, 1), (1, 1))), ((2.0,), (2.0,)))
    scores = readers.rescore("q", pool, shapes)
    assert scores[2] is None and scores[1] > scores[0], scores               # agreement breaks the tie
    expected = -1.5 + sum(w * x for w, x in zip(readers.choice["coef"], search.choice_features((2.0,), -1.0, 0.0, 1.0, 1)))
    assert abs(scores[1] - expected) < 1e-12
    confidence = readers.confidence(pool, scores, shapes, 1)
    assert abs(confidence - 1.0 / (1.0 + math.exp(-scores[1]))) < 1e-12
    assert readers.confident(confidence) == (confidence >= 0.3) and readers.confidence(pool, scores, shapes, 2) is None
    assert not readers.confident(0.29) and readers.confident(None)
    readers.choice = {**readers.choice, "threshold": None}                   # an evaluation records every confidence
    assert readers.confident(0.01)
    # Two re-scorers (rescorer_a, rescorer_b): the choice weighs each one's logit, in the bundle's order. Where the
    # first scores two candidates alike, the second's disagreement decides.
    assert search.choice_feature_names(["rescorer_a", "rescorer_b"])[:2] == ("rescorer_a_logit", "rescorer_b_logit")
    readers.choice = {"features": list(search.choice_feature_names(["rescorer_a", "rescorer_b"])),
                      "coef": [0.4, 0.5, -0.3, -0.2, 1.5, 0.4], "intercept": -1.5, "threshold": 0.3}
    search._keep(search._SCORES, ("q2", ("SELECT 1", "SELECT 2"), ((1, 1), (1, 1))), ((2.0, 3.0), (2.0, -1.0)))
    scores = readers.rescore("q2", pool, shapes)
    assert scores[0] > scores[1], scores
    expected = -1.5 + sum(w * x for w, x in zip(readers.choice["coef"],
                                                search.choice_features((2.0, 3.0), -1.0, 0.0, 0.25, 1)))
    assert abs(scores[0] - expected) < 1e-12


def test_a_reader_that_reads_nothing_is_no_dissent():
    """The readers' share counts a candidate's readings among the readers that build any reading of the question: two
    readers trained on sheet language read "Count all non-empty Order ID rows below the header in the Customers sheet"
    as a count, and the other two built no reading, which halved the counts' share (2026-10-23). Serving
    (search.readers_share, over the pool) and the choice's fit (training.readers.choice.features, over each reader's
    calibration candidates) count it alike."""
    import json
    import tempfile
    from pathlib import Path

    from engine.readers.search import readers_share
    from training.readers import choice

    assert readers_share(2, 2) == 1.0 and readers_share(1, 4) == 0.25 and readers_share(1, 0) == 1.0
    question = {"source": "s", "index": 0, "db_id": "d", "question": "q"}
    counted = {"rank": 0, "score": -0.2, "read_as": "number of rows", "status": "ran", "rows": 1, "columns": 1,
               "sql": "SELECT COUNT(*) FROM t", "right": True}
    refused = {"rank": 0, "score": -0.3, "read_as": "number of rows where id is 'non-empty'", "status": "build",
               "rows": 0, "columns": 0, "sql": None}
    with tempfile.TemporaryDirectory() as folder:
        paths = []
        for name, candidates in (("reads", [counted]), ("silent", [refused])):
            path = Path(folder) / f"{name}.jsonl"
            path.write_text(json.dumps({**question, "candidates": candidates}) + "\n", encoding="utf-8")
            paths.append(str(path))
        X, y, _ = choice.features([{**question, "candidates": [counted]}], paths,
                                  [{("s", 0): {"SELECT COUNT(*) FROM t": 1.5}}])
    assert X.shape == (1, 5) and X[0][3] == 1.0 and list(y) == [1], X


def test_candidates_are_distinct_and_most_probable_first():
    graph = graph_of(ORDERS)
    names = ["*", "orders.order_id", "orders.city", "orders.amount", "orders.placed"]
    picks = {("orders.amount", "select", AGGREGATES.index("SUM")): 0.6,
             ("orders.amount", "select", AGGREGATES.index("AVG")): 0.4}
    output = output_for(names, ["orders"], picks, counts({}, tables=1, select=1))
    found = candidates(output, {"question": "total amount", "text": ""}, graph, k=4)
    shown = [r["blocks"][0]["select"][0][1] for _, r in found]
    assert shown[:2] == ["SUM", "AVG"], shown
    assert len({str(r) for _, r in found}) == len(found)
    assert [s for s, _ in found] == sorted((s for s, _ in found), reverse=True)


def test_choices_override_by_rank():
    choices = Choices({("d",): 1})
    assert choices.pick(("d",), [("a", 0.7), ("b", 0.3)]) == "b"
    assert Choices().pick(("d",), [("a", 0.3), ("b", 0.7)]) == "b"


def _shop(folder, totals, fks=([8, 1], [9, 4])):
    """A training-shaped database (Spider metadata and a SQLite file): customers, products, and purchases whose
    line totals are ``totals`` (one per purchase), with the foreign keys ``fks`` (column indexes)."""
    import os

    from training.readers import structures
    purchases = [(1, 1, 11), (2, 3, 12), (3, 2, 13), (4, 4, 11), (5, 2, 11), (6, 4, 12)]
    tables = {"customers": (["customer_id", "customer_name", "city"],
                            [(1, "Ann", "Paris"), (2, "Bob", "Lyon"), (3, "Cyd", "Paris"), (4, "Dee", "Lyon"),
                             (5, "Eve", "Nice")]),
              "products": (["product_id", "product_name", "category"],
                           [(11, "Alpha", "Toys"), (12, "Beta", "Home"), (13, "Gamma", "Toys"), (14, "Delta", "Home"),
                            (15, "Omega", "Garden")]),
              "purchases": (["purchase_id", "customer_id", "product_id", "line_total"],
                            [row + (total,) for row, total in zip(purchases, totals)])}
    path = os.path.join(folder, f"shop{len(os.listdir(folder))}.sqlite")
    connection = sqlite3.connect(path)
    for name, (columns, rows) in tables.items():
        connection.execute(f'CREATE TABLE "{name}" ({", ".join(columns)})')
        connection.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" for _ in columns)})', rows)
    connection.commit()
    connection.close()
    columns = [[-1, "*"]] + [[index, column] for index, (cs, _) in enumerate(tables.values()) for column in cs]
    meta = {"db_id": "shop", "table_names_original": list(tables), "column_names_original": columns,
            "column_types": ["text"] * len(columns), "primary_keys": [1, 4, 7],
            "foreign_keys": [list(fk) for fk in fks]}
    return structures.Database("shop", meta, path)


def test_built_structures_exclude_and_rank_by_the_total():
    """training.readers.structures: an exclusion filtered through a referenced table drops what that value's rows
    reference and reads as NOT IN a subquery; one value of a filter is kept; a ranking is built only when ranking by
    the number gives other first groups than ranking by row count; an aggregate reads a number of one table or a
    category's number of rows; a grouped total shows each group beside its total, unranked; a question must state the
    value and the number."""
    import tempfile

    from regress.sql_import import import_sql
    from training.readers import structures
    with tempfile.TemporaryDirectory() as folder:
        db = _shop(folder, [10, 10, 500, 5, 5, 20])
        paris = [c for c in structures.unmatched(db) if c["value"] == "Paris" and c["table"] == "products"]
        assert len(paris) == 1
        assert sorted(row[0] for row in db.run(paris[0]["sql"])) == ["Delta", "Gamma", "Omega"]
        readings = readings_of(import_sql(paris[0]["sql"], db.graph))
        where = [list(condition) for condition in readings["blocks"][0]["where"]]
        assert len(readings["blocks"]) == 2 and where[0][1:] == ["NOT IN", {"block": 1}], where
        jobs = structures.queries(db)
        city = [j for j in jobs if '"customers"."city" =' in j["sql"] and j["sql"].startswith('SELECT "products"')]
        assert len(city) <= 1, [j["sql"] for j in city]
        assert any(j["shape"] == "ranked" for j in jobs)
        aggregates = {c["sql"] for c in structures.aggregated(db)}
        assert any(sql.endswith('("purchases"."line_total") FROM "purchases"') for sql in aggregates), aggregates
        assert 'SELECT COUNT(*) FROM "customers" WHERE "customers"."city" = \'Lyon\'' in aggregates, aggregates
        # each category of products beside its purchases' total (or average), unranked
        grouped = [c for c in structures.grouped(db) if '"products"."category"' in c["sql"]]
        assert grouped and all("ORDER BY" not in c["sql"] and "LIMIT" not in c["sql"] for c in grouped), grouped
        totals = dict((row if isinstance(row[0], str) else row[::-1]) for row in db.run(grouped[0]["sql"]))
        assert set(totals) == {"Toys", "Home"}, totals
        # every column of a table's rows, read as the star shown plain
        listings = [job for job in structures.queries(db) if job["shape"] == "listing"]
        assert listings and all(job["sql"].startswith("SELECT * FROM") for job in listings), listings
        assert all(job["readings"]["blocks"][0]["select"] == [[0, "plain", "*", False]] for job in listings)
        db.connection.close()
        # each product's purchases share one line total that grows with their number: every ranking of products by
        # a total or an average is the ranking by row count, so no ranking is built
        same = _shop(folder, [30, 20, 10, 30, 30, 20], fks=([9, 4],))
        assert not structures.ranked(same), [c["sql"] for c in structures.ranked(same)]
        same.connection.close()
    assert structures.keeps({"value": "Paris", "limit": None}, "Which products did nobody in Paris buy?")
    assert not structures.keeps({"value": "Paris", "limit": None}, "Which products did nobody in Lyon buy?")
    assert structures.keeps({"value": None, "limit": 2}, "the two categories that bring in the most revenue")
    assert not structures.keeps({"value": None, "limit": 2}, "the categories that bring in the most revenue")


def test_the_readers_compute_in_float32_over_weights_kept_in_bfloat16():
    """2026-10-27: the readers ran in bfloat16 on Cloud Run's AVX2-only CPUs (AMD EPYC 7B13), which emulate it: one
    large reader's pass took 15.3 s against 2.7 s in float32. Serving computes in float32; the large weights stay in
    bfloat16 (float32 ones would need 4 GiB more) and are widened as they are used."""
    import tempfile
    from pathlib import Path

    import torch
    from transformers import XLMRobertaConfig, XLMRobertaModel

    from engine.readers import models

    config = XLMRobertaConfig(vocab_size=64, hidden_size=16, num_hidden_layers=2, num_attention_heads=2,
                              intermediate_size=32, max_position_embeddings=40)
    torch.manual_seed(0)
    saved = models.Rescorer(XLMRobertaModel(config)).eval()
    with tempfile.TemporaryDirectory() as directory:
        config.save_pretrained(directory)
        state = {key: (value.to(torch.bfloat16) if key.startswith("encoder.") and value.is_floating_point() else value)
                 for key, value in saved.state_dict().items()}                   # stored as promotion stores it
        torch.save(state, Path(directory) / "model.pt")
        served = models.load("rescorer", directory)
    table = served.encoder.get_input_embeddings().weight
    assert table.dtype == torch.bfloat16
    linears = [module for module in served.encoder.modules() if isinstance(module, torch.nn.Linear)]
    assert linears and all(module.parametrizations.weight.original.dtype == torch.bfloat16 for module in linears)
    assert all(module.weight.dtype == torch.float32 for module in linears)       # read widened
    stored = {name: parameter.dtype for name, parameter in served.named_parameters()
              if parameter is not table and ".parametrizations." not in name}
    assert set(stored.values()) == {torch.float32}, stored                         # biases, norms, positions, head
    ids = torch.tensor([[0, 5, 9, 2, 1, 1]])
    mask = torch.tensor([[1, 1, 1, 1, 0, 0]])
    features = torch.zeros(1, models.RESCORER_FEATURES)
    with torch.inference_mode():
        score = served(ids, mask, features)
        states = served.encoder(input_ids=ids, attention_mask=mask).last_hidden_state
    assert score.dtype == torch.float32 and states.dtype == torch.float32
    assert torch.isfinite(score).all()



def test_joint_decisions_use_input_derived_options_and_the_existing_builder():
    from engine.readers.decisions import request, supervision, resolve, InvalidDecision
    graph = graph_of(ORDERS)
    turns = [{'role': 'user', 'content': 'Total amount for Paris?'},
             {'role': 'user', 'content': 'Actually use Lyon instead of Paris.'}]
    reading = {'structure': 'single', 'blocks': [block(select=[[0, 'SUM', 'orders.amount', False]],
                                                     where=[['orders.city', '=', 'Lyon']])]}
    record, bindings = request(turns, graph)
    before = repr(record)
    labelled = supervision(reading, record, bindings)
    assert repr(record) == before and 'gold' not in record
    result = resolve({k: v['label'] for k, v in labelled['gold'].items()}, record, bindings, graph)
    assert result['readings'] == reading
    assert rows_of(render_query(result['query']), ORDERS) == [(30,)]
    bad = __import__('copy').deepcopy(reading)
    bad['blocks'][0]['where'][0][2] = '__not_in_the_input__'
    try:
        supervision(bad, record, bindings)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('a target must not extend the input option bank')


def test_joint_decisions_fail_closed_and_can_request_clarification():
    from engine.readers.decisions import request, supervision, resolve, InvalidDecision
    graph = graph_of(ORDERS)
    record, bindings = request('Show the top results.', graph)
    labelled = supervision(None, record, bindings, action='clarify')
    answers = {k: v['label'] for k, v in labelled['gold'].items()}
    result = resolve(answers, record, bindings, graph)
    assert result == {'action': 'clarify', 'readings': None, 'query': None}
    for mutation in ('missing', 'unknown', 'extra'):
        bad = dict(answers)
        if mutation == 'missing':
            bad.pop('action')
        elif mutation == 'unknown':
            bad['action'] = 'invented-option'
        else:
            bad['unexpected'] = 'o0'
        try:
            resolve(bad, record, bindings, graph)
        except InvalidDecision:
            pass
        else:
            raise AssertionError(mutation + ' decisions accepted')


def test_ace_view_roundtrips_meaning_and_rejects_unused_facts():
    import copy
    from engine.readers.ace import encode, decode
    from engine.readers.decisions import InvalidDecision
    graph = graph_of(ORDERS)
    reading = {'structure': 'single', 'blocks': [block(select=[[0, 'SUM', 'orders.amount', False]],
                                                     where=[['orders.city', '=', 'Paris']])]}
    text, document = encode(reading)
    recovered, query = decode(document, graph)
    assert recovered == reading
    assert 'v:uses-aggregate "SUM"' in text
    assert rows_of(render_query(query), ORDERS) == [(129,)]
    duplicate = copy.deepcopy(document)
    duplicate['sentences'].insert(-1, copy.deepcopy(document['sentences'][1]))
    malformed = copy.deepcopy(document)
    malformed['sentences'][0]['unexpected'] = True
    for bad in (duplicate, malformed):
        try:
            decode(bad, graph)
        except InvalidDecision:
            pass
        else:
            raise AssertionError('malformed or duplicate ACE facts accepted')


def test_ace_nested_query_keeps_typed_literals_and_block_references():
    from engine.readers.ace import encode, decode
    graph = graph_of(ORDERS)
    reading = {'structure': 'NESTED', 'blocks': [
        block(select=[[0, 'plain', 'orders.city', False]], where=[['orders.amount', '>', {'block': 1}]]),
        block(select=[[0, 'AVG', 'orders.amount', False]])]}
    _, document = encode(reading)
    recovered, query = decode(document, graph)
    assert recovered == reading
    assert rows_of(render_query(query), ORDERS) == [('Paris',)]
    text_reading = {'structure': 'single', 'blocks': [block(select=[[0, 'COUNT', '*', False]],
                        where=[['orders.city', '=', 'a"b\\c']])]}
    _, document = encode(text_reading)
    assert decode(document, graph)[0] == text_reading


def test_joint_prefill_shares_options_and_never_truncates_state():
    from collections import Counter
    from types import SimpleNamespace
    from engine.readers.decisions import encode_record, InvalidDecision
    calls = Counter()
    def tokenizer(text, **kwargs):
        calls[text] += 1
        return SimpleNamespace(input_ids=[ord(c) for c in text])
    q = {'type': 'choice', 'instructions': 'Which value?', 'criteria': {'z': 'Paris', 'a': 'Lyon'}}
    record = {'state': {'conversation': 'preserve-this-entire-state'}, 'questions': {'one': q, 'two': q}}
    encoded = encode_record(tokenizer, record, max_length=10000)
    assert calls['Which value?'] == 1 and calls['\nEND FIELD\n'] == 1, calls
    a, b = encoded.questions
    assert a.option_ids == ('a', 'z')
    assert a.option_spans == b.option_spans
    assert a.question_span != b.question_span
    independent = {'state': record['state'], 'questions': {
        name: {**item, 'criteria': dict(item['criteria'])} for name, item in record['questions'].items()}}
    assert encode_record(tokenizer, independent, max_length=10000) == encoded
    assert 'preserve-this-entire-state' in ''.join(chr(c) for c in encoded.input_ids)
    contextual = {'type': 'choice', 'instructions': 'Which filter column?',
                  'criteria': {'city': '"ledger.city"'}}
    evidence = {'schema': [{'column': 'ledger.city', 'samples': ['Kavora'], 'type': 'text'}],
                'z_complete_state': 'all-evidence-is-before-options'}
    bound = encode_record(tokenizer, {'state': evidence,
                          'questions': {'one': contextual, 'two': contextual}}, max_length=10000)
    rendered = ''.join(chr(c) for c in bound.input_ids)
    end_of_evidence = rendered.index('all-evidence-is-before-options') + len('all-evidence-is-before-options')
    assert bound.questions[0].option_spans == bound.questions[1].option_spans
    assert all(start >= end_of_evidence for start, _ in bound.questions[0].option_spans), (
        'a causal option embedding cannot read schema evidence that follows its reused state span')
    try:
        encode_record(tokenizer, record, max_length=len(encoded.input_ids) - 1)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('oversize input must be rejected, never truncated')



def test_joint_decisions_canonicalize_table_order_and_ground_empty_values():
    from engine.readers.decisions import request, supervision, resolve
    graph = graph_of(ORDERS, PEOPLE, fks=[('orders', 'city', 'players', 'player')])
    reading = {'structure': 'single', 'blocks': [block(tables=['orders', 'players'],
                       select=[[0, 'plain', 'players.player', False]], where=[['orders.city', '=', '']])]}
    # Reversing schema order must not change the canonical meaning.
    reverse = graph_of(PEOPLE, ORDERS, fks=[('orders', 'city', 'players', 'player')])
    for g in (graph, reverse):
        record, bindings = request('List players with an empty city.', g)
        labelled = supervision(reading, record, bindings)
        result = resolve({k: v['label'] for k, v in labelled['gold'].items()}, record, bindings, g)
        assert result['readings'] == reading


def test_joint_candidates_are_scored_without_answers_and_are_repeatable():
    from engine.readers.decisions import request, supervision, candidates, InvalidDecision
    graph = graph_of(ORDERS)
    reading = {'structure': 'single', 'blocks': [block(select=[[0, 'SUM', 'orders.amount', False]],
                                                     where=[['orders.city', '=', 'Paris']])]}
    record, bindings = request('Total amount for Paris?', graph)
    labelled = supervision(reading, record, bindings)
    answers = {key: {'probabilities': {option: float(option == labelled['gold'][key]['label'])
                                     for option in options}} for key, options in bindings.items()}
    name = 'b0.where0.value'
    lyon = next(k for k, v in bindings[name].items() if v == 'Lyon')
    paris = labelled['gold'][name]['label']
    answers[name]['probabilities'][lyon] = 0.51
    answers[name]['probabilities'][paris] = 0.49
    first = candidates(answers, record, bindings, graph, limit=2)
    second = candidates(answers, record, bindings, graph, limit=2)
    assert [render_query(r['query']) for _, r in first] == [render_query(r['query']) for _, r in second]
    assert len(first) == 2
    assert first[0][1]['readings']['blocks'][0]['where'][0][2] == 'Lyon'
    assert first[1][1]['readings'] == reading
    answers[name]['probabilities'][lyon] = float('nan')
    try:
        candidates(answers, record, bindings, graph)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('non-finite probabilities accepted')



def test_joint_search_can_reach_a_valid_column_beyond_the_first_three_choices():
    from engine.readers.decisions import request, supervision, candidates
    table = {'name': 'ledger', 'columns': ['amount', 'units', 'fee', 'city'],
             'rows': [[30, 2, 4, 'Kavora'], [40, 3, 5, 'Senara']]}
    graph = graph_of(table)
    reading = {'structure': 'single', 'blocks': [block(tables=['ledger'],
               select=[[0, 'SUM', 'ledger.amount', False]], where=[['ledger.city', '=', 'Kavora']])]}
    record, bindings = request('Total amount for Kavora?', graph)
    labelled = supervision(reading, record, bindings)
    answers = {key: {'probabilities': {option: float(option == labelled['gold'][key]['label'])
                                     for option in options}} for key, options in bindings.items()}
    name = 'b0.where0.column'
    probabilities = answers[name]['probabilities']
    for option in probabilities:
        probabilities[option] = 0.0
    for column, probability in [('ledger.amount', 0.5), ('ledger.units', 0.25),
                                ('ledger.fee', 0.15), ('ledger.city', 0.1)]:
        probabilities[next(key for key, value in bindings[name].items() if value == column)] = probability
    # Each higher-ranked column is numeric and cannot compare with this text.
    # The valid fourth choice must remain reachable under the attempt budget.
    found = candidates(answers, record, bindings, graph, limit=1, attempts=4)
    assert found and found[0][1]['readings'] == reading
    assert rows_of(render_query(found[0][1]['query']), table) == [(30,)]
    assert candidates(answers, record, bindings, graph, limit=1, attempts=3) == []
    assert [render_query(item['query']) for _, item in found] == [
        render_query(item['query']) for _, item in candidates(answers, record, bindings, graph, limit=1)]


def test_ace_exposes_compiler_normalization_and_not_unused_tables():
    from engine.readers.decisions import request, supervision, resolve
    from engine.readers.ace import decode
    graph = graph_of(ORDERS, PEOPLE)
    raw = {'structure': 'single', 'blocks': [block(tables=['orders', 'players'],
              select=[[0, 'plain', 'orders.amount', False]], order=[['plain', 'orders.amount']], dir='DESC', limit=1)]}
    record, bindings = request('What is the highest amount?', graph)
    labelled = supervision(raw, record, bindings)
    result = resolve({k: v['label'] for k, v in labelled['gold'].items()}, record, bindings, graph)
    assert result['raw_readings'] == raw
    assert result['readings']['blocks'][0]['select'] == [[0, 'MAX', 'orders.amount', False]]
    assert result['readings']['blocks'][0]['tables'] == ['orders']
    assert 'players' not in result['ace']
    assert decode(result['ace_tree'], graph)[0] == result['readings']
    assert rows_of(render_query(result['query']), ORDERS, PEOPLE) == [(100,)]


def test_ace_rejects_a_share_whose_grouping_the_compiler_would_discard():
    from engine.readers.decisions import request, supervision, resolve, InvalidDecision
    graph = graph_of(ORDERS)
    raw = {'structure': 'single', 'blocks': [dict(block(select=[[0, 'SUM', 'orders.amount', False]],
                  where=[['orders.city', '=', 'Paris']], group=['orders.city']), share=[0])]}
    record, bindings = request('Share of amount for Paris, grouped by city.', graph)
    labelled = supervision(raw, record, bindings)
    try:
        resolve({k: v['label'] for k, v in labelled['gold'].items()}, record, bindings, graph)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('share silently discarded a requested grouping')



def test_joint_decoder_accepts_bounded_api_rounding_and_preserves_its_argmax():
    from engine.readers.decisions import request, supervision, candidates, InvalidDecision
    graph = graph_of(ORDERS)
    reading = {'structure': 'single', 'blocks': [block(select=[[0, 'SUM', 'orders.amount', False]],
                                                     where=[['orders.city', '=', 'Paris']])]}
    record, bindings = request('Total amount for Paris?', graph)
    labelled = supervision(reading, record, bindings)
    answers = {key: {'probabilities': {option: float(option == labelled['gold'][key]['label'])
                                     for option in options}} for key, options in bindings.items()}
    name = 'b0.where0.value'
    lyon = next(k for k, v in bindings[name].items() if v == 'Lyon')
    paris = labelled['gold'][name]['label']
    answers[name]['probabilities'][lyon] = 0.1234
    answers[name]['probabilities'][paris] = 0.8765
    assert candidates(answers, record, bindings, graph, limit=1)[0][1]['readings'] == reading
    answers[name]['probabilities'][lyon] = 0.5
    answers[name]['probabilities'][paris] = 0.5
    answers[name]['choice'] = max(lyon, paris)
    result = candidates(answers, record, bindings, graph, limit=1)[0][1]
    assert result['readings']['blocks'][0]['where'][0][2] == bindings[name][answers[name]['choice']]
    answers[name]['probabilities'][lyon] = 0.8
    answers[name]['probabilities'][paris] = 0.8
    try:
        candidates(answers, record, bindings, graph)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('arbitrary unnormalized probabilities accepted')


def test_joint_decoder_rejects_invalid_asts_and_explores_valid_choices():
    from engine.readers.decisions import request, supervision, resolve, candidates, InvalidDecision
    graph = graph_of(ORDERS)
    record, bindings = request('Count the orders.', graph)
    valid = {'structure': 'single', 'blocks': [block(select=[[0, 'COUNT', '*', False]])]}
    invalid = {'structure': 'single', 'blocks': [block(select=[[0, 'COUNT', '*', True]])]}
    labelled = supervision(invalid, record, bindings)
    picks = {key: item['label'] for key, item in labelled['gold'].items()}
    try:
        resolve(picks, record, bindings, graph)
    except InvalidDecision as exc:
        assert 'COUNT(DISTINCT *)' in str(exc), exc
    else:
        raise AssertionError('invalid AST escaped a successful readings round trip')
    answers = {key: {'probabilities': {option: float(option == picks[key]) for option in options}}
               for key, options in bindings.items()}
    field = 'b0.select0.distinct'
    good = next(option for option, value in bindings[field].items() if value is False)
    answers[field]['probabilities'][picks[field]] = 0.51
    answers[field]['probabilities'][good] = 0.49
    found = candidates(answers, record, bindings, graph, limit=1)
    assert found and found[0][1]['readings'] == valid
    assert rows_of(render_query(found[0][1]['query']), ORDERS) == [(len(ORDERS['rows']),)]
    for aggregate, column in [('SUM', 'orders.city'), ('AVG', 'orders.city')]:
        bad = {'structure': 'single', 'blocks': [block(select=[[0, aggregate, column, False]])]}
        labelled = supervision(bad, record, bindings)
        try:
            resolve({key: item['label'] for key, item in labelled['gold'].items()}, record, bindings, graph)
        except InvalidDecision as exc:
            assert 'numeric column' in str(exc), exc
        else:
            raise AssertionError('text aggregate escaped AST validation')


def test_explicit_ace_joins_preserve_two_different_relationships_between_the_same_tables():
    from engine.readers import ace
    from engine.sql_ast import Join, SelectItem, SelectQuery
    from jsonschema import Draft202012Validator
    people = {'name': 'people', 'columns': ['id', 'name'], 'rows': [[1, 'Ada'], [2, 'Bo']]}
    links = {'name': 'links', 'columns': ['owner_id', 'friend_id'], 'rows': [[1, 2]]}
    graph = graph_of(people, links, fks=[('links', 'friend_id', 'people', 'id'),
                                        ('links', 'owner_id', 'people', 'id')])
    ref = lambda table, col: graph.column_map[table, col].ref
    queries = [SelectQuery((SelectItem(ref('people', 'name')),), 'people',
               joins=(Join('links', ref('people', 'id'), ref('links', key)),)) for key in ('owner_id', 'friend_id')]
    assert readings_of(queries[0]) == readings_of(queries[1])  # table membership alone loses the relationship
    validate = Draft202012Validator(ace.schema()).validate
    for query, expected in zip(queries, [[('Ada',)], [('Bo',)]]):
        reading = readings_of(query, include_joins=True)
        text, document = ace.encode(reading); validate(document)
        assert 'v:uses-left-column "people.id"' in text
        for evidence in (graph, graph_of(people, links)):
            # An explicit relationship does not depend on graph order, data
            # coincidences, or a declared FK being present in imported metadata.
            recovered, rebuilt = ace.decode(document, evidence)
            assert recovered == reading
            assert render_query(rebuilt) == render_query(query)
            assert rows_of(render_query(rebuilt), people, links) == expected


def test_explicit_ace_joins_preserve_left_join_and_every_composite_key_column():
    from engine.readers import ace
    from engine.sql_ast import Join, SelectItem, SelectQuery
    parent = {'name': 'parent', 'columns': ['tenant', 'id', 'name'],
              'rows': [['a', 1, 'Ada'], ['b', 1, 'Bo'], ['c', 2, 'Cy']]}
    child = {'name': 'child', 'columns': ['tenant', 'parent_id', 'amount'],
             'rows': [['a', 1, 5], ['b', 1, 7]]}
    graph = graph_of(parent, child)
    ref = lambda table, col: graph.column_map[table, col].ref
    query = SelectQuery((SelectItem(ref('parent', 'name')), SelectItem(ref('child', 'amount'))), 'parent',
                        joins=(Join('child', ref('parent', 'id'), ref('child', 'parent_id'), kind='LEFT',
                                    additional=((ref('parent', 'tenant'), ref('child', 'tenant')),)),))
    reading = readings_of(query, include_joins=True)
    assert len(reading['blocks'][0]['joins'][0]['pairs']) == 2
    _, document = ace.encode(reading)
    recovered, rebuilt = ace.decode(document, graph)
    assert recovered == reading and render_query(rebuilt) == render_query(query)
    assert rows_of(render_query(rebuilt), parent, child) == [('Ada', 5), ('Bo', 7), ('Cy', None)]


def test_explicit_ace_joins_reject_invalid_scope_and_never_infer_a_replacement_path():
    import copy
    from engine.readers import ace
    from engine.readers.decisions import InvalidDecision
    graph = graph_of(ORDERS, PEOPLE, fks=[('orders', 'city', 'players', 'player')])
    good = {'structure': 'single', 'blocks': [block(tables=['orders', 'players'], root='orders',
              joins=[{'table': 'players', 'kind': 'INNER', 'pairs': [['orders.city', 'players.player']]}],
              select=[[0, 'plain', 'players.player', False]])]}
    for mutation in ('missing-root', 'missing-joins', 'unconnected', 'unknown-column', 'repeated-table'):
        reading = copy.deepcopy(good); b = reading['blocks'][0]
        if mutation == 'missing-root':b.pop('root')
        elif mutation == 'missing-joins':b.pop('joins')
        elif mutation == 'unconnected':b['joins'][0]['pairs'][0] = ['players.player', 'players.college']
        elif mutation == 'unknown-column':b['joins'][0]['pairs'][0][0] = 'orders.absent'
        else:b['joins'].append(copy.deepcopy(b['joins'][0]))
        try:
            _, document = ace.encode(reading); ace.decode(document, graph)
        except InvalidDecision:
            pass
        else:
            raise AssertionError(mutation + ' silently became an inferred join')
    invalid = {'structure': 'single', 'blocks': [block(select=[[0, 'COUNT', '*', True]])]}
    try:
        ace.decode(ace.encode(invalid)[1], graph_of(ORDERS))
    except InvalidDecision as exc:
        assert 'COUNT(DISTINCT *)' in str(exc), exc
    else:
        raise AssertionError('ACE decode exposed an invalid SQL AST')


def test_ace_profile_preserves_source_programs_wider_than_the_neural_head():
    import copy
    from engine.readers import ace
    from engine.readers.decisions import BOUNDS, InvalidDecision
    from engine.sql_ast import BooleanExpr, Comparison, Literal, OrderTerm, SelectItem, SelectQuery
    from jsonschema import Draft202012Validator
    table = {'name': 'measurements', 'columns': [f'c{i}' for i in range(9)],
             'rows': [[i + 1 for i in range(9)], [i + 11 for i in range(9)]]}
    graph = graph_of(table)
    refs = [graph.column_map['measurements', name].ref for name in table['columns']]
    query = SelectQuery(tuple(SelectItem(ref) for ref in refs), 'measurements',
                        where=BooleanExpr('AND', tuple(Comparison(ref, '>', Literal(0)) for ref in refs[:7])),
                        order_by=tuple(OrderTerm(ref, 'ASC') for ref in refs[:3]))
    reading = readings_of(query, include_joins=True)
    assert len(reading['blocks'][0]['select']) > BOUNDS['select']
    assert len(reading['blocks'][0]['where']) > BOUNDS['where']
    assert len(reading['blocks'][0]['order']) > BOUNDS['order']
    _, document = ace.encode(reading)
    Draft202012Validator(ace.schema()).validate(document)
    recovered, rebuilt = ace.decode(document, graph)
    assert recovered == reading and render_query(rebuilt) == render_query(query)
    assert rows_of(render_query(rebuilt), table) == rows_of(render_query(query), table)
    for name in ('select', 'where', 'group', 'order'):
        excessive = copy.deepcopy(reading)
        values = excessive['blocks'][0][name]
        values[:] = [copy.deepcopy(values[0] if values else 'measurements.c0')
                     for _ in range(ace.ACE_BOUNDS[name] + 1)]
        try:
            ace.encode(excessive)
        except InvalidDecision as exc:
            assert 'bound exceeded: ' + name in str(exc), exc
        else:
            raise AssertionError('unbounded ACE ' + name)


def test_ace_encoder_enforces_its_literal_document_and_variable_bounds():
    import copy
    from engine.readers import ace
    from engine.readers.decisions import InvalidDecision
    from jsonschema import Draft202012Validator
    good = {'structure': 'single', 'blocks': [block(select=[[0, 'COUNT', '*', False]],
             where=[['orders.city', '=', 'x' * 4096]])]}
    _, document = ace.encode(good)
    Draft202012Validator(ace.schema()).validate(document)
    for mutation in ('overlong', 'surrogate', 'oversized', 'blocks'):
        reading = copy.deepcopy(good)
        if mutation == 'overlong':reading['blocks'][0]['where'][0][-1] += 'x'
        elif mutation == 'surrogate':reading['blocks'][0]['where'][0][-1] = '\ud800'
        elif mutation == 'oversized':reading['blocks'][0]['tables'] = ['orders'] * ace.MAX_SENTENCES
        else:reading['blocks'] *= 3
        try:
            ace.encode(reading)
        except InvalidDecision:
            pass
        else:
            raise AssertionError('ACE encoder exposed an invalid schema instance: ' + mutation)


def test_joint_score_validation_preserves_logits_before_a_grammar_mask():
    from engine.readers.decisions import checked_scores, InvalidDecision
    binding = {'field': {'illegal': 0, 'legal': 1, 'other_legal': 2}}
    answer = {'field': {'choice': 'illegal', 'logits': {'illegal': 0.0, 'legal': -1000.0, 'other_legal': -1001.0}}}
    options, scores = checked_scores(answer, binding)
    assert options['field'] == ['illegal', 'legal', 'other_legal']
    assert scores['field']['legal'] == -1000.0, 'do not underflow a legal logit through global softmax'
    for mutation in ('nan', 'missing', 'wrong_argmax', 'mixed'):
        bad = __import__('copy').deepcopy(answer)
        if mutation == 'nan':bad['field']['logits']['legal'] = float('nan')
        elif mutation == 'missing':bad['field']['logits'].pop('legal')
        elif mutation == 'wrong_argmax':bad['field']['choice'] = 'legal'
        else:bad['field']['probabilities'] = {'illegal': 1.0, 'legal': 0.0, 'other_legal': 0.0}
        try:checked_scores(bad, binding)
        except InvalidDecision:pass
        else:raise AssertionError('invalid native score contract admitted: ' + mutation)


def test_named_binding_supplies_an_input_choice_to_the_existing_compiler():
    from engine.readers.decisions import named_choice, request, resolve, supervision
    from engine.schema_decode import BindingBasis, BindingPolicy, BindingProfile, replay_binding

    graph = graph_of(ORDERS)
    record, bindings = request("Total amount", graph)
    reading = {"structure": "single", "blocks": [block(select=[[0, "SUM", "orders.amount", False]])]}
    gold = supervision(reading, record, bindings)
    answers = {key: value["label"] for key, value in gold["gold"].items()}
    basis = BindingBasis(("https://schema.org/currency", "https://schema.org/name"), (1, 1),
                         (True, True), "compiler-mechanism-fixture-not-serving-calibration", 8,
                         (("noun", (True, True)), ("column", (True, True))))
    q = BindingProfile(basis.identity, "question/amount", "noun", (8, 0), "expected_concept")
    options = {key: value for key, value in bindings["b0.select0.column"].items()
               if value in ("orders.amount", "orders.city")}
    profiles = {key: BindingProfile(basis.identity, value, "column",
                                   (8, 0) if value == "orders.amount" else (0, 8), "expected_concept")
                for key, value in options.items()}
    named = named_choice(basis, q, options, profiles, BindingPolicy((1, 2), (1, 1), (1, 10)),
                         candidate_kind="column")
    assert replay_binding(basis.record(), named["binding"]) == named["binding"]
    answers["b0.select0.column"] = named["choice"]
    result = resolve(answers, record, bindings, graph)
    assert rows_of(render_query(result["query"]), ORDERS) == [(207,)]
    assert result["readings"] == reading


def test_named_operator_coordinates_causally_change_the_existing_compiler_and_execution():
    import copy
    from engine.readers.decisions import (InvalidDecision, NamedDecisionPolicy, named_operator_choice,
                                         replay_named_operator, request, resolve, supervision)
    from engine.readers.layout import named_coordinate_contract
    from engine.schema_decode import BindingBasis, BindingProfile
    from training.readers.gold import semantic_operator_labels

    contract = named_coordinate_contract()
    names = tuple(r["name"] for r in contract["coordinates"])
    aggregate_names = {r["name"] for r in contract["coordinates"] if r.get("selector") and
                       r["selector"]["node"] == "Aggregate" and r["selector"]["field"] == "function"}
    mask = tuple(n in aggregate_names for n in names)
    basis = BindingBasis(names, (1,) * len(names), mask, "operator-mechanism-fixture-not-calibration", 16,
                         (("operation", mask),))
    graph = graph_of(ORDERS)
    record, bindings = request("Total amount", graph)
    reading = {"structure": "single", "blocks": [block(select=[[0, "SUM", "orders.amount", False]])]}
    gold = supervision(reading, record, bindings)
    answers = {k: v["label"] for k, v in gold["gold"].items()}
    options = bindings["b0.select0.aggregate"]
    policy = NamedDecisionPolicy((9, 10), (1, 10))
    results = {}
    for operation, coordinate in (("SUM", "intent_agg_sum"), ("AVG", "intent_agg_avg"), ("plain", None)):
        values = tuple(16 if name == coordinate else 0 if name in aggregate_names else None for name in names)
        profile = BindingProfile(basis.identity, "projection/0/aggregate", "operation", values, "expected_concept")
        chosen = named_operator_choice(basis, profile, options, policy, node="Aggregate", field="function",
                                       allow_zero=True, zero_value="plain")
        assert options[chosen["choice"]] == operation
        assert replay_named_operator(chosen) == chosen
        for candidate in chosen["candidates"]:
            assert sum(t["contribution"] for t in candidate["contributions"]) == candidate["numerator"]
        altered = copy.deepcopy(chosen)
        altered["candidates"][0]["contributions"][0]["contribution"] += 1
        try: replay_named_operator(altered)
        except InvalidDecision: pass
        else: raise AssertionError("unaccounted operator contribution was accepted")
        answers["b0.select0.aggregate"] = chosen["choice"]
        results[operation] = resolve(answers, record, bindings, graph)
        if operation != "plain":
            labels = semantic_operator_labels(results[operation]["query"])["operations"]
            assert any(r["value"] == operation and r["labels"][coordinate] for r in labels)
    assert rows_of(render_query(results["SUM"]["query"]), ORDERS) == [(207,)]
    assert rows_of(render_query(results["AVG"]["query"]), ORDERS) == [(51.75,)]
    assert len(rows_of(render_query(results["plain"]["query"]), ORDERS)) == 4
    for mutation in ("unknown", "unqualified", "ambiguous"):
        values = tuple(8 if mutation == "ambiguous" and n in aggregate_names else
                       16 if n == "intent_agg_sum" else 0 if n in aggregate_names else None for n in names)
        changed_basis = basis
        if mutation == "unknown": values = tuple(None if n == "intent_agg_avg" else v for n, v in zip(names, values))
        if mutation == "unqualified":
            qualified = tuple(ok and n != "intent_agg_avg" for n, ok in zip(names, mask))
            changed_basis = BindingBasis(names, basis.weights, qualified, "incomplete-operator-evidence", 16,
                                         (("operation", qualified),))
        profile = BindingProfile(changed_basis.identity, "projection/0/aggregate", "operation", values, "expected_concept")
        assert named_operator_choice(changed_basis, profile, options, policy, node="Aggregate", field="function",
                                     allow_zero=True, zero_value="plain")["choice"] is None


def test_scoped_operator_labels_preserve_default_directions_and_distinct_occurrences():
    from engine.sql_ast import Aggregate, ColumnRef, Comparison, Literal, OrderTerm, SelectItem, SelectQuery, SQLType, Star
    from training.readers.gold import semantic_operator_labels
    from training.readers.examples import semantic_example

    c = ColumnRef("t", "v", SQLType.INTEGER)
    query = SelectQuery((SelectItem(Aggregate("SUM", c)), SelectItem(Aggregate("AVG", c))), "t",
                        order_by=(OrderTerm(Aggregate("SUM", c)),))
    operations = semantic_operator_labels(query)["operations"]
    assert [(r["path"], r["value"]) for r in operations] == [
        ("$.select[0].expression.function", "SUM"), ("$.select[1].expression.function", "AVG"),
        ("$.order_by[0].direction", "ASC"), ("$.order_by[0].expression.function", "SUM")]
    assert operations[0]["labels"]["intent_agg_sum"] == 1 and operations[0]["labels"]["intent_agg_avg"] == 0
    assert operations[1]["labels"]["intent_agg_sum"] == 0 and operations[1]["labels"]["intent_agg_avg"] == 1
    assert operations[2]["labels"]["intent_sort_asc"] == 1
    grouped = SelectQuery((SelectItem(c),), "t", group_by=(c,), having=Comparison(Aggregate("COUNT", Star()), ">", Literal(2)))
    state = {"schema": [{"table": "t", "column": "v", "type": "number"}],
             "conversation": [{"role": "user", "content": "values with more than two records"}]}
    example = semantic_example(state, identity="having", source="train_spider", db_id="t", split="train", query=grouped)
    target = example["targets"][next(i for i, item in enumerate(example["items"]) if item["identity"] == "b0.having0.operator")]
    assert target["sql.Comparison.operator.greater"] == 1


def test_scoped_shared_head_prefill_and_targets_keep_inactive_operations_unknown():
    from types import SimpleNamespace
    import torch
    from engine.readers.decisions import NamedDecisionPolicy, named_operator_answers, named_operator_requests
    from engine.readers.layout import named_coordinate_contract
    from engine.readers.models import NamedSemanticModel, semantic_encode
    from engine.schema_decode import BindingBasis
    from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType
    from training.readers.examples import semantic_example
    from training.readers.train import masked_semantic_loss, semantic_batch

    class Tokenizer:
        pad_token_id = 0
        def __call__(self, text, **kwargs): return SimpleNamespace(input_ids=[ord(c) for c in text])
    class Encoder(torch.nn.Module):
        config = SimpleNamespace(hidden_size=2)
        def __init__(self): super().__init__(); self.calls = 0
        def forward(self, input_ids, attention_mask):
            self.calls += 1
            return SimpleNamespace(last_hidden_state=torch.zeros((*input_ids.shape, 2)))

    state = {"schema": [{"table": "t", "column": "v", "type": "number"}],
             "conversation": [{"role": "user", "content": "the requested amount"}]}
    rows = []
    for operation in ("SUM", "AVG"):
        query = SelectQuery((SelectItem(Aggregate(operation, ColumnRef("t", "v", SQLType.REAL))),), "t")
        rows.append(semantic_example(state, identity=operation, source="train_spider", db_id="t", split="train", query=query))
    assert rows[0]["input"] == rows[1]["input"] and rows[0]["items"] == rows[1]["items"]
    slot = next(i for i, item in enumerate(rows[0]["items"]) if item["identity"] == "b0.select0.aggregate")
    unused = next(i for i, item in enumerate(rows[0]["items"]) if item["identity"] == "b1.select0.aggregate")
    assert rows[0]["targets"][slot]["intent_agg_sum"] == 1 and rows[1]["targets"][slot]["intent_agg_sum"] == 0
    assert rows[0]["targets"][unused] == rows[1]["targets"][unused] == {}
    contract = named_coordinate_contract()
    names = tuple(r["name"] for r in contract["coordinates"])
    qualified = tuple(r["block"] == "sql" for r in contract["coordinates"])
    basis = BindingBasis(names, (1,) * len(names), qualified, "scoped-inference-mechanism-fixture", 16,
                         (("operation", qualified),))
    tokenizer, encoder = Tokenizer(), Encoder()
    model = NamedSemanticModel(encoder, basis).eval()
    with torch.no_grad():
        model.head.projection.weight.zero_(); model.head.projection.bias.fill_(-30)
        model.head.projection.bias[names.index("intent_agg_sum")] = 30
    items = [r["item"] for r in named_operator_requests().values()]
    profiles = model.read(tokenizer, state, items)
    assert encoder.calls == 1
    decisions = named_operator_answers(basis, {p.identity: p for p in profiles}, NamedDecisionPolicy((9, 10), (1, 10)))
    answer = decisions["b0.select0.aggregate"]
    assert answer["options"][answer["choice"]] == "SUM"
    assert decisions["b0.conj"]["choice"] is None
    encoded = [semantic_encode(tokenizer, r["input"], r["items"]) for r in rows]
    tensors = semantic_batch(rows, encoded, tokenizer, names)
    assert not tensors["observed"][:, unused].any()
    logits = torch.zeros_like(tensors["targets"], requires_grad=True)
    masked_semantic_loss(logits, tensors["targets"], tensors["observed"]).backward()
    assert not logits.grad[:, unused].any()


def test_named_binding_cannot_fill_an_ambiguous_choice_or_omit_option_evidence():
    from engine.readers.decisions import InvalidDecision, named_choice
    from engine.schema_decode import BindingBasis, BindingPolicy, BindingProfile

    basis = BindingBasis(("https://schema.org/name",), (1,), (True,), "identity-mechanism-fixture", 8,
                         (("noun", (True,)), ("column", (True,))))
    query = BindingProfile(basis.identity, "question/city", "noun", (8,), "expected_concept")
    options = {"o1": "departure_city", "o2": "arrival_city"}
    profiles = {key: BindingProfile(basis.identity, value, "column", (8,), "expected_concept")
                for key, value in options.items()}
    policy = BindingPolicy((1, 2), (1, 1), (1, 10))
    result = named_choice(basis, query, options, profiles, policy, candidate_kind="column")
    assert result["choice"] is None and result["binding"]["reason"] == "ambiguous_binding"
    try:
        named_choice(basis, query, options, {"o1": profiles["o1"]}, policy, candidate_kind="column")
    except InvalidDecision:
        pass
    else:
        raise AssertionError("candidate pruning must not be hidden as missing profile evidence")


def test_named_head_has_no_hidden_residual_path_to_binding():
    import torch
    from engine.readers.models import NamedSemanticHead
    from engine.schema_decode import BindingBasis, BindingPolicy, bind_profiles

    basis = BindingBasis(("https://schema.org/address", "https://schema.org/geo"),
                         (1, 1), (True, True), "neural-mechanism-fixture", 8,
                         (("noun", (True, True)), ("column", (True, True))))
    head = NamedSemanticHead(4, basis)
    with torch.no_grad():
        head.projection.weight.copy_(torch.tensor([[1., 0., 0., 0.], [0., 1., 0., 0.]]))
        head.projection.bias.zero_()
    states = torch.tensor([[9., 9., 0., 0.], [9., 9., 1., 2.], [-9., -9., 3., 4.]])
    args = {"identities": ("question/city", "places/cities", "people/names"),
            "kinds": ("noun", "column", "column"), "evidence_kinds": ("expected_concept",) * 3}
    profiles = head.profiles(states, **args)
    changed = states.clone()
    changed[:, 2:] = torch.tensor([[200., -300.], [-600., 700.], [800., -900.]])
    assert not torch.equal(changed, states)
    after = head.profiles(changed, **args)
    assert profiles == after
    policy = BindingPolicy((1, 2), (1, 1), (1, 10))
    before_choice = bind_profiles(basis, profiles[0], profiles[1:], policy, candidate_kind="column")
    after_choice = bind_profiles(basis, after[0], after[1:], policy, candidate_kind="column")
    assert before_choice == after_choice and before_choice["selected"] == "places/cities"


def test_named_head_exports_only_coordinates_calibrated_for_each_role():
    import torch
    from engine.readers.models import NamedSemanticHead
    from engine.schema_decode import BindingBasis

    basis = BindingBasis(("https://schema.org/geo", "sql.project"), (1, 1), (True, True), "role-head", 8,
                         (("question", (False, True)), ("column", (True, False))))
    head = NamedSemanticHead(2, basis)
    with torch.no_grad():
        head.projection.weight.zero_()
        head.projection.bias.fill_(9)
    profiles = head.profiles(torch.zeros(3, 2), identities=("query", "city-column", "paris"),
                             kinds=("question", "column", "value"), evidence_kinds=("expected_concept",) * 3)
    assert [p.values for p in profiles] == [(None, 8), (8, None), (None, None)]


def test_named_head_masks_unqualified_and_unobserved_predictions():
    import torch
    from engine.readers.models import NamedSemanticHead
    from engine.schema_decode import BindingBasis

    basis = BindingBasis(("a", "b", "c"), (1, 1, 1), (True, False, True), "mask-fixture", 8,
                         (("value", (True, False, True)),))
    head = NamedSemanticHead(2, basis)
    result = head.profiles(torch.zeros(1, 2), identities=("source/value",), kinds=("value",),
                           evidence_kinds=("expected_concept",),
                           evidence_mask=torch.tensor([[True, True, False]]))
    assert result[0].values[0] is not None
    assert result[0].values[1:] == (None, None)
    try:
        head.profiles(torch.zeros(1, 2), identities=("source/value",), kinds=("value",),
                      evidence_kinds=("observed_instance",))
    except ValueError as error:
        assert "observed instance facts" in str(error)
    else:
        raise AssertionError("a predicted meaning profile was labelled as an observed fact")
    try:
        head.profiles(torch.full((1, 2), float("nan")), identities=("x",), kinds=("value",),
                      evidence_kinds=("expected_concept",))
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite semantic predictions must fail closed")


def test_semantic_training_masks_unknown_labels_in_loss_and_gradient():
    import torch
    from training.readers.train import masked_semantic_loss

    logits = torch.zeros((2, 3), requires_grad=True)
    targets = torch.tensor([[1., float("nan"), 0.], [0., float("nan"), 1.]])
    mask = torch.tensor([[True, False, True], [True, False, True]])
    loss = masked_semantic_loss(logits, targets, mask)
    assert torch.isfinite(loss)
    loss.backward()
    assert torch.equal(logits.grad[:, 1], torch.zeros(2))
    assert (logits.grad[:, 0] != 0).all() and (logits.grad[:, 2] != 0).all()
    try:
        masked_semantic_loss(logits, targets, torch.zeros_like(mask))
    except ValueError:
        pass
    else:
        raise AssertionError("an unlabelled batch must not produce a successful training step")


def test_named_coordinate_contract_covers_215_explicit_migrated_targets():
    from engine.readers.layout import named_coordinate_contract
    from engine.schema_org import load_contract

    contract = named_coordinate_contract()
    assert contract == named_coordinate_contract()
    assert contract["blocks"] == {"property": 71, "sql": 64, "ace_frame": 32,
                                  "structure_type_role": 32, "dialogue": 16}
    rows = contract["coordinates"]
    assert len(rows) == len({r["name"] for r in rows}) == 215
    assert all(r["meaning"] and not r["calibrated"] for r in rows)
    ontology = load_contract()
    assert all(ontology.property(r["name"]) for r in rows if r["block"] == "property")
    migrated = [r for r in rows if r.get("migration")]
    assert {r["legacy_name"] for r in migrated} == {"GeoCoordinates", "taxonName"}
    assert all(r["inherited_index"] is None for r in migrated)
    assert not any(r["name"].startswith("ast.node.") for r in rows)


def test_semantic_prefill_static_profiles_cannot_read_the_question_or_physical_ids():
    from types import SimpleNamespace
    from engine.readers.models import semantic_encode

    class Tokenizer:
        def __call__(self, text, **kwargs):
            return SimpleNamespace(input_ids=[ord(c) for c in text])

    tokenizer = Tokenizer()
    items = [{"identity": "physical-secret-A", "kind": "column", "text": "city | text | Paris, Oslo"},
             {"identity": "q", "kind": "question", "text": "Read the complete requested operations."}]
    state = {"schema": [{"column": "cities", "samples": ["Paris", "Oslo"]}],
             "conversation": [{"role": "user", "content": "How many cities?"}]}
    first, spans = semantic_encode(tokenizer, state, items)
    state["conversation"][0]["content"] = "Show cities starting with O instead."
    second, other_spans = semantic_encode(tokenizer, state, items)
    end = max(spans[0]) + 1
    assert spans[0] == other_spans[0] and first[:end] == second[:end]
    assert min(spans[1]) > max(spans[0]) and first != second
    assert "physical-secret-A" not in "".join(map(chr, first))
    items[0]["identity"] = "totally-different-physical-id"
    assert semantic_encode(tokenizer, state, items)[0] == second
    try:
        semantic_encode(tokenizer, state, items, max_len=10)
    except ValueError:
        pass
    else:
        raise AssertionError("an oversized semantic state must refuse, not truncate")
    items[0]["targets"] = {"chosen": 1}
    try:
        semantic_encode(tokenizer, state, items)
    except ValueError:
        pass
    else:
        raise AssertionError("target annotations must never become model input")


def test_sql_semantic_labels_keep_complete_sql_flags_and_scoped_occurrences():
    from engine.sql_ast import (Aggregate, ColumnRef, Comparison, Join, Literal, OrderTerm,
                                SelectItem, SelectQuery, SQLType)
    from training.readers.gold import semantic_labels

    amount = ColumnRef("sale", "amount", SQLType.REAL)
    key = ColumnRef("sale", "customer")
    query = SelectQuery(
        select=(SelectItem(key), SelectItem(Aggregate("SUM", amount, distinct=True))),
        from_table="sale",
        joins=(Join("customer", key, ColumnRef("customer", "id"), kind="LEFT",
                    additional=((ColumnRef("sale", "region"), ColumnRef("customer", "region")),)),),
        where=Comparison(amount, ">=", Literal(10)), group_by=(key,),
        order_by=(OrderTerm(Aggregate("SUM", amount), "DESC"),), limit=2, with_ties=True)
    result = semantic_labels(query)
    labels = result["labels"]
    assert len(labels) == 64 and all(v in (0, 1) for v in labels.values())
    for name in ("intent_agg_sum", "intent_filter_gt", "intent_group", "intent_limit", "intent_sort_desc",
                 "sql.join", "sql.join_composite_key", "sql.Join.kind.LEFT", "sql.sum_distinct", "sql.with_ties"):
        assert labels[name], name
    assert not labels["intent_filter_eq"]  # equality in a join is not a user filter
    roles = {r["path"]: r["roles"] for r in result["arguments"]}
    assert roles["$.where.left"] == ["role.filter_attribute"]
    assert roles["$.joins[0].additional[0][1]"] == ["role.join_right"]
    assert roles["$.select[0].expression"] == ["role.projection"]
    assert roles["$.group_by[0]"] == ["role.group_key"]
    assert not any(name.startswith("https://schema.org/") or name.startswith("ace.") for name in labels)


def test_sql_semantic_labels_restore_canonical_default_direction_and_join_kind():
    from engine.sql_ast import ColumnRef, Join, OrderTerm, SelectItem, SelectQuery, query_to_tree
    from training.readers.gold import semantic_labels

    value = ColumnRef("a", "value")
    query = SelectQuery((SelectItem(value),), "a", joins=(Join("b", ColumnRef("a", "id"), ColumnRef("b", "id")),),
                        order_by=(OrderTerm(value),))
    tree = query_to_tree(query)
    assert "kind" not in tree["joins"][0] and "direction" not in tree["order_by"][0]
    labels = semantic_labels(query)["labels"]
    assert labels["intent_sort_asc"] == labels["sql.Join.kind.INNER"] == 1
    assert labels["intent_sort_desc"] == labels["sql.Join.kind.LEFT"] == 0


def test_sql_semantic_labels_distinguish_nulls_counts_and_nested_scope():
    from engine.sql_ast import (Aggregate, ColumnRef, Comparison, ExistsPredicate, Literal,
                                SelectItem, SelectQuery, Star)
    from training.readers.gold import semantic_labels

    column = ColumnRef("t", "value")
    for operand, distinct, facet in ((Star(), False, "count_rows"), (column, False, "count_non_null"),
                                     (column, True, "count_distinct")):
        inner = SelectQuery((SelectItem(Aggregate("COUNT", operand, distinct)),), "t",
                            where=Comparison(column, "IS NOT", Literal(None)))
        outer = SelectQuery((SelectItem(column),), "t", where=ExistsPredicate(inner, negated=True))
        result = semantic_labels(outer)
        for name in ("sql." + facet, "sql.not_exists", "sql.is_not_null", "sql.Comparison.operator.IS_NOT"):
            assert result["labels"][name], name
        others = {"count_rows", "count_non_null", "count_distinct"} - {facet}
        assert all(not result["labels"]["sql." + name] for name in others)
        assert any(r["path"] == "$.where.query.where.left" for r in result["arguments"])


def test_qwen_unit_program_uses_existing_importer_and_rejects_untyped_filter_values():
    import copy
    import sqlite3
    from training.readers.gold import read_unit_program, semantic_labels
    from regress.sql_import import Unsupported
    from engine.sql_ast import query_to_tree, query_from_tree, render_query

    def token(kind, text, clause, col=-1):
        return {"kind": kind, "text": text, "clause": clause, "col": col}
    pair = {"schema_units": [{"kind": "name", "text": "values", "col": 0}],
            "sql_units": [token("kw", "SELECT", "proj"), token("punc", "*", "proj"),
                          token("kw", "FROM", "source"), token("table", "order", "source"),
                          token("kw", "ORDER", "order"), token("kw", "BY", "order"),
                          token("field", "values", "order", 0), token("kw", "ASC", "order"),
                          token("kw", "LIMIT", "limit"), token("literal", "3", "limit")]}
    sql, graph, query = read_unit_program(pair)
    assert graph.tables == ("order",) and '"values"' in sql and '"order"' in sql
    assert query_from_tree(query_to_tree(query)) == query and semantic_labels(query)["labels"]["intent_sort_asc"] == 1
    with sqlite3.connect(":memory:") as connection:
        connection.execute('CREATE TABLE "order" ("values")')
        connection.executemany('INSERT INTO "order" VALUES (?)', [(7,), (None,), (2,), (2,), (9,)])
        assert connection.execute(sql).fetchall() == connection.execute(render_query(query)).fetchall()
    for text in ("001", "Paris", "O'Brien", "True"):
        altered = copy.deepcopy(pair)
        altered["sql_units"][-1] = token("literal", text, "filter", 0)
        try:
            read_unit_program(altered)
        except Unsupported as error:
            assert str(error) == "source-filter-literal-type-unknown"
        else:
            raise AssertionError("source filter literal type was guessed")
    duplicate = copy.deepcopy(pair)
    duplicate["schema_units"].extend([{"kind": "name", "text": "mysql", "col": 1},
                                      {"kind": "name", "text": "MySQL", "col": 2}])
    _, normalized_graph, _ = read_unit_program(duplicate)
    assert [c.ref.name for c in normalized_graph.columns] == [
        "values", "mysql [column B]", "MySQL [column C]"]


def test_qwen_original_source_restores_types_only_after_exact_pair_verification():
    import copy
    import sqlite3
    from engine.sql_ast import SQLType, render_query
    from training.readers.gold import read_unit_program
    from training.readers.sources import unit_graph_pairs
    from regress.sql_import import Unsupported

    for raw, spelling in (("001", "'001'"), ("O'Brien", "'O''Brien'"), ("-2", "-2")):
        schema = [{"group": "schema", "kind": "name", "text": "value", "col": 0}]
        question = [{"group": "q", "kind": "q", "text": "find", "link": None, "fired": []}]
        specifications = [("SELECT", "kw", "proj", None, None), ("*", "punc", "proj", None, None),
                          ("FROM", "kw", "source", None, None), ('"rows"', "table", "source", None, "rows"),
                          ("WHERE", "kw", "filter", None, None), ('"value"', "field", "filter", 0, "value"),
                          ("=", "op", "filter", None, None), (spelling, "literal", "filter", 0, raw)]
        tokens = [{"text": t, "srole": k, "clause": c, "col": i, "raw": v} for t, k, c, i, v in specifications]
        graph_tokens = [{"group": "sql", "text": str(u["raw"]) if u["raw"] is not None else u["text"],
                         "kind": u["srole"], "clause": u["clause"], "col": -1 if u["col"] is None else u["col"]}
                        for u in tokens]
        pair, = unit_graph_pairs([{"file": "test", "task": "intent", "units": schema + question},
                                  {"file": "test", "task": "role", "units": schema + graph_tokens}])
        source = {"file": "test", "question": "find", "q_units": [{"text": "find", "link": None, "intent": []}],
                  "sql_units": tokens, "sql": " ".join(u["text"] for u in tokens)}
        sql, graph, query = read_unit_program(pair, source_record=source, column_types={0: SQLType.TEXT})
        assert spelling in sql and graph.columns[0].ref.type == SQLType.TEXT
        with sqlite3.connect(":memory:") as connection:
            connection.execute('CREATE TABLE "rows" ("value" TEXT)')
            connection.executemany('INSERT INTO "rows" VALUES (?)', [("001",), ("O'Brien",), ("-2",), (None,)])
            assert connection.execute(sql).fetchall() == connection.execute(render_query(query)).fetchall()
        limited_pair, limited_source = copy.deepcopy(pair), copy.deepcopy(source)
        for text, kind in (("LIMIT", "kw"), ("2", "literal")):
            limited_pair["sql_units"].append({"group": "sql", "text": text, "kind": kind, "clause": "limit", "col": -1})
            limited_source["sql_units"].append({"text": text, "srole": kind, "clause": "limit", "col": None, "raw": None})
        limited_source["sql"] = " ".join(u["text"] for u in limited_source["sql_units"])
        assert read_unit_program(limited_pair, source_record=limited_source)[2].limit == 2
        for mutation in ("metadata", "question", "link", "sql", "pointer", "identifier", "literal", "multiple", "numeric"):
            altered = copy.deepcopy(source)
            if mutation == "metadata": altered["file"] = "other"
            elif mutation == "question": altered["question"] = "different"
            elif mutation == "link": altered["q_units"][0]["link"] = 0
            elif mutation == "sql": altered["sql"] += " LIMIT 1"
            elif mutation == "pointer": altered["sql_units"][5]["col"] = 1
            else:
                if mutation == "identifier": altered["sql_units"][5]["text"] = '"different"'
                else: altered["sql_units"][-1]["text"] = {"literal": "'different'", "multiple": "1; SELECT 2", "numeric": "3"}[mutation]
                altered["sql"] = " ".join(u["text"] for u in altered["sql_units"])
            try:
                read_unit_program(pair, source_record=altered)
            except (ValueError, Unsupported):
                pass
            else:
                raise AssertionError("unmatched/invalid original source accepted: " + mutation)


def test_qwen_graph_source_recovery_pairs_sql_units_and_preserves_literal_identity():
    import copy
    from training.readers.sources import unit_graph_pairs

    schema = [{"group": "schema", "kind": "name", "text": "city", "table": "places",
               "colname": "city", "col": 0}]
    question = [{"group": "q", "kind": "q", "text": "in", "link": None},
                {"group": "q", "kind": "q", "text": "O'Brien", "link": ["places", "city"]}]
    program = [{"group": "sql", "kind": "kw", "text": "SELECT", "clause": "proj", "col": -1},
               {"group": "sql", "kind": "field", "text": "city", "clause": "filter",
                "table": "places", "colname": "city", "col": 0},
               {"group": "sql", "kind": "literal", "text": "O'Brien", "clause": "filter",
                "table": "places", "colname": "city", "col": 0}]
    rows = [{"file": "source", "task": "intent", "units": schema + question},
            {"file": "source", "task": "role", "units": copy.deepcopy(schema) + program}]
    original = copy.deepcopy(rows)
    recovered, = unit_graph_pairs(rows)
    assert rows == original and recovered["source_lines"] == [1, 2]
    assert recovered["question"] == "in O'Brien" and recovered["question_spans"] == [[0, 2], [3, 10]]
    link, = recovered["alignments"]
    assert recovered["question"][slice(*link["span"])] == "O'Brien"
    assert link["column"]["name"] == "city" and link["literal_type"] == "unknown"
    assert any(x["kind"] == "literal" and x["sql_token"] == 2 for x in link["sql_occurrences"])
    assert recovered["sql_units"][-1]["text"] == "O'Brien" and not recovered["admitted_for_training"]
    broken = []
    for variant in ("metadata", "schema", "pointer", "unpaired"):
        altered = copy.deepcopy(rows)
        if variant == "metadata": altered[1]["file"] = "different"
        elif variant == "schema": altered[1]["units"][0]["text"] = "other"
        elif variant == "pointer": altered[0]["units"][-1]["link"] = ["places", "missing"]
        else: altered = altered[:1]
        broken.append(altered)
    for altered in broken:
        try:
            list(unit_graph_pairs(altered))
        except ValueError:
            pass
        else:
            raise AssertionError("unverified Qwen companion/link accepted")


def test_generated_dialogue_annotations_check_program_deltas_and_do_not_infer_unknown_controls():
    import copy
    from dataclasses import replace
    from engine.readers.layout import named_coordinate_contract
    from engine.sql_ast import ColumnRef, Literal, Comparison, OrderTerm, SelectItem, SelectQuery
    from engine.readers.readings import readings_of
    from training.readers.examples import generated_dialogue_controls, semantic_example

    contract = named_coordinate_contract()
    column = ColumnRef("t", "amount")
    before = SelectQuery((SelectItem(column),), "t", order_by=(OrderTerm(column, "DESC"),), limit=3)
    after = replace(before, order_by=(OrderTerm(column, "ASC"),))
    turns = [{"role": "user", "content": "Show the three largest amounts."},
             {"role": "assistant", "content": "I will use the uploaded tables."},
             {"role": "user", "content": "Actually, show the lowest first instead, and only the first three."}]
    row = {"variant": "correction", "state": {"schema": [], "conversation": turns},
           "target_readings": readings_of(after)}
    previous = {"variant": "single", "state": {"schema": [], "conversation": turns[:1]},
                "target_readings": readings_of(before)}
    controls = generated_dialogue_controls(row, after, previous, contract)
    assert {k for k, v in controls.items() if v} == {
        "dialogue.refer_to_previous_question", "dialogue.replace_order", "dialogue.replace_limit"}
    example = semantic_example(row["state"], identity="test-annotation", source="train_spider", db_id="t",
                               split="train", query=after, dialogue_controls=controls)
    assert all("dialogue." + act in example["targets"][0] for act in ("new_question", "replace_limit", "add_filter"))
    # A declared correction cannot silently add a filter or alter another branch.
    bad = replace(after, where=Comparison(column, ">", Literal(10)))
    for changed, variant in ((bad, "correction"), (after, "reference"), (after, "unknown")):
        candidate = copy.deepcopy(row); candidate["variant"] = variant
        candidate["target_readings"] = readings_of(changed)
        try:
            generated_dialogue_controls(candidate, changed, previous, contract)
        except ValueError:
            pass
        else:
            raise AssertionError("unverified conversation annotation was accepted")
    reference = copy.deepcopy(row); reference["variant"] = "reference"
    reference["state"]["conversation"][-1]["content"] = "Please answer that question."
    reference["target_readings"] = readings_of(before)
    controls = generated_dialogue_controls(reference, before, previous, contract)
    assert sum(controls.values()) == controls["dialogue.refer_to_previous_question"] == 1
    assert controls["dialogue.refer_to_previous_result"] == 0
    # Question-derived sample anchors may change; the actual schema must not.
    previous["state"]["schema"] = [{"column": "t.name", "type": "text", "samples": ["David"]}]
    reference["state"]["schema"] = [{"column": "t.name", "type": "text", "samples": ["Will", "David"]}]
    assert generated_dialogue_controls(reference, before, previous, contract) == controls
    reference["state"]["schema"][0]["column"] = "other.name"
    try:
        generated_dialogue_controls(reference, before, previous, contract)
    except ValueError:
        pass
    else:
        raise AssertionError("changed schema accepted as reference-only dialogue")
    # Ordinary questions retain unknown controls unless explicitly annotated.
    ordinary = semantic_example(row["state"], identity="ordinary", source="train_spider", db_id="t",
                                split="train", query=before)
    assert "dialogue.replace_limit" not in ordinary["targets"][0]


def test_semantic_examples_and_batches_keep_gold_and_unknowns_outside_inputs():
    from types import SimpleNamespace
    import torch
    from engine.readers.models import semantic_encode
    from engine.sql_ast import Aggregate, SelectItem, SelectQuery, Star
    from training.readers.examples import semantic_example
    from training.readers.train import masked_semantic_loss, semantic_batch

    state = {"schema": [{"column": "t.v"}], "conversation": [{"role": "user", "content": "How many rows?"}]}
    query = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "t")
    row = semantic_example(state, identity="train/one", source="train_spider", db_id="db", split="train", query=query)
    assert row["input"] == state and "target_ast" not in row["input"]
    assert row["targets"][0]["sql.count_rows"] == 1
    assert "https://schema.org/name" not in row["targets"][0]
    coordinates = ("sql.count_rows", "sql.count_non_null", "https://schema.org/name")
    row["items"] = row["items"][:1]
    row["targets"] = [{"sql.count_rows": 1, "sql.count_non_null": 0}]
    class Tokenizer:
        pad_token_id = 0
        def __call__(self, text, **kwargs):
            return SimpleNamespace(input_ids=[ord(c) for c in text])
    tokenizer = Tokenizer()
    encoded = semantic_encode(tokenizer, row["input"], row["items"])
    tensors = semantic_batch([row], [encoded], tokenizer, coordinates)
    assert tensors["observed"].tolist() == [[[True, True, False]]]
    logits = torch.zeros_like(tensors["targets"], requires_grad=True)
    masked_semantic_loss(logits, tensors["targets"], tensors["observed"]).backward()
    assert logits.grad[0, 0, 2] == 0
    for source in ("dev", "test", "enron"):
        try:
            semantic_example(state, identity="bad", source=source, db_id="db", split="train", query=query)
        except ValueError:
            pass
        else:
            raise AssertionError("held-out or unapproved source admitted to training")


def test_semantic_evaluation_keeps_padding_masks_roles_and_serial_batch_agreement():
    import gzip
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    import torch
    from engine.readers.models import NamedSemanticModel, semantic_encode
    from engine.schema_decode import BindingBasis
    from training.readers.train import evaluate_semantic

    class Encoder(torch.nn.Module):
        config = SimpleNamespace(hidden_size=2)
        def forward(self, input_ids, attention_mask):
            return SimpleNamespace(last_hidden_state=torch.zeros((*input_ids.shape, 2)))
    class Tokenizer:
        pad_token_id = 0
        def __call__(self, text, **kwargs):
            return SimpleNamespace(input_ids=[ord(c) for c in text])
    names = ("sql.project", "sql.filter", "https://schema.org/name")
    basis = BindingBasis(names, (1, 1, 1), (False, False, False), "test-uncalibrated")
    model = NamedSemanticModel(Encoder(), basis)
    with torch.no_grad():
        model.head.projection.weight.zero_()
        model.head.projection.bias.copy_(torch.logit(torch.tensor([0.9, 0.1, 0.8])))
    state = {"schema": [], "conversation": [{"role": "user", "content": "Read it."}]}
    rows = [{"id": "query", "source": "train_spider", "input": state,
             "items": [{"kind": "question", "identity": "q", "text": "Read operations."}],
             "targets": [{"sql.project": 1, "sql.filter": 0}]},
            {"id": "entity", "source": "semantic_instance", "input": state,
             "items": [{"kind": "value", "identity": "v", "text": "Paris"},
                       {"kind": "noun", "identity": "n", "text": "City"}],
             "targets": [{"https://schema.org/name": 1}, {"https://schema.org/name": 1}]}]
    tokenizer = Tokenizer()
    encoded = {r["id"]: semantic_encode(tokenizer, r["input"], r["items"]) for r in rows}
    with tempfile.TemporaryDirectory() as directory:
        outputs = []
        for size in (1, 2):
            path = Path(directory) / f"{size}.jsonl.gz"
            result = evaluate_semantic(model, rows, encoded, tokenizer, basis, path, batch_size=size)
            assert result["metrics"]["all"]["known"] == 4
            assert abs(result["metrics"]["all"]["mae"] - 0.15) < 1e-6
            assert result["metrics"]["value"]["known"] == result["metrics"]["noun"]["known"] == 1
            assert result["qualified_coordinates"] == 0 and not result["release_qualification"]
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                outputs.append({r["id"]: r["quantized"] for r in map(json.loads, stream)})
        assert outputs[0] == outputs[1]
        assert len(outputs[0]["query"]) == 1 and len(outputs[0]["entity"]) == 2


def test_semantic_diagnostics_expose_a_sparse_zero_predictor_and_missing_roles():
    from training.readers.train import semantic_diagnostics

    names = ("sql.project", "sql.filter", "https://schema.org/name")
    rows = [{"id": "query", "items": [{"kind": "question"}],
             "targets": [{"sql.project": 1, "sql.filter": 0}]},
            {"id": "entity", "items": [{"kind": "value"}],
             "targets": [{"https://schema.org/name": 1}]}]
    zero = [{"id": r["id"], "probabilities": [[0.0, 0.0, 0.0]]} for r in rows]
    result = semantic_diagnostics(rows, zero, names)
    assert result["metrics"]["all"]["mae"] == result["metrics"]["all"]["zero_predictor_mae"]
    assert result["metrics"]["all"]["recall_at_0_5"] == 0
    assert not result["cross_role_evidence_present"] and not result["release_qualification"]
    correct = [{"id": "query", "probabilities": [[0.9, 0.1, 0.9]]},
               {"id": "entity", "probabilities": [[0.0, 0.0, 0.9]]}]
    metrics = semantic_diagnostics(rows, correct, names)["metrics"]["all"]
    assert metrics["tp"] == 2 and metrics["tn"] == 1 and metrics["fp"] == metrics["fn"] == 0
    assert metrics["f1_at_0_5"] == 1 and metrics["mae"] < metrics["zero_predictor_mae"]
    per_coordinate = semantic_diagnostics(rows, zero, names)["metrics"]
    assert per_coordinate["coordinate/sql.project"]["known"] == 1
    assert per_coordinate["coordinate/sql.project"]["fn"] == 1
    assert per_coordinate["coordinate/https://schema.org/name"]["known"] == 1
    paired = {"id": "paired", "items": [{"kind": "frame"}] * 3,
              "targets": [{"sql.project": 1}, {"sql.filter": 1}, {"sql.project": 1}],
              "choice_groups": [{"start": 0, "stop": 2, "chosen": 0, "polarities": [1, -1]},
                                {"start": 2, "stop": 3, "chosen": 2, "polarities": [1]}]}
    prediction = {"id": "paired", "probabilities": [{"sql.project": .51}, {"sql.filter": .50}, {"sql.project": .1}]}
    choices = semantic_diagnostics([paired], [prediction], names)["conditional_choices"]
    assert choices["groups"] == choices["forced_groups"] == choices["raw_correct"] == 1
    assert choices["quantized_correct"] == 0 and choices["quantized_ties"] == 1
    assert choices["raw_all_correct"] == 1 and choices["quantized_all_correct"] == 0



def test_recursive_schema_readout_covers_aliases_derived_sources_arithmetic_and_ace():
    from engine.readers.grammar import ASTGrammar, validate_grounding
    from engine.readers.ace import encode_query, decode_query
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import (SelectQuery, SelectItem, Aggregate, ColumnRef, SQLType, Literal,
                                BinaryExpr, SubquerySource, InPredicate, SetQuery, Join, query_to_tree)
    graph = SchemaGraph.from_tables([{"name": "items", "columns": ["city", "amount"],
                                     "rows": [["Paris", 10], ["London", 20]]}], [])
    city = graph.column_map[("items", "city")].ref
    amount = graph.column_map[("items", "amount")].ref
    inner = SelectQuery((SelectItem(Aggregate("AVG", amount), "average"),), "items")
    plain = SelectQuery((SelectItem(city),), "items")
    cases = [SelectQuery((SelectItem(ColumnRef("original", "amount", SQLType.INTEGER), "total"),), "items", from_alias="original"),
             SelectQuery((SelectItem(BinaryExpr(amount, "*", Literal(2))),), "items"),
             SelectQuery((SelectItem(ColumnRef("stats", "average", SQLType.REAL)),), SubquerySource(inner, "stats")),
             SelectQuery((SelectItem(city),), "items", where=InPredicate(amount, SelectQuery((SelectItem(amount),), "items"))),
             SetQuery(plain, "UNION", plain),
             SelectQuery((SelectItem(ColumnRef("left_side", "city", SQLType.TEXT)),), "items", from_alias="left_side",
                         joins=(Join("items", ColumnRef("left_side", "amount", SQLType.INTEGER),
                                     ColumnRef("right_side", "amount", SQLType.INTEGER), alias="right_side"),))]
    state = [{"role": "user", "content": "Show the city and the amount times 2 in Paris."}]
    for query in cases:
        records, realized = ASTGrammar(state, graph).teacher(query)
        assert records and all(len(r["items"]) == len(r["targets"]) for r in records)
        text, tree = encode_query(realized)
        assert text.endswith("Which n:result v:answers A?")
        assert query_to_tree(decode_query(tree)) == query_to_tree(realized)
        validate_grounding(realized, graph)
    bad = SelectQuery((SelectItem(ColumnRef("missing", "city", SQLType.TEXT)),), "items")
    try:
        validate_grounding(bad, graph)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("unbound physical columns must fail")


def test_recursive_teacher_exposes_only_previous_choices_and_named_role_targets():
    from engine.readers.grammar import ASTGrammar
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, Aggregate
    graph = SchemaGraph.from_tables([{"name": "items", "columns": ["amount"], "rows": [[10], [20]]}], [])
    amount = graph.column_map[("items", "amount")].ref
    turns = [{"role": "user", "content": "What is the requested amount?"}]
    outputs = [ASTGrammar(turns, graph).teacher(SelectQuery((SelectItem(Aggregate(op, amount)),), "items"))[0]
               for op in ("SUM", "AVG")]
    first_difference = next(i for i, pair in enumerate(zip(*outputs)) if pair[0]["selected"] != pair[1]["selected"])
    for left, right in zip(outputs[0][:first_difference + 1], outputs[1][:first_difference + 1]):
        assert left["input"] == right["input"] and left["items"] == right["items"]
    for output in outputs:
        for row in output:
            assert "targets" not in row["input"] and "target_ast" not in row["input"]
            assert all(len(labels) == 1 for labels in row["targets"])
            for option, labels in zip(row["request"], row["targets"]):
                descriptor = row["request"][option]
                value = labels[descriptor["coordinate"]]
                assert (value == (1 if descriptor["polarity"] == 1 else 0)) == (option == row["selected"])


def test_named_field_readout_replays_role_binding_and_rejects_unknown_or_tampered_values():
    import copy
    from engine.readers.decisions import named_propositions, named_field_choice, replay_named_field, NamedDecisionPolicy, InvalidDecision
    from engine.readers.layout import named_coordinate_contract
    from engine.schema_decode import BindingBasis, BindingProfile
    names = tuple(r["name"] for r in named_coordinate_contract()["coordinates"])
    mask = tuple(n == "role.projection" for n in names)
    basis = BindingBasis(names, (1,) * len(names), mask, "controlled-test-only", role_qualified=(("frame", mask),))
    options = {"o0": {"column": "items.city"}, "o1": {"column": "items.amount"}}
    request = named_propositions("projection0", options, {k: "role.projection" for k in options})
    profiles = {}
    for i, descriptor in enumerate(request.values()):
        values = tuple((16 if i == 0 else 0) if name == "role.projection" else None for name in names)
        item = descriptor["item"]
        profiles[item["identity"]] = BindingProfile(basis.identity, item["identity"], "frame", values, "expected_concept")
    policy = NamedDecisionPolicy((3, 4), (1, 4))
    trace = named_field_choice(basis, profiles, request, policy)
    assert trace["choice"] == "o0" and trace["hidden_choice_channels"] == 0
    assert replay_named_field(trace) == trace
    changed = copy.deepcopy(trace); changed["contributions"][0]["score"] = [0, 1]
    try:
        replay_named_field(changed)
    except InvalidDecision:
        pass
    else:
        raise AssertionError("changed contributions must fail replay")
    absent = next(iter(profiles))
    profiles[absent] = BindingProfile(basis.identity, absent, "frame", (None,) * len(names), "expected_concept")
    assert named_field_choice(basis, profiles, request, policy)["choice"] is None


def test_parallel_grammar_input_has_no_target_prefix_and_static_columns_ignore_the_question():
    from engine.readers.grammar import ASTGrammar
    from engine.readers.models import semantic_encode
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, Aggregate
    from types import SimpleNamespace
    class Tokenizer:
        def __call__(self, text, **kwargs): return SimpleNamespace(input_ids=[ord(c) % 127 + 1 for c in text], offset_mapping=[(i, i + 1) for i in range(len(text))])
    graph = SchemaGraph.from_tables([{"name": "items", "columns": ["city", "amount"], "rows": [["Paris", 10], ["Lyon", 20]]}], [])
    turns = [{"role": "user", "content": "What is the requested amount?"}]
    amount = graph.column_map[("items", "amount")].ref
    rows = [ASTGrammar(turns, graph).training_example(SelectQuery((SelectItem(Aggregate(op, amount)),), "items"), positions=64)[0]
            for op in ("SUM", "AVG")]
    assert rows[0]["input"] == rows[1]["input"] and rows[0]["items"] == rows[1]["items"]
    assert rows[0]["targets"] != rows[1]["targets"]
    assert "prefix" not in str(rows[0]["input"])
    token = Tokenizer()
    original = ASTGrammar(turns, graph); state, items = original.bank(64)
    other = ASTGrammar([{"role": "user", "content": "Show Lyon instead of Paris."}], graph); changed, bank = other.bank(64)
    first, spans = semantic_encode(token, state, items)
    second, _ = semantic_encode(token, changed, bank)
    static_end = max(max(span) for item, span in zip(items, spans) if item["kind"] == "column") + 1
    assert first[:static_end] == second[:static_end]


def test_one_prefill_named_query_replays_full_ast_without_an_encoder_or_residual_score():
    import torch
    from torch import nn
    from types import SimpleNamespace
    from engine.readers.models import NamedSemanticModel
    from engine.readers.grammar import ASTGrammar, replay_query_trace
    from engine.readers.decisions import NamedDecisionPolicy
    from engine.readers.layout import named_coordinate_contract
    from engine.schema_decode import BindingBasis
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, Aggregate, query_to_tree
    class Tokenizer:
        pad_token_id = 0
        def __call__(self, text, **kwargs): return SimpleNamespace(input_ids=[ord(c) % 127 + 1 for c in text], offset_mapping=[(i, i + 1) for i in range(len(text))])
    class Encoder(nn.Module):
        def __init__(self):
            super().__init__(); self.embedding = nn.Embedding(128, 8); self.config = SimpleNamespace(hidden_size=8); self.calls = 0
        def forward(self, input_ids, attention_mask, **kwargs):
            self.calls += 1
            return SimpleNamespace(last_hidden_state=self.embedding(input_ids))
    names = tuple(r["name"] for r in named_coordinate_contract()["coordinates"])
    mask = (True,) * len(names)
    basis = BindingBasis(names, (1,) * len(names), mask, "controlled-mechanism-test-only", role_qualified=(("frame", mask),))
    graph = SchemaGraph.from_tables([{"name": "items", "columns": ["amount"], "rows": [[10], [20]]}], [])
    turns = [{"role": "user", "content": "What is the average amount?"}]
    gold = SelectQuery((SelectItem(Aggregate("AVG", graph.columns[0].ref)),), "items")
    prepared, expected, _ = ASTGrammar(turns, graph).training_example(gold, positions=64)
    oracle = {}
    for pair, labels in zip(prepared["pairs"], prepared["targets"]):
        if pair["interaction"]:
            oracle[(pair["query"], tuple(pair["components"]))] = labels
    class ControlledModel(NamedSemanticModel):
        def pair_logits(self, pooled, queries, components, component_mask, interactions):
            result = torch.zeros((1, queries.shape[1], len(names)))
            for i, query in enumerate(queries[0].tolist()):
                parts = tuple(components[0, i][component_mask[0, i]].tolist())
                labels = oracle[(query, parts)]
                for name, value in labels.items(): result[0, i, names.index(name)] = 12 if value else -12
            return result
    encoder = Encoder(); model = ControlledModel(encoder, basis).eval()
    result = model.read_query(Tokenizer(), turns, graph, NamedDecisionPolicy((3, 4), (1, 4)), max_decisions=64)
    assert result["action"] == "answer" and query_to_tree(result["query"]) == query_to_tree(expected)
    assert encoder.calls == result["named_trace"]["prefill_calls"] == 1
    assert query_to_tree(replay_query_trace(turns, graph, result["named_trace"])) == query_to_tree(expected)
    assert encoder.calls == 1, "trace replay must not rerun a model"
    # The candidate uses the production selection/executor, without loading the
    # promoted encoder or an unrelated ranking model.
    from engine.readers.search import ReadingSearch
    from engine.tables import TableQuery
    from unittest.mock import patch
    captured_turns = []
    class RecordedRead:
        def read_query(self, tokenizer, turns, graph, policy, *, max_len):
            captured_turns.append(turns)
            return result
    reader = ReadingSearch.__new__(ReadingSearch)
    reader.semantic = True; reader.model = RecordedRead(); reader.tokenizer = Tokenizer()
    reader.max_len = 32768
    reader.policy = NamedDecisionPolicy((3, 4), (1, 4))
    planner = TableQuery(named_reader=reader)
    with patch("engine.readers.search.reading_search", side_effect=AssertionError("promoted readers were called")):
        norm, keys = planner.ingest([{"name": "items", "columns": ["amount"], "rows": [[10], [20]]}])
        schema, _, tables = planner.schema(norm, keys)
        selected = planner.select_query(turns[0]["content"], norm, keys, schema, tables)
        assert selected.candidate is not None
        from engine.sql_rank import replay_served_query_trace
        assert {key: value for key, value in selected.candidate.named_trace.items()
                if key != "served_transform"} == result["named_trace"]
        replayed = replay_served_query_trace(turns, reader.last_named_graph, selected.candidate.named_trace)
        assert query_to_tree(replayed) == query_to_tree(expected)
        _, answer = planner.execute(tables, schema, selected.candidate.sql, query=selected.candidate.query)
        assert float(answer[0][0]) == 15
        conversation = [{"role": "user", "content": "Show the total amount."},
                        {"role": "assistant", "content": "The total is 30."},
                        {"role": "user", "content": "Use the average instead."}]
        selected = planner.select_query(conversation[-1]["content"], norm, keys, schema, tables, turns=conversation)
        assert selected.candidate is not None and captured_turns[-1] == conversation
    action_index = next(i for i, item in enumerate(prepared["items"]) if item["identity"] == "response-action")
    for (query_index, _), labels in oracle.items():
        if query_index != action_index:
            for name in labels:
                labels[name] = 0
    refused = model.read_query(Tokenizer(), turns, graph, NamedDecisionPolicy((3, 4), (1, 4)), max_decisions=64)
    assert refused["action"] == "clarify" and refused["named_trace"]["fields"]
    assert replay_query_trace(turns, graph, refused["named_trace"]) is None


def test_frozen_semantic_features_preserve_paired_static_logits_gradients_and_input_identity():
    from copy import deepcopy
    from types import SimpleNamespace
    import tempfile
    import torch
    from torch import nn
    from engine.readers.models import NamedSemanticModel
    from engine.schema_decode import BindingBasis
    from training.readers.train import (SemanticFeatureCache, semantic_batch, semantic_cached_batch,
                                         semantic_forward, semantic_training_loss)

    class Encoder(nn.Module):
        def __init__(self):
            super().__init__(); self.embedding = nn.Embedding(8, 8)
            self.config = SimpleNamespace(hidden_size=8); self.calls = 0
        def forward(self, input_ids, attention_mask, **kwargs):
            self.calls += 1
            self.grad_enabled_seen = torch.is_grad_enabled()
            states = self.embedding(input_ids).requires_grad_(True)
            return SimpleNamespace(last_hidden_state=states.sin())

    coordinates = ("role.projection", "sql.count", "https://schema.org/name")
    basis = BindingBasis(coordinates, (1, 1, 1), (False, False, False), "feature-mechanism-test")
    encoder = Encoder().requires_grad_(False)
    model = NamedSemanticModel(encoder, basis)
    items = [{"identity": str(i), "kind": "column", "text": str(i)} for i in range(4)]
    paired = {"id": "paired", "input": {"schema": [], "conversation": []}, "items": items,
              "pairs": [{"query": 1, "components": [2], "interaction": True},
                        {"query": 1, "components": [3], "interaction": True}],
              "targets": [{"role.projection": 1}, {"role.projection": 0}],
              "outputs": [{"kind": "frame"}, {"kind": "frame"}],
              "choice_groups": [{"start": 0, "stop": 2, "chosen": 0, "polarities": [1, 1]}]}
    static = {"id": "static", "input": {"schema": [], "conversation": []},
              "items": [{"identity": "concept", "kind": "noun", "text": "City"}],
              "targets": [{"https://schema.org/name": 1}]}
    rows = [paired, static]; tokenizer = SimpleNamespace(pad_token_id=0)
    tensors = semantic_batch(rows, [([1, 2, 3, 4], [[0], [1], [2], [3]]), ([5], [[0]])], tokenizer, coordinates)
    direct = semantic_forward(model, tensors)
    assert not encoder.grad_enabled_seen, "frozen encoder must not retain the input-gradient hook's graph"
    semantic_training_loss(direct, tensors).backward()
    reference = model.head.projection.weight.grad.clone()
    model.zero_grad(set_to_none=True)
    pooled = model.pooled_states(tensors["ids"], tensors["mask"], tensors["item_index"], tensors["item_mask"])
    with tempfile.TemporaryDirectory() as directory:
        cache = SemanticFeatureCache(directory, {"width": 8, "encoder": "fixed-test-encoder"})
        for i, row in enumerate(rows):
            cache.put(row, pooled[i, :len(row["items"])], [4, 1][i])
        payload, _ = cache.load(paired)
        assert payload["indices"].tolist() == [1, 2, 3], "unused states do not alter the complete encoder input"
        frozen_batch = semantic_cached_batch(rows, cache, tokenizer, coordinates)
        calls = encoder.calls
        replayed = semantic_forward(model, frozen_batch)
        assert encoder.calls == calls and torch.equal(direct, replayed)
        encoder.requires_grad_(False)
        model.train()
        assert model.head.training and not encoder.training
        encoder.requires_grad_(True)
        model.train()
        assert model.head.training and encoder.training
        semantic_training_loss(replayed, frozen_batch).backward()
        assert torch.equal(reference, model.head.projection.weight.grad)
        assert not model.head.projection.weight.grad[1].any(), "unknown labels retain zero gradient"
        changed = deepcopy(paired); changed["input"]["conversation"] = [{"role": "user", "content": "another question"}]
        try:
            cache.load(changed)
        except ValueError:
            pass
        else:
            raise AssertionError("changed encoder evidence accepted")
        data_path, _ = cache.paths(paired)
        data = data_path.read_bytes(); data_path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        try:
            cache.load(paired)
        except ValueError:
            pass
        else:
            raise AssertionError("altered feature tensor accepted")

    # Importing/loading a featurizer must not leave a different global matmul
    # policy than the trainer used. No GPU or external weights are needed here.
    import sys
    from unittest.mock import patch
    from engine.readers.models import clef_semantic_model
    previous = (torch.backends.cuda.matmul.allow_tf32, torch.backends.cudnn.deterministic,
                torch.backends.cudnn.benchmark)
    def construct(*args, **kwargs):
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.benchmark = True
        return SimpleNamespace(encoder=Encoder()), SimpleNamespace(tokenizer="pinned-tokenizer")
    try:
        fake = SimpleNamespace(FastDecisionModel=SimpleNamespace(from_pretrained=construct))
        with patch.dict(sys.modules, {"unsloth": fake}):
            frozen, tokenizer = clef_semantic_model(basis, backbone="test", revision="fixed", lora_rank=0)
        assert not torch.backends.cuda.matmul.allow_tf32 and torch.backends.cudnn.deterministic
        assert not torch.backends.cudnn.benchmark and tokenizer == "pinned-tokenizer"
        assert not any(p.requires_grad for p in frozen.encoder.parameters())
    finally:
        torch.backends.cuda.matmul.allow_tf32, torch.backends.cudnn.deterministic, torch.backends.cudnn.benchmark = previous


def test_named_pair_training_uses_the_shared_projection_and_masks_unknown_gradients():
    import torch
    from torch import nn
    from types import SimpleNamespace
    from engine.readers.models import NamedSemanticModel, semantic_encode
    from engine.readers.grammar import ASTGrammar
    from engine.readers.layout import named_coordinate_contract
    from engine.schema_decode import BindingBasis
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, Aggregate
    from training.readers.train import semantic_batch, semantic_forward, semantic_training_loss, semantic_schedule
    class Tokenizer:
        pad_token_id = 0
        def __call__(self, text, **kwargs): return SimpleNamespace(input_ids=[ord(c) % 127 + 1 for c in text], offset_mapping=[(i, i + 1) for i in range(len(text))])
    class Encoder(nn.Module):
        def __init__(self): super().__init__(); self.e = nn.Embedding(128, 8); self.config = SimpleNamespace(hidden_size=8); self.calls = 0
        def forward(self, input_ids, attention_mask, **kwargs):
            self.calls += 1; return SimpleNamespace(last_hidden_state=self.e(input_ids))
    names = tuple(r["name"] for r in named_coordinate_contract()["coordinates"])
    basis = BindingBasis(names, (1,) * len(names), (False,) * len(names), "unqualified-training-test")
    graph = SchemaGraph.from_tables([{"name": "items", "columns": ["amount"], "rows": [[10], [20]]}], [])
    row, _, _ = ASTGrammar([{"role": "user", "content": "average amount"}], graph).training_example(
        SelectQuery((SelectItem(Aggregate("AVG", graph.columns[0].ref)),), "items"), positions=32)
    token = Tokenizer(); encoded = semantic_encode(token, row["input"], row["items"])
    tensors = semantic_batch([row], [encoded], token, names)
    model = NamedSemanticModel(Encoder(), basis)
    logits = semantic_forward(model, tensors); logits.retain_grad()
    semantic_training_loss(logits, tensors).backward()
    assert model.encoder.calls == 1 and model.head.projection.weight.grad.abs().sum() > 0
    assert torch.equal(logits.grad[~tensors["observed"]], torch.zeros_like(logits.grad[~tensors["observed"]]))
    batches = list(semantic_schedule(list(range(5)), 3, 3407, 4))
    assert list(map(len, batches)) == [3, 2, 3, 2]
    assert set(batches[0] + batches[1]) == set(range(5))
    parents = [{"id": str(i)} for i in range(5)]
    token_lengths = {str(i): size for i, size in enumerate((10, 20, 12, 25, 18))}
    bounded = list(semantic_schedule(parents, 2, 3407, 10, token_lengths=token_lengths, token_budget=32, epochs=2))
    assert all(max(token_lengths[row["id"]] for row in chunk) * len(chunk) <= 32 for chunk in bounded)
    from collections import Counter
    assert Counter(row["id"] for chunk in bounded for row in chunk) == Counter({str(i): 2 for i in range(5)})

def test_joint_source_summaries_keep_full_question_spans_and_original_cells():
    import json
    from types import SimpleNamespace
    from engine.readers.grammar import ASTGrammar
    from engine.readers.models import semantic_encode
    class Tokenizer:
        def __call__(self, text, **kwargs):
            return SimpleNamespace(input_ids=[ord(c) + 1 for c in text],
                                   offset_mapping=[(i, i + 1) for i in range(len(text))])
    original = "A long source document. " * 10000
    quoted = "quoted-name-" + "z" * 450
    turns = [{"role": "user", "content": "Find " + quoted + "."}]
    graph = SchemaGraph.from_tables([{"name": "docs", "columns": ["body"], "rows": [[original]]}], [])
    state, items = ASTGrammar(turns, graph).bank(64)
    before = json.dumps(state, sort_keys=True)
    ids, _ = semantic_encode(Tokenizer(), state, items, max_len=32768)
    wire = "".join(chr(n - 1) for n in ids)
    assert quoted in wire and original not in wire
    assert "source_text_preview" in wire and "complete_value_retained_in_source" in wire
    assert graph.columns[0].values[0] == original and json.dumps(state, sort_keys=True) == before


def test_semantic_disk_cache_preserves_exact_records_and_excludes_test_from_fit():
    import tempfile, gzip, json
    from pathlib import Path
    from training.readers.train import SemanticInputs
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "records.jsonl.gz"
        rows = [{"id": str(i), "source": "train_spider", "db_id": "academic", "contract_sha256": "contract",
                 "split": split, "input": {"text": "retained full source " * 1000}}
                for i, split in enumerate(("train", "calibration", "train", "test"))]
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for row in rows: stream.write(json.dumps(row) + "\n")
        cached = SemanticInputs(path, cache_size=1)
        try:
            assert len(cached.metadata) == 3 and "3" not in cached.positions
            for row in rows[:3]:
                assert cached[row["id"]] == row and len(cached.cache) == 1
            assert cached["0"] == rows[0]
        finally:
            cached.close()


def test_semantic_zstandard_cache_iterates_binary_lines_and_text_without_changes():
    import tempfile, json, zstandard
    from pathlib import Path
    from training.readers.train import SemanticInputs, semantic_stream
    row = {"id": "one", "source": "train_spider", "db_id": "academic", "split": "train",
           "contract_sha256": "contract", "input": {"text": "Paris\nLyon"}}
    payload = (json.dumps(row) + "\n").encode()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "examples.jsonl.zst"
        path.write_bytes(zstandard.ZstdCompressor().compress(payload))
        with semantic_stream(path, "rb") as stream:
            assert list(stream) == [payload]
        with semantic_stream(path) as stream:
            assert [json.loads(line) for line in stream] == [row]
        cached = SemanticInputs(path)
        try:
            assert cached["one"] == row
        finally:
            cached.close()


def test_named_qualification_requires_complete_held_out_role_evidence():
    import tempfile, gzip, json, hashlib
    from pathlib import Path
    from types import SimpleNamespace
    from training.readers.train import qualify_semantic_candidate
    from engine.schema_decode import BindingBasis
    names = ("role.projection", "role.modifier")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); candidate = root / "candidate"; candidate.mkdir()
        evaluation = root / "evaluation"; evaluation.mkdir()
        examples = root / "examples.jsonl.gz"
        rows = [{"id": str(i), "source": "train_spider", "db_id": "academic", "split": "calibration",
                 "items": [{"kind": "frame"}], "targets": [{name: 1 - i for name in names}]} for i in range(2)]
        with gzip.open(examples, "wt", encoding="utf-8") as stream:
            for row in rows: stream.write(json.dumps(row) + "\n")
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        metadata = candidate / "semantic-model.json"
        metadata.write_text(json.dumps({"basis": BindingBasis(names, (1, 1), (False, False), "test").record(),
                                        "examples_sha256": sha(examples)}))
        predictions = evaluation / "predictions.jsonl.gz"
        with gzip.open(predictions, "wt", encoding="utf-8") as stream:
            for i in range(2):
                stream.write(json.dumps({"id": str(i), "probabilities": [{names[0]: .99 if i == 0 else .01,
                                                                         names[1]: .01}]}) + "\n")
        (evaluation / "assessment.json").write_text(json.dumps({"split": "calibration", "role_filter": None,
            "candidate_metadata_sha256": sha(metadata), "predictions_sha256": sha(predictions), "examples": 2}))
        qualify_semantic_candidate(SimpleNamespace(candidate=str(candidate), evaluation=str(evaluation), examples=str(examples),
                                                  min_support=1, min_accuracy=.95, max_mae=.1))
        sealed = json.loads((candidate / "qualification.json").read_text())
        basis = BindingBasis.from_record(sealed["basis"])
        assert basis.qualified_for("frame") == (True, False)
        assert basis.qualified_for("column") == (False, False)
        assert sealed["candidate_metadata_sha256"] == sha(metadata)


def test_question_spans_numbers_and_calendar_literals_use_only_input_evidence():
    from engine.readers.grammar import ASTGrammar
    from engine.readers.numerals import mentions, number
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, Literal, Comparison
    assert number("one hundred and twenty five") == 125
    assert number("one and two") is None
    for text, expected in (("30 000", 30000), ("ten million", 10000000),
                           ("10 million", 10000000), ("1.5 billion", 1500000000), ("half", 0.5)):
        assert list(mentions(text))[0][0] == expected
    # A fraction is a written number as a share or a scale, never as the measure of a unit or a calendar part:
    # "stayed longer than half a year" once became the literal 0.5 and a filter "past due is above 0.5"
    # (messy dev, subscriptions, 2026-11-08), where the old reader asked back.
    for text, expected in (("more than half of the orders", [0.5]), ("more than half", [0.5]),
                           ("half a million", [500000]), ("a quarter of the stock", [0.25]),
                           ("revenue from people who stayed longer than half a year", []),
                           ("calls under half an hour", []), ("a quarter of an hour", []),
                           ("revenue last quarter", []), ("sales by quarter", []),
                           ("payments in the first half of 2004", [2004])):
        assert [n for n, _, _ in mentions(text)] == expected, text
    # The decoder reads a value span without its sentence: a lone fraction word is no literal there.
    assert number("half") is None and number("quarter") is None
    graph = SchemaGraph.from_tables([{"name": "items", "columns": ["title", "date"],
                                     "rows": [["unrelated", "2001-01-01"]]}], [])
    turns = [{"role": "user", "content": "Find The Great Railway Bazaar during January 2012."}]
    controller = ASTGrammar(turns, graph)
    _, items = controller.bank(64)
    phrase = next(item for item in items if item["kind"] == "span" and item["text"] == "The Great Railway Bazaar")
    assert turns[0]["content"][phrase["start"]:phrase["stop"]] == phrase["text"]
    query = SelectQuery((SelectItem(graph.columns[0].ref),), "items",
                        where=Comparison(graph.columns[0].ref, "=", Literal(phrase["text"])))
    _, realized = controller.teacher(query)
    assert realized is not None
    assert "2012-01-01" in controller.literals and "2012-01-31" in controller.literals
    forms = ASTGrammar('Find red cakes and Production Technicians.', graph)
    assert 'red cake' in forms.literals and 'Production Technician' in forms.literals
    assert 'red cakes' in forms.literals and 'Production Technicians' in forms.literals
    assert 'blue cake' not in forms.literals and 'Production Engineer' not in forms.literals
    assert graph.columns[0].values == ('unrelated',)


def test_offline_type_repair_preserves_physical_bindings_and_derived_scopes():
    from engine.readers.grammar import retype_grounded_query, validate_grounding
    from engine.readers.decisions import InvalidDecision
    from engine.sql_ast import ColumnRef, SelectItem, SelectQuery, SQLType, SubquerySource
    from engine.sql_schema import SchemaGraph
    graph = SchemaGraph.from_tables([{'name': 'items', 'columns': ['n'], 'rows': [[3], [5]]}], [])
    child = SelectQuery((SelectItem(ColumnRef('items', 'n', SQLType.TEXT)),), 'items')
    query = SelectQuery((SelectItem(ColumnRef('q', 'n', SQLType.TEXT)),), SubquerySource(child, 'q'))
    try:
        validate_grounding(query, graph)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('serving validation silently repaired a mismatched source type')
    corrected = retype_grounded_query(query, graph)
    validate_grounding(corrected, graph)
    assert corrected.select[0].expression.type == SQLType.INTEGER
    assert corrected.from_table.query.select[0].expression.type == SQLType.INTEGER
    assert corrected.select[0].expression.table == 'q'
    try:
        retype_grounded_query(SelectQuery((SelectItem(ColumnRef('items', 'missing', SQLType.TEXT)),), 'items'), graph)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('source repair invented a missing physical column')


def test_corpus_parents_and_encoder_bank_share_actual_physical_schema_evidence():
    import contextlib
    import gzip
    import io
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from types import SimpleNamespace
    from engine.readers.grammar import ASTGrammar, source_schema
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, query_to_tree
    from training.readers.examples import semantic_main

    graph = SchemaGraph.from_tables([{'name': 'places', 'columns': ['code', 'amount'],
        'rows': [['00877', 2], ['877', 4]]}], [])
    physical = source_schema(graph)
    assert physical == [{'table': 'places', 'column': 'code', 'type': 'text', 'samples': ['00877', '877']},
                        {'table': 'places', 'column': 'amount', 'type': 'integer', 'samples': [2, 4]}]
    for question in ('Show amounts for 00877.', 'Show the code with the largest amount.'):
        state, _ = ASTGrammar(question, graph).bank(1)
        assert state['schema'][:len(physical)] == physical
    query = SelectQuery((SelectItem(graph.column_map[('places', 'amount')].ref),), 'places')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); programs = root / 'programs.jsonl'; conversations = root / 'turns.jsonl'
        programs.write_text(json.dumps({'id': 'train_spider/0', 'db_id': 'places', 'partition': 'train',
            'original_question': 'Show amounts.', 'target': query_to_tree(query)}) + '\n', encoding='utf8')
        conversations.write_text(json.dumps({'id': 'train_spider/1/clarify', 'source': 'train_spider',
            'index': 1, 'variant': 'clarify', 'db_id': 'places', 'partition': 'train',
            'state': {'schema': [{'column': 'places.amount', 'type': 'text', 'samples': ['wrong']}],
                      'conversation': [{'role': 'user', 'content': 'Show the best.'}]},
            'questions': {'action': {'criteria': {'choice': '"clarify"'}}},
            'gold': {'action': {'label': 'choice'}}}) + '\n', encoding='utf8')
        with conversations.open('a', encoding='utf8') as stream:
            stream.write(json.dumps({'id': 'train_spider/0/single', 'source': 'train_spider', 'index': 0,
                'variant': 'single', 'db_id': 'places', 'partition': 'train',
                'state': {'schema': [{'column': 'places.amount', 'type': 'text'}],
                          'conversation': [{'role': 'user', 'content': 'Show amounts.'}]},
                'target_readings': {'deliberately_lossy': True},
                'questions': {'action': {'criteria': {'choice': '"answer"'}}},
                'gold': {'action': {'label': 'choice'}}}) + '\n')
        sources = SimpleNamespace(graph=lambda db: graph)
        with patch('training.readers.sources.Sources.from_args', return_value=sources), contextlib.redirect_stdout(io.StringIO()):
            semantic_main(['--programs', str(programs), '--conversation', str(conversations), '--out-dir', str(root / 'out')])
        with gzip.open(root / 'out/semantic-examples.jsonl.gz', 'rt', encoding='utf8') as stream:
            rows = list(map(json.loads, stream))
        assert len(rows) == 3 and all(row['input']['schema'] == physical for row in rows)
        assert rows[0]['target_ast'] == rows[2]['target_ast'] == query_to_tree(query) and rows[1]['target_ast'] is None


def test_generated_dialogues_preserve_source_join_keys_and_only_declared_corrections():
    from dataclasses import replace
    from training.readers.examples import generated_dialogue_query
    from engine.sql_ast import (ColumnRef, SQLType, SelectQuery, SelectItem, Join,
                                Comparison, Literal, OrderTerm, query_to_tree)

    key = ColumnRef('places', 'id', SQLType.INTEGER)
    amount = ColumnRef('places', 'amount', SQLType.INTEGER)
    query = SelectQuery((SelectItem(amount),), 'places',
        joins=(Join('locations', key, ColumnRef('locations', 'place_id', SQLType.INTEGER)),),
        where=Comparison(ColumnRef('locations', 'city', SQLType.TEXT), '=', Literal('Paris')),
        order_by=(OrderTerm(amount, 'DESC'),), limit=10, with_ties=True)
    original = query_to_tree(query);question = 'Show the largest ten amounts in Paris.'
    user = {'role': 'user', 'content': question};assistant = {'role': 'assistant', 'content': 'I will use the uploaded tables.'}
    def row(variant, follow=None):
        return {'variant': variant, 'state': {'conversation': [user] if follow is None else
            [user, assistant, {'role': 'user', 'content': follow}]}}
    assert generated_dialogue_query(row('single'), query, question) == query
    assert generated_dialogue_query(row('reference', 'Please answer that question.'), query, question) == query
    corrected = generated_dialogue_query(row('correction', 'Actually, show the lowest first instead, and only the first three.'), query, question)
    assert corrected == replace(query, order_by=(OrderTerm(amount, 'ASC'),), limit=3, with_ties=False)
    assert query_to_tree(query) == original
    for proposed, source in [(row('single'), 'different question'),
            (row('reference', 'Remove the Paris condition.'), question),
            (row('correction', 'Actually, show the lowest first instead, and only the first three.'), question)]:
        try:
            generated_dialogue_query(proposed, replace(query, order_by=()) if proposed['variant']=='correction' else query, source)
        except ValueError:
            pass
        else:
            raise AssertionError('unverified source/continuation or invented ordering accepted')


def test_source_grouping_expands_only_columns_proved_by_nonnull_primary_keys():
    import sqlite3
    import tempfile
    from pathlib import Path
    from engine.sql_ast import (SelectQuery, SelectItem, Aggregate, Star, Join, render_query, ASTValidationError)
    from engine.sql_schema import SchemaGraph
    from training.readers.gold import normalize_source_grouping
    from training.readers.sources import Sources

    graph = SchemaGraph.from_tables([
        {'name': 'customers', 'columns': ['id', 'name', 'city'], 'rows': [[1, 'Alice', 'Paris'], [2, 'Bob', 'Paris']]},
        {'name': 'orders', 'columns': ['customer_id'], 'rows': [[1], [1], [2]]}], [])
    key = graph.column_map[('customers', 'id')].ref
    name = graph.column_map[('customers', 'name')].ref
    city = graph.column_map[('customers', 'city')].ref
    customer = graph.column_map[('orders', 'customer_id')].ref
    query = SelectQuery((SelectItem(name), SelectItem(Aggregate('COUNT', Star()))), 'orders',
                        joins=(Join('customers', customer, key),), group_by=(customer,))
    normalized = normalize_source_grouping(query, graph, {'customers': ('id',)})
    assert normalized.group_by == (customer, name) and query.group_by == (customer,)
    with sqlite3.connect(':memory:') as connection:
        connection.executescript("CREATE TABLE customers(id INTEGER PRIMARY KEY,name,city); INSERT INTO customers VALUES(1,'Alice','Paris'),(2,'Bob','Paris'); CREATE TABLE orders(customer_id); INSERT INTO orders VALUES(1),(1),(2);")
        expected = connection.execute('SELECT customers.name,COUNT(*) FROM orders JOIN customers ON orders.customer_id=customers.id GROUP BY orders.customer_id').fetchall()
        assert connection.execute(render_query(normalized)).fetchall() == expected == [('Alice', 2), ('Bob', 1)]
    from dataclasses import replace
    ambiguous = replace(query, group_by=(city,))
    assert normalize_source_grouping(ambiguous, graph, {'customers': ('id',)}) == ambiguous
    try:render_query(ambiguous)
    except ASTValidationError:pass
    else:raise AssertionError('arbitrary bare-column SQL became a valid target')
    composite = {'customers': ('id', 'city')}
    assert normalize_source_grouping(query, graph, composite) == query
    assert normalize_source_grouping(replace(query, group_by=(customer, city)), graph, composite).group_by == (customer, city, name)
    from training.readers.examples import generated_dialogue_query
    from engine.sql_ast import OrderTerm
    question = 'Show the customers with the most orders.'
    source = replace(query, order_by=(OrderTerm(Aggregate('COUNT', Star()), 'DESC'),))
    dialogue = {'variant': 'correction', 'state': {'conversation': [
        {'role': 'user', 'content': question},
        {'role': 'assistant', 'content': 'I will use the uploaded tables.'},
        {'role': 'user', 'content': 'Actually, show the lowest first instead, and only the first three.'}]}}
    corrected = generated_dialogue_query(dialogue,
        normalize_source_grouping(source, graph, {'customers': ('id',)}), question)
    assert corrected.group_by == normalized.group_by and corrected.limit == 3
    assert corrected.order_by[0].direction == 'ASC'
    render_query(corrected)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'keys.sqlite'
        with sqlite3.connect(path) as connection:
            connection.executescript("CREATE TABLE valid(id INTEGER PRIMARY KEY,name); CREATE TABLE nullable(id TEXT PRIMARY KEY,name); INSERT INTO nullable VALUES(NULL,'Alice'),(NULL,'Bob');")
        connection.close();sources = Sources.__new__(Sources);sources.db_files = {'keys': str(path)}
        sources.tables = {'keys': {'table_names_original': ['valid', 'nullable']}}
        assert sources.primary_keys('keys') == {'valid': ('id',)}


def test_publisher_definitions_are_visible_input_candidates_and_keep_source_hashes():
    import hashlib
    import json
    import sqlite3
    import tempfile
    from pathlib import Path
    from engine.readers.grammar import ASTGrammar, source_schema
    from engine.readers.decisions import InvalidDecision
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, Comparison, Literal
    from training.readers.sources import Sources

    definition = "The number of items equals units * 100000. The code 'bulk' describes a bulk order."
    plain = SchemaGraph.from_tables([{'name': 'totals', 'columns': ['units'], 'rows': [[2], [20]]}], [])
    table = {'name': 'totals', 'columns': ['units'], 'rows': [[2], [20]],
             'column_annotations': {'units': {'value_description': definition}}}
    annotated = SchemaGraph.from_tables([table], [])
    turns = [{'role': 'user', 'content': 'Which units exceed one million items?'}]
    column = annotated.column_map[('totals', 'units')].ref
    query = SelectQuery((SelectItem(column),), 'totals', where=Comparison(column, '>', Literal(10)))
    original = ASTGrammar(turns, plain)
    assert 10 not in original.literals and 100000 not in original.literals and 'bulk' not in original.literals
    try:
        original.teacher(query, capture=False, collect=False)
    except InvalidDecision:
        pass
    else:
        raise AssertionError('missing source unit definition was inferred from a target')
    controller = ASTGrammar(turns, annotated)
    assert 10 in controller.literals and 100000 in controller.literals and 'bulk' in controller.literals
    assert controller.teacher(query, capture=False, collect=False)[1] == query
    state, _ = controller.bank(1)
    assert state['schema'][0] == source_schema(annotated)[0]
    assert state['schema'][0]['annotations'] == {'value_description': definition}
    for table in ({'name': 'totals', 'columns': ['units'], 'rows': [], 'column_annotations': {'units': {'note': 10}}},):
        try: SchemaGraph.from_tables([table], [])
        except ValueError: pass
        else: raise AssertionError('non-text source annotations accepted')

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); database = root / 'totals.sqlite'; dictionary = root / 'dictionary.csv'
        with sqlite3.connect(database) as connection:
            connection.execute('CREATE TABLE totals (units INTEGER)'); connection.execute('INSERT INTO totals VALUES (2)')
        connection.close(); dictionary.write_text(definition, encoding='utf8')
        manifest = root / 'manifest.json'
        entry = {'db_id': 'totals', 'table': 'totals', 'column': 'units',
                 'annotations': {'value_description': definition}, 'source_path': str(dictionary),
                 'source_sha256': hashlib.sha256(dictionary.read_bytes()).hexdigest()}
        manifest.write_text(json.dumps([entry]), encoding='utf8')
        sources = Sources.__new__(Sources); sources.db_files = {'totals': str(database)}
        sources.tables = {'totals': {'table_names_original': ['totals'], 'column_names_original': [[-1, '*'], [0, 'units']], 'foreign_keys': []}}
        sources.add_column_dictionaries(manifest)
        assert source_schema(sources.graph('totals'))[0]['annotations'] == {'value_description': definition}
        for change in ('question_hint', 'changed_source', 'duplicate_column'):
            dictionary.write_text(definition, encoding='utf8'); record = {**entry, 'annotations': dict(entry['annotations'])}
            if change == 'question_hint': record['annotations']['gold_sql'] = 'SELECT units FROM totals'
            elif change == 'changed_source': dictionary.write_text('changed', encoding='utf8')
            manifest.write_text(json.dumps([record, record] if change == 'duplicate_column' else [record]), encoding='utf8')
            try: sources.add_column_dictionaries(manifest)
            except ValueError: pass
            else: raise AssertionError('untrusted dictionary mutation accepted')


def test_recovered_database_versions_pin_both_inputs_and_select_atomically():
    import hashlib
    import json
    import tempfile
    from pathlib import Path
    from training.readers.sources import Sources

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory); original = root / 'author-input'; recovered = root / 'database.sqlite'
        original.write_bytes(b'author bytes'); recovered.write_bytes(b'recovered bytes')
        record = {'source_path': str(original), 'source_sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
                  'path': str(recovered), 'sha256': hashlib.sha256(recovered.read_bytes()).hexdigest()}
        manifest = root / 'manifest.json'; manifest.write_text(json.dumps({'places': record}), encoding='utf8')
        sources = Sources.__new__(Sources); sources.tables = {'places': {}}; sources.db_files = {'other': 'unchanged'}
        sources.add_database_versions(manifest)
        assert sources.db_files == {'places': str(recovered), 'other': 'unchanged'}
        for mutation in ('original', 'recovered', 'unknown_database'):
            original.write_bytes(b'author bytes'); recovered.write_bytes(b'recovered bytes')
            entries = {'places': record}
            if mutation == 'original': original.write_bytes(b'different author bytes')
            elif mutation == 'recovered': recovered.write_bytes(b'different recovered bytes')
            else: entries['unregistered'] = record
            manifest.write_text(json.dumps(entries), encoding='utf8'); sources.db_files = {'other': 'unchanged'}
            try:
                sources.add_database_versions(manifest)
            except ValueError:
                pass
            else:
                raise AssertionError('unpinned or unregistered source version was accepted')
            assert sources.db_files == {'other': 'unchanged'}


def test_empty_sql_source_columns_keep_declared_numeric_types_and_foreign_keys():
    import sqlite3
    import tempfile
    from pathlib import Path
    from training.readers.sources import capped_graph
    from engine.sql_ast import SQLType
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'source.sqlite'
        with sqlite3.connect(path) as connection:
            connection.executescript('CREATE TABLE parent (id INTEGER, amount REAL); CREATE TABLE child (parent_id INTEGER, mixed REAL); INSERT INTO child VALUES (NULL, "not_numeric"); CREATE TABLE scientific (n REAL, mixed REAL, tiny REAL, wide TEXT); INSERT INTO scientific VALUES (0.00002884400055336, "unknown", 1e-22, "123456789012345678901234567890123456789012");')
        connection.close()
        meta = {'table_names_original': ['parent', 'child', 'scientific'],
                'column_names_original': [(-1, '*'), (0, 'id'), (0, 'amount'), (1, 'parent_id'), (1, 'mixed'), (2, 'n'), (2, 'mixed'), (2, 'tiny'), (2, 'wide')],
                'foreign_keys': [(3, 1)]}
        graph = capped_graph(path, meta)
        assert graph.column_map[('parent', 'id')].ref.type == SQLType.INTEGER
        assert graph.column_map[('parent', 'amount')].ref.type == SQLType.REAL
        assert graph.column_map[('child', 'parent_id')].ref.type == SQLType.INTEGER
        assert graph.column_map[('child', 'mixed')].ref.type == SQLType.TEXT
        assert graph.foreign_keys[0].from_column.type == SQLType.INTEGER
        assert graph.foreign_keys[0].to_column.type == SQLType.INTEGER
        assert graph.column_map[('scientific', 'n')].ref.type == SQLType.REAL
        assert graph.column_map[('scientific', 'n')].values == (0.00002884400055336,)
        assert graph.column_map[('scientific', 'mixed')].ref.type == SQLType.TEXT
        assert graph.column_map[('scientific', 'tiny')].ref.type == SQLType.TEXT
        assert graph.column_map[('scientific', 'tiny')].values == (1e-22,)
        assert graph.column_map[('scientific', 'wide')].ref.type == SQLType.TEXT
        assert graph.column_map[('scientific', 'wide')].values == ('123456789012345678901234567890123456789012',)


def test_small_source_reference_tables_expose_code_meanings_with_explicit_bounds():
    from datetime import date
    from engine.readers.grammar import ASTGrammar
    from engine.sql_schema import SchemaGraph

    rows = [[i, 'Description of survey field ' + str(i)] for i in range(60)]
    graph = SchemaGraph.from_tables([{'name': 'Question', 'columns': ['id', 'description'], 'rows': rows}], [])
    controller = ASTGrammar('Which responses concern illness?', graph)
    assert 34 in controller.literals
    state, bank = controller.bank(1)
    fact = [item for item in bank if item['identity'] == 'source-row/Question/34']
    assert len(fact) == 1 and 'Description of survey field 34' in fact[0]['text'] and '"id":34' in fact[0]['text']
    assert not any('target' in str(item) for item in state['schema'])
    large = SchemaGraph.from_tables([{'name': 'Question', 'columns': ['id', 'description'],
        'rows': [[i, 'Description of survey field ' + str(i)] for i in range(200)]}], [])
    limited = ASTGrammar('Which responses concern illness?', large)
    assert 134 not in limited.literals
    # Source facts encode native dates as their actual ISO values.
    dates = SchemaGraph.from_tables([{'name': 'Calendar', 'columns': ['id', 'day'],
        'rows': [[i, date(2020, 1, i + 1)] for i in range(20)]}], [])
    _, bank = ASTGrammar('Show the requested date.', dates).bank(1)
    assert any('2020-01-20' in item['text'] for item in bank if item['identity']=='source-row/Calendar/19')


def test_recursive_literal_bank_reads_related_cells_from_matching_source_rows():
    from engine.readers.grammar import ASTGrammar
    from engine.sql_schema import SchemaGraph
    table = {'name': 'airports', 'columns': ['code', 'description'],
             'rows': [[f'X{i}', f'Other terminal {i}'] for i in range(1200)]}
    table['rows'][1000][1] = 'Meadows Field airport'
    graph = SchemaGraph.from_tables([table], [])
    controller = ASTGrammar('Which flight arrived at Meadows Field airport?', graph)
    assert 'X1000' in controller.literals
    assert 'X999' not in controller.literals
    _, items = controller.bank(4)
    evidence = [i for i in items if i['identity'] == 'source-row/airports/1000']
    assert len(evidence) == 1 and 'X1000' in evidence[0]['text'] and 'Meadows Field' in evidence[0]['text']
    other = ASTGrammar('Which flight arrived at Other terminal 1199?', graph)
    assert 'X1199' in other.literals
    from engine.readers.grammar import _row_observations
    import re
    words = set(re.findall(r'[^\W_]+', 'Other terminal 1199 Meadows'.casefold()))
    rows, frequencies, _ = _row_observations(graph)
    expected = sorted((min(frequencies[w] for w in tokens & words), -len(tokens & words), table, row, cells)
                      for table, row, cells, tokens in rows if tokens & words)[:16]
    assert ASTGrammar('Other terminal 1199 Meadows', graph)._row_evidence == expected


def test_question_source_retrieval_uses_input_phrases_beyond_cap_without_writing():
    import hashlib
    import sqlite3
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from training.readers.sources import Sources
    from engine.readers.grammar import ASTGrammar
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'papers.sqlite'
        connection = sqlite3.connect(path)
        connection.executescript('CREATE TABLE papers (id INTEGER, title TEXT, body TEXT); '
                                 'INSERT INTO papers VALUES (1, "Other paper", "Ordinary body"), '
                                 '(7654, "KEYWORDS: haplotype; association studies", "Rare body witness"), '
                                 '(9001, "University of Cambridge", "Another body");')
        connection.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        source = Sources.__new__(Sources)
        source.db_files = {'papers': str(path)}
        source.tables = {'papers': {'table_names_original': ['papers'],
                                   'column_names_original': [(-1, '*'), (0, 'id'), (0, 'title'), (0, 'body')],
                                   'foreign_keys': []}}
        turns = [{'role': 'user', 'content': 'Find the paper with title "haplotype; association studies".'}]
        with patch('training.readers.sources.CAP', 1):
            base = source.graph('papers')
            graph, evidence = source.question_graph('papers', turns)
        assert 7654 not in base.column_map[('papers', 'id')].values
        assert 7654 in graph.column_map[('papers', 'id')].values
        assert evidence[0]['values'] == ['KEYWORDS: haplotype; association studies']
        assert 'KEYWORDS: haplotype; association studies' in ASTGrammar(turns, graph).literals
        for question in ("Find the paper titled 'haplotype; association studies'.",
                         'Find the paper titled “haplotype; association studies”.',
                         'Find the paper titled "haplotype; association studies\n".'):
            with patch('training.readers.sources.CAP', 1):
                retrieved, witness = source.question_graph('papers', [{'role': 'user', 'content': question}])
            assert 7654 in retrieved.column_map[('papers', 'id')].values
            assert witness[0]['values'] == ['KEYWORDS: haplotype; association studies']
        with patch('training.readers.sources.CAP', 1):
            retrieved, witness = source.question_graph('papers', [{'role': 'user', 'content':
                                                       'Find papers about University of Cambridge.'}])
        assert 9001 in retrieved.column_map[('papers', 'id')].values
        assert any(item.get('values') == ['University of Cambridge'] for item in witness)
        for question in ('Find all papers.', 'Find papers about University of Oxford.'):
            with patch('training.readers.sources.CAP', 1):
                retrieved, witness = source.question_graph('papers', [{'role': 'user', 'content': question}])
            assert 9001 not in retrieved.column_map[('papers', 'id')].values
            assert not any(item.get('values') for item in witness)
        # A completed read is stable. An unavailable optional read preserves
        # the base bank, and may never leak partially retrieved facts.
        with patch('training.readers.sources.CAP', 1):
            repeated, _ = source.question_graph('papers', turns)
            assert repeated.columns == graph.columns and repeated.foreign_keys == graph.foreign_keys
            unchanged, unavailable = source.question_graph('papers', turns, seconds=0, graph=base)
            assert unchanged is base and unavailable[0]['state'] == 'unavailable'
            assert unavailable[0]['base_bank_unchanged'] and unavailable[0]['discarded_source_rows'] == 0
            assert 7654 not in ASTGrammar(turns, unchanged).literals
            # A raw question span remains a legal predicate; an unspoken ID
            # still requires source evidence and cannot be invented.
            from engine.sql_ast import ColumnRef,Comparison,Literal,SelectItem,SelectQuery,SQLType
            title = ColumnRef('papers','title',SQLType.TEXT)
            written = SelectQuery((SelectItem(title),),'papers',where=Comparison(
                title,'LIKE',Literal('%haplotype; association studies%')))
            ASTGrammar(turns, unchanged).training_example(written, positions=128)
            hidden_id = SelectQuery((SelectItem(title),),'papers',where=Comparison(
                ColumnRef('papers','id',SQLType.INTEGER),'=',Literal(7654)))
            try:
                ASTGrammar(turns, unchanged).teacher(hidden_id,capture=False,collect=False)
            except ValueError as exc:
                assert 'literal absent' in str(exc)
            else:
                raise AssertionError('unavailable source ID became a training input')
            ASTGrammar(turns, graph).teacher(hidden_id,capture=False,collect=False)
            ticks = iter((0,0,2))
            with patch('time.monotonic', side_effect=lambda: next(ticks,2)):
                atomic, receipt = source.question_graph('papers',turns,seconds=1,graph=base)
            assert atomic is base and receipt[0]['discarded_source_rows'] >= 1
            assert not any('values' in item for item in receipt)
            assert 7654 not in ASTGrammar(turns, atomic).literals
            class BrokenConnection:
                def set_progress_handler(self,*_): pass
                def execute(self,*_): raise sqlite3.OperationalError('no such table: papers')
                def close(self): pass
            with patch('sqlite3.connect',return_value=BrokenConnection()):
                try:
                    source.question_graph('papers',turns,graph=base)
                except sqlite3.OperationalError as exc:
                    assert 'no such table' in str(exc)
                else:
                    raise AssertionError('a database error became optional missing evidence')
        with patch('training.readers.sources.CAP', 1):
            body, _ = source.question_graph('papers', [{'role': 'user', 'content':
                'Find the paper whose body mentions "Rare body witness".'}])
        assert 7654 in body.column_map[('papers', 'id')].values
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_input_reference_profiles_expose_real_categorical_rows_without_target_hints():
    import hashlib
    import sqlite3
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from training.readers.sources import Sources
    from engine.readers.grammar import ASTGrammar
    from engine.sql_ast import ColumnRef,Comparison,Literal,SelectItem,SelectQuery,SQLType
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/'records.sqlite'
        with sqlite3.connect(path) as conn:
            conn.executescript("CREATE TABLE topics(topic_id INTEGER PRIMARY KEY,description TEXT);"
                "CREATE TABLE records(record_id INTEGER PRIMARY KEY,topic_id INTEGER,outcome TEXT);"
                "INSERT INTO topics VALUES(1,'shirt size category'),(2,'inventory tracking'),(9,'conference attendance confirmation');"
                "INSERT INTO records VALUES(1,1,'small'),(2,1,'medium'),(3,1,'large'),"
                "(4,9,'Accepted'),(5,9,'Declined'),(6,2,'Deferred');")
        conn.close()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        source = Sources.__new__(Sources);source.db_files={'records':str(path)}
        source.tables={'records':{'table_names_original':['topics','records'],
            'column_names_original':[(-1,'*'),(0,'topic_id'),(0,'description'),(1,'record_id'),(1,'topic_id'),(1,'outcome')],
            'foreign_keys':[(4,1)]}}
        question=[{'role':'user','content':'Find conference attendance confirmation records.'}]
        with patch('training.readers.sources.CAP',3):base=source.graph('records')
        assert 'Accepted' not in ASTGrammar(question,base).literals
        enriched,evidence=source.question_graph('records',question,graph=base)
        assert any(item.get('reference_table')=='topics' for item in evidence)
        assert enriched.source_rows and 'Accepted' in ASTGrammar(question,enriched).literals
        assert 'Declined' in ASTGrammar(question,enriched).literals
        assert 'Deferred' not in ASTGrammar(question,enriched).literals
        wanted=SelectQuery((SelectItem(ColumnRef('records','record_id',SQLType.INTEGER)),),'records',
            where=Comparison(ColumnRef('records','outcome',SQLType.TEXT),'=',Literal('Accepted')))
        ASTGrammar(question,enriched).training_example(wanted,positions=128)
        other,_=source.question_graph('records',[{'role':'user','content':'Find inventory tracking records.'}],graph=base)
        assert 'Deferred' in ASTGrammar('Find inventory tracking records.',other).literals
        assert 'Accepted' not in ASTGrammar('Find inventory tracking records.',other).literals
        unavailable,receipt=source.question_graph('records',question,graph=base,seconds=0)
        assert unavailable is base and receipt[0]['state']=='unavailable'
        assert 'Accepted' not in ASTGrammar(question,unavailable).literals
        assert enriched.without(frozenset({'records'})).source_rows == ()
        from engine.sql_schema import SchemaGraph
        try:SchemaGraph(base.columns,base.foreign_keys,source_rows=[('records',99)])
        except ValueError:pass
        else:raise AssertionError('source marker accepted cells outside the actual observations')
        assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_compact_observed_names_survive_repeated_long_matches_without_inventing_aliases():
    from engine.readers.grammar import ASTGrammar
    rows = [[f'branch{index:02d} Northern Research Institute'] for index in range(40)]
    rows += [['Northern R I'], ['Southern R I']]
    rows += [[f'in {letter}'] for letter in 'abcdefghijklmno']
    rows += [[f'Other department {index}'] for index in range(160)]
    graph = SchemaGraph.from_tables([{'name':'institutions','columns':['label'],'rows':rows}], [])
    question = 'Find records in Northern Research Institute.'
    controller = ASTGrammar(question, graph)
    assert 'Northern R I' in controller.literals
    assert 'Southern R I' not in controller.literals
    assert 'Northern Research I' not in controller.literals
    assert 'branch00 Northern Research Institute' in controller.literals
    assert controller.literals == ASTGrammar(question, graph).literals
    # Actual cells remain distinct; candidate construction must not rewrite one
    # name into another or assert that the abbreviation denotes the institution.
    assert graph.column_map[('institutions','label')].values[40] == 'Northern R I'


def test_input_reference_profiles_bind_decimal_keys_without_float_rounding():
    import hashlib
    import json
    import sqlite3
    import tempfile
    from decimal import Decimal
    from pathlib import Path
    from unittest.mock import patch
    from engine.readers.grammar import ASTGrammar
    from training.readers.sources import Sources
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'fractional.sqlite'
        connection = sqlite3.connect(path)
        connection.executescript('CREATE TABLE topics(topic_id REAL PRIMARY KEY,description TEXT);'
                                'CREATE TABLE records(record_id INTEGER,topic_id REAL,outcome TEXT);')
        connection.executemany('INSERT INTO topics VALUES (?,?)', [(1.1,'inventory tracking'),
            (9.125,'conference attendance confirmation'),(9.126,'unrelated accounting category')])
        connection.executemany('INSERT INTO records VALUES (?,?,?)', [(1,1.1,'small'),(2,1.1,'medium'),
            (3,1.1,'large'),(4,9.125,'Accepted'),(5,9.125,'Declined'),(6,9.126,'Unrelated')])
        connection.commit(); connection.close()
        source = Sources.__new__(Sources)
        source.db_files = {'fractional': str(path)}
        source.tables = {'fractional': {'table_names_original':['topics','records'],
            'column_names_original':[(-1,'*'),(0,'topic_id'),(0,'description'),(1,'record_id'),(1,'topic_id'),(1,'outcome')],
            'foreign_keys': [(4,1)]}}
        with patch('training.readers.sources.CAP',3):
            base = source.graph('fractional')
        assert Decimal('9.125') in base.column_map[('topics','topic_id')].values
        question = [{'role':'user','content':'Find conference attendance confirmation records.'}]
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        enriched, evidence = source.question_graph('fractional', question, graph=base)
        profile = next(item for item in evidence if item.get('reference_table') == 'topics')
        assert profile['context_values'] == ['9.125']
        json.dumps(evidence)
        literals = ASTGrammar(question,enriched).literals
        assert 'Accepted' in literals and 'Declined' in literals and 'Unrelated' not in literals
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_unit_candidates_are_derived_from_input_mentions_without_selecting_a_unit():
    from engine.readers.grammar import ASTGrammar
    from engine.sql_schema import SchemaGraph
    graph = SchemaGraph.from_tables([{'name': 'events', 'columns': ['duration'], 'rows': [[7]]}], [])
    minute = ASTGrammar('Find durations below five minutes.', graph)
    assert 300 in minute.literals and 60 in minute.literals
    hour = ASTGrammar('Find durations below two hours.', graph)
    assert 7200 in hour.literals and 120 in hour.literals
    temperature = ASTGrammar('Which rows are at 20 degrees Celsius?', graph)
    assert 32 in temperature.literals and 1.8 in temperature.literals
    unrelated = ASTGrammar('Find the five shortest events.', graph)
    assert 300 not in unrelated.literals and 7200 not in unrelated.literals
    clock = SchemaGraph.from_tables([{'name': 'events', 'columns': ['time'],
                                     'rows': [['2013/08/29 05:00:00']]}], [])
    pm = ASTGrammar('At 8/29/2013 at 6:14:01 PM, how many events?', clock)
    assert '2013/08/29 18:14:01' in pm.literals
    assert '2013/08/29 06:14:01' not in pm.literals
    iso_clock = SchemaGraph.from_tables([{'name': 'events', 'columns': ['time'],
                                         'rows': [['2013-08-29 05:00:00']]}], [])
    for question in ('At 14:44:29 on 2012/9/6, how many events?',
                     'At 2012/9/6 14:44:29, how many events?',
                     'On 2012/9/6 at 2:44:29 PM, how many events?'):
        assert '2012-09-06 14:44:29' in ASTGrammar(question, iso_clock).literals
    from engine.readers.numerals import mentions, number
    assert number('1,50') is None and number('97,40') is None
    assert number('1,000') == 1000
    assert [n for n, _, _ in mentions('at coordinate 97,40?')] == [97, 40]
    assert [n for n, _, _ in mentions('at (-102,38).')] == [-102, 38]
    assert [n for n, _, _ in mentions('costs 1,50 euros')] == []
    assert [n for n, _, _ in mentions('August 17, 2018.')] == [17, 2018]
    for text, expected in (
            ('in 2003-2004', [2003, 2004]), ('score 1-8', [1, 8]),
            ('on 2020-12-31', [2020, 12, 31]), ('between -10 and -3', [-10, -3]),
            ('from -10--3', [-10, -3]), ('x = 5--3', [5, -3]),
            ('values 10 -3', [10, -3]), ('values 10, -3', [10, -3])):
        found = list(mentions(text))
        assert [n for n, _, _ in found] == expected, text
        assert all(number(text[start:end]) == value for value, start, end in found)
    halves = ASTGrammar('Average payments in the first half of 2004.', graph)
    assert '2004-01-01' in halves.literals and '2004-07-01' in halves.literals
    last_month = ASTGrammar('Books ordered in the last month of the year 2020.', graph)
    assert '2020-12-01' in last_month.literals and '2021-01-01' in last_month.literals
    assert 'member313' in ASTGrammar('Which group does member 313 belong to?', graph).literals
    assert 'member313' not in ASTGrammar('Which group does member 314 belong to?', graph).literals
    hms = SchemaGraph.from_tables([{'name': 'calls', 'columns': ['duration'], 'rows': [['00:07:32']]}], [])
    assert '00:05:00' in ASTGrammar('Calls served in five minutes or less.', hms).literals
    assert '15:00:00' in ASTGrammar('Calls after 3 pm.', hms).literals
    assert '00:05:00' not in ASTGrammar('Find the five longest calls.', hms).literals
    assert '25:00:00' not in ASTGrammar('Calls after 25 pm.', hms).literals
    ms = SchemaGraph.from_tables([{'name': 'songs', 'columns': ['duration'], 'rows': [['3:45']]}], [])
    assert '4:' in ASTGrammar('Songs about four minutes long.', ms).literals
    assert '4:' not in ASTGrammar('Find the four best songs.', ms).literals
    mdy = SchemaGraph.from_tables([{'name': 'sales', 'columns': ['date'], 'rows': [['12/31/17']]}], [])
    assert '12/%/17' in ASTGrammar('Sales in December 2017.', mdy).literals
    assert '12/%/17' not in ASTGrammar('Sales in December 2017.', graph).literals
    written = SchemaGraph.from_tables([{'name': 'songs', 'columns': ['released'], 'rows': [['28-AUG-2011']]}], [])
    assert '-MAR-2017' in ASTGrammar('Songs released in March 2017.', written).literals
    march = ASTGrammar('Songs released in March.', written)
    assert all(value in march.literals for value in ('Mar', 'MAR', 'mar'))
    assert 'Mar' not in ASTGrammar('Find all songs.', written).literals
    assert 'Mar' not in ASTGrammar('Songs released in March.', graph).literals
    from engine.readers.numerals import intervals
    assert list(intervals('Orders from 2018 to 2020.')) == [(2018, 2020, 12, 24)]
    assert [value[:2] for value in intervals('Between eight and ten.')] == [(8, 10)]
    assert [value[:2] for value in intervals('From -10 to -3.')] == [(-10, -3)]
    assert list(intervals('2018 orders and 2020 customers.')) == []
    span = ASTGrammar('Orders from 2018 to 2020.', mdy)
    assert 2019 in span.literals and '%/%/19' in span.literals
    assert '%/%/19' not in ASTGrammar('Orders in 2018 or 2020.', mdy).literals
    assert 1100 not in ASTGrammar('Orders from 1000 to 1200.', graph).literals
    emails = SchemaGraph.from_tables([{'name': 'clients', 'columns': ['email'], 'rows': [['name@gmail.com']]}], [])
    assert '@gmail.com' in ASTGrammar('Clients with gmail.com email.', emails).literals
    assert '@gmail.com' not in ASTGrammar('All clients.', emails).literals
    ambiguous_comma = ASTGrammar('Find prices over 1,50.', graph)
    assert 150 not in ambiguous_comma.literals
    seventies = ASTGrammar('Who was inducted in the 1970s?', graph)
    assert 1979 in seventies.literals
    relative = ASTGrammar('Find values 30% above the average.', graph)
    assert 1.3 in relative.literals and 0.3 in relative.literals
    teens = ASTGrammar('How many customers are teenagers?', graph)
    assert 13 in teens.literals and 19 in teens.literals
    young = ASTGrammar('How many customers are young?', graph)
    assert 13 not in young.literals


def test_observed_spaced_name_candidates_do_not_match_a_different_name_prefix():
    from engine.readers.grammar import ASTGrammar
    from engine.sql_schema import SchemaGraph
    from engine.sql_ast import SelectQuery, SelectItem, Comparison, Lower, Literal
    rows = [[f'Unrelated Person {i}'] for i in range(1500)]
    rows[1000] = ['Jun Du']; rows[1001] = ['Jun Duan']
    graph = SchemaGraph.from_tables([{'name': 'authors', 'columns': ['name'], 'rows': rows}], [])
    controller = ASTGrammar('List papers that Jundu wrote.', graph)
    assert 'Jun Du' in controller.literals and 'Jun Duan' not in controller.literals
    query = SelectQuery((SelectItem(graph.columns[0].ref),), 'authors',
                        where=Comparison(Lower(graph.columns[0].ref), '=', Literal('jun du')))
    _, realized = controller.teacher(query)
    assert realized.where.right.value == 'jun du'
    codes = SchemaGraph.from_tables([{'name': 'publishers', 'columns': ['pub_id'],
                                     'rows': [['0877'], ['877']]}], [])
    assert codes.columns[0].ref.type.value == 'text'
    controller = ASTGrammar('What is the publisher with ID 877?', codes)
    # Both are actual input codes. Preserve each; the readout must distinguish
    # a formatting alias from an ID collision rather than dropping source bytes.
    assert '0877' in controller.literals and '877' in controller.literals


def test_corpus_audit_sql_signature_keeps_duplicates_order_types_and_large_results():
    from training.readers.semantic_audit import sql_result_signature
    first = sql_result_signature(iter([(1,), (2,), (1,)]))
    reordered = sql_result_signature(iter([(2.0,), (1.0,), (1,)]))
    assert first["multiset"] == reordered["multiset"]
    assert first["ordered"] != reordered["ordered"]
    assert first["multiset"] != sql_result_signature(iter([(1,), (2,)]))["multiset"]
    assert sql_result_signature(iter([(1,)]))["multiset"] != sql_result_signature(iter([("1",)]))["multiset"]
    assert sql_result_signature(iter([(-0.0,)])) == sql_result_signature(iter([(0,)]))
    assert sql_result_signature(iter([(10 ** 36 + 1,)])) != sql_result_signature(iter([(10 ** 36 + 2,)]))
    assert sql_result_signature(((i,) for i in range(20001)))["rows"] == 20001
    values = [(i % 9, str(i % 13)) for i in range(500)]
    assert sql_result_signature(iter(values), spill_keys=3) == sql_result_signature(iter(values), spill_keys=10000)
    try:
        sql_result_signature(iter([(1,), (2,)]), max_rows=1)
    except ValueError:
        pass
    else:
        raise AssertionError("audit accepted an incomplete result beyond its explicit row budget")


def test_explicit_numeric_source_review_requires_a_unique_unnegated_bound():
    from engine.sql_ast import SelectQuery, SelectItem, Comparison, Literal, query_to_tree
    from training.readers.semantic_audit import explicit_constraint_issues
    graph = SchemaGraph.from_tables([{'name': 'items', 'columns': ['price'], 'rows': [[10]]}], [])
    column = graph.columns[0].ref
    tree = query_to_tree(SelectQuery((SelectItem(column),), 'items', where=Comparison(column, '<', Literal(1000))))
    turns = lambda text: [{'role': 'user', 'content': text}]
    found = explicit_constraint_issues(turns('Find prices no more than 1,000.'), tree)
    assert len(found) == 1 and found[0]['expected_operator'] == '<='
    assert explicit_constraint_issues(turns('Find prices below 1,000.'), tree) == []
    assert explicit_constraint_issues(turns('Find prices not more than 1,000.'), tree) == []
    assert explicit_constraint_issues(turns('Did not get more than 1,000.'), tree) == []
    assert explicit_constraint_issues(turns('Equal or below 1,000.'), tree)[0]['expected_operator'] == '<='
    assert explicit_constraint_issues(turns('Price over 1,000 or count under 1,000.'), tree) == []
    from engine.sql_ast import SetQuery
    first = SelectQuery((SelectItem(column),), 'items')
    rejected = SelectQuery((SelectItem(column),), 'items', where=Comparison(column, '>=', Literal(1000)))
    complement = query_to_tree(SetQuery(first, 'EXCEPT', rejected))
    assert explicit_constraint_issues(turns('Find items always below 1,000.'), complement) == []
    from engine.sql_ast import BinaryExpr, Aggregate
    assert explicit_constraint_issues(turns('Find prices no more than one thousand.'), tree)[0]['expected_operator'] == '<='
    for expression in (BinaryExpr(column, '*', Literal(100)), Aggregate('SUM', column), Aggregate('AVG', column)):
        clause = 'having' if isinstance(expression, Aggregate) else 'where'
        scaled = query_to_tree(SelectQuery((SelectItem(expression),), 'items',
                               **{clause: Comparison(expression, '>', Literal(300000))}))
        found = explicit_constraint_issues(turns('Find 300000 sales and above.'), scaled)
        assert len(found) == 1 and found[0]['expected_operator'] == '>='
        assert explicit_constraint_issues(turns('Find sales above 300000.'), scaled) == []
        assert explicit_constraint_issues(turns('Find sales not above 300000.'), scaled) == []
    from engine.sql_ast import ColumnRef, SQLType
    text_column = ColumnRef('codes', 'code', SQLType.TEXT)
    text_tree = query_to_tree(SelectQuery((SelectItem(text_column),), 'codes',
                                          where=Comparison(text_column, '<', Literal('1000'))))
    assert explicit_constraint_issues(turns('Find codes no more than one thousand.'), text_tree) == []


def test_explicit_interval_source_review_keeps_columns_scopes_and_negation_separate():
    from engine.sql_ast import (BooleanExpr, ColumnRef, Comparison, ExistsPredicate, Literal,
                                SelectItem, SelectQuery, SQLType, query_to_tree)
    from training.readers.semantic_audit import explicit_constraint_issues
    year = ColumnRef('works', 'year', SQLType.INTEGER)
    other = ColumnRef('works', 'price', SQLType.INTEGER)
    turns = lambda text: [{'role': 'user', 'content': text}]
    def bounds(low, high, second=year):
        return BooleanExpr('AND', (Comparison(year, '>=', Literal(low)), Comparison(second, '<=', Literal(high))))
    def check(text, predicate):
        tree = query_to_tree(SelectQuery((SelectItem(year),), 'works', where=predicate))
        return explicit_constraint_issues(turns(text), tree)
    found = check('List works from 1900 to 2000.', bounds(1500, 2000))
    assert len(found) == 1 and found[0]['family'] == 'explicit_numeric_interval_disagreement'
    assert found[0]['expected_bounds'] == ['1900', '2000']
    assert found[0]['source_bounds'] == ['1500', '2000']
    assert check('List works between 1900 and 2000.', bounds(1900, 2000)) == []
    equivalent = BooleanExpr('AND', (Comparison(year, '>', Literal(1899)), Comparison(year, '<', Literal(2001))))
    assert check('List works from 1900 to 2000.', equivalent) == []
    fractional = BooleanExpr('AND', (Comparison(year, '>', Literal(1899.5)), Comparison(year, '<', Literal(2000.5))))
    assert check('List works from 1900 to 2000.', fractional) == []
    assert check('List works from one thousand nine hundred to two thousand.', bounds(1900, 2000)) == []
    assert check('List works from 1900 to 2000.', bounds(1500, 2000, other)) == []
    assert check('List works in 1900 and prices 2000.', bounds(1500, 2000)) == []
    assert check('List works from 1900 to 2000 and prices from 20 to 30.', bounds(1500, 2000)) == []
    assert check('Compare works in 1971 to 1980 with papers from 1991 to 2000.', bounds(1971, 1980)) == []
    assert check('List works from 2000 to 1900.', bounds(1500, 2000)) == []
    assert check('List works not from 1900 to 2000.', bounds(1500, 2000)) == []
    disjunction = BooleanExpr('OR', (Comparison(year, '>=', Literal(1500)), Comparison(year, '<=', Literal(2000))))
    assert check('List works from 1900 to 2000.', disjunction) == []
    inner = SelectQuery((SelectItem(year),), 'works', where=Comparison(year, '<=', Literal(2000)))
    mixed = BooleanExpr('AND', (Comparison(year, '>=', Literal(1500)), ExistsPredicate(inner)))
    assert check('List works from 1900 to 2000.', mixed) == []
    excluded = SelectQuery((SelectItem(year),), 'works', where=bounds(1500, 2000))
    assert check('List works from 1900 to 2000.', ExistsPredicate(excluded, negated=True)) == []
    multiple = BooleanExpr('AND', (bounds(1500, 2000),
                        BooleanExpr('AND', (Comparison(other, '>=', Literal(20)), Comparison(other, '<=', Literal(30))))))
    assert check('List works from 1900 to 2000.', multiple) == []


def test_failed_native_parser_proof_never_populates_the_audit_cache():
    from training.readers.semantic_audit import verified_program_document
    from engine.sql_ast import SelectQuery, SelectItem, query_to_tree
    graph = SchemaGraph.from_tables([{'name': 'items', 'columns': ['n'], 'rows': [[7]]}], [])
    tree = query_to_tree(SelectQuery((SelectItem(graph.columns[0].ref),), 'items'))
    cache, calls = {}, []
    def native(*args):
        calls.append(args[0])
        if len(calls) == 1:
            raise ValueError('injected native rejection')
    try:
        verified_program_document('first', tree, cache=cache, validate_document=lambda d: None, native_check=native)
    except ValueError:
        pass
    else:
        raise AssertionError('a native rejection passed')
    assert cache == {}
    checked = verified_program_document('second', tree, cache=cache, validate_document=lambda d: None, native_check=native)
    assert len(cache) == 1 and calls == ['first', 'second']
    assert verified_program_document('third', tree, cache=cache, validate_document=lambda d: None, native_check=native) == checked
    assert calls == ['first', 'second']


def test_teacher_validation_without_records_preserves_choices_and_resets_after_failure():
    from engine.readers.grammar import ASTGrammar
    from engine.readers.decisions import InvalidDecision
    from engine.sql_ast import SelectQuery, SelectItem, Comparison, Literal, query_to_tree
    graph = SchemaGraph.from_tables([{'name': 'items', 'columns': ['name'], 'rows': [['Known']]}], [])
    query = SelectQuery((SelectItem(graph.columns[0].ref),), 'items',
                        where=Comparison(graph.columns[0].ref, '=', Literal('Known')))
    controller = ASTGrammar('Show Known.', graph)
    records, expected = controller.teacher(query, capture=False)
    assert records
    empty, actual = controller.teacher(query, capture=False, collect=False)
    assert empty == [] and query_to_tree(actual) == query_to_tree(expected)
    missing = SelectQuery(query.select, 'items', where=Comparison(graph.columns[0].ref, '=', Literal('Unobserved')))
    for collect in (True, False):
        try:
            controller.teacher(missing, capture=False, collect=collect)
        except InvalidDecision as exc:
            assert exc.missing_binding['value'] == 'Unobserved'
        else:
            raise AssertionError('record suppression accepted an ungrounded target')
    records, recovered = controller.teacher(query, capture=False)
    assert records and query_to_tree(recovered) == query_to_tree(expected)


TESTS = [
    test_small_source_reference_tables_expose_code_meanings_with_explicit_bounds,
    test_source_grouping_expands_only_columns_proved_by_nonnull_primary_keys,
    test_publisher_definitions_are_visible_input_candidates_and_keep_source_hashes,
    test_generated_dialogues_preserve_source_join_keys_and_only_declared_corrections,
    test_corpus_parents_and_encoder_bank_share_actual_physical_schema_evidence,
    test_recovered_database_versions_pin_both_inputs_and_select_atomically,
    test_teacher_validation_without_records_preserves_choices_and_resets_after_failure,
    test_failed_native_parser_proof_never_populates_the_audit_cache,
    test_explicit_numeric_source_review_requires_a_unique_unnegated_bound,
    test_explicit_interval_source_review_keeps_columns_scopes_and_negation_separate,
    test_observed_spaced_name_candidates_do_not_match_a_different_name_prefix,
    test_unit_candidates_are_derived_from_input_mentions_without_selecting_a_unit,
    test_question_source_retrieval_uses_input_phrases_beyond_cap_without_writing,
    test_input_reference_profiles_expose_real_categorical_rows_without_target_hints,
    test_input_reference_profiles_bind_decimal_keys_without_float_rounding,
    test_compact_observed_names_survive_repeated_long_matches_without_inventing_aliases,
    test_offline_type_repair_preserves_physical_bindings_and_derived_scopes,
    test_empty_sql_source_columns_keep_declared_numeric_types_and_foreign_keys,
    test_recursive_literal_bank_reads_related_cells_from_matching_source_rows,
    test_corpus_audit_sql_signature_keeps_duplicates_order_types_and_large_results,
    test_semantic_zstandard_cache_iterates_binary_lines_and_text_without_changes,
    test_joint_source_summaries_keep_full_question_spans_and_original_cells,
    test_semantic_disk_cache_preserves_exact_records_and_excludes_test_from_fit,
    test_named_qualification_requires_complete_held_out_role_evidence,
    test_question_spans_numbers_and_calendar_literals_use_only_input_evidence,
    test_parallel_grammar_input_has_no_target_prefix_and_static_columns_ignore_the_question,
    test_one_prefill_named_query_replays_full_ast_without_an_encoder_or_residual_score,
    test_frozen_semantic_features_preserve_paired_static_logits_gradients_and_input_identity,
    test_named_pair_training_uses_the_shared_projection_and_masks_unknown_gradients,
    test_recursive_schema_readout_covers_aliases_derived_sources_arithmetic_and_ace,
    test_recursive_teacher_exposes_only_previous_choices_and_named_role_targets,
    test_named_field_readout_replays_role_binding_and_rejects_unknown_or_tampered_values,
    test_named_head_exports_only_coordinates_calibrated_for_each_role,
    test_qwen_unit_program_uses_existing_importer_and_rejects_untyped_filter_values,
    test_qwen_original_source_restores_types_only_after_exact_pair_verification,
    test_qwen_graph_source_recovery_pairs_sql_units_and_preserves_literal_identity,
    test_generated_dialogue_annotations_check_program_deltas_and_do_not_infer_unknown_controls,
    test_sql_semantic_labels_restore_canonical_default_direction_and_join_kind,
    test_semantic_evaluation_keeps_padding_masks_roles_and_serial_batch_agreement,
    test_semantic_diagnostics_expose_a_sparse_zero_predictor_and_missing_roles,
    test_semantic_examples_and_batches_keep_gold_and_unknowns_outside_inputs,
    test_semantic_prefill_static_profiles_cannot_read_the_question_or_physical_ids,
    test_named_coordinate_contract_covers_215_explicit_migrated_targets,
    test_sql_semantic_labels_keep_complete_sql_flags_and_scoped_occurrences,
    test_sql_semantic_labels_distinguish_nulls_counts_and_nested_scope,
    test_named_head_has_no_hidden_residual_path_to_binding,
    test_named_head_masks_unqualified_and_unobserved_predictions,
    test_semantic_training_masks_unknown_labels_in_loss_and_gradient,
    test_named_binding_supplies_an_input_choice_to_the_existing_compiler,
    test_named_operator_coordinates_causally_change_the_existing_compiler_and_execution,
    test_scoped_operator_labels_preserve_default_directions_and_distinct_occurrences,
    test_scoped_shared_head_prefill_and_targets_keep_inactive_operations_unknown,
    test_named_binding_cannot_fill_an_ambiguous_choice_or_omit_option_evidence,
    test_joint_score_validation_preserves_logits_before_a_grammar_mask,
    test_ace_encoder_enforces_its_literal_document_and_variable_bounds,
    test_ace_profile_preserves_source_programs_wider_than_the_neural_head,
    test_explicit_ace_joins_preserve_two_different_relationships_between_the_same_tables,
    test_explicit_ace_joins_preserve_left_join_and_every_composite_key_column,
    test_explicit_ace_joins_reject_invalid_scope_and_never_infer_a_replacement_path,
    test_joint_decoder_rejects_invalid_asts_and_explores_valid_choices,
    test_joint_decoder_accepts_bounded_api_rounding_and_preserves_its_argmax,
    test_ace_exposes_compiler_normalization_and_not_unused_tables,
    test_ace_rejects_a_share_whose_grouping_the_compiler_would_discard,
    test_joint_decisions_canonicalize_table_order_and_ground_empty_values,
    test_joint_candidates_are_scored_without_answers_and_are_repeatable,
    test_joint_search_can_reach_a_valid_column_beyond_the_first_three_choices,
    test_joint_decisions_use_input_derived_options_and_the_existing_builder,
    test_joint_decisions_fail_closed_and_can_request_clarification,
    test_ace_view_roundtrips_meaning_and_rejects_unused_facts,
    test_ace_nested_query_keeps_typed_literals_and_block_references,
    test_joint_prefill_shares_options_and_never_truncates_state,
    test_a_table_read_from_nowhere_does_not_need_a_join,
    test_near_copies_of_an_export_are_one_layout_and_the_first_is_read,
    test_the_tab_on_screen_is_the_copy_a_question_reads,
    test_a_value_spelled_almost_as_read_is_the_stored_value,
    test_training_candidates_are_the_pool_serving_ranks,
    test_reader_training_reads_no_dev_or_test_question,
    test_a_question_past_half_the_budget_is_read_as_far_as_it_fits,
    test_a_listed_value_cut_short_reads_as_the_stored_value,
    test_the_readers_compute_in_float32_over_weights_kept_in_bfloat16,
    test_readings_round_trip_through_the_builder,
    test_readings_hold_only_the_operators_the_readers_read,
    test_a_grouped_block_groups_what_it_shows,
    test_a_group_is_ordered_by_its_total,
    test_groups_with_no_aggregate_are_distinct_rows,
    test_a_share_says_its_part_and_its_whole,
    test_two_named_values_of_one_column_read_either,
    test_a_named_period_groups_by_the_period,
    test_a_named_period_groups_the_date_when_the_readers_group_by_a_row_key,
    test_a_set_membership_compares_values_of_one_kind,
    test_every_column_is_the_star_shown_plain,
    test_a_number_column_compares_with_numbers_and_never_with_its_own_values,
    test_a_membership_of_two_kinds_is_read_on_the_column_its_values_reference,
    test_the_highest_of_the_one_ranked_column_is_its_max,
    test_a_share_divides_the_part_by_the_whole,
    test_a_share_keeps_the_whole_conditions_in_both,
    test_a_share_needs_one_aggregate,
    test_a_contained_text_matches_in_any_case,
    test_a_pinned_column_is_not_shown_again,
    test_a_matched_column_is_not_counted_distinct,
    test_a_count_of_a_referencing_column_counts_each_row_it_names_once,
    test_a_date_condition_reads_the_days_the_question_names,
    test_a_stored_date_keeps_its_exact_value,
    test_a_listed_date_is_not_the_question_s_stored_form,
    test_a_date_range_is_not_read_under_or,
    test_a_pointed_value_drops_the_marks_of_its_list,
    test_the_question_values_are_listed_beside_their_columns,
    test_the_samples_go_before_a_table,
    test_the_choice_ranks_by_agreement_and_refuses_below_its_threshold,
    test_a_reader_that_reads_nothing_is_no_dissent,
    test_candidates_are_distinct_and_most_probable_first,
    test_choices_override_by_rank,
    test_built_structures_exclude_and_rank_by_the_total,
]


# CI's hermetic job installs no model stack; the engine image does.
NEEDS_TORCH = {test_named_qualification_requires_complete_held_out_role_evidence,
               test_frozen_semantic_features_preserve_paired_static_logits_gradients_and_input_identity,
               test_semantic_zstandard_cache_iterates_binary_lines_and_text_without_changes,
               test_joint_source_summaries_keep_full_question_spans_and_original_cells,
               test_semantic_disk_cache_preserves_exact_records_and_excludes_test_from_fit,
               test_parallel_grammar_input_has_no_target_prefix_and_static_columns_ignore_the_question,
               test_one_prefill_named_query_replays_full_ast_without_an_encoder_or_residual_score,
               test_named_pair_training_uses_the_shared_projection_and_masks_unknown_gradients,test_the_readers_compute_in_float32_over_weights_kept_in_bfloat16,
               test_scoped_shared_head_prefill_and_targets_keep_inactive_operations_unknown,
               test_named_head_exports_only_coordinates_calibrated_for_each_role,
               test_semantic_evaluation_keeps_padding_masks_roles_and_serial_batch_agreement,
               test_semantic_diagnostics_expose_a_sparse_zero_predictor_and_missing_roles,
               test_semantic_examples_and_batches_keep_gold_and_unknowns_outside_inputs,
               test_semantic_prefill_static_profiles_cannot_read_the_question_or_physical_ids,
               test_named_head_has_no_hidden_residual_path_to_binding,
               test_named_head_masks_unqualified_and_unobserved_predictions,
               test_semantic_training_masks_unknown_labels_in_loss_and_gradient}


def main():
    import importlib.util

    failed, skipped = [], 0
    for test in TESTS:
        if test in {test_semantic_zstandard_cache_iterates_binary_lines_and_text_without_changes,
                    test_frozen_semantic_features_preserve_paired_static_logits_gradients_and_input_identity} and importlib.util.find_spec("zstandard") is None:
            skipped += 1
            print(f"  SKIP {test.__name__}: needs the optional Zstandard training-data codec")
            continue
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
    print(f"\nreaders: {len(TESTS) - len(failed) - skipped} passed, {len(failed)} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
