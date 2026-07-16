"""Probe D (the crown-jewel): run the PreReasoner COMPOSITION CORE end-to-end on Spider, Postgres-free.

The live serving stack (WorldReasoner -> ComposedWorldQuery -> WorldQuery) is Postgres-gated and cannot run
here. BUT the reasoning core it delegates to — ComposeEngine + the LEARNED 10-primitive head
(PrimitiveReader) + the unified Qwen/LoRA encoder (EncoderQuery.read_op_model / _encode) — executes on
in-memory SQLite with `world=None`. For self-contained Spider DBs `world` IS None (no world knowledge), so
this reproduces the compose path faithfully, minus:
  * the tables.py slot-filler (general `WHERE col=value` value-matched filters) — a SECOND live SQL generator
    that executes on Postgres, so plain FILTERED single-table SELECTs are UNDER-counted here;
  * the Postgres world-join (irrelevant to Spider) and the clarify gate.
So this is a COMPONENT probe: a partial, compose-core execution accuracy — a lower bound on the full system
for filtered queries, faithful for grouped/ranked/derived/counted ones.

Two input configs, reported side by side:
  * gold_tables : feed ONLY the tables the gold SQL references (oracle table selection) — isolates
                  operand/linking quality from table-selection.
  * whole_db    : feed ALL of the DB's tables (gold-blind, product-realistic) — measures the extra cost of
                  table selection + the FK-join flattening behavior.
"""
from __future__ import annotations
import argparse
import collections
import json
import os
import sqlite3
import traceback
import warnings

warnings.filterwarnings("ignore")

from hardness import eval_hardness, shape
from spider_eval import compare, record_integrated_result, recursive_gold_table_names

DIFFS = ["easy", "medium", "hard", "extra"]


# ---------------- data loading ----------------
def sqlite_tables(db_path):
    con = sqlite3.connect(db_path)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = [r[0] for r in cur.fetchall()]
    out = {}
    for n in names:
        try:
            cur.execute(f'SELECT * FROM "{n}"')
        except sqlite3.Error:
            continue
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        out[n.lower()] = {"name": n, "columns": cols, "rows": rows}
    con.close()
    return out


def exec_gold(db_path, sql):
    con = sqlite3.connect(db_path)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    try:
        cur = con.execute(sql)
        return [list(r) for r in cur.fetchall()], None
    except Exception as e:                       # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    finally:
        con.close()


