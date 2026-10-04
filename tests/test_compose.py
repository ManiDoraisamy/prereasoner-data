"""Deterministic, infra-free unit tests for the ComposeEngine view-stack reasoner.

Runs ComposeEngine(reader=None) — the encoder-free regex/value-matching fallback — over synthetic tables, so there
is NO Postgres, NO model load, and NO API key. Focus: the PLAIN input-column value filter ('total amount in Chennai'
-> WHERE city='Chennai', no world model) added alongside the world-attribute filter, plus guards that it doesn't
fire spuriously or regress the world path.

Run:  python -m tests.test_compose
"""
from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal

from engine.compose import ComposeEngine
from engine.joins import discover_fks, join_plan
from engine.knowledge_query import content_words, semantic_predicate, verify_nonempty
from engine.numeric import register_sqlite_decimal
from engine.primitives import filter_view


ORDERS = {"name": "orders", "columns": ["city", "amount"],
          "rows": [["Paris", 100], ["Lyon", 80], ["Chennai", 90], ["Chennai", 60]]}
# The world-meaning table ComposeEngine joins: geo value (col 0) -> attributes (country, ...).
WORLD = {"name": "knowledgebase facts", "columns": ["city", "country"],
         "rows": [["Paris", "France"], ["Lyon", "France"], ["Chennai", "India"]]}

# A STRING foreign key (the sample demo): orders.customer holds a NAME, not a number, and references
# customers.name. The amount lives in orders; the city (needed for the world query) lives in customers.
CUSTOMERS = {"name": "customers", "columns": ["customer ID", "name", "city"],
             "rows": [[1, "Holmes", "London"], [2, "Clouseau", "Paris"], [3, "Lupin", "Paris"]]}
ORDERS_FK = {"name": "orders", "columns": ["order ID", "customer", "ordered", "amount"],
             "rows": [[101, "Holmes", "Pipe", 100], [102, "Clouseau", "Coat", 310],
                      [103, "Lupin", "Hat", 180], [104, "Holmes", "Cap", 20]]}
WORLD2 = {"name": "knowledgebase facts", "columns": ["city", "country"],
          "rows": [["London", "UK"], ["Paris", "France"]]}


def _run(question, world=WORLD, tables=(ORDERS,)):
    return ComposeEngine(reader=None).run([dict(t) for t in tables], question, world=world)


# Cities with their sales, and the populations the knowledgebase lookup supplies for them: Tokyo, Osaka and
# Nagoya have over 1,000,000 people, Lyon and Marseille do not.
CITY_SALES = {"name": "s", "columns": ["city", "sales"],
              "rows": [["Tokyo", 100], ["Osaka", 200], ["Nagoya", 50], ["Lyon", 30], ["Marseille", 40]]}
CITY_ORDERS = {"name": "c", "columns": ["city", "orders"],
               "rows": [["Tokyo", 1], ["Osaka", 1], ["Nagoya", 1], ["Lyon", 1], ["Marseille", 1]]}
CITY_WORLD = {"name": "knowledgebase facts", "columns": ["city", "country", "population"],
              "rows": [["Tokyo", "Japan", 14264798], ["Osaka", "Japan", 2751862], ["Nagoya", "Japan", 2326844],
                       ["Lyon", "France", 519127], ["Marseille", "France", 886040]]}


def _steps(run):
    return [(step["op"], step.get("conds", step.get("by"))) for step in run["bindings"]["steps"]]


def test_named_input_value_filters_directly_without_world_model():
    # 'in Chennai' is a value that already lives in the city column -> filter city='Chennai' DIRECTLY. No world
    # join/filter needed (the whole point: the value is in the upload). Chennai rows = 90 + 60 = 150.
    r = _run("total amount in Chennai", world=None)
    assert r["plan"] == ["filter", "group_agg"], r["plan"]
    assert r["answer"]["rows"] == [[150.0]], r["answer"]


def test_named_input_value_filters_even_when_world_available():
    # Same query WITH a world table present: still a plain city filter (NOT a spurious world join), still 150.
    r = _run("total amount in Chennai")
    assert r["plan"] == ["filter", "group_agg"], r["plan"]
    assert r["answer"]["rows"] == [[150.0]], r["answer"]


