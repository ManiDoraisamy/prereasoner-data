"""Build the clean qid-keyed `wikipedia` schema: one EMPTY table per taxonomy leaf,
named by its EXACT Wikidata label, columns copied from the knowledgebase."<leaf>" mirror table
(create those first with mirror_schema.py / sync_entity.py --schema-only), with
**qid as PRIMARY KEY (NOT NULL)** — so every join is qid PK/FK. NO data migration:
lazy sync fills each table on first use.

OPTIONAL for a fresh instance: sync_entity.ensure_entity creates missing wikipedia
tables on demand, so a fresh deployment works without this. It exists to pre-create the
known leaves and, opt-in, to wipe per-user schemas for a fully fresh qid-keyed start.

SAFETY (the destructive schema drop is OPT-IN):
  - DEFAULT (no flag) = DRY-RUN: prints which per-user/test schemas WOULD be dropped,
    drops NOTHING. The non-destructive wikipedia-table creation still runs and commits.
  - --yes (alias --force) = actually drop the matched schemas.
  - Only schemas matching a KNOWN generated pattern are ever eligible: a Google `sub`
    (long all-digit id) or `*_test`. public/world/wikipedia/pg_* are never dropped.

Run (after sync_types.py; mirror_schema.py recommended first):
  export KB_PG_HOST=... KB_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/build_wikipedia.py            # dry-run (creates tables, drops nothing)
  python db/sync/build_wikipedia.py --yes      # also drop per-user/test schemas
"""
from __future__ import annotations
import argparse
import csv
import re
from pathlib import Path

try:
    from _conn import connect
    from sync_entity import snake
except ImportError:
    from ._conn import connect
    from .sync_entity import snake

TAXONOMY = Path(__file__).resolve().parent / "data" / "taxonomy.csv"
KEEP_SCHEMAS = {"public", "knowledgebase", "information_schema", "pg_catalog", "pg_toast"}
_GOOGLE_SUB_RE = re.compile(r"^\d{15,}$")          # per-user schemas ARE the verified Google `sub`


def _leaf_qid():
    """{snake(leaf label) -> qid} from taxonomy.csv (accepted/added rows)."""
    out = {}
    for r in csv.DictReader(open(TAXONOMY, encoding="utf-8")):
        if r.get("status") not in ("accepted", "added"):
            continue
        cats = [r[f"category_{i}"] for i in range(1, 10) if r.get(f"category_{i}")]
        if cats:
            out[snake(cats[-1])] = r["qid"]
    return out


def _is_droppable(schema: str) -> bool:
    if schema in KEEP_SCHEMAS or schema.startswith("pg_"):
        return False
    return bool(_GOOGLE_SUB_RE.match(schema)) or schema.endswith("_test")


def _exists(cur, schema, table):
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (schema, table))
    return cur.fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description="Build the clean qid-keyed `wikipedia` schema; optionally drop "
                                             "per-user (Google-sub) + *_test schemas for a fresh start.")
    ap.add_argument("--yes", "--force", dest="yes", action="store_true",
                    help="ACTUALLY drop the matched per-user/test schemas (default: dry-run).")
    args = ap.parse_args()

    conn = connect(); cur = conn.cursor()
    leaf_qid = _leaf_qid()

    # ---- 1+2: wikipedia schema + EMPTY faithful tables, qid PRIMARY KEY ----
    cur.execute("CREATE SCHEMA IF NOT EXISTS knowledgebase")
    cur.execute('SELECT qid, label FROM knowledgebase."types"')
    qlabel = {q: l for q, l in cur.fetchall()}
    created, skipped, seen = 0, 0, {}
    for leaf in sorted(leaf_qid):
        qid = leaf_qid[leaf]
        if not _exists(cur, "world", leaf):                                 # no faithful schema mirror -> discover later
            skipped += 1; continue
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='knowledgebase' AND table_name=%s "
                    "AND column_name='qid'", (leaf,))
        if not cur.fetchone():
            skipped += 1; continue
        label = (qlabel.get(qid) or leaf).strip()[:63]                      # EXACT Wikidata name (<=63 ident limit)
        if label in seen:
            label = f"{label} ({qid})"[:63]                                 # disambiguate a shared label
        seen[label] = leaf
        cur.execute(f'DROP TABLE IF EXISTS knowledgebase."{label}" CASCADE')    # fresh + idempotent (tables are empty)
        cur.execute(f'CREATE TABLE knowledgebase."{label}" (LIKE knowledgebase."{leaf}")')   # faithful columns
        cur.execute(f'ALTER TABLE knowledgebase."{label}" ALTER COLUMN qid SET NOT NULL')
        cur.execute(f'ALTER TABLE knowledgebase."{label}" ADD PRIMARY KEY (qid)')     # qid PK — joins are qid PK/FK
        created += 1
    print(f"wikipedia: {created} EMPTY faithful tables created (qid PK), {skipped} leaves skipped (no mirror schema)", flush=True)

    # ---- 3: DROP every user (Google-sub) + test schema -> fresh, qid-keyed start ----
    cur.execute("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")
    all_schemas = [s for (s,) in cur.fetchall()]
    targets = [s for s in all_schemas if _is_droppable(s)]
    skipped_nonmatch = [s for s in all_schemas
                        if s not in KEEP_SCHEMAS and not s.startswith("pg_") and not _is_droppable(s)]
    if skipped_nonmatch:
        print(f"NOT touching {len(skipped_nonmatch)} non-matching schema(s) (kept for safety): {skipped_nonmatch}",
              flush=True)

    if not args.yes:
        print(f"DRY-RUN: would DROP {len(targets)} per-user/test schema(s): {targets}", flush=True)
        print("DRY-RUN: nothing dropped. Re-run with --yes (or --force) to actually drop them.", flush=True)
        conn.commit()   # persist ONLY the non-destructive wikipedia-table creation
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='knowledgebase'")
        print(f"committed wikipedia tables only. wikipedia now has {cur.fetchone()[0]} tables "
              f"(all empty, lazy-fill on demand).", flush=True)
        return

    dropped = []
    for s in targets:
        cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
        dropped.append(s)
    print(f"dropped {len(dropped)} user/test schemas: {dropped}", flush=True)

    conn.commit()
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='knowledgebase'")
    print(f"committed. wikipedia now has {cur.fetchone()[0]} tables (all empty, lazy-fill on demand).", flush=True)


if __name__ == "__main__":
    main()
