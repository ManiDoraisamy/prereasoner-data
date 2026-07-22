#!/usr/bin/env python3
"""
runtime21 assignment v2 — fixes every verify-assignment21 blocker:
  * TYPE targets = PER-INSTANCE schema.org props from Postgres capped.entity (de-densed avg ~1.4-12, not the 61-dim block).
  * BASIS EXTENSION (option A): 5 Wikidata bio dims (taxonRank/parentTaxon/taxonName/habitat/foodSource) so taxa get a
    real signature. Basis = schema.org(bridged) + these 5.
  * COMMON COLUMNS classified 4-way: type / property(->1 dim + struct) / value_channel(name/title/desc -> is_str only,
    routes to resolution) / meaningless(all-0 abstain). Fine subtypes -> coarse family (value channel does the rest).
  * struct dims revived by real property-VALUE rows (dates/numbers).

  Stage 1 of the schema.org-property pipeline (see training/props/pipeline.md).
  in:  training/props/bridge_prop.csv, training/props/data/columns.csv, Postgres capped.entity (WORLD_PG_PASSWORD)
  out: training/props/data/{assignment21.csv, inference21.csv, alloc21_dims.json, assignment21_report.json}
"""
import csv, hashlib, json, os, re, psycopg2
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data"); os.makedirs(DATA, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(HERE))                  # repo root (training/props -> training -> repo)
ENGINE_DATA = os.environ.get("PREREASONER_ENGINE_DATA", os.path.join(REPO, "engine", "data"))
STRUCT = ["is_str", "is_num", "num_frac", "is_time"]   # dropped currency/is_key (0 positives on this all-string corpus)
BIO = {"P105": "taxonRank", "P171": "parentTaxon", "P225": "taxonName"}   # dropped near-dead habitat/foodSource
PERSON_MODAL = {"birthDate", "gender", "nationality", "hasOccupation", "affiliation"}  # capped has no human leaf; coarse-only per design
NUMRE = re.compile(r"^[\s$£€]*-?[\d.,]+%?$")
DATERE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b")  # FULL date only (not bare 4-digit)
PER_TYPE, MIN_SUPPORT = 250, 25
def short(u): return u.rstrip("/").split("/")[-1]
def split_of(k): return "test" if int(hashlib.sha1(k.encode()).hexdigest(), 16) % 5 == 0 else "train"
def clean(v):
    v = " ".join(str(v).split()); return v if v and len(v) <= 60 and any(c.isalnum() for c in v) else None
def struct_of(tok):
    if NUMRE.match(tok):
        d = ["is_num"] + (["num_frac"] if "." in tok else []) + (["currency"] if any(c in tok for c in "$£€") else [])
        return d
    if DATERE.search(tok): return ["is_str", "is_time"]
    return ["is_str"]

# capped type_qid -> coarse family (fine subtypes collapse to family; value channel resolves fine)
TYPES = {"Q79007": "place", "Q3914": "place", "Q123705": "place", "Q16917": "place", "Q3918": "place",
         "Q11707": "place", "Q515": "place", "Q56061": "place",   # bank Q22687 -> org (below): it's an organization, not place
         "Q11424": "film", "Q215380": "music", "Q7366": "music", "Q737498": "publication",   # SPLIT creativework -> film/music/publication (each a distinct joinable Wikidata type)
         "Q3231690": "product", "Q16521": "organism",   # dropped Q726 horse (animal, not taxon -> split organism)
         "Q7278": "org", "Q4830453": "org", "Q22687": "org",   # bank=org (audit fix); no person here — capped has no human leaf, injected below
         "Q7397": "software"}   # software (Q7397, 13958 instances) -> its own family (distinctive props: operatingSystem/softwareVersion/author/runtimePlatform/programmingLanguage)
