"""Pilot selector replay over cached proposer-inclusive pools (Step 3 of the plan).

Selectors use ONLY serving-available signals; strict labels score outcomes and fit S2 on
FIT databases only. Contracts enforced by tests: (1) endorsement never removes a
candidate's enumerator origin from the deterministic baseline; (2) likelihoods stay
namespaced per source model — input-file order cannot change any selection; (3) the
denominator is every labeled example, including empty/failed pools.

Experiment script with expiry: it either funds the full relabel (the winning selector
then graduates into the rank pipeline) or records a bounded negative result.
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


def _decode_tag(evidence):
    return any(tag == "proposer:greedy" or tag.startswith("proposer:beam")
               for tag in evidence)


def load_pools(paths):
    """Merge candidates across pool files by (db_id, idx, sql).

    Origin is tracked structurally per source file: an entry is enumerator-origin in a
    file when the search produced it there (ranked features present / no decode tag /
    an endorsement, which by definition decorates an enumerator candidate). Features and
    ranks are kept PER SOURCE — nothing is overwritten across files, so results are
    invariant to input order (sources are keyed by basename, not position)."""
    examples = {}
    presence = collections.defaultdict(set)
    for path in paths:
        source = os.path.basename(path).replace("pools_", "").replace(".jsonl", "")
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            if "idx" not in record:
                continue
            key = (record["db_id"], record["idx"])
            presence[source].add(key)
            example = examples.setdefault(key, {"db_id": record["db_id"], "by_sql": {}})
            for rank, candidate in enumerate(record.get("candidates") or ()):
                evidence = tuple(candidate.get("evidence") or ())
                endorsed = "proposer:endorsed" in evidence
                enumerator_origin = endorsed or not _decode_tag(evidence)
                entry = example["by_sql"].setdefault(candidate["sql"], {
                    "sql": candidate["sql"], "strict": bool(candidate.get("strict")),
                    "sources": {}, "from_enumerator": False, "from_proposer": False,
                    "endorsed": False, "executable": False,
                })
                # Failed candidates stay SELECTABLE (the frozen policy returns their
                # failure — no silent fallback); execution success on any source marks
                # the SQL executable for the separately-named execution-filtered selectors.
                entry["executable"] |= "error" not in candidate
                entry["strict"] |= bool(candidate.get("strict"))
                entry["sources"][source] = {
                    "rank": rank,
                    "score": candidate.get("score"),
                    "features": dict(candidate.get("features") or {}),
                }
                entry["from_enumerator"] |= enumerator_origin
                entry["from_proposer"] |= _decode_tag(evidence)
                entry["endorsed"] |= endorsed
    pools = []
    for (db_id, idx), example in sorted(examples.items()):
        pools.append({"db_id": db_id, "idx": idx,
                      "candidates": list(example["by_sql"].values())})
    return pools, dict(presence)


def _rank_in(entry, source):
    info = entry["sources"].get(source)
    return info["rank"] if info else float("inf")


def _min_rank(entry):
    return min(info["rank"] for info in entry["sources"].values())


def deterministic_top(candidates, source=None):
    """The enumerator's own first candidate; endorsement must never remove membership."""
    ranked = [c for c in candidates if c["from_enumerator"]]
    if not ranked:
        return None
    if source is not None:
        ranked = [c for c in ranked if source in c["sources"]] or ranked
        return min(ranked, key=lambda c: _rank_in(c, source))
    return min(ranked, key=_min_rank)


def policy_first_novel(candidates, source=None):
    """proposer_first replay: first NOVEL proposal (never enumerator-origin), else the
    deterministic top. Over a beam-built source this approximates (not reproduces) the
    frozen greedy policy; the exact replay uses a greedy-built pool file alone."""
    proposals = [c for c in candidates if c["from_proposer"] and not c["from_enumerator"]]
    if source is not None:
        proposals = [c for c in proposals if source in c["sources"]]
        if proposals:
            return min(proposals, key=lambda c: _rank_in(c, source))
        return deterministic_top(candidates, source)
    if proposals:
        return min(proposals, key=_min_rank)
    return deterministic_top(candidates)


def _likelihood(entry, source):
    info = entry["sources"].get(source)
    if not info:
        return None
    features = info["features"]
    logprob = features.get("proposer:scored_logprob", features.get("proposer:logprob"))
    if logprob is None:
        return None
    tokens = max(features.get("proposer:scored_tokens",
                              features.get("proposer:tokens", 1.0)), 1.0)
    return float(logprob), float(tokens)


