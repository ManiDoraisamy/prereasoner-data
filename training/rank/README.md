# CPU SQL selector training and bundle staging

Production has one proposer: XiYanSQL 7B Q4_K_M, deterministic greedy decoding.
The recorded baseline uses neutral likelihood sentinels with a disclosed historical
0.5B-fit arbiter. New selectors must be fitted and validated on this 7B's own pools.

1. Label official Spider TRAIN examples through the production selection owner:

   ```bash
   python -m training.rank.build_pool_labels --data <spider-data> --dbs <sqlite-directory> \
       --out training/rank/data/experiments/<id>/pools.jsonl --dbs-filter <dbs>
   ```

   The header pins generation, source hashes, dataset and database hashes, model bundle,
   CPU threads, and every expected question identity. Resume refuses any changed contract.
   Missing/failed examples remain in the denominator. Each candidate retains exact score,
   structural origin, eligibility and gold-blind calculation/money-total facts.
   DEV and TEST are never label inputs. Use `PREREASONER_DATA_DIR` for a candidate bundle.

2. Fit on database-disjoint fit and validation sets declared in a preregistered split JSON:

   ```bash
   python -m training.rank.fit_arbiter --pools <complete-shards> --split <split.json> \
       --out training/rank/data/experiments/<id>/sql_arbiter.json
   ```

   The split has `fit_dbs` and `validation_dbs`. Seed is 7. All shards must share the same
   source/model/data contract and contain every declared index once. Ineligible candidates
   cannot be fitted or selected. Replay calls the same post-ranking rule as serving.
   Fitting-side holdout does not prove the pretrained proposer never saw those databases.
   Compare baseline and candidate on identical pools before expanding an experiment.

3. Stage one complete immutable bundle, never a hot per-file update:

   ```bash
   python -m training.rank.promote --source-bundle engine/data --arbiter <candidate.json> \
       --destination training/rank/data/experiments/<id>/bundle
   ```

   The destination must not exist. The tool validates binding and hashes in a temporary sibling
   directory, then publishes it with one directory rename. It rejects a new mismatched selector;
   the exact frozen baseline can be restaged without pretending it is model-matched.
   Staging is NOT evidence that release gates passed. Test the candidate via
   `PREREASONER_DATA_DIR`, run full paired CPU DEV and official test-suite metrics and the full
   product/browser gates, then release one immutable image. Rollback is the previous image and
   its original hash-bound bundle, not an unverified mix of model files.

Candidate artifacts live only in `training/rank/data/experiments/<id>/` (gitignored).
No experiment overwrites the deployed bundle. No gold-derived information enters a serving
decision; gold is used only to label TRAIN examples or grade an independently selected answer.
