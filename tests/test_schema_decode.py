"""Hermetic proof that the Schema.org class decode is MECHANISM, not narration.

Drives the REAL production artifacts — engine/data/schema_class_signatures.json (calibrated signatures +
class thresholds) and engine/data/schema_property_model.json (per-property thresholds) — through the REAL
engine.schema_decode.ClassDecoder with synthetic property profiles. No torch, no Postgres, no network.

The interpretability contract under test:
  (1) the decode is reconstructable from the surfaced named-property contributions;
  (2) INTERVENTION: changing one coordinate changes the class logit by exactly its signed contribution and
      leaves disjoint-signature classes numerically untouched — the explanation IS the computation;
  (3) unsupported/uncalibrated classes and evidence-free profiles abstain instead of guessing.

python -m tests.test_schema_decode
"""
from __future__ import annotations
import json
import math
import sys

from engine.config import DATA_DIR
from engine.schema_decode import ClassDecoder, SIGNATURES_PATH

ERS = "https://schema.org/ExchangeRateSpecification"
UPS = "https://schema.org/UnitPriceSpecification"
MED = "https://schema.org/MedicalCode"


def _thresholds():
    meta = json.loads((DATA_DIR / "schema_property_model.json").read_text(encoding="utf-8"))
    return {k: float(v) for k, v in meta["thresholds"].items()}


def _p(name):
    return f"https://schema.org/{name}"


def _logit(value):
    return math.log(value / (1.0 - value))


def _reconstructed_score(evidence):
    if evidence.score_model == "logistic_property_probability":
        linear = evidence.bias + sum(
            item["class_contribution"] for item in (*evidence.fired, *evidence.missing)
        )
        return 1.0 / (1.0 + math.exp(-linear))
    total = sum(max(float(item["weight"]), 0.0) for item in (*evidence.fired, *evidence.missing))
    return sum(max(float(item["weight"]), 0.0) for item in evidence.fired) / total


# Unit-strength named coordinates make mechanism probes independent of separately calibrated property
# thresholds. Schema-v2 class calibration consumes the continuous values; merely crossing every individual
# threshold is neither necessary nor sufficient for a joint class decision.
_ECB = {_p(name): 1.0 for name in ("currency", "currentExchangeRate", "price", "priceCurrency")}
_MEDICAL = {_p(name): 1.0 for name in ("codeValue", "codingSystem", "inCodeSet")}


def test_ecb_profile_decodes_exchange_rate():
    # The multi-label decode: an ECB-shaped profile yields BOTH ExchangeRateSpecification (currency +
    # currentExchangeRate) and UnitPriceSpecification (price + priceCurrency) — schema.org's actual
    # representation of an exchange rate. Winner ordering is deterministic (score desc, then uri).
    d = ClassDecoder()
    out = d.decode(_ECB, property_thresholds=_thresholds())
    uris = [e.class_uri for e in out]
    assert ERS in uris and UPS in uris, f"ECB profile must decode both exchange-rate classes: {uris}"
    for e in out:
        assert e.servable and e.score >= (e.threshold or 1.0), f"decoded class below threshold: {e.record()}"
    print(f"  PASS  ECB profile -> {[u.rsplit('/', 1)[1] for u in uris]}")


def test_evidence_reconstructs_the_class_decision():
    # Faithfulness: every signature property appears in fired|missing and the class score is reconstructable
    # from the surfaced named-coordinate contributions (plus the surfaced bias for schema v2).
    d = ClassDecoder()
    thr = _thresholds()
    ev = d.evidence(_ECB, ERS, thr)
    row = d.classes[ERS]
    sig_props = {it["property"] for it in row["signature"]}
    seen = {r["property"] for r in ev.fired} | {r["property"] for r in ev.missing}
    assert seen == sig_props, f"evidence must cover the whole signature: {seen} != {sig_props}"
    for r in list(ev.fired) + list(ev.missing):
        assert r["fired"] == (r["score"] >= r["threshold"]), f"fired inconsistent: {r}"
    recomputed = _reconstructed_score(ev)
    assert abs(recomputed - ev.score) < 1e-6, (
        f"score must be recomputable from surfaced contributions: {recomputed} vs {ev.score}"
    )
    print("  PASS  evidence covers the signature; class score is reconstructable")


