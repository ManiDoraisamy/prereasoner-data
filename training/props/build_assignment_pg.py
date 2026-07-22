#!/usr/bin/env python3
"""
runtime21 — PER-INSTANCE schema.org targets from POSTGRES (capped.entity.properties), the RIGHT source Mani flagged:
the earlier runtimes already synced 557k Wikidata entities-with-properties into Cloud SQL, so we read per-instance
props locally (no WDQS) and map each entity's Wikidata P-ids -> schema.org props via the P1628 bridge. This DE-DENSES
by construction (each entity fires only the props it actually has) and breaks the block-labeling collapse.

This first pass REPORTS per-type target density + bridge coverage (which families are schema.org-representable).

  Upstream input builder for Stage 2 (build_from_props reads pg_per_instance.jsonl). See training/props/pipeline.md.
  in:  training/props/bridge_prop.csv, Postgres capped.entity (WORLD_PG_PASSWORD)
  out: training/props/data/pg_per_instance.jsonl  {type, qid, label, schema_props:[...]}   + density report
"""
import csv, json, os, psycopg2
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data"); os.makedirs(DATA, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(HERE))                  # repo root (training/props -> training -> repo)
ENGINE_DATA = os.environ.get("PREREASONER_ENGINE_DATA", os.path.join(REPO, "engine", "data"))
PER_TYPE = 250
def short(u): return u.rstrip("/").split("/")[-1]

def main():
    P2S = {short(r["wd"]): short(r["s"]) for r in csv.DictReader(open(os.path.join(HERE, "bridge_prop.csv")))}
    cn = psycopg2.connect(host=os.environ.get("WORLD_PG_HOST", "34.123.19.176"), dbname="world", user="postgres",
                          password=os.environ["WORLD_PG_PASSWORD"], connect_timeout=25)
    cur = cn.cursor()
    cur.execute("""SELECT et.type_qid, t.label, count(*) c FROM capped.entity_type et JOIN capped.type t ON t.qid=et.type_qid
                   GROUP BY 1,2 ORDER BY c DESC LIMIT 22""")
    types = cur.fetchall()
    out = open(os.path.join(DATA, "pg_per_instance.jsonl"), "w", encoding="utf-8")
    print(f"{'type':24s} {'n':>4s} {'avg#sig':>7s} {'%nonempty':>9s}  top schema.org props")
    for q, lab, c in types:
        cur.execute("""SELECT e.label, e.properties FROM capped.entity e JOIN capped.entity_type et ON et.entity_qid=e.qid
                       WHERE et.type_qid=%s AND e.label IS NOT NULL LIMIT %s""", (q, PER_TYPE))
        sizes = []; propfreq = Counter(); n = 0
        for label, props in cur.fetchall():
            if not isinstance(props, dict): continue
            sig = sorted({P2S[p] for p in props if p in P2S})
            n += 1; sizes.append(len(sig))
            for s in sig: propfreq[s] += 1
            out.write(json.dumps({"type": lab, "qid": q, "label": str(label), "schema_props": sig}, ensure_ascii=False) + "\n")
        if not sizes: continue
        avg = sum(sizes) / len(sizes); nonempty = sum(1 for s in sizes if s > 0) / len(sizes)
        top = ", ".join(f"{p}({propfreq[p]})" for p, _ in propfreq.most_common(6))
        print(f"  {lab:22s} {n:>4d} {avg:>7.1f} {nonempty:>8.0%}   {top}")
    out.close()
    print("\nwrote data/pg_per_instance.jsonl (per-instance schema.org targets from Postgres)")
    cn.close()

if __name__ == "__main__":
    main()
