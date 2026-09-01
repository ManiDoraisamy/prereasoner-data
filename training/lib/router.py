"""
gen20 — route an uploaded CSV string column to its taxonomy leaf QID + world table, using the TRAINED gen20
unified model (qwen_lora fine-tuned encoder + the RelBlock readout) — NOT the frozen-Query17 ridge probe.

For a column: build a units graph (header + value units, same_col edges) -> encode with the gen20 LoRA Qwen ->
RelBlock content readout (the 124 anchored dims, per unit) -> mean over the value units -> the column's dim profile.
Then route by the spec's scheme: each candidate leaf scored as sum over its root->leaf path of (firing * DECAY**depth-
from-leaf) so the LEAF is full weight and ancestors fade toward root. The TRAINED model fires the leaf + near-leaf
nodes (city: urban_settlement/populated_place/city; country; u_s_state), so this discriminates city/country/state.

  $env:PYTHONUTF8=1; python -m training.lib.router   # self-test
"""
from __future__ import annotations
import os
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.corpus.build_review import (                               # noqa: E402  (DB-free constants)
    LEAF_PATH, LEAF_QID, LEAF_TABLES, name_like)

R19 = ROOT / "training/data"
DECAY = 0.6                                                                 # parent contribution decays toward root
MAXVALS = 40                                                               # cap encoded cells/column


def _supported_leaves(di):
    """leaves the model was trained to fire (>=3 train rows firing the leaf dim) — restrict candidates."""
    import csv
    leaf_n = {}
    try:
        for r in csv.DictReader(open(R19 / "assignment.csv", encoding="utf-8")):
            for lf in LEAF_PATH:
                if lf in di and r.get(lf) == "1":
                    leaf_n[lf] = leaf_n.get(lf, 0) + 1
    except FileNotFoundError:
        pass
    return [lf for lf, n in leaf_n.items() if n >= 3] or [lf for lf in LEAF_PATH if any(n in di for n in LEAF_PATH[lf])]


