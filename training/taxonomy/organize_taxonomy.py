"""
gen20 — organize the discovered + existing world types into the Wikidata P279 (subclass-of) TAXONOMY, so the
"which types need adding" decision is read off the tree, not guessed. For each seed type it walks the subclass chain
up to the root (one primary parent per step) and prints the path; types that share a high-level branch (geographic
location / organism / person / written work / abstract name) group together. HAVE = already a world table; ADD = a
new entity type with joinable facts; SKIP = a name-component or an already-QID column or a thin-sample misfire.

  $env:PYTHONUTF8=1; python -m training.taxonomy.organize_taxonomy
"""
from __future__ import annotations
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = "PrereasonerWorldExpand/1.0 (research; mani.doraisamy@gmail.com)"
CACHE = Path(__file__).resolve().parent.parent.parent / "training/data/p279_cache.json"

# (qid, friendly, marker) — existing world tables + the discovered types
SEEDS = [
    ("Q515", "city", "HAVE · Cities"),
    ("Q6256", "country", "HAVE · Countries"),
    ("Q35657", "U.S. state", "HAVE · States"),
    ("Q5107", "continent", "HAVE · Continents"),
    ("Q17334923", "location", "HAVE · Places"),
    ("Q11344", "chemical element", "HAVE · Elements"),
    ("Q16521", "taxon", "ADD"),
    ("Q5", "human", "ADD"),
    ("Q101352", "family name", "skip · name-part"),
    ("Q12308941", "male given name", "skip · name-part"),
    ("Q13442814", "scholarly article", "skip · column is already QIDs"),
    ("Q5633421", "scientific journal", "skip · column is already QIDs"),
    ("Q21014462", "cell line", "skip · thin-sample misfire"),
]


def _cache():
    return json.load(open(CACHE, encoding="utf-8")) if CACHE.exists() else {}


def api(params, tries=4):
    url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(params)
    for a in range(tries):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25))
        except Exception as e:                                  # noqa: BLE001
            if a == tries - 1:
                print("api fail", e); return {}
            time.sleep(1.5 * (a + 1))
    return {}


class WD:
    def __init__(self):
        self.c = _cache(); self.lbl = {}

    def parents_label(self, qid):
        """-> ([parent qids], english label). Cached."""
        if qid in self.c:
            return self.c[qid][0], self.c[qid][1]
        e = api({"action": "wbgetentities", "ids": qid, "props": "claims|labels", "format": "json"})
        ent = e.get("entities", {}).get(qid, {}) or {}
        lab = ent.get("labels", {}).get("en", {}).get("value") or qid
        ps = [c["mainsnak"]["datavalue"]["value"]["id"] for c in ent.get("claims", {}).get("P279", [])
              if c.get("mainsnak", {}).get("datavalue")]
        self.c[qid] = [ps, lab]; return ps, lab

    def chain(self, qid, maxd=12):
        """walk ONE primary parent per step to the root -> [seed, ..., root]; labels filled."""
        path, cur, seen = [qid], qid, {qid}
        for _ in range(maxd):
            ps, lab = self.parents_label(cur); self.lbl[cur] = lab
            nxt = next((p for p in ps if p not in seen), None)
            if not nxt:
                break
            path.append(nxt); seen.add(nxt); cur = nxt
        for p in path:
            if p not in self.lbl:
                _, lab = self.parents_label(p); self.lbl[p] = lab
        return path


def main():
    wd = WD()
    chains = {}
    for qid, friendly, mark in SEEDS:
        chains[qid] = wd.chain(qid)
        json.dump(wd.c, open(CACHE, "w", encoding="utf-8"))
    # group by the high-level branch = the ancestor just below "entity"/"object" (2nd or 3rd from root)
    ROOTS = {"Q35120": "entity", "Q488383": "object", "Q99527517": "collection entity",
             "Q23958946": "individual object", "Q4406616": "concrete object", "Q28813620": "class"}

    def branch(path):
        anc = [p for p in reversed(path) if p not in ROOTS]       # root-first, skipping the meta roots
        return anc[0] if anc else path[-1]

    groups = {}
    for qid, friendly, mark in SEEDS:
        b = branch(chains[qid]); groups.setdefault(b, []).append((qid, friendly, mark))

    print("== Wikidata P279 taxonomy of the world types (HAVE = a table exists; ADD = new entity type) ==\n")
    for b, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"▸ {wd.lbl.get(b, b).upper()}")
        for qid, friendly, mark in members:
            path = chains[qid]
            trail = " > ".join(wd.lbl.get(p, p) for p in reversed(path))     # root -> seed
            print(f"    [{mark:30s}] {friendly} ({qid})")
            print(f"        {trail}")
        print()
    add = [f for _, f, m in SEEDS if m == "ADD"]
    print(f"NEEDS ADDING (ADD): {', '.join(add)}")
    print("Already covered (HAVE): Cities, Countries, States, Continents, Places, Elements")


if __name__ == "__main__":
    main()
