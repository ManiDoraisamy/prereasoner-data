# SQL proposer training

This pipeline produces the proposer adapter that serving loads from `engine/data/sql_proposer/`
(`engine/sql_proposer.py`). The proposer is a LoRA adapter on the pinned Qwen2.5-0.5B base. It
suggests candidate SQL; serving accepts a suggestion only if it imports into the typed AST and
validates, and a fitted arbiter decides whether it beats the search's candidates
(`training/rank/`).

## Steps

1. **Targets** — `import_gold.py` maps Spider TRAIN gold SQL into the typed AST with the serving
   importer (`engine/sql_import.py`). An example becomes a target only when the rendered AST executes
   on the capped tables and strictly matches the gold execution, so the targets are exactly the SQL the
   served proposer is allowed to emit. Dev is never read.

   ```bash
   python -m training.proposer.import_gold \
       --targets training/proposer/data/experiments/<id>/targets.jsonl
   ```

2. **Fine-tune** — `train_sft.py` trains the adapter on (serving prompt, rendered SQL + EOS) pairs:
   seed 7, prompt tokens masked from the loss, databases in the `is_validation_db` bucket held out.
   The prompt is `engine/sql_prompt.py:schema_prompt`, the one serving uses. GPU leases go through
   `training/tools/runpod_api.py lease`.

   ```bash
   python -m training.proposer.train_sft \
       --targets training/proposer/data/experiments/<id>/targets.jsonl \
       --out-dir training/proposer/data/experiments/<id> --max-steps 6000
   ```

3. **Scorer check** — after any change to the likelihood code, `verify_scorer.py` confirms the
   batched scorer equals one-at-a-time scoring exactly and matches a naive full forward pass.

   ```bash
   python -m training.proposer.verify_scorer
   ```

4. **Arbiter and promotion** — label pools with the candidate adapter and fit an arbiter
   (`training/rank/README.md`); `training/rank/promote.py` installs the adapter and its arbiter
   together, because an arbiter is only valid for the adapter whose pools it was fit on.

Artifacts live only in `training/proposer/data/experiments/<id>/` (gitignored). Never train into
`engine/data/`.

## The shipped adapter

The served adapter is experiment `d2`: 6,000 steps at effective batch 16 on the 90.0%-importable Spider
TRAIN targets, seed 7. Its measured effect on the served pipeline is in
`spider/results/RESULTS.md`, and its runtime identity (sha256 of `sql_proposer/`) is recorded in
`engine/data/sql_arbiter.json` under `fit.proposer_adapter_sha256`.

## Proposed CPU-only accuracy program (2026-09-24)

Status: proposal requested by the user; no training, paid job, publication, or deployment
has been started by this planning task. This section is the canonical proposal for the
larger-proposer experiment. The shipped pipeline above remains the current implementation;
measured evidence continues to live in `spider/results/RESULTS.md`.

### Objective and constraints

Develop accuracy with a 7-8B proposer, measure a 3B alternative, and promote the smallest
single proposer that meets the accuracy and CPU-serving gates. CPU-only applies to
production inference and local inference; temporary GPU training is an optional research
expense. One engine, one SQL selection implementation, one promoted proposer/arbiter bundle,
and one full-traffic production revision remain the intended release state.

The existing 0.5B semantic encoder, Schema.org head, world grounding, and typed SQL/Python
execution keep their existing responsibilities. Increasing the SQL proposer does not require
increasing that encoder. In particular, do not change the shared `QWEN_MODEL_ID` to replace
both models accidentally: the new proposer needs its own explicit model identity in the
selection bundle.

The historical reference is 645/1,034 (62.4%) strict Spider dev whole_db. A score of at least
80% requires 828 correct, or 183 net additional wins. The recorded d2-beam oracle of 825
cannot support that target. The separate mixed-pool pilot oracle is 302/416; do not combine
those populations. The design target is approximately 90% correct-query coverage and 90%
selection success conditional on coverage (81% overall). Other combinations can work;
90% coverage is not a mathematically necessary threshold.

There are two separate claims to establish:

1. A development milestone of >=828/1,034 under the existing strict evaluator, reported
   alongside official Spider test-suite accuracy, without changing the denominator.
