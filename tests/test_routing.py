"""Tests for the shared execution-engine router (engine.routing) + cross-process SQL repeatability.

engine.routing.route() is the SINGLE routing decision, called by both live serving (engine.knowledge_compose)
and the Spider eval (spider.probe.full_eval). Invariant under test: the typed-AST planner owns EVERY own-data
query; the ComposeEngine may stand ONLY on a NECESSARY world dependency — a world attribute/value the upload
does not already provide. A world_join alone is NOT sufficient (contrastive tests below).
"""
from __future__ import annotations
import os
import subprocess
import sys

from engine.routing import route, compose_owns, world_grounded, Route, COMPOSITION_OPS, WORLD_DEP_OPS, DEPTH_PRIMS
from engine.compose import ComposeEngine


def _views(*ops):
    return [{"op": op, "columns": []} for op in ops]


_NECESSARY = {"is_necessary": True, "necessary": ["continent"]}
_REDUNDANT = {"is_necessary": False, "necessary": []}


def test_own_data_composition_routes_to_ast():
    # own-data HAVING / share / group-by / yoy have NO world dependency -> the typed-AST planner owns them.
    assert route(_views("group_agg", "having"), None) is Route.AST
    assert route(_views("share"), None) is Route.AST
    assert compose_owns(_views("yoy"), None) is False
    assert compose_owns(["having", "group_agg"], None) is False       # flat op-list form (the eval's res['plan'])
    assert compose_owns([], None) is False
    print("  PASS  own-data composition -> AST")


def test_necessary_world_composite_stands():
    # a NECESSARY world dependency + an analytical composition = a genuine world composite -> compose owns it.
    assert route(_views("world_join", "topn"), _NECESSARY) is Route.COMPOSE
    assert compose_owns(["world_join", "share"], _NECESSARY) is True
    assert compose_owns(_views("world_filter", "yoy"), _NECESSARY) is True
    print("  PASS  necessary world composite -> compose stands")


def test_redundant_world_join_is_own_data():
    # THE reviewer's case: a world_join happened but the referenced attribute is ALREADY uploaded -> the join was
    # redundant -> own-data -> the typed-AST planner owns it. world_join op present but is_necessary False.
    assert route(_views("world_join", "share"), _REDUNDANT) is Route.AST
    assert compose_owns(_views("world_join", "group_agg"), _REDUNDANT, result_rows=[["Europe", 1]]) is False
    print("  PASS  redundant world_join (attr uploaded) -> AST, not compose")


def test_plain_world_lookup_defers_to_delegate():
    # a NECESSARY world dependency but NO composition (a bare world-filtered scalar) -> the KnowledgeQuery
    # delegate is authoritative; route() returns AST (compose does not own it).
    assert route(_views("world_join", "world_filter"), _NECESSARY, result_rows=[[270]]) is Route.AST
    print("  PASS  plain world lookup -> delegate (route AST)")


def test_world_group_by_stands():
    # a necessary world dependency + a MULTI-column group_agg that produced rows -> per-dimension breakdown -> compose.
    grouped = [{"op": "world_join"}, {"op": "group_agg", "columns": ["continent", "total"]}]
    assert compose_owns(grouped, _NECESSARY, result_rows=[["Europe", 180], ["Asia", 90]]) is True
    # a single-column (scalar) world group is not a genuine breakdown.
    assert compose_owns([{"op": "world_join"}, {"op": "group_agg", "columns": ["total"]}], _NECESSARY,
                        result_rows=[[270]]) is False
    print("  PASS  world group-by -> compose; scalar world agg -> delegate")


def test_constants_are_coherent():
    assert "having" in COMPOSITION_OPS and "having" not in WORLD_DEP_OPS   # own-data HAVING is the planner's
    assert WORLD_DEP_OPS == {"world_join", "world_filter"}
    assert "HAVING" in DEPTH_PRIMS
    assert world_grounded(_views("world_join")) is True and world_grounded(_views("group_agg")) is False
    print("  PASS  routing constants coherent")


