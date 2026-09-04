"""Model-image/database release smoke run as the real serving identity."""
from __future__ import annotations

import json
import uuid

from engine.pg import _pg, _TableQueryPg
from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    ColumnRef,
    SelectItem,
    SelectQuery,
    SQLType,
    render_query,
)
from engine.tables import table_from_rows


def _assert_reasoning_result(result: dict) -> None:
    rows = (result.get("result") or {}).get("rows")
    if result.get("error") or result.get("clarify") or rows != [["3.3"]]:
        raise RuntimeError(f"model-backed reasoning smoke mismatch: {result!r}")


def run() -> dict:
    connection = _pg()
    try:
        cursor = connection.cursor()
        # Exercise the v2 request-budget schema through the serving role instead of
        # exposing the admin-only migration ledger to runtime.
        cursor.execute(
            "SELECT period, bucket_start, subject_key, operation, request_count "
            "FROM chat.request_usage WHERE false"
        )
        cursor.execute(
            "SELECT lease_id, subject_key, operation, expires_at "
            "FROM chat.request_lease WHERE false"
        )
        cursor.execute("SELECT to_regclass('knowledgebase.schedule'), to_regclass('knowledgebase.exchange_rate')")
        schedule, exchange_rate = cursor.fetchone()
        if schedule is None or exchange_rate is None:
            raise RuntimeError("seeded knowledgebase relations are missing")
    finally:
        connection.close()

    # A unique production-shaped schema proves the runtime role can provision a
    # fresh conversation and cannot collide with a schema owned by an older role.
    schema = f"c_{uuid.uuid4().hex}"
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
    reasoning = None
    try:
        columns, rows = executor.execute(
            {"ledger": table}, planner_schema, render_query(query), query=query,
        )
        if columns != ["total"] or rows != [("900719925474099.33",)]:
            raise RuntimeError(f"exact calculation smoke mismatch: {columns!r} {rows!r}")

        # A health endpoint proves only that weights loaded. This request exercises
        # the active ranker, typed AST planner, upload path, PostgreSQL execution,
        # and result contract using the exact image being promoted.
        from engine.knowledge import KnowledgeReasoner

        reasoning = KnowledgeReasoner().serve(
            [table_from_rows(
                "orders", ["order_id", "amount"],
                [["1", "1.10"], ["2", "2.20"]],
            )],
            "what is the total amount",
            schema,
        )
        _assert_reasoning_result(reasoning)
    finally:
        connection = _pg()
        try:
            cursor = connection.cursor()
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            connection.commit()
        finally:
            connection.close()
    return {
        "ok": True,
        "request_budgets": True,
        "exact_total": rows[0][0],
        "reasoning_sql": reasoning["sql"],
        "reasoning_total": reasoning["result"]["rows"][0][0],
    }


def main() -> int:
    print(json.dumps(run(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
