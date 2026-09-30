# Production-readiness workstream

## Released — 2026-09-30

This supersedes the deployment section below (`00125-jsx`) as the current release status.

Production serves main's code. Engine `prereasoner-api-00126-vjc` =
`engine@sha256:1f4f3b6adcc51d07e56c61ba51630fef488a41981901d0de5fb24a82a046f5bd`, built from `d3f7751` (Cloud Build
`abd8e42e`); `engine/`, `db/` and `regress/` are unchanged since. Chat `prereasoner-chat-00074-pjz` =
`chat@sha256:610bad83aaa7a0f75e19a41e1f5f33f305f17cecd59cbe63c6a76957c186c57c`, built from `75b420d` (Cloud Build
`dbc70617`). Each is at 100% with no tags. Firebase Hosting serves `d3f7751`'s `web/public`, which main has not
changed. The ECB refresh, retention and release-smoke jobs run the engine image. Rollback: engine `00125-jsx`
(`131edc4f…`), chat `00070-hnh` (`98109c53…`, `d67575d`).

Live now: every fix in the 2026-09-30 `DECISIONS.md` entries. That covers the Europe/GBP family, serving that decides
compose ownership with `route()` alone, an output currency that survives a complete question in between, and replies
that say only what the result shows.

Gates on the released code:

- Hermetic suites: `tests.run_all` with the engine suites off, 32 of 33, with `tests.test_complex_datasets` 7/7 on
  the 7B. The live orchestrator suite (claude-sonnet-5) missed one case in that run: "only use the top 2 customers",
  which the model sometimes sends with a decomposition before the engine asks for one, and which the runtime then
  ends with "I couldn't split this question". Replayed 60 times on each prompt, that happened 4 times with the prompt
  before today's changes and 4 times with the released prompt. It passed 37/37 on the next run.
- Engine image: offline 13/13, boot smoke, the live product suites on a disposable seed, and the CPU HTTP smoke. The
  8 live suites also passed against production data through the Cloud SQL proxy.
- Chrome gate, run in the owner's signed-in Chrome on chat.prereasoner.com. It covered every shipped dataset, through
  `?load=` or an upload, in order, one conversation at a time.
  - Fresh conversations: 80/80 turns over 24 datasets (shorthand 14/14, FX 17/17).
  - Conversations created before the release: 55/56. The miss was payment-commissions "how much commission came
    from cards?", which clarified. Replayed twelve times with that conversation on claude-sonnet-5, the model sent the
    shorthand verbatim once. The engine reads the literal question as a digital-wallet total converted at ECB rates
    and asks a question back. Asked again in the same conversation, it answered 9.28.

The authenticated production conversation gate that the section below keeps open ran in this pass, on conversations
the gate created. About a hundred gate conversations remain in the owner's account.

Open, for the owner (capacity and cost; nothing here was changed):

1. `min_instances = 0`. After about 15 idle minutes the engine scales to zero, and loading the 7B takes 5 to 7
   minutes. The first question then fails in the chat ("the assistant hit an error" after the 240 s turn). One warm
   8 vCPU / 16 GiB instance costs about $460 a month at on-demand rates.
2. One question at a time per instance, with a 15 s admission window. Three conversations asked at once, and 2 of the
   first 6 turns came back "Engine is busy". The chat now reports that plainly instead of promising a retry.
3. Autoscaling on CPU during a long decomposition (60–120 s) starts up to two more instances, and each takes about 7
   minutes to load. Requests routed to them meanwhile waited 60–80 s or failed at the Hosting proxy.
   `max_instance_count = 1` avoids this at no cost but caps throughput. Warm capacity costs as in item 1.
4. The daily ECB refresh (16:30 UTC, about 4.5 minutes) held a lock that stalled one FX question for 49 s.
5. The shorthand miss above. It would not happen if the engine read "cards" as the `card` payment instrument, since
   the question would then be answered even when it arrives verbatim.
6. A decomposition the model proposes before the engine asks for one ends the turn at once (no repair round), about
   1 time in 15 for the category-gaps cutoff follow-up in replay. A repair round that says to ask the question first
   would recover it.

## Deployment completion and remaining live-browser gap — 2026-09-30

This is the current release status and supersedes the dated pre-deployment checkpoints below.

