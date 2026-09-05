"""Deterministic, infra-free unit tests for the ComposeEngine view-stack reasoner.

Runs ComposeEngine(reader=None) — the encoder-free regex/value-matching fallback — over synthetic tables, so there
is NO Postgres, NO model load, and NO API key. Focus: the PLAIN input-column value filter ('total amount in Chennai'
-> WHERE city='Chennai', no world model) added alongside the world-attribute filter, plus guards that it doesn't
fire spuriously or regress the world path.

Run:  python -m tests.test_compose
"""
from __future__ import annotations

import sys

from engine.compose import ComposeEngine
from engine.joins import discover_fks, join_plan


ORDERS = {"name": "orders", "columns": ["city", "amount"],
          "rows": [["Paris", 100], ["Lyon", 80], ["Chennai", 90], ["Chennai", 60]]}
# The world-meaning table ComposeEngine joins: geo value (col 0) -> attributes (country, ...).
WORLD = {"name": "knowledgebase", "columns": ["city", "country"],
         "rows": [["Paris", "France"], ["Lyon", "France"], ["Chennai", "India"]]}

# A STRING foreign key (the sample demo): orders.customer holds a NAME, not a number, and references
# customers.name. The amount lives in orders; the city (needed for the world query) lives in customers.
CUSTOMERS = {"name": "customers", "columns": ["customer ID", "name", "city"],
             "rows": [[1, "Holmes", "London"], [2, "Clouseau", "Paris"], [3, "Lupin", "Paris"]]}
ORDERS_FK = {"name": "orders", "columns": ["order ID", "customer", "ordered", "amount"],
             "rows": [[101, "Holmes", "Pipe", 100], [102, "Clouseau", "Coat", 310],
                      [103, "Lupin", "Hat", 180], [104, "Holmes", "Cap", 20]]}
WORLD2 = {"name": "knowledgebase", "columns": ["city", "country"],
          "rows": [["London", "UK"], ["Paris", "France"]]}


def _run(question, world=WORLD, tables=(ORDERS,)):
    return ComposeEngine(reader=None).run([dict(t) for t in tables], question, world=world)


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


def test_name_signaled_foreign_keys_still_resolve():
    # Contrastive: the name-signal requirement must NOT over-reject real FKs. An id-named FK (shops.city_id ->
    # cities.id) and the relationship-named STRING FK (orders.customer -> customers.name) both carry name evidence.
    cities = {"name": "cities", "columns": ["id", "city"], "rows": [[1, "Paris"], [2, "Lyon"], [3, "Nice"]]}
    shops = {"name": "shops", "columns": ["shop_id", "city_id", "rev"], "rows": [[9, 1, 5], [8, 2, 7], [7, 1, 3]]}
    assert ("shops", "city_id", "cities", "id") in discover_fks([cities, shops]), discover_fks([cities, shops])
    assert ("orders", "customer", "customers", "name") in discover_fks([CUSTOMERS, ORDERS_FK])


TESTS = [
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
