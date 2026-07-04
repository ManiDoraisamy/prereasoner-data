"""
gen20 — emit the TAXONOMY anchoring corpus as table-graphs (gen9 units format), so the unified RelBlock anchors
the taxonomy node-dims with relational (same_col) edges — NOT just a flat per-token ridge.

REUSES build_review.build_from_mapped (the SAME bge column->leaf mapping that built the taxonomy): each mapped real
corpus column -> ONE graph = a header unit (col=0,row=-1) + its cell-value units (col=0,row=0..), every unit's `fired`
= the struct datatype dims + the taxonomy PATH node-dims (city -> geographical_feature ... city), `sup` =
['struct','taxonomy'] so build_from_units masks exactly those families. Column-disjoint train/test (inherited from
build_from_mapped's split). Intent is NOT emitted here — train_taxonomy reuses the existing sql/join graphs for intent.

  $env:PYTHONUTF8=1; python -m training.corpus.build_corpus
"""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.corpus import build_review as br                             # noqa: E402

OUT = ROOT / "training/data"
DIM_NAMES = br.STRUCT + br.NODE_DIMS + br.INTENT


def fired_of(row):
    return [d for d in DIM_NAMES if row.get(d) == 1]                         # the dims that fire for this token


def graphs_from_tokens(tokens):
    """group token rows (Source, Token, Example, Category, + 0/1 dims) by Example = one column -> one table-graph."""
    by_col = {}
    for r in tokens:
        by_col.setdefault((r["Source"], r["Example"]), []).append(r)
    graphs = []
    for (src, ex), rows in by_col.items():
        hdr = [r for r in rows if r["Category"] == "column_name"]
        vals = [r for r in rows if r["Category"] == "cell_value"]
        if not hdr or len(vals) < 2:                                         # need a header + >=2 values to anchor a column
            continue
        units = [{"text": hdr[0]["Token"], "kind": "colname", "role": "header", "col": 0, "row": -1,
                  "fired": fired_of(hdr[0]), "sup": ["struct", "taxonomy"]}]
        for i, v in enumerate(vals):
            units.append({"text": v["Token"], "kind": "cell", "role": "value", "col": 0, "row": i,
                          "fired": fired_of(v), "sup": ["struct", "taxonomy"]})
        graphs.append({"file": f"{src}:{hashlib.sha1(ex.encode('utf-8')).hexdigest()[:12]}", "units": units})  # deterministic across runs
    return graphs


def main():
    out = br.build_from_mapped()
    if not out:
        raise SystemExit("mapped_columns.json missing — run reconcile_taxonomy first")
    train_tok, test_tok = out
    train, test = graphs_from_tokens(train_tok), graphs_from_tokens(test_tok)
    for name, gs in (("units_train.jsonl", train), ("units_test.jsonl", test)):
        with open(OUT / name, "w", encoding="utf-8") as f:
            for g in gs:
                f.write(json.dumps(g, ensure_ascii=False) + "\n")
    nu_tr = sum(len(g["units"]) for g in train)
    print(f"wrote units_train.jsonl ({len(train)} column-graphs / {nu_tr} units) + "
          f"units_test.jsonl ({len(test)} graphs / {sum(len(g['units']) for g in test)} units)")
    print(f"  fired families: struct({len(br.STRUCT)}) + taxonomy({len(br.NODE_DIMS)}) co-fired down each token's P279 path")


if __name__ == "__main__":
    main()
