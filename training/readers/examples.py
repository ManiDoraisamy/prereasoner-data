"""Training examples for both reader models, from the gold readings of each question (engine.readers.readings) and
the schema as the engine receives it (engine.readers.text).

Stage 1 (schema items, stage1.jsonl): one example per (question, table); labels say whether the table is used and,
for each column, whether it is used and in which roles (select, filter, group, order, having).

Stage 2 (readings, stage2.jsonl): one example per question, the question paired with a pruned schema as serving
prunes it: the gold tables plus the nearest other tables by foreign-key hops, up to 6 in all (gold_pruning), in
schema order, at most 20 columns per table (gold columns kept), fitted to the token budget by dropping the farthest
tables, then the samples. With anchors (the default) the question's own values are listed beside their columns;
--no-anchors serializes without them. Labels are every reading the builder uses; a literal value is located by its
mention in the question, else in its column's listed values.

Rows carry split "train" (readings_train, BIRD, Spider-Syn, rewordings) or "calibration" (readings_calibration, the 20 held-out
Spider databases); the trainers fit on the first and select their checkpoint on the second.

usage: python -m training.readers.examples <readings_train.jsonl> <readings_calibration.jsonl> <out dir>
           [--bird <BIRD train dir> --bird-readings <readings_bird.jsonl>[,<readings_bird_share.jsonl>]]
           [--syn <Spider-Syn dir> --syn-readings <readings_syn.jsonl>]
           [--reworded <rewordings.jsonl>[,...] --reworded-readings <readings_reworded.jsonl>[,...]]
           [--lookups <training.readers.lookups out dir>] [--structures <training.readers.structures out dir>]
           [--no-anchors]
           [--tokenizer <dir or hf id>] [--spider-zip <zip>] [--db-dir <dir> ...]
"""
import argparse
import collections
import json
import os
import random

from engine.readers import numerals
from engine.readers.models import ROLES
from engine.readers.search import BUDGET, MAX_COLUMNS
from engine.readers.text import TOP_TABLES, Schema, fit, table_text
from training.readers.sources import TOKENIZER, Sources, add_arguments


def roles_of(readings):
    """{"t.c": set of roles} and the set of used tables."""
    roles = collections.defaultdict(set)
    tables = set()
    for block in readings["blocks"]:
        tables.update(block["tables"])
        for _, _, column, _ in block["select"]:
            roles[column].add("select")
        for column, _, _ in block["where"]:
            roles[column].add("filter")
        for column in block["group"]:
            roles[column].add("group")
        for _, column, _, _ in block["having"]:
            roles[column].add("having")
        for _, column in block["order"]:
            roles[column].add("order")
    roles.pop("*", None)
    return roles, tables