def test_regression_the_named_filter_is_not_dropped():
    # Before the fix, 'total amount in Chennai' bound NO filter and summed EVERY row (100+80+90+60=330). Assert the
    # constraint is honored now: the answer is Chennai's 150, never the grand total 330.
    r = _run("total amount in Chennai", world=None)
    assert r["answer"]["rows"] != [[330.0]], "the 'in Chennai' filter was dropped -> summed all rows"


def test_country_still_uses_the_world_path_unchanged():
    # 'France' is NOT a value in the city column, so the plain filter must NOT fire; the world path resolves the
    # cities to their country and filters there. Plan is the world stack, France total = Paris 100 + Lyon 80 = 180.
    r = _run("total amount in France")
    assert r["plan"] == ["world_join", "world_filter", "group_agg"], r["plan"]
    assert r["answer"]["rows"] == [[180.0]], r["answer"]


def test_no_named_value_does_not_invent_a_filter():
    # A bare aggregate names no value -> NO filter step, grand total over all rows (330).
    r = _run("total amount", world=None)
    assert r["plan"] == ["group_agg"], r["plan"]
    assert r["answer"]["rows"] == [[330.0]], r["answer"]


def test_structural_query_word_never_self_matches_a_cell():
    # A summary-style 'Total' cell must not let the word 'total' in 'total amount' filter on itself (the _VALUE_STOP
    # guard). Expect the grand total across BOTH rows (5+7=12), not a filter on kind='Total'.
    t = {"name": "t", "columns": ["kind", "amount"], "rows": [["Total", 5], ["Line", 7]]}
    r = _run("total amount", world=None, tables=(t,))
    assert "filter" not in r["plan"], r["plan"]
    assert r["answer"]["rows"] == [[12.0]], r["answer"]


def test_deterministic_across_repeated_runs():
    a = _run("total amount in Chennai", world=None)
    b = _run("total amount in Chennai", world=None)
    assert a["plan"] == b["plan"] and a["answer"] == b["answer"]


def test_string_column_is_discovered_as_a_foreign_key():
    # A foreign key is a referential INCLUSION, not a numeric type. orders.customer (a NAME) is included in
    # the unique customers.name key, so it IS a foreign key despite the column names differing. The compose
    # detector must agree with the AST planner here (one shared detector) — it did NOT before this fix.
    fks = discover_fks([CUSTOMERS, ORDERS_FK])
    assert ("orders", "customer", "customers", "name") in fks, fks


def test_string_fk_joins_uploaded_tables_for_a_world_query():
    # The demo: 'total amount in France' needs orders.amount JOINED to customers.city, then city -> country.
    # With the string FK joined, the base carries amount+city and France = Paris = Clouseau 310 + Lupin 180 = 490.
    # If the FK were dropped (the old bug), orders alone has no city -> no world join -> the wrong grand total.
    r = ComposeEngine(reader=None).run([dict(CUSTOMERS), dict(ORDERS_FK)], "total amount in France", world=dict(WORLD2))
    assert r["plan"][0] == "join", r["plan"]                     # the FK join happened (table not dropped)
    assert "world_join" in r["plan"] and "world_filter" in r["plan"], r["plan"]
    assert r["answer"]["rows"] == [[490.0]], r["answer"]         # amount survived the join AND the world filter


def test_non_unique_parent_is_not_a_spurious_foreign_key():
    # The guard against 'any two tables that share string values join': a candidate parent column that REPEATS
    # is not a unique key, so it is NOT an FK target even under full value inclusion. No key -> no join.
    a = {"name": "a", "columns": ["tag", "amount"], "rows": [["x", 1], ["y", 2], ["x", 3]]}
    b = {"name": "b", "columns": ["tag", "note"], "rows": [["x", "p"], ["y", "q"], ["x", "r"]]}  # tag repeats
    assert discover_fks([a, b]) == [], discover_fks([a, b])
    assert join_plan([a, b], discover_fks([a, b])) is None


