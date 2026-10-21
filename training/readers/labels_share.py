"""Share readings from BIRD's percentage questions, each verified by execution.

BIRD writes "the percentage of X among Y" as
    SELECT CAST(SUM(CASE WHEN <part> THEN 1 ELSE 0 END) AS REAL) * 100 / COUNT(*) FROM <tables> [WHERE <whole>]
(or SUM(IIF(<part>, 1, 0)), or COUNT(CASE WHEN <part> THEN ...)). The readings say the same without the scale:
one block over <tables> whose conditions are <whole> and <part>, COUNT(*) shown, and "share" naming the conditions
that make the part. The builder (engine.readers.readings.build) makes it count(part rows) / count(whole rows), the
fraction the product shows. Each reading is checked by running it beside gold on the same capped data: gold's
value must be 100 times the built one, else the question is left out.

usage: python -m training.readers.labels_share <BIRD train dir> <out.jsonl>
"""
import argparse
import json
import os
import re

import sqlglot
from sqlglot import expressions as sge

from engine.readers.readings import NotReadable, build, readings_of
from engine.sql_ast import render_query
from regress.sql_import import import_sql
from spider.probe.evalutil import exec_sql_timed
from training.readers.sources import capped_database

FORM = re.compile(r"(?is)^\s*SELECT\s+CAST\s*\(")


def part_condition(select_expression):
    """The <part> predicate of a share form, or None."""
    node = select_expression
    if isinstance(node, sge.Div):
        numerator = node.this
    else:
        return None
    if isinstance(numerator, sge.Mul):                  # CAST(...) * 100
        numerator = numerator.this if isinstance(numerator.this, sge.Cast) else numerator.expression
    if isinstance(numerator, sge.Cast):
        numerator = numerator.this
    if not isinstance(numerator, (sge.Sum, sge.Count)):
        return None
    inner = numerator.this
    if isinstance(inner, sge.Case) and inner.args.get("ifs"):
        return inner.args["ifs"][0].this
    if isinstance(inner, sge.If):
        return inner.this
    return None


def main():
    ap = argparse.ArgumentParser(description="Share readings from BIRD's percentage questions.")
    ap.add_argument("bird", help="BIRD's train directory (train.json, train_tables.json, train_databases/)")
    ap.add_argument("out")
    args = ap.parse_args()
    bird, out_path = args.bird, args.out
    tables = {t["db_id"]: t for t in json.load(open(os.path.join(bird, "train_tables.json"), encoding="utf-8"))}
    questions = json.load(open(os.path.join(bird, "train.json"), encoding="utf-8"))
    databases = {}
    written = tried = 0
    with open(out_path, "w", encoding="utf-8", newline="\n") as sink:
        for index, q in enumerate(questions):
            if not FORM.match(q["SQL"]):
                continue
            try:
                tree = sqlglot.parse_one(q["SQL"], read="sqlite")
            except Exception:                           # noqa: BLE001 - not parseable: not a share form
                continue
            if not isinstance(tree, sge.Select) or len(tree.expressions) != 1:
                continue
            part = part_condition(tree.expressions[0])
            if part is None:
                continue
            tried += 1
            db = q["db_id"]
            if db not in databases:
                databases[db] = capped_database(os.path.join(bird, "train_databases", db, db + ".sqlite"), tables[db])
            connection, graph = databases[db]
            whole_tree = tree.copy()
            whole_tree.set("expressions", [sqlglot.parse_one("COUNT(*)")])
            part_tree = whole_tree.copy()
            part_tree = part_tree.where(part.copy(), append=True)
            try:
                whole = readings_of(import_sql(whole_tree.sql(dialect="sqlite"), graph))
                both = readings_of(import_sql(part_tree.sql(dialect="sqlite"), graph))
            except Exception:                           # noqa: BLE001 - the parts the typed AST cannot hold
                continue
            if whole["structure"] != "single" or both["structure"] != "single":
                continue
            block = both["blocks"][0]
            whole_keys = {json.dumps(w) for w in whole["blocks"][0]["where"]}
            block["share"] = [i for i, w in enumerate(block["where"]) if json.dumps(w) not in whole_keys]
            if not block["share"]:
                continue
            if block["conj"] == "OR":
                continue                                # whole AND part cannot be one OR
            try:
                sql = render_query(build(both, graph))
            except (NotReadable, ValueError):
                continue
            gold, error = exec_sql_timed(connection, q["SQL"], timeout=8.0)
            got, error2 = exec_sql_timed(connection, sql, timeout=8.0)
            if error or error2 or not gold or not got or gold[0][0] is None or got[0][0] is None:
                continue
            if abs(float(gold[0][0]) - 100.0 * float(got[0][0])) > 1e-6 * max(1.0, abs(float(gold[0][0]))):
                continue
            sink.write(json.dumps({"source": "bird_train", "index": index, "db_id": db, "status": "ok",
                                   "readings": both}) + "\n")
            written += 1
    print(f"BIRD share forms tried {tried}; readings built and matching gold {written} -> {out_path}")


if __name__ == "__main__":
    main()