class Router:
    """Loads the TRAINED gen20 model once (lazy); route(values, header) -> {leaf, qid, world_tables, score,
    decayed, confidence} or None. Importing this module needs no torch — the model loads on first route()."""

    def __init__(self, shared=None):
        # shared = (qwen, tok, model) from an already-loaded gen20 world encoder (Query17World). When given, the
        # router REUSES it (one Qwen in memory) instead of loading its own — so /world runs a SINGLE gen20 model.
        self._shared = shared
        alloc = json.load(open(R19 / "alloc.json"))
        self.nc = alloc["n_content"]
        self.di = {d["name"]: i for i, d in enumerate(alloc["dims"])}
        sup = _supported_leaves(self.di)
        inter = {n for lf in LEAF_PATH for n in LEAF_PATH[lf][:-1]}         # intermediate (ancestor/spine) nodes
        self.leaves = [lf for lf in sup if lf not in inter]                # true leaves only (not the shared geo spine)
        wt = [lf for lf in sup if LEAF_TABLES.get(lf)]                     # leaves that map to a world TABLE
        wt_inter = {n for lf in wt for n in LEAF_PATH[lf][:-1]}
        # Route to the MOST SPECIFIC table-bearing leaf only -> {city, country, u_s_state}. Ancestor table-bearing types
        # (e.g. urban_settlement -> Places) are intentionally dropped: they sit ON city's path
        # (...->populated_place->urban_settlement->city), so a settlement column routes to `city`, which ALREADY maps to
        # BOTH ['Cities','Places'] (LEAF_TABLES['city']). So Places stays reachable via city; nothing is lost.
        self.world_leaves = [lf for lf in wt if lf not in wt_inter]
        tj = R19 / "route_thresholds.json"                                 # per-leaf firing gate (calibrated on THIS model,
        self.thr = json.load(open(tj)) if tj.exists() else {}              # recall-favoring; calibrate_route.py). No file => no gate.
        self._m = None                                                     # (enc, model, fam_dims) — lazy

    def _load(self):
        if self._m is not None:
            return self._m
        import torch
        from training.lib.encoder import LiveQwen                            # import-light encoder (no training deps)
        from training.lib.relblock import RelBlockModel
        from training.lib.edges import fam_dims_map
        dev = torch.device(os.environ.get("DEVICE", "cuda" if torch.cuda.is_available() else "cpu"))
        if self._shared is not None:                                        # REUSE the world encoder's gen20 Qwen+RelBlock
            qwen, tok, model = self._shared
            enc = LiveQwen(dev, shared_qwen=qwen, shared_tok=tok)
        else:                                                               # standalone (calibrate/validate/local): load own
            enc = LiveQwen(dev, warm_lora=str(R19 / "qwen_lora"), serving=True)  # eval/dropout-off (deterministic)
            cfg = torch.load(R19 / "encoder_meta.pt", map_location="cpu", weights_only=True)["cfg"]
            model = RelBlockModel(in_dim=cfg["in_dim"], H=cfg["H"], layers=cfg["layers"], heads=cfg["heads"],
                                   nc=cfg["nc"], n_edge=cfg["n_edge"]).to(dev)
            model.load_state_dict(torch.load(R19 / "encoder.pt", map_location="cpu", weights_only=True))
            model.eval()
        self._m = (enc, model, fam_dims_map(json.load(open(R19 / "alloc.json"))), dev, torch)
        return self._m

    def _profile(self, values, header):
        """the column's per-dim readout = mean of the RelBlock content readout over the value units."""
        from training.lib.walker import build_from_units
        enc, model, fam_dims, dev, torch = self._load()
        vals = [str(v) for v in values if name_like(str(v))][:MAXVALS]
        if not vals:
            return None
        units = [{"text": str(header) if header else "value", "kind": "colname", "role": "header",
                  "col": 0, "row": -1, "fired": [], "sup": []}]
        for i, v in enumerate(vals):
            units.append({"text": v, "kind": "cell", "role": "value", "col": 0, "row": i, "fired": [], "sup": []})
        u = build_from_units({"file": "route", "units": units}, self.di, fam_dims, self.nc)
        V = enc.encode(u["texts"], grad=False).detach().cpu().numpy().astype(np.float32)
        x = torch.tensor(V)[None].to(dev); E = torch.tensor(u["E"])[None].to(dev)
        kp = torch.zeros(1, len(u["texts"]), dtype=torch.bool).to(dev)
        with torch.no_grad():
            content = model(x, E, kp)["content"][0].cpu().numpy()
        return content[1:].mean(0)                                         # mean over value units (skip header at idx 0)

    def _leaf_score(self, s, lf, shared=frozenset()):
        # leaf-weighted (DECAY**depth-from-leaf), but ONLY over nodes UNIQUE to this leaf among the candidates — the
        # shared geo spine (geolocatable_entity/geographical_feature/region) fires ~equally for city/country/state and
        # only biases toward shorter paths, so it must be dropped; the discriminating signal is the leaf + near-leaf.
        return sum(max(0.0, float(s[self.di[n]])) * (DECAY ** d)
                   for d, n in enumerate(reversed(LEAF_PATH[lf])) if n in self.di and n not in shared)

    def candidates(self, values, header=None, leaves=None, topk=6, min_fire=0.0):
        """The MODEL's ranked candidate leaves for a column, by DIRECT leaf-dim firing (NOT the path-decay, whose
        shared broad ancestors — 'organization' — boost the wrong specific leaf: a hospital column path-routes to
        'sports_team', but the hospital DIM (0.197) out-fires sports_team/street). Used for the non-geo world join:
        the model proposes these candidates, Wikidata grounding then disambiguates which the cells actually are.
        leaves defaults to all true leaves; pass the world-table leaves to restrict. -> [leaf] high->low, positive only."""
        s = self._profile(values, header)
        if s is None:
            return []
        cand = leaves if leaves is not None else self.leaves
        scored = sorted(((lf, float(s[self.di[lf]])) for lf in cand if lf in self.di), key=lambda x: -x[1])
        return [lf for lf, sc in scored[:topk] if sc > min_fire]

    def route(self, values, header=None, world_only=False, min_fire=0.0):
        from collections import Counter
        s = self._profile(values, header)
        cands = self.world_leaves if world_only else self.leaves           # world join: restrict to table-bearing leaves
        if s is None or not cands:
            return None
        nodecount = Counter(n for lf in cands for n in LEAF_PATH[lf])
        shared = {n for n, c in nodecount.items() if c > 1}                # nodes on >1 candidate path = non-discriminating
        # GATE: a world leaf only counts if its raw dim clears the model-calibrated threshold. For world routing this
        # is decisive — a column that fires NO world leaf above its gate is NOT a world column -> None (the embedding
        # cell-resolver, spec item 3, then supplies precision among the leaks). General typing (no thresholds) is ungated.
        gated = [lf for lf in cands if float(s[self.di[lf]]) >= self.thr.get(lf, -1e9)]
        if world_only and not gated:
            return None
        # SELECT by the DIRECT leaf dim — the model's own per-leaf signal. _leaf_score (path-decay) sums broad shared
        # ancestors (organization/service_provider/...) which are NOT shared among these candidates yet still dominate
        # the specific leaf, so a hospital column path-routed to a non-leaf intermediate ('sports_team'). The hospital
        # DIM itself out-fires every other TRUE leaf (0.197 > street 0.139 > university 0.121) — that is the decision.
        leaf = max(gated or cands, key=lambda lf: float(s[self.di[lf]]) if lf in self.di else -9.0)
        if not world_only and float(s[self.di[leaf]]) < min_fire:          # general typing floor (no calibrated gate)
            return None
        path = LEAF_PATH[leaf]
        decayed = {n: round(float(s[self.di[n]]) * (DECAY ** d), 3)
                   for d, n in enumerate(reversed(path)) if n in self.di and n not in shared}
        tables = LEAF_TABLES.get(leaf) or next((LEAF_TABLES[n] for n in reversed(path[:-1]) if LEAF_TABLES.get(n)), [])
        return {"leaf": leaf, "qid": LEAF_QID.get(leaf), "world_tables": tables,
                "score": round(float(s[self.di[leaf]]), 3), "decayed": decayed,
                "confidence": round(float(sum(decayed.values())), 3),
                "gated": gated, "raw": {lf: round(float(s[self.di[lf]]), 3) for lf in cands}}