# common columns -> (kind, target-spec). place/person/etc = TYPE(family modal); prop:X = PROPERTY; vc = value_channel; '' = meaningless
COMMON = {
    "city": ("type", "place"), "country": ("type", "place"), "country_region": ("type", "place"),
    "province_state": ("type", "place"), "region": ("type", "place"), "province": ("type", "place"),
    "state": ("type", "place"), "county": ("type", "place"), "place": ("type", "place"), "area": ("type", "place"),
    "species": ("type", "organism"), "genus": ("type", "organism"), "family": ("type", "organism"),
    "scientificname": ("type", "organism"), "player": ("type", "person"), "first_name": ("type", "person"),
    "artist": ("type", "person"), "team": ("type", "org"), "teamname": ("type", "org"), "company": ("type", "org"),
    "league": ("type", "org"),
    "address": ("property", "address"), "location": ("property", "address"), "date": ("property", "startDate"),
    "start": ("property", "startDate"), "email": ("property", "email"),
    "name": ("vc", ""), "title": ("vc", ""), "description": ("vc", ""), "comment": ("vc", ""), "text": ("vc", ""),
    "summary": ("vc", ""), "label": ("vc", ""), "tweet": ("vc", ""), "sentence": ("vc", ""),
}
MEANINGLESS_DEFAULT = True  # any common col not in COMMON -> meaningless