2. Accuracy on independently held-out databases, with uncertainty and limitations stated.
   An 80% dev result alone never establishes 80% live-product or unseen-domain accuracy.

### Model shortlist and immutable inputs

Start with exactly these two deployment candidates, plus the frozen current baseline:

| Role | Model | Proposed immutable upstream revision | License |
|---|---|---|---|
| Accuracy lead | `Qwen/Qwen2.5-Coder-7B-Instruct` (7.61B) | `c03e6d358207e414f1eca0bb1891e29f1db0e242` | Apache-2.0 |
| Smaller alternative | `HuggingFaceTB/SmolLM3-3B` | `a07cc9a04f16550a088caea529712d1d335b0ac1` | Apache-2.0 |

These repository revisions and license metadata were read from the publishers' Hugging Face
APIs on 2026-09-24. Freeze the actual downloaded weight, tokenizer, template, configuration,
license, and conversion hashes before executing an experiment; no mutable `main` loads.
This is a comparison of practical model choices across families, not a causal size-only
ablation. The lead was chosen for a mature code-oriented model/runtime combination, not a
claim that it is the newest or best available model.

Qwen2.5-Coder-3B-Instruct is excluded from the default public-release shortlist: its published
license grants non-commercial research/evaluation use and requires a separate license for
commercial use. Its 7B sibling's Apache license does not apply to it. SmolLM3 is the proposed
permissive 3B alternative. Disable its extended-thinking mode using its pinned template and
verify that the token budget includes all generated tokens.

Primary references:
- [7B publisher model card](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct)
- [3B publisher model card](https://huggingface.co/HuggingFaceTB/SmolLM3-3B)
- [Qwen Coder 3B license](https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct/blob/main/LICENSE)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)

### Evaluation design before model work

- Capture the current code, evaluator, complete model bundle, dependency lock, and baseline
  predictions by hash. Compare each new model against that baseline on identical questions,
  database snapshots, row caps, execution limits, and failure accounting.
- Existing Spider dev and the 416-question pilot are consulted development sets. Keep their
  histories visible. Never rename either an untouched holdout.
- Make database-level fitting/development folds from Spider TRAIN, seed 7, before new fitting.
  Every new proposer fit excludes its reporting fold. Arbiter fitting uses out-of-fold
  candidate pools where feasible; a question's gold target must not have trained the proposer
  that generated its arbiter-training pool. Keep all augmentations of a database in its fold.
  Expand to three folds only after the feasibility screen; charge those runs to the budget.
- Audit exposure of every retained learned component. An old adapter that trained on newly
  reserved databases is not an unbiased comparator there. Reserve a separately authored or
  externally held database set, ideally >=1,000 questions across >=20 databases, whose labels
  the development process cannot inspect. Verify official Spider test submission access
  before relying on it; do not assume its hidden labels are downloadable.
- Public base-model pretraining exposure is not fully known. State that limitation for public
  benchmarks; an independent new private-schema set offers stronger evidence.
- Report current strict, official test-suite accuracy, pool coverage, capture conditional on
  coverage, paired wins/losses, and per-database/difficulty/error-family results. Report
  uncertainty using database-clustered resampling. Do not advertise a lower confidence bound
  of 80% merely because the point estimate passes 80%.
- Invalid SQL, rejected imports, missing records, timeouts, and abstentions count as incorrect.
  Use full manifest denominators and fail on duplicate or missing shard identities. A large
  raw-SQL oracle that disappears at the typed-AST gate does not count as usable coverage.
- Maintain a separate product suite covering knowledgebase joins, uploaded/reference tables,
  ambiguous requests, currency, numeric precision, follow-ups, and complex decompositions.
  Benchmark success never substitutes for that suite.

