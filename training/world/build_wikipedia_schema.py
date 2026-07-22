"""
gen20 — build the qid-keyed entity tables in the `knowledgebase` schema (formerly a separate `wikipedia` schema).
Original intent: "create the exact wikidata name as table name along with the exact columns … let the lazy sync work.
Also delete all user and test schema and start from scratch so that the primary key - foreign key is always qid."

WHAT IT DOES:
  1. CREATE SCHEMA knowledgebase (if absent).
  2. One EMPTY table per router leaf, named by its EXACT Wikidata label, with the faithful Wikidata property columns
     (copied from the knowledgebase."<leaf>" mirror schema that was built by property-frequency discovery) + **qid as PRIMARY KEY
     (NOT NULL)** — so every join is qid PK/FK. NO data migration: lazy-sync fills each table on first use
     (sync_wikidata_world.lazy_resolve resolves a CSV cell -> qid, fetches that one entity from Wikidata, inserts it).
  3. DROP every per-user (Google-sub) schema and *_test schema, so uploads + the "<csv> connected to wikipedia" bridge
     are rebuilt fresh and qid-keyed (the old name-keyed bridges are gone). Keeps only public / world / wikipedia / system.

SAFETY (the destructive step 3 is OPT-IN):
  - DEFAULT (no flag) = DRY-RUN: prints exactly which per-user/test schemas WOULD be dropped, drops NOTHING, exits 0.
    The non-destructive wikipedia-table creation (step 1+2) still runs and commits.
  - --yes (alias --force) = actually drop the matched schemas.
  - Only schemas matching a KNOWN generated pattern are ever eligible: a Google `sub` (long all-digit id) or a
    `*_test` schema. KEEP_SCHEMAS (public/world/wikipedia/information_schema/pg_catalog/pg_toast) and any `pg_*` are
    never dropped; any other unexpected schema is reported and left untouched (never dropped).

The `world` schema (old hand-crafted "… in the World" tables) is left in place for now; it is dropped only after the
serving is verified on `wikipedia`.

  $env:KB_PG_PASSWORD=(gcloud secrets versions access latest --secret=prereasoner-kb-pg-password --project prereasoner-inference)
  $env:PYTHONUTF8=1; 
  python -m training.world.build_wikipedia_schema            # DRY-RUN (lists drops, drops nothing)
  python -m training.world.build_wikipedia_schema --yes      # actually drop per-user/test schemas
"""
from __future__ import annotations
import argparse, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.pg import _pg                                            # noqa: E402
from training.corpus.build_review import LEAF_QID                         # noqa: E402

KEEP_SCHEMAS = {"public", "knowledgebase", "information_schema", "pg_catalog", "pg_toast"}

# Only schemas matching a KNOWN generated pattern are eligible to be dropped — never an arbitrary schema.
#   - per-user schemas ARE the server-verified Google `sub` (a long, all-digit id, typically 21 chars).
#   - test schemas end in `_test`.
# Anything in KEEP_SCHEMAS, anything `pg_*`, and anything not matching one of these is left untouched.
_GOOGLE_SUB_RE = re.compile(r"^\d{15,}$")          # Google `sub`: long numeric id (>=15 digits)


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
                    help="ACTUALLY drop the matched per-user/test schemas. Without this flag, the destructive "
                         "step is a DRY-RUN: it only lists which schemas WOULD be dropped and drops nothing.")
    args = ap.parse_args()

    conn = _pg(); cur = conn.cursor()

    # ---- 1+2: wikipedia schema + EMPTY faithful tables, qid PRIMARY KEY ----
    cur.execute("CREATE SCHEMA IF NOT EXISTS knowledgebase")
    cur.execute('SELECT qid, label FROM knowledgebase."types"')
    qlabel = {q: l for q, l in cur.fetchall()}
    created, skipped, seen = 0, 0, {}
    for leaf in sorted(LEAF_QID):
        qid = LEAF_QID[leaf]
        if not _exists(cur, "knowledgebase", leaf):                         # no faithful schema mirror -> discover later
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
    # Only schemas matching the KNOWN generated pattern (Google-sub id or *_test) are eligible; everything else
    # (and the KEEP_SCHEMAS / pg_* set) is left untouched. Skipped non-matching schemas are reported for audit.
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
        conn.commit()   # persist ONLY the non-destructive wikipedia-table creation from step 1+2
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
