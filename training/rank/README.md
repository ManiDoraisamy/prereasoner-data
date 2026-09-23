# SQL arbiter training and promotion

This pipeline produces `engine/data/sql_arbiter.json`, the linear score that chooses the served
own-data query among the deterministic search's candidates and the proposer's
(`engine/sql_rank.py:SQLArbiter`), and installs it together with its proposer adapter.

## Steps

1. **Label pools** — `build_pool_labels.py` runs Spider TRAIN questions through the production
   selection (`TableQuery.select_query`, via the evaluator's `pool_oracle` mode) and labels every
   pooled candidate strict/lenient against the gold execution on the same capped tables. Each record
   carries the proposer likelihood and the pool contract it was built under. Pools come from the
   runtime bundle; to label a candidate adapter, point `PREREASONER_DATA_DIR` at a candidate bundle.
   Shards partition the work (`--dbs-filter`); train gold never reaches a serving decision and dev is
   never read.

   ```bash
   python -m training.rank.build_pool_labels \
       --out training/rank/data/experiments/<id>/pools.jsonl --dbs-filter <dbs>
   ```

2. **Fit** — `fit_arbiter.py` computes `ARBITER_FEATURES` for every labeled candidate of the fit
   databases with the serving code (`engine.sql_rank.arbiter_features`), standardizes them and fits a
   logistic regression (seed 7). Validation databases are never fit on; they are replayed with the
   serving rule and reported against the pool oracle. Shards must share one pool contract and one
   proposer identity; duplicate questions or SQL fail the load.

   ```bash
   python -m training.rank.fit_arbiter --pools training/rank/data/experiments/<id>/pools*.jsonl \
       --split training/rank/data/experiments/<id>/split.json \
       --out training/rank/data/experiments/<id>/sql_arbiter.json
   ```

   The split file lists `fit_dbs` and `validation_dbs`.

3. **Promote** — `promote.py` is the only writer of `engine/data/sql_proposer/` and
   `engine/data/sql_arbiter.json`. It refuses an arbiter fit on a different adapter, copies both
   atomically, and records their hashes in `engine/data/weights_manifest.json`. Install locally for
   the release gates, publish the adapter to the weights repository, then pin its revision:

   ```bash
   python -m training.rank.promote --adapter training/proposer/data/experiments/<id> \
       --arbiter training/rank/data/experiments/<id>/sql_arbiter.json --local-only
   # gates: python -m tests.test_sql_ast, regress --offline, a fresh Spider whole_db run
   python -m training.rank.promote ... --revision <immutable-hf-commit>
   ```

   Rollback is the previous commit's `sql_arbiter.json` and manifest plus `python -m
   engine.fetch_weights`, which restores the pinned adapter bytes.

Candidate artifacts live only in `training/rank/data/experiments/<id>/` (gitignored).

## The shipped arbiter

The served arbiter was fit by the arbitration pilot on d2 4-beam pools of 15 Spider TRAIN databases
(31,086 candidates from 1,625 questions; 5 validation databases held out). The pilot's two-proposer
layout also carried six slots that are constant with coefficient exactly 0.0 when one proposer
serves; they were dropped, which leaves every score bit-identical. `fit_arbiter.py` on the same pools
reproduces the means, scales and intercept exactly and the coefficients within 1e-14 relative. The
artifact records its pools, split, adapter identity and the Spider evidence it was served under.
