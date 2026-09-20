"""The ONE prompt serialization for the proposer: training and inference import this.

Schema lines are deterministic (table order as given, column order as stored) and compact —
the 0.5B context is small and CPU prefill cost scales with prompt length. ``values`` is the
d4 experiment's literal-binding signal: sampled column values rendered inline. Adapters are
only valid for the prompt format they were trained with, so the default stays bare
(d1/d2 format) and d4-class adapters opt in explicitly.
"""
from __future__ import annotations

_VALUE_CHARS = 20


def sample_column_values(tables: list[dict], per_column: int = 2) -> dict:
    """First distinct non-null values per column, in row order — deterministic.

    The ONE sampling rule: the training sidecar (build_values.py, from the capped DB
    loader) and inference (propose.py, from the live normalized tables) both use it, so
    the prompt distribution cannot drift between the two.
    """
    out: dict = {}
    for table in tables:
        columns = list(table.get("columns") or ())
        samples: dict = {column: [] for column in columns}
        for row in table.get("rows") or ():
            done = True
            for column, value in zip(columns, row):
                bucket = samples[column]
                if value is None or len(bucket) >= per_column:
                    continue
                text = str(value)[:_VALUE_CHARS]
                if text not in bucket:
                    bucket.append(text)
                if len(bucket) < per_column:
                    done = False
            if done:
                break
        out[table["name"]] = samples
    return out


def schema_prompt(tables: list[dict], question: str, values: dict | None = None) -> str:
    lowered = {str(name).lower(): columns for name, columns in (values or {}).items()}
    lines = []
    for table in tables:
        table_values = {
            str(column).lower(): sampled
            for column, sampled in lowered.get(str(table["name"]).lower(), {}).items()
        }
        parts = []
        for column in table["columns"]:
            column = str(column)
            sampled = table_values.get(column.lower())
            parts.append(f"{column}={'|'.join(sampled)}" if sampled else column)
        lines.append(f"{table['name']}({', '.join(parts)})")
    return "-- schema\n" + "\n".join(lines) + f"\n-- question\n{question}\n-- sql\n"
