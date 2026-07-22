#!/usr/bin/env python3
"""build_from_props.py — the schema.org-PROPERTY re-anchor corpus (runtime21 spike).

A faithful fork of the gen20 build_from_entity corpus builder. The ONLY change: the anchor TARGET per token is the
schema.org PROPERTY signature (from data/pg_per_instance.jsonl `schema_props`, the de-densed per-instance
props validated in build_assignment21_v2) instead of the P279 taxonomy root->leaf path. Everything else is
identical: struct(9) from token kind, intent(10) carried from the gen20 SQL rows, entity-disjoint 80/20 split,
chunk into synthetic columns, emit units_{train,test}.jsonl + alloc.json.

The property dims keep family == "taxonomy" so the anchor/reanchor + fam_report harness runs UNCHANGED — the
"taxonomy" AUC it reports IS the property-discriminability go/no-go for the CPU-only re-anchor. nc = 9 + 67 + 10 = 86.

  Stage 2 of the schema.org-property pipeline (see training/props/pipeline.md).
  in:  training/props/data/{alloc21_dims.json, pg_per_instance.jsonl} (Stages 1 + build_assignment_pg),
       training/props/data/{assignment.csv, inference.csv, alloc.json[.taxbak]} (base gen20 corpus),
       Postgres knowledgebase.{human,taxon} (KB_PG_PASSWORD; person/taxon-bio coverage)
  out: training/props/data/{assignment.csv, inference.csv, alloc.json, units_{train,test}.jsonl}
       (base gen20 assignment/inference/alloc are backed up to *.taxbak on first run)
"""
import os, re, csv, json, random, hashlib, shutil
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data"); os.makedirs(DATA, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(HERE))                  # repo root (training/props -> training -> repo)
ENGINE_DATA = os.environ.get("PREREASONER_ENGINE_DATA", os.path.join(REPO, "engine", "data"))

STRUCT = ["is_str", "is_num", "num_frac", "is_time", "is_bool", "is_enum", "is_key", "is_ref", "currency"]
INTENT = ["intent_agg_count", "intent_agg_sum", "intent_agg_avg", "intent_filter_eq", "intent_filter_gt",
          "intent_filter_lt", "intent_group", "intent_sort_desc", "intent_sort_asc", "intent_limit"]
META = ["Source", "Token", "Example", "Category"]
CATCOLS = ["category_1"]                      # holds the type/family label (display only; not a dim)
MAXVALS = 250
MODAL_FRAC = 0.35                             # a prop is in the header (column_name) target if >= this frac of the type's cells carry it
NUMRE = re.compile(r"^[\s$£€]*-?[\d.,]+%?$")
DATERE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b")