PR #35 merged at `a5d42109bd7389b5b76fe35a85af59a76dcfef15`; Cloud Build
`5a79b52d-236a-490f-a27d-6de3d9708112` published immutable engine image
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:131edc4f683e8694e564f69f2ac3e169338a32d8ed4868810eb519c8c7fd0f1f`.
Runtime startup/health, offline regression, and all configured live product suites on disposable
PostgreSQL passed. The reviewed Terraform apply changed only the API service and two engine jobs
(0 add / 3 in-place / 0 destroy). No SQL instance, deletion-protection, or orchestrator resources
were modified by that apply.

Production now serves revision `prereasoner-api-00125-jsx` at 100% traffic. Its digest matches
the build above; `GET /api/healthz` returns all four health flags true. Startup took 9m45s total,
with the container healthy at 5m50s, within the 10m10s startup-probe budget. Prior revision
`prereasoner-api-00122-zc4` (`82f8f154…`) is retained at 0% for rollback. Observe cold-start timing;
passing readiness does not make the remaining delay disappear.

The orchestrator's `GET /readyz` returns HTTP 200. The exact-image bounded CPU API smoke measured
world-join latency at serial n=3 p50 1.304s and p95/max 8.849s; concurrency 2/4/8 p50
1.484/2.801/5.212s and max 2.692/5.267/9.990s, with 6.288 GiB/16 GiB container memory.
Small smoke samples are not an SLA.

Browser regression passed **33/33** with the repo's synthetic fixture backend. A bounded
two-turn provider-authenticated orchestrator check passed **5/5** assertions using synthetic sample
data, a local stub engine, and the existing Anthropic service credential. These are not a live
Firebase-authenticated browser request to the production orchestrator. The isolated browser tab
had no usable Firebase session token and the upload picker did not open through the browser bridge;
no production conversation or user data was created or changed. Keep the authenticated production
conversation gate open until it is run with an explicitly disposable, valid test identity and
verified cleanup. Do not use an existing user conversation as a substitute.

The post-apply full Terraform plan still reports API scaling and chat-service configuration drift.
It was reviewed but not applied because that would make unrelated configuration changes outside
the approved engine rollout. The targeted apply completed successfully; Terraform's output values
may remain stale until a separately reviewed reconciliation. Current accuracy evidence remains
865/1,034 strict and 832/1,034 official Spider test-suite on the repeatedly consulted Spider DEV
set; neither is an untouched generalization estimate.

## Pre-deployment gate plan (historical checkpoint — superseded 2026-09-30)

This section supersedes the dated historical checkpoints below. The last deployed 7B bundle
is recorded at revision `prereasoner-api-00122-zc4`, digest `82f8f154…`, with 100% traffic.
Its CPU DEV evidence is **864/1,034 strict (83.56%)** and **839/1,034 official Spider
test-suite (81.14%)**. These are repeatedly consulted DEV engineering results, not an untouched
generalization estimate. Official TEST remains untouched. The encoder is still 0.5B; the SQL
proposer is XiYanSQL 7B Q4_K_M. The selector is explicitly NOT model-matched.

The previous claim that post-deployment checks were complete was too broad: fixture browser
tests, health checks, and disposable-Postgres product tests did not verify every authenticated
production demo and conversation follow-up. That gate remains required.

User authorization: implement the reviewed fixes and proceed through testing, refactoring,
merge, deployment, and browser regressions. Do not weaken acceptance to meet a morning deadline.
Preserve Claude/main and research worktrees; work on `codex/7b-release-hardening` from `e50b4a5`.

Execution sequence and fallback decisions:

1. **Correctness and reproducibility.** Fix exact decimal thresholds, incomplete completion
   rejection, context admission, cache synchronization, tokenizer provisioning, suite timeouts,
   stale ownership/docs, obsolete loaders, and test-only decomposition filtering. Every fix gets
   a registered regression. Keep the encoder and one active CPU SQL proposer.
2. **Faithful selection data.** Save structural origin, exact scores, execution/grounding
   eligibility, and gold-blind calculation/money constraints. Replay the shared serving rule.
   Require matching source/model/data contracts on resume and full expected denominators,
   including failed questions. Reject incompatible historical pools rather than recycling them.
3. **Matched arbiter pilot.** Freeze the 7B prompt/quantization/neutral-scoring configuration;
   generate TRAIN-only pools with database-disjoint selector fit/validation. Keep the deployed
   arbiter as a paired control. Preregister the sample and seed before labels. Pretrained XiYan
   exposure is unknown: this is held-out selector validation, not proven end-to-end independence.
   Expand labels only on useful held-out wins without product regression. If the fitted arbiter
   loses, retain the measured baseline and diagnose coverage/selection; do not install a worse
   model just to remove the mismatch flag. Real likelihood scoring is a separate experiment,
   accepted only if accuracy gains justify CPU cost.
4. **Runtime and bundle.** Extend the existing promotion owner for one hash-bound GGUF/tokenizer/
   selector contract. Validate the full staged bundle before a release image is published.
   Measure uncached own-data requests and model queue/decode time, memory, cold startup, and
   concurrency 1/2/4/8; repeated cached world joins do not establish 7B capacity. Add bounded
   admission/cancellation without leaving native inference running after a returned timeout.
   If saturation fails, reduce per-instance admission instead of accepting unbounded queues.
5. **Freeze and evaluate.** New serving-source fingerprints invalidate cached DEV predictions.
   Run all 1,034 questions through the exact CPU-serving configuration. Require no regression
   from 864 strict and 839 official test-suite unless a separately justified correctness change
   is explicitly accepted; at minimum both benchmark metrics must remain over 80%. Report paired
   wins/losses, difficulty/database strata and tail latency. No gold-value substitution.
6. **Release.** Full configured hermetic/live product gates, authenticated orchestrator gate,
   exact-image CPU and browser tests; merge only a verified source tree. Deploy one image to the
   existing service, retain the previous immutable revision for rollback, and run every shipped
   `prompt.txt` plus every `eval.txt` (including `chat:`) in Chrome on fresh AND existing test
   conversations. Roll back on release-blocking regression. Record image/commit/model hashes,
   measured results, outstanding limitations, spend, and worker cleanup.

#### Latest full replay and release status — 2026-09-29

The clean-source hardening replay completed all 1,034 Spider DEV rows with the frozen served
arbiter and CPU XiYanSQL Q4_K_M: **865/1,034 strict (83.72%)**, **868/1,034 lenient (83.95%)**.
The official Spider test-suite score on those exact SQL predictions is **832/1,034 (80.46%)**,
using pinned evaluator commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c` and no gold-value
substitution. This is a repeatedly tuned DEV engineering result, not a generalization estimate.
Paired with `clean-engine-q4-8t`, it gains four strict examples and loses three (861 unchanged
correct, 166 unchanged wrong); 23 SQL outputs differ. On those 23, official test-suite scores
are 15/23 current versus 14/23 control. The older 839/1,034 official score describes another
historical deployed record, not this paired control.

The correctness target passes, but the release does **not** pass CPU latency: full replay p50/p90/
p95/max are 19.335/37.111/42.834/61.705 seconds and 893/1,034 exceed the 12-second soft target.
Four rows fail execution: three connected-AST search misses in `car_1` and one proposer CPU
decode-budget failure. The model-matched selector pilot was rejected (no held-out gain), so the
candidate still uses the frozen neutral-score arbiter. The latest attested candidate image is
`sha256:da66e69be2c73b71c1200a006cb439f785ae9f8a30e313619f597504db8cc0a6`; its startup, health,
disposable-PostgreSQL product suites and bounded API smoke passed, but authenticated conversation
follow-ups were skipped for lack of an external-model credential. Browser Playwright passed 33/33
against a synthetic local backend only. No merge or traffic change has occurred: the production
service remains at 100% on `prereasoner-api-00122-zc4` (`sha256:82f8f154…`).

