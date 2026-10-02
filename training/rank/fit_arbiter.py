"""Fit the SQL arbiter: a logistic score over ARBITER_FEATURES on execution-labeled pools.

Input: pools labeled by build_pool_labels.py (JSONL shards of one proposer and one pool
contract) and a split manifest naming fit and validation databases. Every labeled candidate of
a fit database is one training row: its ARBITER_FEATURES (computed by engine.sql_rank, exactly
as serving computes them) and its strict label. The features are standardized and a logistic
regression (seed 7) is fit; the standardization and coefficients ARE the arbiter.

Validation databases are never fit on. Replay uses the shared post-ranking serving rule
and saved gold-blind eligibility/calculation/money-total facts. A candidate still requires
a fresh production-entry-point evaluation before promotion.

Output: the arbiter JSON in the runtime format read by engine.sql_rank.SQLArbiter, written into
the experiment directory. Installing it is training/rank/promote.py's job.

    python -m training.rank.fit_arbiter --pools training/rank/data/experiments/<id>/pools*.jsonl \\
        --split training/rank/data/experiments/<id>/split.json \\
        --out training/rank/data/experiments/<id>/sql_arbiter.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.artifact_provenance import sha256_file, write_json_artifact
from engine.sql_candidate import ScoredQuery
from engine.sql_rank import (
    ARBITER_FEATURES, ENDORSED, SQLArbiter, arbiter_features, arbitrate, select_ranked_candidate,
)
from spider.probe.full_eval import _git_provenance

SEED = 7


def _decoded_by_proposer(evidence) -> bool:
    return any(tag == "proposer:greedy" or tag.startswith(("proposer:beam", "proposer:variant"))
               for tag in evidence)


def load_pools(paths):
    """Labeled pools keyed by (db_id, idx), plus the pool contract and proposer they share.

    Shards are partitions of one labeling run: a duplicate question or a duplicate SQL within
    a pool is an error, never a silent overwrite, and every shard must declare the same pool
    contract and the same proposer adapter.
    """
    pools, identity = {}, None
    database_hashes = {}
    for path in paths:
        meta = None
        found = set()
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle):
                record = json.loads(line)
                if "_meta" in record:
                    if number != 0:
                        raise ValueError(f"{path}: _meta must be the first record")
                    meta = record["_meta"]
                    if meta.get("pool") is None or meta.get("proposer_adapter_sha256") is None:
                        raise ValueError(f"{path}: _meta lacks the pool contract or proposer identity")
                    if (meta.get("schema_version") != 2
                            or meta.get("selection_policy") != "shared-ranked-intents-v1"
                            or not meta.get("expected_examples") or not meta.get("source_hashes")):
                        raise ValueError(f"{path}: regenerate labels with the serving-faithful contract")
                    shard = {k: v for k, v in meta.items()
                             if k not in {"expected_examples", "databases"}}
                    if identity is not None and shard != identity:
                        raise ValueError(f"{path}: labeling contract differs from earlier shards")
                    identity = shard
                    for db, digest in meta["databases"].items():
                        if db in database_hashes and database_hashes[db] != digest:
                            raise ValueError(f"{path}: database {db} differs between shards")
                        database_hashes[db] = digest
                    continue
                if meta is None:
                    raise ValueError(f"{path}: missing _meta header")
                key = (record["db_id"], record["idx"])
                if key in pools:
                    raise ValueError(f"{path}: duplicate question {key}")
                sqls = [candidate["sql"] for candidate in record.get("candidates") or ()]
                if len(set(sqls)) != len(sqls):
                    raise ValueError(f"{path}: duplicate candidate SQL in {key}")
                pools[key] = record
                found.add((record["idx"], record["db_id"]))
        if meta is None or found != {tuple(pair) for pair in meta["expected_examples"]}:
            raise ValueError(f"{path}: incomplete or unexpected question denominator")
    if identity is None:
        raise ValueError("no pool contract found in the labeled pools")
    return [pools[key] for key in sorted(pools)], identity["pool"], identity["proposer_adapter_sha256"]


def pool_rows(record):
    """(features, strict, executable) for every pooled candidate that carries a likelihood."""
    candidates = record.get("candidates") or ()
    rows = []
    for rank, candidate in enumerate(candidates):
        scored = candidate.get("features") or {}
        missing = {"proposer:scored_logprob", "proposer:scored_tokens"} - set(scored)
        if missing:
            raise ValueError(f"candidate {rank} is missing proposer likelihood features: {sorted(missing)}")
        logprob = float(scored["proposer:scored_logprob"])
        tokens = float(scored["proposer:scored_tokens"])
        if not math.isfinite(logprob) or not math.isfinite(tokens) or tokens <= 0:
            raise ValueError(f"candidate {rank} has invalid proposer likelihood features")
        evidence = tuple(candidate.get("evidence") or ())
        proposed = candidate.get("proposed")
        if proposed is None:
            # Historical pools predate structural origin; endorsement retains
            # search membership even when decode tags were copied onto it.
            proposed = _decoded_by_proposer(evidence) and ENDORSED not in evidence
        features = arbiter_features(
            ScoredQuery(None, candidate["sql"], candidate["score"], evidence),
            rank, len(candidates), logprob, tokens, proposed,
        )
        # `error` is from the second evaluation pass that obtains the gold label.
        # It must not rewrite the original serving-time eligibility facts: the
        # candidate may have executed in TableQuery and then failed only in this
        # independent label pass. Its label is conservatively false, while the
        # selection replay still sees the same candidate pool as serving.
        eligible = (candidate.get("executable", True) and candidate.get("grounded", True)
                    and candidate.get("eligible", True))
        rows.append((rank, features, bool(candidate.get("strict")), eligible))
    return rows


def selected_index(record, arbiter):
    """Return the served candidate for one saved pool, using only serving-time fields."""
    candidates = record.get("candidates") or []
    required = {"proposed", "eligible", "executable", "grounded",
                "calculation_satisfied", "money_total", "date_satisfied"}
    if any(not required.issubset(candidate) for candidate in candidates):
        raise ValueError("pool lacks serving selection facts; regenerate labels before replay")
    rows = pool_rows(record)
    pool = [ScoredQuery(None, candidate["sql"], candidate["score"],
                        tuple(candidate.get("evidence") or ())) for candidate in candidates]
    likelihoods = [None] * len(pool)
    proposed = set()
    for rank, features, _, eligible in rows:
        if eligible:
            likelihoods[rank] = (features[0], features[1])
        if features[ARBITER_FEATURES.index("from_search")] == 0.0:
            proposed.add(pool[rank].sql)
    _, ranking = arbitrate(pool, frozenset(proposed), likelihoods, arbiter)
    return select_ranked_candidate(
        ranking, [c["calculation_satisfied"] for c in candidates],
        [c["money_total"] for c in candidates], [c["date_satisfied"] for c in candidates])


def replay(pools, arbiter):
    """Apply the serving ranking/intent rule; refuse pools missing its required facts."""
    per_db = {}
    for record in pools:
        tally = per_db.setdefault(record["db_id"], {"n": 0, "strict": 0, "oracle": 0})
        tally["n"] += 1
        candidates = record.get("candidates") or ()
        tally["oracle"] += any(strict and eligible for _, _, strict, eligible in pool_rows(record))
        selected = selected_index(record, arbiter)
        if selected is not None:
            tally["strict"] += bool(candidates[selected].get("strict"))
    total = {key: sum(tally[key] for tally in per_db.values()) for key in ("n", "strict", "oracle")}
    return total, per_db


def paired_replay(pools, candidate_arbiter, baseline_arbiter):
    """Compare two selectors question-by-question on exactly the same saved candidate pools."""
    per_db = {}
    totals = {"n": 0, "candidate_strict": 0, "baseline_strict": 0,
              "wins": 0, "losses": 0, "oracle": 0}
    for record in pools:
        db = record["db_id"]
        tally = per_db.setdefault(db, {key: 0 for key in totals})
        candidates = record.get("candidates") or []
        candidate_index = selected_index(record, candidate_arbiter)
        baseline_index = selected_index(record, baseline_arbiter)
        candidate_correct = bool(candidates[candidate_index].get("strict")) if candidate_index is not None else False
        baseline_correct = bool(candidates[baseline_index].get("strict")) if baseline_index is not None else False
        oracle = any(strict and eligible for _, _, strict, eligible in pool_rows(record))
        tally["n"] += 1
        tally["candidate_strict"] += candidate_correct
        tally["baseline_strict"] += baseline_correct
        tally["wins"] += candidate_correct and not baseline_correct
        tally["losses"] += baseline_correct and not candidate_correct
        tally["oracle"] += oracle
        totals["n"] += 1
        totals["candidate_strict"] += candidate_correct
        totals["baseline_strict"] += baseline_correct
        totals["wins"] += candidate_correct and not baseline_correct
        totals["losses"] += baseline_correct and not candidate_correct
        totals["oracle"] += oracle
    totals["net_wins"] = totals["wins"] - totals["losses"]
    for tally in per_db.values():
        tally["net_wins"] = tally["wins"] - tally["losses"]
    return totals, per_db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", nargs="+", required=True)
    ap.add_argument("--split", required=True,
                    help="JSON with fit_dbs and validation_dbs lists")
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline-arbiter", default=os.path.join(ROOT, "engine", "data", "sql_arbiter.json"),
                    help="frozen arbiter used as the paired control on the same validation pools")
    args = ap.parse_args()

    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    split = json.load(open(args.split, encoding="utf-8"))
    fit_dbs, validation_dbs = set(split["fit_dbs"]), set(split["validation_dbs"])
    if fit_dbs & validation_dbs:
        raise SystemExit("split manifest lists a database in both fit and validation")
    pools, contract, proposer_sha256 = load_pools(args.pools)
    with open(args.pools[0], encoding="utf-8") as handle:
        labeling_meta = json.loads(handle.readline())["_meta"]
    fit = [record for record in pools if record["db_id"] in fit_dbs]
    validation = [record for record in pools if record["db_id"] in validation_dbs]
    represented = {record["db_id"] for record in pools}
    if not fit_dbs or not validation_dbs or represented != fit_dbs | validation_dbs:
        raise ValueError("split must cover every labeled database, with nonempty fit and validation")

    features, labels = [], []
    for record in fit:
        for _, row, strict, eligible in pool_rows(record):
            if eligible:
                features.append(list(row))
                labels.append(int(strict))
    if len(set(labels)) < 2:
        raise ValueError("fit pools must contain both strict-correct and strict-incorrect candidates")
    scaler = StandardScaler().fit(features)
    model = LogisticRegression(random_state=SEED, max_iter=1000).fit(
        scaler.transform(features), labels)
    artifact = {
        "features": list(ARBITER_FEATURES),
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "pool": contract,
        "fit": {
            "pools": {os.path.relpath(path, ROOT).replace(os.sep, "/"): sha256_file(path)
                      for path in args.pools},
            "split": {os.path.relpath(args.split, ROOT).replace(os.sep, "/"):
                      sha256_file(args.split)},
            "proposer_adapter_sha256": proposer_sha256,
            "likelihood_policy": labeling_meta["proposer_contract"]["likelihood_policy"],
            "seed": SEED,
            "fit_questions": len(fit),
            "fit_candidates": len(features),
        },
    }
    arbiter = SQLArbiter.from_payload(artifact, args.out)
    total, per_db = replay(validation, arbiter)
    baseline_payload = json.loads(Path(args.baseline_arbiter).read_text(encoding="utf-8"))
    baseline_arbiter = SQLArbiter.from_payload(baseline_payload, args.baseline_arbiter)
    if (baseline_arbiter.search_candidates != contract["search_candidates"]
            or baseline_arbiter.execution_op_limit != contract["execution_op_limit"]):
        raise ValueError("baseline arbiter and validation pools use different deterministic-search contracts")
    baseline_total, baseline_per_db = replay(validation, baseline_arbiter)
    paired, paired_per_db = paired_replay(validation, arbiter, baseline_arbiter)
    if paired["candidate_strict"] != total["strict"] or paired["oracle"] != total["oracle"]:
        raise AssertionError("paired replay and candidate validation totals disagree")
    artifact["fit"]["trainer_sha256"] = sha256_file(__file__)
    artifact["fit"]["baseline_arbiter_sha256"] = sha256_file(args.baseline_arbiter)
    artifact["fit"]["trainer_source"] = _git_provenance(ROOT)
    artifact["fit"]["baseline_fit_pool"] = baseline_payload["pool"]
    artifact["validation"] = {"metric": "saved_pool_serving_selection",
                              "questions": total["n"], "strict": total["strict"],
                              "pool_oracle": total["oracle"], "per_db": per_db,
                              "baseline_strict": baseline_total["strict"],
                              "paired": paired, "paired_per_db": paired_per_db,
                              "baseline_per_db": baseline_per_db}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    write_json_artifact(args.out, artifact, indent=2)
    print(f"fit {len(features)} candidates from {len(fit)} questions; validation "
          f"{total['strict']}/{total['n']} strict vs baseline "
          f"{baseline_total['strict']}/{baseline_total['n']} "
          f"(paired +{paired['wins']}/-{paired['losses']}, oracle {total['oracle']}) -> {args.out}")


if __name__ == "__main__":
    main()
