"""PreReasoner regression gate — run BEFORE every deploy.

Two tiers, run together:
  * OFFLINE (always): the non-world text-to-SQL golden cases (regress/offline_cases.py), executed through the
    REAL engine (live routing: compose view-stack vs slot-filler) on in-memory SQLite. No Postgres needed.
  * WORLD (iff KB_PG_PASSWORD): the world-model-join golden cases (regress/world_cases.py) against a seeded
    world Postgres — the product's differentiator (city->country resolution, "total amount in France"=270).

Exit non-zero if ANY case regresses. Designed to be the test step in cloudbuild.yaml (runs inside the built
engine image, where torch + the model weights live — GitHub CI can only compile-check, see .github/workflows).

  python -m regress.run_regression            # offline + (world if PG env set)
  python -m regress.run_regression --offline  # offline only
"""
from __future__ import annotations
import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")

# live routing gate — mirrors engine.knowledge_compose.ComposedKnowledgeQuery.DEPTH_PRIMS
DEPTH_PRIMS = frozenset({"EXCL", "RATIO", "SHARE", "HAVING", "DIVIDE", "RUNNING"})
_COMPOSITION_VIEWS = {"topn", "yoy", "running", "share", "divide", "having", "time_filter", "sort"}


def _nval(v):
    if v is None:
        return "∅"
    s = str(v).strip()
    try:
        return round(float(s.replace(",", "").lstrip("$").rstrip("%")), 3)
    except (ValueError, AttributeError):
        return s.lower()


def _flatset(rows):
    return {_nval(v) for r in (rows or []) for v in r}


class Engine:
    """Loads the trained encoder once; routes + runs a case exactly like the live serve() (offline, world=None)."""

    def __init__(self):
        from engine.encoder_overlay import EncoderQuery
        from engine.primitive_head import PrimitiveReader
        from engine.compose import ComposeEngine
        self.enc = EncoderQuery()
        self.reader = PrimitiveReader(encoder=self.enc)
        self.compose = ComposeEngine(reader=self.reader)

    def _slot(self, tabs, q):
        norm, fks = self.enc.ingest(tabs)
        sch, _, tmap = self.enc.schema(norm, fks)
        slots, join, involved, _ = self.enc.plan(q, sch, norm, fks)
        sql, _ = self.enc.assemble(slots, join, involved, sch)
        ok, why = self.enc.guard(sql)
        if not ok:
            raise RuntimeError(f"guard: {why}")
        _, rows = self.enc.execute(tmap, sch, sql)
        return [list(r) for r in rows], sql

    def run(self, tabs, q):
        try:
            depth = bool(self.reader.present(q) & DEPTH_PRIMS)
        except Exception:                                    # noqa: BLE001
            depth = False
        if depth:
            try:
                res = self.compose.run(tabs, q, world=None)
                if any(op in _COMPOSITION_VIEWS for op in (res.get("plan") or [])):
                    ans = res.get("answer") or {}
                    return ans.get("rows"), (res["views"][-1]["sql"] if res.get("views") else None)
            except Exception:                                # noqa: BLE001 — live serve() delegates on engine error
                pass
        return self._slot(tabs, q)


def check(case, rows):
    fset = _flatset(rows)
    fails = []
    if "expect_scalar" in case:
        if not (rows and len(rows) == 1 and len(rows[0]) == 1 and _nval(rows[0][0]) == _nval(case["expect_scalar"])):
            fails.append(f"expected scalar {case['expect_scalar']}, got {rows}")
    if "forbid_scalar" in case:
        if rows and len(rows) == 1 and len(rows[0]) == 1 and _nval(rows[0][0]) == _nval(case["forbid_scalar"]):
            fails.append(f"got forbidden scalar {case['forbid_scalar']}")
    for v in case.get("expect_contains", []):
        if _nval(v) not in fset:
            fails.append(f"missing expected value {v!r}")
    if "expect_min_rows" in case and len(rows or []) < case["expect_min_rows"]:
        fails.append(f"expected >={case['expect_min_rows']} rows, got {len(rows or [])}")
    return fails


