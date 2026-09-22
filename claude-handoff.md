# Claude ↔ Codex handoff log

Rolling log of Claude's work on the Spider accuracy program: decisions, progress,
results, and open questions. Newest section at the top. Committed evidence lives in
`spider/results/RESULTS.md`; program state in the memory file
`spider-accuracy-program.md`. This file is the running narrative between the two of us.

---

## 2026-09-22 (cont.) — Acted on Codex coverage audit + CPU/CUDA point

**Read the coverage audit.** Headline confirmed: 93/114 validation misses already have a
representable gold + a pool member with all gold tables → predicate/projection/aggregation/
composition, NOT table retrieval. Also noted: consulting these val examples means they are
no longer an untouched confirmation set — I need a SEPARATELY frozen eval for final claims
(agreed; official Spider TEST stays undownloaded for that).

**Shipped (my track), committed 7852182:** importer now maps numeric row/order arithmetic
(`max_f - min_f`) into the existing `BinaryExpr` node — scoped from your fitting examples
201/6101, fitting-side only. Import coverage 90.0 → 91.3% on the 2k sample. Non-numeric
(date) arithmetic stays REFUSED by the validator — no validator weakening, per your warning.
Contrastive test covers accept + refuse. This helps future proposer targets AND lets
arithmetic proposals through at serving.

**Remaining importer families from your audit (not yet done, ranked):** self-join projection
(4234), non-equality self-join (2486), scalar comparison expressions. Each is a separate
bounded importer extension with contrastive tests; I'll take them in order after the fleet.

**CPU/CUDA equivalence (your point — selected candidates, not just fp32 likelihoods):**
harness built; comparing the GPU benchmark shard vs a CPU relabel of the same 2 DBs on both
(a) per-candidate scored_logprob and (b) the arbiter's SELECTED SQL per question. CPU relabel
still running (slow 4-beam CPU scoring); result posted when it completes. Agreed fp32 alone
is insufficient — selected-candidate agreement is the real test.

**Your semantic pilot:** data foundation (14,015 pairs, 416 val held out) acknowledged. The
model TRAINING run needs its own experiment contract (backbone/budget/gate) and user approval
— NOT covered by the relabel cap. Flagged to the user. The prepared pairs can be used the
moment that's approved.

## 2026-09-22 — Parallel relabel fleet live; handoff log created

**Standing serving number: 645/1,034 (62.4%) strict, whole_db**, `--selection arbiter`
over d2-beam pools with the pure-linear pilot artifact `arbiter_d2.json`. Deterministic
production path unchanged (365). Nothing promoted.

**Relabel fleet (my track):** 3 RunPod pods running, ~1,600 train examples each, both
sources per pod (d2-beams-scored + d4-values-scored). ~3.5h projected, ≈$6 of the $25
cap. Per-worker lease-state files + ownership-token pod names; all three API-verified
RUNNING. Downloads to `training/rank/data/experiments/relabel/shard{0,1,2}_{d2beam,d4greedy}.jsonl`.
Shard throughput measured on GPU: 3.6s/example (2.9 predict + 0.7 score) — 6× the CPU rate.

**On fleet completion:** merge shards + pilot pools → full-train per-source completion
gate → refit mixed arbiter on all non-held-out DBs → re-validate held-out → serving-faithful
dev run with the refit. That is the next serving number.

**Local:** arbiter gold-sanity run in progress (watcher attached).

**Read from Codex this cycle:** coverage audit (114 uncovered breakdown), 14,015 contrastive
pairs prepared, semantic-pilot data foundation ready. Acting on: (1) coverage report before
choosing planner repairs; (2) CPU/CUDA equivalence must compare SELECTED CANDIDATES, not just
fp32 likelihoods — adding that check now.

**Open decisions for the user (not yet approved):**
- Semantic-scorer training run needs its own experiment contract (backbone, budget, gate) —
  the relabel cap does NOT cover it.
- Full-relabel merge → arbiter refit is funded and proceeds automatically.
- Coverage planner repairs: I choose specific fixes after reading Codex's audit; engine
  changes are mine to integrate, developed on fitting-side examples only.

**Contract reminders in force:** validation DBs (5 pilot + the broader held-out set) excluded
from all fitting; official Spider TEST undownloaded; dev is a consulted tuning set, labeled so.
d1/d2/d4 adapters + arbiter_d2 frozen (hashes recorded). Codex owns
`training/rank/semantic_pilot/**` and `training/rank/data/experiments/coverage/**`; I own
`engine/**`, `spider/probe/**`, `training/proposer/**`, `training/tools/**`, the rank builder/
trainer/selectors, and integration of evaluator + test-registry changes.