def test_no_name_signal_column_is_not_a_foreign_key():
    # A foreign key needs NAME evidence, not merely value inclusion (adversarial-verification finding). A
    # REPEATING measure / flag / low-cardinality categorical column whose distinct values coincidentally fall
    # inside an unrelated UNIQUE key must NOT be faked into an FK — all three below carry zero name signal.
    measure = {"name": "order_items", "columns": ["item_id", "qty"],
               "rows": [[1, 1], [2, 2], [3, 3], [4, 2], [5, 1], [6, 3]]}       # qty (a measure) subset of wh_id 1..4
    warehouse = {"name": "warehouse", "columns": ["wh_id", "location"], "rows": [[1, "n"], [2, "s"], [3, "e"], [4, "w"]]}
    assert discover_fks([measure, warehouse]) == [], discover_fks([measure, warehouse])
    flag = {"name": "users", "columns": ["user_id", "is_active"], "rows": [[1, 1], [2, 0], [3, 1], [4, 1], [5, 0]]}
    bit = {"name": "bit_lookup", "columns": ["bit_id", "meaning"], "rows": [[0, "off"], [1, "on"]]}   # 0/1 flag ⊆ {0,1}
    assert discover_fks([flag, bit]) == [], discover_fks([flag, bit])
    cat = {"name": "tickets", "columns": ["ticket_id", "severity"],
           "rows": [["t1", "Low"], ["t2", "High"], ["t3", "High"], ["t4", "Med"]]}                    # categorical ⊆ level
    pri = {"name": "priorities", "columns": ["level", "sla"], "rows": [["Low", 72], ["Med", 24], ["High", 4]]}
    assert discover_fks([cat, pri]) == [], discover_fks([cat, pri])


def test_tabs_with_the_same_columns_are_not_foreign_keys_of_each_other():
    # A customer's Keyword Stats workbook (2026-10-02): the Checklist and Inspection tabs are two exports of
    # one keyword-planner layout, and ingest linked Checklist."Avg. monthly searches" to the unique values of
    # Inspection."Avg. monthly searches", a measure, on the identical name alone. Tabs with the same columns are
    # siblings, never parent and child.
    columns = ["Keyword", "Avg. monthly searches", "Competition"]
    checklist = {"name": "Checklist", "columns": columns,
                 "rows": [["safety checklist", 5000, "Low"], ["forklift inspection checklist", 5000, "High"],
                          ["vehicle inspection checklist", 500, "Medium"], ["daily checklist", 500, "Low"]]}
    inspection = {"name": "Inspection", "columns": columns,
                  "rows": [["forklift inspection", 500, "Medium"], ["vehicle inspection", 5000, "Medium"],
                           ["home inspection", 50000, "High"]]}
    assert discover_fks([checklist, inspection]) == [], discover_fks([checklist, inspection])
    # Contrast: a same-named code column between differently shaped tables is still a reference.
    products = {"name": "products", "columns": ["sku", "title"], "rows": [["A1", "Pen"], ["B2", "Ink"], ["C3", "Pad"]]}
    sales = {"name": "sales", "columns": ["order", "sku", "qty"], "rows": [[1, "A1", 2], [2, "B2", 1], [3, "A1", 5]]}
    assert ("sales", "sku", "products", "sku") in discover_fks([products, sales]), discover_fks([products, sales])


def test_name_signaled_foreign_keys_still_resolve():
    # Contrastive: the name-signal requirement must NOT over-reject real FKs. An id-named FK (shops.city_id ->
    # cities.id) and the relationship-named STRING FK (orders.customer -> customers.name) both carry name evidence.
    cities = {"name": "cities", "columns": ["id", "city"], "rows": [[1, "Paris"], [2, "Lyon"], [3, "Nice"]]}
    shops = {"name": "shops", "columns": ["shop_id", "city_id", "rev"], "rows": [[9, 1, 5], [8, 2, 7], [7, 1, 3]]}
    assert ("shops", "city_id", "cities", "id") in discover_fks([cities, shops]), discover_fks([cities, shops])
    assert ("orders", "customer", "customers", "name") in discover_fks([CUSTOMERS, ORDERS_FK])



def _agg_result(rows, functions=("SUM",), columns=("sum",)):
    """The serve() payload shape as it exists BEFORE provenance decoration: the typed planner has
    published aggregate evidence in `computation`, but `column_provenance` is not attached yet."""
    return {"question": "q", "sql": "SELECT SUM(...)", "result": {"columns": list(columns), "rows": rows},
            "computation": {"verified": True, "source": "typed_ast", "branches": [
                {"outputs": [{"numeric": True, "aggregate_functions": list(functions)}]}]}}


