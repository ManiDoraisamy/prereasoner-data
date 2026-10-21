"""Choose an answer among a question's candidates, and score the choice.

Policies: "first" (the most probable candidate), "ran" (the most probable that ran and returned rows, else the first
that ran: execution-guided), and "rescorer" (the re-scorer's best among those that ran). On calibration candidate
files (training.readers.candidates) every policy is scored, with the pool's recall (any candidate right). The Spider
test set is scored end to end in totals only: its candidates are built in memory, the chosen one alone is compared
with gold, and nothing per question is written or shown.

usage: python -m training.readers.choose calibration <candidates.jsonl ...> [--rescorer <run dir>]
       python -m training.readers.choose test <rows.jsonl> <outputs.jsonl> [<rows.jsonl> <outputs.jsonl> ...]
           --test-questions <test.json> --test-tables <test_tables.json> --test-dbs <dir>
           [--rescorer <run dir>] [--shard i/n]          (several pairs: the union of their pools)
"""
import argparse
import json
import math
import os


def pick_ran(candidates):
    ran = [c for c in candidates if c["status"] == "ran"]
    with_rows = [c for c in ran if c["rows"]]
    return (with_rows or ran or [None])[0]


class RescorerChoice:
    """A re-scorer run's choice among a question's servable candidates."""

    def __init__(self, directory):
        from transformers import AutoTokenizer

        from training.readers.outputs import load_run
        self.tokenizer = AutoTokenizer.from_pretrained(os.path.join(directory, "tokenizer"))
        self.model = load_run("rescorer", directory)

    def pick(self, question, candidates):
        """(chosen candidate or None, its probability)."""
        from training.readers import rescorer
        ran = [c for c in candidates if rescorer.servable(c)][:rescorer.MAX_CANDIDATES]
        if not ran:
            return None, 0.0
        scores = rescorer.score(self.model, self.tokenizer, question, ran)
        best = max(range(len(ran)), key=lambda i: (scores[i], -i))
        return ran[best], 1.0 / (1.0 + math.exp(-scores[best]))


def calibration(paths, rescorer_dir=None):
    records = [json.loads(line) for path in paths for line in open(path, encoding="utf-8")]
    chooser = RescorerChoice(rescorer_dir) if rescorer_dir else None
    totals = {"first": 0, "ran": 0, "rescorer": 0, "pool": 0}
    for record in records:
        candidates = record["candidates"]
        totals["first"] += bool(candidates and candidates[0].get("right"))
        chosen = pick_ran(candidates)
        totals["ran"] += bool(chosen and chosen.get("right"))
        totals["pool"] += any(c.get("right") for c in candidates)
        if chooser:
            chosen, _ = chooser.pick(record["question"], candidates)
            totals["rescorer"] += bool(chosen and chosen.get("right"))
    n = len(records)
    print(json.dumps({"questions": n, **{k: f"{v} ({v / max(1, n):.1%})" for k, v in totals.items()
                                         if k != "rescorer" or chooser}}))


def test(pairs, questions_path, tables_path, dbs_dir, rescorer_dir=None, shard=(0, 1)):
    """Totals on the Spider test set for the union of the pools that each (rows, outputs) pair gives."""
    from spider.probe.evalutil import exec_sql_timed
    from spider.probe.spider_eval import compare
    from training.readers.candidates import CANDIDATE_SECONDS, candidate_records
    from training.readers.sources import capped_database
    from training.readers.union import merge
    questions = json.load(open(questions_path, encoding="utf-8"))
    metas = {t["db_id"]: t for t in json.load(open(tables_path, encoding="utf-8"))}
    views = []
    for rows_path, outputs_path in pairs:
        rows = {json.loads(line)["index"]: json.loads(line) for line in open(rows_path, encoding="utf-8")}
        outputs = {json.loads(line)["index"]: json.loads(line) for line in open(outputs_path, encoding="utf-8")}
        views.append((rows, outputs))
    chooser = RescorerChoice(rescorer_dir) if rescorer_dir else None
    databases = {}
    right = {"ran": 0, "rescorer": 0}
    n = 0
    for index in sorted(views[0][1]):
        if index % shard[1] != shard[0]:
            continue
        q = questions[index]
        db = q["db_id"]
        if db not in databases:
            databases[db] = capped_database(os.path.join(dbs_dir, db + ".sqlite"), metas[db])
        connection, graph = databases[db]
        pool = merge([candidate_records(outputs[index], rows[index], graph, connection, None)
                      for rows, outputs in views])
        gold_rows, _ = exec_sql_timed(connection, q["query"], timeout=8.0)
        n += 1
        picks = {"ran": pick_ran(pool)}
        if chooser:
            picks["rescorer"] = chooser.pick(q["question"], pool)[0]
        for policy, chosen in picks.items():
            if chosen is None:
                continue
            got, error = exec_sql_timed(connection, chosen["sql"], timeout=CANDIDATE_SECONDS)
            right[policy] += error is None and bool(compare(gold_rows, got).get("strict"))
    print(json.dumps({"set": "test", "shard": f"{shard[0]}/{shard[1]}", "questions": n,
                      **{k: v for k, v in right.items() if k != "rescorer" or chooser}}))


def main():
    ap = argparse.ArgumentParser(description="Score answer choices on calibration candidates or the Spider test set.")
    commands = ap.add_subparsers(dest="command", required=True)
    cal = commands.add_parser("calibration")
    cal.add_argument("candidates", nargs="+")
    cal.add_argument("--rescorer", help="a re-scorer run directory")
    tst = commands.add_parser("test")
    tst.add_argument("files", nargs="+", help="rows.jsonl outputs.jsonl pairs")
    tst.add_argument("--test-questions", required=True)
    tst.add_argument("--test-tables", required=True)
    tst.add_argument("--test-dbs", required=True)
    tst.add_argument("--rescorer", help="a re-scorer run directory")
    tst.add_argument("--shard", default="0/1")
    args = ap.parse_args()
    if args.command == "calibration":
        calibration(args.candidates, args.rescorer)
    else:
        if len(args.files) % 2:
            ap.error("test takes rows.jsonl outputs.jsonl pairs")
        test(list(zip(args.files[0::2], args.files[1::2])), args.test_questions, args.test_tables, args.test_dbs,
             args.rescorer, tuple(map(int, args.shard.split("/"))))


if __name__ == "__main__":
    main()
