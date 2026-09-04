"""Re-score a full_eval per-example file on the NON-WORLD (pure text-to-SQL) subset of Spider dev.

Rationale: Prereasoner has two separable capabilities — (1) text-to-SQL over self-contained data, and
(2) world-model resolution+join (only `city`/`country` have world tables). Spider tests only (1). A Spider
example whose gold query references a world-typed column (country/city) is where (2) would intrude on the
live system, so it belongs in the WORLD suite, not the Spider suite. This script partitions dev into
world-touching vs clean and recomputes accuracy on the clean subset (no model re-run — reuses per-example).

Usage: python subset_rescore.py <per_example_1.json> [<per_example_2.json> ...] [--broad]
  --broad also excludes state/province/continent/region/nationality (wikipedia entities without a world
  table — Prereasoner can't resolve them, so by default they STAY in Spider; --broad reports the sensitivity).
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
TABLES = {t["db_id"]: t for t in json.load(open(os.path.join(DATA, "tables.json"), encoding="utf-8"))}

# Core world set = the leaves that actually have a world table (capability 2 fires here).
COUNTRY = {"country", "countries", "nation", "nations", "nationality", "nationalities", "countrycode",
           "countryname", "countryid", "countrycode2", "governmentform"}
CITY = {"city", "cities", "town", "towns", "capital", "cityname", "cityid", "district"}
CORE = COUNTRY | CITY
# Broader geo entities (wikipedia-resolvable, but NO world table in Prereasoner -> plain strings by default).
BROAD_EXTRA = {"state", "states", "province", "provinces", "continent", "continents", "region", "regions"}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def world_tokens(db_id, broad=False):
    """The set of ORIGINAL column/table name tokens in this DB that denote a world (country/city) entity."""
    t = TABLES[db_id]
    lex = {_norm(x) for x in (CORE | BROAD_EXTRA if broad else CORE)}
    toks = set()
    for _tidx, cname in t["column_names_original"]:
        n = _norm(cname)
        if not n:
            continue
        if n in lex or any(n == x or n.endswith(x) for x in lex):
            toks.add(cname.lower())
    for tn in t["table_names_original"]:
        if _norm(tn) in lex:
            toks.add(tn.lower())
    return toks


def is_world_touching(ex, broad=False):
    toks = world_tokens(ex["db_id"], broad)
    if not toks:
        return False
    g = ex["gold"].lower()
    return any(re.search(r"\b" + re.escape(tok) + r"\b", g) for tok in toks)


def score(rows):
    n = len(rows)
    if not n:
        return None
    lenient = sum(1 for r in rows if r.get("lenient"))
    strict = sum(1 for r in rows if r.get("strict"))
    sc_rows = [r for r in rows if r.get("gold_scalar")]
    sc_ok = sum(1 for r in sc_rows if r.get("scalar_exact"))
    return {"n": n, "lenient_pct": round(100 * lenient / n, 1), "strict_pct": round(100 * strict / n, 1),
            "scalar_pct": (round(100 * sc_ok / len(sc_rows), 1) if sc_rows else None), "scalar_n": len(sc_rows)}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    broad = "--broad" in sys.argv
    # partition is identical across files (same dev set); compute counts once from the first file
    pe0 = json.load(open(args[0], encoding="utf-8"))
    world = [e for e in pe0 if is_world_touching(e, broad)]
    clean = [e for e in pe0 if not is_world_touching(e, broad)]
    print(f"partition ({'BROAD geo' if broad else 'core country/city'}): "
          f"{len(clean)} clean text-to-SQL  |  {len(world)} world-touching (-> world suite)  of {len(pe0)}")
    dbs = {}
    for e in world:
        dbs[e["db_id"]] = dbs.get(e["db_id"], 0) + 1
    print("  world-touching by db:", dict(sorted(dbs.items(), key=lambda x: -x[1])))
    print("  sample excluded:")
    for e in world[:6]:
        print(f"    [{e['db_id']}] {e['question']!r}\n        gold: {e['gold']}")
    print()
    for path in args:
        pe = json.load(open(path, encoding="utf-8"))
        tag = os.path.basename(path).replace("full_eval_per_example_", "").replace(".json", "")
        cl = [e for e in pe if not is_world_touching(e, broad)]
        wo = [e for e in pe if is_world_touching(e, broad)]
        print(f"== {tag} ==")
        print(f"   ALL   {score(pe)}")
        print(f"   CLEAN {score(cl)}")
        print(f"   WORLD {score(wo)}")


if __name__ == "__main__":
    main()