A paired 32-question experiment with `SQL_PROPOSER_THREADS=16` preserved all 32 selected SQLs
and labels relative to eight threads. On that subset, p50/p90/max improved from 14.781/26.544/
35.609s to 12.271/22.146/28.839s, but 18/32 rows still exceeded 12 seconds. This was a local
host with 16 logical CPUs, not the production 8-vCPU Cloud Run shape; it does not justify a
thread override or establish production latency. The full 8-thread replay remains the accepted
accuracy result.

Therefore the accuracy objective is achieved, while the production objective is **not yet ready**.
The immediate work is a bounded 8-vCPU exact-image tail/concurrency test, resolving or explicitly
waiving the 12-second target, and obtaining a credentialed authenticated-orchestrator/browser run
without touching real user data. Preserve the current revision until these gates are resolved or
explicitly waived. Full provenance and results are in
`spider/results/RESULTS.md` and `chatgpt-handoff.md`.

#### 2026-09-29 pilot decision

The preregistered CPU pool build completed with 240 unique TRAIN questions (24 per database),
using the pinned XiYan Q4_K_M proposer (`50840d65…`, CPU), neutral-sentinel likelihoods, one
greedy 1,024-token proposer beam, and deterministic search budget 25. The pool input SHA256 is
`2e98974a4c8b1308afef40e7b9f105631aba7e1419e6832eb2d7f077a3be46bd`; source/model/split details
are recorded in its header. One malformed Spider TRAIN gold query (idx 4514, invalid ORDER BY
placement before INTERSECT) remains in the 240 denominator and was not assigned candidate labels;
239 questions were labeled. It is a data-quality exception, not a model miss.

The selector was fit on 144 questions from six databases and evaluated on 96 questions from four
disjoint databases. This is selector-DB holdout only; XiYan pretraining exposure to those DBs is
unknown. The candidate and frozen production arbiter each selected **82/96 strict-correct SQLs**;
paired comparison was **0 wins / 0 losses**, with 85/96 eligible pool-oracle hits. Every validation
database was unchanged (customer_complaints 19/24, program_share 24/24, student_1 21/24, wine_1
18/24). Therefore the preregistered gain gate failed: **do not promote or install the fitted
arbiter**. Its experimental file SHA256 is
`e37d48dd50fe380c2d220cf0fd3b577e94d0ab9810f84e29d7e7f47fad129061`; pool SHA above. The
validation result is a diagnostic on this sample, not a new full-DEV score or unbiased estimate.

Next: freeze the existing arbiter and run a fresh, full 1,034-question CPU DEV replay from a clean
source commit with current serving code, then evaluate those exact saved SQL predictions with the
official Spider test-suite evaluator (without gold-value substitution). The prior 864/1,034 and
839/1,034 records are historical evidence, not substitutes for that replay. No merge, deployment,
production browser gate, or official Spider TEST access follows from this pilot.

### Preregistered neutral-selector pilot (before labeling)

- Hypothesis: a selector fitted on this exact 7B/neutral-score candidate distribution improves
  selection over the frozen historical arbiter without changing proposals or eligibility.
- Baseline model SHA `50840d65a753074a670d7929ca0a4b5d633b0a4b435f1a68b4a6fba26c4d18bb`;
  arbiter SHA `fc84162a4dc9900f963bbea751ccf13f2d9218842a0f781ee1cb7e964584977d`.
- Public `train_spider.json` SHA `c43d0d72e59e1a9e1a60837da9bf70d5a6277226bdb7f634d544f380646f527a`.
  First ten DBs with >=24 examples sorted by SHA256(`xiyan-neutral-pilot-7:` + db_id).
  Fit: soccer_2, loan_1, document_management, company_office, chinook_1, voter_2.
  Validation: wine_1, program_share, customer_complaints, student_1. Pick 24 indices per DB
  by SHA256(`7:` + db_id + `:` + index): 144 fit / 96 validation, fixed before results.
- Objective: standardized logistic strict-correctness prediction, seed 7; only coefficients
  change. Generation remains one greedy CPU Q4_K_M completion, neutral `(0,1)` scores.
- Expand only for >=3 net validation wins, no lost eligibility/oracle coverage, and no DB
  losing more than two questions. Report wins/losses and uncertainty; this small pilot cannot
  establish generalization or justify promotion by itself. On failure, do not bulk-relabel.
- Output: `training/rank/data/experiments/xiyan-neutral-pilot/`. No promoted weights overwritten.
  Final acceptance still requires full CPU DEV strict >=864 and official test-suite >=839 plus
  product/browser gates. Rollback is the current immutable production image.
- First run is local CPU (no RunPod charge). If throughput is prohibitive, price a bounded
  public-data-only RunPod lease after reconciling existing budget; do not invent new funding.

## Historical checkpoints (superseded for current deployment status)

## Combined-tree release gate (2026-09-28)

An isolated merge rehearsal at `codex/merge-readiness-rehearsal` combines current `main`
(`6d8b7dd`) with `codex/prod-readiness` (`a1ece3e`). The rehearsal resolved one real conflict in
`tests/test_world.py` (duplicate reasoner setup) and corrected one stale deploy-contract assertion
in `tests/test_community_deploy.py`. The combined tree is at `ba130b77ab5a67bfb800f1751c2b0628923fe811`
and has 0 commits behind `main`; no change was made to `main` or the product-readiness checkout.

The full configured offline suite passed with the model bundle present: all 31 configured suites,
including `test_complex_datasets` (7 passed, 0 skipped), schema coverage, release, and community
deploy tests. Focused reruns passed `test_community_deploy` 21/21 and `test_release` 40/40.
`git diff --check` passed. The rehearsal also corrected the stale example command in `cloudbuild.yaml`
to pass `--target release`.

