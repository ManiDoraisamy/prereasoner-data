"""
gen20 — CLUSTER COHERENCE gate. The scaled MiniBatchKMeans produces some GARBAGE buckets: a cluster whose member
columns are unrelated (e.g. Q2235308 'ship_type' held real ships AND Speciality/Personality/Occupation/Department
columns). Stamping one leaf on an incoherent bucket mislabels ~all its members and creates contradictory bare-token
targets (mathematics -> ship_type AND academic_discipline).

This measures intra-cluster coherence = mean cosine of each member column ('header: values', bge-embedded) to the
cluster's own centroid, writes it back into clusters.json, and flags clusters below THRESH as 'incoherent' so
reconcile skips them (no leaf assigned, no training rows). Tight country/city clusters score high; grab-bag buckets low.

  $env:PYTHONUTF8=1; python -m training.corpus.cluster_coherence [--thresh 0.55]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.embedder import Embedder                                       # noqa: E402

OUT = ROOT / "training/data"


def member_text(m):
    return f"{m['header']}: " + "; ".join(str(v) for v in m.get("values", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresh", type=float, default=0.55, help="flag clusters with mean-to-centroid cosine below this")
    args = ap.parse_args()

    clusters = json.load(open(OUT / "clusters.json", encoding="utf-8"))
    rows = []                                                                # (cluster_index, member_text)
    for ci, c in enumerate(clusters):
        for m in c.get("members", []):
            rows.append((ci, member_text(m)))
    print(f"embedding {len(rows)} member columns across {len(clusters)} clusters (bge)...", flush=True)
    X = Embedder().encode([t for _, t in rows]).astype(np.float32)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)

    by_c = {}
    for (ci, _), x in zip(rows, X):
        by_c.setdefault(ci, []).append(x)
    coh = {}
    for ci, vs in by_c.items():
        v = np.asarray(vs, np.float32)
        cen = v.mean(0); cen /= (np.linalg.norm(cen) + 1e-9)                 # recomputed from THESE members (consistent)
        coh[ci] = float((v @ cen).mean())                                   # mean cosine member -> centroid

    for ci, c in enumerate(clusters):
        c["coherence"] = round(coh.get(ci, 1.0), 4)
        c["incoherent"] = coh.get(ci, 1.0) < args.thresh
    json.dump(clusters, open(OUT / "clusters.json", "w", encoding="utf-8"))

    vals = np.array([coh[ci] for ci in coh])
    print(f"coherence percentiles: p05={np.percentile(vals,5):.3f} p25={np.percentile(vals,25):.3f} "
          f"p50={np.percentile(vals,50):.3f} p75={np.percentile(vals,75):.3f} p95={np.percentile(vals,95):.3f}")
    nflag = sum(c["incoherent"] for c in clusters)
    print(f"thresh={args.thresh}: {nflag}/{len(clusters)} clusters flagged incoherent "
          f"({sum(c['n'] for c in clusters if c['incoherent'])} columns)")
    print("\nLOWEST-coherence clusters (the grab-bags to drop):")
    for c in sorted(clusters, key=lambda c: c["coherence"])[:12]:
        print(f"   coh={c['coherence']:.3f} n={c['n']:>4} name='{c['name']}'  e.g. {' ; '.join(str(v) for v in c['values'][:4])[:70]}")
    print("\nHIGHEST-coherence clusters (kept):")
    for c in sorted(clusters, key=lambda c: -c["coherence"])[:6]:
        print(f"   coh={c['coherence']:.3f} n={c['n']:>4} name='{c['name']}'  e.g. {' ; '.join(str(v) for v in c['values'][:4])[:70]}")


if __name__ == "__main__":
    main()
