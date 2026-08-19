#!/usr/bin/env python3
"""build_families.py — the FAMILY-DECODE table for the superposition-decode router (engine/data/families.json).

The property model reads 67 schema.org property dims per column. The engine decodes the FAMILY
(place/person/org/film/music/publication/product/organism/software) by column-consensus over that firing. This
computes, per family: (1) its DISTINCTIVE properties (fire >=0.30 in-family AND >=3x out-family, from the corpus
TARGETS — the same rule as the column_consensus router), and (2) the knowledgebase join target(s) via
type_table_map.csv. `geo=True` families (place) route through the existing bge geo resolver (city/country/state);
others resolve cells to their family table's qids. Written to BOTH the DATA dir and engine/data.

  Stage 4 of the schema.org-property pipeline (see training/props/pipeline.md).
  in:  training/props/data/{alloc.json, assignment.csv, type_table_map.csv}  (Stage 2 corpus + join map)
  out: training/props/data/families.json + engine/data/families.json (staged)   (KB_PG_PASSWORD not needed)
"""
import csv, json, os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data"); os.makedirs(DATA, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(HERE))                  # repo root (training/props -> training -> repo)
ENGINE_DATA = os.environ.get("PREREASONER_ENGINE_DATA", os.path.join(REPO, "engine", "data"))

# corpus type-label (snake header) -> coarse family. Mirrors build_assignment21_v2.TYPES + the pg_per_instance types.
TYPE_FAM = {
    "street": "place", "school": "place", "neighborhood": "place", "hospital": "place", "university": "place",
    "city": "place", "administrative_territorial_entity": "place", "geographical_feature": "place",
    "political_party": "org", "bank": "org",
    "film": "film", "musical_group": "music", "song": "music", "academic_journal": "publication",
    "car_model": "product", "software": "software", "website": "product",
    "taxon": "organism", "horse": "organism", "person": "person",
}
# family -> the schema_type names in type_table_map.csv that belong to it (for the join-table adapter).
FAM_SCHEMATYPES = {
    "place": ["City", "Country", "AdministrativeArea", "Hospital", "School", "CollegeOrUniversity",
              "LandmarksOrHistoricalBuildings", "Place"],
    "person": ["Person"],
    "org": ["Organization", "Corporation", "NGO", "PoliticalParty", "BankOrCreditUnion"],
    "film": ["Movie"],
    "music": ["MusicGroup", "MusicAlbum", "MusicComposition", "MusicRecording"],
    "publication": ["Periodical", "Book", "CreativeWork"],
    "product": ["Product", "Vehicle", "WebSite"],
    "organism": ["Taxon"],
    "software": ["SoftwareApplication"],   # software is its own family (moved out of product)
}
GEO = {"place"}                       # place routes through the existing bge geo resolver (city/country/state)
STRUCT = {"is_str", "is_num", "num_frac", "is_time", "is_bool", "is_enum", "is_key", "is_ref", "currency"}


def main():
    alloc = json.load(open(os.path.join(DATA, "alloc.json")))                 # the PROPERTY alloc (nc=86), NOT alloc20.json (taxonomy)
    PROPS = [d["name"] for d in alloc["dims"] if d["family"] == "taxonomy"]   # the 67 property dims
    assert len(PROPS) > 40 and "GeoCoordinates" in PROPS, f"expected property alloc, got {PROPS[:5]}"

    # COLUMN-LEVEL firing per family (the router profiles a COLUMN = mean over its cells, so distinctive props are
    # computed the same way — matches the column_consensus router). Group cell rows by column (Source+Example),
    # mean each prop over the column's cells, then per family average over its columns.
    bycol = defaultdict(lambda: defaultdict(list))     # (fam, Source, Example) -> prop -> [0/1 over cells]
    for r in csv.DictReader(open(os.path.join(DATA, "assignment.csv"), encoding="utf-8")):
        if r["Category"] != "cell_value":
            continue
        src = r.get("Source", "")
        if not src.startswith("type:"):
            continue
        fam = TYPE_FAM.get(src.split(":")[1])
        if not fam:
            continue
        key = (fam, src, r.get("Example", ""))
        for p in PROPS:
            bycol[key][p].append(1 if r.get(p) == "1" else 0)
    cols_byfam = defaultdict(list)                      # fam -> [ {prop: mean firing over the column's cells} ]
    for (fam, _s, _e), d in bycol.items():
        cols_byfam[fam].append({p: (sum(v) / len(v) if v else 0.0) for p, v in d.items()})

    FAMILIES = sorted(cols_byfam)
    fam_rate = {F: {p: (sum(c[p] for c in cols_byfam[F]) / max(len(cols_byfam[F]), 1)) for p in PROPS}
                for F in FAMILIES}                     # per-family mean column firing per prop
    distinctive = {}
    for F in FAMILIES:
        others = [f2 for f2 in FAMILIES if f2 != F]
        dp = []
        for p in PROPS:
            rin = fam_rate[F][p]
            rout = sum(fam_rate[f2][p] for f2 in others) / max(len(others), 1)   # macro-avg over other families
            if rin >= 0.15 and rin >= 3 * rout:        # distinctive: fires much more on THIS family's columns
                dp.append((p, round(rin, 2), round(rout, 2)))
        distinctive[F] = sorted(dp, key=lambda x: -x[1])
    byfam = cols_byfam                                 # for the n= print below

    # family -> join tables from type_table_map.csv
    t2 = {r["schema_type"]: (r["wikidata_qid"], r["world_table"]) for r in
          csv.DictReader(open(os.path.join(DATA, "type_table_map.csv"), encoding="utf-8"))}
    families = {}
    for F in FAMILIES:
        tables = [{"schema_type": st, "qid": t2[st][0], "table": t2[st][1]}
                  for st in FAM_SCHEMATYPES.get(F, []) if st in t2]
        families[F] = {"distinctive": [p for p, _i, _o in distinctive[F]], "geo": F in GEO,
                       "tables": tables, "n_train_cells": len(byfam[F])}

    out = {"families": families, "props": PROPS,
           "note": "superposition-decode: family = argmax mean-firing over its distinctive props; place=geo path"}
    for d in (DATA, ENGINE_DATA):
        os.makedirs(d, exist_ok=True)
        json.dump(out, open(os.path.join(d, "families.json"), "w", encoding="utf-8"), indent=1)

    print(f"families ({len(FAMILIES)}): " + ", ".join(FAMILIES))
    for F in FAMILIES:
        print(f"  {F:11s} geo={F in GEO!s:5s} n={len(byfam[F]):4d}  distinctive({len(distinctive[F])}): "
              f"{[p for p,_i,_o in distinctive[F][:8]]}  tables={[t['table'] for t in families[F]['tables']]}")
    print(f"\nwrote families.json -> {DATA} + {ENGINE_DATA} (staged)")


if __name__ == "__main__":
    main()