def test_intervention_is_faithful_and_local():
    """The mechanism proof, without assuming which learned dimension must dominate.

    An earlier version asserted that suppressing `schema:currency` collapses ExchangeRateSpecification. That
    encoded an assumption about the calibration, not a property of the design, and it broke honestly: the
    precision-first threshold settled at exactly `currentExchangeRate`'s weight share, making that property
    alone sufficient. Which property is load-bearing is a calibration outcome and may change. What must NOT
    change is that the surfaced evidence IS the computation:

      FAITHFUL   a coordinate intervention moves exactly the surfaced weighted term
      LOCAL      a class with a disjoint signature is numerically identical under the same intervention
    """
    d = ClassDecoder()
    thr = _thresholds()
    row = d.classes[ERS]
    base = d.evidence(_ECB, ERS, thr)
    assert base.score >= (base.threshold or 1.0), f"baseline must decode: {base.score} vs {base.threshold}"
    assert len(base.fired) >= 2, (
        f"the intervention probe needs two active coordinates, got {len(base.fired)}: "
        f"{[(r['name'], r['score'], r['threshold']) for r in row['signature'] and base.missing]}"
    )

    med_signature = {item["property"] for item in d.classes[MED]["signature"]}
    for fired in base.fired:
        suppressed = {**_ECB, fired["property"]: 0.0}
        after = d.evidence(suppressed, ERS, thr)
        if base.score_model == "logistic_property_probability":
            expected_delta = float(fired["weight"]) * float(_ECB[fired["property"]])
            actual_delta = _logit(base.score) - _logit(after.score)
            assert abs(actual_delta - expected_delta) < 1e-7, (
                f"suppressing {fired['name']} must remove exactly its signed logit contribution: "
                f"{actual_delta} vs {expected_delta}"
            )
        else:
            total = sum(max(float(item["weight"]), 0.0) for item in row["signature"])
            expected = base.score - max(float(fired["weight"]), 0.0) / total
            assert abs(after.score - expected) < 1e-9, (
                f"suppressing {fired['name']} must move the score by exactly its weight share: "
                f"{after.score} vs expected {expected}"
            )
        # LOCAL: a disjoint-signature class is untouched by this same intervention
        if fired["property"] not in med_signature:
            med = d.evidence(_MEDICAL, MED, thr).score
            med_after = d.evidence({**_MEDICAL, fired["property"]: 0.0}, MED, thr).score
            assert med == med_after, f"disjoint class moved under {fired['name']}: {med} vs {med_after}"
    print("  PASS  intervention removes the exact surfaced term and is local to its signature")