def main():
    r = Router()
    tests = {
        "city": ["Paris", "Tokyo", "London", "Berlin", "Madrid", "Rome"],
        "country": ["France", "Germany", "Japan", "Brazil", "Canada", "Italy"],
        "state": ["California", "Texas", "Florida", "New York", "Ohio", "Georgia"],
        "hospital": ["Mayo Clinic", "Cleveland Clinic", "Mount Sinai", "Johns Hopkins Hospital"],
        "software": ["Photoshop", "Microsoft Word", "Blender", "Visual Studio Code"],
    }
    tests["name"] = ["John Smith", "Mary Johnson", "Alice Brown", "Bob Davis", "Carol White"]
    tests["amount"] = ["120", "80", "45", "200", "15"]
    print("world_leaves:", r.world_leaves)
    for h in ("city", "country", "state"):                                 # WORLD routing (restricted to table leaves)
        o = r.route(tests[h], header=h, world_only=True)
        if o is None:
            print(f"  [world] {h:8s} -> None (rejected by gate)"); continue
        print(f"  [world] {h:8s} -> leaf={o['leaf']:14s} qid={o['qid']!s:>9s} tables={o['world_tables']} score={o['score']}")
    for h in ("hospital", "software", "name", "amount"):                   # general type routing (min_fire floor)
        o = r.route(tests[h], header=h, min_fire=0.12)
        if o is None:
            print(f"  [type]  {h:8s} -> None (below floor — not a typed column)"); continue
        print(f"  [type]  {h:8s} -> leaf={o['leaf']:14s} qid={o['qid']!s:>9s} score={o['score']}")


if __name__ == "__main__":
    main()
