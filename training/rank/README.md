# Rank-head training pipeline

The ONE pipeline for the structural rerank head experiment (Phase B of the Spider accuracy
plan, `spider/results/RESULTS.md`). It owns two steps:

1. `build_pool_labels.py` — runs Spider TRAIN questions through the PRODUCTION search path
   (`spider.probe.full_eval.ast_predict`, `pool_oracle` selection) and labels every pooled
   candidate by execution-verified strict/lenient match against the train gold denotation.
   Spider train gold is training data only; no gold-derived signal ever reaches a serving
   decision, and the dev split is never read here.
2. Head training (pairwise ranking over within-pool labels) — the head reranks the top of the
   deterministic candidate order through the existing `rank_model` hook in
   `engine/sql_search.py:SQLSearcher.search`. Only head weights train; the encoder, LoRA,
   searcher, and named features stay frozen.

Candidate artifacts live ONLY in `training/rank/data/experiments/<experiment-id>/`
(gitignored, disposable). Never train into `engine/data/`. Promotion of a head into the
runtime bundle is a separate, explicit, user-approved action with a `weights_manifest.json`
update and rollback instructions, and it must update every "no learned ranker" statement in
`docs/SQL_AST.md` / `docs/ARCHITECTURE.md` in the same change.

```bash
# smoke (30 examples)
python -m training.rank.build_pool_labels --limit 30 \
  --out training/rank/data/experiments/b1/pool_labels.jsonl
# full train split (~7,000 examples, hours; resumable — reruns skip finished idx)
python -m training.rank.build_pool_labels \
  --out training/rank/data/experiments/b1/pool_labels.jsonl
```
