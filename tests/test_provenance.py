"""Regression tests for server-authored column provenance."""
from __future__ import annotations

from types import SimpleNamespace

from engine.knowledge_compose import _trace_view
from engine.provenance import ProvenanceContext
from engine.dataset_semantics import synthetic_currency_column


def test_provenance_uses_request_roles_not_column_name_guesses():
    context = ProvenanceContext([
        {"name": "orders", "columns": ["country_name", "amount"], "rows": []},
        {"name": "iana_country", "columns": ["country_code", "country_name"], "rows": []},
    ], uploaded_count=1)
    joined = context.decorate_view({
        "op": "join", "columns": ["amount", "country_code"], "rows": [],
    })
    assert joined["column_provenance"][0]["kind"] == "input"
    assert joined["column_provenance"][0]["table"] == "orders"
    assert joined["column_provenance"][1]["kind"] == "reference"
    assert joined["column_provenance"][1]["source"] == "iana_country"


def test_calculation_and_ecb_columns_keep_distinct_lineage():
    context = ProvenanceContext([
        {"name": "orders", "columns": ["amount", "currency"], "rows": []},
    ], uploaded_count=1)
    context.decorate_view({"op": "filter", "columns": ["amount", "currency"], "rows": []})
    calculated = context.decorate_view({
        "op": "convert",
        "columns": ["amount", "currency", "rate_to_usd", "rate_published", "converted"],
        "rows": [], "source_release_id": "ecb-2026-09-05",
    })
    records = dict(zip(calculated["columns"], calculated["column_provenance"]))
    assert records["amount"]["kind"] == "input"
    assert records["rate_to_usd"]["source"] == "European Central Bank"
    assert records["rate_published"]["release_id"] == "ecb-2026-09-05"
    assert records["converted"]["kind"] == "derived"
    assert records["converted"]["operation"] == "multiply"


def test_http_and_stream_paths_emit_the_same_provenance_shape():
    tables = [{"name": "orders", "columns": ["amount"], "rows": []}]
    response = {"views": [{"op": "group_agg", "columns": ["total"], "rows": [[3]]}],
                "result": {"columns": ["total"], "rows": [[3]]}}
    direct = ProvenanceContext(tables, uploaded_count=1).decorate_response(response)

    emitted = []
    context = ProvenanceContext(tables, uploaded_count=1)
    emit = context.wrap_emitter(lambda node, value, merge=False: emitted.append((node, value)))
    emit("views/0", response["views"][0])
    emit("result", response["result"])
    assert emitted[0][1]["column_provenance"] == direct["views"][0]["column_provenance"]
    assert emitted[1][1]["column_provenance"] == direct["result"]["column_provenance"]


def test_result_uses_typed_expression_even_when_alias_matches_input_column():
    context = ProvenanceContext([
        {"name": "orders", "columns": ["amount"], "rows": []},
    ], uploaded_count=1)
    response = {
        "computation": {
            "verified": True,
            "branches": [{"outputs": [{"expression": {
                "kind": "aggregate", "function": "SUM", "distinct": False,
                "operand": {"kind": "column", "table": "orders", "column": "amount"},
            }}]}],
        },
        "result": {"columns": ["amount"], "rows": [[3]]},
    }
    record = context.decorate_response(response)["result"]["column_provenance"][0]
    assert record["kind"] == "derived"
    assert record["operation"] == "SUM"
    assert record["inputs"] == ["orders.amount"]


def test_trace_view_preserves_server_authored_lineage():
    view = {
        "name": "calculated", "op": "convert", "label": "calculated", "sql": "SELECT 1",
        "columns": ["converted"], "rows": [["3.30"]],
        "source_release_id": "ecb-2026-09-05",
        "column_provenance": [{"kind": "derived", "source": "Prereasoner"}],
        "internal": "must not cross the trace boundary",
    }
    streamed = _trace_view(view)
    assert streamed["source_release_id"] == "ecb-2026-09-05"
    assert streamed["column_provenance"] == view["column_provenance"]
    assert "internal" not in streamed


