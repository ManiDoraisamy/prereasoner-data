"""
gen20 — reconcile the column CLUSTERS with taxonomy.csv, grounded by the LLM RENAME (cluster_renames.json):
  - each ENTITY cluster has a clean, Wikidata-searchable type name (renamed/wikidata_query) instead of the noisy mode
    header. SEARCH Wikidata for that NAME -> the CLASS QID (far more reliable than wbsearchentities on short/ambiguous
    cell VALUES like "North"/"the"/"freshman", which mis-resolved ~30%).
  - taxonomy.csv = the distinct cluster class-QIDs (P279 paths); KEEP rows a cluster maps to, ADD the new ones, REMOVE
    the rest. Rewrite mapped_columns.json from cluster members so build_review regenerates assignment/inference (anti-
    drift). Non-entity clusters (codes/ids/times/text) map to nothing. Also fills the `renamed` column in columns.csv.

  $env:PYTHONUTF8=1; python -m training.taxonomy.reconcile_taxonomy
"""
from __future__ import annotations
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.taxonomy.organize_taxonomy import WD, CACHE as P279_CACHE      # noqa: E402

OUT = ROOT / "training/data"
DROP = {"Q35120", "Q136433660", "Q23958946", "Q99526405"}
NODE_TABLE = {"Q515": "Cities", "Q123964505": "Places", "Q6256": "Countries",
              "Q35657": "States", "Q5107": "Continents", "Q11344": "Elements"}
UA = {"User-Agent": "PrereasonerResearch/1.0 (mani.doraisamy@gmail.com)"}
SEARCH_CACHE = OUT / "wd_class_search.json"
VCACHE = OUT / "value_p31_cache.json"                                       # value->P31 fallback cache
COHERENCE_THRESH = 0.80                                                     # skip incoherent clusters (cluster_coherence.py)


def _get(url):
    for a in range(4):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25))
        except Exception:                                                  # noqa: BLE001
            if a == 3:
                return {}
            time.sleep(1.5 * (a + 1))
    return {}


