#!/usr/bin/env python3
"""
gen20/scripts/build_from_entity.py — the CLEAN gen20 training data: cell-value tokens come straight from
capped.entity (real, correctly-typed Wikidata P31 instances), NOT the noisy bge column->leaf mapping that taught
gen20 to fire `athlete` on any person name.

  - LIVE leaves = taxonomy leaves with >= MIN_INSTANCES real instances in capped.entity. The data-starved
    abstraction-leaves (athlete 5, person 1, bird 11, + the n=0 ones) are DROPPED — the entity table proves they
    have ~no instances, so they can't be anchored.
  - For each live leaf: pull up to MAXVALS distinct labels, entity-disjoint 80/20 split, chunk into synthetic
    columns (header = the leaf label) -> column_name + cell_value rows. struct datatype from the token; the leaf's
    tree path co-fires the named dims (sparse, ~4/token) exactly like gen20.
  - The intent SQL_kw/SQL_op query rows are carried over from gen20 unchanged.
  - anchored node set = the union of the LIVE leaves' tree-path nodes (dead pure-leaves drop out automatically).

  out: training/data/{assignment.csv, inference.csv, taxonomy.csv, alloc.json, nodes.csv} (+ ref csvs)
"""
import os
import re
import csv
import json
import random
import shutil
import hashlib
import psycopg2
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
R19 = os.path.join(ROOT, "training", "data")
R20 = os.path.join(ROOT, "training", "data")
os.makedirs(R20, exist_ok=True)

STRUCT = ["is_str", "is_num", "num_frac", "is_time", "is_bool", "is_enum", "is_key", "is_ref", "currency"]
INTENT = ["intent_agg_count", "intent_agg_sum", "intent_agg_avg", "intent_filter_eq", "intent_filter_gt",
          "intent_filter_lt", "intent_group", "intent_sort_desc", "intent_sort_asc", "intent_limit"]
META = ["Source", "Token", "Example", "Category"]
MIN_INSTANCES = 50          # a leaf needs >= this many real instances to be a trainable dim
MAXVALS = 250               # distinct tokens per leaf (balanced; matches gen20)
NUMRE = re.compile(r"^[\s$£€]*-?[\d.,]+%?$")
# leaf QID -> world tables joined along the path (the geo world-join targets; SERVING config, not a training dim, so
# it never affects assignment/alloc/units — only LEAF_TABLES at route time). Without it router's world_leaves is empty.
WORLD_TABLES = {"Q515": "Cities;Places", "Q6256": "Countries", "Q3957": "Places", "Q124250988": "Places"}