def test_aggregate_over_zero_rows_is_not_presented_as_an_answer():
    """Regression for an OBSERVED silent wrong answer (2026-09-08). SUM over an empty relation is one
    all-NULL row, which rendered as [['']] and was returned with no clarify -- 'total budget in
    Africa' on a dataset with no African row looked exactly like a real answer. That blank is what
    made a mis-routed world join fail silently instead of visibly."""
    out = verify_nonempty(_agg_result([[""]]), "total budget in Africa")
    assert out.get("clarify") is True, f"an empty aggregate must not be an answer, got {out}"
    assert out.get("result") is None, f"the blank result must not survive, got {out}"
    # The reason is the reply a user reads: the aggregate in their words, not "SUM".
    assert "no total" in (out.get("reason") or ""), f"the reason must name the aggregate, got {out}"
    assert "SUM" not in out["reason"], f"the reason must be written for a reader, got {out}"


def test_real_aggregates_and_plain_selects_are_untouched():
    """The gate must be narrow: only an ALL-empty single row whose every output is typed as an
    aggregate. A real total, a genuine zero COUNT, a plain SELECT, and a multi-row result all pass."""
    unchanged = {
        "a real total": (_agg_result([[113000]]), "total budget"),
        "a genuine zero count": (_agg_result([[0]], functions=("COUNT",), columns=("count",)),
                                 "how many leads in Africa"),
        "a multi-row aggregate": (_agg_result([[1], [2]]), "budget by country"),
        # No aggregate evidence -> never second-guessed, even though the single cell is empty.
        "a plain select of an empty cell": ({"question": "q", "result": {"columns": ["remark"], "rows": [[""]]},
                                             "computation": {"branches": [{"outputs": [{"aggregate_functions": []}]}]}},
                                            "the remark for Bo"),
        "a payload with no computation": ({"question": "q", "result": {"columns": ["x"], "rows": [[""]]}}, "x"),
    }
    for label, (payload, question) in unchanged.items():
        out = verify_nonempty(payload, question)
        assert out is payload, f"{label} must pass through the gate untouched, got {out}"


def test_words_that_name_the_sheet_leave_nothing_to_search_for():
    """Production, 2026-09-27: 'amount in france' left the word 'amount' after France was resolved, and
    that column name became a free-text search. The semantic path ranked France's orders by similarity to
    the word, capped them at ten, and showed one opaque step instead of the lookup and the filter. A search
    needs a word about the rows' content; the sheet's own vocabulary is not one."""
    sales = [{"table": "sales", "name": name} for name in
             ("order ID", "customer", "city", "tier", "ordered", "currency", "amount")]
    for question in ("amount in france", "customers in France", "orders in france", "sales in france",
                     "rows in france", "amounts and cities in France"):
        assert semantic_predicate(question, ["France"], sales) == "", question
    # Same sheet, words about the rows' content: the search keeps the question's words, including the
    # ones that also name a column, so the embedding sees the whole phrase.
    assert semantic_predicate("trench coat orders in france", ["France"], sales) == "trench coat orders"
    remarks = [{"table": "orders", "name": "remarks"}, {"table": "orders", "name": "city"}]
    assert semantic_predicate("who complained about bad delivery in France", ["France"], remarks) == "bad delivery"
    # Without a schema, nothing is sheet vocabulary.
    assert semantic_predicate("amount in france", ["France"]) == "amount"


def test_grammar_computations_and_exclusions_are_not_searched_for():
    """'everything in France' searched the free text for the word 'everything' (2026-09-27). A search needs
    a content word: closed-class words (the caller passes spaCy's reading, engine.closed_class) carry
    grammar. A question that asks for a computation ('highest') or an exclusion ('without') is never a
    search: similarity and word matching cannot express either."""
    sales = [{"table": "sales", "name": name} for name in
             ("order ID", "customer", "city", "tier", "ordered", "currency", "amount")]
    trench = "who ordered a trench coat in france"
    assert semantic_predicate(trench, ["France"], sales, closed={"who", "a", "in"}) == "ordered trench coat"
    assert content_words(trench, ["France"], sales, closed={"who", "a", "in"}) == ["trench", "coat"]
    assert semantic_predicate("trench coat orders in France", ["France"], sales, closed={"in"}) == "trench coat orders"
    for question, closed in (("everything in france", {"everything", "in"}),
                             ("anything from france", {"anything", "from"}),
                             ("What is the highest amount paid?", {"what", "is", "the", "?"}),
                             ("orders in France without a trench coat", {"in", "a"})):
        assert semantic_predicate(question, ["France"], sales, closed=closed) == "", question
    # A quoted cell value is an exact filter, dropped like the country ('Gold' is a tier): nothing is left to
    # search for. With a phrase beside it, the search keeps only the phrase.
    assert semantic_predicate("gold customers in france", ["France", "Gold"], sales, closed={"in"}) == ""
    assert content_words("who ordered a trench coat in paris", ["Paris"], sales,
                         closed={"who", "a", "in"}) == ["trench", "coat"]
    # An adverb that asks for the total is realized by the operator, not a constraint the query dropped: 'the
    # total population of these cities combined' was declined for 'combined' (2026-09-28). Contrast: a real
    # content word beside it still counts.
    cities = [{"table": "jp", "name": name} for name in ("city", "units")]
    for question, expected in (("What is the total population of these cities combined?", ["population"]),
                               ("units sold altogether", ["sold"]), ("the overall average units", [])):
        assert content_words(question, [], cities, closed={"what", "is", "the", "of", "these", "?"}) \
            == expected, question
    assert content_words("units sold in gold cities combined", [], cities, closed={"in"}) == ["sold", "gold"]


