"""Deterministic Schema.org class decoding from named property probabilities."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from engine.config import DATA_DIR
from engine.schema_org import schema_name, schema_uri


SIGNATURES_PATH = DATA_DIR / "schema_class_signatures.json"


@dataclass(frozen=True)
class ClassEvidence:
    class_uri: str
    class_name: str
    score: float
    threshold: float | None
    servable: bool
    state: str
    support: dict
    fired: tuple[dict, ...]
    missing: tuple[dict, ...]

    def record(self) -> dict:
        return {
            "class": self.class_uri, "name": self.class_name,
            "score": round(self.score, 6), "threshold": self.threshold,
            "servable": self.servable, "state": self.state,
            "support": self.support, "fired": list(self.fired), "missing": list(self.missing),
        }


class ClassDecoder:
    def __init__(self, path: str | Path = SIGNATURES_PATH):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported class-signature schema_version")
        # The signature builder writes every class with servable=False and threshold=None; the trainer is
        # what calibrates and promotes them, clearing this flag. Rebuilding signatures WITHOUT retraining
        # therefore yields an artifact in which no class can ever decode — serving would silently abstain
        # on every table, which looks identical to "this upload matched nothing". Fail loudly instead.
        if payload.get("property_model_pending", True):
            raise ValueError(
                f"{Path(path).name} has not been calibrated: every class is unservable until "
                f"training.schema_org.train_property_head runs against this corpus. Rebuilding "
                f"signatures alone leaves the class decode inert."
            )
        self.identity = payload.get("artifact_sha256", "")
        self.classes = {row["uri"]: row for row in payload["classes"]}

    @staticmethod
    def score_signature(profile: dict[str, float], signature: list[dict],
                        property_thresholds: dict[str, float] | None = None) -> float:
        """Class score = the WEIGHTED FRACTION of signature properties that FIRE above their calibrated
        thresholds — the same superposition-decode the family router uses (consensus over firing), so the
        surfaced fired/missing evidence IS the score: a class can never decode while its evidence shows a
        missing signature property carrying no penalty. Calibrated per-property thresholds gate 'fired';
        0.5 is only the uncalibrated fallback."""
        thresholds = property_thresholds or {}
        total = sum(max(float(item["weight"]), 0.0) for item in signature)
        if total <= 0:
            return 0.0
        return sum(
            max(float(item["weight"]), 0.0)
            for item in signature
            if float(profile.get(item["property"], 0.0)) >= float(thresholds.get(item["property"], 0.5))
        ) / total

    def evidence(self, profile: dict[str, float], class_uri: str,
                 property_thresholds: dict[str, float] | None = None) -> ClassEvidence:
        class_uri = schema_uri(class_uri)
        row = self.classes[class_uri]
        thresholds = property_thresholds or {}
        fired = []
        missing = []
        for item in row["signature"]:
            prop = item["property"]
            score = float(profile.get(prop, 0.0))
            threshold = float(thresholds.get(prop, 0.5))
            record = {
                "property": prop, "name": schema_name(prop),
                "score": round(score, 6), "threshold": round(threshold, 6),
                "weight": item["weight"], "fired": score >= threshold,
            }
            (fired if record["fired"] else missing).append(record)
        fired.sort(key=lambda item: (-item["weight"], item["property"]))
        missing.sort(key=lambda item: (-item["weight"], item["property"]))
        return ClassEvidence(
            class_uri, row["name"],
            self.score_signature(profile, row["signature"], thresholds),
            row.get("threshold"), bool(row.get("servable")), row["state"],
            row["support"], tuple(fired), tuple(missing),
        )

    def decode(self, profile: dict[str, float], *, property_thresholds=None,
               include_unservable: bool = False, top_k: int = 5) -> tuple[ClassEvidence, ...]:
        candidates = []
        for uri, row in self.classes.items():
            if not row["signature"] or (not include_unservable and not row.get("servable")):
                continue
            evidence = self.evidence(profile, uri, property_thresholds)
            threshold = evidence.threshold if evidence.threshold is not None else 1.0
            if include_unservable or evidence.score >= threshold:
                candidates.append(evidence)
        candidates.sort(key=lambda item: (-item.score, item.class_uri))
        return tuple(candidates[:top_k])