def main():
    P2S = {short(r["wd"]): short(r["s"]) for r in csv.DictReader(open(os.path.join(HERE, "bridge_prop.csv")))}
    P2S.update(BIO)
    cn = psycopg2.connect(host=os.environ.get("WORLD_PG_HOST", "34.123.19.176"), dbname="world", user="postgres",
                          password=os.environ["WORLD_PG_PASSWORD"], connect_timeout=25)
    cur = cn.cursor()

    rows = []
    def add(source, token, category, kind, dims, sp):
        rows.append({"source": source, "token": token, "category": category, "kind": kind, "target": set(dims), "split": sp})

    # (A) per-instance type entities from Postgres
    fam_props = defaultdict(Counter); fam_n = Counter()
    for q, fam in TYPES.items():
        cur.execute("""SELECT e.label, e.properties FROM capped.entity e JOIN capped.entity_type et ON et.entity_qid=e.qid
                       WHERE et.type_qid=%s AND e.label IS NOT NULL LIMIT %s""", (q, PER_TYPE))
        got = cur.fetchall()
        for label, props in got:
            lbl = clean(label)
            if not lbl or not isinstance(props, dict): continue
            sig = {P2S[p] for p in props if p in P2S}
            fam_n[fam] += 1
            for s in sig: fam_props[fam][s] += 1
            add(f"type:{fam}:{q}", lbl, "cell_value", "type", sig | set(struct_of(lbl)), split_of("PG|" + lbl))
    cn.close()
    # PERSON: not in capped -> inject bare Wikidata person labels with a DE-DENSED Person modal (birthDate/gender/...)
    wpath = os.path.join(DATA, "wikidata_instances.jsonl")
    if os.path.exists(wpath):
        for l in open(wpath, encoding="utf-8"):
            e = json.loads(l)
            if e.get("schema_type") == "Person":
                lbl = clean(e["name"])
                if lbl:
                    fam_n["person"] += 1
                    add(f"type:person:{e['type']}", lbl, "cell_value", "type", PERSON_MODAL | set(struct_of(lbl)), split_of("PG|" + lbl))
    # family modal signature (props on >=35% of family instances) — for common TYPE columns
    fam_modal = {f: {p for p, c in fam_props[f].items() if c >= 0.35 * max(fam_n[f], 1)} for f in fam_props}
    fam_modal["person"] = set(PERSON_MODAL)

    # (B) common columns
    freq = Counter(); vals = defaultdict(list)
    for r in csv.DictReader(open(os.path.join(DATA, "columns.csv"), encoding="utf-8")):
        h = r["name"].strip().lower(); freq[h] += int(r["n_columns"])
        for v in r["sample_values"].split(";"):
            cv = clean(v)
            if cv: vals[h].append(cv)
    common = [(h, c) for h, c in freq.most_common(60) if len(vals[h]) >= 4]
    col_assign = {}
    for h, c in common:
        spec = COMMON.get(h)
        if spec is None:
            kind, tgt = "meaningless", set()
        elif spec[0] == "type":
            kind, tgt = "type", set(fam_modal.get(spec[1], set()))
        elif spec[0] == "property":
            kind, tgt = "property", {spec[1]}
        elif spec[0] == "vc":
            kind, tgt = "value_channel", {"is_str"}
        else:
            kind, tgt = "meaningless", set()
        col_assign[h] = {"kind": kind, "n_target": len(tgt), "spec": spec}
        sp = split_of("COL|" + h)
        add(f"col:{h}", h, "column_name", kind, tgt, sp)
        for v in vals[h][:8]:
            vd = (tgt | set(struct_of(v))) if kind in ("type", "property") else ({"is_str"} if kind == "value_channel" else set())
            add(f"col:{h}", v, "cell_value", kind, vd, sp)

    # dims = struct + props with >= MIN_SUPPORT positives in TRAIN + the 5 bio dims (always kept)
    propcount = Counter(d for r in rows if r["split"] == "train" for d in r["target"] if d not in STRUCT)
    PROPS = sorted(set([p for p, c in propcount.items() if c >= MIN_SUPPORT]) | set(BIO.values()))
    DIMS = STRUCT + PROPS
    json.dump({"n_dims": len(DIMS), "struct": STRUCT, "properties": PROPS, "bio_ext": list(BIO.values())},
              open(os.path.join(DATA, "alloc21_dims.json"), "w"), indent=1)

    # drop contradictions + value-disjoint test
    sigby = defaultdict(set)
    for r in rows: sigby[(r["token"].lower(), r["category"])].add(frozenset(d for d in r["target"] if d in DIMS))
    bad = {k for k, v in sigby.items() if len(v) > 1}
    tr_tokens = {r["token"].lower() for r in rows if r["split"] == "train"}

    def write(path, want, blankpass):
        seen = set(); kc = Counter()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["Source", "Token", "Category", "kind"] + DIMS + (["PASS"] if blankpass else []))
            for r in rows:
                if r["split"] != want or (r["token"].lower(), r["category"]) in bad: continue
                if want == "test" and r["token"].lower() in tr_tokens: continue
                k = (r["token"], r["category"], r["source"])
                if k in seen: continue
                seen.add(k); kc[r["kind"]] += 1
                w.writerow([r["source"], r["token"], r["category"], r["kind"]] + [1 if d in r["target"] else 0 for d in DIMS] + ([""] if blankpass else []))
        return sum(kc.values()), kc
    ntr, kinds = write(os.path.join(DATA, "assignment21.csv"), "train", False)   # kinds counted from WRITTEN rows (no drift)
    nte, _ = write(os.path.join(DATA, "inference21.csv"), "test", True)
    report = {"n_dims": len(DIMS), "n_props": len(PROPS), "bio_ext": list(BIO.values()),
              "assignment_rows": ntr, "inference_rows": nte, "kinds_train": dict(kinds),
              "contradictions_dropped": len(bad),
              "family_modal_sizes": {f: len(fam_modal[f]) for f in fam_modal},
              "family_n_instances": dict(fam_n),
              "common_col_assignment": {h: {"kind": a["kind"], "n_target": a["n_target"]} for h, a in col_assign.items()},
              "avg_type_target": round(sum(len([d for d in r["target"] if d in PROPS]) for r in rows if r["kind"] == "type" and r["category"] == "cell_value") / max(1, sum(1 for r in rows if r["kind"] == "type" and r["category"] == "cell_value")), 1)}
    json.dump(report, open(os.path.join(DATA, "assignment21_report.json"), "w"), indent=1)
    print(json.dumps(report, indent=1)[:1600])
    print("\nfamily modal signatures:")
    for f in sorted(fam_modal): print(f"  {f:12s} ({fam_n[f]:4d} inst) -> {sorted(fam_modal[f])[:10]}")
    print("\nwrote assignment21.csv, inference21.csv, alloc21_dims.json, assignment21_report.json")

if __name__ == "__main__":
    main()
