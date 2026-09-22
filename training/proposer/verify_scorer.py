"""Repeatable equivalence check: KV-cache score_sqls vs the naive full-forward reference.

Guards the e142d3a optimization: varied SQL lengths, reversed candidate order (cache
contamination shows up as order-dependent scores), and a tolerance assertion. Model-bound,
so it is a script rather than a hermetic-suite test; run it after any scorer change:

    python -m training.proposer.verify_scorer --adapter training/proposer/data/experiments/d4
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TABLES = [{"name": "pets", "columns": ["pet_id", "pet_type", "weight", "born"],
           "rows": [[1, "cat", 12.0, "2019-01-01"], [2, "dog", 9.5, "2020-05-02"],
                    [3, "dog", 22.1, "2018-03-03"]]}]
QUESTION = "How many dogs weigh more than ten?"
SQLS = [
    'SELECT COUNT(*) FROM "pets"',                                        # short
    'SELECT "pets"."pet_type", AVG("pets"."weight") FROM "pets" '
    'GROUP BY "pets"."pet_type" ORDER BY AVG("pets"."weight") DESC',      # long
    'SELECT MAX("pets"."weight") FROM "pets" WHERE "pets"."pet_type" = \'dog\'',
    'SELECT "pets"."born" FROM "pets" WHERE "pets"."weight" > 10 '
    'AND "pets"."pet_type" = \'dog\' ORDER BY "pets"."born"',
    'SELECT 1',                                                           # tiny
]


def naive_reference(proposer, tables, question, sqls):
    import torch

    from training.proposer.serialize import sample_column_values, schema_prompt

    values = (sample_column_values(tables)
              if getattr(proposer, "include_values", False) else None)
    prompt_ids = proposer.tokenizer(schema_prompt(tables, question, values),
                                    add_special_tokens=False).input_ids
    out = []
    with torch.no_grad():
        for sql in sqls:
            target = proposer.tokenizer(sql, add_special_tokens=False).input_ids
            logits = proposer.model(input_ids=torch.tensor([prompt_ids + target])).logits[0]
            logprobs = torch.log_softmax(
                logits[len(prompt_ids) - 1:len(prompt_ids) - 1 + len(target)], dim=-1)
            out.append((float(logprobs[range(len(target)), target].sum()), len(target)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--values", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.01)
    args = ap.parse_args()

    from training.proposer.propose import Proposer

    proposer = Proposer.load(args.adapter)
    proposer.include_values = args.values
    reference = naive_reference(proposer, TABLES, QUESTION, SQLS)
    fast = proposer.score_sqls(TABLES, QUESTION, SQLS)
    reversed_fast = list(reversed(proposer.score_sqls(TABLES, QUESTION, SQLS[::-1])))
    repeated = proposer.score_sqls(TABLES, QUESTION, SQLS)
    failures = 0
    for sql, ref, out, rev, rep in zip(SQLS, reference, fast, reversed_fast, repeated):
        checks = {"vs-naive": ref, "order-reversed": rev, "repeat": rep}
        for name, other in checks.items():
            if other[1] != out[1] or abs(other[0] - out[0]) > args.tolerance:
                failures += 1
                print(f"FAIL {name}: {sql[:50]}  {out} != {other}")
    if failures:
        sys.exit(f"{failures} scorer equivalence failures")
    print(f"scorer equivalent and order-invariant on {len(SQLS)} SQLs "
          f"(tolerance {args.tolerance})")


if __name__ == "__main__":
    main()
