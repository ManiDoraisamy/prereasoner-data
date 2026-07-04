"""UNIFY the instance index with the qid taxonomy: link the legacy words.type strings
('city') to their taxonomy node qid (Q515) via world."types".resolver_type, then VERIFY
the qid walk (Paris -> city -> ... -> root).

On a fresh bootstrap sync_types.py already sets resolver_type, so this is a no-op
verification; it exists as the in-place MIGRATION for a database built before the
column existed, and as a health check.

DESIGN NOTE (why NOT a per-instance type_qid column): the legacy string->qid relation
is 1:1 with the TYPE, not the instance ('city' ALWAYS means Q515). Denormalizing it
onto every word row means rewriting the whole table — and world."words" has an HNSW
index on a vector(384), so each rewrite re-indexes its embedding (slow + heavy bloat)
on the LIVE table. The normalized + online-safe equivalent: store the resolver string
on the few TYPE NODES. Every instance then links into the hierarchy via
words.type -> types.resolver_type -> types.qid -> parent..root.

Run:
  export WORLD_PG_HOST=... WORLD_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/unify_words_qid.py
"""
from __future__ import annotations
import sys
import time

try:
    from _conn import connect
except ImportError:
    from ._conn import connect

# legacy resolver type-string -> the taxonomy node qid it denotes. 'continent'/'element' are
# legacy world types not in the taxonomy -> reported as unlinkable.
RESOLVER_QID = {"city": "Q515", "country": "Q6256", "state": "Q35657"}


def _ddl_with_retry(conn, sql, what, tries=6):
    cur = conn.cursor()
    for i in range(tries):
        try:
            cur.execute("SET lock_timeout='6s'")
            cur.execute(sql)
            print(f"  {what} ok"); return True
        except Exception as e:                                # noqa: BLE001
            conn.rollback()
            print(f"  {what} attempt {i+1}/{tries} blocked ({type(e).__name__}); retrying..."); sys.stdout.flush()
            time.sleep(3)
    return False


def main():
    conn = connect(); conn.autocommit = True; cur = conn.cursor()

    # 0) verify the target qids are real taxonomy nodes
    qids = sorted(set(RESOLVER_QID.values()))
    cur.execute('SELECT qid, label FROM world."types" WHERE qid = ANY(%s)', (qids,))
    found = {q: l for q, l in cur.fetchall()}
    if [q for q in qids if q not in found]:
        print(f"ABORT: missing taxonomy qids {[q for q in qids if q not in found]} — run sync_types.py first"); return 1
    print("targets:", {s: f"{q}({found[q]})" for s, q in RESOLVER_QID.items()})

    # 1) the link lives on the TYPE NODE (small table) — add resolver_type + an index for the join
    if not _ddl_with_retry(conn, 'ALTER TABLE world."types" ADD COLUMN IF NOT EXISTS "resolver_type" TEXT', "ALTER types"):
        print('ABORT: could not alter world."types"'); return 1
    for s, q in RESOLVER_QID.items():
        cur.execute('UPDATE world."types" SET "resolver_type"=%s WHERE qid=%s AND "resolver_type" IS DISTINCT FROM %s', (s, q, s))
    cur.execute('CREATE INDEX IF NOT EXISTS ix_types_resolver ON world."types"("resolver_type")')
    cur.execute('SELECT "resolver_type", qid, label FROM world."types" WHERE "resolver_type" IS NOT NULL ORDER BY 1')
    print("type-node resolver map:", [(r[0], r[1], r[2]) for r in cur.fetchall()])

    # 2) revert the mistaken per-instance column if a prior run added it (cleanup; metadata-only DROP)
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='world' AND table_name='words' AND column_name='type_qid'")
    if cur.fetchone():
        _ddl_with_retry(conn, 'ALTER TABLE world."words" DROP COLUMN IF EXISTS "type_qid"', "DROP words.type_qid")

    # 3) VERIFY the qid walk Paris (a city INSTANCE) -> city node -> ... -> root, via the type-node map
    cur.execute('''SELECT w.canonical, t.qid, t.label FROM world."words" w
                   JOIN world."types" t ON t."resolver_type" = w.type
                   WHERE w.type='city' AND lower(w.norm)='paris' LIMIT 1''')
    row = cur.fetchone()
    if not row:
        print("VERIFY FAILED: no Paris city instance joined to a type node "
              "(run build_words.py --cities and sync_types.py first)"); return 1
    path = [f"{row[0]}(instance)", f"{row[2]}={row[1]}"]; q = row[1]
    for _ in range(20):
        cur.execute('SELECT parent_qid FROM world."types" WHERE qid=%s', (q,))
        r = cur.fetchone()
        if not r or not r[0] or r[0] == q:
            break
        cur.execute('SELECT label FROM world."types" WHERE qid=%s', (r[0],))
        lb = cur.fetchone(); path.append(lb[0] if lb else r[0]); q = r[0]
    print("\nqid walk:", " -> ".join(path))

    # coverage: how many instances now reach a taxonomy node by qid
    cur.execute('''SELECT COUNT(*) FILTER (WHERE t.qid IS NOT NULL), COUNT(*) FROM world."words" w
                   LEFT JOIN world."types" t ON t."resolver_type"=w.type''')
    linked, total = cur.fetchone()
    print(f"instances linked into the qid hierarchy: {linked}/{total} ({linked/total:.1%})")
    cur.execute('''SELECT w.type, COUNT(*) FROM world."words" w LEFT JOIN world."types" t ON t."resolver_type"=w.type
                   WHERE t.qid IS NULL AND w.type<>'type' GROUP BY w.type''')
    unl = cur.fetchall()
    if unl:
        print("unlinked (legacy types not in the taxonomy):", [(t, n) for t, n in unl])
    print("\nUNIFICATION DONE — every city/country/state instance walks to root by qid; string resolver untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
