"""Explicit URI-indexed Schema.org property head and table interpreter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from engine.config import DATA_DIR, DEVICE
from engine.schema_decode import SIGNATURES_PATH, ClassDecoder
from engine.schema_org import load_contract, schema_name


MODEL_PATH = DATA_DIR / "schema_property_head.pt"
MODEL_META_PATH = DATA_DIR / "schema_property_model.json"
MAX_SAMPLE_VALUES = 6


def summarize_table(table: dict) -> str:
    """Canonical serving/training table text; values are bounded and order-preserving."""
    name = str(table.get("name") or "table").strip()
    columns = [str(value) for value in (table.get("columns") or ())]
    rows = table.get("rows") or ()
    parts = ["table " + name.replace("_", " ")]
    for index, column in enumerate(columns):
        values = []
        for row in rows:
            if index >= len(row) or row[index] is None or not str(row[index]).strip():
                continue
            value = str(row[index]).strip().replace("\n", " ")[:160]
            if value not in values:
                values.append(value)
            if len(values) == MAX_SAMPLE_VALUES:
                break
        parts.append(f"{column.replace('_', ' ')}: {'; '.join(values)}" if values else
                     column.replace("_", " "))
    return " | ".join(parts)[:16_000]


class NamedPropertyHead:
    """Torch-light wrapper; torch is imported only when the model is actually used."""

    def __init__(self, in_dim: int, property_uris, state=None):
        import torch.nn as nn
        self.property_uris = tuple(property_uris)
        self.module = nn.Linear(in_dim, len(self.property_uris))
        if state is not None:
            self.module.load_state_dict(state)


class SchemaInterpreter:
    def __init__(self, shared=None, *, model_path: str | Path = MODEL_PATH,
                 meta_path: str | Path = MODEL_META_PATH):
        import torch
        self.torch = torch
        self.contract = load_contract()
        meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        if meta["ontology_contract_sha256"] != self.contract.contract_sha256:
            raise ValueError("Schema.org property model and ontology contract differ")
        self.meta = meta
        # The head's thresholds and the class signatures are calibrated together against ONE corpus.
        # Rebuilding either alone leaves a pair that loads without complaint and then behaves incoherently
        # — class scores computed from thresholds that were never fitted to those signatures.
        signatures = json.loads(SIGNATURES_PATH.read_text(encoding="utf-8"))
        if signatures.get("corpus_sha256") != meta.get("corpus_sha256"):
            raise ValueError(
                f"class signatures describe corpus {str(signatures.get('corpus_sha256'))[:16]} but the "
                f"property model describes {str(meta.get('corpus_sha256'))[:16]}; rebuild and retrain together"
            )
        self.properties = tuple(meta["trained_properties"])
        self.thresholds = {key: float(value) for key, value in meta["thresholds"].items()}
        # A dim being TRAINED is not the same as its threshold being trustworthy. Calibration can fail to
        # find any operating point at the precision floor, in which case the recorded threshold is merely
        # `nextafter(max validation score)` — a value that says "never fire" on validation but can fire on
        # anything a serving table scores above it. Those dims must not be presented as evidence alongside
        # properly gated ones, so keep the qualified set and mark the difference.
        self.qualified = frozenset(meta.get("qualified_properties", meta["trained_properties"]))
        state = torch.load(model_path, map_location="cpu", weights_only=True)
        self.head = NamedPropertyHead(meta["input_dim"], self.properties, state).module
        self.head.eval()
        self.decoder = ClassDecoder()
        self.shared = shared
        self._encoder = None

    @property
    def identity(self) -> str:
        return self.meta["artifact_sha256"]

    def _load_encoder(self):
        if self._encoder is not None:
            return self._encoder
        import torch
        from engine.encoder import LiveQwen
        dev = torch.device(DEVICE if DEVICE != "cuda" or torch.cuda.is_available() else "cpu")
        if self.shared is not None:
            qwen, tok = self.shared
            self._encoder = LiveQwen(dev, shared_qwen=qwen, shared_tok=tok)
        else:
            self._encoder = LiveQwen(dev, warm_lora=str(DATA_DIR / "qwen_lora"), serving=True)
        self.head.to(dev)
        return self._encoder

    def profile_text(self, text: str) -> dict[str, float]:
        torch = self.torch
        encoder = self._load_encoder()
        with torch.no_grad():
            embedding = encoder.encode([text], max_len=128, grad=False, bs=1)
            probabilities = torch.sigmoid(self.head(embedding))[0].detach().cpu().tolist()
        return dict(zip(self.properties, probabilities))

    def interpret_table(self, table: dict, *, include_unservable: bool = False) -> dict:
        text = summarize_table(table)
        profile = self.profile_text(text)
        classes = self.decoder.decode(
            profile, property_thresholds=self.thresholds,
            include_unservable=include_unservable,
        )
        properties = [
            {"property": uri, "name": schema_name(uri), "score": round(score, 6),
             "threshold": round(self.thresholds.get(uri, 0.5), 6),
             "qualified": uri in self.qualified,
             # An unqualified dim never counts as fired: its threshold did not survive the precision floor,
             # so treating it as evidence would put an uncalibrated signal next to a calibrated one in the
             # served explanation. Class signatures already exclude these, so this only aligns the surfaced
             # property list with what the decode actually used.
             "fired": uri in self.qualified and score >= self.thresholds.get(uri, 0.5)}
            for uri, score in profile.items()
        ]
        properties.sort(key=lambda item: (not item["fired"], -item["score"], item["property"]))
        return {
            "table": str(table.get("name") or ""),
            "input_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "ontology_version": self.contract.version,
            "ontology_contract_sha256": self.contract.contract_sha256,
            "model_artifact_sha256": self.identity,
            "properties": properties,
            "classes": [item.record() for item in classes],
            "abstained": not classes,
        }

