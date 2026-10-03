"""The schema and single-request wording prompt for deterministic SQL search.

Gemini sees table and column names, inferred types, listed foreign keys, and the current question. It
does not receive cell values, conversation history, or full table rows.
"""
from __future__ import annotations

REWRITE_SYSTEM = (
    "You reword a question about the user's tables so that a deterministic SQL planner can read it. "
    "Use the exact table and column names supplied in the schema when they express the same meaning. "
    "Keep the user's operation and every filter, named entity, value, date, number, and currency exactly "
    "as stated. Do not invent or infer values from the schema. "
    "Do not treat words inside a column name as an operation unless the user asks for that operation. "
    "Do not add or drop a condition, do not answer the question, and do not write SQL. "
    "If the question cannot be answered from these tables, return it unchanged."
)

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}},
    "required": ["question"],
}


def schema_text(graph) -> str:
    """Render table and column names, types, and declared relationships without cell data."""
    lines = ["【DB_ID】 SQLite database", "【Schema】"]
    for table_name in graph.tables:
        lines.extend((f"# Table: {table_name}", "["))
        fields = []
        for column in graph.by_table[table_name]:
            type_name = getattr(column.ref.type, "value", str(column.ref.type)).upper()
            fields.append(f"({column.ref.name}:{type_name})")
        lines.append(",\n".join(fields))
        lines.append("]")
    relationships = [
        f"{left.table}.{left.name}={right.table}.{right.name}"
        for edge in graph.foreign_keys
        for left, right in edge.column_pairs
    ]
    if relationships:
        lines.append("【Foreign keys】")
        lines.extend(relationships)
    return "\n".join(lines)


def rewrite_prompt(graph, question: str) -> str:
    return "Tables:\n" + schema_text(graph) + "\n\nQuestion:\n" + question
