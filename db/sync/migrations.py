"""Versioned migrations for already-materialized source-owned schemas.

Source synchronization and schema migration are separate operations. This runner never
creates an absent publisher schema, downloads data, or changes source release identity.
Each applied migration is checksummed in the source schema and advances the materialized
``release.schema_version`` for every release transformed by that migration.
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass

from db.sync._conn import connect


@dataclass(frozen=True)
class Migration:
    source_schema: str
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


MIGRATIONS = (
    Migration("cldr", 2, "typed_territory_currency_dates", (
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='cldr' AND table_name='territory_currency'
              AND column_name='valid_from' AND data_type='text'
          ) THEN
            ALTER TABLE cldr.territory_currency ALTER COLUMN valid_from DROP NOT NULL;
            ALTER TABLE cldr.territory_currency
              ALTER COLUMN valid_from TYPE date USING NULLIF(valid_from, '')::date;
          END IF;
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema='cldr' AND table_name='territory_currency'
              AND column_name='valid_to' AND data_type='text'
          ) THEN
            ALTER TABLE cldr.territory_currency ALTER COLUMN valid_to DROP NOT NULL;
            ALTER TABLE cldr.territory_currency
              ALTER COLUMN valid_to TYPE date USING NULLIF(valid_to, '')::date;
          END IF;
        END $$
        """,
    )),
    Migration("ec_tedb", 2, "replace_optional_country_coverage", (
        """
        CREATE TABLE IF NOT EXISTS ec_tedb.country_coverage (
          release_id text NOT NULL REFERENCES ec_tedb.release(release_id),
          member_state text NOT NULL,
          cn_code_provided boolean NOT NULL,
          cpa_code_provided boolean NOT NULL,
          PRIMARY KEY (release_id, member_state)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ec_tedb.response_status (
          release_id text NOT NULL REFERENCES ec_tedb.release(release_id),
          member_state text NOT NULL,
          requested boolean NOT NULL,
          returned_rate_count integer NOT NULL CHECK (returned_rate_count >= 0),
          cn_code_provided boolean,
          cpa_code_provided boolean,
          metadata_present boolean NOT NULL,
          PRIMARY KEY (release_id, member_state)
        )
        """,
        """
        WITH requested AS (
          SELECT r.release_id, item.member_state
          FROM ec_tedb.release r
          CROSS JOIN LATERAL jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(r.import_scope->'member_states') = 'array'
                 THEN r.import_scope->'member_states' ELSE '[]'::jsonb END
          ) AS item(member_state)
        ),
        observed AS (
          SELECT release_id, member_state FROM ec_tedb.vat_rate GROUP BY 1,2
        ),
        metadata AS (
          SELECT release_id, member_state, cn_code_provided, cpa_code_provided
          FROM ec_tedb.country_coverage
        ),
        states AS (
          SELECT release_id, member_state FROM requested
          UNION SELECT release_id, member_state FROM observed
          UNION SELECT release_id, member_state FROM metadata
        ),
        rate_counts AS (
          SELECT release_id, member_state, count(*)::integer AS rate_count
          FROM ec_tedb.vat_rate GROUP BY 1,2
        )
        INSERT INTO ec_tedb.response_status
          (release_id, member_state, requested, returned_rate_count,
           cn_code_provided, cpa_code_provided, metadata_present)
        SELECT s.release_id, s.member_state, req.member_state IS NOT NULL,
               coalesce(rc.rate_count, 0), md.cn_code_provided, md.cpa_code_provided,
               md.member_state IS NOT NULL
        FROM states s
        LEFT JOIN requested req USING (release_id, member_state)
        LEFT JOIN rate_counts rc USING (release_id, member_state)
        LEFT JOIN metadata md USING (release_id, member_state)
        ON CONFLICT (release_id, member_state) DO NOTHING
        """,
        """
        UPDATE ec_tedb.release r
        SET table_counts = (r.table_counts - 'country_coverage') ||
          jsonb_build_object('response_status', (
            SELECT count(*) FROM ec_tedb.response_status s WHERE s.release_id=r.release_id
          ))
        """,
        "DROP TABLE ec_tedb.country_coverage",
    )),
    Migration("geonames", 2, "release_scoped_postal_lookup_index", (
        "DROP INDEX IF EXISTS geonames.ix_geonames_postal_lookup",
        """
        CREATE INDEX IF NOT EXISTS ix_geonames_postal_release_lookup
          ON geonames.postal_code (release_id, country_code, postal_code)
        """,
    )),
)

_BY_SOURCE = {
    source: tuple(sorted((migration for migration in MIGRATIONS
                          if migration.source_schema == source), key=lambda item: item.version))
    for source in sorted({migration.source_schema for migration in MIGRATIONS})
}


def latest_schema_version(source_schema: str) -> int:
    migrations = _BY_SOURCE.get(source_schema, ())
    return migrations[-1].version if migrations else 1


def _validate_registry() -> None:
    for source, migrations in _BY_SOURCE.items():
        versions = [migration.version for migration in migrations]
        if versions != list(range(2, max(versions, default=1) + 1)):
            raise ValueError(f"{source}: migration versions must be contiguous from 2")
        if len({migration.name for migration in migrations}) != len(migrations):
            raise ValueError(f"{source}: migration names must be unique")


_validate_registry()


def migrate_source(conn, source_schema: str) -> tuple[int, ...]:
    """Apply pending migrations for one existing source schema and commit atomically."""
    if source_schema not in _BY_SOURCE:
        raise ValueError(f"no migrations registered for source {source_schema!r}")
    cur = conn.cursor()
    try:
        cur.execute("SELECT to_regnamespace(%s)", (source_schema,))
        if cur.fetchone()[0] is None:
            conn.rollback()
            return ()
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"prereasoner-source-migration:{source_schema}",))
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {source_schema}.schema_migration (
              version integer PRIMARY KEY,
              name text NOT NULL UNIQUE,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        cur.execute(f"SELECT version, name, checksum FROM {source_schema}.schema_migration")
        applied = {int(version): (name, checksum) for version, name, checksum in cur.fetchall()}
        known_versions = {migration.version for migration in _BY_SOURCE[source_schema]}
        unknown = set(applied) - known_versions
        if unknown:
            raise ValueError(f"{source_schema}: database has unknown migrations {sorted(unknown)}")
        completed = []
        for migration in _BY_SOURCE[source_schema]:
            previous = applied.get(migration.version)
            if previous:
                if previous != (migration.name, migration.checksum):
                    raise ValueError(
                        f"{source_schema}: migration {migration.version} checksum/name drift"
                    )
                continue
            for statement in migration.statements:
                cur.execute(statement)
            cur.execute(
                f"UPDATE {source_schema}.release SET schema_version=%s",
                (migration.version,),
            )
            cur.execute(
                f"INSERT INTO {source_schema}.schema_migration(version,name,checksum) "
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
    parser = argparse.ArgumentParser(description="Migrate existing source-owned schemas")
    parser.add_argument("--source", action="append", choices=sorted(_BY_SOURCE),
                        help="source schema to migrate; repeatable (default: all registered)")
    parser.add_argument("--dry-run", action="store_true", help="list versions without writing")
    args = parser.parse_args()
    sources = tuple(args.source or sorted(_BY_SOURCE))
    if args.dry_run:
        for source in sources:
            versions = ", ".join(
                f"v{migration.version}:{migration.name}" for migration in _BY_SOURCE[source]
            )
            print(f"{source}: {versions}")
        return
    conn = connect()
    try:
        for source in sources:
            applied = migrate_source(conn, source)
            print(f"{source}: applied {list(applied) if applied else 'none'}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
