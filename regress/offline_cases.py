"""Offline (no-Postgres) golden regression cases — the NON-world-model-join tier.

Each case carries its own sample sheets + a plain-English question + an expected-denotation assertion, run
through the REAL engine (live routing: compose view-stack vs slot-filler) on in-memory SQLite. These guard the
shared engine code (engine/joins.py, engine/tables.py, engine/knowledge_compose.py routing) that the Spider tier-1
fixes touched — including two regressions those fixes SHIPPED, encoded here as cases that must go from red to green.

Assertion keys (any subset): expect_scalar, forbid_scalar, expect_contains (all must appear in the answer's
flattened value-set), expect_min_rows. `regression=True` marks a case that documents a shipped regression.
"""
from __future__ import annotations

# ---- sample sheets reused across cases ----
STORES = {"name": "stores", "columns": ["Store_ID", "Region", "Manager_ID", "Revenue"],
          "rows": [["S1", "West", "E1", 5000], ["S2", "East", "E2", 7000],
                   ["S3", "West", "E1", 4000], ["S4", "East", "E3", 9000]]}
EMPLOYEES = {"name": "employees", "columns": ["Employee_ID", "Employee_Name", "Title"],
             "rows": [["E1", "Alice", "Sr Mgr"], ["E2", "Bob", "Mgr"], ["E3", "Carol", "Mgr"]]}
SINGER = {"name": "singer", "columns": ["Singer_ID", "Name", "Country", "Age"],
          "rows": [["1", "Joe", "France", 52], ["2", "Lin", "France", 43], ["3", "Bo", "Netherlands", 28],
                   ["4", "Amy", "France", 33], ["5", "Sal", "US", 41]]}
CARS = {"name": "cars", "columns": ["Car_ID", "Model", "Year", "Price"],
        "rows": [["1", "A", 1980, 10], ["2", "B", 1980, 12], ["3", "C", 1980, 9],
                 ["4", "D", 1990, 20], ["5", "E", 1995, 25]]}
ORDERS = {"name": "orders", "columns": ["Order_ID", "Amount"],
          "rows": [["O1", 100], ["O2", 150], ["O3", 50]]}

CASES = [
    # --- GUARD: relationship-named FK join on the SLOT path (engine.relations) still groups correctly.
    # NB the joins.py (compose-path) regression for the same FK is pinned by run_unit_checks(), since this
    # phrasing fires no depth primitive and routes to the slot planner. ---
    {"name": "fk_relationship_named_group_slot", "tables": [STORES, EMPLOYEES],
     "question": "total revenue by title",
     "expect_contains": [9000, 16000], "forbid_scalar": 24000, "expect_min_rows": 2,
     "note": "Manager_ID->employees.Employee_ID FK; SUM(Revenue) by Title = {Sr Mgr:9000, Mgr:16000}."},

    # --- REGRESSION 2: integer-year filter ("in 1980" on an INT Year column) ---
    {"name": "int_year_filter", "tables": [CARS],
     "question": "how many cars were made in 1980",
     "expect_scalar": 3, "forbid_scalar": 5, "regression": True,
     "note": "Year is INTEGER; the slot year-filter only fired on date-typed cols -> counted all 5."},

    # --- GUARD: COUNT vs SUM (tier-1 fix must stay) ---
    {"name": "count_total_number", "tables": [SINGER],
     "question": "what is the total number of singers", "expect_scalar": 5, "forbid_scalar": 197,
     "note": "'total number of' is a row count, not SUM(Age)=197."},

    # --- GUARD: a clear measure must still SUM (the COUNT rule must not over-fire) ---
    {"name": "sum_total_amount", "tables": [ORDERS],
     "question": "what is the total amount", "expect_scalar": 300,
     "note": "'amount' is a clear measure column -> SUM(Amount)=300, not COUNT(*)."},

    # --- GUARD: projection question routes to slot (not the always-aggregating compose path) ---
    {"name": "projection_names_countries", "tables": [SINGER],
     "question": "what are the names and countries of all singers",
     "expect_contains": ["Joe", "Netherlands"], "forbid_scalar": 197,
     "note": "A projection (no aggregate); routing must send it to the slot planner."},

    # --- GUARD: single-table filtered count (basic slot WHERE) ---
    {"name": "filtered_count_country", "tables": [SINGER],
     "question": "how many singers are from France", "expect_scalar": 3,
     "note": "WHERE Country='France' value-matched -> COUNT=3."},
]
