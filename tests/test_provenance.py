"""Regression tests for server-authored column provenance."""
from __future__ import annotations

from types import SimpleNamespace

from engine.dataset_semantics import synthetic_currency_column
from engine.knowledge_compose import _trace_view
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


def test_qualified_stage_columns_keep_the_origin_their_table_names():
    """payment-commissions (Chrome gate, 2026-10-01): the deterministic emitter names stage
    columns `<table>__<column>`, which the bare-name catalog never matched. Every column of the
    `combined` sheet was badged as calculated, and the sheet's own commission rates as European
    Central Bank data on the `calculated` sheet."""
    context = ProvenanceContext([
        {"name": "payments", "columns": ["payment_instrument", "amount"], "rows": []},
        {"name": "commission_rates", "columns": ["payment_instrument", "commission_percent"],
         "rows": []},
    ], uploaded_count=2)
    combined = context.decorate_view({"op": "join", "rows": [], "columns": [
        "payments__payment_instrument", "payments__amount",
        "commission_rates__payment_instrument", "commission_rates__commission_percent",
    ]})
    assert [(item["kind"], item["table"]) for item in combined["column_provenance"]] == [
        ("input", "payments"), ("input", "payments"),
        ("input", "commission_rates"), ("input", "commission_rates")]
    calculated = context.decorate_view({"op": "convert", "rows": [], "columns": [
        "payments__amount", "commission_rates__commission_percent", "aggregate_operand_1",
    ]})
    records = dict(zip(calculated["columns"], calculated["column_provenance"]))
    assert records["commission_rates__commission_percent"]["kind"] == "input"
    assert records["commission_rates__commission_percent"]["source"] == "upload"
    assert records["aggregate_operand_1"]["kind"] == "derived"
    assert records["aggregate_operand_1"]["operation"] == "calculation"

    # Contrast: the world trail's lookup and exchange-rate columns stay references.
    context = ProvenanceContext([
        {"name": "orders", "columns": ["order ID", "city", "amount"], "rows": []},
    ], uploaded_count=1)
    enriched = context.decorate_view({
        "op": "world_join", "rows": [], "source_release_id": "kb-2026-09",
        "columns": ["orders__order ID", "orders__city", "city__country"],
    })
    records = dict(zip(enriched["columns"], enriched["column_provenance"]))
    assert records["orders__order ID"]["kind"] == "input"
    assert (records["city__country"]["kind"], records["city__country"]["source"]) == (
        "reference", "Wikidata")
    converted = context.decorate_view({
        "op": "convert", "rows": [], "source_release_id": "ecb-2026-09-30",
        "columns": ["orders__amount", "exchange_rate__rate_to_usd", "calculated_value"],
    })
    records = dict(zip(converted["columns"], converted["column_provenance"]))
    assert records["orders__amount"]["kind"] == "input"
    assert records["exchange_rate__rate_to_usd"]["source"] == "European Central Bank"
    assert records["exchange_rate__rate_to_usd"]["release_id"] == "ecb-2026-09-30"
    assert records["calculated_value"]["kind"] == "derived"


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