def test_world_join_prefers_the_registered_publisher_over_a_wikidata_fallback():
    context = ProvenanceContext([
        {"name": "orders", "columns": ["country_code"], "rows": []},
        {"name": "iana_country", "columns": ["country_code", "country_name"], "rows": []},
    ], uploaded_count=1, enrichment=SimpleNamespace(outcomes=(
        SimpleNamespace(
            matched=True,
            dataset_name="iana_country",
            provenance={"source": "IANA", "release_id": "iana-2026-09"},
        ),
    )))
    joined = context.decorate_view({
        "op": "world_join", "columns": ["country_code", "country_name"], "rows": [],
    })
    record = joined["column_provenance"][1]
    assert record["source"] == "IANA"
    assert record["release_id"] == "iana-2026-09"


def test_asserted_currency_column_is_conversation_provenance():
    currency_column = synthetic_currency_column("budget")
    context = ProvenanceContext([
        {"name": "orders", "columns": ["budget", currency_column], "rows": []},
    ], uploaded_count=1, dataset_semantics=({
        "table": "orders", "column": "budget", "currency": "EUR",
        "currency_column": currency_column,
    },))
    record = context.decorate_view({
        "op": "filter", "columns": ["budget", currency_column], "rows": [],
    })["column_provenance"][1]
    assert record["kind"] == "asserted"
    assert record["source"] == "conversation"
    assert record["inputs"] == ["orders.budget"]


def test_saved_reference_decimals_do_not_break_the_response():
    """Regression for a 2026-09-07 production 500: a user with SAVED REFERENCE data got
    `world request failed: TypeError` on every request. Reference cells become Decimal via
    tables._typed and can reach the response without the Postgres round trip that normally
    normalizes them, so json.dumps raised. The response encoder now applies the existing exact
    wire contract (numeric.wire_value) and LOGS the leak rather than hiding it."""
    import json, datetime
    from decimal import Decimal
    from engine.server import _json_safe

    payload = {"result": {"columns": ["rate"],
                          "rows": [[Decimal("1.1622")], [Decimal("4")], [Decimal("0.5")]]},
               "as_of": datetime.date(2026, 9, 7)}
    encoded = json.loads(json.dumps(payload, default=_json_safe))
    rows = encoded["result"]["rows"]
    # The exact-decimal contract, unchanged: a value no binary float represents exactly crosses the
    # wire as its canonical STRING; representable and integral values stay JSON numbers.
    assert rows[0][0] == "1.1622", f"1.1622 must stay exact, got {rows[0][0]!r}"
    assert rows[1][0] == 4, "integral decimals stay JSON integers"
    assert rows[2][0] == 0.5, "an exactly representable fraction stays a JSON number"
    assert encoded["as_of"] == "2026-09-07"
    # An genuinely unknown type must still fail loudly — this is a normalizer, not a silencer.
    try:
        json.dumps({"x": object()}, default=_json_safe)
        raise AssertionError("an unserializable object must still raise")
    except TypeError:
        pass


def test_saved_reference_decimals_do_not_break_the_live_trace():
    import datetime
    from decimal import Decimal
    from engine.trace import rtdb_safe

    encoded = rtdb_safe({
        "rows": [[Decimal("1.1622")], [Decimal("4")]],
        "effective_date": datetime.date(2026, 9, 8),
    })
    assert encoded == {
        "rows": [["1.1622"], [4]],
        "effective_date": "2026-09-08",
    }
    try:
        rtdb_safe({"x": object()})
        raise AssertionError("an unknown trace value must still raise")
    except TypeError:
        pass


TESTS = [
    test_saved_reference_decimals_do_not_break_the_response,
    test_saved_reference_decimals_do_not_break_the_live_trace,
    test_provenance_uses_request_roles_not_column_name_guesses,
    test_calculation_and_ecb_columns_keep_distinct_lineage,
    test_http_and_stream_paths_emit_the_same_provenance_shape,
    test_result_uses_typed_expression_even_when_alias_matches_input_column,
    test_trace_view_preserves_server_authored_lineage,
    test_world_join_prefers_the_registered_publisher_over_a_wikidata_fallback,
    test_asserted_currency_column_is_conversation_provenance,
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
    print(f"\nprovenance: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