def test_a_comparison_binds_the_attribute_it_names():
    # 'What is the total sales in big cities with population over 1,000,000?' was served as an empty table of
    # cities: the comparison thresholded the sales total whatever it named, and the mentioned cities became a
    # grouping (2026-09-28). Population is a row attribute, so it keeps rows before the one total.
    for question, cmp, value, total in (
        ("What is the total sales in big cities with population over 1,000,000?", ">", 1000000, 350),
        ("What is the total sales in cities with population under 1,000,000?", "<", 1000000, 70),
        ("total sales in cities with a population of over 2.5 million", ">", 2500000, 300),
        ("total sales in cities with more than 1 million population", ">", 1000000, 350),
        ("What is the total sales in cities whose population is at least 2,751,862?", ">=", 2751862, 300),
    ):
        run = _run(question, world=CITY_WORLD, tables=(CITY_SALES,))
        assert _steps(run) == [("filter", [("population", cmp, Decimal(value))]), ("group_agg", [])], \
            (question, _steps(run))
        assert run["answer"]["rows"] == [[total]], (question, run["answer"])


def test_a_comparison_on_the_measure_still_thresholds_each_total():
    # Contrast: 'cities with total sales over 100' compares the aggregated measure, so it stays a threshold on
    # each city's total. A comparison that names no column ('cities that sold over 100') thresholds the metric
    # as before.
    for question in ("cities with total sales over 100", "which cities sold over 100"):
        run = _run(question, world=CITY_WORLD, tables=(CITY_SALES,))
        assert _steps(run) == [("group_agg", ["city"]), ("having", [("sales", ">", Decimal(100))])], \
            (question, _steps(run))
    # 'total amount' is what is compared, so another numeric column never takes over as the measure.
    orders = {"name": "orders", "columns": ["customer", "quantity", "amount"],
              "rows": [["Ada", 1, 120], ["Bob", 5, 80], ["Ada", 2, 150], ["Eve", 9, 40]]}
    run = _run("customers with total amount over 100", world=None, tables=(orders,))
    assert _steps(run) == [("group_agg", ["customer"]), ("having", [("amount", ">", Decimal(100))])], _steps(run)
    assert run["bindings"]["steps"][0]["aggs"] == [("SUM", "amount", "amount")], run["bindings"]["steps"]


def test_the_restricted_noun_groups_only_when_the_question_asks_for_it():
    # Negative: the cities a row threshold restricts are one set to total, unless the question groups them
    # ('by city') or asks for no aggregate at all ('which cities ...'), which lists them.
    run = _run("total sales by city for cities with population over 1,000,000",
               world=CITY_WORLD, tables=(CITY_SALES,))
    assert _steps(run) == [("filter", [("population", ">", Decimal(1000000))]), ("group_agg", ["city"])], _steps(run)
    assert sorted(map(tuple, run["answer"]["rows"])) == [("Nagoya", 50), ("Osaka", 200), ("Tokyo", 100)]
    run = _run("Which cities have a population greater than 1,000,000?", world=CITY_WORLD, tables=(CITY_ORDERS,))
    assert _steps(run)[0] == ("filter", [("population", ">", Decimal(1000000))]), _steps(run)
    assert sorted(row[0] for row in run["answer"]["rows"]) == ["Nagoya", "Osaka", "Tokyo"], run["answer"]
    run = _run("How many cities have a population over 1,000,000?", world=CITY_WORLD, tables=(CITY_SALES,))
    assert run["answer"]["rows"] == [[3]], run["answer"]


