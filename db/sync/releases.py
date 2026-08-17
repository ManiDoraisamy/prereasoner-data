"""Atomic activation and rollback for already validated immutable source releases."""
from __future__ import annotations

import argparse
import re

from psycopg2 import sql

from db.sync._conn import connect


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def activate_validated_release(cur, schema_name: str, release_id: str) -> bool:
    """Activate a validated retired release; return False when it is already active."""
    if not _IDENTIFIER.fullmatch(schema_name or ""):
        raise ValueError("schema must be a lowercase PostgreSQL identifier")
    if not isinstance(release_id, str) or not release_id.strip():
        raise ValueError("release_id is required")
    schema = sql.Identifier(schema_name)
    cur.execute(sql.SQL("LOCK TABLE {}.release IN EXCLUSIVE MODE").format(schema))
    cur.execute(
        sql.SQL("SELECT status FROM {}.release WHERE release_id=%s").format(schema),
        (release_id,),
    )
    target = cur.fetchone()
    if target is None:
        raise ValueError(f"unknown {schema_name} release: {release_id}")
    if target[0] == "active":
        return False
    if target[0] != "retired":
        raise ValueError(
            f"{schema_name} release {release_id} is {target[0]!r}; only validated retired releases can activate"
        )
    cur.execute(sql.SQL("UPDATE {}.release SET status='retired' WHERE status='active'").format(schema))
    cur.execute(
        sql.SQL("UPDATE {}.release SET status='active' WHERE release_id=%s AND status='retired'").format(schema),
        (release_id,),
    )
    if cur.rowcount != 1:
        raise RuntimeError("release transition lost its target row")
    cur.execute(sql.SQL("SELECT count(*) FROM {}.release WHERE status='active'").format(schema))
    if cur.fetchone()[0] != 1:
        raise RuntimeError("release transition did not leave exactly one active release")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    connection = connect()
    try:
        with connection.cursor() as cur:
            changed = activate_validated_release(cur, args.schema, args.release_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print("activated" if changed else "already active", args.schema, args.release_id)


if __name__ == "__main__":
    main()
