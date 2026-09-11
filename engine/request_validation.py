"""Canonical validation for requests that carry user tables.

The engine and orchestrator accept different top-level request shapes, but table
names and payload limits must mean exactly the same thing at both boundaries.
This module is intentionally lightweight so the chat image can import it without
loading the SQL/model runtime.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from engine.analysis import AnalysisError, validate_analysis_spec

MAX_QUESTION_CHARS = 20_000
MAX_HISTORY_ITEMS = 24
MAX_HISTORY_CHARS = 80_000
MAX_TABLES = 8
MAX_TABLE_DISPLAY_NAME_CHARS = 128
MAX_TABLE_CHARS = 2_000_000
MAX_TABLE_TOTAL_CHARS = 6_000_000

# PostgreSQL identifiers are limited to 63 bytes. Runtime bridge tables append
# " unconnected to knowledgebase" (29 characters), so uploaded identifiers use
# at most 34 ASCII bytes and can never be silently truncated into a collision.
MAX_TABLE_IDENTIFIER_BYTES = 34
_KNOWN_TABLE_EXTENSIONS = re.compile(r"\.(csv|tsv|txt|xlsx|xlsm|xls)$", re.IGNORECASE)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CONVERSATION_ID = re.compile(r"^c_[0-9a-f]{32}$")
_EXECUTION_USE = {
    "auto": "auto",
    "sql": "sql",
    "py": "python",
    "python": "python",
    "both": "verify",
    "verify": "verify",
}


@dataclass(frozen=True)
class RequestValidationError(ValueError):
    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message


def canonical_table_name(name: object, index: int = 0) -> str:
    """Return one collision-resistant SQL/planner identifier for a display name."""
    display = str(name or "").strip()
    stem = _KNOWN_TABLE_EXTENSIONS.sub("", display)
    normalized = re.sub(r"[^0-9A-Za-z_]+", "_", stem).strip("_").lower()
    normalized = normalized or f"t{index}"
    if len(normalized.encode("ascii")) <= MAX_TABLE_IDENTIFIER_BYTES:
        return normalized
    digest = hashlib.sha256(normalized.encode("ascii")).hexdigest()[:8]
    prefix = normalized[:MAX_TABLE_IDENTIFIER_BYTES - len(digest) - 1].rstrip("_")
    return f"{prefix}_{digest}"


def _optional_id(req: dict, name: str, *, conversation: bool = False) -> str | None:
    value = req.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RequestValidationError(f"{name} is invalid")
    value = value.strip()
    pattern = _CONVERSATION_ID if conversation else _SAFE_REQUEST_ID
    if not pattern.fullmatch(value):
        raise RequestValidationError(f"{name} is invalid")
    return value


def validate_tables(value, *, allow_single: bool = False) -> list[dict]:
    """Validate table objects and replace display names with canonical names."""
    if allow_single and isinstance(value, dict):
        value = [value]
    if value is None:
        return []
    if not isinstance(value, list):
        raise RequestValidationError("tables must be a list")
    if len(value) > MAX_TABLES:
        raise RequestValidationError(f"tables must contain at most {MAX_TABLES} items", 413)

    tables = []
    identifiers: dict[str, str] = {}
    total_chars = 0
    for index, table in enumerate(value):
        if not isinstance(table, dict):
            raise RequestValidationError("each table must be an object")
        raw_name = table.get("name", f"t{index}")
        data = table.get("data", "")
        if raw_name in (None, ""):
            raw_name = f"t{index}"
        if data is None:
            data = ""
        if not isinstance(raw_name, str) or not isinstance(data, str):
            raise RequestValidationError("table name and data must be strings")
        display_name = raw_name.strip()
        if len(display_name) > MAX_TABLE_DISPLAY_NAME_CHARS:
            raise RequestValidationError("table name is too long")
        if len(data) > MAX_TABLE_CHARS:
            raise RequestValidationError("table is too large", 413)
        total_chars += len(data)
        if total_chars > MAX_TABLE_TOTAL_CHARS:
            raise RequestValidationError("uploaded tables are too large", 413)
        identifier = canonical_table_name(display_name, index)
        previous = identifiers.get(identifier)
        if previous is not None:
            raise RequestValidationError(
                f"table names {previous!r} and {display_name!r} resolve to the same identifier"
            )
        identifiers[identifier] = display_name
        tables.append({"name": identifier, "data": data})
    return tables


def validate_question(value, *, field: str = "question") -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError(f"{field} is required")
    value = value.strip()
    if len(value) > MAX_QUESTION_CHARS:
        raise RequestValidationError(f"{field} is too long", 413)
    return value


def validate_as_of(value) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise RequestValidationError("as_of must be an ISO date")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise RequestValidationError("as_of must be an ISO date") from exc


def validate_execution_use(value) -> str | None:
    """Normalize the user-facing deterministic backend selector.

    ``use`` is deliberately request-local.  The deployment environment still supplies the
    default mode, while a signed-in browser may explicitly request SQL, Python, or parity
    verification for one named analysis without mutating process-wide configuration.
    """
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise RequestValidationError("use must be sql, py, or both")
    normalized = value.strip().lower()
    try:
        return _EXECUTION_USE[normalized]
    except KeyError as exc:
        raise RequestValidationError("use must be sql, py, or both") from exc


def validate_reason_request(req: object) -> dict:
    """Validate the authenticated /api/reason and /api/knowledge body."""
    if not isinstance(req, dict):
        raise RequestValidationError("request must be a JSON object")
    normalized = dict(req)
    normalized["question"] = validate_question(req.get("question"))
    normalized["tables"] = validate_tables(req.get("tables"), allow_single=True)
    if not normalized["tables"]:
        data = req.get("data", "")
        if not isinstance(data, str):
            raise RequestValidationError("CSV data must be text")
        if len(data) > MAX_TABLE_CHARS:
            raise RequestValidationError("uploaded table is too large", 413)
        raw_name = req.get("table", "data")
        if not isinstance(raw_name, str):
            raise RequestValidationError("table name must be text")
        if len(raw_name.strip()) > MAX_TABLE_DISPLAY_NAME_CHARS:
            raise RequestValidationError("table name is too long")
        normalized["data"] = data
        normalized["table"] = canonical_table_name(raw_name, 0)
    normalized["jobId"] = _optional_id(req, "jobId")
    normalized["conversation_id"] = _optional_id(req, "conversation_id", conversation=True)
    normalized["as_of"] = validate_as_of(req.get("as_of"))
    normalized["use"] = validate_execution_use(req.get("use"))
    normalized["dataset_ops"] = _validate_dataset_ops_shape(req.get("dataset_ops"))
    try:
        from engine.decomposition import DecompositionError, validate_decomposition

        normalized["decomposition"] = validate_decomposition(req.get("decomposition"))
    except DecompositionError as exc:
        raise RequestValidationError(str(exc)) from exc
    try:
        normalized["analysis"] = validate_analysis_spec(req.get("analysis"))
    except AnalysisError as exc:
        raise RequestValidationError(str(exc)) from exc
    return normalized


def _validate_dataset_ops_shape(ops):
    """Structural bounds for the dataset-semantics op list (transport tier only).

    Grammar and table binding are owned by engine.dataset_semantics.validate_ops — that tier reads
    the parsed tables, which this module deliberately does not. Here we only refuse shapes that
    could not possibly be ops (non-lists, non-string fields, oversized payloads) before anything
    reaches a conversation write."""
    if ops is None:
        return None
    if not isinstance(ops, list):
        raise RequestValidationError("dataset_ops must be a list")
    if len(ops) > 50:
        raise RequestValidationError("too many dataset_ops")
    import json as _json
    for op in ops:
        if not isinstance(op, dict):
            raise RequestValidationError("each dataset op must be an object")
        for key in ("op", "table", "column"):
            value = op.get(key)
            if not isinstance(value, str) or not value or len(value) > 200:
                raise RequestValidationError(f"dataset op field {key!r} must be a short string")
        metadata = op.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise RequestValidationError("dataset op metadata must be an object")
        if len(_json.dumps(op, ensure_ascii=False)) > 2000:
            raise RequestValidationError("dataset op is too large")
    return ops


def validate_chat_request(req: object):
    """Validate and bound every client-controlled object before paid inference."""
    if not isinstance(req, dict):
        raise RequestValidationError("request must be a JSON object")
    message = validate_question(req.get("message"), field="message")
    tables = validate_tables(req.get("tables"))

    history = req.get("history") or []
    if not isinstance(history, list) or len(history) > MAX_HISTORY_ITEMS:
        raise RequestValidationError("history is too long", 413)
    normalized_history = []
    history_chars = 0
    for item in history:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            raise RequestValidationError("history contains an invalid message")
        content = item.get("content")
        if not isinstance(content, str) or len(content) > MAX_QUESTION_CHARS:
            raise RequestValidationError("history message is too long", 413)
        history_chars += len(content)
        if history_chars > MAX_HISTORY_CHARS:
            raise RequestValidationError("history is too large", 413)
        normalized_history.append({"role": item["role"], "content": content})

    return (message, tables, normalized_history, _optional_id(req, "turnId"),
            _optional_id(req, "conversation_id", conversation=True),
            validate_execution_use(req.get("use")))
