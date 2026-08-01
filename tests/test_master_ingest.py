"""Hermetic tests for Phase 3 reference-data (master) ingestion — engine.server._master_tabs.

The reasoning table set must span the uploaded CSVs + the user's own reference (master) tables that JOIN
this data, so the deterministic typed-AST planner can answer e.g. "total amount for apparel" by joining
orders.ordered -> ordered-ref.ordered and filtering the reference's category. This suite pins the RELEVANCE
scoping (a cross-conversation reference that doesn't relate to this data must never enter the schema) and the
tab SHAPE (a master table is just another own-data table to the planner). No Postgres / no weights.

Regression for: "how much apparel was sold?" clarified because the reference table never reached the engine.

Run: python -m tests.test_master_ingest
"""
from __future__ import annotations

import sys

from engine import master, server
from engine.tables import csv_table

ORDERS = csv_table(
    "order ID,customer,ordered,amount\n"
    "101,Sherlock Holmes,Deerstalker Cap,72\n"
    "102,Hercule Poirot,Top Hat,180\n"
    "103,Sherlock Holmes,Calabash Pipe,95\n"
    "104,Hercule Poirot,Gabardine Trench Coat,310\n",
    "orders",
)
CUSTOMERS = csv_table("customer ID,name,city,series\n1,Sherlock Holmes,London,Doyle\n", "customers")

# The user's per-user master store (cross-conversation). "ordered" is the reference FOR orders.ordered;
# "region" is an unrelated reference from another conversation; "bare" has a key but no attribute to join to.
STORE = {
    "ordered": {"name": "ordered", "columns": ["ordered", "category", "series", "estimated amount"],
                "rows": [["Deerstalker Cap", "Apparel", "Sherlock Holmes", "29.99"],
                         ["Top Hat", "Apparel", "Sherlock Holmes", "32.00"],
                         ["Calabash Pipe", "Detective Accessory", "Sherlock Holmes", "18.50"],
                         ["Gabardine Trench Coat", "Apparel", "Hercule Poirot", "89.99"],
                         ["Monocle", "Accessory", "Sherlock Holmes", "16.50"]]},
    "region": {"name": "region", "columns": ["region", "manager"], "rows": [["North", "Alice"], ["South", "Bob"]]},
    "bare": {"name": "bare", "columns": ["ordered"], "rows": [["Deerstalker Cap"], ["Top Hat"]]},
}


class _patched_store:
    """Point engine.master's fetchers at an in-memory STORE for the duration of a test (restored after)."""
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self._list, self._get = master.list_master, master.get_master
        master.list_master = lambda sub: [{"name": k} for k in self.store]
        master.get_master = lambda sub, name: self.store.get(name)
        return self

    def __exit__(self, *a):
        master.list_master, master.get_master = self._list, self._get


def test_folds_in_the_relevant_reference_table():
    with _patched_store(STORE):
        tabs = server._master_tabs("sub", [ORDERS, CUSTOMERS])
    names = [t["name"] for t in tabs]
    assert names == ["ordered"], names                                   # only the reference FOR this data
    t = tabs[0]
    assert t["columns"] == ["ordered", "category", "series", "estimated amount"], t["columns"]
    assert len(t["rows"]) == 5, t["rows"]                                # full reference, ready to join


def test_skips_unrelated_cross_conversation_reference():
    with _patched_store(STORE):
        names = [t["name"] for t in server._master_tabs("sub", [ORDERS, CUSTOMERS])]
    assert "region" not in names, names                                 # its keys overlap no uploaded column


def test_skips_key_only_reference_with_no_attributes():
    with _patched_store(STORE):
        names = [t["name"] for t in server._master_tabs("sub", [ORDERS, CUSTOMERS])]
    assert "bare" not in names, names                                   # a lone key column has nothing to join to


def test_no_relevant_reference_yields_empty():
    with _patched_store(STORE):
        assert server._master_tabs("sub", [CUSTOMERS]) == []            # customers alone matches no reference


def test_never_shadows_an_uploaded_table():
    store = {"orders": {"name": "orders", "columns": ["ordered", "category"],
                        "rows": [["Deerstalker Cap", "Apparel"], ["Top Hat", "Apparel"]]}}
    with _patched_store(store):
        names = [t["name"] for t in server._master_tabs("sub", [ORDERS, CUSTOMERS])]
    assert "orders" not in names, names                                 # an uploaded table name is never overridden


def test_caps_reference_rows_to_max_rows():
    big = {"ordered": {"name": "ordered", "columns": ["ordered", "category"],
                       "rows": [["Deerstalker Cap", "Apparel"], ["Top Hat", "Apparel"]]
                               + [[f"p{i}", "x"] for i in range(server.MAX_ROWS + 50)]}}
    with _patched_store(big):
        tabs = server._master_tabs("sub", [ORDERS, CUSTOMERS])
    assert tabs and len(tabs[0]["rows"]) <= server.MAX_ROWS, len(tabs[0]["rows"]) if tabs else None


def test_best_effort_on_store_failure():
    saved = master.list_master
    try:
        def _boom(sub):
            raise RuntimeError("store down")
        master.list_master = _boom
        assert server._master_tabs("sub", [ORDERS, CUSTOMERS]) == []     # never breaks reasoning
    finally:
        master.list_master = saved


TESTS = [
    test_folds_in_the_relevant_reference_table,
    test_skips_unrelated_cross_conversation_reference,
    test_skips_key_only_reference_with_no_attributes,
    test_no_relevant_reference_yields_empty,
    test_never_shadows_an_uploaded_table,
    test_caps_reference_rows_to_max_rows,
    test_best_effort_on_store_failure,
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
    print(f"\nmaster ingest: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