Fresh exact-source release Cloud Build `1e04710b-2b5e-4c9e-a490-1fb25126d952` completed **SUCCESS**
in 35m23s from rehearsal commit `ba130b7`. Its image passed the offline product regression, runtime
startup/health, all configured live suites against disposable PostgreSQL 16/pgvector restored from
the checksum-pinned public seed (world joins, nongeo, routed, geography, schema probes, and datasets),
and the live CPU API join smoke. CPU API world-join latency was serial n=3 p50 1.276s/max 4.954s;
concurrency 2/4/8 p50 1.502/2.805/5.197s and max 2.773/5.284/10.496s. Container memory was
6.699/16 GiB; process RSS 6.86 GiB. These are bounded samples, not an SLA. External authenticated
orchestrator and production Cloud SQL were not exercised.

The gated build published the uniquely tagged candidate
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine:merge-ba130b7`, digest
`sha256:6868b1b869caff21257dc05f6853cd1528c289fdea2a2c6979311acf98f4a219`. Read-only verification
afterwards confirmed production remains revision `prereasoner-api-00236-noz`, 100% traffic, image
digest `sha256:3cbb0037832a06630dc1e0d0a450e44b415e4e5f0ac4ef92b7607de2862891dd`.

**Decision boundary:** this is strong merge/release evidence for the existing 0.5B product path. It
does not contain or validate the separate 7B Spider candidate, its real-likelihood selector, or its
latency; that candidate still cannot be promoted. No merge to `main`, Terraform apply, Cloud Run
revision creation, or traffic change occurred. The image tag is a gated rehearsal artifact only.

Status as of 2026-09-28: the full public-seed product suite and bounded CPU API check passed
against the exact current production image `3cbb003…` in Cloud Build
`17436fb4-3251-433d-9966-37da60eb9551` (source `39b1aa9`). Full hermetic validation also passed
against that digest on latest source `016c83d` as `ec4f7b47-7f51-46f8-8354-f0230d12793d` (31
configured suites). A bounded concurrency-8 CPU smoke also passed as
`ae908abb-962c-4e70-ac5a-38401fa79d9c`. Nothing has been merged, promoted, or deployed. These
gates validate the currently serving candidate only;
release remains blocked for any claim that it ships the +10.4 Spider DEV points because the 7B
artifact still has no pool-matched selector/runtime package in this branch. The separate 80%
Spider accuracy work remains in its own worktree.

### Latest release work (2026-09-28)

- **Exact-current-image live product gate passed:** Cloud Build `17436fb4-3251-433d-9966-37da60eb9551`
  ran source commit `39b1aa9b94797d81ff580362339040283e2be128` against production image digest
  `sha256:3cbb0037832a06630dc1e0d0a450e44b415e4e5f0ac4ef92b7607de2862891dd`. Disposable PostgreSQL
  16/pgvector was restored from the pinned Community seed and refreshed with ECB data. Offline,
  world, nongeo, world joins, router, geo, schema-probe, and full dataset prompt/follow-up suites
  all passed. CPU-only `/api/reason` world-join calls all matched expected answers: serial n=3
  p50/max `1.214/4.251s`, concurrency 2 n=6 `1.187/2.374s`, concurrency 4 n=12 `2.531/4.859s`.
  Container peak was `6.693/16 GiB`; process peak RSS `8,024,748 KiB` (~7.65 GiB). Small-sample
  p95 equals max and is not an SLA. Python `3.11.16`, Torch `2.13.0+cpu`, Transformers `5.10.4`,
  spaCy `3.8.13`, sqlglot `30.18.0`, and psycopg2-binary `2.9.12` were verified; `pip check`
  exited 0. The build produced no image and changed no traffic.
- **Exact-current-image hermetic gate passed:** Cloud Build `66370892-0dd4-4a3c-84ce-237c41e35524`
  used the same digest and source commit. All **31 configured suites exited 0**, including complex
  dataset, MCP, and orchestrator unit tests. `RUN_ENGINE_TESTS=0` and
  `RUN_ORCHESTRATOR_TESTS=0` are explicit in the hermetic harness, so live external-engine and
  authenticated external-orchestrator behavior are not covered by this gate.
- Because the CPU runner changed after that first exact-image hermetic pass, it was rerun at latest
  source commit `016c83dce6f973da7c5546950f8002237adb8e69`: build
  `ec4f7b47-7f51-46f8-8354-f0230d12793d`, **SUCCESS**, all 31 configured hermetic suites exit 0.
  The live dataset/product acceptance remains `17436fb4-3251-433d-9966-37da60eb9551` at source
  `39b1aa9`; intervening changes only expanded the smoke runner/test and documentation.
- **Configured CPU concurrency cap checked:** Cloud Run's current service config is 8 vCPU / 16 GiB,
  container concurrency 8, max scale 3. The initial smoke (`d1cd9876-d700-46cd-8170-d7eefa9eac8f`)
  sent 45 calls under one principal per minute and correctly received HTTP 429 at the engine's
  30/minute limiter; this was a test-rate-budget bug, not an application capacity failure. The test
  was corrected to stay within the limiter and rerun as CPU-smoke-only build
  `ae908abb-962c-4e70-ac5a-38401fa79d9c` (source `423535b`). It passed all **8/8** simultaneous
  read-only world-join requests at concurrency 8. At concurrency 1/2/4/8, p50 was
  `1.367/1.578/3.768/5.435s` and max `5.182/3.078/6.505/10.729s`; the eight-sample p95 is its max.
  Process peak was ~7.64 GiB; container snapshot `6.709/16 GiB`. This is one bounded burst, not a
  sustained-load, multi-instance, or latency-percentile SLA. The smoke-only build skipped product
  modules already passed in the full live gate and produced no image.
- These results validate the already-serving candidate, not the 7B Spider +10.4 DEV-point change.
  A model/pool-matched 7B selector and verified runtime artifact remain necessary before that result
  can enter this release path. Authenticated external-orchestrator coverage, production-database
  acceptance, operational approval, and a sustained CPU load/SLA target also remain open; no
  production deployment is authorized by these test results alone.
- **Verified model-contract mismatch:** this branch pins `Qwen/Qwen2.5-0.5B` in
  `engine/model_revisions.py` and its existing adapter/artifact hashes in
  `engine/data/weights_manifest.json`. The +10.4 Spider DEV result belongs to the separate 7B
  experiment; its matched proposer pool, selector, and runtime artifact are absent here. Thus a
  merge of this branch alone would not ship or reproduce that measured 7B delta. Do not label this
  current-image acceptance as 7B promotion evidence.
- Read-only production verification after the builds still shows revision
  `prereasoner-api-00236-noz` at 100% traffic; `/api/healthz` reports `ok`, `reason`, `world`, and
  `dimension` healthy. The disposable product suite did not query production Cloud SQL. Parallel
  local accuracy jobs were left untouched because their Python processes were using about 12 GiB
  combined at the time of inspection.

- **Previous release-image live product gate passed:** Cloud Build `9c12a8ab-55dd-4469-817a-876c93b75db7` ran
  source commit `78dbcd91bc10a8f0f5eeefc3de21705f58fd385e` on an `E2_HIGHCPU_32` worker. On the
  release image `us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:57d49a5da4e5ec2fa03881969e9424bb5032584aaf94bc581b6bbfc74aeb8482`,
  the Python 3.11 runtime check passed `pip check` and
  reported Python 3.11.16, CPU Torch 2.13.0, Transformers 5.10.4, spaCy 3.8.13, sqlglot 30.18.0,
  and psycopg2-binary 2.9.12. Against an isolated PostgreSQL 16/pgvector instance restored from the
  checksum-pinned public Community seed and refreshed with ECB release `2026-09-25`
  (`dfe9ab070c0679d42a174981e07b240131d2db19d20108a5d0e4653d9f61fc9f`), migrations/grants,
  curated world goldens, all configured live modules, and the full `tests.test_datasets` prompt and
  follow-up matrix passed. The actual CPU-only HTTP entrypoint returned the expected France world
  join on all three calls. At 8 vCPU / 16 GiB, call latency was p50 **1.18 s**, p95/max **4.29 s**;
  container memory was **6.63 GiB** and process peak RSS **7.64 GiB**. This small smoke is not an
  SLA or concurrency test. The authenticated external-orchestrator path remains explicitly
  untested, and the disposable public seed is not production Cloud SQL. No image was promoted and
  no Cloud Run traffic/configuration changed.
- The follow-up CPU-only concurrency check is isolated in Cloud Build
  `c0f5e9d4-2d6c-412f-a522-6c2200b37afe` (commit `b7e97a9`) against the same pinned image and
  disposable seed. It passed all **21** exact-answer world-join requests: concurrency 1, n=3,
  p50/max **1.20/4.68 s**; concurrency 2, n=6, p50/max **1.28/2.49 s**; concurrency 4, n=12,
  p50/max **2.50/4.96 s**. Container snapshot was **6.70/16 GiB**, process peak RSS **7.66 GiB**.
  p95 equals max at these small sample sizes. This is a bounded CPU resource smoke, not sustained
  load, saturation, or an SLA; the full live suite was not rerun in this smoke-only build because
  it passed in the preceding build.
- Scope boundary: both runs exercised the existing **0.5B mainline serving image** and prove its
  isolated product regression/runtime only. They are not the 7B Spider DEV experiment or its
  +10.4-point diagnostic delta. That 7B bundle still lacks a selector matched to its candidate pool
  and a verified production runtime package; this branch has not packaged or promoted it.
- Exact-current-revision check found production revision `prereasoner-api-00236-noz` uses image
  digest `sha256:3cbb0037832a06630dc1e0d0a450e44b415e4e5f0ac4ef92b7607de2862891dd`, whereas the
  completed live suite above used `57d49a…`. Both product and hermetic build configs are now
  retargeted to the current production digest; do not treat earlier results as exact-current-image
  acceptance until both reruns pass.

- Live build `d635705f-58e5-401a-bf7a-c580fcd92af3` passed offline and the two curated world
  goldens, then reported SIGKILL (`-9`) for `tests.test_world`/`tests.test_geo` and a 600-second
  timeout for `tests.test_datasets`. `tests.test_world_joins` passed **6/6** and `test_route_wired`
  passed. Geo/data tests revealed the static Community seed's ECB projection had no row for the
  current `as_of` date; FX conversions safely clarified rather than fabricating rates. API CPU smoke
  did not run because this runner revision stopped on the suite failure. Next-run changes: use a
  32-GB Cloud Build worker while retaining the production-matched 8-vCPU/16-GiB app container;
  refresh the public ECB release in the isolated database; raise the per-suite timeout to 1,800s;
  preserve suite failure but still execute the CPU HTTP world-join smoke; and make the world test
  reuse one `KnowledgeReasoner`/router instance as serving does.
- The full clean-image hermetic rerun now passes. Cloud Build
  `3e11903b-948f-4d3e-9279-ebc1e8ed5df8` used the digest-pinned 0.5B serving image
  `sha256:57d49a5d…` plus its model bundle and the repository's hash-locked CI-only test dependencies;
  all **31 configured suites passed**, including `tests.test_complex_datasets` **7/7** and the
  Schema.org coverage ratchet **15/15**. The live Postgres/world and external-orchestrator suites
  were explicitly disabled, so this is not live-product acceptance. No serving image was built or
  pushed by this test-only build, and no Cloud Run traffic changed. The Node helper resolved to
  `node:22-bookworm-slim@sha256:43ac6c60…`; that exact digest is now pinned in the harness.
- Commits `533d7b0` through `f03a11b` fixed the harness rather than suppressing product tests:
  it now uses a clean allowlisted Git archive, avoids applying `.gcloudignore` twice, keeps the
  model weights from the base image, overlays current source, installs the CI lock only in the
  disposable runner, and stages a synthetic Git commit for provenance tests without shipping
  history. The formerly failing `WEIGHTS` import test was corrected to assert against the
  manifest-driven fetch list; the request-limit test now supplies a fake key to its mocked chat
  path. Focused release tests pass **38/38**, request-limit tests **18/18**, and schema coverage
  **15/15**.

- A separate live-product gate is wired locally: it restores only the public Community seed into a
  temporary, pinned PostgreSQL 16/pgvector Docker container inside Cloud Build, applies the normal
  migrations/grants, requires the world regression tier and all live engine/dataset suites to run,
  and sends three CPU-only `/api/reason` requests through the actual server at the 8-vCPU/16-GiB
  container limit. It leaves the production Python 3.11 package set unchanged and tears down its
  DB/container/network on exit. Local release-contract tests pass **38/38** and the runner passes
  `bash -n`; the Cloud Build live gate is the next execution, not yet a result.
- First live attempt `5d25edb8-ac1b-4443-a3fe-42b31cd17311` restored the seed, passed the model-bundle
  load and all **13/13 offline cases**, then exited **137** during world-tier model loading. The
  actual cause was the regression runner retaining its offline model while constructing a second
  model for the live tier; I added explicit release/GC between tiers and lowered PostgreSQL's
  cgroup budget after its seed-only HNSW build.
- Second attempt `5d9f8849-c7c7-4a42-b33c-c07eba0ab227` passed that OOM point and entered the world
  tier, but emitted no further logs after model initialization for over ten minutes. I cancelled the
  disposable build to avoid an unbounded billable worker. The gate now separates the curated world
  regression from the full live-suite list and has per-regression/per-suite timeouts; the next run
  should identify a slow module or a real product failure rather than stall silently.

- Third attempt `f736404e-20cf-428c-a768-eba399d74be4` passed restore and the offline tier, then
  failed both curated world requests before SQL execution: the suite container forced
  `AUTH_TEST_SUB=localdev`, causing `live_schema()` to mistake a bare fixed ID for an existing
  caller-owned conversation. The serving role correctly rejected writes to `chat.working_table`
  without a `chat.conversation` row. I removed that override only from the live-suite container
  (the separate HTTP smoke retains its auth bypass), and added a regression assertion. The runner
  now uses the registered, random production-shaped conversation lease already implemented by
  `regress.live_schema`. Local release contracts pass **39/39**; the disposable live gate must be
  rerun before any live-world or dataset result can be claimed.

- The public mainline bundle was fetched in this checkout only from the immutable source in
  `engine/data/weights_manifest.json`; Python 3.11 validated its complete fingerprint as
  `1400e39e1dec7da7ca0648c5c8e4bbcc41417a0ede1df39acf69cd4a5c7956ce`.
- `engine.fetch_weights` now refuses repository/revision overrides and mutable revisions. The
  focused release suite passes 35/35 after this change; Python 3.11 compiled `engine`, `db`,
  `regress`, and `tests` successfully.
- The complete offline regression subsequently passed: engine invariants, Schema.org interpreter
  bundle load, and 13/13 non-world product cases. Its final line says `world tier skipped`; it is
  not full product acceptance. The earlier attempt that stopped below 1 GiB did not count as a pass.
  The separate 7B job was not modified.
- The former live dataset-test worker is no longer running. This checkout has no
  `KB_PG_PASSWORD`, and localhost port 5432 currently refuses connections; live Postgres/world
  acceptance therefore remains blocked on its seeded database and credentials.
- Docker and WSL are unavailable in this Windows environment, so clean target-image verification
  was performed remotely through Cloud Build. For the current 0.5B mainline bundle, Cloud Build
  `4a688469-8c85-421a-a20f-b8bdff9a0afc` built commit `4755ee67ce53f160d931eeadb5d08273a33bf623`
  on the pinned Linux/Python 3.11 image, installed the full hash lock, and passed the in-image
  offline gate 13/13. The verified image digest is
  `us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:bd5a2be1435eab883b25a1908b973f29211d998bed7728c466cef4c266185330`.
  It is tagged in Artifact Registry only—no Cloud Run revision or traffic change—and contains the
  matched 0.5B bundle, not a 7B model-matched selector.
- After that build, the offline startup smoke was strengthened to import `engine.server` itself,
  not only its helper modules. Commit `938adba9791501f3977b775403ceeda750640782` passed in clean
  Cloud Build `49c564f7-a0c6-4422-a028-ae4ff01dc74a`: server/startup imports passed, the model bundle
  and interpreter loaded, and the offline product gate passed 13/13 (world/live tier skipped). Its
  Artifact Registry digest is
  `us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:129f2614d97a86bc94edcaeca4826800c868b749fa484dad69b0aa837223779e`.
  This is an isolated image tag only; no Cloud Run revision or traffic was changed.
- The release pipeline then gained a non-deploying runtime health smoke. Commit
  `d30d1872d8ca4b2c3730f5d89b90fdfa6240c972` passed Cloud Build
  `11282f3a-6606-4f36-a7db-a11176110a1b`: offline regression 13/13, then the image's real entrypoint
  loaded the engine and returned `ok/reason/world/dimension=true` from `/api/healthz` in 22 seconds.
  Its immutable digest is
  `us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:05209bc7baf01efaed96445366042c5da4cc7e2f67e685835dd79e0d323e194a`.
  This health route reports loaded components, not database connectivity. It was pushed under a
  unique Artifact Registry tag only; it was not deployed.
- Commit `597300ad10e4e8f17328345a9cdf53c423362071` constrained offline and runtime containers to
  the configured Cloud Run limits (8 vCPU / 16 GiB) and added per-case latency/peak-RSS reporting.
  Cloud Build `3477f8e6-5a02-49b0-a21e-17a1f680fdf4` passed offline product cases **13/13** at p50
  **5.28s**, p95/max **12.84s**, with Python process peak RSS **5,373.4 MiB**, then started the real
  server and passed `/api/healthz` in **16s**. Its digest is
  `us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:57d49a5da4e5ec2fa03881969e9424bb5032584aaf94bc581b6bbfc74aeb8482`.
  These results are for the 0.5B mainline bundle, not the 7B diagnostic; 13 cases do not establish
  a hard request SLA. Read-only inspection shows production revision `prereasoner-api-00236-noz`
  uses a different image digest (`3cbb0037…`) and is configured for 8 CPU / 16 GiB. This workstream
  did not alter its traffic or configuration.

## What is measured

- Main's shipped 0.5B/arbiter bundle remains the reference product path. In this worktree,
  `python -m regress.run_regression --offline` passed its invariants, model-bundle check,
  and all 13 offline non-world product cases. The live Postgres/world tier was skipped;
  this machine has no `KB_PG_PASSWORD` configured.
- The latest clean-image `tests.run_all` rerun passed all 31 configured suites with live-engine and
  external-orchestrator tiers explicitly disabled. The complex model-backed suite passed 7/7.
  This supersedes the earlier memory-interrupted attempt; it does not supply live Postgres/world
  evidence. Focused SQL AST tests remain 124/124.
- The release/provenance suite now passes **35/35** after making artifact downloads manifest-driven
  and rejecting traversal/absolute artifact paths. This ensures a future package's declared files
  are actually fetched; it does not certify the current unmatched GGUF bundle.
- After adding the runtime-server smoke contract, the release/provenance suite passes **36/36**;
  refreshed SQL AST, provenance, and decomposition suites pass **124/124**, **11/11**, and **14/14**.
- After that change, the focused provenance tests passed **11/11**, SQL AST/runtime tests
  **124/124**, and decomposition tests **14/14**. Earlier checks found localhost PostgreSQL
  accepting connections, but the latest check on 2026-09-27 found it down and the
  `tests.test_datasets` process gone. No final result from that process is available, so it is not
  counted as a pass.
- A 7B pool-oracle screen remains active in the accuracy worktree. The first clean offline
  regression attempt against the fetched mainline bundle initialized its model, then was
  interrupted by this worktree when free memory fell below 1 GiB. The screen was left untouched;
  the full offline gate must be rerun after it exits. Its latest readable checkpoint is **570/1,034**
  at 22:45 local time on 2026-09-27; this is an oracle diagnostic, not serving accuracy.
- The branch's best 7B Spider DEV diagnostic is 755/1,034 (73.0%), versus the main Spider
  baseline 647/1,034 (62.6%): +108 questions, +10.44 percentage points. This is not a
  product-regression result or an unbiased holdout result.
- The 7B diagnostic used one prompt variant, neutral likelihoods, and the existing arbiter
  fitted on the 0.5B proposer with a four-beam pool. It is not a model-matched serving bundle.
  The measured local one-variant proposer median was 8.84 seconds; 149/200 calls met the
  12-second soft target. This is not a full-engine or Cloud Run latency result.
- A self-contained diagnostic bundle was built from the frozen 7B base/LoRA and tokenizer, with
  artifact hashes pinned in its runtime and weight manifests. With `n_gpu_layers=0`,
  `python -m regress.run_regression --offline` against it passed 13/13 non-world fixtures.
  This is a candidate-specific CPU integration check, not the full acceptance gate: the copied
  arbiter was marked unmatched, the run used explicit test mode, and the live Postgres/world
  tier was skipped. The focused SQL AST/runtime contract suite passed 124/124. llama.cpp emitted
  CPU_REPACK fallback notices for LoRA tensors; generation completed, but this run established no
  latency or memory SLA.
- After fixing the bundle builder and decomposition fallback below, the self-contained 7B
  diagnostic bundle passed `tests.test_complex_datasets` **7/7** and
  `tests.test_schema_coverage` **15/15**. Its offline CPU product regression passed **13/13**.
  A subsequent full `tests.run_all` run passed 30 configured suites; the complex suite's final
  two model-backed cases could not initialize llama.cpp (`Failed to load model from file`) while
  a separate 7B CPU-screen process was consuming memory. The same complex suite passed 7/7 as a
  standalone run before that memory-contended full run. This full-suite run is therefore not
  green and must be repeated on an adequately provisioned idle runner. The default `engine/data`
  bundle is absent from this isolated checkout (its model files are ignored), so its regression
  cannot be replayed here without importing files from another worktree. No live Postgres/world
  result is available.

## Refactor work in this branch

The serving call site remains one `SQLProposer` interface. A hash-pinned GGUF CPU runtime is
dispatched through its runtime manifest; the existing HF/PEFT bundle remains the default
when no GGUF runtime manifest is present. The runtime path validates artifact hashes, exact
beam/token contracts, SQL import/AST validation, and preserves multiline SQL. It preserves
the model's declared chat-template thinking mode rather than silently changing it.

The loader now verifies the proposer identity against the arbiter's fit provenance. For GGUF,
the runtime manifest must also be pinned by `weights_manifest.json`; an artifact cannot assert
`model_matched_arbiter=true` unless its adapter identity and likelihood protocol agree with the
arbiter fit. Unmatched GGUF/arbiter pairs are refused outside explicit development/test mode.
Regression coverage includes these refusals, hash pins, exact candidate counts, and multiline
output handling.

The artifact fetcher previously duplicated the current nine download paths in source, even though
the authoritative bundle contract is `weights_manifest.json`. That would make future matched
packages with a GGUF base/adapter or an additional selector fail on a clean install unless the
fetcher were separately edited. It now derives downloads from the manifest's hash-pinned `files`
map and rejects absolute/traversal paths and malformed digests. A focused regression verifies
that new manifest entries are fetched and unsafe paths are refused. The diagnostic package builder
had a related defect: it copied GGUF/tokenizer files locally without adding them to the manifest,
so a clean build could silently omit the files. It now pins every copied runtime file, marks the
bundle unpublished/local-only, and refuses a misleading HF revision. A small synthetic package
test verifies the full contract. These changes make packaging auditable; they do not publish or
endorse the current unmatched diagnostic bundle.

The complex product fixture uncovered a real decomposition seam: a highest-ranked proposal
could fail deterministic dual-lowering even when another ranked pool member was compilable. The
compiler now tries the ranked pool in order, preserving the existing SQL guard, named-measure,
ranked-grain, cutoff, typed-AST, and dual-emitter checks. A second fixture showed every generated
candidate for “top product category names by revenue” grouped by both category and product. For
cross-input measure rankings only, a narrow typed-AST projection now drops a uniquely named
extra group key, keeps the requested entity and the ORDER BY measure, validates the new AST, and
records provenance evidence. Ambiguous nouns, non-measure ranking, and unsafe projections do
not rewrite. Regression tests cover both accepted rewrites and rejected ambiguity. The focused
decomposition suite passes **14/14**; the top-category fixture also passes when rerun against
the final projection tightening. SQL AST/runtime contract tests pass **124/124**.

## Current release decision (2026-09-28)

For the exact candidate already serving at `prereasoner-api-00236-noz` (0.5B bundle),
the full hermetic suite, disposable public-seed live product suite, Python 3.11 dependency
check, and bounded CPU API smoke at concurrency 1/2/4/8 have passed. The concurrency-8 test
was one bounded burst, not sustained-load acceptance or an agreed latency SLA. Those results
do not validate the separate 7B Spider accuracy claim.

The standard deployment build now gates publication on offline regression, startup health, and
the disposable live-product suite against the exact candidate image. The first attempt,
`2c82e9f5-c3e1-4837-bd8f-a0ad676b21ef`, failed closed because the clean release context omitted
`/app/tests/build_provenance.json`. I fixed the release-context generator to emit both the runtime
weight provenance and hermetic-suite source inventory. The corrected build
`db2c9921-d638-45e3-963b-4478bce02943` from commit `39a70cf` completed **SUCCESS**. Offline
regression, Python 3.11 package consistency, startup health, live public-seed PostgreSQL/world
checks, routing, geography, schema probes, and dataset prompt/follow-up tests all passed. The
runner reported `ALL SUITES PASSED`; runtime versions were Python `3.11.16`, Torch `2.13.0+cpu`,
Transformers `5.10.4`, spaCy `3.8.13`, sqlglot `30.18.0`, and psycopg2-binary `2.9.12`.

The same build's CPU-only 8-vCPU/16-GiB API smoke returned the expected result and healthy
components. Serial requests: n=3, p50 `1.420s`, p95/max `5.129s`. Concurrency 2/4/8: p50
`1.499/2.922/5.691s`, p95/max `2.860/5.698/11.184s`; container memory snapshot `6.697/16 GiB`.
This is a bounded burst, not a sustained-load or agreed SLA. The immutable community image was
published only after these gates passed:
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine:community-39a70cf`, digest
`sha256:2b60c9c9859992cc0879c8e2bb5622d867e88d940f3b916dbb4cadf7b3ca80d9`. This is a non-traffic
candidate build, not a production promotion and not evidence for the separate 7B Spider gain.

