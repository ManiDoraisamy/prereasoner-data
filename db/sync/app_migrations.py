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
    ApplicationMigration(
        2,
        "distributed_request_budgets",
        (
            """
            CREATE TABLE IF NOT EXISTS "chat"."request_usage" (
              period text NOT NULL CHECK (period IN ('minute', 'day')),
              bucket_start timestamptz NOT NULL,
              subject_key text NOT NULL,
              operation text NOT NULL,
              request_count integer NOT NULL CHECK (request_count > 0),
              PRIMARY KEY (period, bucket_start, subject_key, operation)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS "chat"."request_lease" (
              lease_id text PRIMARY KEY,
              subject_key text NOT NULL,
              operation text NOT NULL,
              expires_at timestamptz NOT NULL
            )
            """,
            'CREATE INDEX IF NOT EXISTS "ix_request_lease_active" '
            'ON "chat"."request_lease" (operation, expires_at)',
        ),
    ),
)

# Legacy compatibility functions from the former request-time Wikidata fill path.
# Their SQL is retained because migration checksums are immutable. New bootstrap
# code revokes runtime EXECUTE; only offline synchronization may write shared facts.
_LAZY_ENSURE_TABLE = """
CREATE OR REPLACE FUNCTION knowledgebase.lazy_ensure_table(label text, cols text[])
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  coldefs text := '';
  c text;
BEGIN
  IF label IS NULL OR label = '' OR length(label) > 63 THEN
    RAISE EXCEPTION 'invalid lazy table label';
  END IF;
  FOREACH c IN ARRAY coalesce(cols, ARRAY[]::text[]) LOOP
    IF c IS NULL OR c = '' OR length(c) > 63 OR c IN ('qid', 'name') THEN
      RAISE EXCEPTION 'invalid lazy column: %', coalesce(c, '<null>');
    END IF;
    coldefs := coldefs || format(', %I TEXT', c);
  END LOOP;
  EXECUTE format(
    'CREATE TABLE IF NOT EXISTS knowledgebase.%I (qid TEXT PRIMARY KEY, name TEXT%s)',
    label, coldefs);
END
$fn$
"""

# ON CONFLICT (qid) is a structural guard as well as idempotence: only the
# entity-shaped lazy tables (qid PRIMARY KEY) have that arbiter, so the function
# cannot be aimed at curated non-entity tables such as exchange_rate or "words".
_LAZY_UPSERT_ENTITY = """
CREATE OR REPLACE FUNCTION knowledgebase.lazy_upsert_entity(label text, cols text[], vals text[])
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  collist text;
  sellist text;
BEGIN
  IF label IS NULL OR label = '' OR length(label) > 63 THEN
    RAISE EXCEPTION 'invalid lazy table label';
  END IF;
  IF cols IS NULL OR vals IS NULL OR array_length(cols, 1) IS NULL
     OR array_length(cols, 1) <> array_length(vals, 1)
     OR array_position(cols, 'qid') IS NULL THEN
    RAISE EXCEPTION 'lazy entity rows need matching cols/vals including qid';
  END IF;
  SELECT string_agg(format('%I', t.c), ', '), string_agg(format('($1)[%s]', t.i), ', ')
    INTO collist, sellist
    FROM unnest(cols) WITH ORDINALITY AS t(c, i);
  EXECUTE format(
    'INSERT INTO knowledgebase.%I (%s) SELECT %s ON CONFLICT (qid) DO NOTHING',
    label, collist, sellist) USING vals;
END
$fn$
"""

_LAZY_REGISTER_WORD = """
CREATE OR REPLACE FUNCTION knowledgebase.lazy_register_word(
  surface text, canonical text, wtype text, norm text, embedding text, qid text)
RETURNS void LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
  INSERT INTO knowledgebase."words"(surface, canonical, type, norm, embedding, qid)
  VALUES ($1, $2, $3, $4, $5::public.vector, $6)
  ON CONFLICT DO NOTHING
$fn$
"""

