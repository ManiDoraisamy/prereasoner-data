"""Bounded validation for the public chat request shape."""
from __future__ import annotations

MAX_MESSAGE_CHARS = 20_000
MAX_HISTORY_ITEMS = 24
MAX_HISTORY_CHARS = 80_000
MAX_TABLES = 8
MAX_TABLE_NAME_CHARS = 128
MAX_TABLE_CHARS = 2_000_000
MAX_TABLE_TOTAL_CHARS = 6_000_000


def validate_chat_request(req):
    """Validate and bound every client-controlled object before paid inference starts."""
    if not isinstance(req, dict):
        raise ValueError("request must be a JSON object")
    message = req.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message is required")
    message = message.strip()
    if len(message) > MAX_MESSAGE_CHARS:
        raise ValueError("message is too long")

    tables = req.get("tables") or []
    if not isinstance(tables, list) or len(tables) > MAX_TABLES:
        raise ValueError("tables must contain at most 8 items")
    normalized_tables = []
    total_chars = 0
    for table in tables:
        if not isinstance(table, dict):
            raise ValueError("each table must be an object")
        name = table.get("name") or "data"
        data = table.get("data") or ""
        if not isinstance(name, str) or not isinstance(data, str):
            raise ValueError("table name and data must be strings")
        name = name.strip()
        if len(name) > MAX_TABLE_NAME_CHARS or len(data) > MAX_TABLE_CHARS:
            raise ValueError("table is too large")
        total_chars += len(data)
        if total_chars > MAX_TABLE_TOTAL_CHARS:
            raise ValueError("uploaded tables are too large")
        normalized_tables.append({"name": name or "data", "data": data})

    history = req.get("history") or []
    if not isinstance(history, list) or len(history) > MAX_HISTORY_ITEMS:
        raise ValueError("history is too long")
    normalized_history = []
    history_chars = 0
    for item in history:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            raise ValueError("history contains an invalid message")
        content = item.get("content")
        if not isinstance(content, str) or len(content) > MAX_MESSAGE_CHARS:
            raise ValueError("history message is too long")
        history_chars += len(content)
        if history_chars > MAX_HISTORY_CHARS:
            raise ValueError("history is too large")
        normalized_history.append({"role": item["role"], "content": content})

    def optional_id(name):
        value = req.get(name)
        if value is None or value == "":
            return None
        if not isinstance(value, str) or len(value.strip()) > 128:
            raise ValueError(f"{name} is invalid")
        return value.strip() or None

    return (message, normalized_tables, normalized_history, optional_id("turnId"),
            optional_id("conversation_id"))