# ---------------- run one config ----------------
def run_config(eng, tabs, question):
    try:
        res = eng.run(tabs, question, world=None)
        ans = res.get("answer")
        rows = ans["rows"] if ans else None
        return {"ok": True, "plan": res.get("plan"), "primitives": res.get("primitives"),
                "sql": (res["views"][-1]["sql"] if res.get("views") else None),
                "rows": rows}
    except Exception as e:                       # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        stage = "join_build" if "ambiguous" in str(e) or "join" in str(e).lower() else "plan_exec"
        return {"ok": False, "error": msg, "stage": stage}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--dbs", required=True, help="dir of <db_id>.sqlite files")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    ap.add_argument("--per-diff", type=int, default=0, help="cap examples per difficulty (0=all)")
    ap.add_argument("--limit", type=int, default=0, help="global cap (0=all)")
    ap.add_argument("--configs", default="gold_tables,whole_db")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    dev = json.load(open(os.path.join(args.data, "dev.json"), encoding="utf-8"))
    tables_meta = {t["db_id"]: t for t in json.load(open(os.path.join(args.data, "tables.json"), encoding="utf-8"))}
    configs = args.configs.split(",")

    # stratified selection
    buckets = collections.defaultdict(list)
    for i, ex in enumerate(dev):
        buckets[eval_hardness(ex["sql"])].append(i)
    picked = []
    for d in DIFFS:
        idxs = buckets[d]
        picked += idxs[:args.per_diff] if args.per_diff else idxs
    picked.sort()
    if args.limit:
        picked = picked[:args.limit]

    print(f"loading encoder (Qwen LoRA + relational readout, CPU)...", flush=True)
    from engine.encoder_overlay import EncoderQuery
    from engine.primitive_head import PrimitiveReader
    from engine.compose import ComposeEngine
    enc = EncoderQuery()
    eng = ComposeEngine(reader=PrimitiveReader(encoder=enc))
    print(f"loaded. evaluating {len(picked)} examples x {len(configs)} configs\n", flush=True)

    db_cache = {}
    def get_db(db_id):
        if db_id not in db_cache:
            db_cache[db_id] = sqlite_tables(os.path.join(args.dbs, db_id + ".sqlite"))
        return db_cache[db_id]

    # counters per config
    stat = {c: collections.defaultdict(collections.Counter) for c in configs}   # config -> diff -> Counter
    stage_hist = {c: collections.Counter() for c in configs}
    per_example = []

    for n, i in enumerate(picked):
        ex = dev[i]; db_id = ex["db_id"]; diff = eval_hardness(ex["sql"])
        dbp = os.path.join(args.dbs, db_id + ".sqlite")
        gold_rows, gerr = exec_gold(dbp, ex["query"])
        alltabs = get_db(db_id)
        rec = {"idx": i, "db_id": db_id, "difficulty": diff, "question": ex["question"],
               "gold": ex["query"], "gold_exec_error": gerr, "configs": {}}
        for c in configs:
            if c == "gold_tables":
                names = [t.lower() for t in recursive_gold_table_names(ex, tables_meta)]
                tabs = [alltabs[t] for t in names if t in alltabs]
                if not tabs:
                    tabs = list(alltabs.values())
            else:
                tabs = list(alltabs.values())
            r = run_config(eng, tabs, ex["question"])
            cmp = compare(gold_rows, r.get("rows")) if r["ok"] else {}
            st = stat[c][diff]
            st["n"] += 1
            record_integrated_result(st, gold_rows, cmp, bool(r["ok"]))
            if not r["ok"]:
                stage_hist[c][r["stage"]] += 1
            rec["configs"][c] = {**r, **cmp}
        per_example.append(rec)
        if (n + 1) % 25 == 0:
            print(f"  {n+1}/{len(picked)} done", flush=True)

    # ---------------- summarize ----------------
    def agg(c):
        tot = collections.Counter()
        for d in DIFFS:
            tot.update(stat[c][d])
        return tot

    summary = {"n_examples": len(picked), "per_diff_cap": args.per_diff, "configs": {}}
    for c in configs:
        tot = agg(c)
        summary["configs"][c] = {
            "totals": dict(tot),
            "answered_pct": round(100 * tot["answered"] / max(tot["n"], 1), 1),
            "error_pct": round(100 * tot["error"] / max(tot["n"], 1), 1),
            "correct_lenient_pct": round(100 * tot["correct_lenient"] / max(tot["n"], 1), 1),
            "correct_strict_pct": round(100 * tot["correct_strict"] / max(tot["n"], 1), 1),
            "scalar_gold_accuracy_pct": round(100 * tot["scalar_correct"] / max(tot["scalar_total"], 1), 1),
            "scalar_gold_n": tot["scalar_total"],
            "error_stage_histogram": dict(stage_hist[c]),
            "by_difficulty": {d: dict(stat[c][d]) for d in DIFFS},
        }

    os.makedirs(args.out, exist_ok=True)
    suf = ("_" + args.tag) if args.tag else ""
    json.dump(summary, open(os.path.join(args.out, f"compose_eval{suf}.json"), "w"), indent=2)
    json.dump(per_example, open(os.path.join(args.out, f"compose_eval_per_example{suf}.json"), "w"), indent=2)

    # ---------------- report ----------------
    P = print
    P("\n" + "=" * 78); P("PROBE D — COMPOSE-CORE END-TO-END (SQLite, learned head + unified encoder)"); P("=" * 78)
    P(f"examples: {len(picked)}   (gold exec errors excluded from accuracy denominators where they occur)")
    for c in configs:
        s = summary["configs"][c]; t = s["totals"]
        P(f"\n--- config: {c} ---")
        P(f"  answered   : {t.get('answered',0):4d}  ({s['answered_pct']}%)")
        P(f"  error      : {t.get('error',0):4d}  ({s['error_pct']}%)   stages={s['error_stage_histogram']}")
        P(f"  CORRECT (lenient containment, generous UB): {t.get('correct_lenient',0):4d}  ({s['correct_lenient_pct']}%)")
        P(f"  CORRECT (strict row-set equality, harsh LB): {t.get('correct_strict',0):4d}  ({s['correct_strict_pct']}%)")
        P(f"  SCALAR-gold accuracy (clean, unambiguous subset): "
          f"{t.get('scalar_correct',0)}/{s['scalar_gold_n']}  ({s['scalar_gold_accuracy_pct']}%)")
        P(f"  by difficulty (lenient / n):")
        for d in DIFFS:
            dd = stat[c][d]
            P(f"     {d:8s} n={dd.get('n',0):3d}  answered={dd.get('answered',0):3d}  "
              f"lenient={dd.get('correct_lenient',0):3d}  strict={dd.get('correct_strict',0):3d}  "
              f"scalar={dd.get('scalar_correct',0)}/{dd.get('scalar_total',0)}")
    P(f"\nwrote: results/compose_eval{suf}.json (+ per_example)")


if __name__ == "__main__":
    main()
