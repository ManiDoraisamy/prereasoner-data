"""
gen20 — fetch CLEAN, discriminative training instances per NON-GEO leaf type straight from Wikidata, to replace the
noisy bge-mapped CSV cell values that poisoned the anchored dims (a 'referral' column mapped to `hospital` taught
'PHYS_REFERRAL'/'Pinos' as hospitals; columns of proper nouns taught `street` to fire on ANY proper noun, so `street`
out-fires `hospital` on "Mayo Clinic"). For each leaf qid we pull real instance LABELS (P31 = the leaf), preferring
NOTABLE ones (sitelink-ordered) so the set overlaps real demo data and what the frozen Qwen already knows.

Geo leaves (city/country/u_s_state/continent/chemical_element) are SKIPPED — their clean values already come from
knowledgebase."words". Output: training/data/type_instances.json = {leaf: [label, ...]}. Resumable (skips leaves already
present); re-run to top up. build_review.build_from_mapped prefers these over the mapped cell values.

  $env:PYTHONUTF8=1; python -m training.corpus.fetch_type_instances
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.wdqs import wdqs, V                        # noqa: E402  (robust SPARQL: timeout+retries)
from training.corpus.build_review import LEAF_QID, LEAF_PATH               # noqa: E402

OUT = ROOT / "training/data"
DEST = OUT / "type_instances.json"
# clean geo/struct-backed leaves come from knowledgebase."words"; skip them (and `human`, where a bare personal name can't be
# typed to a profession — that ambiguity is expected, not a label to teach).
SKIP = {"city", "country", "u_s_state", "continent", "chemical_element", "human"}
WANT = 220                                                                    # distinct instance labels / leaf (enough to anchor)


def _labels(qid, limit, ordered):
    """instance labels of `qid` (direct P31). ordered=True -> notable first (DESC sitelinks); on timeout the caller
    retries unordered (the sort over a big type is what blows the 60s budget)."""
    if ordered:
        q = (f"SELECT ?name WHERE {{ ?x wdt:P31 wd:{qid} ; wikibase:sitelinks ?sl ; rdfs:label ?name . "
             f"FILTER(lang(?name)='en') }} ORDER BY DESC(?sl) LIMIT {limit}")
    else:
        q = (f"SELECT ?name WHERE {{ ?x wdt:P31 wd:{qid} ; rdfs:label ?name . FILTER(lang(?name)='en') }} LIMIT {limit}")
    out = []
    for b in wdqs(q, timeout=58, retries=2):
        nm = V(b, "name")
        if nm and not nm.startswith("Q") and len(nm) > 1:
            out.append(nm)
    return out


def fetch_leaf(leaf, qid):
    """notable instances first; fall back to unordered if the sorted query times out; dedup case-insensitively."""
    rows = []
    for ordered in (True, False):
        try:
            rows = _labels(qid, WANT, ordered)
        except Exception as e:                                                # noqa: BLE001
            print(f"    {leaf} ({qid}) ordered={ordered} FAILED: {str(e)[:80]}", flush=True)
            rows = []
        if rows:
            break
    seen, uniq = set(), []
    for r in rows:
        k = r.strip().lower()
        if k and k not in seen:
            seen.add(k); uniq.append(r.strip())
    return uniq


def main():
    leaves = {lf: q for lf, q in LEAF_QID.items()
              if lf not in SKIP and lf == LEAF_PATH[lf][-1]}                  # true leaves with a qid, non-geo
    have = json.load(open(DEST, encoding="utf-8")) if DEST.exists() else {}
    todo = [(lf, q) for lf, q in sorted(leaves.items()) if not have.get(lf)]
    print(f"{len(leaves)} non-geo leaves; {len(have)} already cached; fetching {len(todo)}", flush=True)
    for i, (lf, q) in enumerate(todo, 1):
        inst = fetch_leaf(lf, q)
        if inst:
            have[lf] = inst
            json.dump(have, open(DEST, "w", encoding="utf-8"), ensure_ascii=False, indent=0)   # checkpoint each leaf
        print(f"  [{i}/{len(todo)}] {lf:28s} {q:>10s} -> {len(inst)} instances", flush=True)
    tot = sum(len(v) for v in have.values())
    print(f"done: {len(have)} leaves / {tot} clean instances -> {DEST}", flush=True)


if __name__ == "__main__":
    main()
