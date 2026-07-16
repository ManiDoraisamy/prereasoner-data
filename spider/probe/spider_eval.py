"""Shared Spider schema and denotation utilities.

This module deliberately has no engine or probe-runner dependencies. Evaluation and
training scripts can import it without loading a model, planner, or command-line entry
point.
"""
from __future__ import annotations

import collections
from typing import Any, Mapping, Sequence


def gold_table_names(
    example: Mapping[str, Any], tables_meta: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Return original table names from the gold query's top-level FROM clause."""
    names = []
    table_names = tables_meta[str(example["db_id"])]["table_names_original"]
    for table_unit in example["sql"]["from"]["table_units"]:
        if table_unit[0] == "table_unit" and isinstance(table_unit[1], int):
            names.append(str(table_names[table_unit[1]]))
    return list(dict.fromkeys(names))


def recursive_gold_table_names(
    example: Mapping[str, Any], tables_meta: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """Return original table names from the complete parsed gold query tree."""
    table_names = tables_meta[str(example["db_id"])]["table_names_original"]
    indexes = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
            return
        if not isinstance(value, (list, tuple)):
            return
        if (len(value) >= 2 and value[0] == "table_unit"
                and isinstance(value[1], int) and value[1] >= 0):
            indexes.append(value[1])
            return
        for child in value:
            visit(child)

    visit(example.get("sql", {}))
    return list(dict.fromkeys(str(table_names[index]) for index in indexes))


def spider_foreign_keys(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convert Spider's indexed foreign keys to the planner's named edge format."""
    columns = meta["column_names_original"]
    tables = meta["table_names_original"]
    edges = []
    for from_index, to_index in meta.get("foreign_keys", []):
        from_table, from_column = columns[from_index]
        to_table, to_column = columns[to_index]
        if from_table < 0 or to_table < 0:
            continue
        edges.append({
            "from_table": tables[from_table],
            "from_col": from_column,
            "to_table": tables[to_table],
            "to_col": to_column,
            "conf": 1.0,
        })
    return edges


def normalize_value(value: Any) -> str | float:
    """Normalize SQLite values for the benchmark's denotation comparison."""
    if value is None:
        return "\u2205"
    text = str(value).strip()
    try:
        return round(float(text.replace(",", "").lstrip("$").rstrip("%")), 3)
    except (ValueError, AttributeError):
        return text.lower()


def flat_value_set(rows: Sequence[Sequence[Any]] | None) -> set[str | float]:
    return {normalize_value(value) for row in (rows or []) for value in row}


def is_scalar(rows: Sequence[Sequence[Any]] | None) -> bool:
    return rows is not None and len(rows) == 1 and len(rows[0]) == 1


def compare(
    gold_rows: Sequence[Sequence[Any]] | None,
    predicted_rows: Sequence[Sequence[Any]] | None,
) -> dict[str, bool | None]:
    """Compare denotations using the probe suite's lenient, scalar, and strict metrics."""
    if gold_rows is None:
        return {"gold_exec_error": True}
    gold_values = flat_value_set(gold_rows)
    predicted_values = flat_value_set(predicted_rows)
    lenient = bool(gold_values) and gold_values <= predicted_values
    scalar_exact = None
    if is_scalar(gold_rows):
        scalar_exact = normalize_value(gold_rows[0][0]) in predicted_values

    def row_sets(rows: Sequence[Sequence[Any]] | None) -> collections.Counter:
        return collections.Counter(
            frozenset(normalize_value(value) for value in row)
            for row in (rows or [])
        )

    return {
        "lenient": lenient,
        "scalar_exact": scalar_exact,
        "strict": row_sets(gold_rows) == row_sets(predicted_rows),
        "gold_scalar": is_scalar(gold_rows),
    }
