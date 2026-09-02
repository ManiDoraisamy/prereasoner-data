"""Model-image/database release smoke run as the real serving identity."""
from __future__ import annotations

import hashlib
import json

from engine.pg import _TableQueryPg, _pg
from engine.sql_ast import Aggregate, BinaryExpr, ColumnRef, SQLType, SelectItem, SelectQuery, render_query
from engine.tables import table_from_rows


def run() -> dict:
    connection = _pg()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT max(version) FROM chat.schema_migration")
        chat_version = cursor.fetchone()[0]
        if chat_version is None or int(chat_version) < 2:
            raise RuntimeError("chat application migrations are incomplete")
        cursor.execute("SELECT to_regclass('knowledgebase.schedule'), to_regclass('knowledgebase.exchange_rate')")
        schedule, exchange_rate = cursor.fetchone()
        if schedule is None or exchange_rate is None:
            raise RuntimeError("seeded knowledgebase relations are missing")
    finally:
        connection.close()

    schema = "c_" + hashlib.md5(b"prereasoner-release-smoke", usedforsecurity=False).hexdigest()
    table = table_from_rows(
        "ledger", ["amount", "rate"],
        [["9007199254740993.1", "0.1"], ["0.1", "0.2"]],
    )
    planner_schema = [
        {"table": "ledger", "name": "amount", "affinity": "REAL"},
        {"table": "ledger", "name": "rate", "affinity": "REAL"},
    ]
    amount = ColumnRef("ledger", "amount", SQLType.REAL)
    rate = ColumnRef("ledger", "rate", SQLType.REAL)
    query = SelectQuery((SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", rate)), "total"),), "ledger")
    executor = _TableQueryPg()
    executor._pg_schema = schema
    try:
        columns, rows = executor.execute(
            {"ledger": table}, planner_schema, render_query(query), query=query,
        )
        if columns != ["total"] or rows != [("900719925474099.33",)]:
            raise RuntimeError(f"exact calculation smoke mismatch: {columns!r} {rows!r}")
    finally:
        connection = _pg()
        try:
            cursor = connection.cursor()
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            connection.commit()
        finally:
            connection.close()
    return {"ok": True, "chat_migration": int(chat_version), "exact_total": rows[0][0]}


def main() -> int:
    print(json.dumps(run(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