def test_a_noun_an_aggregate_runs_over_is_not_a_grouping():
    # 'What is the average population of these cities?' and 'What is the total population of these cities
    # combined?' were served as per-city tables: every text column a question mentioned became a grouping
    # (2026-09-28). A noun named only as the set an aggregate runs over is one set.
    for question, function in (("What is the average population of these cities?", "AVG"),
                               ("What is the total population of these cities combined?", "SUM"),
                               ("total population for the cities", "SUM")):
        run = _run(question, world=CITY_WORLD, tables=(CITY_SALES,))
        steps = [(step["op"], step["by"], step["aggs"]) for step in run["bindings"]["steps"]]
        assert steps == [("group_agg", [], [(function, "population", "population")])], (question, steps)
    run = _run("What is the total population of these cities combined?", world=CITY_WORLD, tables=(CITY_SALES,))
    assert run["answer"]["rows"] == [[20748671]], run["answer"]
    # Contrast: a grouping cue keeps the grouping, including 'each of the cities' and a second column's 'by'.
    for question, by in (("total population by country", ["country"]),
                         ("What is the population of each city?", ["city"]),
                         ("total sales for each of the cities", ["city"]),
                         ("average population of these cities by country", ["country"])):
        run = _run(question, world=CITY_WORLD, tables=(CITY_SALES,))
        assert run["bindings"]["steps"][0]["by"] == by, (question, run["bindings"]["steps"])
    # Negative: a ranked noun is what the answer lists, whether ranked by a number or by a ranking word.
    for question in ("Which are the top 2 cities by population?", "total population of the top 3 cities"):
        run = _run(question, world=CITY_WORLD, tables=(CITY_SALES,))
        assert [(step["op"], step.get("by")) for step in run["bindings"]["steps"]][:1] == [("group_agg", ["city"])] \
            and run["bindings"]["steps"][-1]["op"] == "topn", (question, run["bindings"]["steps"])


def test_the_table_name_is_not_a_grouping_column():
    """'total amount of GBP orders in Europe' on the customer-orders sheet listed five items instead of one
    total (2026-09-30): 'orders' names the rows being summed, but the loose stem match read it as the
    `ordered` column ('ordered'[:5] == 'order') and grouped by it."""
    import csv
    from pathlib import Path

    rows = list(csv.reader((Path(__file__).resolve().parents[1] / "web" / "public" / "dataset" / "customer-orders"
                            / "orders.csv").read_text(encoding="utf-8").splitlines()))
    table = {"name": "orders", "columns": rows[0], "rows": rows[1:]}
    engine = ComposeEngine()
    for question, by in (("total amount of GBP orders", []),          # the table's rows: one total
                         ("total amount of orders", []),
                         ("total amount by ordered", ["ordered"]),    # contrast: the column named outright
                         ("total amount of orders by tier", ["tier"]),
                         ("total amount by customer", ["customer"])):  # negative: another column is untouched
        steps = engine.plan(question, table)
        assert [step.get("by") for step in steps if step.get("op") == "group_agg"] == [by], (question, steps)
    # Served through the view stack, the plan reads a materialized view ('filter_1'), not the sheet: the uploaded
    # sheet's name must still be claimed there. The GBP value filter keeps the five London rows: 810.
    run = _run("total amount of GBP orders", world=None, tables=(table,))
    assert run["answer"]["rows"] == [[810]], run["answer"]


def test_a_learned_ranking_needs_a_ranking_word():
    """The learned head fired TOPN (and GROUP) on 'total amount in North America in USD' and compose served
    the top 3 customers (2026-09-30). A ranking changes which rows answer, so it needs the question to rank."""
    import csv
    from pathlib import Path

    rows = list(csv.reader((Path(__file__).resolve().parents[1] / "web" / "public" / "dataset" / "customer-orders"
                            / "orders.csv").read_text(encoding="utf-8").splitlines()))
    table = {"name": "orders", "columns": rows[0], "rows": rows[1:]}
    engine = ComposeEngine(reader=None)
    head = frozenset({"TOPN", "GROUP"})
    steps = engine.plan("total amount in North America", table, prims=head)
    assert [(step["op"], step.get("by")) for step in steps] == [("group_agg", [])], steps
    steps = engine.plan("total amount sorted by", table, prims=frozenset({"SORT"}))   # an explicit sort cue
    assert any(step["op"] == "sort" for step in steps), steps
    # Contrast: a ranking word keeps the head's ranking, with its entity.
    for question in ("top customers by total amount", "which customer spent the most"):
        steps = engine.plan(question, table, prims=head)
        assert steps[-1]["op"] == "topn" and steps[0].get("by") == ["customer"], (question, steps)


