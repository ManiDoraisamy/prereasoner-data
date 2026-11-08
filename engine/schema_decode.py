"""Deterministic Schema.org class decoding from named property probabilities."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import hashlib
from fractions import Fraction
from pathlib import Path

from engine.config import DATA_DIR
from engine.schema_org import schema_name, schema_uri


SIGNATURES_PATH = DATA_DIR / "schema_class_signatures.json"
BINDING_KINDS = ("noun", "column", "value", "table", "question", "operation", "frame", "control")


@dataclass(frozen=True)
class BindingBasis:
    """Pinned, quantized semantic interface. No embeddings or arbitrary scores cross it.

    A basis describes meaning and calibration, not merely a vector width. Callers must
    explicitly qualify coordinates; an unqualified or unknown observation is never zero.
    Integer weights and levels make every binding contribution exactly replayable.
    """

    coordinates: tuple[str, ...]
    weights: tuple[int, ...]
    qualified: tuple[bool, ...]
    calibration_id: str
    levels: int = 16
    role_qualified: tuple[tuple[str, tuple[bool, ...]], ...] = ()

    def __post_init__(self):
        if (not isinstance(self.coordinates, tuple) or not self.coordinates
                or len(set(self.coordinates)) != len(self.coordinates)
                or any(not isinstance(c, str) or not c.strip() for c in self.coordinates)):
            raise ValueError("binding coordinates must be a nonempty ordered tuple of unique names")
        if (not isinstance(self.weights, tuple) or len(self.weights) != len(self.coordinates)
                or any(type(w) is not int or w < 0 for w in self.weights)
                or not any(self.weights)):
            raise ValueError("binding weights must be nonnegative integers with positive mass")
        if (not isinstance(self.qualified, tuple) or len(self.qualified) != len(self.coordinates)
                or any(type(q) is not bool for q in self.qualified)):
            raise ValueError("binding qualification must explicitly cover every coordinate")
        if type(self.levels) is not int or not 1 <= self.levels <= 65535:
            raise ValueError("binding quantization levels must be in 1..65535")
        if not isinstance(self.calibration_id, str) or not self.calibration_id.strip():
            raise ValueError("binding basis requires an explicit calibration identity")
        if (not isinstance(self.role_qualified, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2 for item in self.role_qualified)):
            raise ValueError("binding role qualification must be explicit immutable masks")
        roles = [kind for kind, _ in self.role_qualified]
        if (any(not isinstance(kind, str) for kind in roles) or len(set(roles)) != len(roles)
                or any(kind not in BINDING_KINDS for kind in roles)):
            raise ValueError("binding role qualification has duplicate or unknown roles")
        for _, mask in self.role_qualified:
            if (not isinstance(mask, tuple) or len(mask) != len(self.coordinates)
                    or any(type(value) is not bool for value in mask)):
                raise ValueError("binding role qualification must cover every coordinate")
        object.__setattr__(self, "role_qualified", tuple(sorted(self.role_qualified)))
        union = tuple(any(mask[i] for _, mask in self.role_qualified) for i in range(len(self.coordinates)))
        if union != self.qualified:
            raise ValueError("global qualification must be the union of explicit calibrated role masks")

    def qualified_for(self, kind):
        if kind not in BINDING_KINDS:
            raise ValueError("unknown semantic profile input kind")
        return dict(self.role_qualified).get(kind, (False,) * len(self.coordinates))

    def record(self):
        result = {"coordinates": list(self.coordinates), "weights": list(self.weights),
                  "qualified": list(self.qualified), "calibration_id": self.calibration_id,
                  "levels": self.levels}
        # Existing unqualified candidates retain their exact basis identity.
        # A globally qualified prototype without role evidence cannot be loaded.
        if self.role_qualified:
            result["role_qualified"] = {kind: list(mask) for kind, mask in sorted(self.role_qualified)}
        return result

    @classmethod
    def from_record(cls, record):
        required = {"coordinates", "weights", "qualified", "calibration_id", "levels"}
        if (not isinstance(record, dict) or not required <= set(record)
                or set(record) - required - {"role_qualified"}
                or not isinstance(record.get("role_qualified", {}), dict)):
            raise ValueError("unknown or malformed binding basis fields")
        if (any(not isinstance(record[key], (list, tuple)) for key in ("coordinates", "weights", "qualified"))
                or any(not isinstance(kind, str) or not isinstance(mask, (list, tuple))
                       for kind, mask in record.get("role_qualified", {}).items())):
            raise ValueError("malformed binding basis qualification masks")
        return cls(tuple(record["coordinates"]), tuple(record["weights"]), tuple(record["qualified"]),
                   record["calibration_id"], record["levels"],
                   tuple((kind, tuple(mask)) for kind, mask in sorted(record.get("role_qualified", {}).items())))

    @property
    def identity(self):
        return hashlib.sha256(json.dumps(self.record(), sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class BindingProfile:
    """One contextual item, with physical identity kept outside semantic coordinates."""

    basis_id: str
    identity: str
    kind: str
    values: tuple[int | None, ...]
    evidence: str
    relationship: str = ""
    datatype: str = ""

    def __post_init__(self):
        if not all(isinstance(s, str) and s.strip() for s in (self.basis_id, self.identity)):
            raise ValueError("binding profile requires basis and physical identity")
        if self.kind not in BINDING_KINDS:
            raise ValueError("unknown semantic profile input kind")
        if self.evidence not in ("expected_concept", "observed_instance"):
            raise ValueError("binding evidence kind must distinguish expected and observed")
        if not isinstance(self.values, tuple) or any(v is not None and type(v) is not int for v in self.values):
            raise ValueError("binding profile values must be quantized integers or explicit unknowns")
        if not isinstance(self.relationship, str) or not isinstance(self.datatype, str):
            raise ValueError("binding relationship and datatype must be explicit strings")

    def record(self):
        return {"basis_id": self.basis_id, "identity": self.identity, "kind": self.kind,
                "values": list(self.values), "evidence": self.evidence,
                "relationship": self.relationship, "datatype": self.datatype}


@dataclass(frozen=True)
class BindingPolicy:
    """Explicit acceptance gates; these require held-out calibration before serving."""

    min_score: tuple[int, int]
    min_coverage: tuple[int, int]
    min_margin: tuple[int, int]
    min_shared_positive: int = 1

    def __post_init__(self):
        for name in ("min_score", "min_coverage", "min_margin"):
            value = getattr(self, name)
            if (not isinstance(value, tuple) or len(value) != 2
                    or any(type(v) is not int for v in value) or value[1] <= 0):
                raise ValueError("binding gates require exact numerator/positive-denominator tuples")
            if not 0 <= Fraction(*value) <= 1:
                raise ValueError("binding gates must be between zero and one")
        if type(self.min_shared_positive) is not int or self.min_shared_positive < 1:
            raise ValueError("binding requires at least one shared positive coordinate")

    def record(self):
        return {"min_score": list(self.min_score), "min_coverage": list(self.min_coverage),
                "min_margin": list(self.min_margin), "min_shared_positive": self.min_shared_positive}


def bind_profiles(basis: BindingBasis, query: BindingProfile, candidates: tuple[BindingProfile, ...],
                  policy: BindingPolicy, *, candidate_kind: str, required_identity: str = "") -> dict:
    """The named-property contributions ARE the semantic binding computation.

    No question text, hidden feature, lexical residual or unaccounted model logit is
    accepted. Unknown candidate observations reduce coverage, not the denominator;
    this prevents an almost-empty profile winning through a tiny overlap. A tie
    between different identities clarifies; stable ordering is only for the trace.
    """
    if candidate_kind not in ("column", "value") or not isinstance(required_identity, str):
        raise ValueError("binding needs a declared candidate kind and optional physical identity")
    if not isinstance(basis, BindingBasis) or not isinstance(query, BindingProfile) or not isinstance(policy, BindingPolicy):
        raise ValueError("binding requires the declared basis, profile and policy types")
    if not isinstance(candidates, tuple) or any(not isinstance(c, BindingProfile) for c in candidates):
        raise ValueError("binding candidates must be an immutable tuple of profiles")
    if len({c.identity for c in candidates}) != len(candidates):
        raise ValueError("duplicate candidate physical identity")
    basis_id = basis.identity
    for profile in (query, *candidates):
        if profile.basis_id != basis_id or len(profile.values) != len(basis.coordinates):
            raise ValueError("binding profile basis/order/calibration mismatch")
        if any(v is not None and not 0 <= v <= basis.levels for v in profile.values):
            raise ValueError("binding value outside the declared quantization range")
    query_qualified = basis.qualified_for(query.kind)
    target_qualified = basis.qualified_for(candidate_kind)
    # Compare the two declared roles' calibrated space. This projection is
    # fixed for the whole candidate set; missing individual observations still
    # reduce coverage and cannot shrink their own denominator.
    available = sum(w * basis.levels for q, w, query_ok, target_ok
                    in zip(query.values, basis.weights, query_qualified, target_qualified)
                    if query_ok and target_ok and q is not None)
    evidence = []
    for candidate in candidates:
        candidate_qualified = basis.qualified_for(candidate.kind)
        rejected = []
        if candidate.kind != candidate_kind:
            rejected.append("candidate_kind_mismatch")
        if required_identity and candidate.identity != required_identity:
            rejected.append("physical_identity_mismatch")
        if query.relationship and candidate.relationship != query.relationship:
            rejected.append("relationship_mismatch")
        if query.datatype and candidate.datatype != query.datatype:
            rejected.append("datatype_mismatch")
        terms, shared, covered, numerator = [], 0, 0, 0
        for name, q, c, weight, qualified, query_ok, candidate_ok in zip(
                basis.coordinates, query.values, candidate.values, basis.weights, basis.qualified,
                query_qualified, candidate_qualified):
            reason = ("unqualified" if not qualified else "query_role_unqualified" if not query_ok else
                      "candidate_role_unqualified" if not candidate_ok else "zero_weight" if not weight else
                      "query_unknown" if q is None else "candidate_unknown" if c is None else "")
            contribution = 0
            if not reason:
                contribution = weight * (min(q, c) - abs(q - c))
                numerator += contribution
                covered += weight * basis.levels
                shared += int(q > 0 and c > 0)
            terms.append({"coordinate": name, "query": q, "candidate": c, "weight": weight,
                          "qualified": qualified and query_ok and candidate_ok,
                          "contribution": contribution, "masked_reason": reason})
        score = Fraction(numerator, available) if available else Fraction(0)
        coverage = Fraction(covered, available) if available else Fraction(0)
        if not available:
            rejected.append("no_query_evidence")
        if shared < policy.min_shared_positive:
            rejected.append("insufficient_shared_positive_evidence")
        if coverage < Fraction(*policy.min_coverage):
            rejected.append("insufficient_coverage")
        if score < Fraction(*policy.min_score):
            rejected.append("below_score_threshold")
        evidence.append({"identity": candidate.identity, "score": [score.numerator, score.denominator],
                         "coverage": [coverage.numerator, coverage.denominator],
                         "numerator": numerator, "normalizer": available, "shared_positive": shared,
                         "eligible": not rejected, "rejected": rejected, "contributions": terms})
    evidence.sort(key=lambda r: (-Fraction(*r["score"]), r["identity"]))
    eligible = [r for r in evidence if r["eligible"]]
    winner, reason = None, "no_eligible_binding"
    margin = None
    if eligible:
        reason = "accepted"
        # A nearly tied runner-up below the absolute score floor still matters:
        # crossing that floor by one quantized step is not a resolved identity.
        rivals = [r for r in evidence if r["identity"] != eligible[0]["identity"]
                  and not any(reason != "below_score_threshold" for reason in r["rejected"])]
        if rivals:
            gap = Fraction(*eligible[0]["score"]) - Fraction(*rivals[0]["score"])
            margin = [gap.numerator, gap.denominator]
            if gap == 0 or gap < Fraction(*policy.min_margin):
                reason = "ambiguous_binding"
        if reason == "accepted":
            winner = eligible[0]["identity"]
    return {"state": "bound" if winner is not None else "clarify", "selected": winner,
            "reason": reason, "basis_id": basis_id, "policy": policy.record(),
            "candidate_kind": candidate_kind, "required_identity": required_identity,
            "margin": margin, "query": query.record(),
            "candidate_profiles": [c.record() for c in sorted(candidates, key=lambda c: c.identity)],
            "candidates": evidence}


def replay_binding(basis_record: dict, decision: dict) -> dict:
    """Recompute a serialized binding with the same strict, embedding-free API."""
    basis = BindingBasis.from_record(basis_record)

    def profile(row):
        if set(row) != {"basis_id", "identity", "kind", "values", "evidence", "relationship", "datatype"}:
            raise ValueError("unknown binding profile fields")
        return BindingProfile(**{**row, "values": tuple(row["values"])})

    p = decision["policy"]
    if set(p) != {"min_score", "min_coverage", "min_margin", "min_shared_positive"}:
        raise ValueError("unknown binding policy fields")
    policy = BindingPolicy(tuple(p["min_score"]), tuple(p["min_coverage"]), tuple(p["min_margin"]),
                           p["min_shared_positive"])
    result = bind_profiles(basis, profile(decision["query"]),
                           tuple(profile(c) for c in decision["candidate_profiles"]), policy,
                           candidate_kind=decision["candidate_kind"], required_identity=decision["required_identity"])
    if result != decision:
        raise ValueError("binding trace changed or contains unaccounted fields")
    return result


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