The official evaluation implementation is
[test-suite-sql-eval](https://github.com/taoyds/test-suite-sql-eval). Pin its revision and
database artifacts and report it alongside, rather than replacing, the existing metric.

### Stage 1: CPU runtime and model screen

Use the local Ryzen 9 8945HS / approximately 32 GB RAM and a bounded Cloud Run job in
`prereasoner-inference`, `us-central1`, initially 8 vCPU / 24 GiB, one task and one question
at a time. The full engine must be resident when measuring memory. Measure 16 GiB after
feasibility; use 32 GiB only to diagnose a measured memory requirement. Cloud Run services
currently cap CPU at 8 vCPU, so adding instance replicas does not accelerate one question.

1. Pin the llama.cpp build and CPU options. Begin with 20 fitting-side smoke questions
   spanning schema width and SQL shape. Test prompt serialization, arbitrary supplied-SQL
   token likelihoods, prefix-cache reuse, bounded memory, error propagation, and selected-SQL
   repeatability. A text-generation-only wrapper or top-k-only log-probability API is
   insufficient for the current arbiter.
2. Establish a small 0.5B bridge comparison to separate runtime/conversion effects from model
   changes. Compare fixed-candidate likelihoods and selected SQL; do not assume quantization
   reproduces FP32 values. Historical inference stays in an immutable baseline checkout,
   not a second serving implementation or automatic fallback.
3. Screen Q4_K_M on both candidate models. On a fixed subset compare against Q8_0 as a
   higher-precision reference; try Q5_K_M only if Q4 shows consequential degradation. Fit
   selectors using the actual chosen quantized model's scores. Quantization is part of the
   experiment identity and must be evaluated after any adapter training/merge.
4. Freeze a 200-question development screen before inspecting outputs. Use the same questions,
   maximum candidate count, token ceilings, retrieval inputs, and execution contract for both
   sizes. Use each model's required template and record it. Start with 25 search candidates
   plus at most four canonical, distinct proposer queries, a maximum of 256 generated tokens
   per proposal, and a measured context budget initially capped at 8,192 tokens. These are
   new candidate settings, not claims of parity with the baseline's 96-token beam contract.
5. Prove whether the pinned backend supports the required beam operation. If it does not,
   use four fixed deterministic prompt variants as a separate, declared generation experiment
   for both candidate models; update the fitted pool contract. Never call four greedy prompts
   equivalent to four beams. No runtime sampling, unbounded reasoning, or repeated repair loop.
6. Measure cold startup, prompt processing, generation, all-candidate scoring, SQL execution,
   end-to-end engine time, full complex-turn time, peak memory, and billed resource time.
   Report uncached requests and repeat-question cache hits separately. A scoring-only
   benchmark or a warmed repeated prompt is not the latency result.

Screen decision: advance a candidate that meets the runtime feasibility gates and improves
validated pool coverage by at least five percentage points on the fixed screen, or produces
at least five points of final top-1 improvement with its correctly fitted selector on a
disjoint reporting set. A 200-question screen is triage, not a generalization claim. If
neither improves, allow one preregistered fitting-side grounding/SFT pilot when the error
audit supports it; do not launch bulk relabeling or an unlimited model search.

### Proposed runtime acceptance gates

These are proposed product targets, not measured promises. Freeze them before scoring the
screen; do not relax them after seeing a favorite model's results.

| Measurement | Initial target |
|---|---|
| Warm complete engine call, uncached question, Cloud Run | median <=20 s and p95 <=60 s |
| Full complex conversation turn, including orchestration | p95 <=180 s; retain the existing 240 s end-to-end budget |
| Peak complete-container memory at 24 GiB | <=20 GiB, no OOM or swapping |
| Cold ready time | <=180 s target and within the platform-supported configured startup limit |
| Repeatability on pinned CPU/runtime | same chosen canonical SQL on 3 repeats of the fixed 50-question check |
| Active compute cost per 1,000 matched questions | <=2x freshly measured current baseline |

Measure those same timing boundaries for the baseline; historical conversational timing is
not interchangeable with engine-only timing. CPU microarchitecture can change floating-point
results, so measure local/cloud agreement and record any differences instead of promising
cross-hardware bit identity. Load-test arrivals at concurrency 1, 2, and 4 and distinguish
inference from queue time. Select Cloud Run request concurrency based on the existing engine
lock and measured autoscaling; do not assume its current setting of 8 means eight independent
reasoning requests execute simultaneously. Never share a mutable model cache across requests
without testing isolation.

### Stage 2: improve coverage with controlled data and training

Classify fitting-side failures into schema/value grounding, representability, query structure,
selection, and execution before choosing a change. The historical 93/114 diagnostic says
many missed examples have representable SQL and a candidate containing all gold tables;
it does not establish that the join paths or schema interpretation are correct.

- First ablation: deterministic, question-relevant value retrieval, types, and foreign-key
  paths in the shared prompt. Bound the values and prompt tokens, keep bridge tables, and
  log truncation as a failure/limitation rather than silently dropping parts of the question.
  Retrieval may use the supplied database, never gold SQL, gold tables, or answer-derived data.
- Train SQL adapters through `training/proposer/train_sft.py`, extending that owner for the
  pinned new model/template and verified token budgets. Fix the current 384-token truncation
  risk before larger-schema training: require intact targets and at least one supervised
  token, report exclusions, and measure length coverage. No adapter transfer from 0.5B to 7B.
- Use one seed-7 pilot with an epoch-based schedule fixed in its contract, then confirm a
  promising recipe with an additional seed (17) within budget. Freeze learning rate, effective
  batch, LoRA configuration, epochs, and checkpoint selection using fitting/development data
  before running it. Do not keep selecting checkpoints on Spider dev.
- Prioritize projection, aggregate grain, count/distinct, join multiplicity, zero-count
  entities, anti-joins, ties, literal binding, and nested comparisons. Include same-schema
  contrastive negatives. Changes to the AST/importer require positive and negative semantics
  tests and dual-emitter parity for newly supported operations; never weaken validation.
- SFT positives require fitting gold SQL or an independently specified correct query.
  Teacher-generated examples need semantic checks, including discriminating database cases
  where appropriate. Successful execution alone is never a correctness label.
- Retain a change only after paired development wins exceed losses and the product regression
  gates pass. Use +3 percentage points as the initial minimum worthwhile development gain
  for an expensive additional recipe; confirm promising gains across databases.

### Stage 3: selection on the actual deployed candidate distribution

Regenerate labels only after the proposer, prompt, precision, and generation contract are
frozen for that experiment. Start with the existing named-feature linear arbiter as the
control, refit to the new model's scores. The old coefficients, scales, token counts, and
pool provenance are not transferable.

Only when a substantial selection gap remains, run one hybrid selector experiment through
the existing ranking/training owners: combine structural features with a semantic score
conditioned on question, schema, relevant values, and candidate SQL. Use hard pairwise
negatives on fitting-side pools and out-of-fold semantic features for any learned combination.
Keep inference bounded: no quadratic tournament over every pair of 29 candidates; test a
fixed shortlist and measure correct-query loss from that shortlist. Include its complete CPU
cost in the gate. Prefer a shared-backbone or compact head design over loading a second large
inference model.

The previous standalone semantic pilot lost 210 versus 237 on the same 416 examples; the
partial canonical refits gave 225 versus 224 (d2) and 234 versus 237 (mixed). They justify
testing a materially different hypothesis, not repeating those recipes or recovering old
labels solely for an unchanged linear refit. No selector can correct absent pool coverage.

Decision gates: evaluate same-pool paired results, require at least +3 development points
over the matched selector control, improvements on a majority of development databases,
and no new product regression. Investigate losses exceeding five percentage points on an
individual database before advancing; report sample sizes and uncertainty. Treat 70%, 75%,
and 80% as observed milestones, not automatic forecasts from oracle coverage.

### Stage 4: choose the production size

- If 7B passes accuracy and CPU gates, it is eligible for the final freeze.
- If both pass, prefer 3B when its final development score is within one percentage point
  of 7B and it also meets the target; report paired uncertainty and actual cost savings.
- If 7B supplies markedly better coverage but misses CPU latency, keep it as an offline
  teacher and try one separately contracted distillation into 3B using verified fitting-side
  examples and hard negatives. The student must pass the same full evaluation; transfer is
  a hypothesis, not a guarantee. Teacher generation and distillation consume the same budget.
- If neither reaches 80%, publish the measured result and the remaining failure distribution.
  Keep the best accepted production bundle until a candidate passes promotion gates; do not
  lower the metric, omit questions, silently add a GPU, or ensemble two serving engines.

### Stage 5: freeze, verify, and replace one runtime

Integrate the chosen CPU backend by extending `engine/sql_proposer.py`; migrate its callers
and remove the replaced proposer-specific PyTorch decode/scoring implementation in the same
change. PyTorch may still serve the existing encoder. Keep `schema_prompt`, `import_sql`,
`TableQuery.select_query`, and the ranking/evaluation owners shared across all consumers.
Experiment utilities stay outside production imports and expire when the decision is made.
Model-size comparisons are sequential runs of the same pipeline with different artifacts,
not simultaneous public model versions or a user-facing model selector.

Extend `training/rank/promote.py`, `engine/fetch_weights.py`, the manifest schema/validation,
and Docker/dependency packaging coherently for the chosen GGUF model and paired arbiter.
Bind base/adapter or merged-model identity, tokenizer/template, GGUF hash, quantization,
runtime build, prompt/generation contract, selector features, and training provenance.
Replace the selected artifact format and remove obsolete proposer-only requirements and
documentation; no broad exception fallback to the former proposer. Historical baselines
remain immutable experiment evidence and source-control history.

After development is frozen, run fresh serving-path whole_db, the official evaluator,
the reserved independent evaluation once, and all required SQL, decomposition, routing,
deterministic-emitter, release, and live-Postgres dataset suites. Recheck the final quantized
CPU image, not only the training checkpoint. Investigating a failed final holdout converts
that set into development data; it cannot be reused as a fresh final claim.

For a release, verify the immutable image in a bounded Cloud Run job under the appropriate
serving identity, then replace the production service at 100% traffic using its existing
deployment workflow. No tagged candidate serving URL or split-traffic rollout. Verify every
demo prompt and follow-up in Chrome against both existing and fresh conversations after
replacement; roll back the whole bundle on a release failure. Retaining an immutable,
inactive rollback image does not introduce a second active engine implementation. Publish
the exact source, weights, license notices, reproduction commands, limitations, and model
card after the required checks. Do not claim the release before the browser gate finishes.

### Schedule, spending, and execution order

Planning estimate: first CPU/model decision in 2-3 engineering days; one complete development
cycle in roughly 1-3 weeks, depending on runtime integration and labeling throughput. Creating
an independent evaluation set can take longer. This is not a deadline or guarantee for 80%.

Proposed initial research cap: **USD 300**, released by stage, not automatically spent:

| Envelope | Maximum |
|---|---:|
| CPU/runtime/model screen, including cloud build/storage allowance | $40 |
| Controlled GPU adapter-training pilots | $120 |
| New-pool labeling, arbiter/selector experiments | $80 |
| Final CPU evaluation and release-image checks | $60 |

These are stop limits, not vendor quotes or a claim that every branch will fit. No new spend
is authorized by merely writing this proposal; the prior $25 RunPod pilot approval is not
reused for this program. Measure per-question throughput and price before launching large
shards. If the necessary next run cannot fit its envelope, report its concrete cost estimate
and result-based justification rather than spending past the cap. Keep local storage and
cloud artifact retention bounded too.

For scale, published us-central1 Cloud Run Jobs list rates of $0.000018 per vCPU-second and
$0.000002 per GiB-second imply about $0.69/hour for 8 vCPU / 24 GiB active job execution,
before discounts, build, storage, transfers, and other services. Verify rates at launch;
this is not the production service's complete bill. See
[Cloud Run pricing](https://cloud.google.com/run/pricing) and
[CPU limits](https://docs.cloud.google.com/run/docs/configuring/services/cpu).

Start each job with one task, retries disabled, a 60-minute limit, durable checkpoints,
and a known output prefix. Shard longer work by disjoint database/question manifests,
record every execution ID, and bound combined worker-hours. A billing alert is not a hard
cap: orchestration must stop launching work and terminate owned active work before the
envelope is exceeded, allowing for delayed billing and startup/artifact overhead.

Independent work can proceed concurrently: runtime integration and inference contracts;
fitting-side error/data preparation; and evaluation manifests/product cases. Selection
training waits for fixed, complete new-model pools. Distillation waits for useful teacher
results. Large cloud labeling waits for numerical correctness and a throughput measurement.
Runtime integration and final promotion have one integrator to prevent competing owners.

Every meaningful result is mirrored as a concise progress/result entry in
`chatgpt-handoff.md`; Claude's log is read before touching shared work. Keep measured scores
in `spider/results/RESULTS.md`, architecture facts in `docs/ARCHITECTURE.md`, and accepted
structural decisions in `DECISIONS.md`. The next implementation deliverable is the pinned
screen contract, fixed question manifest, and local runtime conformance check, before any
large training or relabel request.