def run_unit_checks():
    """Deterministic invariants on engine.joins (the compose-path FK discovery the tier-1 change touched).
    The end-to-end 'by title' case routes to the SLOT path (engine.relations, unchanged), so the joins.py
    regression is only visible here — a compose-routed share/having question over these sheets would hit it."""
    from engine.joins import discover_fks, join_plan
    from regress.offline_cases import STORES, EMPLOYEES
    print("\n=== UNIT invariants (engine.joins compose-path FK discovery) ===")
    fails = []
    stores, emps = dict(STORES), dict(EMPLOYEES)
    fks = discover_fks([stores, emps])
    # (1) a relationship-named FK must resolve (Manager_ID -> employees.Employee_ID) — the SHIPPED regression
    if not any(f[1] == "Manager_ID" and f[2] == "employees" for f in fks):
        fails.append(f"REG joins.discover_fks dropped relationship-named FK Manager_ID->employees (got {fks})")
    else:
        print("  ok   REG fk_manager_id_resolves")
    # (2) the anti-spurious win must hold: a table's OWN self-id coincidentally ⊆ another id col is NOT an FK
    concert = {"name": "concert", "columns": ["concert_ID", "Theme"],
               "rows": [["1", "Rock"], ["2", "Jazz"], ["3", "Pop"]]}
    stadium = {"name": "stadium", "columns": ["Stadium_ID", "Name", "Capacity"],
               "rows": [["1", "Alpha", 100], ["2", "Beta", 200], ["3", "Gamma", 300], ["4", "Delta", 400]]}
    sfks = discover_fks([concert, stadium])
    if any(f[0] == "concert" and f[1] == "concert_ID" and f[2] == "stadium" for f in sfks):
        fails.append(f"joins.discover_fks re-admitted spurious self-id join concert_ID->stadium (got {sfks})")
    else:
        print("  ok       fk_no_spurious_selfid")
    # (3) TableQuery.plan calls self._is_id (the year-filter helper), so EVERY TableQuery subclass used in
    # serving must have it — the PG own-data planner _TableQueryPg lacked it and crashed live (offline
    # EncoderQuery has it, so this is invisible to the model tiers). Pin every serving subclass.
    from engine.tables import TableQuery
    from engine.pg import _TableQueryPg
    from engine.encoder_overlay import EncoderQuery
    missing = [c.__name__ for c in (TableQuery, _TableQueryPg, EncoderQuery) if not hasattr(c, "_is_id")]
    if missing:
        fails.append(f"REG TableQuery subclass(es) missing _is_id -> plan() crashes: {missing}")
    else:
        print("  ok   REG tablequery_subclasses_have_is_id")
    if fails:
        for f in fails:
            print(f"  FAIL {f}")
    return ["unit:" + f.split()[0] for f in fails]


def run_offline(eng):
    from regress import offline_cases
    print("\n=== OFFLINE tier (non-world text-to-SQL) ===")
    passed, failed = 0, []
    for c in offline_cases.CASES:
        try:
            rows, sql = eng.run([dict(t) for t in c["tables"]], c["question"])
            fails = check(c, rows)
        except Exception as e:                               # noqa: BLE001
            rows, sql, fails = None, None, [f"exception: {type(e).__name__}: {e}"]
        tag = "REG " if c.get("regression") else "    "
        if fails:
            failed.append(c["name"])
            print(f"  FAIL {tag}{c['name']}: {'; '.join(fails)}")
            print(f"            sql={sql}")
        else:
            passed += 1
            print(f"  ok   {tag}{c['name']}")
    print(f"  offline: {passed} passed, {len(failed)} failed")
    return failed


def run_world():
    if not os.environ.get("KB_PG_PASSWORD"):
        print("\n=== WORLD tier: SKIPPED (no KB_PG_PASSWORD) ===")
        print("  NOTE: a deploy gate MUST run this against a seeded world Postgres (db/sync seed, or a")
        print("        hermetic mini-seed — a documented follow-up, see regress/README.md). Skipped != passed.")
        return [], True
    from regress import world_cases
    print("\n=== WORLD tier (world-model-join, live Postgres) ===")
    return world_cases.run(), False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="offline tier only")
    ap.add_argument("--require-world", action="store_true", help="fail if the world tier is skipped")
    args = ap.parse_args()

    unit_failed = run_unit_checks()

    print("\nloading engine (Qwen LoRA + relational readout)...", flush=True)
    eng = Engine()
    off_failed = run_offline(eng) + unit_failed

    world_failed, skipped = ([], True)
    if not args.offline:
        world_failed, skipped = run_world()

    print("\n" + "=" * 60)
    total_fail = len(off_failed) + len(world_failed)
    if total_fail:
        print(f"REGRESSION GATE: FAIL — {total_fail} case(s): {off_failed + world_failed}")
        sys.exit(1)
    if skipped and args.require_world:
        print("REGRESSION GATE: FAIL — world tier required but skipped (no Postgres)")
        sys.exit(1)
    print("REGRESSION GATE: PASS" + ("  (world tier skipped)" if skipped else ""))
    sys.exit(0)


if __name__ == "__main__":
    main()
