"""Hermetic regressions for authenticated, measure-scoped dataset semantics."""
from __future__ import annotations

import sys
from unittest.mock import patch

from engine import dataset_attestation
from engine.dataset_semantics import (
    DatasetOpError,
    apply,
    effective,
    synthetic_currency_column,
    validate_ops,
    validate_replay,
)


def _tables():
    return [{"name": "responses",
             "columns": ["submitted", "name", "country", "budget"],
             "rows": [["2026-08-03", "Elena", "Germany", "15000"],
                      ["2026-08-04", "Pierre", "France", "8000"]]}]


def _set(currency="EUR", column="budget", table="responses", **metadata):
    return {"op": "set_measure_metadata", "table": table, "column": column,
            "metadata": {"currency": currency, **metadata},
            "basis": {"source": "conversation", "text": "This is in euros."}}


def _validated(*ops, tables=None):
    return validate_ops(list(ops), tables or _tables(), attested=True)


def test_valid_set_normalizes_and_binds():
    ops = _validated(_set(currency="eur"))
    assert ops[0]["metadata"]["currency"] == "EUR"
    assert ops[0]["basis"] == {
        "source": "conversation", "text": "This is in euros.", "attested": True,
    }


def test_unknown_op_table_column_and_code_are_rejected():
    tables = _tables()
    for bad in [
        {"op": "drop_rows", "table": "responses", "column": "budget",
         "basis": {"source": "conversation", "text": "quoted"}},
        _set(table="orders"),
        _set(column="revenue"),
        _set(currency="EURO"),
        _set(date_column="posted"),
    ]:
        try:
            validate_ops([bad], tables, attested=True)
            raise AssertionError(f"must reject {bad}")
        except DatasetOpError:
            pass


def test_src_data_outranks_conversation():
    tables = _tables()
    tables[0]["columns"].append("Currency Code")
    tables[0]["rows"] = [row + ["USD"] for row in tables[0]["rows"]]
    try:
        validate_ops([_set()], tables, attested=True)
        raise AssertionError("a real currency column must reject the operation")
    except DatasetOpError as exc:
        assert "outranks" in str(exc)


def test_replay_last_set_wins_and_clear_removes():
    log = _validated(_set("EUR"), _set("GBP")) + [
        {"op": "clear_measure_metadata", "table": "responses", "column": "budget"},
    ]
    assert effective(log) == {}
    log.extend(_validated(_set("CHF")))
    assert effective(log)[("responses", "budget")]["currency"] == "CHF"
    assert len(log) == 4


def test_apply_synthesizes_a_private_constant_column():
    tables = _tables()
    ops = _validated(_set(), tables=tables)
    records = apply(tables, ops)
    synth = synthetic_currency_column("budget")
    assert tables[0]["columns"][-1] == synth
    assert all(row[-1] == "EUR" for row in tables[0]["rows"])
    assert records == [{
        "table": "responses", "column": "budget", "currency": "EUR",
        "date_column": None, "currency_column": synth,
        "basis": {"source": "conversation", "text": "This is in euros.", "attested": True},
        "supplied_by": "conversation",
    }]


def test_apply_is_idempotent_and_respects_clear():
    tables = _tables()
    ops = _validated(_set(), tables=tables)
    apply(tables, ops)
    width = len(tables[0]["columns"])
    apply(tables, ops)
    assert len(tables[0]["columns"]) == width
    cleared = ops + [{"op": "clear_measure_metadata", "table": "responses", "column": "budget"}]
    assert apply(_tables(), cleared) == []


def test_ops_for_absent_tables_stay_in_log_but_do_not_apply():
    ops = _validated(_set())
    other = [{"name": "orders", "columns": ["id"], "rows": [["1"]]}]
    assert apply(other, ops) == []
    assert other[0]["columns"] == ["id"]


def test_date_column_binding_survives_to_record():
    tables = _tables()
    records = apply(tables, _validated(_set(date_column="submitted"), tables=tables))
    assert records[0]["date_column"] == "submitted"
    tables[0]["rows"][0][0] = "03/08/2026"
    try:
        _validated(_set(date_column="submitted"), tables=tables)
        raise AssertionError("a non-ISO rate date must be rejected before serving")
    except DatasetOpError as exc:
        assert "ISO date" in str(exc)


def test_multiple_measures_get_distinct_private_columns():
    tables = [{"name": "sales", "columns": ["submitted", "budget", "cost"],
               "rows": [["2026-01-01", "10", "20"]]}]
    ops = validate_ops([
        _set("EUR", table="sales", column="budget"),
        _set("GBP", table="sales", column="cost"),
    ], tables, attested=True)
    records = apply(tables, ops)
    expected = [synthetic_currency_column("budget"), synthetic_currency_column("cost")]
    assert tables[0]["columns"][-2:] == expected
    assert tables[0]["rows"][0][-2:] == ["EUR", "GBP"]
    assert {row["currency_column"] for row in records} == set(expected)


def test_unsupported_currency_and_non_monetary_measure_are_rejected():
    try:
        validate_ops([_set("ZZZ")], _tables(), attested=True)
        raise AssertionError("unsupported ISO code must be rejected")
    except DatasetOpError:
        pass
    try:
        validate_ops([_set()], [{"name": "responses", "columns": ["budget"],
                                 "rows": [["not a number"]]}], attested=True)
        raise AssertionError("a monetary-looking column with nonnumeric values must be rejected")
    except DatasetOpError:
        pass
    try:
        validate_ops([_set(column="employees")], [{"name": "responses",
                     "columns": ["employees"], "rows": [[10]]}], attested=True)
        raise AssertionError("non-monetary column must be rejected")
    except DatasetOpError:
        pass


