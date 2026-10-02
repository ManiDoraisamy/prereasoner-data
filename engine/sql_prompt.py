"""The schema and single-request wording prompt for deterministic SQL search.

Gemini sees table and column names, inferred types, listed foreign keys, up to three sample values per
column, and the current question. It does not receive the conversation or full table rows.
"""
from __future__ import annotations

import json

EXAMPLES_PER_COLUMN = 3
EXAMPLE_CHARS = 64

REWRITE_SYSTEM = (
    "You reword a question about the user's tables so that a deterministic SQL planner can read it. "
    "The planner matches words to the tables' column names and values, and reads plain operation "
    "words: how many, total, average, highest, lowest, top N, by <column>, per <column>. "
    "Reword the question with the tables' own column names and values and those plain words. "
    "Keep every value, name, date, number and currency the question states, exactly as written. "
    "Do not add or drop a condition, do not answer the question, and do not write SQL. "
    "If the question cannot be answered from these tables, return it unchanged."
)

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}},
    "required": ["question"],
}


def schema_text(graph) -> str:
    """Render typed schema and bounded examples in XiYan-SQL's M-Schema layout."""
    lines = ["【DB_ID】 SQLite database", "【Schema】"]
    for table_name in graph.tables:
        lines.extend((f"# Table: {table_name}", "["))
        fields = []
        for column in graph.by_table[table_name]:
            type_name = getattr(column.ref.type, "value", str(column.ref.type)).upper()
            field = f"({column.ref.name}:{type_name}"
            examples = [value for value in column.values if value is not None][:EXAMPLES_PER_COLUMN]
            if examples:
                encoded = [json.dumps(value, ensure_ascii=False, default=str)[:EXAMPLE_CHARS]
                           for value in examples]
                field += ", Examples: [" + ", ".join(encoded) + "]"
            fields.append(field + ")")
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
