"""FULL SCHEMA MIRROR — for every concrete accepted/added taxonomy leaf, discover its
real Wikidata property schema and CREATE the empty faithful table in the `wikipedia`
schema. Data is NOT bulk-loaded — it fills lazily via sync_entity.lazy_resolve /
ensure_entity when a CSV cell misses in knowledgebase."words".

OPTIONAL: ensure_entity creates missing tables on demand anyway; running this just
pre-creates them (and warms the property discovery) so first queries are faster.

Abstract leaves (legal form, taxonomic rank, "person or organization", ...) are
SKIPPED — they aren't world-entity tables.

Run (after sync_types.py):
  export KB_PG_HOST=... KB_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/mirror_schema.py
"""
from __future__ import annotations
import csv
import sys
from pathlib import Path

try:
    from _conn import connect
    from sync_entity import discover, ensure_table, snake
except ImportError:
    from ._conn import connect
    from .sync_entity import discover, ensure_table, snake

TAXONOMY = Path(__file__).resolve().parent / "data" / "taxonomy.csv"
# abstract / non-entity leaves — NOT world tables
SKIP = {"Q10541491", "Q11862829", "Q427626", "Q10856962", "Q106559804", "Q949344",
        "Q56061", "Q138341612", "Q11618417", "Q726"}


def leaf_label(r):
    cats = [r[f"category_{i}"] for i in range(1, 10) if r.get(f"category_{i}")]
    return cats[-1] if cats else r["qid"]


def main():
    rows = [r for r in csv.DictReader(open(TAXONOMY, encoding="utf-8"))
            if r.get("status") in ("accepted", "added")]
    conn = connect(); conn.autocommit = True; cur = conn.cursor()
    done = skip = fail = 0
    for r in rows:
        qid = r["qid"]; label = snake(leaf_label(r))
        if qid in SKIP:
            print(f"  SKIP abstract {label} ({qid})", flush=True); skip += 1; continue
        try:
            props = discover(qid)
            if not props:
                print(f"  skip {label}: no real-attribute properties", flush=True); skip += 1; continue
            ensure_table(cur, label, props)
            print(f"  OK  {label:28s} {len(props)} cols: {[c for _p, c, _l, _t in props][:6]}...", flush=True); done += 1
        except Exception as e:                                # noqa: BLE001 — one flaky WDQS type shouldn't abort
            print(f"  FAIL {label}: {type(e).__name__} {e}", flush=True); fail += 1
    print(f"\nSCHEMA MIRROR: {done} faithful tables created, {skip} skipped (abstract/empty), {fail} failed", flush=True)
    return 1 if done == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
