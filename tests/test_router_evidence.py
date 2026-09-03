"""Hermetic audit tests for generalized Schema.org named-property routing.

The tests inject URI-indexed property profiles, avoiding Torch and PostgreSQL,
then prove that the surfaced class, score, threshold, and property evidence are
the same named-dimension superposition used by the production decoder.

python -m tests.test_router_evidence
"""
from __future__ import annotations
import sys

from engine.router import Router


def _class_row(router, name):
    return next(row for row in router.decoder.classes.values() if row["name"] == name)


def _full_profile(router, name):
    row = _class_row(router, name)
    if row.get("score_model") == "logistic_property_probability":
        return {
            item["property"]: 1.0 if float(item["weight"]) > 0.0 else 0.0
            for item in row["signature"]
        }
    return {item["property"]: 1.0 for item in row["signature"]}


def _expected_score(router, row, profile):
    return router.decoder.score_signature(
        profile, row["signature"], router.thresholds,
        bias=float(row.get("bias", 0.0)),
        score_model=row.get("score_model", "weighted_firing_fraction"),
    )


def test_evidence_reconstructs_the_decode():
    r = Router()
    row = _class_row(r, "Movie")
    profile = _full_profile(r, "Movie")
    ev = r._class_evidence(profile, row["uri"])
    assert ev.servable and ev.class_name == "Movie"
    assert abs(ev.score - _expected_score(r, row, profile)) < 1e-12
    assert ev.score >= ev.threshold
    assert {item["property"] for item in (*ev.fired, *ev.missing)} == {
        item["property"] for item in row["signature"]
    }
    for item in (*ev.fired, *ev.missing):
        assert item["fired"] == (item["score"] >= item["threshold"])
        assert item["threshold"] == round(r.thresholds[item["property"]], 6)
    print("  PASS  evidence exactly reconstructs Movie's named-property signature")


def test_evidence_is_ordered_fired_first():
    r = Router()
    row = _class_row(r, "Movie")
    profile = _full_profile(r, "Movie")
    missing_property = next(
        item["property"] for item in row["signature"] if profile[item["property"]] > 0.0
    )
    profile[missing_property] = 0.0
    ev = r._class_evidence(profile, row["uri"])
    combined = [*ev.fired, *ev.missing]
    assert combined and all(item["fired"] for item in ev.fired)
    assert all(not item["fired"] for item in ev.missing)
    assert missing_property in {item["property"] for item in ev.missing}
    expected = _expected_score(r, row, profile)
    assert abs(ev.score - expected) < 1e-12
    print("  PASS  fired and missing evidence reproduces the class score")


def test_route_surfaces_evidence_matching_the_family():
    r = Router()
    profile = _full_profile(r, "Movie")
    r._profile = lambda values, header=None: profile
    out = r.route(["Arrival", "Moonlight", "Parasite"], header="movie")
    assert out is not None and out["class"] == "https://schema.org/Movie", out
    row = _class_row(r, "Movie")
    assert out["family"] == "film"
    assert out["frac"] == round(_expected_score(r, row, profile), 6)
    assert out["class_threshold"] == row["threshold"]
    assert out["class_score_model"] == row.get("score_model", "weighted_firing_fraction")
    assert out["class_bias"] == float(row.get("bias", 0.0))
    assert out["ontology_version"] == "30.0"
    assert out["model_artifact_sha256"] == r.model_artifact_sha256
    assert {item["property"] for item in out["evidence"]} == {
        item["property"] for item in row["signature"]
    }
    print("  PASS  route surfaces the canonical class, family, artifact, and actual evidence")


def test_abstain_still_returns_none():
    r = Router()
    r._profile = lambda values, header=None: {}
    assert r.route(["120", "80", "45"], header="amount") is None, "a literal must still abstain (decision unchanged)"
    unsupported = r.decoder.classes["https://schema.org/Thing"]
    assert not unsupported["servable"] and unsupported["threshold"] is None
    print("  PASS  unsupported classes remain representable and explicitly abstain")


# ---- the SERVING capture: the model's typing evidence is collected during a serve and attached to the answer,
# ---- so the learned world-grounding decision reaches the user. These exercise the REAL capture methods on a
# ---- KnowledgeQuery / KnowledgeReasoner built WITHOUT __init__ (no Postgres, no model) — pure buffer logic.
from engine.knowledge_query import KnowledgeQuery
from engine.knowledge import KnowledgeReasoner

_REC = {"table": "customers", "column": "city", "family": "place", "frac": 0.75, "geo": True,
        "grounded_to": "Cities in the World", "evidence": [{"property": "GeoCoordinates", "fired": True}]}


def _bare_qw():
    return KnowledgeQuery.__new__(KnowledgeQuery)                    # bypass __init__ — just the capture-buffer methods


