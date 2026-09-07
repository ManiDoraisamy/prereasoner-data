"""test_dataset_semantics.py — the v1 measure-metadata grammar: validate, replay, apply.

Registered in tests/run_all.py. Hermetic: pure functions over table dicts, no database and no
model. Pins the contracts from docs/DATASET_FORMATTER.md — the closed two-op grammar, SRC data
outranking conversation claims, append-only replay, and the synthesized-column application the
existing FX machinery consumes.
"""
from __future__ import annotations

import sys

from engine.dataset_semantics import (
    DatasetOpError, SYNTH_COLUMN, apply, effective, validate_ops,
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


def test_valid_set_normalizes_and_binds():
    ops = validate_ops([_set(currency="eur")], _tables())
    assert ops[0]["metadata"]["currency"] == "EUR", "ISO codes normalize to uppercase"
    assert ops[0]["basis"]["text"] == "This is in euros."


def test_unknown_op_table_column_and_code_are_rejected():
    tables = _tables()
    for bad, why in [
        ({"op": "drop_rows", "table": "responses", "column": "budget"}, "op outside the v1 grammar"),
        (_set(table="orders"), "unknown table"),
        (_set(column="revenue"), "unknown column"),
        (_set(currency="EURO"), "not an ISO-3 code"),
        (_set(date_column="posted"), "unknown date_column"),
    ]:
        try:
            validate_ops([bad], tables)
            raise AssertionError(f"must reject: {why}")
        except DatasetOpError:
            pass


def test_src_data_outranks_conversation():
    """A table that already carries a currency column refuses the claim — the uploaded data wins."""
    tables = _tables()
    tables[0]["columns"].append("Currency Code")
    tables[0]["rows"] = [r + ["USD"] for r in tables[0]["rows"]]
    try:
        validate_ops([_set()], tables)
        raise AssertionError("a real currency column must reject the op")
    except DatasetOpError as e:
        assert "outranks" in str(e), f"the refusal must say WHY: {e}"


def test_replay_last_set_wins_and_clear_removes():
    """The log is append-only history; EFFECTIVE state comes from replay."""
    log = [_set("EUR"), _set("GBP"),
           {"op": "clear_measure_metadata", "table": "responses", "column": "budget"}]
    assert effective(log) == {}, "clear must remove the claim"
    log.append(_set("CHF"))
    state = effective(log)
    assert state[("responses", "budget")]["currency"] == "CHF", "a later set re-establishes"
    assert len(log) == 4, "replay must never rewrite the stored log"


def test_apply_synthesizes_the_constant_column():
    tables = _tables()
    ops = validate_ops([_set()], tables)
    records = apply(tables, ops)
    t = tables[0]
    assert t["columns"][-1] == SYNTH_COLUMN, "the FX machinery consumes a currency-code column"
    assert all(row[-1] == "EUR" for row in t["rows"]), "constant code on every row"
    assert records == [{"table": "responses", "column": "budget", "currency": "EUR",
                       "date_column": None,
                       "basis": {"source": "conversation", "text": "This is in euros."},
                       "supplied_by": "conversation"}]


def test_apply_is_idempotent_and_respects_clear():
    tables = _tables()
    ops = validate_ops([_set()], tables)
    apply(tables, ops)
    width = len(tables[0]["columns"])
    apply(tables, ops)
    assert len(tables[0]["columns"]) == width, "re-applying must not add a second column"
    cleared = ops + [{"op": "clear_measure_metadata", "table": "responses", "column": "budget"}]
    assert apply(_tables(), cleared) == [], "a cleared claim synthesizes nothing"


def test_ops_for_absent_tables_stay_in_the_log_but_do_not_apply():
    """A follow-up turn may upload a subset of sheets; the claim persists without breaking."""
    ops = validate_ops([_set()], _tables())
    other = [{"name": "orders", "columns": ["id"], "rows": [["1"]]}]
    assert apply(other, ops) == []
    assert other[0]["columns"] == ["id"], "unrelated tables are untouched"


def test_date_column_binding_survives_to_the_record():
    tables = _tables()
    ops = validate_ops([_set(date_column="submitted")], tables)
    records = apply(tables, ops)
    assert records[0]["date_column"] == "submitted"


TESTS = [
    test_valid_set_normalizes_and_binds,
    test_unknown_op_table_column_and_code_are_rejected,
    test_src_data_outranks_conversation,
    test_replay_last_set_wins_and_clear_removes,
    test_apply_synthesizes_the_constant_column,
    test_apply_is_idempotent_and_respects_clear,
    test_ops_for_absent_tables_stay_in_the_log_but_do_not_apply,
    test_date_column_binding_survives_to_the_record,
]


def main():
    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