The old “remaining acceptance work” list immediately below is historical and superseded by
these completed gates. The actual blockers are now:

1. The deployed manifest/image is 0.5B. The separate 7B result is not a releasable model/pool/
   selector package, and the last committed fold-1 7B full-DEV measurement is 755/1,034 strict,
   below the 828 target. Do not label 0.5B release tests as evidence for the claimed 7B gain.
2. No accepted, immutable, model-matched 7B selector/runtime bundle has been installed in the
   production manifest. It must pin weights, tokenizer, runtime, prompt/pool and scoring
   contracts, and be reproducibly fetchable from a clean build.
3. Once that bundle exists, repeat the full offline and disposable live product gates and
   complete-engine CPU latency/memory checks against that exact bundle. Existing 0.5B results
   cannot be reused for it.
4. Authenticated external-LLM integration incurs provider cost; production Cloud SQL tests touch
   user data. Neither has been run. The public-seed test is the completed routine product gate;
   any further external spend or production-data access needs explicit operator scope.
5. No sustained-load acceptance target is defined. Agree p95, error-rate, throughput, and test
   duration before calling the bounded concurrency smoke a load gate.

No merge, production traffic change, or deployment is justified until the requested candidate
itself has a frozen matched bundle and passes blockers 1–3. The release harness now publishes a
separate, immutable community-tagged image only after its gates pass; that is not promotion to
production. The refactor and harness can be reviewed independently, but they do not prove the 7B
gain.

