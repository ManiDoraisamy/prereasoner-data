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


def schema_prompt(tables: list[dict], question: str) -> str:
    lines = [f"{table['name']}({', '.join(str(column) for column in table['columns'])})"
             for table in tables]
    return "-- schema\n" + "\n".join(lines) + f"\n-- question\n{question}\n-- sql\n"