def test_capture_buffer_brackets_a_serve():
    # begin opens a buffer, route emits into it, take closes+returns and clears (so the next serve starts empty).
    qw = _bare_qw()
    assert qw.take_typing() == [], "no buffer open -> nothing captured"
    qw.begin_typing()
    qw._emit_typing([_REC])
    got = qw.take_typing()
    assert got == [_REC], f"captured typing must come back from take: {got}"
    assert qw.take_typing() == [], "take must clear the buffer (no leak into the next serve)"
    print("  PASS  capture buffer brackets a serve (begin/emit/take)")


def test_emit_is_noop_without_a_buffer_and_dedups():
    # emit before begin is a safe no-op (route always calls it; only a serve opens the buffer). Within a serve,
    # the SAME column captured twice (cache miss then a later cache hit) must appear once.
    qw = _bare_qw()
    qw._emit_typing([_REC])                                         # no buffer -> must not raise, must not capture
    assert "_typing_run" not in qw.__dict__
    qw.begin_typing()
    qw._emit_typing([_REC])
    qw._emit_typing([_REC])                                         # cache-hit re-emit of the same column
    qw._emit_typing([{**_REC, "column": "country", "family": "place"}])
    got = qw.take_typing()
    cols = [(r["table"], r["column"]) for r in got]
    assert cols == [("customers", "city"), ("customers", "country")], f"dedup by (table,column) failed: {cols}"
    print("  PASS  emit is a no-op without a buffer + dedups repeat columns")


def test_table_sig_is_value_sensitive():
    # the routing/typing cache key must change when the DATA changes (so stale typing can't attach to new rows).
    a = {"name": "t", "columns": ["city"], "rows": [["Paris"], ["Lyon"]]}
    b = {"name": "t", "columns": ["city"], "rows": [["Paris"], ["Berlin"]]}
    assert KnowledgeQuery._table_sig(a) == KnowledgeQuery._table_sig(dict(a)), "same data -> same key"
    assert KnowledgeQuery._table_sig(a) != KnowledgeQuery._table_sig(b), "different values -> different key"
    print("  PASS  table signature is value-sensitive (no stale typing across data)")


def test_source_grounding_is_the_only_model_abstention_fallback():
    import inspect
    import engine.knowledge_query as knowledge_query

    qw = _bare_qw()
    qw._value_membership_routes = lambda _table: {("customers", "city"): "city"}
    original = knowledge_query.kb_model_route_enabled
    knowledge_query.kb_model_route_enabled = lambda: False
    try:
        qw.begin_typing()
        routes = qw.route({"name": "customers", "columns": ["city"], "rows": [["Paris"]]})
        typing = qw.take_typing()
    finally:
        knowledge_query.kb_model_route_enabled = original
    assert routes == {("customers", "city"): "city"}
    assert typing == [{
        "table": "customers", "column": "city", "kind": "source_grounding",
        "family": "place", "frac": None, "geo": True, "grounded_to": "city",
        "grounding": {
            "source": "wikidata", "index": "knowledgebase.words",
            "method": "exact_normalized_membership",
        },
        "class": None, "class_name": None, "ontology_version": None,
        "model_artifact_sha256": None, "evidence": [],
    }]
    assert "super().route(table)" not in inspect.getsource(KnowledgeQuery.route)
    print("  PASS  generalized abstention falls back only to explicit synchronized source keys")


def test_attach_typing_is_additive_and_safe():
    # the answer carries `typing` only when the model typed something; an own-data answer (no typing) is untouched;
    # a non-dict result never raises.
    kr = KnowledgeReasoner.__new__(KnowledgeReasoner)               # bypass __init__ — just _attach_typing
    answered = kr._attach_typing({"result": {"rows": [[2]]}}, [_REC])
    assert answered.get("typing") == [_REC], "typed world answer must surface `typing`"
    own_data = kr._attach_typing({"result": {"rows": [[883]]}}, [])
    assert "typing" not in own_data, "own-data answer types nothing -> `typing` absent (honest)"
    assert kr._attach_typing(None, [_REC]) is None, "non-dict result must pass through unharmed"
    print("  PASS  _attach_typing additive (present iff typed), safe on non-dict")


TESTS = [
    test_evidence_reconstructs_the_decode,
    test_evidence_is_ordered_fired_first,
    test_route_surfaces_evidence_matching_the_family,
    test_abstain_still_returns_none,
    test_capture_buffer_brackets_a_serve,
    test_emit_is_noop_without_a_buffer_and_dedups,
    test_table_sig_is_value_sensitive,
    test_source_grounding_is_the_only_model_abstention_fallback,
    test_attach_typing_is_additive_and_safe,
]

if __name__ == "__main__":
    print("=== router interpretability: per-property evidence ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_router_evidence: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
