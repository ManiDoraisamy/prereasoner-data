"""Maintenance catalog for the curated world tables — the ONE owner of knowledgebase.schedule.

Answers a question nothing else in the system could: *when was this world fact last verified, and
was it due?* The per-source `<source>.release` tables already record what DID land (release id,
content hash, materialized_at) but they are backward-looking, exist for only 9 sources, and live in
per-source schemas the serving role does not read. This table is the forward-looking half: for each
maintained table, who maintains it, how often it is expected to refresh, and when it last did.

Deliberately NOT listed here:

* the ``"... in the World"`` relations — they are VIEWS over the base tables (db/init.sql:282-287),
  so scheduling them would double-count the same data;
* the lazy-fill entity tables (``city``, ``hospital``, ``film`` …) created at request time by
  engine/knowledge_sync.py — they have no maintenance cadence by construction, and listing them
  would make the catalog claim upkeep that nobody performs.

``cadence_hours = None`` is a first-class, honest value: the table is rebuilt from a snapshot on
demand, not on a timer. It means "no automatic refresh", NOT "unknown".
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from db.sync._conn import connect


@dataclass(frozen=True)
class Maintained:
    table_name: str
    source: str
    source_schema: str | None   # the schema whose `release` table tracks it, when one exists
    cadence_hours: int | None   # None = rebuilt from a snapshot on demand, not on a timer
    note: str


# The declared catalog. Adding a maintained table here and nowhere else is deliberate: the row is
# upserted by ensure_catalog(), so the code stays the single source of truth for WHAT is maintained
# and the table carries only the observed facts (when it last refreshed, from which release).
CATALOG: tuple[Maintained, ...] = (
    Maintained("exchange_rate", "ecb", "ecb", 24,
               "ECB euro reference rates; rebuilt at 16:30 UTC by the <service_name>-ecb-rates-refresh "
               "Cloud Run job (infra/main.tf). The ECB does not publish at weekends, so a refresh may "
               "legitimately find no new print."),
    # The three tables the world-filter path actually joins. They are LAZY-FILLED per request by
    # engine/knowledge_sync.py:ensure_entity, so a row is fetched once when first needed and never
    # re-verified. cadence_hours=None states that plainly rather than implying upkeep nobody does.
    Maintained("city", "wikidata-lazy", None, None,
               "Filled on demand when an uploaded cell first resolves to a city; rows are never re-verified."),
    Maintained("country", "wikidata-lazy", None, None,
               "Filled on demand from a resolved city's country FK; rows are never re-verified."),
    Maintained("words", "wikidata", None, None,
               "The pgvector resolution index (db/sync/build_words.py); rebuilt with the snapshot."),
    Maintained("types", "wikidata", None, None,
               "Taxonomy leaves (db/sync/sync_types.py); rebuilt with the snapshot."),
    Maintained("Cities", "wikidata", None, None,
               "Curated settlements (db/sync/build_world.py); rebuilt with the snapshot."),
    Maintained("Countries", "wikidata", None, None,
               "Curated countries + continent/currency (db/sync/build_world.py)."),
    Maintained("Continents", "wikidata", None, None,
               "Curated continents (db/sync/build_world.py)."),
    Maintained("States", "wikidata", None, None,
               "Curated first-level administrative areas (db/sync/build_world.py)."),
    Maintained("Places", "wikidata", None, None,
               "Curated place long tail (db/sync/build_world.py)."),
    Maintained("Elements", "wikidata", None, None,
               "Chemical elements (db/sync/build_world.py); effectively static."),
    Maintained("Country Aliases", "wikidata", None, None,
               "Country name variants (db/sync/build_words.py --city-aliases)."),
    Maintained("u_s_state", "wikidata", None, None,
               "US states (db/sync/build_u_s_state.py); rebuilt with the snapshot."),
)

CREATE_SCHEDULE = """
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


