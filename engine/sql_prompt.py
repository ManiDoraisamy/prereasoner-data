"""The one prompt the SQL proposer reads. Training, evaluation and serving all import it.

Every table becomes one line, ``name(column, column, ...)``, in the order given and with
columns in stored order, followed by the question:

    -- schema
    singer(Singer_ID, Name, Country)
    -- question
    How many singers are there?
    -- sql

The format is part of the proposer adapter's identity: an adapter only understands the
prompt it was fine-tuned on (``training/proposer/train_sft.py`` builds its targets with this
same function), so any change here requires retraining the adapter.
"""
from __future__ import annotations

import json


def schema_prompt(tables: list[dict], question: str) -> str:
    lines = [f"{table['name']}({', '.join(str(column) for column in table['columns'])})"
             for table in tables]
    return "-- schema\n" + "\n".join(lines) + f"\n-- question\n{question}\n-- sql\n"


def xiyan_mschema(graph) -> str:
    """Render the XiYan prompt's typed schema and bounded examples.

    Column types are inferred by ``SchemaGraph`` from names and observed values. The labels
    intentionally describe that inference rather than claiming SQLite DDL types.
    """
    lines = ["citeDB_ID SQLite database", "citeSchema"]
    for table_name in graph.tables:
        lines.extend((f"# Table: {table_name}", "["))
        fields = []
        for column in graph.by_table[table_name]:
            type_name = getattr(column.ref.type, "value", str(column.ref.type)).upper()
            field = f"({column.ref.name}:{type_name}"
            examples = [value for value in column.values if value is not None][:3]
            if examples:
                encoded = [json.dumps(value, ensure_ascii=False, default=str)[:64]
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
        lines.append("citeForeign keys")
        lines.extend(relationships)
    return "\n".join(lines)


def xiyansql_prompt(tokenizer, graph, question: str) -> str:
    """Build the pinned publisher chat prompt used by the measured Q4_K_M candidate."""
    user_prompt = (
        "You are an SQLite expert. Read and understand the database schema below, "
        "then use SQLite knowledge to write SQL that answers the user question.\n"
        "User question:\n" + question + "\n\n"
        "Database schema:\n" + xiyan_mschema(graph) + "\n\n"
        "Reference information:\n\n"
        "User question:\n" + question + "\n\n```sql"
    )
    rendered = tokenizer.apply_chat_template(
        [{"role": "user", "content": user_prompt}],
        tokenize=False,
        add_generation_prompt=True,
    )
    if not isinstance(rendered, str) or not rendered:
        raise RuntimeError("XiYanSQL tokenizer returned an empty chat prompt")
    return rendered
