"""Route an uploaded CSV string column to its FAMILY (place/person/org/film/music/publication/product/organism)
via the SUPERPOSITION-DECODE: the ONE trained model reads schema.org PROPERTY dims per column, and the family is
DECODED by column-consensus over that firing — the fraction of the family's DISTINCTIVE properties that fire
(calibrated by per-property Youden-J thresholds). Nothing is anchored as a "type"; the type EMERGES from the
properties (Mani's thesis). A column that fires no family's distinctive props (a literal — amount/id/status) ABSTAINS.

For a column: build a units graph (header + value units, same_col edges) -> encode with the LoRA Qwen -> relational
content readout (the anchored property dims, per unit) -> mean over the value units -> the column's property profile
-> family consensus. The COARSE family gates entity-vs-literal + primes the resolver; the FINE table + qid come from
cell resolution (engine.knowledge_query grounding) — the two-tier design.
"""
from __future__ import annotations
import json

import numpy as np

from engine.config import DATA_DIR, DEVICE
from engine.taxonomy import name_like

MAXVALS = 40                                                               # cap encoded cells/column
ABSTAIN = 0.40                                                             # a family needs >= this fraction of its distinctive props firing


class Router:
    """Loads the TRAINED property model once (lazy); route(values, header) -> {family, frac, geo, is_entity, scores}
    or None (abstain/literal). Importing needs no torch — the model loads on first route()."""

    def __init__(self, shared=None):
        # shared = (qwen, tok, model) from the already-loaded encoder (engine.knowledge_query). When given, the router
        # REUSES it (one Qwen in memory) — so /api/knowledge runs a SINGLE model for operator, bridge, AND typing.
        self._shared = shared
        alloc = json.load(open(DATA_DIR / "alloc.json"))
        self.nc = alloc["n_content"]
        self.di = {d["name"]: i for i, d in enumerate(alloc["dims"])}
        F = json.load(open(DATA_DIR / "families.json"))
        self.fams = F["families"]                                          # family -> {distinctive:[props], geo, tables}
        tp = DATA_DIR / "props_thr.json"
        self.thr = json.load(open(tp)) if tp.exists() else {}             # per-property Youden-J firing threshold
        self._m = None                                                     # (enc, model, fam_dims, dev, torch) — lazy

    def _load(self):
        if self._m is not None:
            return self._m
        import torch
        from engine.artifact_provenance import validate_weight_bundle
        from engine.encoder import LiveQwen                                # import-light encoder (no training deps)
        from engine.encoder_model import RelationalModel
        from engine.fk_edges import fam_dims_map
        dev = torch.device(DEVICE if DEVICE != "cuda" or torch.cuda.is_available() else "cpu")
        if self._shared is not None:                                        # REUSE the encoder's Qwen + readout
            qwen, tok, model = self._shared
            enc = LiveQwen(dev, shared_qwen=qwen, shared_tok=tok)
        else:                                                               # standalone (calibrate/validate/local)
            validate_weight_bundle(DATA_DIR)
            enc = LiveQwen(dev, warm_lora=str(DATA_DIR / "qwen_lora"), serving=True)
            cfg = torch.load(DATA_DIR / "encoder_meta.pt", map_location="cpu", weights_only=True)["cfg"]
            model = RelationalModel(in_dim=cfg["in_dim"], H=cfg["H"], layers=cfg["layers"], heads=cfg["heads"],
                                    nc=cfg["nc"], n_edge=cfg["n_edge"]).to(dev)
            model.load_state_dict(torch.load(
                DATA_DIR / "encoder.pt", map_location="cpu", weights_only=True
            ))
            model.eval()
        self._m = (enc, model, fam_dims_map(json.load(open(DATA_DIR / "alloc.json"))), dev, torch)
        return self._m

    def _profile(self, values, header):
        """the column's per-dim property readout = mean of the relational content readout over the value units."""
        from engine.graph_walk import build_from_units
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

    def _consensus(self, s):
        """family -> fraction of its DISTINCTIVE props that FIRE (calibrated by Youden-J thresholds)."""
        scores = {}
        for F, fd in self.fams.items():
            dp = [p for p in fd["distinctive"] if p in self.di]
            if dp:
                scores[F] = sum(1 for p in dp if s[self.di[p]] >= self.thr.get(p, 0.5)) / len(dp)
        return scores

    def _evidence(self, s, family):
        """The per-property firing that DROVE the decode — the model's auditable 'why'. For each of the
        family's distinctive schema.org properties: the read strength, its calibrated Youden-J threshold,
        and whether it fired. This is computed inside _consensus; surfacing it makes the LEARNED typing
        decision inspectable (not a black box) — fired properties first, then by strength."""
        out = []
        for p in self.fams[family]["distinctive"]:
            if p in self.di:
                score = float(s[self.di[p]])
                threshold = float(self.thr.get(p, 0.5))
                out.append({"property": p, "score": round(score, 3),
                            "threshold": round(threshold, 3), "fired": score >= threshold})
        return sorted(out, key=lambda e: (not e["fired"], -e["score"]))

    def route(self, values, header=None, world_only=False, min_fire=0.0):
        """Decode the FAMILY by property consensus. Returns {family, frac, geo, is_entity, scores} or None (a
        literal column whose best family fires < ABSTAIN of its distinctive props). `world_only`/`min_fire` are
        accepted for interface compatibility; the abstain gate + downstream grounding (knowledge_query) decide the
        join, so they do not change the family decode."""
        s = self._profile(values, header)
        if s is None:
            return None
        scores = self._consensus(s)
        if not scores:
            return None
        best = max(scores, key=scores.get)
        if scores[best] < ABSTAIN:                                          # literal / non-entity -> abstain
            return None
        return {"family": best, "frac": round(float(scores[best]), 3), "geo": bool(self.fams[best]["geo"]),
                "is_entity": True, "scores": {k: round(float(v), 3) for k, v in scores.items()},
                "evidence": self._evidence(s, best)}                        # the auditable per-property 'why'


def main():
    r = Router()
    tests = {
        "city": ["Paris", "Tokyo", "London", "Berlin", "Madrid", "Rome"],
        "country": ["France", "Germany", "Japan", "Brazil", "Canada", "Italy"],
        "film": ["Inception", "Titanic", "Gladiator", "The Matrix", "Interstellar"],
        "person": ["Barry Levinson", "Taylor Swift", "Neil Young", "John Asher"],
        "hospital": ["Mayo Clinic", "Cleveland Clinic", "Johns Hopkins Hospital"],
        "amount": ["120", "80", "45", "200", "15"],
        "status": ["active", "pending", "closed", "cancelled"],
    }
    for h, vals in tests.items():
        o = r.route(vals, header=h)
        print(f"  {h:9s} -> {('ABSTAIN' if o is None else o['family']+' (%.2f, geo=%s)' % (o['frac'], o['geo']))}")


if __name__ == "__main__":
    main()
