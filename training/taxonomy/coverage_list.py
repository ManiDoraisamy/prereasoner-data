"""
gen20 — coverage audit: which column CLUSTERS in columns.csv are NOT represented in the accepted taxonomy, and
what each one's ACCURATE Wikidata node is (resolved by searching the cluster's cell VALUES -> P31, never invented).

A cluster is REPRESENTED iff its resolved class QID survives rollup into an active (accepted|added) taxonomy node —
either it stayed accepted, or it was rolled into a node that's accepted/added. Everything else is a gap:
  - entity-likely but its cl_qid was rejected-and-dropped (orphan/over_specific/confusable-no-home), or
  - entity-likely but neither the type NAME nor the value->P31 fallback resolved a class, or
  - non-entity (codes/ids/times) — expected, no Wikidata entity by construction.

For every gap we print the GROUNDED node: the QID the values actually resolve to (value_p31), with its label — so the
list is auditable against Wikidata, not made up.

  $env:PYTHONUTF8=1; python -m training.taxonomy.coverage_list
"""
from __future__ import annotations
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.taxonomy.organize_taxonomy import WD                            # noqa: E402
from training.taxonomy.reconcile_taxonomy import value_p31, VCACHE            # noqa: E402

OUT = ROOT / "training/data"


def main():
    clusters = {c["cluster"]: c for c in json.load(open(OUT / "clusters.json", encoding="utf-8"))}
    cl_qid = json.load(open(OUT / "cluster_qid.json", encoding="utf-8"))      # {str cid: qid}
    fallback = json.load(open(OUT / "value_fallback.json", encoding="utf-8")) if (OUT / "value_fallback.json").exists() else {}
    renames = json.load(open(OUT / "cluster_renames.json", encoding="utf-8"))
    vcache = json.load(open(VCACHE, encoding="utf-8")) if VCACHE.exists() else {}
    tax = list(csv.DictReader(open(OUT / "taxonomy.csv", encoding="utf-8")))
    status = {r["qid"]: r["status"] for r in tax}
    rej_for = {r["qid"]: r["rejected_for"] for r in tax}
    active = {q for q, s in status.items() if s in ("accepted", "added")}
    absorbed_to = {}                                                         # rejected qid -> the active node it rolled into
    for r in tax:
        for q in (r["added_for"] or "").split(";"):
            if q:
                absorbed_to[q] = r["qid"]
    covered = active | set(absorbed_to)

    wd = WD()

    def rn(cid):
        return (renames.get(str(cid)) or {}).get("renamed") or clusters[int(cid)]["name"]

    def lbl(q):
        wd.chain(q); return wd.lbl.get(q, q)

    def final_node(q):                                                       # where q lands after rollup
        return q if q in active else absorbed_to.get(q, q)

    # ---- (A) recovered by the grounded value->P31 fallback ----
    print("=" * 100)
    print(f"RECOVERED by value->P31 fallback ({len(fallback)} clusters — name found no class, VALUES gave the real type):")
    for cid, q in sorted(fallback.items(), key=lambda kv: -clusters[int(kv[0])]["n"]):
        c = clusters[int(cid)]
        f = final_node(q)
        print(f"  {c['n']:4d}  {rn(cid):30s}  values->P31 = {lbl(q):26s} ({q})  ->  {lbl(f)} ({f})")

    # ---- (B) still NOT represented ----
    ent_gaps, non_gaps = [], []
    for cid, c in clusters.items():
        q = cl_qid.get(str(cid))
        if q and final_node(q) in active:
            continue                                                         # represented
        r = renames.get(str(cid)) or {}
        (ent_gaps if r.get("is_entity") else non_gaps).append((cid, c, q))

    print("\n" + "=" * 100)
    print(f"NOT REPRESENTED — entity-likely ({len(ent_gaps)}), with accurate Wikidata node (value->P31, searched):")
    json.dump(vcache, open(VCACHE, "w", encoding="utf-8"))  # placeholder so file exists
    for cid, c, q in sorted(ent_gaps, key=lambda t: -t[1]["n"]):
        node = q if q else value_p31(c.get("values", []), vcache)            # resolve the grounded node if not already
        json.dump(vcache, open(VCACHE, "w", encoding="utf-8"))
        why = (f"resolved->{lbl(q)} but {rej_for.get(q) or 'dropped'}" if q else
               (f"value->P31 = {lbl(node)} ({node}) [not added: below class-guard]" if node else "values resolve to no single type"))
        vals = " ; ".join(c.get("values", [])[:3])
        print(f"  {c['n']:4d}  {rn(cid):30s}  [{why}]")
        print(f"         e.g. {vals[:80]}")

    print("\n" + "=" * 100)
    nzero = sum(c["n"] for _, c, _ in non_gaps)
    print(f"NOT REPRESENTED — non-entity ({len(non_gaps)} clusters / {nzero} columns): codes/ids/times/free-text — no")
    print("Wikidata entity by construction (auto-marked by the proper-noun heuristic). A few examples:")
    for cid, c, q in sorted(non_gaps, key=lambda t: -t[1]["n"])[:12]:
        vals = " ; ".join(str(v) for v in c.get("values", [])[:3])
        print(f"  {c['n']:4d}  {c['name']:24s}  e.g. {vals[:60]}")

    nent = sum(c["n"] for _, c, _ in ent_gaps)
    print("\n" + "=" * 100)
    print(f"SUMMARY: {len(clusters)} clusters. represented {len(clusters)-len(ent_gaps)-len(non_gaps)} | "
          f"entity gaps {len(ent_gaps)} ({nent} cols) | non-entity {len(non_gaps)} ({nzero} cols)")


if __name__ == "__main__":
    main()
