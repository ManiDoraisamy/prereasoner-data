"""Admin-run migrations for application-owned database schemas.

Serving requests must not create or alter shared application tables.  Fresh databases
get the base schema from ``db/init.sql``; this command applies forward-only upgrades
before a non-superuser serving role is allowed to connect.
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass

from db.sync._conn import connect


@dataclass(frozen=True)
class ApplicationMigration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(
            statement.strip() for statement in self.statements
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


CHAT_MIGRATIONS = (
    ApplicationMigration(
        1,
        "conversation_state",
        (
            'ALTER TABLE "chat"."conversation" '
            'ADD COLUMN IF NOT EXISTS state jsonb',
        ),
    ),
)


def migrate_chat(conn) -> tuple[int, ...]:
    """Apply pending chat migrations in one admin transaction.

    The base chat tables must already exist from ``db/init.sql``.  Failing here is
    deliberate: it prevents a partial application bootstrap from being mistaken for
    a ready serving database.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regnamespace('chat')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("chat schema is missing; apply db/init.sql first")
        cur.execute(
            "SELECT to_regclass('chat.user_profile'), "
            "to_regclass('chat.conversation'), "
            "to_regclass('chat.user_conversation')"
        )
        if any(item is None for item in cur.fetchone()):
            raise RuntimeError("chat tables are missing; apply db/init.sql first")

        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("prereasoner-application-migration:chat",))
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS "chat"."schema_migration" (
              version integer PRIMARY KEY,
              name text NOT NULL UNIQUE,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute('SELECT version, name, checksum FROM "chat"."schema_migration"')
        applied = {int(version): (name, checksum) for version, name, checksum in cur.fetchall()}
        known_versions = {migration.version for migration in CHAT_MIGRATIONS}
        unknown = set(applied) - known_versions
        if unknown:
            raise ValueError(f"chat database has unknown migrations {sorted(unknown)}")

        completed = []
        for migration in CHAT_MIGRATIONS:
            previous = applied.get(migration.version)
            if previous:
                if previous != (migration.name, migration.checksum):
                    raise ValueError(
                        f"chat migration {migration.version} checksum/name drift"
                    )
                continue
            for statement in migration.statements:
                cur.execute(statement)
            cur.execute(
                'INSERT INTO "chat"."schema_migration"(version,name,checksum) '
                "VALUES (%s,%s,%s)",
                (migration.version, migration.name, migration.checksum),
            )
            completed.append(migration.version)
        conn.commit()
        return tuple(completed)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list application migrations without writing")
    args = parser.parse_args()
    if args.dry_run:
        for migration in CHAT_MIGRATIONS:
            print(f"chat: v{migration.version}:{migration.name}")
        return

    conn = connect()
    try:
        applied = migrate_chat(conn)
        print(f"chat: applied {list(applied) if applied else 'none'}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