def snake(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def ok(v):
    v = str(v).strip()
    return bool(v) and len(v) <= 80 and not any(ord(c) < 32 or ord(c) == 0xFFFD for c in v)


def main():
    cn = psycopg2.connect(host=os.environ.get("KB_PG_HOST", "localhost"), dbname=os.environ.get("KB_PG_DB", "world"), user=os.environ.get("KB_PG_USER", "postgres"),
                          password=os.environ["KB_PG_PASSWORD"], connect_timeout=30)
    cur = cn.cursor()

    # 1. leaves + tree paths from gen20 taxonomy.csv (the curated single-path, glue-dropped lineage)
    tax = list(csv.DictReader(open(os.path.join(R19, "taxonomy.csv"), encoding="utf-8")))
    ccols = [c for c in tax[0] if c.startswith("category_")]
    leaf_path, leaf_label = {}, {}
    for r in tax:
        if r.get("status") == "rejected":
            continue
        path = []
        for c in ccols:
            s = snake(r[c]) if r[c] else ""
            if s and s not in path:
                path.append(s)
        if path:
            leaf_path[r["qid"]] = path
            leaf_label[r["qid"]] = path[-1]

    # 2. instance counts -> live leaves
    cur.execute("SELECT type_qid, count(*) FROM capped.entity_type GROUP BY 1")
    cnt = dict(cur.fetchall())
    live = {q: p for q, p in leaf_path.items() if cnt.get(q, 0) >= MIN_INSTANCES}
    dropped = sorted(leaf_label[q] for q in leaf_path if q not in live)
    # anchored dims = REAL nodes, TRIMMING single-leaf glue (gen20 rule): keep a node iff it generalizes >=2 live
    # leaves OR it IS a live leaf. The full path still shows in category_1..N; only the 0/1 dims are trimmed.
    _ndoc = Counter(n for q in live for n in live[q])
    _live_leaves = set(leaf_label[q] for q in live)
    NODE_DIMS = sorted({n for n in _ndoc if _ndoc[n] >= 2 or n in _live_leaves})
    DIMS = STRUCT + NODE_DIMS + INTENT
    MAXD = max(len(p) for p in live.values())
    CATCOLS = [f"category_{i+1}" for i in range(MAXD)]

    def blank():
        row = {d: 0 for d in DIMS}
        for c in CATCOLS:
            row[c] = ""
        return row

    def trow(token, path, is_value):
        row = blank()
        if is_value:
            isnum = bool(NUMRE.match(token))
            row["is_num"], row["is_str"] = (1, 0) if isnum else (0, 1)
            row["num_frac"] = 1 if (isnum and "." in token) else 0
        else:
            row["is_str"] = 1
        for i, lab in enumerate(path):
            if i < len(CATCOLS):
                row[CATCOLS[i]] = lab
            if lab in row:                                       # co-fire only the KEPT node dims
                row[lab] = 1
        return row

    # 3. pull capped.entity labels per live leaf -> token rows (entity-disjoint 80/20, chunked into columns)
    rng = random.Random(7)
    train, test = [], []
    for q, path in sorted(live.items()):
        hdr = leaf_label[q]
        cur.execute("SELECT e.label FROM capped.entity e JOIN capped.entity_type et ON et.entity_qid=e.qid "
                    "WHERE et.type_qid=%s AND e.label IS NOT NULL LIMIT 3000", (q,))
        seen, uniq = set(), []
        for (v,) in cur.fetchall():
            k = str(v).strip().lower()
            if k and k not in seen and ok(v):
                seen.add(k)
                uniq.append(str(v).strip())
        rng.shuffle(uniq)
        uniq = uniq[:MAXVALS]
        n = len(uniq)
        cut = int(0.8 * n) if n >= 5 else (n - 1 if n >= 2 else n)
        for split, part in (("tr", uniq[:cut]), ("te", uniq[cut:])):
            dst = train if split == "tr" else test
            for ci in range(0, len(part), 5):
                chunk = part[ci:ci + 5]
                if not chunk:
                    continue
                ex = f"{hdr}: " + "; ".join(chunk)
                dst.append({"Source": q, "Token": hdr, "Example": ex, "Category": "column_name", **trow(hdr, path, False)})
                for v in chunk:
                    dst.append({"Source": q, "Token": v, "Example": ex, "Category": "cell_value", **trow(v, path, True)})

    # 4. carry over the intent SQL_kw/SQL_op rows from gen20 (projected onto the new dim set; no taxonomy)
    def carry_sql(src_path, dst):
        for r in csv.DictReader(open(src_path, encoding="utf-8")):
            if r["Category"] in ("SQL_kw", "SQL_op"):
                row = blank()
                for s in STRUCT:
                    row[s] = int(r.get(s) or 0)
                for it in INTENT:
                    row[it] = int(r.get(it) or 0)
                dst.append({"Source": r["Source"], "Token": r["Token"], "Example": r["Example"],
                            "Category": r["Category"], **row})
    carry_sql(os.path.join(R19, "assignment.csv"), train)
    carry_sql(os.path.join(R19, "inference.csv"), test)

    # 5. clean: value-disjoint test, drop contradictions (one token -> two targets), drop exact dup rows
    def tgt(r):
        return tuple(d for d in DIMS if r.get(d) == 1)
    tr_vals = {r["Token"].lower() for r in train if r["Category"] == "cell_value"}
    test = [r for r in test if not (r["Category"] == "cell_value" and r["Token"].lower() in tr_vals)]
    sig = defaultdict(set)
    for r in train + test:
        if r["Category"] in ("column_name", "cell_value"):
            sig[(r["Token"].lower(), r["Category"])].add(tgt(r))
    bad = {k for k, v in sig.items() if len(v) > 1}

    def finalize(rows):
        seen, out = set(), []
        for r in rows:
            if (r["Token"].lower(), r["Category"]) in bad:
                continue
            k = (r["Source"], r["Token"], r["Example"], r["Category"])
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out
    train, test = finalize(train), finalize(test)

    # 6. write
    base = META + CATCOLS
    with open(os.path.join(R20, "assignment.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base + DIMS); w.writeheader()
        for r in train:
            w.writerow({k: r.get(k, "") for k in base + DIMS})
    with open(os.path.join(R20, "inference.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base + DIMS + ["Accuracy", "R2", "PASS"]); w.writeheader()
        for r in test:
            w.writerow({**{k: r.get(k, "") for k in base}, **{d: "" for d in DIMS}, "Accuracy": "", "R2": "", "PASS": ""})

    # units_train/test.jsonl — the column-graphs the RelBlock trainer (reanchor) consumes (same format as build_corpus)
    def graphs(tokens):
        by = defaultdict(list)
        for r in tokens:
            by[(r["Source"], r["Example"])].append(r)
        out = []
        for (src, ex), rs in by.items():
            h = [r for r in rs if r["Category"] == "column_name"]
            v = [r for r in rs if r["Category"] == "cell_value"]
            if not h or len(v) < 2:
                continue
            def fired(r):
                return [d for d in DIMS if r.get(d) == 1]
            us = [{"text": h[0]["Token"], "kind": "colname", "role": "header", "col": 0, "row": -1,
                   "fired": fired(h[0]), "sup": ["struct", "taxonomy"]}]
            for i, x in enumerate(v):
                us.append({"text": x["Token"], "kind": "cell", "role": "value", "col": 0, "row": i,
                           "fired": fired(x), "sup": ["struct", "taxonomy"]})
            digest = hashlib.sha1(ex.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
            out.append({"file": "%s:%s" % (src, digest), "units": us})
        return out
    for name, toks in (("units_train.jsonl", train), ("units_test.jsonl", test)):
        with open(os.path.join(R20, name), "w", encoding="utf-8") as f:
            for g in graphs(toks):
                f.write(json.dumps(g, ensure_ascii=False) + "\n")

    with open(os.path.join(R20, "taxonomy.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["qid"] + CATCOLS + ["status", "world_tables"]); w.writeheader()
        for q, path in sorted(live.items(), key=lambda x: leaf_label[x[0]]):
            row = {"qid": q, "status": "accepted", "world_tables": WORLD_TABLES.get(q, "")}
            for i, c in enumerate(CATCOLS):
                row[c] = path[i] if i < len(path) else ""
            w.writerow(row)

    dims, did = [], 0
    for fam, names in (("struct", STRUCT), ("taxonomy", NODE_DIMS), ("intent", INTENT)):
        for n in names:
            dims.append({"name": n, "family": fam, "dim_id": did}); did += 1
    a19id = {d["name"]: d["dim_id"] for d in json.load(open(os.path.join(R19, "alloc.json"), encoding="utf-8"))["dims"]}
    json.dump({"n_content": len(dims), "dims": dims,
               "warm_from_alloc": {d["name"]: a19id[d["name"]] for d in dims if d["name"] in a19id}},
              open(os.path.join(R20, "alloc.json"), "w", encoding="utf-8"), indent=1)

    with open(os.path.join(R20, "nodes.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["node", "is_leaf", "instances"])
        leafset = set(leaf_label[q] for q in live)
        for n in NODE_DIMS:
            inst = next((cnt.get(q, 0) for q in live if leaf_label[q] == n), "")
            w.writerow([n, "t" if n in leafset else "f", inst])

    for fn in ("qid.csv", "full-taxonomy.csv", "properties.csv"):
        if os.path.exists(os.path.join(R19, fn)):
            shutil.copy2(os.path.join(R19, fn), os.path.join(R20, fn))

    fires = [sum(1 for n in NODE_DIMS if r.get(n) == 1) for r in train if r["Category"] == "cell_value"]
    print("LIVE leaves: %d of %d  (dropped %d data-starved: %s)" % (len(live), len(leaf_path), len(dropped), ", ".join(dropped)))
    print("anchored node dims: %d  ->  alloc n_content = %d" % (len(NODE_DIMS), len(dims)))
    print("assignment.csv %d rows | inference.csv %d rows | firing/token avg=%.1f max=%d (sparse)"
          % (len(train), len(test), sum(fires) / len(fires), max(fires)))
    cn.close()


if __name__ == "__main__":
    main()
