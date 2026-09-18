"""The ONE prompt serialization for the proposer: training and inference import this.

Schema lines are deterministic (table order as given, column order as stored) and compact —
the 0.5B context is small and CPU prefill cost scales with prompt length.
"""
from __future__ import annotations


def schema_prompt(tables: list[dict], question: str) -> str:
    lines = []
    for table in tables:
        columns = ", ".join(str(column) for column in table["columns"])
        lines.append(f"{table['name']}({columns})")
    return "-- schema\n" + "\n".join(lines) + f"\n-- question\n{question}\n-- sql\n"