def find_value(value, question, value_spans):
    """('question'|'schema', start, end) of the literal's mention, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for number, start, end in numerals.mentions(question):
            if float(number) == float(value):
                return "question", start, end
        return None
    text = str(value).strip("%")
    if not text:
        return None
    position = question.lower().find(text.lower())
    if position >= 0:
        return "question", position, position + len(text)
    for listed, start, end in value_spans:
        if listed.lower() == text.lower():
            return "schema", start, end
    return None


def reading_labels(readings):
    """{"t.c" or "*": {"block": [facts of block 0, facts of block 1]}} and [(block, column, literal)]."""
    labels, values = {}, []

    def entry(column):
        return labels.setdefault(column, {"block": [{} for _ in range(2)]})

    for b, block in enumerate(readings["blocks"]):
        for slot, aggregate, column, distinct in block["select"]:
            item = entry(column)["block"][b]
            item.setdefault("select", []).append(aggregate)
            item.setdefault("slots", {}).setdefault(aggregate, slot)
            if distinct:
                item["distinct_aggregate"] = 1
        share = set(block.get("share") or ())
        for i, (column, operator, value) in enumerate(block["where"]):
            if i in share:
                entry(column)["block"][b]["share_part"] = 1
            entry(column)["block"][b].setdefault("where", []).append(operator)
            if isinstance(value, dict):
                entry(column)["block"][b]["where_sub"] = 1
            else:
                values.append((b, column, value))
        for (column, _), count in collections.Counter((c, o) for c, o, _ in block["where"]).items():
            if count > 1:
                entry(column)["block"][b]["where_twice"] = 1
        for column in block["group"]:
            entry(column)["block"][b]["group"] = 1
        for aggregate, column, operator, value in block["having"]:
            item = entry(column)["block"][b]
            item.setdefault("having", []).append(aggregate)
            item.setdefault("having_op", []).append(operator)
            if isinstance(value, dict):
                item["having_sub"] = 1
            else:
                values.append((b, column, value))
        for aggregate, column in block["order"]:
            entry(column)["block"][b].setdefault("order", []).append(aggregate)
    return labels, values


def reading_example(record, question, schema, tables, columns_of, droppable, tokenizer, anchored):
    tables, (text, star, table_spans, column_spans) = fit(tokenizer, BUDGET, question, schema, tables, columns_of,
                                                          droppable, anchored)
    readings = record["readings"]
    blocks = readings["blocks"]
    labels, values = reading_labels(readings)
    value_labels, missing = [], 0
    for b, column, value in values:
        found = find_value(value, question, column_spans.get(column, (None, []))[1])
        if found is None:
            missing += 1
            continue
        where, start, end = found
        value_labels.append({"block": b, "column": column, "value": value, "in": where, "start": start, "end": end})
    return {
        "kind": "gold", "split": record["split"], "db_id": record["db_id"], "source": record["source"],
        "index": record["index"], "question": question, "text": text, "star": star,
        "tables": [{"name": t, "span": table_spans[t],
                    "used": [int(t in block["tables"]) for block in blocks] + [0] * (2 - len(blocks))}
                   for t in tables],
        "columns": [{"name": f"{t}.{c}", "span": column_spans[f"{t}.{c}"][0]} for t in tables for c in columns_of[t]],
        "labels": labels, "values": value_labels, "missing_values": missing, "structure": readings["structure"],
        "globals": [{"distinct": int(block["distinct"]), "conj": block["conj"], "dir": block["dir"],
                     "limit": block["limit"], "counts": counts_of(block, tables),
                     "share": int(bool(block.get("share")))} for block in blocks],
    }


def counts_of(block, tables):
    """How many of each item the block holds, as the decoder takes them: kept tables it uses, (column, aggregate)
    items shown, (column, operator) pairs compared, grouped columns, sort terms and HAVING terms."""
    return {"tables": sum(t in block["tables"] for t in tables),
            "select": len({(column, aggregate) for _, aggregate, column, _ in block["select"]}),
            "where": len({(column, operator) for column, operator, _ in block["where"]}),
            "group": len(block["group"]), "order": len(block["order"]),
            "having": len({(aggregate, column) for aggregate, column, _, _ in block["having"]})}


def gold_pruning(record, schema, roles, used_tables, rng):
    """The gold tables and other tables as serving keeps them: up to TOP_TABLES in all, the nearest by foreign-key
    hops first (ties seeded), since the tables stage 1 also keeps are the ones that look like the question's. A
    quarter of examples keep fewer, so a short schema is seen too. The farthest go first when the budget is short."""
    others = [t for t in schema.tables if t not in used_tables]
    distance = hops(schema, used_tables)
    ranked = sorted(others, key=lambda t: (distance.get(t, 99), rng.random()))
    room = max(0, TOP_TABLES - len(used_tables))
    extra = ranked[:room if rng.random() < 0.75 else rng.randint(0, room)]
    tables = [t for t in schema.tables if t in used_tables or t in extra]
    columns_of = {}
    for t in tables:
        columns = schema.columns[t]
        if len(columns) > MAX_COLUMNS:
            keep = {c for c in columns if f"{t}.{c}" in roles}
            rest = [c for c in columns if c not in keep]
            keep |= set(rng.sample(rest, max(0, MAX_COLUMNS - len(keep))))
            columns = [c for c in columns if c in keep]
        columns_of[t] = columns
    return tables, columns_of, list(reversed(extra))


def hops(schema, start):
    """Foreign-key hops from the nearest of ``start`` to every table it reaches."""
    distance = {t: 0 for t in start}
    frontier = list(start)
    while frontier:
        following = []
        for table in frontier:
            for neighbour in sorted(schema.related[table]):
                if neighbour not in distance:
                    distance[neighbour] = distance[table] + 1
                    following.append(neighbour)
        frontier = following
    return distance


def main():
    ap = argparse.ArgumentParser(description="Stage-1 and stage-2 training examples from gold readings.")
    ap.add_argument("train", help="readings_train.jsonl")
    ap.add_argument("calibration", help="readings_calibration.jsonl")
    ap.add_argument("out_dir")
    ap.add_argument("--bird", help="BIRD's train directory; requires --bird-readings")
    ap.add_argument("--bird-readings", help="comma-separated BIRD readings files (readings_bird.jsonl[,shares])")
    ap.add_argument("--syn", help="Spider-Syn's directory; requires --syn-readings")
    ap.add_argument("--syn-readings")
    ap.add_argument("--reworded", help="comma-separated training.readers.rewordings sets' rewordings.jsonl, "
                                       "concatenated in order; requires --reworded-readings")
    ap.add_argument("--reworded-readings", help="their readings_reworded.jsonl, in the same order")
    ap.add_argument("--lookups", help="training.readers.lookups' output folder (lookup-table questions)")
    ap.add_argument("--structures", help="training.readers.structures' output folder (unmatched and ranked questions)")
    ap.add_argument("--no-anchors", action="store_true", help="serialize stage 2 without the question's values")
    ap.add_argument("--tokenizer", default=TOKENIZER, help="the readers' tokenizer, for the token budget")
    add_arguments(ap)
    args = ap.parse_args()
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    data = Sources.from_args(args)
    sources = [(args.train, "train"), (args.calibration, "calibration")]
    if args.bird:
        data.add_bird(args.bird)
        for path in args.bird_readings.split(","):
            sources.insert(1, (path, "train"))
    if args.syn:
        data.add_syn(args.syn)
        sources.insert(1, (args.syn_readings, "train"))
    if args.reworded:
        merged = data.add_reworded(args.reworded.split(","), args.reworded_readings.split(","), args.out_dir)
        sources.insert(1, (merged, "train"))
    if args.lookups:
        data.add_lookups(args.lookups)
        sources.insert(1, (os.path.join(args.lookups, "readings_lookup.jsonl"), "train"))
    if args.structures:
        data.add_structures(args.structures)
        sources.insert(1, (os.path.join(args.structures, "readings_structure.jsonl"), "train"))
    anchored = not args.no_anchors
    os.makedirs(args.out_dir, exist_ok=True)
    schemas = {}
    counts = collections.Counter()
    with open(os.path.join(args.out_dir, "stage1.jsonl"), "w", encoding="utf-8", newline="\n") as stage1, \
            open(os.path.join(args.out_dir, "stage2.jsonl"), "w", encoding="utf-8", newline="\n") as stage2:
        for path, split in sources:
            for line in open(path, encoding="utf-8"):
                record = json.loads(line)
                if "readings" not in record:
                    continue
                record["split"] = split
                db_id = record["db_id"]
                if db_id not in schemas:
                    schemas[db_id] = Schema(data.graph(db_id))
                schema = schemas[db_id]
                question = data.questions[record["source"]][record["index"]]["question"]
                roles, used_tables = roles_of(record["readings"])
                for table in schema.tables:
                    text, spans = table_text(schema, table)
                    stage1.write(json.dumps({
                        "split": split, "db_id": db_id, "source": record["source"], "index": record["index"],
                        "question": question, "table": table, "text": text, "table_used": int(table in used_tables),
                        "columns": [{"start": s, "end": e, "name": c, "used": int(f"{table}.{c}" in roles),
                                     "roles": [int(r in roles.get(f"{table}.{c}", ())) for r in ROLES]}
                                    for c, s, e in spans]}) + "\n")
                    counts["stage1"] += 1
                rng = random.Random(f"{record['source']}:{record['index']}")
                tables, columns_of, droppable = gold_pruning(record, schema, roles, used_tables, rng)
                row = reading_example(record, question, schema, tables, columns_of, droppable, tokenizer, anchored)
                stage2.write(json.dumps(row) + "\n")
                counts["stage2"] += 1
                counts["values"] += len(row["values"]) + row["missing_values"]
                counts["values_found"] += len(row["values"])
    print(f"stage1 {counts['stage1']} / stage2 {counts['stage2']} examples -> {args.out_dir}; "
          f"literal values found {counts['values_found']}/{counts['values']}")


if __name__ == "__main__":
    main()