SOURCES = ("d2beam", "d4greedy")


def vector(entry, pool_size):
    row = []
    for source in SOURCES:
        scored = _likelihood(entry, source)
        if scored is None:
            row.extend([-200.0, 1.0, -200.0, 1.0])   # value, tokens, per-token, MISSING
        else:
            logprob, tokens = scored
            row.extend([logprob, tokens, logprob / tokens, 0.0])
    scores = [info["score"] for info in entry["sources"].values()
              if info["score"] is not None]
    row.extend([
        max(scores) if scores else 0.0,
        float(_min_rank(entry)),
        float(len(entry["sources"])),
        1.0 if entry["from_proposer"] else 0.0,
        1.0 if entry["from_enumerator"] else 0.0,
        1.0 if entry["endorsed"] else 0.0,
        float(pool_size),
    ])
    return row


def evaluate(pools, select, expected_by_db):
    """Denominator = the split manifest's expectation; missing/empty pools count wrong."""
    by_db = {db: [0, n] for db, n in expected_by_db.items()}
    for pool in pools:
        if pool["db_id"] not in by_db:
            continue
        chosen = select(pool["candidates"]) if pool["candidates"] else None
        by_db[pool["db_id"]][0] += bool(chosen and chosen["strict"])
    total = [sum(v[0] for v in by_db.values()), sum(v[1] for v in by_db.values())]
    return total, by_db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", nargs="+", required=True)
    ap.add_argument("--exact-policy-pool", default="",
                    help="d2-GREEDY-built pool file for the faithful frozen-policy replay")
    ap.add_argument("--split", default="training/rank/data/experiments/pilot/split.json")
    ap.add_argument("--out", default="training/rank/data/experiments/pilot/report.json")
    ap.add_argument("--save-arbiter", default="",
                    help="persist the fitted S2 as a pure-linear JSON artifact "
                         "(deterministic serving math, no sklearn at load time)")
    args = ap.parse_args()

    import collections as _c
    split = json.load(open(args.split, encoding="utf-8"))
    train = json.load(open(os.path.join(ROOT, "spider", "data", "train_spider.json"),
                           encoding="utf-8"))
    counts = _c.Counter(e["db_id"] for e in train)
    expected_val = {db: counts[db] for db in split["pilot_val_dbs"]}
    expected_fit = {db: counts[db] for db in split["pilot_fit_dbs"]}

    pools, presence = load_pools(args.pools)
    fit = [p for p in pools if p["db_id"] in expected_fit]
    val = [p for p in pools if p["db_id"] in expected_val]
    # Per-source completion: EVERY expected pilot question must have a record in EVERY
    # source before a funding verdict is valid — merged coverage alone can hide an
    # unfinished source behind a finished one.
    expected_keys = {(e["db_id"], i) for i, e in enumerate(train)
                     if e["db_id"] in expected_fit or e["db_id"] in expected_val}
    completion = {source: {"present": len(keys & expected_keys),
                           "expected": len(expected_keys),
                           "missing": len(expected_keys - keys)}
                  for source, keys in presence.items()}
    funding_verdict_allowed = all(c["missing"] == 0 for c in completion.values())
    report = {"pools": args.pools,
              "completion_by_source": completion,
              "funding_verdict_allowed": funding_verdict_allowed,
              "coverage": {"fit": [len(fit), sum(expected_fit.values())],
                           "val": [len(val), sum(expected_val.values())]},
              "selectors": {}}
    print(f"pools loaded: fit {len(fit)}/{sum(expected_fit.values())}  "
          f"val {len(val)}/{sum(expected_val.values())}")
    for source, c in sorted(completion.items()):
        print(f"  source {source}: {c['present']}/{c['expected']} "
              f"({'COMPLETE' if not c['missing'] else str(c['missing']) + ' MISSING'})")
    if not funding_verdict_allowed:
        print("  NOTE: incomplete source(s) — report is diagnostic only, no funding verdict")

    def record(name, select, description=""):
        total, by_db = evaluate(val, select, expected_val)
        report["selectors"][name] = {
            "val_strict": total[0], "val_n": total[1], "description": description,
            "per_db": {db: {"strict": v[0], "n": v[1]} for db, v in sorted(by_db.items())},
        }
        print(f"{name:28s} val {total[0]}/{total[1]} "
              f"({round(100 * total[0] / max(total[1], 1), 1)}%)")

    record("oracle-ceiling", lambda cs: next((c for c in cs if c["strict"]), None))
    record("deterministic-top", deterministic_top,
           "enumerator's own first candidate (endorsements retained)")
    record("policy-novel(d2beam~)",
           lambda cs: policy_first_novel(cs, "d2beam"),
           "beam-pool approximation of the frozen policy")
    if args.exact_policy_pool:
        exact_pools, exact_presence = load_pools([args.exact_policy_pool])
        # The exact-policy file is part of the funding evidence: its completion gates
        # the verdict exactly like the merged sources do.
        expected_val_keys = {(e["db_id"], i) for i, e in enumerate(train)
                             if e["db_id"] in expected_val}
        for source, keys in exact_presence.items():
            missing = len(expected_val_keys - keys)
            completion[f"exact-policy:{source}"] = {
                "present": len(keys & expected_val_keys),
                "expected": len(expected_val_keys), "missing": missing}
            if missing:
                funding_verdict_allowed = False
        report["completion_by_source"] = completion
        report["funding_verdict_allowed"] = funding_verdict_allowed
        exact = {(p["db_id"], p["idx"]): p for p in exact_pools}

        def frozen(candidates, _exact=exact):
            return None  # placeholder; replaced below by pool-keyed replay
        total = [0, sum(expected_val.values())]
        by_db = {db: [0, n] for db, n in expected_val.items()}
        for (db_id, _idx), pool in exact.items():
            if db_id not in by_db:
                continue
            chosen = policy_first_novel(pool["candidates"]) if pool["candidates"] else None
            hit = bool(chosen and chosen["strict"])
            by_db[db_id][0] += hit
            total[0] += hit
        report["selectors"]["policy-frozen(d2greedy)"] = {
            "val_strict": total[0], "val_n": total[1],
            "description": "exact frozen-policy replay over a d2-greedy-built pool",
            "per_db": {db: {"strict": v[0], "n": v[1]} for db, v in sorted(by_db.items())},
        }
        print(f"{'policy-frozen(d2greedy)':28s} val {total[0]}/{total[1]} "
              f"({round(100 * total[0] / max(total[1], 1), 1)}%)")

    # S1: likelihood under ONE defined scorer (d2beam); missing scores lose by default.
    # Execution-filtered: selecting among executed candidates is a DIFFERENT policy from
    # the frozen one, and its serving cost includes executing the pool.
    def s1(alpha, source="d2beam"):
        def select(candidates):
            executable = [c for c in candidates if c["executable"]]
            if not executable:
                return None

            def score(entry):
                scored = _likelihood(entry, source)
                if scored is None:
                    return float("-inf")
                logprob, tokens = scored
                return logprob / (tokens ** alpha)
            return max(executable, key=lambda c: (score(c), -_min_rank(c)))
        return select
    alpha = max(ALPHA_GRID,
                key=lambda a: evaluate(fit, s1(a), expected_fit)[0][0])
    report["s1_alpha"] = alpha
    record(f"S1-likelihood-d2-execfiltered(a={alpha})", s1(alpha),
           "single defined scorer over EXECUTED candidates; union members unscored "
           "by d2 cannot win — a restricted baseline, not a full-union likelihood test")

    # S2: logistic over namespaced per-model features with explicit missingness.
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
        executable = [c for c in candidates if c["executable"]]
        if not executable:
            return None
        rows = scaler.transform([vector(c, len(executable)) for c in executable])
        scores = model.decision_function(rows)
        best = max(range(len(executable)),
                   key=lambda i: (scores[i], -_min_rank(executable[i])))
        return executable[best]
    record("S2-logistic-execfiltered", s2,
           "feature-only baseline over EXECUTED candidates, NOT the semantic-scorer branch")
    if args.save_arbiter:
        with open(args.save_arbiter, "w", encoding="utf-8") as handle:
            json.dump({
                "sources": list(SOURCES),
                "scaler_mean": scaler.mean_.tolist(),
                "scaler_scale": scaler.scale_.tolist(),
                "coef": model.coef_[0].tolist(),
                "intercept": float(model.intercept_[0]),
                "fit_pools": args.pools,
                "seed": SEED,
            }, handle, indent=2)
        print(f"saved arbiter -> {args.save_arbiter}")

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
