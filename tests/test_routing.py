"""Tests for the shared execution-engine router (engine.routing) + cross-process SQL repeatability.

engine.routing is the SINGLE source of truth for both live serving (engine.knowledge_compose) and the
Spider eval (spider.probe.full_eval). Invariant under test: the typed-AST planner owns EVERY own-data query;
the ComposeEngine may stand ONLY on a grounded world dependency (world_join / world_filter). Own-data
analytical shapes (HAVING, group-by) route to the planner; self-contained yoy/running/share/divide were
retired (kept only as world-grounded composites).
"""
from __future__ import annotations
import os
import subprocess
import sys

from engine.routing import compose_owns, world_grounded, DEPTH_PRIMS, COMPOSITION_OPS, WORLD_DEP_OPS


def _views(*ops):
    return [{"op": op, "columns": []} for op in ops]


def test_own_data_composition_routes_to_ast():
    # own-data HAVING / share / group-by / yoy have NO world dependency -> the typed-AST planner owns them.
    assert compose_owns(_views("group_agg", "having")) is False
    assert compose_owns(_views("share")) is False
    assert compose_owns(_views("yoy")) is False
    assert compose_owns(["having", "group_agg"]) is False            # flat op-list form (the eval's res['plan'])
    assert compose_owns([]) is False
    print("  PASS  own-data composition -> AST (compose_owns False)")


def test_world_grounded_composition_stands():
    # a world join + an analytical composition = a genuine world composite -> the ComposeEngine owns it.
    assert compose_owns(_views("world_join", "topn")) is True
    assert compose_owns(["world_join", "share"]) is True
    assert compose_owns(_views("world_filter", "yoy")) is True
    assert world_grounded(_views("world_filter")) is True
    assert world_grounded(_views("group_agg", "having")) is False
    print("  PASS  world-grounded composition -> compose stands")


def test_plain_world_lookup_defers_to_delegate():
    # world join + a bare SCALAR aggregate (no analytical op, single-column group) -> the KnowledgeQuery delegate
    # is authoritative ("total amount in France" = one number); compose only re-expresses it for display.
    scalar = [{"op": "world_join"}, {"op": "world_filter"}, {"op": "group_agg", "columns": ["amount"]}]
    assert compose_owns(scalar, result_rows=[[270]]) is False
    print("  PASS  plain world lookup -> delegate (compose_owns False)")


def test_world_group_by_stands():
    # a world join + a MULTI-column group_agg that produced rows = a per-dimension breakdown the delegate cannot
    # express ("total sales by continent") -> compose owns it. A single-column (scalar) world group does not.
    grouped = [{"op": "world_join"}, {"op": "group_agg", "columns": ["continent", "total"]}]
    assert compose_owns(grouped, result_rows=[["Europe", 180], ["Asia", 90]]) is True
    assert compose_owns([{"op": "world_join"}, {"op": "group_agg", "columns": ["total"]}], result_rows=[[270]]) is False
    print("  PASS  world group-by -> compose stands; scalar world agg -> delegate")


def test_constants_are_coherent():
    # HAVING is a composition op but NOT a world-dependency op (own-data HAVING belongs to the AST planner).
    assert "having" in COMPOSITION_OPS and "having" not in WORLD_DEP_OPS
    assert WORLD_DEP_OPS == {"world_join", "world_filter"}
    assert "HAVING" in DEPTH_PRIMS
    print("  PASS  routing constants coherent")


# A fresh interpreter that types + plans one fixed query and prints ONLY the emitted SQL.
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
    env = {**os.environ, "PR_ROOT": root, "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "PYTHONHASHSEED": "0"}
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
    test_world_grounded_composition_stands,
    test_plain_world_lookup_defers_to_delegate,
    test_world_group_by_stands,
    test_constants_are_coherent,
    test_cross_process_sql_repeatability,
]

if __name__ == "__main__":
    print("=== routing + cross-process repeatability ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_routing: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
