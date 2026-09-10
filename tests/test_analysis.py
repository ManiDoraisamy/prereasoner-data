"""Hermetic tests for named analysis identity, view names, and streamed/HTTP parity."""
from __future__ import annotations

from engine.analysis import (
    AnalysisError,
    analysis_emitter,
    analysis_input_hash,
    analysis_view_name,
    canonical_analysis_slug,
    decorate_analysis_response,
    validate_analysis_spec,
)
from engine.tables import normalize_tables


def _descriptor(action="create"):
    return {"analysis_id": "a_" + "1" * 32, "slug": "total_sales",
            "revision": 1, "action": action, "stale": False}


def test_slugs_are_canonical_bounded_and_deterministic():
    assert canonical_analysis_slug("Total sales in France") == "total_sales_in_france"
    assert canonical_analysis_slug("2026 total") == "analysis_2026_total"
    long = canonical_analysis_slug("A " + "very " * 30 + "long analysis")
    assert len(long.encode("ascii")) <= 40
    assert long == canonical_analysis_slug("A " + "very " * 30 + "long analysis")
    assert len(analysis_view_name(long, "knowledgebase lookup")) <= 63
    # Workbook identity predates and outlives any emitter. Python keywords are ordinary
    # business names and must not be renamed when Python execution is enabled later.
    for word in ("import", "yield", "class", "pass", "lambda"):
        canonical = canonical_analysis_slug(word)
        assert canonical == word
        assert canonical.isidentifier(), word
        assert canonical_analysis_slug(canonical) == canonical, word   # idempotent


def test_effective_input_hash_covers_values_references_and_table_order():
    orders = {"name": "orders", "columns": ["id", "amount"], "rows": [[1, "12.30"]]}
    tiers = {"name": "tier", "columns": ["tier", "discount"], "rows": [["Gold", "0.1"]]}
    assert analysis_input_hash([orders, tiers]) == analysis_input_hash([tiers, orders])
    changed = {**tiers, "rows": [["Gold", "0.2"]]}
    assert analysis_input_hash([orders, tiers]) != analysis_input_hash([orders, changed])
    duplicated = {**orders, "rows": [[1, "12.30"], ["1", "12.30"]]}
    assert analysis_input_hash(normalize_tables([orders])) == \
        analysis_input_hash(normalize_tables([duplicated]))
    fk = {"from_table": "orders", "from_cols": ["tier"],
          "to_table": "tier", "to_cols": ["tier"]}
    assert analysis_input_hash([orders, tiers], explicit_fks=[fk]) != \
        analysis_input_hash([orders, tiers])
    assert analysis_input_hash([orders, tiers], dataset_semantics=[{"currency": "EUR"}]) != \
        analysis_input_hash([orders, tiers])


def test_action_contract_rejects_ambiguous_identity():
    assert validate_analysis_spec({"action": "create", "slug": "Sales"})["slug"] == "sales"
    invalid = (
        ({"action": "modify", "slug": "sales"}, "modify without an engine-owned ID"),
        ({"action": "create", "slug": "sales", "extra": True}, "an unknown field"),
        ({"action": "create"}, "a missing slug"),
        ({"action": "create", "slug": ""}, "an empty slug"),
        ({"action": "create", "slug": 42}, "a non-string slug"),
    )
    for value, message in invalid:
        try:
            validate_analysis_spec(value)
            raise AssertionError(f"{message} was accepted")
        except AnalysisError:
            pass


def test_response_prefixes_views_without_rewriting_executed_sql():
    response = {"sql": 'SELECT SUM("amount") FROM "filtered"', "result": {
        "columns": ["total"], "rows": [["12.30"]]}, "views": [
        {"name": "combined", "sql": 'SELECT * FROM "orders"'},
        {"name": "filtered", "sql": 'SELECT * FROM "combined" WHERE "country" = \'France\''},
        {"name": "total", "sql": 'SELECT SUM("amount") FROM "filtered"'},
    ]}
    decorated = decorate_analysis_response(response, _descriptor())
    assert [view["name"] for view in decorated["views"]] == [
        "total_sales_combined", "total_sales_filtered", "total_sales_total",
    ]
    assert decorated["views"][0]["sql"] == 'SELECT * FROM "orders"'
    assert decorated["views"][1]["sql"] == response["views"][1]["sql"]
    assert decorated["sql"] == response["sql"]
    assert decorated["views"][1]["logical_name"] == "filtered"
    assert response["views"][0]["name"] == "combined", "decoration must not mutate planner output"


def test_live_and_http_view_names_are_identical():
    events = []
    emit = analysis_emitter(lambda node, value, merge=False: events.append((node, value)), _descriptor())
    emit("analysis", {"stale": True})
    emit("views/0", {"name": "combined", "sql": 'SELECT * FROM "orders"'})
    emit("views/1", {"name": "total", "sql": 'SELECT SUM("amount") FROM "combined"'})
    http = decorate_analysis_response({"views": [
        {"name": "combined", "sql": 'SELECT * FROM "orders"'},
        {"name": "total", "sql": 'SELECT SUM("amount") FROM "combined"'},
    ]}, _descriptor())
    assert [value for node, value in events if node.startswith("views/")] == http["views"]
    assert next(value for node, value in events if node == "analysis")["stale"] is True


def test_repeated_logical_steps_get_unique_names_without_changing_sql():
    response = decorate_analysis_response({"views": [
        {"name": "filtered", "sql": 'SELECT * FROM "orders"'},
        {"name": "filtered", "sql": 'SELECT * FROM "filtered" WHERE "active" = true'},
        {"name": "total", "sql": 'SELECT COUNT(*) FROM "filtered"'},
    ]}, _descriptor())
    assert [view["name"] for view in response["views"]] == [
        "total_sales_filtered", "total_sales_filtered_2", "total_sales_total",
    ]
    assert response["views"][1]["sql"] == 'SELECT * FROM "filtered" WHERE "active" = true'
    assert response["views"][2]["sql"] == 'SELECT COUNT(*) FROM "filtered"'


def test_single_sql_answer_gets_a_named_result_view():
    response = decorate_analysis_response({
        "sql": 'SELECT COUNT(*) AS "count" FROM "orders"',
        "result": {"columns": ["count"], "rows": [[3]]},
    }, _descriptor())
    assert response["views"] == [{
        "name": "total_sales_result", "logical_name": "result", "op": "select",
        "label": "result", "sql": 'SELECT COUNT(*) AS "count" FROM "orders"',
        "columns": ["count"], "rows": [[3]], "column_provenance": [],
    }]



def test_already_prefixed_deterministic_views_are_not_prefixed_twice():
    response = decorate_analysis_response({"views": [{
        "name": "total_sales_combined", "logical_name": "combined",
        "sql": 'SELECT * FROM "orders"',
    }]}, _descriptor())
    assert response["views"][0]["name"] == "total_sales_combined"
    assert response["views"][0]["logical_name"] == "combined"


TESTS = [
    test_slugs_are_canonical_bounded_and_deterministic,
    test_effective_input_hash_covers_values_references_and_table_order,
    test_action_contract_rejects_ambiguous_identity,
    test_response_prefixes_views_without_rewriting_executed_sql,
    test_live_and_http_view_names_are_identical,
    test_repeated_logical_steps_get_unique_names_without_changing_sql,
    test_single_sql_answer_gets_a_named_result_view,
    test_already_prefixed_deterministic_views_are_not_prefixed_twice,
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
    print(f"\nanalysis: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