_SCHEDULE_TABLE = """
CREATE TABLE IF NOT EXISTS knowledgebase."schedule" (
  table_name        text PRIMARY KEY,
  source            text NOT NULL,
  source_schema     text,
  cadence_hours     integer,
  note              text,
  last_refreshed_at timestamptz,
  last_release_id   text,
  row_count         bigint,
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT schedule_cadence_positive CHECK (cadence_hours IS NULL OR cadence_hours > 0)
)
"""

KNOWLEDGEBASE_MIGRATIONS = (
    ApplicationMigration(
        1,
        "lazy_fill_definer_functions",
        (
            _LAZY_ENSURE_TABLE,
            _LAZY_UPSERT_ENTITY,
            _LAZY_REGISTER_WORD,
            "REVOKE ALL ON FUNCTION knowledgebase.lazy_ensure_table(text, text[]) FROM PUBLIC",
            "REVOKE ALL ON FUNCTION knowledgebase.lazy_upsert_entity(text, text[], text[]) FROM PUBLIC",
            "REVOKE ALL ON FUNCTION knowledgebase.lazy_register_word(text, text, text, text, text, text) "
            "FROM PUBLIC",
        ),
    ),
    ApplicationMigration(
        2,
        "maintenance_schedule",
        (_SCHEDULE_TABLE,),
    ),
)


def _migrate(conn, schema: str, required_tables: tuple[str, ...], migrations) -> tuple[int, ...]:
    """Apply one schema's pending migrations in one admin transaction.

    The schema and its base tables must already exist (``db/init.sql`` for chat, the
    seed sync for knowledgebase).  Failing here is deliberate: it prevents a partial
    application bootstrap from being mistaken for a ready serving database.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regnamespace(%s)", (schema,))
        if cur.fetchone()[0] is None:
            raise RuntimeError(f"{schema} schema is missing; seed the database first")
        cur.execute(
            "SELECT " + ", ".join("to_regclass(%s)" for _ in required_tables),
            [f"{schema}.{table}" for table in required_tables],
        )
        if any(item is None for item in cur.fetchone()):
            raise RuntimeError(f"{schema} tables are missing; seed the database first")

        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"prereasoner-application-migration:{schema}",))
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema}"."schema_migration" (
              version integer PRIMARY KEY,
              name text NOT NULL UNIQUE,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(f'SELECT version, name, checksum FROM "{schema}"."schema_migration"')
        applied = {int(version): (name, checksum) for version, name, checksum in cur.fetchall()}
        known_versions = {migration.version for migration in migrations}
        unknown = set(applied) - known_versions
        if unknown:
            raise ValueError(f"{schema} database has unknown migrations {sorted(unknown)}")

        completed = []
        for migration in migrations:
            previous = applied.get(migration.version)
            if previous:
                if previous != (migration.name, migration.checksum):
                    raise ValueError(
                        f"{schema} migration {migration.version} checksum/name drift"
                    )
                continue
            for statement in migration.statements:
                cur.execute(statement)
            cur.execute(
                f'INSERT INTO "{schema}"."schema_migration"(version,name,checksum) '
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


def migrate_chat(conn) -> tuple[int, ...]:
    return _migrate(conn, "chat", ("user_profile", "conversation", "user_conversation"),
                    CHAT_MIGRATIONS)


def migrate_knowledgebase(conn) -> tuple[int, ...]:
    return _migrate(conn, "knowledgebase", ("words", "types"), KNOWLEDGEBASE_MIGRATIONS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list application migrations without writing")
    args = parser.parse_args()
    if args.dry_run:
        for schema, migrations in (("chat", CHAT_MIGRATIONS),
                                   ("knowledgebase", KNOWLEDGEBASE_MIGRATIONS)):
            for migration in migrations:
                print(f"{schema}: v{migration.version}:{migration.name}")
        return

    conn = connect()
    try:
        for schema, migrate in (("chat", migrate_chat), ("knowledgebase", migrate_knowledgebase)):
            applied = migrate(conn)
            print(f"{schema}: applied {list(applied) if applied else 'none'}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
