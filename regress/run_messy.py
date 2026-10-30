"""The messy-question benchmark on the engine's served path, from one fresh process: right, asked back, or wrong, by workbook
and family. This is the bar for generalization (DECISIONS.md, 2026-10-30): Spider's questions repeat the schema's words,
and these do not. The frozen split reports totals only, so its misses are never studied.

Needs a live Postgres (KB_PG_*), as every served path does.

--pool asks instead whether the readers' pool holds a right answer at all, and at what rank: every candidate run and graded,
no selection, no refusal (an oracle ablation, never serving). It separates what the readers can read from what the choice
chooses, needs no Postgres, and never runs on the frozen split.

usage: python -m regress.run_messy [--split dev|frozen|all] [--workbook a,b] [--only words] [--tag x] [--out dir] [--pool]
"""
import argparse
import collections
import json
import os
import statistics
import subprocess
import time

from regress.messy_cases import cases, grade
from regress.messy_workbooks import workbook


def run(selected, serve_one):
    """One record per case, in order. ``serve_one(tables, question)`` is the served request."""
    records = []
    for case in selected:
        started = time.perf_counter()
        try:
            response = serve_one(workbook(case.workbook), case.question)
        except Exception as exc:                   # noqa: BLE001 - a request that raises is an error the user sees
            response = {"error": f"{type(exc).__name__}: {exc}"}
        verdict, note = grade(case.expected, response)
        records.append({"workbook": case.workbook, "question": case.question, "family": case.family, "split": case.split,
                        "verdict": verdict, "seconds": round(time.perf_counter() - started, 1), "note": note,
                        "read_as": str(response.get("read_as") or "")[:300], "sql": str(response.get("sql") or "")[:1200]})
    return records


def pool_records(selected):
    """One record per case: the best rank at which the readers' pool holds a right answer, None when it holds none."""
    from engine.sql_schema import SchemaGraph
    from engine.tables import TableQuery

    planner = TableQuery()
    prepared = {}
    records = []
    for case in selected:
        if case.workbook not in prepared:
            norm, keys = planner.ingest(workbook(case.workbook))
            schema, _, tables = planner.schema(norm, keys)
            prepared[case.workbook] = norm, keys, schema, tables, SchemaGraph.from_planner(schema, keys)
        norm, keys, schema, tables, graph = prepared[case.workbook]
        started = time.perf_counter()
        pool = planner.search_pool(case.question, norm, keys, schema, graph=graph)
        best = None
        for rank, candidate in enumerate(pool):
            try:
                result = planner.execute(tables, schema, candidate.sql, query=candidate.query)
            except Exception:                      # noqa: BLE001 - a candidate that fails answers nothing
                continue
            if grade(case.expected, {"result": result})[0] == "right":
                best = rank
                break
        records.append({"workbook": case.workbook, "question": case.question, "family": case.family, "split": case.split,
                        "verdict": "right" if best is not None else "wrong", "best_rank": best, "pool": len(pool),
                        "seconds": round(time.perf_counter() - started, 1), "note": "", "read_as": "", "sql": ""})
    return records


def summary(records):
    """{right, asked, wrong, n, seconds_p50} overall and by workbook, family and split."""
    def tally(rows):
        count = collections.Counter(r["verdict"] for r in rows)
        return {"n": len(rows), "right": count["right"], "asked": count["asked"], "wrong": count["wrong"],
                "seconds_p50": statistics.median(r["seconds"] for r in rows) if rows else None}

    groups = {key: {name: tally([r for r in records if r[key] == name]) for name in sorted({r[key] for r in records})}
              for key in ("workbook", "family", "split")}
    return {"total": tally(records), **groups}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--split", choices=("dev", "frozen", "all"), default="dev")
    ap.add_argument("--workbook", help="comma-separated workbooks (default all)")
    ap.add_argument("--only", help="only questions containing these words")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--out", default=os.path.join("regress", "results"))
    ap.add_argument("--pool", action="store_true", help="the readers' pool's recall, not the served answer")
    args = ap.parse_args()
    if args.pool and args.split != "dev":
        ap.error("--pool studies misses, so it reads the dev split only")
    selected = [case for case in cases(args.workbook.split(",") if args.workbook else None)
                if (args.split == "all" or case.split == args.split) and (not args.only or args.only in case.question)]
    if args.pool:
        records = pool_records(selected)
    else:
        from engine.knowledge import KnowledgeReasoner
        from regress.live_schema import live_schema, served

        reasoner = KnowledgeReasoner()
        schema = live_schema().name
        records = run(selected, lambda tables, question: served(schema, reasoner.serve, tables, question, schema))
    frozen = args.split != "dev"
    if not frozen:
        for record in records:
            print(f"{record['verdict']:5} {record['seconds']:6.1f}s {record['workbook']:13} {record['question']}\n"
                  f"      read as: {record['read_as'][:140]}\n      {record['note'][:160]}", flush=True)
    report = {"summary": summary(records), "split": args.split,
              "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
              "dirty": bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip()),
              "data_dir": os.environ.get("PREREASONER_DATA_DIR", "engine/data")}
    if not frozen:
        report["records"] = records
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, f"messy_{args.tag}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as sink:
        json.dump(report, sink, indent=1)
    total = report["summary"]["total"]
    print(f"{args.split}: {total['right']} right, {total['asked']} asked, {total['wrong']} wrong of {total['n']} "
          f"(p50 {total['seconds_p50']} s) -> {path}", flush=True)
    for key in ("family", "workbook"):
        print(key, {name: f"{t['right']}/{t['asked']}/{t['wrong']}" for name, t in report["summary"][key].items()}, flush=True)


if __name__ == "__main__":
    main()
