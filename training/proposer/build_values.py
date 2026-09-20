"""Build the per-db sampled-values sidecar for value-linked proposer training (d4).

Sampling goes through the ONE rule in serialize.sample_column_values over the same capped
loader the evaluator uses, so training prompts see the value distribution inference sees.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spider.probe.evalutil import load_capped
from training.proposer.serialize import sample_column_values


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbs", default=os.path.join(ROOT, "spider", "data", "dbs"))
    ap.add_argument("--cap", type=int, default=5000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sidecar = {}
    names = sorted(name for name in os.listdir(args.dbs) if name.endswith(".sqlite"))
    for i, name in enumerate(names):
        db_id = name[:-len(".sqlite")]
        capped = load_capped(os.path.join(args.dbs, name), cap=args.cap)
        sidecar[db_id] = sample_column_values(list(capped.values()))
        if (i + 1) % 40 == 0:
            print(f"  {i + 1}/{len(names)} dbs", flush=True)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(sidecar, handle)
    print(f"wrote {args.out} ({len(sidecar)} dbs)")


if __name__ == "__main__":
    main()
