"""Real-PostgreSQL exact-numeric parity check used by CI."""
from __future__ import annotations

import os

import psycopg2

from engine.pg import _PGTYPE


def main() -> int:
    connection = psycopg2.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", "postgres"),
    )
    try:
        cursor = connection.cursor()
        cursor.execute(f"CREATE TEMP TABLE exact_values(value {_PGTYPE['REAL']})")
        cursor.executemany("INSERT INTO exact_values VALUES (%s)", [
            ("9007199254740993.1",), ("0.1",),
        ])
        cursor.execute("SELECT sum(value), 1::numeric / 3::numeric FROM exact_values")
        total, third = cursor.fetchone()
        assert total == "9007199254740993.2"
        assert third == "0.33333333333333333333"
    finally:
        connection.close()
    print("PostgreSQL NUMERIC parity: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
