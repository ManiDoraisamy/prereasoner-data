"""DimensionModel — the /api/dimension analyze model on the TRAINED taxonomy encoder (qwen_lora +
RelationalModel), reading the TAXONOMY named dims: the per-column/per-cell readout fires the Wikidata
taxonomy nodes (geographical_feature -> ... -> urban_settlement -> city). Per-dim thresholds come from the
anchor head (anchor_assignment.npz, Youden-J calibrated ~0.05 where the taxonomy dims fire — a 0.5 cut would
show nothing), OVERRIDDEN by thresholds calibrated on the trained model (dim_thresholds.json) where present.
Inherits analyze() from the shared readout; only the loader + _salient_evo differ.
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import torch

from engine.config import DATA_DIR
from engine.encoder_overlay import EncoderQuery                 # reuse analyze/_layers/_encode
from engine.encoder_model import RelationalModel
from engine.tables import MODEL_ID

TAX_FAMS = {"struct", "taxonomy", "intent"}                     # the alloc families of the taxonomy model


class DimensionModel(EncoderQuery):
    """/api/dimension analyze on the trained taxonomy model. Loads encoder_meta.pt/encoder.pt/qwen_lora
    directly + the anchor-head thresholds; reads the TAXONOMY family in the readout."""

    def __init__(self, deploy_dir=DATA_DIR):
        d = Path(deploy_dir)
        pt = torch.load(d / "encoder_meta.pt", map_location="cpu", weights_only=False)
        self.alloc = pt["alloc"]; self.nc = self.alloc["n_content"]
        self.dims = sorted(self.alloc["dims"], key=lambda x: x["dim_id"])
        self.sid = {dm["name"]: dm["dim_id"] for dm in self.dims}
        z = np.load(d / "anchor_assignment.npz", allow_pickle=True)         # ridge-probe Youden-J thresholds (base, all dims)
        self.thr = {str(n): float(t) for n, t in zip(z["dims"], z["thr"])}
        dt = d / "dim_thresholds.json"                                       # OVERRIDE with thresholds calibrated on the
        if dt.exists():                                                      # TRAINED model (calibrate_dims) — the
            self.thr.update({str(k): float(v)                               # ridge scale mis-fits the qwen_lora+readout
                             for k, v in json.load(open(dt)).items()})       # this model actually runs

        self.model = RelationalModel(**pt["cfg"]); self.model.load_state_dict(
            torch.load(d / "encoder.pt", map_location="cpu")); self.model.eval()
        self.nL = pt["cfg"]["layers"] + 1
        from transformers import AutoModel, AutoTokenizer
        from peft import PeftModel
        self.tok = AutoTokenizer.from_pretrained(MODEL_ID)
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        base = AutoModel.from_pretrained(MODEL_ID, low_cpu_mem_usage=True).float()
        self.qwen = PeftModel.from_pretrained(base, str(d / "qwen_lora")).eval()
        self.hdim = base.config.hidden_size

    def _salient_evo(self, layers, ui):
        ddims = [dd for dd in self.dims if dd["family"] in TAX_FAMS]
        fin = layers[-1][ui]
        fired = [dd["name"] for dd in ddims if fin[dd["dim_id"]] >= self.thr.get(dd["name"], 0.5)]
        amax = max(ddims, key=lambda dd: fin[dd["dim_id"]])["name"]
        salient = sorted(set(fired) | {amax})
        return [{nm: round(float(min(1.0, max(0.0, layers[L][ui][self.sid[nm]]))), 3) for nm in salient}
                for L in range(self.nL)]