## Historical diagnostic-bundle checklist (superseded)

The self-contained diagnostic bundle passed its 13-case offline CPU gate and, after the
decomposition fix, all seven complex product fixtures in a standalone run. It is still not a
serving candidate:
its one-output pool and neutral-likelihood features do not match the four-beam,
likelihood-scored arbiter contract, and it is explicitly refused outside development/test mode.
The diagnostic package helper is now correct, but the diagnostic GGUF files are still not in the
repository's production manifest or public artifact revision. The bundle includes a hash-pinned
tokenizer snapshot, but the CPU `llama-cpp-python` runtime is not pinned
in the serving dependency lock or verified on the production Python version/image. llama.cpp
reported CPU_REPACK fallback for LoRA tensors, and no latency or memory SLA was measured.

The remaining acceptance work, still isolated from main, is:

1. Repeat the full offline suite on an idle target-like runner; separately rerun the complex
   fixture suite against the final tree after the last projection-tightening change.
2. Train/calibrate a model-matched selector on the candidate's actual proposal/scoring
   distribution, and package it with the exact candidate, tokenizer, and prompt/pool contract.
3. Add the runtime artifact to the normal production fetch manifest and pin `llama-cpp-python`
   in the serving dependency lock; verify a clean clone can fetch and load it on the production
   Python version/image.
4. Run proposer/arbiter parity and selected-SQL checks on fixed product fixtures; reject any
   mismatch rather than relaxing loader guards.
5. Run the full offline regression against the frozen matched candidate, then the live
   Postgres/world product tier once its seeded database and credentials are available.
6. Measure complete-engine CPU latency and peak memory on the target runtime, not just isolated
   proposal time. A failing or incomplete gate stays unpromoted; main remains unchanged.
