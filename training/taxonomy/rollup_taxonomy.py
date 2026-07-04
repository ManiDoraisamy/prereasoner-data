"""
gen20 — roll up over-specific taxonomy leaves to a GENERAL (metadata) node, grounded on the real P279 chain, and
record the decision in taxonomy.csv via 3 columns: status (accepted|rejected|added), rejected_for, added_for.

Two grounded triggers:
  - NOT METADATA (data/proper-noun-specific): a leaf whose P279 path passes through "administrative territorial entity
    of a single country" (Q12076836) is a COUNTRY-bound division (region of Italy, province of China, district of
    India...). Roll it up to the generic "administrative territorial entity" (Q56061). rejected_for=not_metadata.
  - CONFUSABLE (crowded sibling subtypes): a non-world-table leaf whose nearest ancestor (within K P279 levels) is
    SHARED by >= MINSHARE leaves is one of a confusable family (baseball/basketball player/cricketer/... -> athlete;
    musician/politician/... -> person). Roll it up to that shared ancestor. rejected_for=confusable.

World-table types (city/country/state/...) are never rolled. taxonomy.csv keeps EVERY row: accepted leaves as-is,
rejected leaves marked + their columns remapped to the target, and any NEW target node added (added_for = the QIDs it
absorbed). Then build_review (skips status=rejected) regenerates assignment/inference from the rolled-up mapped_columns.

  $env:PYTHONUTF8=1; python -m training.taxonomy.rollup_taxonomy
"""
from __future__ import annotations
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.taxonomy.organize_taxonomy import WD, CACHE as P279_CACHE      # noqa: E402
import numpy as np                                                           # noqa: E402

T_CONF = 0.78                       # confusable: two sibling leaves whose VALUE-centroids cosine >= this (real signal)
DEPTH_CAP = 9                       # too_detailed: cap depth at category_9; a deeper leaf rolls up to its category_9 node

OUT = ROOT / "training/data"
DROP = {"Q35120", "Q136433660", "Q23958946", "Q99526405"}
# upper-ontology GLUE (shared by almost everything, no routing signal) — dropped from the path so depth/category_9 are
# MEANINGFUL: 'bird' stops being depth>9 (its real path is organism>heterotroph>animal>...>bird), 'politician' stays
# politician, while genuinely-deep 'racehorse' still caps to 'horse'.
GLUE = {"continuant", "independent continuant", "specifically dependent continuant", "anatomical entity",
        "physical anatomical entity", "being", "material entity", "object", "artificial object", "concrete object",
        "abstract entity", "collective entity", "occurrent", "physical object"}
NODE_TABLE = {"Q515": "Cities", "Q123964505": "Places", "Q6256": "Countries",
              "Q5107": "Continents", "Q11344": "Elements"}    # Q35657 'U.S. state' DROPPED — see FORCE_NOT_METADATA
WORLD = set(NODE_TABLE)
SINGLE_COUNTRY = "Q12076836"        # administrative territorial entity of a single country
ADMIN = "Q56061"                    # administrative territorial entity (the geo-admin gate)
# 'U.S. state' (Q35657) is a US-specific division exactly like the rejected 'state of Australia'/'province of Canada',
# but its ADJECTIVE label dodges COUNTRY_BOUND and it was kept only by the world-table privilege above. Drop the
# privilege: reject it as not_metadata -> ADMIN, so the model learns ONE consistent sub-national-division type, not a
# US-only special case. (The States Postgres table still exists; it's just no longer a first-class taxonomy type.)
FORCE_NOT_METADATA = {"Q35657"}
# country-SPECIFIC label: '...of Italy', '...of the United States', '...of a single country' (NOT 'federated state',
# 'region', 'administrative territorial entity'). Detects 'state of Australia'/'province of Canada' that route via
# 'federated state' and skip Q12076836, so not_metadata catches them by LABEL not by one specific path node.
COUNTRY_BOUND = re.compile(r"\bof (the )?[A-Z]|\bof a single country\b")
K = 3                              # confusable: ancestor within K levels...
MINSHARE = 3                       # ...shared by >= this many leaves


