"""Compatibility analytics plus the active generalized Schema.org interpretation.

The old relational readout remains available for the per-cell evolution UI. It is not
the production ontology router. ``schema_org`` is produced by the same URI-indexed,
calibrated property head used by :mod:`engine.router`.
"""
from __future__ import annotations
import json
from pathlib import Path

from engine.config import DATA_DIR
from engine.encoder_overlay import load_encoder                 # reuse the production encoder when standalone
from engine.tables import TableQuery

TAX_FAMS = {"struct", "taxonomy", "intent"}                     # the alloc families of the taxonomy model


class DimensionModel(TableQuery):
    """Dimension analysis over the production encoder and trained taxonomy readout.

    Production passes the already-loaded world encoder; standalone callers load the
    same encoder bundle and nothing else.
    """

    def __init__(self, deploy_dir=DATA_DIR, *, shared_encoder=None):
        d = Path(deploy_dir)
        TableQuery.__init__(self, d)
        if shared_encoder is None:
            # Dimension is a readout over the same encoder bundle. load_encoder supplies
            # the standalone/test path.
            load_encoder(self, d)
        else:
            # The world and dimension endpoints intentionally share one Qwen/LoRA and
            # relational readout. A second AutoModel load made Cloud Run startup exceed
            # its hard 10-minute CPU startup window and doubled resident model memory.
            for attr in ("model_bundle_sha256", "encoder_data_dir", "alloc", "nc", "dims",
                         "sid", "thr", "model", "nL", "tok", "qwen", "hdim"):
                setattr(self, attr, getattr(shared_encoder, attr))
            self.thr = dict(self.thr)

        dt = d / "dim_thresholds.json"                                       # OVERRIDE with thresholds calibrated on the
        if dt.exists():                                                      # TRAINED model (calibrate_dims) — the
            self.thr.update({str(k): float(v)                               # ridge scale mis-fits the qwen_lora+readout
                             for k, v in json.load(open(dt)).items()})       # this model actually runs

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
