"""
gen20 — the TRAINED-MODEL route gate. Proves the OBJECTIVE that the SERVED model (router: qwen_lora + RelBlock +
route_thresholds.json), NOT the ridge probe, types world columns correctly. validate_data.py only checks the ridge-probe
artifacts + data cleanliness; this checks the artifacts router actually serves.

Two parts:
  HARD GATE (exit 1 on fail) — the live-DEMO distribution must route correctly: famous city/country/state columns ->
    the right world leaf, and clear non-geo columns (names/products/status) -> None. If these break, the demo breaks.
  REPORTED METRIC — held-out generalization of the SERVED model on inference.csv's held-out tokens (per-type recall +
    non-geo specificity). This is the honest trained-model number (the checked-in inference.csv PASS% is the RIDGE
    PROBE, a different readout — see anchor_assignment.py).

Deterministic: router now serves the encoder in eval mode (dropout off), so these numbers are reproducible.

  $env:PYTHONUTF8=1; python -m training.calibrate.validate_route
"""
from __future__ import annotations
import csv
import json
import sys
from pathlib import Path

from training.lib.router import Router
from training.corpus.build_review import name_like

R19 = Path(__file__).resolve().parent.parent / "data"
# derive the route objective from alloc — the pruned capped-entity leaf set dropped u_s_state, so the gate must too
# (else it tests a leaf the served model cannot fire). calibrate_route.py derives the world leaves the same way.
WORLD_LEAVES = [lf for lf in ("city", "country", "u_s_state")
                if lf in {d["name"] for d in json.load(open(R19 / "alloc.json"))["dims"]}]
COLSIZE = 8
_WTYPE = {"city": "city", "country": "country", "u_s_state": "state"}        # leaf -> world."words" type


def _try_conn():
    """world DB for grounding, or None (then the gate is router-ALONE — looser, since grounding is what rejects a
    name column the city dim loosely fires on). The DEPLOYED world path ALWAYS grounds (world17._grounds, spec item 3),
    so when the DB is reachable we test THAT (the real served path), not router in isolation."""
    import os
    if not os.environ.get("WORLD_PG_PASSWORD"):
        return None
    try:
        import psycopg2
        host = os.environ.get("WORLD_PG_HOST", "localhost")
        kw = dict(host=host, dbname=os.environ.get("WORLD_PG_DB", "world"), user=os.environ.get("WORLD_PG_USER", "postgres"), password=os.environ["WORLD_PG_PASSWORD"], connect_timeout=30)
        if not host.startswith("/"):
            kw["port"] = 5432; kw["sslmode"] = "require"
        return psycopg2.connect(**kw)
    except Exception as e:                                                    # noqa: BLE001
        print(f"   (no DB grounding -> router-alone: {str(e)[:70]})", flush=True)
        return None


def _grounds(cells, leaf, cur):
    """spec item 3, mirrors world17._grounds: >=80% of cells are real `leaf` entities in world."words". A loose
    city-fire on a name column is dropped here (names don't ground)."""
    from training.lib.embedder import normalize_surface
    wtype = _WTYPE.get(leaf)
    if not wtype:
        return True
    norms = sorted({normalize_surface(str(c)) for c in cells if str(c).strip()})
    if len(norms) < 2:
        return False
    cur.execute('SELECT COUNT(DISTINCT norm) FROM world."words" WHERE type=%s AND norm = ANY(%s)', (wtype, norms))
    return cur.fetchone()[0] >= max(2, 0.8 * len(norms))


def _route(r, col, cur):
    """the DEPLOYED decision: router types the column, then (if a DB is present) grounding confirms — exactly the
    world17 path. cur=None -> router-alone."""
    o = r.route(col, header=None, world_only=True)
    if not o:
        return None
    if cur is None:
        return o["leaf"]
    return o["leaf"] if _grounds(col, o["leaf"], cur) else None