def lpath(wd, qid):
    """[(qid, label)] root->leaf — drop DROP roots, unlabeled nodes, GLUE, and country-SPECIFIC INTERMEDIATE nodes
    ('administrative territorial entity of the United States'). The LEAF is kept even if country-bound, so not_metadata
    can flag it (state of Mexico) while a world-table type's clean leaf (U.S. state) is preserved, not truncated."""
    raw, seen = [], set()
    for p in [x for x in reversed(wd.chain(qid)) if x not in DROP]:
        lab = wd.lbl.get(p, p)
        if re.fullmatch(r"Q\d+", str(lab)) or lab.lower() in GLUE or lab in seen:   # unlabeled / glue / dup — skip
            continue
        seen.add(lab); raw.append((p, lab))
    return [(p, lab) for i, (p, lab) in enumerate(raw)
            if i == len(raw) - 1 or not COUNTRY_BOUND.search(lab)]         # drop country-bound INTERMEDIATES, keep leaf


def labels_of(wd, qid):
    return [lab for _, lab in lpath(wd, qid)][:DEPTH_CAP]                  # cap at category_9 (drop category_10..)


def main():
    leaves = [r["qid"] for r in csv.DictReader(open(OUT / "taxonomy.csv", encoding="utf-8"))]
    # per-LEAF value-centroid (mean of the bge centroids of the clusters that resolved to it) — so "confusable" means
    # the VALUES look alike (baseball/basketball player), NOT just that two types share an ontology ancestor (company vs
    # political party both descend from 'person or organization' but their values are nothing alike).
    clusters = {c["cluster"]: c for c in json.load(open(OUT / "clusters.json", encoding="utf-8"))}
    cl_qid = json.load(open(OUT / "cluster_qid.json", encoding="utf-8"))
    leaf_vecs = defaultdict(list)
    for cid, q in cl_qid.items():
        c = clusters.get(int(cid))
        if c and c.get("centroid"):
            leaf_vecs[q].append(c["centroid"])
    leaf_cent = {}
    for q, vs in leaf_vecs.items():
        m = np.mean(np.asarray(vs, float), 0); leaf_cent[q] = m / (np.linalg.norm(m) + 1e-9)

    def conf(a, b):
        return float(leaf_cent[a] @ leaf_cent[b]) if a in leaf_cent and b in leaf_cent else 0.0

    wd = WD()
    chains = {q: wd.chain(q) for q in leaves}                              # [leaf .. root] qids (raw, with glue)
    json.dump(wd.c, open(P279_CACHE, "w", encoding="utf-8"))
    lps = {q: lpath(wd, q) for q in leaves}                                # GLUE-trimmed [(qid,label)] root->leaf

    def near(q):                                                          # K nearest ancestors above the leaf, nearest-1st
        return [a for a, _ in lps[q][:-1]][::-1][:K]
    anc_leaves = defaultdict(set)
    for q in leaves:
        for a in near(q):
            anc_leaves[a].add(q)

    decision = {}                                                         # leaf -> (status, reason, target)
    for q in leaves:
        if q in FORCE_NOT_METADATA:                                       # country-specific division whose adjective label dodges COUNTRY_BOUND (U.S. state)
            decision[q] = ("rejected", "not_metadata", ADMIN); continue
        if q in WORLD:
            decision[q] = ("accepted", "", None); continue
        if COUNTRY_BOUND.search(lps[q][-1][1]):                          # leaf names a specific country (state of Mexico,
            tgt = next((a for a, lab in reversed(lps[q][:-1])             # province of Canada) -> first generic ancestor
                        if not COUNTRY_BOUND.search(lab)), None)
            if tgt:
                decision[q] = ("rejected", "not_metadata", tgt); continue
        if len(chains[q]) <= 1:                                           # orphan (no P279 parents) = an INSTANCE -> drop
            decision[q] = ("rejected", "over_specific", None); continue
        if len(lps[q]) > DEPTH_CAP:                                       # too detailed (depth>9) -> the category_9 node
            decision[q] = ("rejected", "too_detailed", lps[q][DEPTH_CAP - 1][0]); continue
        # confusable: nearest GLUE-trimmed ancestor (<=K) with a VALUE-SIMILAR sibling (cosine >= T_CONF) — not structural
        tgt = next((a for a in near(q)
                    if a not in WORLD and any(q2 != q and conf(q, q2) >= T_CONF for q2 in anc_leaves[a])), None)
        decision[q] = ("rejected", "confusable", tgt) if tgt else ("accepted", "", None)

    # PROTECT rollup destinations: a node that ABSORBS others (a not_metadata/confusable target) is a real metadata type.
    # Never leave it rejected — that strands its absorbed columns on an inactive node (county of the United States ->
    # administrative territorial entity, but admin-territorial-entity was itself confusable-rejected -> chain). Flip any
    # rejected leaf that is some node's target back to accepted, breaking the chain at the nearest meaningful ancestor.
    for t in {decision[q][2] for q in leaves if decision[q][0] == "rejected" and decision[q][2]}:
        if decision.get(t, ("", ))[0] == "rejected":
            decision[t] = ("accepted", "", None)

    # DEDUP same-label active leaves (two 'borough' QIDs Q5195043 + Q19905455 -> one): build_review keys columns by leaf
    # LABEL, so a second QID with the same label becomes an active target with NO training data. Canonical = prefer an
    # accepted ORIGINAL leaf, then most mapped columns; the rest are merged into it (columns repointed).
    col_count = Counter(d["qid"] for d in json.load(open(OUT / "mapped_columns.json", encoding="utf-8")))
    prospective = {q for q in leaves if decision[q][0] == "accepted"} | \
                  {decision[q][2] for q in leaves if decision[q][0] == "rejected" and decision[q][2]}
    bylabel = defaultdict(list)
    for q in prospective:
        labs = labels_of(wd, q)
        if labs:
            bylabel[labs[-1]].append(q)
    canon = {}
    for lab, qs in bylabel.items():
        if len(qs) > 1:
            c = max(qs, key=lambda q: (q in leaves and decision[q][0] == "accepted", col_count.get(q, 0)))
            canon.update({q: c for q in qs if q != c})
    for q in leaves:                                                       # accepted dup -> merged; retarget dup targets
        st, rs, tgt = decision[q]
        if st == "accepted" and q in canon:
            decision[q] = ("rejected", "duplicate_label", canon[q])
        elif st == "rejected" and tgt in canon:
            decision[q] = (st, rs, canon[tgt])

    absorbed = defaultdict(list)                                          # target -> [rejected qids]
    for q in leaves:
        st, _, tgt = decision[q]
        if st == "rejected" and tgt:
            absorbed[tgt].append(q)

    # remap mapped_columns: rejected qid -> target; duplicate-label qid -> canonical; DROP if rejected with no home
    newmap = []
    for d in json.load(open(OUT / "mapped_columns.json", encoding="utf-8")):
        q = d["qid"]; st, _, tgt = decision.get(q, ("accepted", "", None))
        if st == "rejected":
            if not tgt:
                continue                                                   # instance / no general home -> drop column
            q = tgt
        q = canon.get(q, q)                                               # fold a duplicate-label target into canonical
        newmap.append({**d, "qid": q} if q != d["qid"] else d)
    json.dump(newmap, open(OUT / "mapped_columns.json", "w", encoding="utf-8"), indent=0)

    # write taxonomy.csv: every original leaf (accepted/rejected) + any NEW added target (skip merged dup-label targets)
    maxd = max(len(labels_of(wd, q)) for q in set(leaves) | set(absorbed))
    rows = []
    leafset = set(leaves)
    for q in leaves:
        st, reason, _ = decision[q]
        af = ";".join(absorbed.get(q, [])) if (st == "accepted" and q in absorbed) else ""
        rows.append((q, labels_of(wd, q), st, reason, af))
    for tgt, qs in absorbed.items():
        if tgt not in leafset and tgt not in canon:                       # a brand-new general node (not a merged dup)
            rows.append((tgt, labels_of(wd, tgt), "added", "", ";".join(qs)))

    with open(OUT / "taxonomy.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["qid"] + [f"category_{i+1}" for i in range(maxd)] + ["status", "rejected_for", "added_for", "world_tables"])
        for qid, labs, st, rj, af in sorted(rows, key=lambda r: (r[2] != "accepted", r[0])):
            tables = ";".join(NODE_TABLE[p] for p in wd.chain(qid) if p in NODE_TABLE)
            w.writerow([qid] + labs + [""] * (maxd - len(labs)) + [st, rj, af, tables])

    nacc = sum(1 for q in leaves if decision[q][0] == "accepted")
    nrej = len(leaves) - nacc; nadd = sum(1 for t in absorbed if t not in leafset)
    print(f"rollup: {nacc} accepted, {nrej} rejected, {nadd} added (new general nodes)")
    print(f"  not_metadata: {sum(1 for q in leaves if decision[q][1]=='not_metadata')}  "
          f"confusable: {sum(1 for q in leaves if decision[q][1]=='confusable')}")
    for tgt, qs in sorted(absorbed.items(), key=lambda kv: -len(kv[1]))[:12]:
        print(f"  {wd.lbl.get(tgt, tgt):28s} ({tgt}) <- {len(qs)}: " + ", ".join(wd.lbl.get(q, q) for q in qs[:5]))


if __name__ == "__main__":
    main()
