"""Check the proposer's batched likelihood scorer against a naive full-forward reference.

engine/sql_proposer.py scores every pooled query in padded batches that share one encoding of the
prompt. This script recomputes each likelihood with one plain forward pass over prompt + query and
checks three properties: the values agree (within --tolerance), a batch gives exactly the values
the same queries get one at a time (batching changes cost, not values), and reversing the order
changes nothing. Model-bound, so it is a script rather than a hermetic test; run it after any
scorer change:

    python -m training.proposer.verify_scorer [--adapter engine/data/sql_proposer]
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TABLES = [{"name": "pets", "columns": ["pet_id", "pet_type", "weight", "born"]}]
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


def naive_reference(proposer, prompt, sqls):
    import torch

    prompt_ids = proposer.tokenizer(prompt, add_special_tokens=False).input_ids
    out = []
    with torch.inference_mode():
        for sql in sqls:
            target = proposer.tokenizer(sql, add_special_tokens=False).input_ids
            ids = torch.tensor([prompt_ids + target], device=proposer.device)
            logits = proposer.model(input_ids=ids).logits[0]
            logprobs = torch.log_softmax(
                logits[len(prompt_ids) - 1:len(prompt_ids) - 1 + len(target)], dim=-1)
            out.append((float(logprobs[range(len(target)), target].sum()), len(target)))
    return out


def main():
    from engine.config import DATA_DIR, DEVICE
    from engine.sql_prompt import schema_prompt
    from engine.sql_proposer import SQLProposer
    from engine.sql_rank import SQLArbiter

    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=str(DATA_DIR / "sql_proposer"))
    ap.add_argument("--tolerance", type=float, default=0.01)
    args = ap.parse_args()

    arbiter = SQLArbiter.load(DATA_DIR / "sql_arbiter.json")
    proposer = SQLProposer.load(args.adapter, beams=arbiter.proposer_beams,
                                max_new_tokens=arbiter.proposer_max_new_tokens, device=DEVICE)
    prompt = schema_prompt(TABLES, QUESTION)
    reference = naive_reference(proposer, prompt, SQLS)
    batched = proposer._score(prompt, SQLS)
    singles = [proposer._score(prompt, [sql])[0] for sql in SQLS]
    reversed_batch = list(reversed(proposer._score(prompt, SQLS[::-1])))
    failures = 0
    for sql, ref, out, single, rev in zip(SQLS, reference, batched, singles, reversed_batch):
        if ref[1] != out[1] or abs(ref[0] - out[0]) > args.tolerance:
            failures += 1
            print(f"FAIL vs-naive: {sql[:50]}  {out} != {ref}")
        for name, other in (("one-at-a-time", single), ("order-reversed", rev)):
            if other != out:
                failures += 1
                print(f"FAIL {name}: {sql[:50]}  {out} != {other}")
    if failures:
        sys.exit(f"{failures} scorer equivalence failures")
    worst = max(abs(ref[0] - out[0]) for ref, out in zip(reference, batched))
    print(f"batched scorer matches one-at-a-time exactly, is order-invariant, and is within "
          f"{worst:.2e} of the naive reference on {len(SQLS)} SQLs (tolerance {args.tolerance})")


if __name__ == "__main__":
    main()