def test_serving_hands_an_own_data_composition_to_the_planner():
    """The compose host let compose own any plan with a composition op whenever a request carried an analysis
    context, which every served request does; the Spider evaluator asks route() alone. On Spider DEV that
    served 180 of 1,034 questions from compose (3 right) where the planner the evaluator scores gets 146
    (2026-09-30). Serving now asks route() alone: an own-data top-N comes back unlowered, so serve()
    delegates it, while a composition over a necessary world dependency is still lowered and owned."""
    from unittest.mock import Mock

    from engine.deterministic.context import analysis_execution_context
    from engine.knowledge_compose import ComposedKnowledgeQuery

    class Lowered(Exception):
        pass

    def plan(ops, dependency):
        views = [{"name": f"v{i}", "op": op, "sql": "SELECT 1", "columns": ["x"], "rows": [[1]]}
                 for i, op in enumerate(ops)]
        return {"views": views, "world_dependency": dependency, "plan": list(ops), "primitives": [],
                "bindings": {}, "answer": {"columns": ["x"], "rows": [[1], [2]]}}

    host = ComposedKnowledgeQuery.__new__(ComposedKnowledgeQuery)
    host.qw = Mock()
    host.qw.ingest.side_effect = Lowered("the composition was lowered")
    host.reason = Mock()
    with analysis_execution_context({"slug": "top", "revision": 1}, "c_" + "6" * 32):
        host.reason.run.return_value = plan(["group_agg", "topn"], None)
        local = host._run_engine([], "top 3 customers by total amount", "c_" + "6" * 32, None, world={})
        assert not local.get("deterministic") and local["model"] == "engine - composed view stack", local
        host.reason.run.return_value = plan(["world_join", "world_filter", "group_agg", "topn"],
                                            {"is_necessary": True, "necessary": ["continent"]})
        try:
            host._run_engine([], "top 3 cities in Europe by total amount", "c_" + "6" * 32, None, world={})
        except Lowered:
            pass
        else:
            raise AssertionError("a world composite that route() gives compose was not lowered")


def test_a_threshold_on_an_aggregate_compares_numbers():
    # Compose's SQLite candidate kept every city for 'cities with total sales over 100' and none for 'under 50': a
    # view's decimal_sum is TEXT with no affinity, and SQLite orders every TEXT above every number (2026-09-28).
    # Served answers run the shared plan in Postgres and were right; routing and the re-expression of a
    # world-filtered scalar read this answer.
    for question, rows in (("cities with total sales over 100", [["Osaka", 200]]),
                           ("cities with total sales under 50", [["Lyon", 30], ["Marseille", 40]]),
                           ("cities with total sales of at least 100", [["Osaka", 200], ["Tokyo", 100]])):
        run = _run(question, world=None, tables=(CITY_SALES,))
        assert sorted(run["answer"]["rows"]) == rows, (question, run["answer"])
    # Contrast: a stored decimal column and a year compared numerically already, and still do.
    run = _run("What is the total sales in big cities with population over 1,000,000?",
               world=CITY_WORLD, tables=(CITY_SALES,))
    assert run["answer"]["rows"] == [[350]], run["answer"]
    years = {"name": "y", "columns": ["city", "year", "sales"],
             "rows": [["Tokyo", 2022, 10], ["Tokyo", 2023, 20], ["Osaka", 2023, 5]]}
    assert _run("total sales since 2023", world=None, tables=(years,))["answer"]["rows"] == [[25]]
    # Negative: a text value keeps its text comparison, and a cell that is not a number never passes a numeric
    # comparison (it used to pass every 'greater than', as TEXT above any number).
    assert _run("total sales in Osaka", world=None, tables=(CITY_SALES,))["answer"]["rows"] == [[200]]
    connection = sqlite3.connect(":memory:")
    register_sqlite_decimal(connection)
    connection.execute("CREATE TABLE t (city TEXT, n)")
    connection.execute("INSERT INTO t VALUES ('Paris', '120'), ('Lyon', 'n/a')")
    assert connection.execute(filter_view("t", [("n", ">", 100)])).fetchall() == [("Paris", "120")]
    assert connection.execute(filter_view("t", [("city", ">", 5)])).fetchall() == []
    assert connection.execute(filter_view("t", [("city", "=", "Lyon")])).fetchall() == [("Lyon", "n/a")]


