"""
gen20 — (re)generate alloc.json from the CURRENT taxonomy. alloc = struct + the taxonomy NODE dims + intent,
each with a family ('struct'|'taxonomy'|'intent') for fam_dims_map, warm-start-aligned to alloc_multitask by name. Must be re-run
whenever the taxonomy changes (the node-dim set grows/shrinks), so validate_data's dim-equality check stays green.

  $env:PYTHONUTF8=1; python -m training.taxonomy.build_alloc
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.corpus import build_review as br                             # noqa: E402

OUT = ROOT / "training/data"


def main():
    dims = ([{"name": n, "family": "struct"} for n in br.STRUCT]
            + [{"name": n, "family": "taxonomy"} for n in br.NODE_DIMS]
            + [{"name": n, "family": "intent"} for n in br.INTENT])
    for i, d in enumerate(dims):
        d["dim_id"] = i
    a11 = {d["name"]: d["dim_id"] for d in json.load(open(ROOT / "training/data/alloc_multitask.json"))["dims"]}
    shared = {d["name"]: a11[d["name"]] for d in dims if d["name"] in a11}
    json.dump({"n_content": len(dims), "dims": dims, "warm_from_alloc_multitask": shared},
              open(OUT / "alloc.json", "w"), indent=1)
    print(f"wrote alloc.json: {len(dims)} dims (struct {len(br.STRUCT)} + taxonomy {len(br.NODE_DIMS)} + "
          f"intent {len(br.INTENT)}); {len(shared)} warm-start from alloc_multitask by name")


if __name__ == "__main__":
    main()
