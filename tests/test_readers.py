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
from engine.readers.text import Schema, reading_text
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


def test_the_choice_ranks_by_agreement_and_refuses_below_its_threshold():
    """The readers' choice (choice.json) weighs the re-scorer's logit with what the pool says of a candidate; of two
    candidates the re-scorer scores alike, the one more of the readers' readings build ranks first (calibration,
    four readers: 642 of 1,028 chosen right against 628 for the re-scorer alone). Its probability is the confidence,
    and a choice under the threshold is refused."""
    import math

    from engine.readers import search
    from engine.sql_candidate import ScoredQuery

    readers = search.ReadingSearch.__new__(search.ReadingSearch)      # no weights: the logits are memoized below
    readers.choice = {"features": list(search.CHOICE_FEATURES), "coef": [0.4, -0.3, -0.2, 1.5, 0.4],
                      "intercept": -1.5, "threshold": 0.3}
    pool = [ScoredQuery(None, "SELECT 1", -1.0, ("one",), (("rank", 0.0), ("readers", 0.25))),
            ScoredQuery(None, "SELECT 2", -1.0, ("two",), (("rank", 0.0), ("readers", 1.0))),
            ScoredQuery(None, "SELECT 3", -2.0, ("three",), (("rank", 3.0), ("readers", 0.25)))]
    shapes = [(1, 1), (1, 1), None]
    search._keep(search._SCORES, ("q", ("SELECT 1", "SELECT 2"), ((1, 1), (1, 1))), (2.0, 2.0))
    scores = readers.rescore("q", pool, shapes)
    assert scores[2] is None and scores[1] > scores[0], scores               # agreement breaks the tie
    expected = -1.5 + sum(w * x for w, x in zip(readers.choice["coef"], search.choice_features(2.0, -1.0, 0.0, 1.0, 1)))
    assert abs(scores[1] - expected) < 1e-12
    confidence = readers.confidence(pool, scores, shapes, 1)
    assert abs(confidence - 1.0 / (1.0 + math.exp(-scores[1]))) < 1e-12
    assert readers.confident(confidence) == (confidence >= 0.3) and readers.confidence(pool, scores, shapes, 2) is None
    assert not readers.confident(0.29) and readers.confident(None)
    readers.choice = {**readers.choice, "threshold": None}                   # an evaluation records every confidence
    assert readers.confident(0.01)


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
    category's number of rows; a question must state the value and the number."""
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


TESTS = [
    test_readings_round_trip_through_the_builder,
    test_a_grouped_block_groups_what_it_shows,
    test_a_group_is_ordered_by_its_total,
    test_groups_with_no_aggregate_are_distinct_rows,
    test_a_share_says_its_part_and_its_whole,
    test_two_named_values_of_one_column_read_either,
    test_a_named_period_groups_by_the_period,
    test_a_named_period_groups_the_date_when_the_readers_group_by_a_row_key,
    test_the_highest_of_the_one_ranked_column_is_its_max,
    test_a_share_divides_the_part_by_the_whole,
    test_a_share_keeps_the_whole_conditions_in_both,
    test_a_share_needs_one_aggregate,
    test_a_contained_text_matches_in_any_case,
    test_a_pinned_column_is_not_shown_again,
    test_a_matched_column_is_not_counted_distinct,
    test_a_date_condition_reads_the_days_the_question_names,
    test_a_stored_date_keeps_its_exact_value,
    test_a_date_range_is_not_read_under_or,
    test_a_pointed_value_drops_the_marks_of_its_list,
    test_the_question_values_are_listed_beside_their_columns,
    test_the_choice_ranks_by_agreement_and_refuses_below_its_threshold,
    test_candidates_are_distinct_and_most_probable_first,
    test_choices_override_by_rank,
    test_built_structures_exclude_and_rank_by_the_total,
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
    print(f"\nreaders: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