def test_direct_request_and_missing_basis_are_rejected():
    try:
        validate_ops([_set()], _tables())
        raise AssertionError("a direct client cannot mint trusted dataset metadata")
    except DatasetOpError as exc:
        assert "authenticated chat service" in str(exc)
    op = _set()
    del op["basis"]
    try:
        validate_ops([op], _tables(), attested=True)
        raise AssertionError("an attested transport still requires a user quote")
    except DatasetOpError as exc:
        assert "basis" in str(exc)


def test_legacy_persisted_set_requires_restatement():
    legacy = _set()
    assert effective([legacy]) == {}
    try:
        validate_replay([legacy], _tables())
        raise AssertionError("an applicable legacy claim must request an authenticated restatement")
    except DatasetOpError as exc:
        assert "restate" in str(exc)


def test_clear_allows_replacement_sheet_while_active_claim_is_rechecked():
    claim = _validated(_set())[0]
    replacement = _tables()
    replacement[0]["columns"].append("currency")
    replacement[0]["rows"] = [row + ["USD"] for row in replacement[0]["rows"]]
    try:
        validate_replay([claim], replacement)
        raise AssertionError("an active claim must not shadow an uploaded currency source")
    except DatasetOpError as exc:
        assert "outranks" in str(exc)
    cleared = [claim, {"op": "clear_measure_metadata", "table": "responses", "column": "budget"}]
    assert validate_replay(cleared, replacement) == cleared


def test_world_rate_binding_uses_claimed_measure_and_date():
    from engine.knowledge_tables import KnowledgeTableQuery
    query = KnowledgeTableQuery.__new__(KnowledgeTableQuery)
    query.words = {"exchange_rate": {"columns": ["currency_code", "date", "rate_to_usd"]}}
    synth = synthetic_currency_column("budget")
    schema = [
        {"table": "responses", "name": synth, "is_date": False},
        {"table": "responses", "name": "submitted", "is_date": True},
        {"table": "responses", "name": "paid_at", "is_date": True},
    ]
    binding = query._world_rate_binding(
        "total budget in USD", ("SUM", "responses", "budget"), schema,
        [{"table": "responses", "column": "budget", "currency": "EUR",
          "currency_column": synth, "date_column": "paid_at"}],
    )
    assert binding["ccy_col"] == synth
    assert binding["date_col"] == "paid_at"


def test_unclaimed_measure_cannot_inherit_another_measures_currency():
    from engine.knowledge_tables import KnowledgeTableQuery
    query = KnowledgeTableQuery.__new__(KnowledgeTableQuery)
    query.words = {"exchange_rate": {"columns": ["currency_code", "date", "rate_to_usd"]}}
    synth = synthetic_currency_column("budget")
    schema = [
        {"table": "responses", "name": synth, "is_date": False},
        {"table": "responses", "name": "submitted", "is_date": True},
    ]
    assert query._world_rate_binding(
        "total cost in USD", ("SUM", "responses", "cost"), schema,
        [{"table": "responses", "column": "budget", "currency": "EUR",
          "currency_column": synth, "date_column": "submitted"}],
    ) is None


def test_orchestrator_attests_only_current_user_quotes():
    op = _set()
    verified, trusted = dataset_attestation.verify_quotes([op], "This is in euros.", [])
    assert trusted is True and "verified" not in verified[0]["basis"]
    forged, trusted = dataset_attestation.verify_quotes(
        [op], "convert this", [{"role": "user", "content": "This is in euros."}],
    )
    assert trusted is False
    with patch.dict("os.environ", {"DATASET_ATTESTATION_KEY": "unit-test-secret"}):
        signature = dataset_attestation.sign("user-a", verified)
        assert dataset_attestation.verify("user-a", verified, signature)
        assert not dataset_attestation.verify("user-b", verified, signature)
        tampered = [{**verified[0], "metadata": {"currency": "GBP"}}]
        assert not dataset_attestation.verify("user-a", tampered, signature)


TESTS = [
    test_valid_set_normalizes_and_binds,
    test_unknown_op_table_column_and_code_are_rejected,
    test_src_data_outranks_conversation,
    test_replay_last_set_wins_and_clear_removes,
    test_apply_synthesizes_a_private_constant_column,
    test_apply_is_idempotent_and_respects_clear,
    test_ops_for_absent_tables_stay_in_log_but_do_not_apply,
    test_date_column_binding_survives_to_record,
    test_multiple_measures_get_distinct_private_columns,
    test_unsupported_currency_and_non_monetary_measure_are_rejected,
    test_direct_request_and_missing_basis_are_rejected,
    test_legacy_persisted_set_requires_restatement,
    test_clear_allows_replacement_sheet_while_active_claim_is_rechecked,
    test_world_rate_binding_uses_claimed_measure_and_date,
    test_unclaimed_measure_cannot_inherit_another_measures_currency,
    test_orchestrator_attests_only_current_user_quotes,
]


def main():
    failures = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\ndataset semantics: {len(TESTS) - len(failures)} passed, {len(failures)} failed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    sys.exit(main())
