"""Hermetic tests for saved reference validation and request-time selection.

The master store owns reference integrity and delegates relationship inference to the same
``engine.relations.discover_fks`` function used by the typed AST planner. No Postgres or model weights are
required here.

Run: python -m tests.test_master_ingest
"""
from __future__ import annotations

import sys
from decimal import Decimal

from engine import master
from engine.relations import discover_fks
from engine.tables import csv_table, table_from_rows, table_name

ORDERS = csv_table(
    "order ID,customer,ordered,amount\n"
    "101,Sherlock Holmes,Deerstalker Cap,72\n"
    "102,Hercule Poirot,Top Hat,180\n"
    "103,Sherlock Holmes,Calabash Pipe,95\n"
    "104,Hercule Poirot,Gabardine Trench Coat,310\n",
    "orders",
)
CUSTOMERS = csv_table(
    "customer ID,name,city,series\n1,Sherlock Holmes,London,Doyle\n",
    "customers",
)

STORE = {
    "ordered": {
        "name": "ordered",
        "columns": ["ordered", "category", "series", "estimated amount"],
        "rows": [
            ["Deerstalker Cap", "Apparel", "Sherlock Holmes", "29.99"],
            ["Top Hat", "Apparel", "Sherlock Holmes", "32.00"],
            ["Calabash Pipe", "Detective Accessory", "Sherlock Holmes", "18.50"],
            ["Gabardine Trench Coat", "Apparel", "Hercule Poirot", "89.99"],
            ["Monocle", "Accessory", "Sherlock Holmes", "16.50"],
        ],
    },
    "region": {
        "name": "region",
        "columns": ["region", "manager"],
        "rows": [["North", "Alice"], ["South", "Bob"]],
    },
    "bare": {
        "name": "bare",
        "columns": ["ordered"],
        "rows": [["Deerstalker Cap"], ["Top Hat"]],
    },
}


class _patched_store:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.saved_catalog = master.load_master_catalog
        self.saved_get = master.get_master
        master.load_master_catalog = lambda user_id: [
            {**table, "rows": [[row[0]] + [None] * (len(table["columns"]) - 1) for row in table["rows"]]}
            for table in self.store.values()
        ]
        master.get_master = lambda user_id, name: self.store.get(name)
        return self

    def __exit__(self, *args):
        master.load_master_catalog = self.saved_catalog
        master.get_master = self.saved_get


def _selected(store=STORE, sources=None, limit=6, row_limit=5000):
    with _patched_store(store):
        return master.relevant_tables("sub", sources or [ORDERS, CUSTOMERS], limit, row_limit)


def test_selects_only_the_reference_the_planner_can_join():
    result = _selected()
    assert result["warnings"] == [], result
    assert [table["name"] for table in result["tables"]] == ["ordered"], result
    reference = result["tables"][0]
    assert reference["columns"] == STORE["ordered"]["columns"]
    assert reference["rows"][0][-1] == Decimal("29.99"), reference["rows"][0]
    fks = discover_fks([ORDERS, CUSTOMERS, reference])
    assert any(edge["from_table"] == "orders" and edge["from_col"] == "ordered"
               and edge["to_table"] == "ordered" and edge["to_col"] == "ordered" for edge in fks), fks


def test_skips_unrelated_and_key_only_references():
    names = [table["name"] for table in _selected()["tables"]]
    assert "region" not in names and "bare" not in names, names


def test_never_shadows_an_uploaded_table():
    store = {"orders": {"name": "orders", "columns": ["ordered", "category"],
                        "rows": STORE["ordered"]["rows"]}}
    assert _selected(store)["tables"] == []


def test_uses_the_planner_inclusion_threshold():
    partial = {"ordered": {"name": "ordered", "columns": ["ordered", "category"],
                           "rows": STORE["ordered"]["rows"][:2]}}
    assert _selected(partial)["tables"] == []


def test_does_not_select_a_case_insensitive_join_that_sql_cannot_execute():
    changed = {"ordered": {"name": "ordered", "columns": ["ordered", "category"],
                           "rows": [[row[0].upper(), row[1]] for row in STORE["ordered"]["rows"]]}}
    result = _selected(changed)
    assert result["tables"] == [], result
    assert result["warnings"] and "text normalization" in result["warnings"][0], result


def test_table_from_rows_unquotes_cells_like_a_csv_upload():
    # A saved-reference cell with literal wrapping quotes must normalize the SAME as the identical value uploaded
    # as CSV, or foreign-key discovery, which compares keys exactly as the join does, silently drops the reference.
    built = table_from_rows("cities", ['"city"'], [['"Paris"'], ["Lyon"]])
    csv = csv_table('"""city"""\n"""Paris"""\nLyon\n', "cities")
    assert built["columns"] == csv["columns"] == ["city"], (built["columns"], csv["columns"])
    assert built["rows"] == csv["rows"] == [["Paris"], ["Lyon"]], (built["rows"], csv["rows"])


