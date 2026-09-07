"""dataset_semantics.py — conversation-supplied measure metadata, validated and applied deterministically.

The ONE owner of the dataset-ops grammar (docs/DATASET_FORMATTER.md). v1 is exactly two operations:

    {"op": "set_measure_metadata",   "table": T, "column": C,
     "metadata": {"currency": "EUR", "date_column": "submitted"?}, "basis": {...}}
    {"op": "clear_measure_metadata", "table": T, "column": C, "basis": {...}}

An op is a claim from CONVERSATION about the user's own data ("this is in euros"), persisted with
the conversation as an append-only log. Replaying the log yields the EFFECTIVE metadata (last set
wins; clear removes). Application is deterministic and additive: each currency claim synthesizes a
private constant currency-code column for the claimed measure, which the existing FX machinery
(engine.currency_intent + the knowledge_tables conversion path) consumes exactly as it consumes a
real uploaded currency column — same rate binding, same (currency, date) join, same trail. No op
ever changes a cell, and SRC data outranks conversation claims: a table that already carries a
currency-source column rejects the op with a clarify instead of being silently overridden.
"""
from __future__ import annotations

import hashlib
import datetime
import re

from engine.currency_intent import is_currency_measure_column, is_currency_source_column
from engine.enrichment.value_types import ISO4217_CODES
from engine.numeric import parse_decimal

_OPS = ("set_measure_metadata", "clear_measure_metadata")
_ISO_CODE = re.compile(r"^[A-Z]{3}$")
MAX_OPS = 50                                     # an append-only log a conversation can realistically need
SYNTH_COLUMN_PREFIX = "__currency_for_"            # one private source column per claimed measure


class DatasetOpError(ValueError):
    """A dataset op that must be surfaced to the user as a clarify, never applied."""


def synthetic_currency_column(column):
    """Return a bounded, collision-resistant physical name for one measure's stated currency."""
    digest = hashlib.sha256(str(column).encode("utf-8")).hexdigest()[:16]
    return f"{SYNTH_COLUMN_PREFIX}{digest}"


def is_synthetic_currency_column(column):
    return str(column).startswith(SYNTH_COLUMN_PREFIX)


def _numeric_measure(table, column):
    """Require the claimed measure to be numeric and planner-recognized as monetary."""
    if not is_currency_measure_column(column):
        return False
    index = table["columns"].index(column)
    values = [row[index] for row in table.get("rows", []) if index < len(row)
              and row[index] not in (None, "")]
    if not values:
        return True
    try:
        return all(parse_decimal(value) is not None for value in values)
    except (TypeError, ValueError):
        return False


def _valid_date_binding(table, measure_column, date_column):
    """Every populated measure must have an exact ISO date usable by the rate join."""
    measure_index = table["columns"].index(measure_column)
    date_index = table["columns"].index(date_column)
    for row in table.get("rows", []):
        measure = row[measure_index] if measure_index < len(row) else None
        if measure in (None, ""):
            continue
        value = row[date_index] if date_index < len(row) else None
        text = str(value).strip() if value is not None else ""
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return False
            datetime.date.fromisoformat(text)
        except ValueError:
            return False
    return True


def validate_ops(ops, tables, *, attested=False):
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
    if ops and not attested:
        raise DatasetOpError("dataset metadata must come through the authenticated chat service")
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
        if not isinstance(basis, dict) or basis.get("source") != "conversation":
            raise DatasetOpError("dataset metadata requires a conversation basis")
        quote = basis.get("text")
        if not isinstance(quote, str) or not quote.strip():
            raise DatasetOpError("dataset metadata requires the user's quoted text")
        # The transport signature is verified by engine.server before this function is called.
        # Ignore any client/model verification field and persist only this server-generated marker.
        op["basis"] = {"source": "conversation", "text": quote[:200], "attested": True}
        if kind == "set_measure_metadata":
            metadata = raw.get("metadata")
            if not isinstance(metadata, dict):
                raise DatasetOpError("set_measure_metadata requires a metadata object")
            currency = str(metadata.get("currency", "")).upper()
            if not _ISO_CODE.match(currency) or currency not in ISO4217_CODES:
                raise DatasetOpError(f"currency must be a supported ISO 4217 code, got {metadata.get('currency')!r}")
            if not _numeric_measure(table, column):
                raise DatasetOpError(f"{table_name!r}.{column!r} must be a numeric monetary measure")
            clean = {"currency": currency}
            date_column = metadata.get("date_column")
            if date_column is not None:
                if date_column not in table["columns"]:
                    raise DatasetOpError(
                        f"metadata date_column {date_column!r} is not a column of {table_name!r}")
                if not _valid_date_binding(table, column, date_column):
                    raise DatasetOpError(
                        f"metadata date_column {date_column!r} must contain an ISO date for every "
                        f"populated {column!r} value")
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
        if not isinstance(op, dict) or op.get("op") not in _OPS:
            continue
        table_name, column = op.get("table"), op.get("column")
        if not isinstance(table_name, str) or not isinstance(column, str):
            continue
        key = (table_name, column)
        if op["op"] == "set_measure_metadata":
            state[key] = op
        elif op["op"] == "clear_measure_metadata":
            state.pop(key, None)
    effective_state = {}
    for key, op in state.items():
        basis, metadata = op.get("basis"), op.get("metadata")
        if (not isinstance(basis, dict) or basis.get("source") != "conversation"
                or not isinstance(basis.get("text"), str) or not basis.get("text").strip()
                or basis.get("attested") is not True
                or not isinstance(metadata, dict) or not metadata.get("currency")):
            continue
        effective_state[key] = {**metadata, "basis": basis}
    return effective_state


def validate_replay(ops, tables):
    """Re-check persisted claims that bind to tables present on this request.

    Claims for sheets that are not attached yet remain in the audit log, but an active claim that
    becomes applicable to a newly uploaded/replaced sheet must satisfy today's source-precedence and
    value checks before it can synthesize a column. An applicable legacy record without authenticated
    provenance asks the user to restate it; a later clear removes it before validation.
    """
    by_name = {t["name"]: t for t in tables or ()}
    active = {}
    for raw in ops or ():
        if not isinstance(raw, dict) or raw.get("op") not in _OPS:
            continue
        key = (raw.get("table"), raw.get("column"))
        if raw["op"] == "clear_measure_metadata":
            active.pop(key, None)
        else:
            active[key] = raw
    for (table_name, column), raw in active.items():
        table = by_name.get(table_name)
        if table is None or column not in table.get("columns", ()):
            continue
        basis = raw.get("basis")
        if not (isinstance(basis, dict) and basis.get("attested") is True):
            raise DatasetOpError(
                f"please restate the currency for {table_name!r}.{column!r}; its saved claim "
                "predates authenticated provenance"
            )
        validate_ops([raw], tables, attested=True)
    return list(ops or ())


def apply(tables, ops):
    """Apply effective metadata to normalized tables IN PLACE; return the semantics records.

    A currency claim appends one synthesized constant column for the claimed measure to the
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
        if currency:
            ccy_column = synthetic_currency_column(column)
            if ccy_column not in table["columns"]:
                table["columns"] = list(table["columns"]) + [ccy_column]
                table["rows"] = [list(row) + [currency] for row in table["rows"]]
        records.append({
            "table": table_name, "column": column, "currency": currency,
            "date_column": metadata.get("date_column"),
            "currency_column": ccy_column if currency else None,
            "basis": metadata.get("basis"), "supplied_by": "conversation",
        })
    return records
