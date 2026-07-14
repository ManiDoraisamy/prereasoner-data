"""run_all.py — run the full PreReasoner test suite and report a single pass/fail.

Runs each suite as a subprocess and aggregates exit codes. Suites self-skip when their infra isn't
present, so this is safe to run anywhere:
  - test_mcp            always runs (in-process stub; no external deps)
  - test_orchestrator   runs iff ANTHROPIC_API_KEY is set (else SKIP, exit 0)
  - test_world/geo/...  run iff WORLD_PG_PASSWORD is set (else SKIP, exit 0) — the real engine tests

The PRE-DEPLOY gate is `regress.run_regression` (offline text-to-SQL goldens + world-model-join goldens);
its world tier reuses these engine suites. cloudbuild.yaml runs the offline tier in the built image; run
this (or `regress.run_regression --require-world`) against a seeded Postgres for the full world suite.

Run:  python -m tests.run_all
Env:  RUN_ENGINE_TESTS=0 skips the live-Postgres engine suites even if WORLD_PG_PASSWORD is set.
"""
from __future__ import annotations

import os
import subprocess
import sys

SUITES = ["tests.test_mcp", "tests.test_orchestrator"]
ENGINE_SUITES = ["tests.test_world", "tests.test_nongeo", "tests.test_world_joins",
                 "tests.test_route_wired", "tests.test_geo"]


def main():
    suites = list(SUITES)
    if os.environ.get("RUN_ENGINE_TESTS", "1") != "0":
        suites += ENGINE_SUITES

    results = []
    for mod in suites:
        print(f"\n{'='*70}\n# {mod}\n{'='*70}", flush=True)
        rc = subprocess.call([sys.executable, "-m", mod], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        results.append((mod, rc))

    print(f"\n{'='*70}\n# SUMMARY\n{'='*70}")
    failed = [m for m, rc in results if rc != 0]
    for mod, rc in results:
        print(f"  {'OK  ' if rc == 0 else 'FAIL'}  {mod}  (exit {rc})")
    if failed:
        print(f"\n{len(failed)} suite(s) FAILED: {', '.join(failed)}")
        sys.exit(1)
    print("\nALL SUITES PASSED (skipped suites count as pass)")
    sys.exit(0)


if __name__ == "__main__":
    main()