def test_selects_a_reference_whose_stored_values_carry_wrapping_quotes():
    # orders.ordered holds unquoted product names (as a CSV upload parses them); the reference stored the same
    # keys with literal wrapping quotes. After unquoting they match, so the reference must still be selected.
    quoted = {"ordered": {"name": "ordered", "columns": ["ordered", "category"],
                          "rows": [['"' + row[0] + '"', row[1]] for row in STORE["ordered"]["rows"]]}}
    result = _selected(quoted)
    assert [table["name"] for table in result["tables"]] == ["ordered"], result
    assert result["warnings"] == [], result


def test_caps_reference_rows_and_table_count():
    rows = STORE["ordered"]["rows"] + [[f"p{i}", "x"] for i in range(100)]
    result = _selected({"ordered": {"name": "ordered", "columns": ["ordered", "category"], "rows": rows}},
                       limit=1, row_limit=7)
    assert len(result["tables"]) == 1
    assert len(result["tables"][0]["rows"]) == 7
    assert result["warnings"] == ['Saved reference "ordered" has 105 rows and a request reads 7: this answer '
                                  'read every row your data names and left out 98 other rows.'], result


def _products(count, tail):
    """``count`` saved product keys K00000..., the first ``count - len(tail)`` in category "included" and the
    keys in ``tail`` in category "tail"."""
    keys = [f"K{index:05d}" for index in range(count)]
    return {"product": {"name": "product", "columns": ["product", "category"],
                        "rows": [[key, "tail" if key in tail else "included"] for key in keys]}}


def _orders(keys):
    return csv_table("order,product,amount\n" + "".join(
        f"{index},{key},1\n" for index, key in enumerate(key for key in keys for _ in range(2))), "orders")


def _totals(orders, reference):
    """Total amount by category over the inner join the planner emits, and the order rows it kept."""
    category = {row[0]: row[1] for row in reference["rows"]}
    kept = [row for row in orders["rows"] if row[1] in category]
    totals = {}
    for row in kept:
        totals[category[row[1]]] = totals.get(category[row[1]], 0) + row[2]
    return sorted(totals.items()), len(kept)


def test_a_reference_over_the_row_budget_keeps_every_row_the_request_names():
    # 5,001 saved products; orders name the first 100 and the last. A request reads 5,000 reference rows: the
    # key-order prefix lost K05000, so the inner join dropped its 2 orders from every total, unannounced
    # (release review, revision 2, N07, 2026-10-07).
    store = _products(5001, {"K05000"})
    orders = _orders([f"K{index:05d}" for index in range(100)] + ["K05000"])
    result = _selected(store, [orders], row_limit=5000)
    [reference] = result["tables"]
    assert len(reference["rows"]) == 5000, len(reference["rows"])
    assert _totals(orders, reference) == ([("included", 200), ("tail", 2)], 202), _totals(orders, reference)
    assert result["warnings"] == ['Saved reference "product" has 5,001 rows and a request reads 5,000: this '
                                  'answer read every row your data names and left out 1 other row.'], result
    assert any(edge["from_table"] == "orders" and edge["to_table"] == "product"
               for edge in discover_fks([orders, reference])), "the kept rows still join"
    # Contrast: orders naming only keys past the prefix. Discovery reads every saved key, so the reference
    # is still found, and the rows it keeps are the ones the orders name.
    tail_only = _orders([f"K{index:05d}" for index in range(4950, 5001)])
    result = _selected(_products(5001, {f"K{index:05d}" for index in range(4950, 5001)}), [tail_only],
                       row_limit=60)
    [reference] = result["tables"]
    assert _totals(tail_only, reference) == ([("tail", 102)], 102), _totals(tail_only, reference)
    # Negative: a reference within the budget is read whole and says nothing.
    small = _orders([f"K{index:05d}" for index in range(10)])
    result = _selected(_products(40, set()), [small], row_limit=40)
    assert len(result["tables"][0]["rows"]) == 40 and result["warnings"] == [], result


def test_a_reference_naming_more_rows_than_the_budget_is_not_used_and_says_so():
    # A join to a part of the named rows would answer a partial total as the whole.
    orders = _orders([f"K{index:05d}" for index in range(30)])
    result = _selected(_products(100, set()), [orders], row_limit=20)
    assert result["tables"] == [], result
    assert result["warnings"] == ['Saved reference "product" was not used: your data names 30 of its rows and '
                                  'a request reads at most 20.'], result


