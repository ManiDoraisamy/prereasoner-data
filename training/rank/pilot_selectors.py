"""Pilot selector replay over cached proposer-inclusive pools (Step 3 of the plan).

Selectors use ONLY serving-available signals (order, scores, likelihoods, endorsements,
execution success); strict labels score outcomes and fit S2 on FIT databases only.
Reported per validation database against BOTH required baselines: the frozen d2
proposer_first policy replay and the deterministic top-1, on identical pools.

Experiment script with expiry: it either funds the full relabel (then its winning
selector graduates into the rank pipeline) or records a bounded negative result.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SEED = 7
ALPHA_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)


def load_pools(paths):
    """Merge candidates across pool files by (db_id, idx, sql); keep source tags."""
    examples = {}
    for path in paths:
        source = os.path.basename(path).replace("pools_", "").replace(".jsonl", "")
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            if "idx" not in record or not record.get("candidates"):
                continue
            key = (record["db_id"], record["idx"])
            example = examples.setdefault(key, {"db_id": record["db_id"], "by_sql": {}})
            for rank, candidate in enumerate(record["candidates"]):
                if "error" in candidate:
                    continue  # unexecutable: never selectable
                entry = example["by_sql"].setdefault(candidate["sql"], {
                    "sql": candidate["sql"], "strict": bool(candidate.get("strict")),
                    "sources": set(), "ranks": {}, "features": {}, "evidence": set(),
                })
                entry["sources"].add(source)
                entry["ranks"][source] = rank
                entry["features"].update(candidate.get("features") or {})
                entry["evidence"].update(candidate.get("evidence") or ())
    pools = []
    for (db_id, idx), example in sorted(examples.items()):
        candidates = list(example["by_sql"].values())
        if candidates:
            pools.append({"db_id": db_id, "idx": idx, "candidates": candidates})
    return pools


def is_proposal(entry):
    return any(tag.startswith("proposer:beam") or tag == "proposer:greedy"
               for tag in entry["evidence"])


def deterministic_top(candidates):
    ranked = [c for c in candidates if not is_proposal(c)]
    ranked.sort(key=lambda c: min(c["ranks"].values()))
    return ranked[0] if ranked else candidates[0]


def policy_first_novel(candidates):
    """Frozen proposer_first replay: first novel proposal, else deterministic top."""
    proposals = [c for c in candidates if is_proposal(c) and "proposer:endorsed" not in c["evidence"]]
    proposals.sort(key=lambda c: min(c["ranks"].values()))
    return proposals[0] if proposals else deterministic_top(candidates)


def vector(entry, pool_size):
    features = entry["features"]
    logprob = features.get("proposer:scored_logprob", features.get("proposer:logprob", -200.0))
    tokens = max(features.get("proposer:scored_tokens", features.get("proposer:tokens", 1.0)), 1.0)
    return [
        logprob, tokens, logprob / tokens,
        float(features.get("base", 0.0)),
        float(min(entry["ranks"].values())),
        float(len(entry["sources"])),
        1.0 if is_proposal(entry) else 0.0,
        1.0 if "proposer:endorsed" in entry["evidence"] else 0.0,
        float(pool_size),
    ]


def evaluate(pools, select):
    by_db = collections.defaultdict(lambda: [0, 0])
    for pool in pools:
        chosen = select(pool["candidates"])
        by_db[pool["db_id"]][0] += bool(chosen and chosen["strict"])
        by_db[pool["db_id"]][1] += 1
    total = [sum(v[0] for v in by_db.values()), sum(v[1] for v in by_db.values())]
    return total, dict(by_db)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", nargs="+", required=True)
    ap.add_argument("--split", default="training/rank/data/experiments/pilot/split.json")
    ap.add_argument("--out", default="training/rank/data/experiments/pilot/report.json")
    args = ap.parse_args()

    split = json.load(open(args.split, encoding="utf-8"))
    pools = load_pools(args.pools)
    fit = [p for p in pools if p["db_id"] in set(split["pilot_fit_dbs"])]
    val = [p for p in pools if p["db_id"] in set(split["pilot_val_dbs"])]
    print(f"pools: {len(fit)} fit / {len(val)} val")

    report = {"n_fit": len(fit), "n_val": len(val), "selectors": {}}

    def record(name, select):
        total, by_db = evaluate(val, select)
        report["selectors"][name] = {
            "val_strict": total[0], "val_n": total[1],
            "per_db": {db: {"strict": v[0], "n": v[1]} for db, v in sorted(by_db.items())},
        }
        print(f"{name:24s} val {total[0]}/{total[1]} ({round(100 * total[0] / max(total[1], 1), 1)}%)")

    record("oracle-ceiling", lambda cs: next((c for c in cs if c["strict"]), None))
    record("deterministic-top", deterministic_top)
    record("policy-first-novel", policy_first_novel)

    # S1: length-normalized likelihood over ALL candidates; alpha chosen on FIT only.
    def s1(alpha):
        def select(candidates):
            def score(entry):
                features = entry["features"]
                logprob = features.get("proposer:scored_logprob",
                                       features.get("proposer:logprob", -1e9))
                tokens = max(features.get("proposer:scored_tokens",
                                          features.get("proposer:tokens", 1.0)), 1.0)
                return logprob / (tokens ** alpha)
            return max(candidates, key=lambda c: (score(c), -min(c["ranks"].values())))
        return select
    alpha = max(ALPHA_GRID, key=lambda a: evaluate(fit, s1(a))[0][0])
    report["s1_alpha"] = alpha
    record(f"S1-likelihood(a={alpha})", s1(alpha))

    # S2: logistic combination fit on FIT candidates only.
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    fit_x, fit_y = [], []
    for pool in fit:
        for entry in pool["candidates"]:
            fit_x.append(vector(entry, len(pool["candidates"])))
            fit_y.append(int(entry["strict"]))
    scaler = StandardScaler().fit(fit_x)
    model = LogisticRegression(random_state=SEED, max_iter=1000).fit(
        scaler.transform(fit_x), fit_y)

    def s2(candidates):
        rows = scaler.transform([vector(c, len(candidates)) for c in candidates])
        scores = model.decision_function(rows)
        best = max(range(len(candidates)),
                   key=lambda i: (scores[i], -min(candidates[i]["ranks"].values())))
        return candidates[best]
    record("S2-logistic", s2)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
