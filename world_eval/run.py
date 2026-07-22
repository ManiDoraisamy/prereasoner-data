"""World-model eval — the ORACLE joins the world tables, PreReasoner runs its own path, compare.

For each case: build the uploaded table(s) as SQL CTE(s), run the case's oracle SQL (which JOINs the upload
against knowledgebase."Countries"/"Cities"/"Elements") to get the EXPECTED rows straight from the world DB; then run
the live KnowledgeReasoner (the /api/reason path) on the same upload+question and compare. Reports scalar / lenient
/ strict like the Spider eval, so the two suites read on the same yardstick.

  KB_PG_PASSWORD must be set (autoloaded from .env). Loads the model ONCE.
  python world_eval/run.py
"""
from __future__ import annotations
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                       # sibling cases.py
sys.path.insert(0, os.path.dirname(HERE))      # repo root for `engine`
from cases import CASES                          # noqa: E402


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _lit(v, cast=None):
    if _is_num(v):
        return f"{v}::{cast}" if cast else str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'::{cast}" if cast else f"'{s}'"


def build_cte(tables):
    """Expose each uploaded table as a CTE named by its `name`, with first-row casts to set column types."""
    parts = []
    for t in tables:
        cols, rows = t["columns"], t["rows"]
        types = ["numeric" if _is_num(rows[0][j]) else "text" for j in range(len(cols))]
        vrows = ["(" + ",".join(_lit(v, types[j] if i == 0 else None) for j, v in enumerate(r)) + ")"
                 for i, r in enumerate(rows)]
        parts.append(f'{t["name"]}(' + ",".join('"' + c + '"' for c in cols) + ") AS (VALUES "
                     + ",".join(vrows) + ")")
    return "WITH " + ", ".join(parts) + " "


def _norm(v):
    try:
        return round(float(str(v).replace(",", "").lstrip("$").rstrip("%")), 2)
    except (ValueError, TypeError):
        return str(v).strip().lower()


def _rowset(rows):
    # sort cells within a row (column-order-insensitive) and rows among themselves (row-order-insensitive)
    return sorted(tuple(sorted((_norm(c) for c in r), key=str)) for r in rows)


def main():
    import engine.config  # noqa: F401 — autoloads .env
    if not os.environ.get("KB_PG_PASSWORD"):
        print("KB_PG_PASSWORD not set — cannot run the world model"); sys.exit(2)
    from engine.pg import _pg
    from engine.knowledge import KnowledgeReasoner
    print("loading KnowledgeReasoner (encoder + bge + spaCy + live PG)...", flush=True)
    Q = KnowledgeReasoner()
    sub = os.environ.get("AUTH_TEST_SUB", "world_eval")
    conn = _pg(); conn.autocommit = True; cur = conn.cursor()

    agg = {"n": 0, "pass": 0, "lenient": 0}
    out = []
    for c in CASES:
        # --- ORACLE: expected rows straight from the world tables ---
        try:
            cur.execute(build_cte(c["tables"]) + c["oracle"])
            oracle = cur.fetchall()
        except Exception as e:                                   # noqa: BLE001
            print(f"  [ORACLE-ERR] {c['label']}: {type(e).__name__}: {e}")
            out.append({"label": c["label"], "oracle_error": str(e)}); continue
        # --- PREDICTION: the live world path ---
        perr = None; psql = None; pred = []
        try:
            r = Q.serve([dict(t) for t in c["tables"]], c["question"], sub)
            pred = (r.get("result") or {}).get("rows") or []
            perr = r.get("error"); psql = (r.get("sql") or "")
        except Exception as e:                                   # noqa: BLE001
            perr = f"{type(e).__name__}: {e}"
        o, p = _rowset(oracle), _rowset(pred)
        scalar = len(oracle) == 1 and len(oracle[0]) == 1
        if scalar:
            want = _norm(oracle[0][0])
            passed = any(_norm(x) == want for row in pred for x in row)
        else:
            passed = (o == p)
        lenient = bool(o) and set(map(tuple, o)).issubset(set(map(tuple, p)))
        agg["n"] += 1; agg["pass"] += int(passed); agg["lenient"] += int(lenient or passed)
        verdict = "PASS" if passed else ("LENIENT" if lenient else "FAIL")
        print(f"  [{verdict:7s}] {c['label']:24s} {c['cap']}")
        print(f"            oracle={oracle}  pred={pred[:4]}" + (f"  ERR={perr}" if perr else ""))
        out.append({"label": c["label"], "cap": c["cap"], "question": c["question"],
                    "oracle": [list(map(str, r)) for r in oracle], "pred": [list(map(str, r)) for r in pred],
                    "verdict": verdict, "passed": passed, "lenient": lenient, "pred_sql": psql, "error": perr})
    conn.close()
    n = agg["n"] or 1
    print(f"\n=== WORLD EVAL: {agg['pass']}/{agg['n']} exact ({100*agg['pass']/n:.0f}%), "
          f"{agg['lenient']}/{agg['n']} lenient ({100*agg['lenient']/n:.0f}%) ===")
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    json.dump({"n": agg["n"], "exact": agg["pass"], "lenient": agg["lenient"], "cases": out},
              open(os.path.join(HERE, "results", "world_eval_results.json"), "w"), indent=2)
    print("wrote world_eval/results/world_eval_results.json")


if __name__ == "__main__":
    main()