def snake(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def okv(v):
    v = str(v).strip()
    return bool(v) and len(v) <= 80 and any(c.isalnum() for c in v) and not any(ord(c) < 32 or ord(c) == 0xFFFD for c in v)


def struct_dims(tok, is_value):
    """struct target from the token kind — same rules as build_from_entity.trow + build_assignment21_v2.struct_of."""
    if not is_value:
        return {"is_str"}
    if NUMRE.match(tok):
        d = {"is_num"}
        if "." in tok:
            d.add("num_frac")
        if any(c in tok for c in "$£€"):
            d.add("currency")
        return d
    if DATERE.search(tok):
        return {"is_str", "is_time"}
    return {"is_str"}


def main():
    PROPS = list(json.load(open(os.path.join(DATA, "alloc21_dims.json"), encoding="utf-8"))["properties"])  # 67
    PROPSET = set(PROPS)
    DIMS = STRUCT + PROPS + INTENT

    # 1. per-instance property targets from pg_per_instance.jsonl (type, qid, label, schema_props)
    by_type = defaultdict(list)               # (type_label, qid) -> [(label, {props})]
    for line in open(os.path.join(DATA, "pg_per_instance.jsonl"), encoding="utf-8"):
        e = json.loads(line)
        lbl = e.get("label")
        if not lbl or not okv(lbl):
            continue
        sig = {p for p in (e.get("schema_props") or []) if p in PROPSET}
        by_type[(str(e.get("type")), str(e.get("qid")))].append((str(lbl).strip(), sig))

    # 1b. FULL-COVERAGE families: person + taxon-bio, whose distinctive props are DEAD in pg_per_instance
    # (person absent; taxon's bio props uncaptured). Their entity data is already synced in knowledgebase, so
    # pull labels + fire the mapped schema.org props from the table's non-null property columns. Replaces the
    # bio-less pg taxon so the taxonName/Rank/parentTaxon dims (and person nationality/hasOccupation) get positives.
    import psycopg2
    KB = {"human": ("Q5", "person", {"date_of_birth": "birthDate", "occupation": "hasOccupation",
                                      "country_of_citizenship": "nationality", "sex_or_gender": "gender"}),
          "taxon": ("Q16521", "taxon", {"taxon_rank": "taxonRank", "taxon_name": "taxonName",
                                        "parent_taxon": "parentTaxon"})}
    try:
        cn = psycopg2.connect(host=os.environ.get("KB_PG_HOST", "34.123.19.176"), dbname="world", user="postgres",
                              password=os.environ["KB_PG_PASSWORD"], sslmode="require", connect_timeout=25)
        cur = cn.cursor()
        by_type.pop(("taxon", "Q16521"), None)     # drop the bio-less pg taxon -> replace with the bio-carrying rows
        for tbl, (qid, fam, colmap) in KB.items():
            cols = list(colmap)
            cur.execute(f'SELECT name,{",".join(chr(34)+c+chr(34) for c in cols)} FROM knowledgebase."{tbl}" '
                        f'WHERE name IS NOT NULL LIMIT 400')
            for row in cur.fetchall():
                lbl = row[0]
                if not lbl or not okv(lbl):
                    continue
                sig = {colmap[c] for c, v in zip(cols, row[1:])
                       if v not in (None, "", "None") and colmap[c] in PROPSET}
                by_type[(fam, qid)].append((str(lbl).strip(), sig))
        cn.close()
        print(f"added person({sum(1 for k in by_type if k[0]=='person')}) + taxon-bio from knowledgebase")
    except Exception as e:
        print("KB person/taxon pull SKIPPED (set KB_PG_PASSWORD):", str(e)[:80])

    def blank():
        row = {d: 0 for d in DIMS}
        for c in CATCOLS:
            row[c] = ""
        return row

    def trow(token, propset, is_value, fam_label):
        row = blank()
        for s in struct_dims(token, is_value):
            if s in row:
                row[s] = 1
        for p in propset:
            row[p] = 1                        # co-fire the schema.org property dims (family "taxonomy")
        row["category_1"] = fam_label
        return row

    # 2. per type: modal header signature + entity-disjoint 80/20 chunked columns
    rng = random.Random(7)
    train, test = [], []
    for (tlabel, qid), insts in sorted(by_type.items()):
        hdr = snake(tlabel) or "entity"
        # dedup by lowercased label
        seen, uniq = set(), []
        for lbl, sig in insts:
            k = lbl.lower()
            if k not in seen:
                seen.add(k); uniq.append((lbl, sig))
        rng.shuffle(uniq)
        uniq = uniq[:MAXVALS]
        n = len(uniq)
        modal = {p for p, c in Counter(p for _l, s in uniq for p in s).items() if c >= MODAL_FRAC * max(n, 1)}
        cut = int(0.8 * n) if n >= 5 else (n - 1 if n >= 2 else n)
        for split, part in (("tr", uniq[:cut]), ("te", uniq[cut:])):
            dst = train if split == "tr" else test
            for ci in range(0, len(part), 5):
                chunk = part[ci:ci + 5]
                if not chunk:
                    continue
                ex = f"{hdr}: " + "; ".join(l for l, _s in chunk)
                dst.append({"Source": f"type:{hdr}:{qid}", "Token": hdr, "Example": ex, "Category": "column_name",
                            **trow(hdr, modal, False, hdr)})
                for lbl, sig in chunk:
                    dst.append({"Source": f"type:{hdr}:{qid}", "Token": lbl, "Example": ex, "Category": "cell_value",
                                **trow(lbl, sig, True, hdr)})

    # 3. carry the intent SQL_kw/SQL_op rows from the gen20 corpus (projected onto struct+intent; no property)
    def carry_sql(src_path, dst):
        if not os.path.exists(src_path):
            return
        for r in csv.DictReader(open(src_path, encoding="utf-8")):
            if r.get("Category") in ("SQL_kw", "SQL_op"):
                row = blank()
                for s in STRUCT:
                    row[s] = int(r.get(s) or 0)
                for it in INTENT:
                    row[it] = int(r.get(it) or 0)
                dst.append({"Source": r["Source"], "Token": r["Token"], "Example": r["Example"],
                            "Category": r["Category"], **row})
    carry_sql(os.path.join(DATA, "assignment.csv"), train)
    carry_sql(os.path.join(DATA, "inference.csv"), test)

    # 4. clean: value-disjoint test, drop contradictions, drop exact dup rows (identical to build_from_entity)
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
                seen.add(k); out.append(r)
        return out
    train, test = finalize(train), finalize(test)

    # 5. back up the taxonomy corpus, then write the property corpus to the standard reanchor filenames
    for f in ("assignment.csv", "inference.csv", "alloc.json"):
        p = os.path.join(DATA, f)
        if os.path.exists(p) and not os.path.exists(p + ".taxbak"):
            shutil.copy2(p, p + ".taxbak")

    base = META + CATCOLS
    with open(os.path.join(DATA, "assignment.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base + DIMS); w.writeheader()
        for r in train:
            w.writerow({k: r.get(k, "") for k in base + DIMS})
    with open(os.path.join(DATA, "inference.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base + DIMS + ["Accuracy", "R2", "PASS"]); w.writeheader()
        for r in test:
            w.writerow({**{k: r.get(k, "") for k in base}, **{d: "" for d in DIMS}, "Accuracy": "", "R2": "", "PASS": ""})

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
            out.append({"file": "%s:%s" % (src, hashlib.sha1(ex.encode("utf-8")).hexdigest()[:12]), "units": us})
        return out
    for name, toks in (("units_train.jsonl", train), ("units_test.jsonl", test)):
        with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
            for g in graphs(toks):
                f.write(json.dumps(g, ensure_ascii=False) + "\n")

    # 6. alloc.json — struct(9) then PROPS(67, family "taxonomy") then intent(10); warm ids from the base engine alloc
    dims, did = [], 0
    for fam, names in (("struct", STRUCT), ("taxonomy", PROPS), ("intent", INTENT)):
        for nm in names:
            dims.append({"name": nm, "family": fam, "dim_id": did}); did += 1
    base_alloc = json.load(open(os.path.join(DATA, "alloc.json.taxbak"), encoding="utf-8")) \
        if os.path.exists(os.path.join(DATA, "alloc.json.taxbak")) else {"dims": []}
    warm = {d["name"]: d["dim_id"] for d in base_alloc.get("dims", [])}
    json.dump({"n_content": len(dims), "dims": dims,
               "warm_from_alloc": {d["name"]: warm[d["name"]] for d in dims if d["name"] in warm}},
              open(os.path.join(DATA, "alloc.json"), "w", encoding="utf-8"), indent=1)

    ncell = sum(1 for r in train if r["Category"] == "cell_value")
    fires = [sum(1 for p in PROPS if r.get(p) == 1) for r in train if r["Category"] == "cell_value"]
    ppos = Counter(p for r in train if r["Category"] == "cell_value" for p in PROPS if r.get(p) == 1)
    print(f"types: {len(by_type)} | nc = {len(dims)} (struct 9 + props {len(PROPS)} + intent 10)")
    print(f"assignment {len(train)} rows | inference {len(test)} rows | {ncell} train cells | "
          f"prop firing/cell avg={sum(fires)/max(len(fires),1):.1f} max={max(fires) if fires else 0}")
    weak = [p for p in PROPS if ppos[p] < 25]
    print(f"props with <25 train positives ({len(weak)}): {weak}")


if __name__ == "__main__":
    main()
