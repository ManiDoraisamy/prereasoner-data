"""Idempotent minimal database bootstrap for a self-hosted Community deployment.

This command runs inside the immutable engine image as a short-lived Cloud Run Job. It
uses the privileged ``SYNC_PG_*`` connection only for initialization, then installs and
audits the non-superuser serving-role grants. The serving service never runs this module.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from psycopg2 import sql

from db.reference_grants import apply_reference_grants
from db.sync._conn import connect

ROOT = Path(__file__).resolve().parents[2]
INIT_SQL = ROOT / "db" / "init.sql"
BOOTSTRAP_VERSION = 2
_ROLE = re.compile(r"^[a-z][a-z0-9_]*$")
_LOCK_NAME = "prereasoner-community-bootstrap"


def command_plan() -> tuple[tuple[str, ...], ...]:
    """Return the deterministic, reviewable minimal-world build plan."""
    return (
        (sys.executable, "-m", "db.sync.sync_wikidata", "--reset", "--high-only"),
        (sys.executable, "-m", "db.sync.build_world"),
        (sys.executable, "-m", "db.sync.build_words", "--cities"),
        (sys.executable, "-m", "db.sync.sync_types"),
        (sys.executable, "-m", "db.sync.build_u_s_state"),
        (sys.executable, "-m", "db.sync.app_migrations"),
        (sys.executable, "-m", "db.sync.sources.ecb.sync"),
        (sys.executable, "-m", "db.sync.build_exchange_rate"),
        (sys.executable, "-m", "db.sync.schedule", "--backfill", "--show"),
    )


def _run(command: Sequence[str]) -> None:
    print("bootstrap:", " ".join(command[2:]), flush=True)
    subprocess.run(tuple(command), cwd=ROOT, check=True)


def _initialize_database(connection) -> None:
    if not INIT_SQL.is_file():
        raise RuntimeError(f"database initialization file is missing: {INIT_SQL}")
    with connection.cursor() as cursor:
        cursor.execute(INIT_SQL.read_text(encoding="utf-8"))
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledgebase.community_bootstrap (
              singleton       boolean PRIMARY KEY DEFAULT true CHECK (singleton),
              version         integer NOT NULL,
              status          text NOT NULL CHECK (status IN ('running','ready','failed')),
              started_at      timestamptz NOT NULL DEFAULT now(),
              completed_at    timestamptz,
              error           text
            )
            """
        )
    connection.commit()


def _ready(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT version, status FROM knowledgebase.community_bootstrap WHERE singleton"
        )
        row = cursor.fetchone()
    return bool(row and row == (BOOTSTRAP_VERSION, "ready"))


def _mark(connection, status: str, error: str | None = None) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO knowledgebase.community_bootstrap
              (singleton, version, status, started_at, completed_at, error)
            VALUES (true, %s, %s, now(), CASE WHEN %s = 'ready' THEN now() END, %s)
            ON CONFLICT (singleton) DO UPDATE SET
              version = EXCLUDED.version,
              status = EXCLUDED.status,
              started_at = CASE
                WHEN EXCLUDED.status = 'running' THEN now()
                ELSE knowledgebase.community_bootstrap.started_at
              END,
              completed_at = CASE WHEN EXCLUDED.status = 'ready' THEN now() END,
              error = EXCLUDED.error
            """,
            (BOOTSTRAP_VERSION, status, status, error),
        )
    connection.commit()


def _grant_serving_access(connection, role: str, datasets: frozenset[str]) -> None:
    role_id = sql.Identifier(role)
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        database = cursor.fetchone()[0]
        cursor.execute(sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(database), role_id
        ))
        for schema_name in ("knowledgebase", "public"):
            schema_id = sql.Identifier(schema_name)
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                schema_id, role_id
            ))
            cursor.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                schema_id, role_id
            ))
            cursor.execute(sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
            ).format(schema_id, role_id))
        apply_reference_grants(cursor, role, datasets)
    connection.commit()


def bootstrap(
    connection,
    role: str,
    datasets: frozenset[str] = frozenset(),
    *,
    force: bool = False,
    runner: Callable[[Sequence[str]], None] = _run,
) -> bool:
    """Build the minimal world once; return ``True`` when work was performed."""
    if not _ROLE.fullmatch(role or "") or role == "postgres":
        raise ValueError("role must be a non-postgres lowercase PostgreSQL identifier")

    _initialize_database(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_NAME,))
    try:
        if _ready(connection) and not force:
            print(f"bootstrap: version {BOOTSTRAP_VERSION} is already ready", flush=True)
            return False
        _mark(connection, "running")
        try:
            for command in command_plan():
                runner(command)
            _grant_serving_access(connection, role, datasets)
        except Exception as exc:
            connection.rollback()
            _mark(connection, "failed", str(exc)[:1000])
            raise
        _mark(connection, "ready")
        print(f"bootstrap: version {BOOTSTRAP_VERSION} ready", flush=True)
        return True
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))
        connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="serving", help="existing non-superuser serving role")
    parser.add_argument(
        "--datasets",
        default="",
        help="comma-separated code-approved reference datasets to grant after synchronization",
    )
    parser.add_argument("--force", action="store_true", help="rebuild the minimal world data")
    args = parser.parse_args()
    datasets = frozenset(item.strip() for item in args.datasets.split(",") if item.strip())
    connection = connect()
    try:
        bootstrap(connection, args.role, datasets, force=args.force)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
