"""Build knowledgebase."types" — the type-taxonomy DAG (qid PK) — from db/sync/data/taxonomy.csv,
and register every type label in knowledgebase."words" (type='type') so a TYPE mention or a
column type resolves by exact norm or bge nearest-neighbour.

  knowledgebase."types"  (qid PK, label, parent_qid, is_leaf, world_table, depth, resolver_type)
  knowledgebase."words"  type='type' rows (one embedded label per node, qid-linked)

Node lineage (root -> leaf, with per-node qids) is derived from the real Wikidata P279
chain (walked via the Wikidata API, cached in db/sync/data/p279_cache.json so re-runs —
and typically the first run — need no network). resolver_type links the legacy
words.type strings to their node ('city' -> Q515), which the engine's qid taxonomy walk
uses (see unify_words_qid.py for the original in-place migration + verification).

Read by the engine for lazy table naming, world-QID resolution, and taxonomy routing.

Run (after build_words.py):
  export KB_PG_HOST=... KB_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/sync_types.py
"""
from __future__ import annotations
import csv
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from _conn import connect
    from _embed import Embedder, normalize_surface
except ImportError:
    from ._conn import connect
    from ._embed import Embedder, normalize_surface

DATA = Path(__file__).resolve().parent / "data"
TAXONOMY = DATA / "taxonomy.csv"
CACHE = DATA / "p279_cache.json"
UA = "prereasoner-db-sync/1.0 (https://github.com/ManiDoraisamy/prereasoner-data)"

# world-table mapping for the geo/element spine nodes (rollup_taxonomy.NODE_TABLE)
NODE_TABLE = {"Q515": "Cities", "Q123964505": "Places", "Q6256": "Countries",
              "Q5107": "Continents", "Q11344": "Elements"}
# legacy resolver type-string -> the taxonomy node qid it denotes ('city' ALWAYS means Q515)
RESOLVER_QID = {"city": "Q515", "country": "Q6256", "state": "Q35657"}

# taxonomy path cleanup (rollup_taxonomy): drop upper-ontology roots + GLUE nodes shared
# by almost everything, and country-SPECIFIC intermediates; the LEAF is always kept.
DROP = {"Q35120", "Q136433660", "Q23958946", "Q99526405"}
GLUE = {"continuant", "independent continuant", "specifically dependent continuant", "anatomical entity",
        "physical anatomical entity", "being", "material entity", "object", "artificial object", "concrete object",
        "abstract entity", "collective entity", "occurrent", "physical object"}
COUNTRY_BOUND = re.compile(r"\bof (the )?[A-Z]|\bof a single country\b")


# --- minimal Wikidata P279 walker (organize_taxonomy.WD), cached on disk ------------------------------------
def _api(params, tries=4):
    url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(params)
    for a in range(tries):
        try:
            return json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25))
        except Exception as e:                                  # noqa: BLE001
            if a == tries - 1:
                print("api fail", e); return {}
            time.sleep(1.5 * (a + 1))
    return {}


class WD:
    def __init__(self):
        self.c = json.load(open(CACHE, encoding="utf-8")) if CACHE.exists() else {}
        self.lbl = {}

    def parents_label(self, qid):
        """-> ([parent qids], english label). Cached."""
        if qid in self.c:
            return self.c[qid][0], self.c[qid][1]
        e = _api({"action": "wbgetentities", "ids": qid, "props": "claims|labels", "format": "json"})
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

    def save(self):
        json.dump(self.c, open(CACHE, "w", encoding="utf-8"))


def lpath(wd, qid):
    """[(qid, label)] root->leaf — drop DROP roots, unlabeled nodes, GLUE, and country-specific
    INTERMEDIATE nodes; the leaf itself is always kept (rollup_taxonomy.lpath)."""
    raw, seen = [], set()
    for p in [x for x in reversed(wd.chain(qid)) if x not in DROP]:
        lab = wd.lbl.get(p, p)
        if re.fullmatch(r"Q\d+", str(lab)) or lab.lower() in GLUE or lab in seen:   # unlabeled / glue / dup
            continue
        seen.add(lab); raw.append((p, lab))
    return [(p, lab) for i, (p, lab) in enumerate(raw)
            if i == len(raw) - 1 or not COUNTRY_BOUND.search(lab)]         # drop country-bound intermediates


def main():
    rows = [r for r in csv.DictReader(open(TAXONOMY, encoding="utf-8"))
            if r["status"] in ("accepted", "added")]
    leaves = {r["qid"] for r in rows}
    wd = WD()
    nodes = {}                                                             # qid -> {label, parent}
    for r in rows:
        path = lpath(wd, r["qid"])                                         # [(qid,label)] root -> leaf (clean)
        for i, (q, lab) in enumerate(path):
            parent = path[i - 1][0] if i > 0 else None                     # parent = one step toward root
            nodes.setdefault(q, {"label": lab, "parent": parent})
    wd.save()

    def depth(q, seen=()):
        p = nodes.get(q, {}).get("parent")
        return 0 if (not p or p not in nodes or q in seen) else 1 + depth(p, seen + (q,))

    cn = connect(); cur = cn.cursor()
    cur.execute('TRUNCATE knowledgebase."types"')                                  # table created by init.sql
    for q, n in nodes.items():
        cur.execute('INSERT INTO knowledgebase."types" (qid,label,parent_qid,is_leaf,world_table,depth) '
                    'VALUES (%s,%s,%s,%s,%s,%s)',
                    (q, n["label"], n["parent"], q in leaves, NODE_TABLE.get(q), depth(q)))
    # link the legacy resolver type strings onto their nodes ('city' -> Q515 ...)
    for s, q in RESOLVER_QID.items():
        cur.execute('UPDATE knowledgebase."types" SET "resolver_type"=%s WHERE qid=%s', (s, q))
    cn.commit()

    # sync type labels into world.words (type='type') so a type mention / column type resolves
    emb = Embedder.get()
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