def ensure_catalog(cur) -> int:
    """Upsert the declared catalog. Idempotent; never clears observed refresh facts."""
    cur.execute(CREATE_SCHEDULE)
    for m in CATALOG:
        cur.execute(
            'INSERT INTO knowledgebase."schedule"'
            " (table_name, source, source_schema, cadence_hours, note)"
            " VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (table_name) DO UPDATE SET"
            "   source=EXCLUDED.source, source_schema=EXCLUDED.source_schema,"
            "   cadence_hours=EXCLUDED.cadence_hours, note=EXCLUDED.note",
            (m.table_name, m.source, m.source_schema, m.cadence_hours, m.note),
        )
    # A table that leaves the catalog stops being claimed as maintained.
    cur.execute('DELETE FROM knowledgebase."schedule" WHERE table_name <> ALL(%s)',
                ([m.table_name for m in CATALOG],))
    return len(CATALOG)


def record_refresh(cur, table_name: str, release_id: str | None = None) -> None:
    """Record that `table_name` just refreshed successfully. Called by the sync job that wrote it,
    AFTER its data is committed — a refresh that failed must not advance the clock."""
    if not any(m.table_name == table_name for m in CATALOG):
        raise ValueError(f"{table_name} is not a declared maintained table; add it to CATALOG first")
    cur.execute(f'SELECT count(*) FROM knowledgebase."{table_name}"')
    rows = cur.fetchone()[0]
    cur.execute(
        'UPDATE knowledgebase."schedule"'
        " SET last_refreshed_at=now(), last_release_id=%s, row_count=%s, recorded_at=now()"
        " WHERE table_name=%s",
        (release_id, rows, table_name),
    )


def backfill(cur) -> list[tuple[str, str]]:
    """Seed observed facts for tables that refreshed BEFORE this catalog existed, so the first
    deployment does not report every table as never-refreshed. Uses the authoritative evidence that
    already exists: the active row in <source>.release, else the newest per-row updated_at."""
    done = []
    for m in CATALOG:
        cur.execute('SELECT last_refreshed_at FROM knowledgebase."schedule" WHERE table_name=%s',
                    (m.table_name,))
        row = cur.fetchone()
        if row and row[0] is not None:
            continue                                    # already observed; never overwrite
        stamp = release_id = None
        if m.source_schema:
            cur.execute("SELECT to_regclass(%s)", (f"{m.source_schema}.release",))
            if cur.fetchone()[0] is not None:
                cur.execute(f'SELECT release_id, materialized_at FROM "{m.source_schema}".release'
                            " WHERE status='active' ORDER BY materialized_at DESC LIMIT 1")
                r = cur.fetchone()
                if r:
                    release_id, stamp = r[0], r[1]
                    done.append((m.table_name, f"active {m.source_schema} release {r[0][:10]}"))
        if stamp is None:
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='knowledgebase'"
                        " AND table_name=%s AND column_name='updated_at'", (m.table_name,))
            if cur.fetchone():
                cur.execute(f'SELECT max(updated_at) FROM knowledgebase."{m.table_name}"')
                stamp = cur.fetchone()[0]
                if stamp is not None:
                    done.append((m.table_name, f"newest updated_at {stamp}"))
        if stamp is None:
            continue                                    # genuinely unknown — leave NULL, do not invent
        cur.execute(
            'UPDATE knowledgebase."schedule" SET last_refreshed_at=%s, last_release_id=%s,'
            ' row_count=%s WHERE table_name=%s',
            (stamp, release_id, _count(cur, m.table_name), m.table_name),
        )
    return done


def _count(cur, table_name: str) -> int:
    cur.execute(f'SELECT count(*) FROM knowledgebase."{table_name}"')
    return cur.fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backfill", action="store_true",
                    help="seed last_refreshed_at from existing release rows / updated_at columns")
    ap.add_argument("--show", action="store_true", help="print the catalog and its observed state")
    args = ap.parse_args()
    conn = connect()
    try:
        cur = conn.cursor()
        n = ensure_catalog(cur)
        seeded = backfill(cur) if args.backfill else []
        conn.commit()
        print(f"catalog: {n} maintained tables")
        for t, why in seeded:
            print(f"  backfilled {t}: {why}")
        if args.show:
            cur.execute('SELECT table_name, source, cadence_hours, last_refreshed_at, row_count'
                        ' FROM knowledgebase."schedule" ORDER BY table_name')
            for t, s, c, l, rc in cur.fetchall():
                due = f"{c}h" if c else "on demand"
                print(f"  {t:18} {s:9} every {due:10} last={l} rows={rc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