def test_schema_v2_signed_superposition_is_reconstructable():
    import tempfile
    from pathlib import Path

    payload = {
        "schema_version": 2,
        "property_model_pending": False,
        "classes": [{
            "uri": "https://schema.org/Product",
            "name": "Product",
            "signature": [
                {"property": _p("sku"), "weight": 4.0, "ontology_weight": 3.0},
                {"property": _p("duration"), "weight": -2.0, "ontology_weight": 1.0},
            ],
            "bias": -1.0,
            "score_model": "logistic_property_probability",
            "threshold": 0.8,
            "servable": True,
            "state": "servable",
            "support": {},
        }],
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "signatures.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        decoder = ClassDecoder(path)
        evidence = decoder.evidence({_p("sku"): 1.0, _p("duration"): 0.25}, "Product")
        assert abs(evidence.score - (1.0 / (1.0 + math.exp(-2.5)))) < 1e-12
        assert abs(_reconstructed_score(evidence) - evidence.score) < 1e-9
        assert decoder.decode({_p("sku"): 1.0})[0].class_name == "Product"
        assert decoder.decode({}) == ()
    print("  PASS  schema-v2 signed superposition is exact and evidence-free input abstains")


def test_silent_profile_abstains():
    # A literal table (no property fires) decodes to NOTHING — abstention, not a guess.
    d = ClassDecoder()
    assert d.decode({}, property_thresholds=_thresholds()) == (), "empty profile must abstain"
    print("  PASS  silent profile -> abstain (no class invented)")


def test_unservable_classes_never_decode():
    # Honest coverage: classes that failed calibration (Country/City/... on this head) or were never observed
    # must NOT appear in a default decode, no matter the profile. They remain representable (present in the
    # artifact with an explicit state), just not servable.
    d = ClassDecoder()
    everything = {p: 1.0 for row in d.classes.values() for it in row["signature"] for p in [it["property"]]}
    out = d.decode(everything, property_thresholds=_thresholds(), top_k=10_000)
    assert all(e.servable for e in out), "default decode must only ever emit servable classes"
    states = {row["state"] for row in d.classes.values()}
    assert "representable_unobserved" in states, "unobserved classes must stay representable (explicit state)"
    n_serv = sum(1 for row in d.classes.values() if row.get("servable"))
    assert n_serv == len({e.class_uri for e in out}), f"decode ceiling must equal servable classes ({n_serv})"
    print(f"  PASS  only servable classes decode ({n_serv} of {len(d.classes)}); the rest abstain with explicit states")


def test_uncalibrated_signatures_refuse_to_load():
    # REGRESSION: rebuilding class signatures without retraining produces an artifact where every class has
    # servable=False/threshold=None, because the TRAINER is what calibrates and promotes them. Loading it
    # silently made serving abstain on every table — indistinguishable from an honest "nothing matched".
    # The decoder must refuse it instead. (Observed: signatures regenerated at 15:38 against a training run
    # that last promoted at 15:17 left the entire class path inert.)
    import json as _json
    import tempfile
    payload = _json.loads(SIGNATURES_PATH.read_text(encoding="utf-8"))
    payload["property_model_pending"] = True
    for row in payload["classes"]:
        row["servable"], row["threshold"] = False, None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        _json.dump(payload, handle)
        pending = handle.name
    try:
        ClassDecoder(pending)
    except ValueError as exc:
        assert "calibrat" in str(exc).lower(), f"must name the cause, got: {exc}"
        print("  PASS  uncalibrated signatures refuse to load (loud, not silently all-abstain)")
        return
    raise AssertionError("an uncalibrated signature artifact must not load — it makes every class abstain")


def test_artifact_chain_is_pinned():
    # Reproducibility: signatures and model meta must agree on the ontology contract + corpus identity, so a
    # served explanation names exactly which artifacts produced it.
    sig = json.loads(SIGNATURES_PATH.read_text(encoding="utf-8"))
    meta = json.loads((DATA_DIR / "schema_property_model.json").read_text(encoding="utf-8"))
    assert sig["ontology_contract_sha256"] == meta["ontology_contract_sha256"], "ontology contract mismatch"
    assert sig["corpus_sha256"] == meta["corpus_sha256"], "corpus identity mismatch"
    assert meta["weights_sha256"] and meta["artifact_sha256"] and sig["artifact_sha256"], "artifacts must be hashed"
    print("  PASS  artifact chain pinned: ontology == corpus identity across signatures + model meta")


TESTS = [
    test_ecb_profile_decodes_exchange_rate,
    test_evidence_reconstructs_the_class_decision,
    test_intervention_is_faithful_and_local,
    test_schema_v2_signed_superposition_is_reconstructable,
    test_silent_profile_abstains,
    test_unservable_classes_never_decode,
    test_uncalibrated_signatures_refuse_to_load,
    test_artifact_chain_is_pinned,
]

if __name__ == "__main__":
    print("=== schema.org class decode: mechanism + intervention ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_schema_decode: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
