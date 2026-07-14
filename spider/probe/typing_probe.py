"""Probe C — typing-router behaviour on real Spider columns (model, CPU).

Tests the v1 Phase-4 worry: on Spider's overwhelmingly out-of-taxonomy columns, does engine.router.Router
correctly ABSTAIN, or does it silently MIS-FIRE (type a non-entity column to a leaf) / OVER-REACH (route a
non-geo column to the city/country world tables, which in the live path would trigger a spurious world
join)? Runs over a sample of text columns drawn with REAL cell values from the 20 dev DBs.
"""
from __future__ import annotations
import argparse
import collections
import json
import os
import re
import sqlite3
import warnings

warnings.filterwarnings("ignore")

GEO_HINT = re.compile(r"\b(city|cities|country|countries|nation|state|province|continent|town)\b", re.I)


def sample_columns(dbdir, dbids, per_db=8, max_vals=25):
    cols = []
    for db in dbids:
        p = os.path.join(dbdir, db + ".sqlite")
        if not os.path.exists(p):
            continue
        con = sqlite3.connect(p); con.text_factory = lambda b: b.decode("utf-8", "replace")
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tabs = [r[0] for r in cur.fetchall()]
        picked = 0
        for t in tabs:
            try:
                cur.execute(f'SELECT * FROM "{t}"')
            except sqlite3.Error:
                continue
            names = [d[0] for d in cur.description]
            rows = cur.fetchall()
            for ci, cn in enumerate(names):
                vals = [str(r[ci]) for r in rows if r[ci] not in (None, "")]
                # text-ish: has letters, not mostly numeric
                textish = [v for v in vals if re.search(r"[A-Za-z]", v) and not re.fullmatch(r"[\d.,:/_\- ]+", v)]
                distinct = sorted(set(textish))
                if len(distinct) < 3:
                    continue
                cols.append({"db": db, "table": t, "column": cn,
                             "geo_hint": bool(GEO_HINT.search(cn)),
                             "values": distinct[:max_vals]})
                picked += 1
                if picked >= per_db:
                    break
            if picked >= per_db:
                break
        con.close()
    return cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", required=True)
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    ap.add_argument("--per-db", type=int, default=8)
    args = ap.parse_args()

    dev = json.load(open(os.path.join(args.data, "dev.json"), encoding="utf-8"))
    dbids = sorted({e["db_id"] for e in dev})
    cols = sample_columns(args.dbs, dbids, per_db=args.per_db)

    print(f"loading Router (Qwen LoRA + relational readout, CPU)...", flush=True)
    from engine.router import Router
    r = Router()
    print(f"world_leaves={r.world_leaves}. probing {len(cols)} columns\n", flush=True)

    out = []
    typ = collections.Counter()          # general-typing leaf (or __abstain__)
    world = collections.Counter()        # world routing result (city/country/__none__)
    misfire = 0                          # non-geo column that types to SOME leaf
    overreach = 0                        # non-geo column that ROUTES to a world table
    geo_cols = 0
    geo_world_ok = 0
    for c in cols:
        o_type = r.route(c["values"], header=c["column"], world_only=False, min_fire=0.12)
        o_world = r.route(c["values"], header=c["column"], world_only=True)
        tleaf = o_type["leaf"] if o_type else "__abstain__"
        wleaf = o_world["leaf"] if o_world else "__none__"
        typ[tleaf] += 1
        world[wleaf] += 1
        if c["geo_hint"]:
            geo_cols += 1
            if wleaf != "__none__":
                geo_world_ok += 1
        else:
            if tleaf != "__abstain__":
                misfire += 1
            if wleaf != "__none__":
                overreach += 1
        out.append({**{k: c[k] for k in ("db", "table", "column", "geo_hint")},
                    "typed_leaf": tleaf, "typed_score": (o_type or {}).get("score"),
                    "world_leaf": wleaf, "world_score": (o_world or {}).get("score"),
                    "sample": c["values"][:6]})

    n = len(cols); nongeo = n - geo_cols
    summary = {
        "n_columns": n, "geo_hint_cols": geo_cols, "nongeo_cols": nongeo,
        "general_typing_distribution": dict(typ.most_common()),
        "world_routing_distribution": dict(world.most_common()),
        "nongeo_general_misfire": {"n": misfire, "pct": round(100 * misfire / max(nongeo, 1), 1)},
        "nongeo_world_overreach": {"n": overreach, "pct": round(100 * overreach / max(nongeo, 1), 1)},
        "geo_world_recall": {"n": geo_world_ok, "of": geo_cols,
                             "pct": round(100 * geo_world_ok / max(geo_cols, 1), 1)},
    }
    os.makedirs(args.out, exist_ok=True)
    json.dump({"summary": summary, "columns": out}, open(os.path.join(args.out, "typing_probe.json"), "w"), indent=2)

    P = print
    P("=" * 78); P("PROBE C — TYPING ROUTER on Spider columns"); P("=" * 78)
    P(f"columns probed: {n}  (geo-hinted headers: {geo_cols}, other: {nongeo})")
    P(f"world tables available: {r.world_leaves}")
    P("")
    P(f"general-typing (min_fire=0.12) leaf distribution: {dict(typ.most_common())}")
    P(f"world-routing distribution: {dict(world.most_common())}")
    P("")
    P(f"NON-geo columns that MIS-FIRE to some leaf : {misfire}/{nongeo}  ({summary['nongeo_general_misfire']['pct']}%)")
    P(f"NON-geo columns that OVER-REACH to a world table (spurious world-join risk): "
      f"{overreach}/{nongeo}  ({summary['nongeo_world_overreach']['pct']}%)")
    P(f"geo-hinted columns correctly routed to a world table (recall): "
      f"{geo_world_ok}/{geo_cols}  ({summary['geo_world_recall']['pct']}%)")
    P("\nwrote results/typing_probe.json")


if __name__ == "__main__":
    main()