def test_a_computed_quantity_is_marked_and_a_derived_key_is_not():
    """The reply formats a measure's numbers and writes a key as it is (engine/answer_presentation.py):
    a year grouped by DatePart must never read "2,026"."""
    context = ProvenanceContext([
        {"name": "orders", "columns": ["ordered", "amount"], "rows": []},
    ], uploaded_count=1)
    amount = {"kind": "column", "table": "orders", "column": "amount"}
    response = {
        "computation": {"verified": True, "branches": [{"outputs": [
            {"expression": {"kind": "DatePart"}},
            {"expression": {"kind": "aggregate", "function": "SUM", "distinct": False, "operand": amount}},
            {"expression": {"kind": "binary", "operator": "*", "left": amount, "right": amount}},
            {"expression": amount},
        ]}]},
        "result": {"columns": ["year", "sum", "squared", "amount"], "rows": [[2026, 3, 9, 3]]},
    }
    records = context.decorate_response(response)["result"]["column_provenance"]
    assert [record.get("measure", False) for record in records] == [False, True, True, False]
    assert records[3]["kind"] == "input"
    view = context.decorate_view({"op": "group_agg", "columns": ["amount", "total"], "rows": []})
    assert [record.get("measure", False) for record in view["column_provenance"]] == [False, True]


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
    import datetime
    import json
    from decimal import Decimal

    from engine.server import _json_safe

    payload = {"result": {"columns": ["rate"],
                          "rows": [[Decimal("1.1622")], [Decimal(4)], [Decimal("0.5")]]},
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
        "rows": [[Decimal("1.1622")], [Decimal(4)]],
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


def test_json_artifacts_preserve_exact_database_scalars():
    import datetime
    import json
    from decimal import Decimal

    from engine.artifact_provenance import json_artifact_bytes

    encoded = json.loads(json_artifact_bytes({
        "rows": [[Decimal("1.1622")], [Decimal(4)], [Decimal("0.5")]],
        "effective_date": datetime.date(2026, 9, 10),
    }))
    assert encoded == {
        "rows": [["1.1622"], [4], [0.5]],
        "effective_date": "2026-09-10",
    }


def test_rtdb_array_envelope_preserves_sparse_and_empty_results():
    import json
    from decimal import Decimal
    from engine.trace import rtdb_encode

    rows = [[None, None], [Decimal('1.1622'), None], [None, None]]
    wire = rtdb_encode({'columns': ['amount', 'currency'], 'rows': rows, 'empty': []})
    assert wire['rows']['__pr_wire__'] == 'array/v1'
    assert json.loads(wire['rows']['json']) == [[None, None], ['1.1622', None], [None, None]]
    assert json.loads(wire['empty']['json']) == []
    assert json.loads(wire['columns']['json']) == ['amount', 'currency']


def test_section_cell_lineage_keeps_the_original_and_the_exact_anchor():
    from engine.provenance import InputLineage

    table = {"name": "events", "columns": ["Type", "Contract"],
             "rows": [["Expiration", "a"], [None, "b"], ["Termination", "c"], ["", "d"], [None, "e"]]}
    lineage = InputLineage(table, source_id="upload/events").section_labels(
        0, ((0, 2), (2, 4)), evidence="layout/source-scopes")
    assert lineage.cell(1, 0)["original"] is None
    assert lineage.cell(1, 0)["value"] == "Expiration"
    assert lineage.cell(1, 0)["inputs"] == [{"row": 0, "column": 0}]
    assert lineage.cell(1, 0)["scope"] == [0, 2]
    assert lineage.cell(3, 0)["value"] == "Termination"
    assert lineage.cell(4, 0)["value"] is None  # outside every declared scope
    assert table["rows"][1][0] is None  # evidence does not rewrite the input
    replay = InputLineage.replay(table, lineage.record())
    assert replay.cell(1, 0) == lineage.cell(1, 0)


def test_section_scope_conflicts_fail_atomically_without_overfilling():
    from engine.provenance import InputLineage

    table = {"name": "events", "columns": ["Type"], "rows": [["Expiration"], [None], ["Termination"], [None]]}
    for scopes in (((0, 4),), ((1, 2),), ((0, 3), (2, 4)), ((3, 5),)):
        lineage = InputLineage(table, source_id="events")
        try:
            lineage.section_labels(0, scopes, evidence="source")
        except ValueError:
            pass
        else:
            raise AssertionError("an invalid section declaration was accepted")
        assert lineage.record()["section_cells"] == []
    lineage = InputLineage(table, source_id="events").section_labels(0, ((0, 2),), evidence="source")
    before = lineage.record()
    try:
        lineage.section_labels(0, ((0, 2),), evidence="overwritten")
    except ValueError:
        pass
    else:
        raise AssertionError("a section declaration silently overwrote evidence")
    assert lineage.record() == before


def test_input_lineage_preserves_duplicate_observations_and_execution_multiplicity():
    from engine.provenance import InputLineage

    table = {"name": "payments", "columns": ["amount"], "rows": [[100], [100]]}
    lineage = InputLineage(table, source_id="upload")
    assert lineage.row_id(0) != lineage.row_id(1)
    result = lineage.contributor_record((0, 1, 0), (0,))
    assert result["row_count"] == 3 and result["distinct_source_rows"] == 2
    assert [c["value"] for c in result["cell_lineage"]] == [100, 100, 100]
    assert result["row_ids"][0] == result["row_ids"][2]


def test_input_lineage_snapshot_hash_binds_types_values_and_layout():
    from copy import deepcopy
    from engine.provenance import InputLineage

    table = {"name": "data", "columns": ["value"], "rows": [[123], [None]]}
    original = InputLineage(table, source_id="upload")
    for row in ([["123"], [None]], [[124], [None]], [[None], [123]]):
        changed = {**table, "rows": row}
        assert InputLineage(changed, source_id="upload").source_sha256 != original.source_sha256
        try:
            InputLineage.replay(changed, original.record())
        except ValueError:
            pass
        else:
            raise AssertionError("a changed source replayed under the old identity")
    wire = deepcopy(original.record())
    wire["unaccounted_transform"] = True
    try:
        InputLineage.replay(table, wire)
    except ValueError:
        pass
    else:
        raise AssertionError("unaccounted source transforms crossed the lineage boundary")


def test_section_intervention_changes_only_rows_in_its_scope():
    from engine.provenance import InputLineage

    rows = [["Expiration"], [None], [None], ["Termination"], [None]]
    table = {"name": "events", "columns": ["Type"], "rows": rows}
    before = InputLineage(table, source_id="upload").section_labels(0, ((0, 3), (3, 5)), evidence="sections")
    after = InputLineage({**table, "rows": [["Termination"], *rows[1:]]}, source_id="upload").section_labels(
        0, ((0, 3), (3, 5)), evidence="sections")
    b = [before.cell(i, 0)["value"] for i in range(5)]
    a = [after.cell(i, 0)["value"] for i in range(5)]
    assert b.count("Expiration") == 3 and a.count("Expiration") == 0
    assert b[3:] == a[3:] == ["Termination", "Termination"]


def test_physical_cell_lineage_uses_importer_ranges_including_gaps_and_offsets():
    from engine.provenance import InputLineage

    table = {"name": "normalized", "columns": ["Type", "Contract"], "rows": [["Expiration", "a"], [None, "b"]]}
    meta = {"sourceSheet": "Original Sheet", "sourceRange": "D5:E9",
            "sourceRows": {"encoding": "ranges", "count": 2, "ranges": [[0, 6, 1], [1, 9, 1]]},
            "columnBindings": [{"name": "Type", "column": "D"}, {"name": "Contract", "column": "E"}]}
    lineage = InputLineage(table, source_id="upload", import_record=meta).section_labels(
        0, ((0, 2),), evidence="source/section-annotation")
    cell = lineage.cell(1, 0)
    assert cell["source_cell"] == {"sheet": "Original Sheet", "row": 9, "column": "D", "address": "D9"}
    assert cell["inputs"][0]["source_cell"]["address"] == "D6"
    assert InputLineage.replay(table, lineage.record(), import_record=meta).cell(1, 0) == cell
    try:
        InputLineage.replay(table, lineage.record())
    except ValueError:
        pass
    else:
        raise AssertionError("physical coordinates must replay against the original import, not self-asserted metadata")


def test_legacy_import_lineage_never_guesses_an_excel_row_from_a_header_offset():
    from engine.provenance import InputLineage

    table = {"name": "t", "columns": ["v"], "rows": [[1], [2]]}
    lineage = InputLineage(table, source_id="upload", import_record={"headerRow": 5, "skippedBlankRows": [7]})
    assert lineage.cell(1, 0)["source_cell"] is None
    assert lineage.cell(1, 0)["row"] == 1


def test_physical_lineage_rejects_corrupt_or_out_of_range_row_maps():
    from copy import deepcopy
    from engine.provenance import InputLineage

    table = {"name": "t", "columns": ["v"], "rows": [[1], [2]]}
    meta = {"sourceSheet": "S", "sourceRange": "A1:A4", "columnBindings": [{"name": "v", "column": "A"}],
            "sourceRows": {"encoding": "ranges", "count": 2, "ranges": [[0, 2, 2]]}}
    for ranges in ([[0, 2, 1]], [[1, 2, 2]], [[0, 2, 1], [1, 2, 1]], [[0, 4, 2]]):
        altered = deepcopy(meta)
        altered["sourceRows"]["ranges"] = ranges
        try:
            InputLineage(table, source_id="upload", import_record=altered)
        except ValueError:
            pass
        else:
            raise AssertionError("corrupt physical row map accepted")
    original = InputLineage(table, source_id="upload", import_record=meta)
    moved = deepcopy(meta)
    moved["sourceRows"]["ranges"] = [[0, 3, 2]]
    assert InputLineage(table, source_id="upload", import_record=moved).source_sha256 != original.source_sha256


def test_executor_row_key_resolution_requires_unique_keys_or_verified_row_order():
    from engine.provenance import InputLineage

    source = InputLineage({"name": "t", "columns": ["id", "v"], "rows": [["001", 3], ["002", 4]]}, source_id="input")
    def key(columns, values):
        return {"schema": "conversation", "table": "t", "key_columns": columns, "key_values": values}
    keys = [key(["id"], [2]), key(["id"], [1]), key(["id"], [2])]
    result = source.resolve_row_keys(keys, (1,), key_affinities={"id": "INTEGER"})
    assert [c["original"] for c in result["cell_lineage"]] == [4, 3, 4]
    assert len(source._row_key_lookups) == 1
    native = [key(["rowid"], [2]), key(["rowid"], [1]), key(["rowid"], [2])]
    for kwargs in ({}, {"rowid_source_sha256": "wrong-snapshot"}):
        try: source.resolve_row_keys(native, (1,), **kwargs)
        except ValueError: pass
        else: raise AssertionError("unattested SQLite rowid was presented as an input ordinal")
    assert source.resolve_row_keys(native, (1,), rowid_source_sha256=source.source_sha256)["row_ids"] == result["row_ids"]
    duplicate = InputLineage({"name": "t", "columns": ["id"], "rows": [["x"], ["x"]]}, source_id="input")
    try: duplicate.resolve_row_keys([key(["id"], ["x"])], (0,))
    except ValueError: pass
    else: raise AssertionError("ambiguous natural key attributed an arbitrary duplicate row")


def test_context_lineage_captures_the_request_snapshot_and_rejects_ambiguous_names():
    table = {"name": "t", "columns": ["v"], "rows": [[1], [2]]}
    context = ProvenanceContext([table], uploaded_count=1)
    table["rows"][0][0] = 99
    assert context.input_lineage("t").cell(0, 0)["value"] == 1
    assert context.input_lineage("t") is context.input_lineage("t")
    for ctx, name in ((context, "absent"), (ProvenanceContext([table, table], uploaded_count=2), "t")):
        try:
            ctx.input_lineage(name)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown or duplicate physical tables cannot produce an attributed trace")


TESTS = [
    test_executor_row_key_resolution_requires_unique_keys_or_verified_row_order,
    test_context_lineage_captures_the_request_snapshot_and_rejects_ambiguous_names,
    test_physical_cell_lineage_uses_importer_ranges_including_gaps_and_offsets,
    test_legacy_import_lineage_never_guesses_an_excel_row_from_a_header_offset,
    test_physical_lineage_rejects_corrupt_or_out_of_range_row_maps,
    test_section_cell_lineage_keeps_the_original_and_the_exact_anchor,
    test_section_scope_conflicts_fail_atomically_without_overfilling,
    test_input_lineage_preserves_duplicate_observations_and_execution_multiplicity,
    test_input_lineage_snapshot_hash_binds_types_values_and_layout,
    test_section_intervention_changes_only_rows_in_its_scope,
    test_rtdb_array_envelope_preserves_sparse_and_empty_results,
    test_saved_reference_decimals_do_not_break_the_response,
    test_saved_reference_decimals_do_not_break_the_live_trace,
    test_json_artifacts_preserve_exact_database_scalars,
    test_provenance_uses_request_roles_not_column_name_guesses,
    test_calculation_and_ecb_columns_keep_distinct_lineage,
    test_qualified_stage_columns_keep_the_origin_their_table_names,
    test_http_and_stream_paths_emit_the_same_provenance_shape,
    test_result_uses_typed_expression_even_when_alias_matches_input_column,
    test_a_computed_quantity_is_marked_and_a_derived_key_is_not,
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