# --- Contrastive: the SAME query, once with the attribute reachable only via world grounding, once already ---
# --- uploaded. Necessity must differ (the whole point of the explicit world_dependency record). reader=None ---
# --- exercises the deterministic heuristic path (no encoder needed). ---
_WORLD = {"name": "world", "columns": ["city", "country", "continent"],
          "rows": [["Paris", "France", "Europe"], ["Lyon", "France", "Europe"], ["Tokyo", "Japan", "Asia"]]}


def _dep(tables, question):
    res = ComposeEngine(reader=None).run([dict(t) for t in tables], question, world=_WORLD)
    return res.get("world_dependency")


def test_contrastive_necessity_world_vs_uploaded():
    # (a) referenced value 'France' is only reachable by resolving city -> country through the world model.
    cities = {"name": "s", "columns": ["city", "amount"], "rows": [["Paris", 100], ["Lyon", 80], ["Tokyo", 50]]}
    dep_world = _dep([cities], "total amount in France")
    assert dep_world and dep_world["is_necessary"], f"France via city->country must be world-necessary: {dep_world}"
    # (b) SAME query, but 'country'/'France' is ALREADY an uploaded column -> filtered directly, no world grounding
    # needed -> the query is own-data (world dependency absent or not necessary).
    country = {"name": "s", "columns": ["country", "amount"], "rows": [["France", 100], ["France", 80], ["Japan", 50]]}
    dep_own = _dep([country], "total amount in France")
    assert not (dep_own and dep_own.get("is_necessary")), f"France already uploaded must not be world-necessary: {dep_own}"
    print("  PASS  contrastive necessity: world-resolved vs uploaded differ")


_REPEAT_SCRIPT = (
    "import os, sys\n"
    "sys.path.insert(0, os.environ['PR_ROOT'])\n"
    "import torch; torch.manual_seed(0)\n"
    "from engine.encoder_overlay import EncoderQuery\n"
    "enc = EncoderQuery()\n"
    "tables=[{'name':'t','columns':['city','amount'],'rows':[['Paris',100],['Lyon',80],['Paris',50]]}]\n"
    "norm,fks=enc.ingest(tables); sch,_,tmap=enc.schema(norm,fks)\n"
    "c=enc.search_ast('total amount in Paris', sch, norm, fks, max_candidates=25)\n"
    "print('SQL::' + (c[0].sql if c else 'NONE'))\n"
)


def test_cross_process_sql_repeatability():
    # The typed-AST planner's SELECTION must be deterministic ACROSS processes (review Finding 4): two fresh
    # interpreters, same input, byte-identical SQL. Pin threads/hashseed so ordering can't drift.
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env = {**os.environ, "PR_ROOT": root, "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "PYTHONHASHSEED": "0"}
    outs, err = [], ""
    for _ in range(2):
        r = subprocess.run([sys.executable, "-c", _REPEAT_SCRIPT], capture_output=True, text=True,
                           env=env, timeout=300)
        err = r.stderr[-500:]
        line = next((l[5:] for l in r.stdout.splitlines() if l.startswith("SQL::")), "")
        outs.append(line.strip())
    assert outs[0] and outs[0] != "NONE", f"planner produced no SQL: {outs}\nstderr: {err}"
    assert outs[0] == outs[1], f"cross-process SQL differs:\n  run1={outs[0]}\n  run2={outs[1]}"
    print(f"  PASS  cross-process SQL byte-identical: {outs[0]}")


TESTS = [
    test_own_data_composition_routes_to_ast,
    test_necessary_world_composite_stands,
    test_redundant_world_join_is_own_data,
    test_plain_world_lookup_defers_to_delegate,
    test_world_group_by_stands,
    test_constants_are_coherent,
    test_contrastive_necessity_world_vs_uploaded,
    test_cross_process_sql_repeatability,
]

if __name__ == "__main__":
    print("=== routing + necessity + cross-process repeatability ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_routing: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
