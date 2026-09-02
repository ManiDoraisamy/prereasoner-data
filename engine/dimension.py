"""Compatibility analytics plus the active generalized Schema.org interpretation.

The old relational readout remains available for the per-cell evolution UI. It is not
the production ontology router. ``schema_org`` is produced by the same URI-indexed,
calibrated property head used by :mod:`engine.router`.
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np
import torch

from engine.artifact_provenance import validate_weight_bundle
from engine.config import BASE_MODEL_REVISION as MODEL_REVISION, DATA_DIR
from engine.encoder_overlay import EncoderQuery                 # reuse analyze/_layers/_encode
from engine.encoder_model import RelationalModel
from engine.tables import MODEL_ID

TAX_FAMS = {"struct", "taxonomy", "intent"}                     # the alloc families of the taxonomy model


class DimensionModel(EncoderQuery):
    """/api/dimension analyze on the trained taxonomy model. Loads encoder_meta.pt/encoder.pt/qwen_lora
    directly + the anchor-head thresholds; reads the TAXONOMY family in the readout."""

    def __init__(self, deploy_dir=DATA_DIR):
        d = Path(deploy_dir)
        self.model_bundle_sha256 = validate_weight_bundle(d)
        pt = torch.load(d / "encoder_meta.pt", map_location="cpu", weights_only=True)
        self.alloc = pt["alloc"]; self.nc = self.alloc["n_content"]
        self.dims = sorted(self.alloc["dims"], key=lambda x: x["dim_id"])
        self.sid = {dm["name"]: dm["dim_id"] for dm in self.dims}
        z = np.load(d / "anchor_assignment.npz", allow_pickle=False)        # ridge-probe Youden-J thresholds (base, all dims)
        self.thr = {str(n): float(t) for n, t in zip(z["dims"], z["thr"])}
        dt = d / "dim_thresholds.json"                                       # OVERRIDE with thresholds calibrated on the
        if dt.exists():                                                      # TRAINED model (calibrate_dims) — the
            self.thr.update({str(k): float(v)                               # ridge scale mis-fits the qwen_lora+readout
                             for k, v in json.load(open(dt)).items()})       # this model actually runs

        self.model = RelationalModel(**pt["cfg"]); self.model.load_state_dict(
            torch.load(d / "encoder.pt", map_location="cpu", weights_only=True)); self.model.eval()
        self.nL = pt["cfg"]["layers"] + 1
        from transformers import AutoModel, AutoTokenizer
        from peft import PeftModel
        self.tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        base = AutoModel.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, low_cpu_mem_usage=True
        ).float()
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

    def _schema_interpreter(self):
        interpreter = self.__dict__.get("_schema_interp")
        if interpreter is None:
            from engine.schema_model import SchemaInterpreter
            interpreter = SchemaInterpreter(shared=(self.qwen, self.tok))
            self._schema_interp = interpreter
        return interpreter

    def analyze(self, table, max_rows=24, table_unit=False):
        """Return compatibility evolution and the active Schema.org class decode."""
        result = super().analyze(table, max_rows=max_rows, table_unit=table_unit)
        bounded = {**table, "rows": list(table.get("rows") or ())[:max_rows]}
        result["schema_org"] = self._schema_interpreter().interpret_table(bounded)
        result["model"] = (
            "generalized Schema.org v30 named-property head; legacy per-cell evolution retained for compatibility"
        )
        return result
