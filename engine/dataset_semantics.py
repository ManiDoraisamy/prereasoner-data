"""dataset_semantics.py — conversation-supplied measure metadata, validated and applied deterministically.

The ONE owner of the dataset-ops grammar (docs/DATASET_FORMATTER.md). v1 is exactly two operations:

    {"op": "set_measure_metadata",   "table": T, "column": C,
     "metadata": {"currency": "EUR", "date_column": "submitted"?}, "basis": {...}?}
    {"op": "clear_measure_metadata", "table": T, "column": C, "basis": {...}?}

An op is a claim from CONVERSATION about the user's own data ("this is in euros"), persisted with
the conversation as an append-only log. Replaying the log yields the EFFECTIVE metadata (last set
wins; clear removes). Application is deterministic and additive: a currency claim synthesizes a
constant currency-code column on the claimed table, which the existing FX machinery
(engine.currency_intent + the knowledge_tables conversion path) consumes exactly as it consumes a
real uploaded currency column — same rate binding, same (currency, date) join, same trail. No op
ever changes a cell, and SRC data outranks conversation claims: a table that already carries a
currency-source column rejects the op with a clarify instead of being silently overridden.
"""
from __future__ import annotations

import re

from engine.currency_intent import is_currency_source_column

_OPS = ("set_measure_metadata", "clear_measure_metadata")
_ISO_CODE = re.compile(r"^[A-Z]{3}$")
MAX_OPS = 50                                     # an append-only log a conversation can realistically need
SYNTH_COLUMN = "currency"                        # the synthesized code column; free by construction (see validate)


class DatasetOpError(ValueError):
    """A dataset op that must be surfaced to the user as a clarify, never applied."""


def validate_ops(ops, tables):
    """Validate a dataset-ops list against the uploaded tables. Returns the normalized list.

    Raises DatasetOpError with a user-facing sentence on the first violation — the caller turns it
    into a clarify. `tables` is the normalized [{name, columns, rows}] list the ops must bind to.
    """
    if ops is None:
        return []
    if not isinstance(ops, list):
        raise DatasetOpError("dataset operations must be a list")
    if len(ops) > MAX_OPS:
        raise DatasetOpError(f"too many dataset operations (limit {MAX_OPS})")
    by_name = {t["name"]: t for t in tables}
    normalized = []
    for raw in ops:
        if not isinstance(raw, dict):
            raise DatasetOpError("each dataset operation must be an object")
        kind = raw.get("op")
        if kind not in _OPS:
            raise DatasetOpError(f"unknown dataset operation {kind!r}")
        table_name = raw.get("table")
        column = raw.get("column")
        table = by_name.get(table_name)
        if table is None:
            raise DatasetOpError(f"dataset operation names a table that is not uploaded: {table_name!r}")
        if column not in table["columns"]:
            raise DatasetOpError(f"dataset operation names a column {column!r} that is not in {table_name!r}")
        op = {"op": kind, "table": table_name, "column": column}
        basis = raw.get("basis")
        if isinstance(basis, dict):
            op["basis"] = {k: str(v)[:200] for k, v in basis.items() if isinstance(k, str)}
        if kind == "set_measure_metadata":
            metadata = raw.get("metadata")
            if not isinstance(metadata, dict):
                raise DatasetOpError("set_measure_metadata requires a metadata object")
            currency = str(metadata.get("currency", "")).upper()
            if not _ISO_CODE.match(currency):
                raise DatasetOpError(f"currency must be a three-letter ISO code, got {metadata.get('currency')!r}")
            clean = {"currency": currency}
            date_column = metadata.get("date_column")
            if date_column is not None:
                if date_column not in table["columns"]:
                    raise DatasetOpError(
                        f"metadata date_column {date_column!r} is not a column of {table_name!r}")
                clean["date_column"] = date_column
            existing = next((c for c in table["columns"] if is_currency_source_column(c)), None)
            if existing is not None:
                raise DatasetOpError(
                    f"{table_name!r} already has a currency column ({existing!r}) — the uploaded data "
                    "outranks a conversational claim; edit the column instead")
            op["metadata"] = clean
        normalized.append(op)
    return normalized


def effective(ops):
    """Replay an append-only op log to the EFFECTIVE metadata: {(table, column): {currency, ...}}.

    Later sets replace earlier ones; clear removes. The log itself is the audit history — this
    function never mutates it.
    """
    state = {}
    for op in ops or []:
        key = (op["table"], op["column"])
        if op["op"] == "set_measure_metadata":
            state[key] = {**op["metadata"], "basis": op.get("basis")}
        elif op["op"] == "clear_measure_metadata":
            state.pop(key, None)
    return state


def apply(tables, ops):
    """Apply effective metadata to normalized tables IN PLACE; return the semantics records.

    A currency claim appends one synthesized constant column named SYNTH_COLUMN to the claimed
    table (validate_ops guarantees no real currency column exists, so the name is free). The
    records drive the response field and the UI badge; provenance is explicit: the column exists
    because the conversation said so, and the basis quote rides along.
    """
    state = effective(ops)
    records = []
    by_name = {t["name"]: t for t in tables}
    for (table_name, column), metadata in sorted(state.items()):
        table = by_name.get(table_name)
        if table is None or column not in table["columns"]:
            continue                             # table not part of THIS request; the log keeps the claim
        currency = metadata.get("currency")
        if currency and SYNTH_COLUMN not in table["columns"]:
            table["columns"] = list(table["columns"]) + [SYNTH_COLUMN]
            table["rows"] = [list(row) + [currency] for row in table["rows"]]
        records.append({
            "table": table_name, "column": column, "currency": currency,
            "date_column": metadata.get("date_column"),
            "basis": metadata.get("basis"), "supplied_by": "conversation",
        })
    return records