def _claims(ids):
    """{qid: {'is_class': bool (has P279), 'p31': [qids]}} for the search hits — to tell a CLASS from an INSTANCE."""
    out = {}
    for i in range(0, len(ids), 50):
        d = _get("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "props": "claims", "format": "json"}))
        for q, e in (d.get("entities", {}) or {}).items():
            cl = e.get("claims", {}) or {}
            p31 = [c["mainsnak"]["datavalue"]["value"]["id"] for c in cl.get("P31", [])
                   if c.get("mainsnak", {}).get("datavalue")]
            out[q] = {"is_class": bool(cl.get("P279")), "p31": p31}        # P279 (subclass-of) => it IS a class
    return out


def search_class(name, cache):
    """Resolve a type NAME to its CLASS QID, NOT a same-named INSTANCE. Among the top hits prefer one that HAS P279
    (is a subclass of something => a class), exact-label first; if none is a class, fall back to the top hit's P31
    (the class it instantiates) — so 'county' -> the county CLASS, not 'County Galway' the instance."""
    if name in cache:
        return cache[name]
    r = _get("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "wbsearchentities", "search": name, "language": "en", "format": "json", "limit": 10, "type": "item"}))
    hits = [{"id": h["id"], "label": h.get("label", "")} for h in (r.get("search") or [])]
    if not hits:
        cache[name] = None; return None
    info = _claims([h["id"] for h in hits])
    ql = name.lower().strip()
    cls = [h for h in hits if info.get(h["id"], {}).get("is_class")]       # the CLASS hits
    if cls:
        cache[name] = next((h["id"] for h in cls if h["label"].lower() == ql), cls[0]["id"])
    else:
        cache[name] = None                                                # no class among the hits -> DROP (not a type;
    return cache[name]                                                     # do NOT invent a broad P31 like 'source entity')


def value_p31(values, vcache):
    """GROUNDED fallback when the type NAME finds no class: resolve the cluster's actual cell VALUES to their real
    Wikidata type. Each value's top search hit -> its P31 (instance-of); majority vote (>=2 values agree). Returns the
    type QID or None — NOT invented, it is the P31 Wikidata records for the values ('Autauga County' -> 'county of
    Alabama' Q13410400, which rollup then takes up its real P279 chain to 'administrative territorial entity')."""
    vals = [v for v in values if v][:8]
    key = "||".join(vals)
    if key in vcache:
        return vcache[key]
    ids = []
    for v in vals:
        r = _get("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "wbsearchentities", "search": v, "language": "en", "format": "json", "limit": 1, "type": "item"}))
        h = r.get("search") or []
        if h:
            ids.append(h[0]["id"])
    votes = Counter()
    for i, info in _claims(ids).items():
        for p in info.get("p31", [])[:1]:                                  # the value's PRIMARY recorded type
            votes[p] += 1
    q = votes.most_common(1)[0][0] if votes and votes.most_common(1)[0][1] >= 2 else None
    vcache[key] = q; return q


def old_qids():
    p = OUT / "taxonomy.csv"
    return {r["qid"] for r in csv.DictReader(open(p, encoding="utf-8"))} if p.exists() else set()


def main():
    clusters = {c["cluster"]: c for c in json.load(open(OUT / "clusters.json", encoding="utf-8"))}
    renames = json.load(open(OUT / "cluster_renames.json", encoding="utf-8"))
    cache = json.load(open(SEARCH_CACHE, encoding="utf-8")) if SEARCH_CACHE.exists() else {}
    vcache = json.load(open(VCACHE, encoding="utf-8")) if VCACHE.exists() else {}

    cl_qid, fallback, n_incoh = {}, {}, 0                                  # cluster id -> class QID (entity clusters)
    for cid, cl in clusters.items():
        r = renames.get(str(cid))
        if not r or not r.get("is_entity"):
            continue
        if cl.get("coherence", 1.0) < COHERENCE_THRESH:                    # incoherent grab-bag (ship_type = ships +
            n_incoh += 1; continue                                        # disciplines + occupations) -> no leaf, skip
        q = None
        for nm in [s for s in [(r.get("renamed") or "").strip(), (r.get("wikidata_query") or "").strip()] if s]:
            q = search_class(nm, cache)                                    # RENAMED is the exact WD label ('county of the
            if q:                                                          # United States' -> Q47168); wikidata_query
                break                                                      # sometimes drops 'of the' and misses the class
        json.dump(cache, open(SEARCH_CACHE, "w", encoding="utf-8"))
        if not q:                                                          # neither name found a class -> grounded value->P31
            q = value_p31(cl.get("values", []), vcache)
            json.dump(vcache, open(VCACHE, "w", encoding="utf-8"))
            if q:
                fallback[cid] = q
        if q:
            cl_qid[cid] = q
    json.dump({str(k): v for k, v in fallback.items()}, open(OUT / "value_fallback.json", "w", encoding="utf-8"), indent=0)
    print(f"{len(cl_qid)} entity clusters resolved ({len(cl_qid)-len(fallback)} by name, {len(fallback)} by value->P31); "
          f"{n_incoh} skipped as incoherent (coherence < {COHERENCE_THRESH})", flush=True)
    # save cluster -> qid so rollup can compute a per-LEAF value-centroid (confusability = value-similar, not structural)
    json.dump({str(k): v for k, v in cl_qid.items()}, open(OUT / "cluster_qid.json", "w", encoding="utf-8"), indent=0)

    wd = WD()
    new_tax = set(cl_qid.values()); old = old_qids()
    print(f"\n== reconcile: {len(new_tax & old)} KEPT, {len(new_tax - old)} ADDED, {len(old - new_tax)} REMOVED ==")
    for cid in sorted(cl_qid, key=lambda c: -clusters[c]["n"]):
        q = cl_qid[cid]; wd.chain(q)
        print(f"  {clusters[cid]['n']:4d}  {renames[str(cid)]['renamed']:24s} -> {wd.lbl.get(q, q):24s} ({q:>9s})"
              f"  [{'kept' if q in old else 'ADDED'}]")
    json.dump(wd.c, open(P279_CACHE, "w", encoding="utf-8"))

    # rewrite mapped_columns from cluster members
    newmap = []
    for cid, q in cl_qid.items():
        for m in clusters[cid].get("members", []):
            newmap.append({"header": m["header"], "values": m["values"], "qid": q})
    json.dump(newmap, open(OUT / "mapped_columns.json", "w", encoding="utf-8"), indent=0)

    # build taxonomy.csv from the cluster classes
    rows, maxd = [], 0
    for qid in sorted(new_tax):
        path = [p for p in reversed(wd.chain(qid)) if p not in DROP]
        labels, seen = [], set()
        for p in path:
            lab = wd.lbl.get(p, p)
            if re.fullmatch(r"Q\d+", str(lab)):                            # unlabeled node (no en label) — drop it
                continue
            if lab not in seen:
                seen.add(lab); labels.append(lab)
        if labels:
            tables = [NODE_TABLE[p] for p in path if p in NODE_TABLE]
            rows.append({"qid": qid, "labels": labels, "tables": tables, "status": "HAVE" if tables else "ADD"})
            maxd = max(maxd, len(labels))
    json.dump(wd.c, open(P279_CACHE, "w", encoding="utf-8"))
    with open(OUT / "taxonomy.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["qid"] + [f"category_{i+1}" for i in range(maxd)] + ["status", "world_tables"])
        for r in rows:
            w.writerow([r["qid"]] + r["labels"] + [""] * (maxd - len(r["labels"])) + [r["status"], ";".join(r["tables"])])

    # add the `renamed` column to columns.csv
    crows = list(csv.DictReader(open(OUT / "columns.csv", encoding="utf-8")))
    with open(OUT / "columns.csv", "w", newline="", encoding="utf-8") as f:
        fn = ["cluster", "name", "renamed", "n_columns", "headers", "sample_values"]
        w = csv.DictWriter(f, fieldnames=fn); w.writeheader()
        for cr in crows:
            nm = str(cr.get("name", "")).strip()
            if not nm or any(ord(c) < 32 or ord(c) == 0xFFFD for c in nm):  # blank / mojibake cluster -> drop from columns.csv
                continue
            cr["renamed"] = (renames.get(cr["cluster"], {}) or {}).get("renamed", "")
            w.writerow({k: cr.get(k, "") for k in fn})
    print(f"\nwrote taxonomy.csv ({len(rows)} types) + mapped_columns.json ({len(newmap)} cols) + columns.csv (renamed)")


if __name__ == "__main__":
    main()
