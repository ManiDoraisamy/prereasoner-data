"""
gen20 — sync the taxonomy TYPE hierarchy into the live world DB, so the expanded world model knows EVERY accepted/
added type + its P279 parents to root, keyed by qid (the join + routing primary key).

  knowledgebase."types"  (qid PK, label, parent_qid, is_leaf, world_table, depth)  — the type DAG (one row per node)
  knowledgebase."words"  type='type' rows                                          — each type label embedded (bge) + qid,
                                                                             so a TYPE mention / column type resolves.

Grounded: reuses rollup_taxonomy.lpath(wd, qid) = the SAME clean qid+label P279 path the taxonomy is built from (glue/
country-bound intermediates dropped, capped at category_9) — never re-derived.

  $env:KB_PG_PASSWORD=(gcloud secrets versions access latest --secret=prereasoner-kb-pg-password --project prereasoner-inference)
  $env:PYTHONUTF8=1; python -m training.world.sync_world_types
"""
from __future__ import annotations
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.taxonomy.rollup_taxonomy import WD, lpath, NODE_TABLE        # noqa: E402  (clean P279 path + world-table map)
from training.lib.pg import _pg                                          # noqa: E402
from training.lib.embedder import Embedder, normalize_surface                  # noqa: E402

OUT = ROOT / "training/data"


def main():
    rows = [r for r in csv.DictReader(open(OUT / "taxonomy.csv", encoding="utf-8"))
            if r["status"] in ("accepted", "added")]
    leaves = {r["qid"] for r in rows}
    wd = WD()
    nodes = {}                                                             # qid -> {label, parent_qid}
    for r in rows:
        path = lpath(wd, r["qid"])                                         # [(qid,label)] root -> leaf (clean)
        for i, (q, lab) in enumerate(path):
            parent = path[i - 1][0] if i > 0 else None                     # parent = the node one step toward root
            nodes.setdefault(q, {"label": lab, "parent": parent})

    def depth(q, seen=()):
        p = nodes.get(q, {}).get("parent")
        return 0 if (not p or p not in nodes or q in seen) else 1 + depth(p, seen + (q,))

    cn = _pg(); cur = cn.cursor()
    cur.execute('DROP TABLE IF EXISTS knowledgebase."types"')
    cur.execute('CREATE TABLE knowledgebase."types" (qid text PRIMARY KEY, label text, parent_qid text, '
                'is_leaf boolean, world_table text, depth int)')
    for q, n in nodes.items():
        cur.execute('INSERT INTO knowledgebase."types" VALUES (%s,%s,%s,%s,%s,%s)',
                    (q, n["label"], n["parent"], q in leaves, NODE_TABLE.get(q), depth(q)))
    cur.execute('CREATE INDEX ix_types_parent ON knowledgebase."types"(parent_qid)')
    cur.execute('CREATE INDEX ix_types_leaf ON knowledgebase."types"(is_leaf)')
    cn.commit()

    # sync type labels into world.words (type='type') so a type mention / column type resolves by exact norm or bge NN
    emb = Embedder()
    items = list(nodes.items())
    vecs = emb.encode([n["label"] for _, n in items])
    cur.execute("DELETE FROM knowledgebase.\"words\" WHERE type='type'")
    for (q, n), v in zip(items, vecs):
        lit = "[" + ",".join(f"{x:.6f}" for x in v) + "]"
        cur.execute('INSERT INTO knowledgebase."words" (surface,canonical,type,norm,qid,embedding) '
                    'VALUES (%s,%s,%s,%s,%s,%s)', (n["label"], n["label"], "type",
                                                   normalize_surface(n["label"]), q, lit))
    cn.commit()
    nleaf = sum(1 for q in nodes if q in leaves)
    nmap = sum(1 for q in nodes if NODE_TABLE.get(q))
    print(f"synced world.types: {len(nodes)} nodes ({nleaf} leaves, {len(nodes)-nleaf} ancestors), "
          f"{nmap} world-table-mapped; type rows in words: {len(items)}")
    cn.close()


if __name__ == "__main__":
    main()