# the live-demo distribution — the HARD gate (must route correctly or the demo breaks)
DEMO_POS = {
    "city": [["Paris", "Lyon", "Berlin", "Tokyo", "Madrid", "Rome"], ["Mumbai", "Osaka", "Toronto", "Sydney", "Oslo"]],
    "country": [["France", "Germany", "Japan", "Brazil", "Canada", "Italy"], ["Mexico", "Egypt", "Kenya", "Chile"]],
    "u_s_state": [["California", "Texas", "Florida", "Ohio", "Georgia", "Nevada"], ["Arizona", "Oregon", "Kansas"]],
}
DEMO_NEG = [["Alice", "Bob", "Carol", "Dan", "Eve", "Frank"], ["Laptop", "Mouse", "Keyboard", "Monitor", "Webcam"],
            ["shipped", "pending", "delivered", "cancelled", "returned"]]


def _columns(tokens, colsize=COLSIZE):
    toks = sorted({t for t in tokens if name_like(t)})
    ncol = max(1, len(toks) // colsize)
    cols = [[] for _ in range(ncol)]
    for i, t in enumerate(toks):
        cols[i % ncol].append(t)
    return [c for c in cols if len(c) >= 3]


def main():
    r = Router()
    conn = _try_conn()
    cur = conn.cursor() if conn else None
    mode = "router + GROUNDING (deployed world path)" if cur else "router-ALONE (no DB; looser)"
    print(f"served thresholds: {r.thr}\nworld_leaves: {r.world_leaves}\nmode: {mode}\n")
    fails = []

    # ---- HARD GATE: the demo distribution ----
    print("HARD GATE (live-demo distribution):")
    for lf in WORLD_LEAVES:                                                  # only leaves the pruned model can fire
        for col in DEMO_POS.get(lf, []):
            got = _route(r, col, cur)
            ok = got == lf
            if not ok:
                fails.append(f"demo {lf} column {col[:3]}... -> {got} (expected {lf})")
            print(f"   {'OK ' if ok else 'XX '} {lf:10s} {col[:3]}... -> {got}")
    for col in DEMO_NEG:
        got = _route(r, col, cur)
        ok = got is None
        if not ok:
            fails.append(f"demo NON-GEO column {col[:3]}... -> {got} (expected None)")
        print(f"   {'OK ' if ok else 'XX '} non-geo    {col[:3]}... -> {got}")

    # ---- REPORTED METRIC: held-out generalization of the SERVED model ----
    rows = list(csv.DictReader(open(R19 / "inference.csv", encoding="utf-8")))
    pos = {lf: [x["Token"] for x in rows if x.get(lf) == "1"] for lf in WORLD_LEAVES}
    neg = [x["Token"] for x in rows if not any(x.get(lf) == "1" for lf in WORLD_LEAVES)]
    print("\nHELD-OUT (inference.csv) — the SERVED model's generalization:")
    metric = {"thresholds": r.thr, "per_type_recall": {}, "non_geo_specificity": None}
    for lf in WORLD_LEAVES:
        cols = _columns(pos[lf])
        hit = sum(1 for c in cols if _route(r, c, cur) == lf)                              # SERVED path (grounded if DB up)
        alone = sum(1 for c in cols                                                        # model-only = ROUTING SKILL
                    if (lambda o: bool(o) and o["leaf"] == lf)(r.route(c, header=None, world_only=True)))
        metric["per_type_recall"][lf] = {"hit": hit, "n": len(cols), "recall": round(hit / max(1, len(cols)), 3),
                                         "model_only": alone, "model_only_recall": round(alone / max(1, len(cols)), 3)}
        print(f"   {lf:10s} grounded {hit}/{len(cols)} | model-only {alone}/{len(cols)} (the model's routing "
              f"generalization; the grounded gap = world.words coverage of the obscure held-out tokens, not a model miss)")
    negcols = _columns(neg)[:40]
    nnone = sum(1 for c in negcols if _route(r, c, cur) is None)
    metric["non_geo_specificity"] = {"none": nnone, "n": len(negcols), "specificity": round(nnone / max(1, len(negcols)), 3)}
    print(f"   non-geo    specificity (->None) = {nnone}/{len(negcols)} = {nnone/max(1,len(negcols)):.0%}")
    # persist the SERVED-model held-out metric (distinct from inference.csv's RIDGE-PROBE PASS%) so there is an
    # inspectable trained-model generalization number, not just console output.
    json.dump(metric, open(R19 / "route_eval.json", "w"), indent=2)
    print(f"   -> wrote {R19 / 'route_eval.json'}")

    print("\n" + ("ROUTE GATE PASSED — the served model types the demo distribution correctly" if not fails
                  else "ROUTE GATE FAILED:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
