"""The text Gemini reads when the labelled selection fallback runs (engine/sql_fallback.py).

``schema_text`` renders the request's typed schema; ``rewrite_prompt`` asks for one rewording of the
question in the tables' own words, and ``propose_prompt`` for one SQLite SELECT. Each reply is a JSON
object of the matching ``*_SCHEMA`` shape. Gemini never sees the rows: only table and column names,
inferred column types, foreign keys and at most three example values per column.
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

PROPOSE_SYSTEM = (
    "You are an SQLite expert. Write one SQLite SELECT statement that answers the question from the "
    "tables described. Use only the tables and columns listed, compare text columns with values as "
    "they appear in the examples, and join tables only on the listed foreign keys. Return only the "
    "statement."
)

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}},
    "required": ["question"],
}

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
}


def schema_text(graph) -> str:
    """Render the typed schema and bounded examples in XiYan-SQL's M-Schema layout.

    The section markers are M-Schema's own, between the full-width brackets U+3010 and U+3011:
    【DB_ID】, 【Schema】 and 【Foreign keys】 (XGenerationLab/M-Schema, ``to_mschema``). Column
    types are inferred by ``SchemaGraph`` from names and observed values. The labels intentionally
    describe that inference rather than claiming SQLite DDL types.
    """
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


def propose_prompt(graph, question: str) -> str:
    return "Database schema:\n" + schema_text(graph) + "\n\nQuestion:\n" + question
