"""Hermetic proof that the LEARNED typing decision is AUDITABLE — engine.router.Router surfaces the
per-property evidence it uses to decode the family, so "typed X as Place" is inspectable, not a black box.

The Router reads schema.org PROPERTY dims per column and DECODES the family by consensus over which of a
family's DISTINCTIVE properties fire above their calibrated Youden-J thresholds (engine/data/props_thr.json).
route() now returns that firing as `evidence`. These tests drive the REAL production _evidence()/route() with
a SYNTHETIC property profile — no torch, no Postgres — so the interpretability contract is checked
deterministically: the reported evidence must exactly reconstruct the decode (which props fired, vs which
thresholds), and must stay consistent with the family fraction the router acts on.

python -m tests.test_router_evidence
"""
from __future__ import annotations
import sys

import numpy as np

from engine.router import Router, ABSTAIN


def _profile_with(di, n, firings):
    """A synthetic column property profile: a zero readout with the named dims set to given strengths."""
    s = np.zeros(n, dtype=np.float32)
    for name, val in firings.items():
        s[di[name]] = val
    return s


# A profile that reads like a CITY column: 3 of Place's 4 distinctive props fire (addressCountry, GeoCoordinates,
# postalCode) and one does NOT (image below its 0.403 threshold). Place consensus = 3/4 = 0.75.
_PLACE_FIRINGS = {"addressCountry": 0.80, "GeoCoordinates": 0.75, "postalCode": 0.30, "image": 0.20}


def test_evidence_reconstructs_the_decode():
    # The evidence for the decoded family must list EVERY distinctive property with its read strength, its
    # calibrated threshold, and whether it fired — and fired>=threshold must be internally consistent.
    r = Router()
    s = _profile_with(r.di, r.nc, _PLACE_FIRINGS)
    ev = r._evidence(s, "place")
    props = {e["property"] for e in ev}
    assert props == set(r.fams["place"]["distinctive"]), f"evidence must cover all distinctive props: {props}"
    for e in ev:
        assert e["fired"] == (e["score"] >= e["threshold"]), f"fired inconsistent with score/threshold: {e}"
        assert abs(e["threshold"] - round(float(r.thr.get(e["property"], 0.5)), 3)) < 1e-6, \
            f"surfaced threshold must be the calibrated Youden-J value (rounded): {e}"
    fired = {e["property"] for e in ev if e["fired"]}
    assert fired == {"addressCountry", "GeoCoordinates", "postalCode"}, f"wrong props fired: {fired}"
    assert not any(e["fired"] for e in ev if e["property"] == "image"), "image is below threshold -> must NOT fire"
    print(f"  PASS  evidence reconstructs the decode: fired {sorted(fired)} of place's distinctive props")


def test_evidence_is_ordered_fired_first():
    # Auditability: the properties that DROVE the decode come first (then by strength), so the top of the list is
    # the reason. The non-firing property must sort last.
    r = Router()
    ev = r._evidence(_profile_with(r.di, r.nc, _PLACE_FIRINGS), "place")
    assert [e["property"] for e in ev] == ["addressCountry", "GeoCoordinates", "postalCode", "image"], ev
    assert ev[0]["fired"] and not ev[-1]["fired"], "fired must sort before not-fired"
    print("  PASS  evidence ordered fired-first, strongest-first")


def test_route_surfaces_evidence_matching_the_family():
    # End-to-end through the REAL route(): a Place-like profile decodes to 'place' AND carries the evidence for
    # that family. The evidence's fired-fraction must equal the frac the router acted on (evidence == mechanism).
    r = Router()
    s = _profile_with(r.di, r.nc, _PLACE_FIRINGS)
    r._profile = lambda values, header=None: s                      # inject the synthetic readout (skip the model)
    out = r.route(["Paris", "Tokyo", "Berlin"], header="city")
    assert out is not None and out["family"] == "place", f"expected place decode, got {out}"
    assert "evidence" in out, "route() must surface the per-property evidence (the learned 'why')"
    dp = r.fams["place"]["distinctive"]
    fired_frac = sum(1 for e in out["evidence"] if e["fired"]) / len(dp)
    assert abs(fired_frac - out["frac"]) < 1e-6, f"evidence fired-fraction {fired_frac} must equal frac {out['frac']}"
    assert out["frac"] == 0.75, f"3 of 4 place props fired -> frac 0.75, got {out['frac']}"
    print(f"  PASS  route() surfaces evidence; frac {out['frac']} == evidence fired-fraction (faithful)")


def test_abstain_still_returns_none():
    # A literal column (no family's distinctive props fire) still ABSTAINS — surfacing evidence must not change the
    # decision boundary. Only one weak sub-threshold place prop -> below ABSTAIN -> None (no evidence to surface).
    r = Router()
    s = _profile_with(r.di, r.nc, {"image": 0.20})                  # nothing fires -> every family < ABSTAIN
    r._profile = lambda values, header=None: s
    assert r.route(["120", "80", "45"], header="amount") is None, "a literal must still abstain (decision unchanged)"
    assert ABSTAIN == 0.40, "abstain gate constant unchanged"
    print("  PASS  literal still abstains — evidence is additive, decision boundary unchanged")


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
