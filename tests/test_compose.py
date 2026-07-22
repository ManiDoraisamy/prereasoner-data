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


ORDERS = {"name": "orders", "columns": ["city", "amount"],
          "rows": [["Paris", 100], ["Lyon", 80], ["Chennai", 90], ["Chennai", 60]]}
# The world-meaning table ComposeEngine joins: geo value (col 0) -> attributes (country, ...).
WORLD = {"name": "world meaning", "columns": ["city", "country"],
         "rows": [["Paris", "France"], ["Lyon", "France"], ["Chennai", "India"]]}


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


TESTS = [
    test_named_input_value_filters_directly_without_world_model,
    test_named_input_value_filters_even_when_world_available,
    test_regression_the_named_filter_is_not_dropped,
    test_country_still_uses_the_world_path_unchanged,
    test_no_named_value_does_not_invent_a_filter,
    test_structural_query_word_never_self_matches_a_cell,
    test_deterministic_across_repeated_runs,
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
