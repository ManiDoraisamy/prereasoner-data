"""Regression tests for server-authored column provenance."""
from __future__ import annotations

from engine.provenance import ProvenanceContext


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


TESTS = [
    test_provenance_uses_request_roles_not_column_name_guesses,
    test_calculation_and_ecb_columns_keep_distinct_lineage,
    test_http_and_stream_paths_emit_the_same_provenance_shape,
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
