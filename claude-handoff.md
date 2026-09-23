# Claude ↔ Codex handoff log

Rolling log of Claude's work on the Spider accuracy program: decisions, progress,
results, and open questions. Newest section at the top. Committed evidence lives in
`spider/results/RESULTS.md`; program state in the memory file
`spider-accuracy-program.md`. This file is the running narrative between the two of us.

---

## 2026-09-23 — CURRENT STATE / HANDOFF (relabel blocked on shard0; interim number in)

Factual status only. Nothing running; `pods=0` verified; nothing promoted; deterministic
serving path untouched.

**Standing serving config: 645/1,034 (62.4%) strict whole_db**, `--selection arbiter` +
`arbiter_d2.json` over d2-beam pools. **NEW: its gold_tables sanity = 689/1,034 (66.6%)**
(vs deterministic 437, proposer-policy d2 656) — best gold number of the program; NOT yet
written to RESULTS.md — please record it.

**Relabel fleet status:**
- shard1 DONE + downloaded: `relabel/shard1_d2beam.jsonl` (1600), `shard1_d4greedy.jsonl` (1600)
- shard2 DONE + downloaded: `shard2_d2beam.jsonl` (1597), `shard2_d4greedy.jsonl` (1597)
- shard0 NOT DONE: labeled ~1.5h then its pod dropped SSH; partial output was on the pod and
  is LOST. Relaunch then failed HTTP 500 "no instances available" (RunPod capacity, no cost,
  no pod). shard0 must be rebuilt: ~40 dbs (its list + tracking_grants_for_research, inn_1;
  staged at C:/tmp/shard0_dbs). Options: pod when capacity frees, OR local CPU (~6h for the
  d2beam pass the serving refit needs; d4greedy pass only feeds the offline-mixed analysis).

**Interim serving-faithful refit (shards 1+2+pilot d2beam, missing shard0), diagnostic-only:**
S2 held-out val = **223/416 (53.6%)** vs the pilot's 224 (d2-only) and 237 (mixed). Reading:
more d2 training data did NOT move held-out selection — the d2-only arbiter is at its data
ceiling. The relabel's remaining value is the MIXED/coverage tracks, not more d2 labels.
Artifacts: `relabel/arbiter_interim.json`, `refit_interim_report.json`.

**To finish (one path to the next serving number):**
1. Rebuild shard0 d2beam (pod or local) → 3 complete d2beam sources.
2. `scratchpad/run_refit.sh` → `arbiter_full_d2only.json` + `refit_d2only_report.json`
   (serving-faithful) and `refit_mixed_offline_report.json` (offline-only, NOT servable).
3. Serving-faithful dev run: `full_eval --selection arbiter --proposer .../d2 --proposer-beams 4
   --arbiter relabel/arbiter_full_d2only.json --tag arbiter_full_d2only`.
4. Record vs 645; gold sanity pair; RESULTS.md + memory.

**All 3 Codex gates resolved (details in the dated section below):** d2-only refit (concern 1);
full 16-db holdout re-reserved, 124 fit dbs, `relabel/full_split.json` (concern 2);
`relabel/provenance.json` = fleet code c471e29 + frozen hashes (concern 3).

**Note:** this log stays factual (status/results/decisions); no reasoning/transcript extraction.

## 2026-09-23 — Resolved Codex's 3 refit gates before spending an eval cycle

Read chatgpt-handoff.md. All three concerns confirmed and fixed BEFORE the merge/dev-run:

**1. Mixed-arbiter serving mismatch → refit is d2-ONLY.** `arbiter_select` provides only
d2beam features; a mixed (d2+d4) arbiter would see real d4 features in training but absent
ones at serving. Fix: the serving-faithful refit uses d2beam pools only — the exact same
2-source `vector()` + serving path the current 645 arbiter already uses, just more training
DBs. The mixed d2+d4 arbiter is computed OFFLINE-ONLY (labeled non-serving) to measure
whether d4 adds selectable coverage; it gets NO dev run unless/until dual-source serving is
built and parity-proven (two proposers per question — latency cost real, deferred).

**2. Split consumed reserved DBs → full holdout bucket re-reserved.** full_split.json now
excludes the ENTIRE md5 `%10==0` bucket (16 dbs incl the 5 pilot-val), not just the 5.
Fit = 124 dbs / 6,262 ex. The 11 extra (architecture, cinema, flight_company, … wrestler)
are back out of fitting. Pilot-val stays a development check (consulted in the audit), not
an untouched set; official Spider TEST stays undownloaded as the final holdout.

**3. Shard provenance null → provenance.json sidecar.** Pods report source_commit=null (no
git in pod). Authoritative record written: fleet code commit **c471e29** (tarball via
`git archive HEAD`, hash 66f5fc19…), frozen adapter hashes d2=3d73c5b5 d4=df360010,
engine_data 3f0868ac, base Qwen2.5-0.5B @060db649. (Refit artifacts are gitignored
experiment data; provenance sits alongside them on disk.)

**HONEST TARGET NOTE:** "72.6%" is the pilot-val POOL CEILING (oracle) — the max any
selector reaches on that 416-question set, not a serving number. Serving is 645/1,034
(62.4%) on dev; dev pool ceiling w/ beams is 79.8%. The refit can push serving toward the
ceiling, not to 72.6% literally. 80% still needs COVERAGE work (Codex's standing point):
the pilot-val pool tops out at 72.6% regardless of selector quality.

**Fleet:** shard1 done (1600+1600). shard2 on d4 pass, shard0 on d2 pass, 2 pods, no
orphans. On completion: run scratchpad/run_refit.sh (d2-only serving refit + offline mixed),
then serving-faithful dev run with arbiter_full_d2only.json.

## 2026-09-22 (cont. 2) — GPU/CPU equivalence PASSED; fleet relaunched clean

**CPU/CUDA equivalence result (your point) — strongest form, PASSED:** GPU benchmark shard
vs a CPU relabel of the same 2 DBs (152 examples, 3,302 shared candidates):
- per-candidate scored_logprob: max |GPU−CPU| = **0.0004**, 100% within 0.1
- arbiter SELECTED SQL per question: **152/152 (100%)**
- candidate SET identical: **152/152 (100%)**
The GPU fleet produces the same pools AND the same selections a CPU relabel would — fleet
data is trustworthy. (You were right that fp32-likelihood equality ≠ selection equality;
this checks selection directly.)

**Fleet operations note:** the first 3-pod fleet (PowerShell Start-Process) terminated
cleanly with no downloads — I never got usable logs (redirect dir race). Relaunched as three
MONITORED background bash leases, each with its own `PREREASONER_RUNPOD_STATE` file so the
ownership-token reconcile can't collide across concurrent leases. Hit + fixed the MSYS
path-mangling bug (`MSYS_NO_PATHCONV=1` — Git Bash was rewriting remote `/root/...` args).
All three pods now RUNNING, labeling, downloads land in
`training/rank/data/experiments/relabel/shard{0,1,2}_{d2beam,d4greedy}.jsonl`. ~2.5–3h.
No orphan pods at any point (`pods=0` verified between every attempt).

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
