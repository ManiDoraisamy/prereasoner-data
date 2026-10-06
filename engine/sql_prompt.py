"""The schema and single-request wording prompt for deterministic SQL search.

Gemini sees table and column names, inferred types, listed foreign keys, and the current question, with the
words the search reads as cell values quoted (``prompt_question``). It does not receive a cell value the question
does not state, conversation history, or table rows.
"""
from __future__ import annotations

import re

REWRITE_SYSTEM = (
    "You reword a question about the user's tables so that a deterministic SQL planner can read it. "
    "Use the exact table and column names supplied in the schema when they express the same meaning. "
    "Keep the user's operation and every filter, named entity, value, date, number, and currency exactly "
    "as stated. Do not invent or infer values from the schema. "
    "Do not treat words inside a column name as an operation unless the user asks for that operation. "
    "Do not add or drop a condition, do not answer the question, and do not write SQL. "
    "Do not name a table the question does not name: when several tables could answer, the planner "
    "chooses among them. "
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


def prompt_question(graph, question: str) -> str:
    """``question`` as the rewrite prompt states it: each phrase the search reads as a cell value in quotes, and
    one it reads as held inside a column's values (sql_search.substring_requests: "all inspection checklist")
    spelled as that column containing the quoted phrase. Gemini sees only names: over tabs named Inspection
    and Checklist it reworded "keyword volume for all inspection checklist" into keywords of the Inspection
    tab containing 'checklist' 5 times of 5, and with the phrase only quoted, into keywords "matching" it or
    the one keyword, 8 of 8; spelled, into keywords containing it, 8 of 8 (a customer's keyword sheet,
    2026-10-06). Only the question's own words are quoted, and only words: a number stays as written."""
    from engine.sql_expansion import word_spans
    from engine.sql_search import SQLSearcher, _quoted_positions, _tokens, substring_requests

    matches, _ = SQLSearcher(graph)._value_matches(_tokens(question), frozenset(), question)
    held = {(request.start, request.end): request
            for request in substring_requests(question, graph) if request.columns}
    spans, quoted = word_spans(question), _quoted_positions(question)
    out = question
    for start, end, _phrase, _options in reversed(matches):
        left, right = spans[start][0], spans[end - 1][1]
        if set(range(start, end)) & quoted or not re.search(r"[^\W\d_]", question[left:right]):
            continue
        request = held.get((start, end))
        phrase = "'" + out[left:right] + "'"
        out = out[:left] + (f"{request.columns[0].name} containing {phrase}" if request else phrase) + out[right:]
    return out


def rewrite_prompt(graph, question: str) -> str:
    return "Tables:\n" + schema_text(graph) + "\n\nQuestion:\n" + prompt_question(graph, question)