def test_a_chain_through_a_cut_reference_reads_the_rows_its_kept_rows_name():
    # orders -> product (cut to the budget) -> supplier: the supplier rows kept are the ones the kept product
    # rows name, so the chain loses no order.
    products = [[f"K{index:05d}", f"S{index % 50:03d}" if index < 40 else f"S{index:03d}"]
                for index in range(300)]
    store = {"product": {"name": "product", "columns": ["product", "supplier"], "rows": products},
             "supplier": {"name": "supplier", "columns": ["supplier", "country"],
                          "rows": [[f"S{index:03d}", "far" if index >= 290 else "near"] for index in range(300)]}}
    orders = _orders([f"K{index:05d}" for index in range(40)] + ["K00295"])
    result = _selected(store, [orders], row_limit=45)
    product, supplier = result["tables"]
    supplier_of = {row[0]: row[1] for row in product["rows"]}
    country_of = {row[0]: row[1] for row in supplier["rows"]}
    countries = [country_of.get(supplier_of.get(row[1])) for row in orders["rows"]]
    assert None not in countries, "every order reaches a supplier"
    assert countries.count("far") == 2 and countries.count("near") == 80, countries


def test_selects_multi_hop_reference_chains_to_a_fixed_point():
    store = {
        "category": {"name": "category", "columns": ["category", "department"],
                     "rows": [["Apparel", "Retail"], ["Detective Accessory", "Props"],
                              ["Accessory", "Retail"]]},
        "ordered": STORE["ordered"],
    }
    result = _selected(store)
    assert [table["name"] for table in result["tables"]] == ["ordered", "category"], result
    fks = discover_fks([ORDERS, *result["tables"]])
    assert any(edge["from_table"] == "ordered" and edge["from_col"] == "category"
               and edge["to_table"] == "category" for edge in fks), fks


def test_store_failure_is_visible_instead_of_silent():
    saved = master.load_master_catalog
    saved_log = master.LOG.exception
    try:
        def _boom(user_id):
            raise RuntimeError("store down")
        master.load_master_catalog = _boom
        master.LOG.exception = lambda *args, **kwargs: None
        result = master.relevant_tables("sub", [ORDERS], 2, 5000)
    finally:
        master.load_master_catalog = saved
        master.LOG.exception = saved_log
    assert result["tables"] == []
    assert result["warnings"] == ["Saved reference data was unavailable; the answer used uploaded tables only."], result


def test_table_names_have_one_canonical_owner():
    assert table_name("Revenue Report.csv", 3) == "revenue_report"
    assert table_name("***", 3) == "t3"


def test_validation_preserves_columns_and_normalizes_rows():
    name, columns, rows = master._validated_table(
        "products", ["sku", "category"], [[" A-1 ", " Apparel "], ["", ""]]
    )
    assert name == "products" and columns == ["sku", "category"]
    assert rows == [["A-1", "Apparel"]], rows


def test_validation_rejects_unsafe_reference_shapes():
    bad = [
        ("products", ["sku", ""], [["A", "x"]], "column 2 name cannot be empty"),
        ("products", ["sku", "SKU"], [["A", "x"]], "column names must be unique"),
        ("products", ["sku", "category"], [["A", "x"], [" a ", "y"]], "duplicate sku key: a"),
        ("products", ["sku", "category"], [[None, "x"]], "row 1 is missing its sku key"),
        ("products", ["sku"], [["A", "hidden"]],
         "row 1 has more values than the declared columns"),
        ("x" * 64, ["sku"], [], "table name must be at most 63 UTF-8 bytes"),
    ]
    for name, columns, rows, message in bad:
        try:
            master._validated_table(name, columns, rows)
        except ValueError as exc:
            assert str(exc) == message, (str(exc), message)
        else:
            raise AssertionError(f"expected validation error: {message}")


TESTS = [
    test_selects_only_the_reference_the_planner_can_join,
    test_skips_unrelated_and_key_only_references,
    test_never_shadows_an_uploaded_table,
    test_uses_the_planner_inclusion_threshold,
    test_does_not_select_a_case_insensitive_join_that_sql_cannot_execute,
    test_table_from_rows_unquotes_cells_like_a_csv_upload,
    test_selects_a_reference_whose_stored_values_carry_wrapping_quotes,
    test_caps_reference_rows_and_table_count,
    test_a_reference_over_the_row_budget_keeps_every_row_the_request_names,
    test_a_reference_naming_more_rows_than_the_budget_is_not_used_and_says_so,
    test_a_chain_through_a_cut_reference_reads_the_rows_its_kept_rows_name,
    test_selects_multi_hop_reference_chains_to_a_fixed_point,
    test_store_failure_is_visible_instead_of_silent,
    test_table_names_have_one_canonical_owner,
    test_validation_preserves_columns_and_normalizes_rows,
    test_validation_rejects_unsafe_reference_shapes,
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