def test_an_id_column_is_never_offered_as_a_measure():
    """'orders for a brass magnifying glass in france' on the customer-orders sheet summed "order ID"
    (2026-09-27): compose's own key test matched 'order_id' but not 'order ID', so the ID reached the measure
    candidates, where the encoder's operand pick could choose it. Compose now reads the engine's one
    surrogate-key rule (engine.sql_schema.is_surrogate_key)."""
    import csv
    from pathlib import Path

    rows = list(csv.reader((Path(__file__).resolve().parents[1] / "web" / "public" / "dataset" / "customer-orders"
                            / "orders.csv").read_text(encoding="utf-8").splitlines()))
    table = {"name": "orders", "columns": rows[0], "rows": rows[1:]}
    engine = ComposeEngine()
    offered = []
    pick = engine._pick_measure
    engine._pick_measure = lambda low, numeric, question: offered.append(list(numeric)) or pick(low, numeric, question)
    engine.plan("orders for a brass magnifying glass in france", table)
    assert offered == [["amount"]], offered


def test_numeric_filter_preserves_decimal_boundary_and_large_integer():
    import sqlite3
    from decimal import Decimal
    from engine.numeric import register_sqlite_decimal
    from engine.primitives import filter_view

    with sqlite3.connect(":memory:") as connection:
        register_sqlite_decimal(connection)
        connection.execute('CREATE TABLE values_to_filter (amount TEXT)')
        values = ["0.10000000000000000002", "0.10000000000000000003",
                  "0.10000000000000000004"]
        connection.executemany('INSERT INTO values_to_filter VALUES (?)', [(v,) for v in values])
        threshold = Decimal(values[1])
        for op, expected in ((">", values[2:]), ("<", values[:1]), ("=", values[1:2]),
                             (">=", values[1:]), ("<=", values[:2]), ("!=", values[::2])):
            sql = filter_view("values_to_filter", [("amount", op, threshold)])
            assert [r[0] for r in connection.execute(sql)] == expected, (op, sql)
        connection.execute('DELETE FROM values_to_filter')
        connection.execute('INSERT INTO values_to_filter VALUES (?)', ("9223372036854775809",))
        assert not connection.execute(filter_view(
            "values_to_filter", [("amount", ">", 9223372036854775810)])).fetchall()


TESTS = [
    test_numeric_filter_preserves_decimal_boundary_and_large_integer,
    test_aggregate_over_zero_rows_is_not_presented_as_an_answer,
    test_real_aggregates_and_plain_selects_are_untouched,
    test_words_that_name_the_sheet_leave_nothing_to_search_for,
    test_grammar_computations_and_exclusions_are_not_searched_for,
    test_an_id_column_is_never_offered_as_a_measure,
    test_a_comparison_binds_the_attribute_it_names,
    test_a_comparison_on_the_measure_still_thresholds_each_total,
    test_the_restricted_noun_groups_only_when_the_question_asks_for_it,
    test_a_noun_an_aggregate_runs_over_is_not_a_grouping,
    test_the_table_name_is_not_a_grouping_column,
    test_a_learned_ranking_needs_a_ranking_word,
    test_serving_hands_an_own_data_composition_to_the_planner,
    test_a_threshold_on_an_aggregate_compares_numbers,
    test_named_input_value_filters_directly_without_world_model,
    test_named_input_value_filters_even_when_world_available,
    test_regression_the_named_filter_is_not_dropped,
    test_country_still_uses_the_world_path_unchanged,
    test_no_named_value_does_not_invent_a_filter,
    test_structural_query_word_never_self_matches_a_cell,
    test_deterministic_across_repeated_runs,
    test_string_column_is_discovered_as_a_foreign_key,
    test_string_fk_joins_uploaded_tables_for_a_world_query,
    test_non_unique_parent_is_not_a_spurious_foreign_key,
    test_no_name_signal_column_is_not_a_foreign_key,
    test_tabs_with_the_same_columns_are_not_foreign_keys_of_each_other,
    test_name_signaled_foreign_keys_still_resolve,
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
    print(f"\nCompose: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
