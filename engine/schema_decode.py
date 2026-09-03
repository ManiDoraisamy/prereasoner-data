"""Deterministic Schema.org class decoding from named property probabilities."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
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
    score_model: str = "weighted_firing_fraction"
    bias: float = 0.0

    def record(self) -> dict:
        return {
            "class": self.class_uri, "name": self.class_name,
            "score": round(self.score, 6), "threshold": self.threshold,
            "servable": self.servable, "state": self.state,
            "support": self.support, "score_model": self.score_model,
            "bias": self.bias, "fired": list(self.fired), "missing": list(self.missing),
        }


class ClassDecoder:
    def __init__(self, path: str | Path = SIGNATURES_PATH):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.schema_version = payload.get("schema_version")
        if self.schema_version not in (1, 2):
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
        if self.schema_version == 2:
            invalid = [
                row.get("uri", "<unknown>")
                for row in payload.get("classes", ())
                if row.get("servable")
                and row.get("score_model") != "logistic_property_probability"
            ]
            if invalid:
                raise ValueError(
                    "schema-v2 servable classes require logistic named-property scores: "
                    + ", ".join(invalid)
                )
        self.identity = payload.get("artifact_sha256", "")
        self.classes = {row["uri"]: row for row in payload["classes"]}

    @staticmethod
    def score_signature(profile: dict[str, float], signature: list[dict],
                        property_thresholds: dict[str, float] | None = None, *,
                        bias: float = 0.0,
                        score_model: str = "weighted_firing_fraction") -> float:
        """Compute a class score entirely from surfaced named-property coordinates.

        Schema v1 uses a weighted fraction of thresholded firings. Schema v2 uses a
        calibrated logistic superposition of continuous property probabilities; its
        bias and every signed contribution are retained in the class artifact.
        """
        if score_model == "logistic_property_probability":
            linear = float(bias) + sum(
                float(item["weight"]) * float(profile.get(item["property"], 0.0))
                for item in signature
            )
            if linear >= 0:
                return 1.0 / (1.0 + math.exp(-min(linear, 700.0)))
            exp_linear = math.exp(max(linear, -700.0))
            return exp_linear / (1.0 + exp_linear)
        if score_model != "weighted_firing_fraction":
            raise ValueError(f"unsupported class score model: {score_model}")
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
        score_model = row.get("score_model", "weighted_firing_fraction")
        bias = float(row.get("bias", 0.0))
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
                "class_contribution": round(float(item["weight"]) * score, 8),
            }
            (fired if record["fired"] else missing).append(record)
        fired.sort(key=lambda item: (-abs(item["weight"]), item["property"]))
        missing.sort(key=lambda item: (-abs(item["weight"]), item["property"]))
        return ClassEvidence(
            class_uri, row["name"],
            self.score_signature(
                profile, row["signature"], thresholds,
                bias=bias, score_model=score_model,
            ),
            row.get("threshold"), bool(row.get("servable")), row["state"],
            row["support"], tuple(fired), tuple(missing), score_model, bias,
        )

    def decode(self, profile: dict[str, float], *, property_thresholds=None,
               include_unservable: bool = False, top_k: int = 5) -> tuple[ClassEvidence, ...]:
        if not profile or not any(float(value) > 0.0 for value in profile.values()):
            return ()
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
