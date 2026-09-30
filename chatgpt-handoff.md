# ChatGPT / Codex handoff log

Running work log for Claude and the user: progress, measured results, decisions,
user-visible transcript excerpts, and outstanding questions. Newest dated entry first.
This supplements, rather than replaces, `CLAUDE.md`, `DECISIONS.md`, and the authoritative
measured-results ledger at `spider/results/RESULTS.md`. It is not a second implementation
plan or a change to production ownership.

Codex writes this file. Claude writes `claude-handoff.md`. Read the other log before
work that could affect the shared experiment. Codex will update this log at meaningful
milestones and handoffs while actively working; no background monitor was created.
Transcript excerpts below are explicitly labeled and are not a full verbatim chat export.

---

## 2026-09-30 — Europe scope / GBP follow-up regressions added; not deployed

Reviewed the two annotated turns from the live conversation. The product bug is real: the prior
Europe-in-GBP answer was £810 (only the five London rows already denominated in GBP), omitting the
13 Brussels/Paris EUR rows; the assistant then claimed it could not convert currencies even though
the engine's supported FX path is intended to do that. Updated the orchestrator follow-up contract
to keep geography separate from output currency, preserve the Europe+GBP request, execute a clear
"yes" against the immediately preceding offer, and relay only an engine-reported FX limitation.

Added regression coverage at three levels: (1) a deterministic live-Postgres engine case requires
Europe scope to include London/Brussels/Paris (13 rows), exclude Burbank/Kolkata, and materialize
per-row `rate_to_gbp` conversion; (2) a credential-gated orchestrator integration case checks the
exact Europe/GBP rewrite and the screenshot's follow-up/"yes" flow; and (3) the public
`customer-orders` demo gold now covers that ordered conversation, while a Chrome fixture test opens
`?load=customer-orders` and carries all four turns with the same table/history. The unmocked browser
matrix was updated to use each public demo's exact `?load=<dataset>` URL and shipped `prompt.txt`,
then send every ordered `eval.txt` follow-up in one conversation.

Verification on the isolated `codex/cloudrun-cold-start-spacy` worktree (`eedee7d` base): Chrome
fixture suite **34/34**, `npm run test:web` passed, `tests.test_calculations` **105/105**,
`tests.test_orchestrator_unit` **20/20**, modified Python files compile, and `git diff --check`
passes. `tests.test_datasets` skipped because `KB_PG_PASSWORD` is absent; live Postgres geo checks
were not run. `tests.test_orchestrator` skipped because `ANTHROPIC_API_KEY` is absent; its exact
conversation integration assertions were therefore not exercised against a real model. The full
unmocked Chrome release matrix (`?load` across all public datasets and their ordered follow-ups)
was **not run**: this checkout has no `EVAL_STORAGE_STATE` or live model/database credentials.
The 34 Chrome tests use the disposable fixture backend, not the deployed model.

No merge, commit, push, Cloud Build, or deployment occurred. The currently deployed revision
recorded above (`prereasoner-api-00125-jsx`) does not contain these uncommitted changes, so this
fix is **not live yet**. No existing user conversation was modified.

## 2026-09-30 — merged release deployed; live health and browser regressions verified

PR #35 is merged at `a5d42109bd7389b5b76fe35a85af59a76dcfef15`. Cloud Build
`5a79b52d-236a-490f-a27d-6de3d9708112` succeeded for immutable engine image
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:131edc4f683e8694e564f69f2ac3e169338a32d8ed4868810eb519c8c7fd0f1f`.
The runtime smoke, offline regression, and full disposable-PostgreSQL product suites passed in that
build. The API, ECB-refresh job, and retention-cleanup job were applied with a reviewed targeted
Terraform plan (0 added / 3 changed / 0 destroyed; no Cloud SQL or deletion-protection changes).

Cloud Run revision `prereasoner-api-00125-jsx` is ready and receives **100% traffic** at
`https://prereasoner-api-3vfsfkezsq-uc.a.run.app`; `GET /api/healthz` reports `ok`, `reason`,
`world`, and `dimension` all true. Startup completed in 9m45s total, including 5m50s for
container health, within the configured 10m10s probe window. The earlier revision
`prereasoner-api-00122-zc4` (digest `82f8f154…`) remains available at 0% as rollback. The release
fix moves spaCy warmup off the readiness path; the long cold start is still an operational metric
to monitor.

The existing orchestrator's `GET /readyz` also returns HTTP 200. The exact-image bounded CPU API
smoke measured a world-join serial sample (n=3) at p50 1.304s and p95/max 8.849s; concurrency
2/4/8 p50 was 1.484/2.801/5.212s and max 2.692/5.267/9.990s. Container memory was 6.288 GiB
of 16 GiB. These short samples are regression evidence, not an SLA or sustained-load guarantee.

`npm run test:browser` passed **33/33** on the merged source, including synthetic sign-in, upload,
answer, trace inspection, follow-up, and deletion flows against its disposable fixture backend.
Separately, two synthetic orchestrator turns using the existing Anthropic service credential and
the local stub engine passed **5/5** checks: engine answer/grounding, follow-up qualifier retention,
and bearer-token passthrough. The credential was not printed or retained in the environment.

The production app is Firebase-authenticated. The isolated browser tab had no usable signed-in
token, and its CSV picker did not open through the available browser bridge. No production chat,
user table, or durable conversation was created, and no open user conversation was touched. Thus
the authenticated **production-browser** conversation gate remains unverified; the 33/33 fixture
suite and stub-engine orchestrator test must not be described as that gate. The temporary
synthetic CSV was removed. A complete post-apply Terraform plan shows only residual API scaling
and chat-service configuration diffs; it was not applied because it includes unrelated service
configuration changes. No benchmark score changed: the accepted 7B DEV result remains 865/1,034
strict and official Spider test-suite DEV remains 832/1,034; both are repeatedly consulted DEV
engineering metrics, not untouched test-set generalization claims.

## 2026-09-29 — full 7B replay and official Spider metric complete; release still gated

The final clean-source CPU replay produced all 1,034 rows with exact ordered indices and
verified provenance (`worktree_dirty=false`; source commit `40cd5a44947edaa62b788210afdfd1fcf112875b`).
XiYanSQL QwenCoder 7B Q4_K_M, eight CPU threads, frozen served neutral-score arbiter, no gold
substitution: **865/1,034 strict (83.72%)**, **868/1,034 lenient (83.95%)**. By difficulty,
strict is easy 234/248, medium 378/446, hard 150/174, extra 103/166. This clears 80% on a
repeatedly consulted DEV engineering set; it is not an unbiased generalization claim.

The pinned official Spider test-suite evaluator also passed the full denominator: **832/1,034
(80.46%)**, evaluator commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`, 695 suite SQLite
files, `keep_distinct`, no `--plug_value`, zero timed-out rows. It is a secondary Spider-only
metric, not product Knowledgebase-join accuracy. Its exact run artifacts and hashes are in
`%LOCALAPPDATA%/Temp/prereasoner-7b-hardening-20260929/`; the ledger is updated in
`spider/results/RESULTS.md`.

Paired against the prior `clean-engine-q4-8t` full replay, strict gains are four and losses three
(861 unchanged correct, 166 unchanged wrong); 23 SQL outputs differ. Those 23 score 15/23 on the
official test-suite metric for this candidate versus 14/23 for the paired control. The distinct
historical deployed record at 839/1,034 is not this paired control.

The hard blocker is now latency, not accuracy: p50/p90/p95/max are 19.335/37.111/42.834/61.705s,
with 893/1,034 exceeding the 12s soft target. Four rows failed (three connected-AST misses on
`car_1`, idx 104/135/136; one proposer CPU decode-budget failure, idx 575). The experimental
model-matched arbiter did not improve the preregistered held-out sample, so no new selector was
installed. A bounded 16-thread pilot on 32 difficulty-balanced rows preserved selected SQL and
strict labels 32/32; p50/p90/max improved from 14.781/26.544/35.609s to 12.271/22.146/28.839s,
though 18/32 remained over 12s. The test used a 16-logical-CPU host, so it does not qualify a
thread override for the 8-vCPU Cloud Run service. Reran `npm run test:browser`: **33/33** passed
against its synthetic backend; this is not signed-in exact-image browser E2E. Release Cloud Build
`b0cc8a30-0a32-4a6c-9efb-98b18277f12f`
passed startup/health, seven disposable-Postgres suites and the bounded CPU API smoke; its image
digest is `sha256:da66e69be2c73b71c1200a006cb439f785ae9f8a30e313619f597504db8cc0a6`. Synthetic
Playwright passed 33/33, but authenticated orchestrator cases remain untested because no external
model credential is available. Production remains unchanged: revision `prereasoner-api-00122-zc4`
serves 100% traffic. No merge or deployment has occurred.

## 2026-09-29 — release build green; full CPU replay in progress (earlier checkpoint)

Commit `7817f69f0f8055a988022b9fab8d18e6c6a349dd` adds the community-seed post-migration QID
projection rebuild and bumps the bootstrap marker so upgraded databases apply it. Focused tests
passed (`tests.test_community_deploy`: 24/24; QID projection tests: 3/3), as did compileall and
`git diff --check`. Ruff's changed-file `F,I`/`F` checks passed; the repository-wide style check
still reports pre-existing line-length violations in these files.

Attested release-context Cloud Build `b0cc8a30-0a32-4a6c-9efb-98b18277f12f` completed SUCCESS
from that commit. Immutable image:
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:da66e69be2c73b71c1200a006cb439f785ae9f8a30e313619f597504db8cc0a6`.
Offline regression and startup/health passed (17s startup); all seven configured live suites passed
against disposable PostgreSQL: world, nongeo, world joins, route-wired, geo, schema probes, and
datasets. API health was true for all components. Candidate CPU API world-join sample was n=3,
p50 1.396s, max/p95 4.930s. Across 42 proposer timing spans recorded inside the live dataset
suite’s 8-CPU container, decode p50/p90/max was 6.713s/10.104s/36.419s. This is useful exact-image
CPU evidence but not a Cloud Run request-latency/concurrency measurement. This build did not deploy
to Cloud Run. `test_datasets` explicitly skips authenticated orchestrated follow-ups; those remain
an open gate.

The isolated Playwright suite passed **33/33** against its synthetic local backend. This is client
regression evidence only, not exact-image/authenticated production browser E2E. Additional reruns
passed `tests.test_orchestrator_unit` (20/20) and `tests.test_release` (43/43); the seed-import
focused suites remain 24/24 and 3/3 as above.

Fresh full Spider DEV replay uses source contract commit `40cd5a4`, frozen current serving arbiter,
CPU XiYan Q4_K_M, 8 threads, no value substitution, and continues in process PID 8316. At 1,025/1,034
indices it has 857 strict-correct rows (83.61% partial); 9 rows remain. The 80% floor is crossed in
this partial prefix; seven of the remaining nine correct would match the historical 864 strict
score. For these 1,025 rows, prediction p50/p90/max were 19.36s/37.11s/61.71s and 886/1,025
exceeded the 12s soft latency budget. Difficulty so far: easy 233/247, medium 376/443, hard 145/169,
extra 103/166. Four records are errors and will be classified in the final report. Both strict
count and full tail-latency distribution must be reported at completion.
Verified `git diff 40cd5a4 7817f69 -- engine spider/probe` is empty: the tested release image’s
serving/evaluation code is identical to the replay source; the intervening fix is seed-import-only.
Instrumentation shows proposer decoding ran on all 750 rows and the proposal-origin candidate was
selected on 450/750; decode alone had p50/p90 17.98s/34.03s. The live `tests.test_orchestrator`
API test self-skipped because `ANTHROPIC_API_KEY` is unavailable in this workspace, so its live
external-model/browser gate remains open. This makes a blanket
“skip the proposer” optimization incompatible with preserving the measured candidate behavior;
any latency optimization must be paired and re-evaluated for correctness.
The replay checkpoint is under `%LOCALAPPDATA%/Temp/prereasoner-7b-hardening-20260929/`.
Official Spider test-suite evaluation has not run yet. Production remains unchanged on the existing
100%-traffic revision; no merge, deployment, official TEST evaluation, or authenticated browser
claim is made here.

## 2026-09-29 — neutral arbiter pilot complete; candidate rejected

The local 240-question XiYan CPU pool build completed against `train_spider.json`: exactly 24
questions each for the preregistered ten databases, 240 unique indices, 239 labeled rows and one
malformed gold query (`idx=4514`, `document_management`, invalid ORDER BY placement before
INTERSECT). The row remains in the denominator. Model/proposer hash is
`50840d65a753074a670d7929ca0a4b5d633b0a4b435f1a68b4a6fba26c4d18bb`; the pool contract uses
CPU, neutral-sentinel likelihood, one 1,024-token beam and deterministic search budget 25. Pool
JSONL SHA256: `2e98974a4c8b1308afef40e7b9f105631aba7e1419e6832eb2d7f077a3be46bd`.

Fit used 144 questions / six TRAIN databases; validation used 96 questions / four disjoint TRAIN
databases. This holds out selector-fitting DBs only; XiYan pretraining exposure is unknown. The
fitted arbiter and frozen production arbiter both selected **82/96 strict-correct** candidates,
with **0 paired wins and 0 losses**. Eligible pool-oracle coverage was 85/96. Per validation DB:
customer_complaints 19/24, program_share 24/24, student_1 21/24, wine_1 18/24 for both selectors.
This fails the preregistered selector-gain gate; the candidate was not installed or promoted.
Experimental arbiter SHA256: `e37d48dd50fe380c2d220cf0fd3b577e94d0ab9810f84e29d7e7f47fad129061`.

The new replay serializer change and regression test passed `tests.test_sql_ast` (129/129). Next is
a fresh full CPU DEV replay of the frozen current serving arbiter from a clean, fingerprinted
commit, followed by the official Spider test-suite evaluation on those exact saved SQL strings.
The older 864 strict / 839 test-suite records remain historical and do not validate the new source
tree. No merge/deploy/browser gate has been claimed from this pilot. No RunPod was used.

## 2026-09-29 — review fixes underway; new release gates NOT complete

User requested the full review-fix/80%+/merge/deploy/browser plan, then authorized continuing
overnight. Work is isolated in `7b-production`, branch `codex/7b-release-hardening` from
`e50b4a5`; Claude/main and the dirty `af15` research checkout are unchanged. The active plan
is now at the top of `PRODUCTION_READINESS.md`, not a second plan document.

Corrected the prior completion framing: the deployed baseline recorded 864/1,034 strict and
839/1,034 official test-suite, but its authenticated production demo/follow-up Chrome gate
was not complete. Those scores are not yet verified for the new source changes.

Reproduced and fixed exact decimal threshold rounding, accepting truncated completions,
missing context preflight, and duplicate concurrent prompt decoding. Focused results:
XiYan 9/9, compose 23/23, SQL AST 129/129, calculations 104/104 on local Python 3.14
(not a substitute for the Python 3.11 image gate). Saved pools now retain structural
proposer origin, full-precision scores, execution/grounding eligibility and calculation/money
facts. Serving and fitter replay call the same post-ranking rule. Resume and denominator
contracts are being hardened before any matched pilot. No new model promoted or deployed.

The prior full offline run finished with every invoked suite exit-zero, but its `complex_datasets`
suite imported the proposer before the final lifecycle fix, so that run is diagnostic only. The
native model now has a weak-reference finalizer (no temporary test instance is retained to process
exit); a real local GGUF load was collected and finalized successfully. I also corrected the pool
oracle: only candidates that were serving-eligible and actually produced a labeled denotation count
toward oracle coverage. Checkpoints now reject duplicate/out-of-split indices. Current focused
results: SQL AST 128/128, XiYan proposer 12/12, release 43/43; Ruff, compileall and `git diff
--check` pass. I will rerun the frozen full suite after the preregistered local pilot source is
committed. The 240-question pilot uses six TRAIN databases for fit and four disjoint TRAIN databases
for validation; it is not a claim of independent proposer pretraining exposure or a promotion gate.

Follow-up commit `71da66d` makes the pilot replay the fitted selector and the frozen production
arbiter on the same validation candidate pools, with paired wins/losses by question and database.
The old arbiter declares a 4-beam/96-token fit pool while production now generates one 1,024-token
CPU beam; the report retains that historical mismatch, while the comparison uses the actual new
production pool. Pool collection is running locally from source commit `95c33c9`; at the last check
it had written 44/240 examples, with no errors and stable memory. No selector has been fit, and
these partial counts are not accuracy evidence.

RunPod MCP read-only check: no account pods. Billing for Aug 30–Sep 29 totals $32.500336
across prior work (not all charged to this experiment); Sep 28 total $7.716525. No paid run
has been started in this change. Remaining budget must be reconciled before a new lease.

## 2026-09-29 — 7B merged, fully cut over, and post-deploy checks complete

The unified 7B implementation is merged to `main` via [PR #30](https://github.com/ManiDoraisamy/prereasoner-data/pull/30).
Release source commit: `46b10e42dee5db42bd745537af3c8c6bc423c8b2`; merge commit:
`ad4f4076fc8f7b09c146811e84aa96802794ce2a`. The source tree was checked against the measured
replay: all 37 engine-file hashes match, and the model/arbiter artifacts are SHA-pinned.

The full production service—not a canary or split rollout—is now on the 7B image:
Cloud Run project `prereasoner-inference`, region `us-central1`, service `prereasoner-api`,
revision `prereasoner-api-00122-zc4`, 100% traffic. Immutable image:
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:82f8f154d8c655bb23e05e0c1a98d956175f3ef1ab13a3fbfe6eb3bd9313b79c`.
The existing 8-vCPU/16-GiB sizing, Cloud SQL attachment, service account, and min/max scale
settings were preserved. Revision startup and `GET /api/healthz` passed; `reason`, `world`,
and `dimension` were healthy. The app returned HTTP 200 in a read-only browser visit. A harmless
unauthenticated `/api/reason` request returned 401 as required. No signed-in request or durable
user history was created. The prior revision remains at 0% as rollback capacity.

Release gates on the exact image passed: Cloud Build `22bc2d18-b242-4b90-9c1b-a1502dd5767c`,
offline regression (13 passed), runtime/server smoke, live product suites against disposable
seeded PostgreSQL, CPU HTTP smoke (serial and concurrency 2/4/8), and 33/33 Playwright browser
tests. The disposable DB gate did not touch production Cloud SQL. The unauthenticated check is
not an end-to-end signed-in user transaction; no such write was performed.

Final CPU Spider DEV replay: **864/1,034 strict (83.56%)**, **867 lenient**, versus the historical
0.5B main baseline **647/1,034 (62.57%)**: +217 strict examples / +20.99 points. The same 1,034
selected SQL strings exactly match the prior full replay (zero SQL, strict-label, or identity
mismatches), so the official Spider test-suite result remains **839/1,034 (81.14%)**. This is a
repeatedly tuned DEV engineering result, not an unbiased generalization estimate; the test-suite
metric is Spider-only and says nothing about production Knowledgebase joins. Full evaluator latency
was p50/p90/p95/p99/max **7.172/16.384/17.840/19.965/138.576 seconds**; 220/1,034 exceeded the
evaluator's soft 12-second threshold. This evaluator tail differs from the bounded production-image
HTTP smoke (concurrency-8 max 11.648 seconds), so it is disclosed rather than conflated with API
latency. Track it as follow-up performance work.

The RunPod replay artifacts were downloaded and SHA-256 verified before the task-created CPU pod
`h4h6uni6jbq23f` was terminated. The pod's final itemized billing was `$6.81668396841269`; a
subsequent lookup returned 404, confirming it is gone. No unrelated RunPod resources were changed.
The authoritative metric update is recorded in `spider/results/RESULTS.md`; older entries below
are historical checkpoints and should not be read as the current deployment state.

## 2026-09-28 — Final exact-source 7B score and official Spider test-suite metric

The production-matched CPU-served XiYanSQL Q4_K_M candidate completed the full Spider DEV replay:
**864/1,034 strict (83.56%)**, compared with the recorded 0.5B main baseline of 647/1,034
(62.57%), a **+217 questions / +20.99 percentage points** engineering-set difference. DEV has
been repeatedly consulted during development; this is a tuning result, not an unbiased estimate.
The replay has 1,034 rows with unique indices 0–1,033. Three `car_1` AST-search failures
(indices 104, 135, 136; no selected SQL) count wrong. Difficulty strict: easy 233/248, medium
378/446, hard 149/174, extra 104/166. The separate `spider/results/RESULTS.md` baseline remains
the historical 0.5B score; this candidate result does not replace it.

The pinned official evaluator `taoyds/test-suite-sql-eval` at
`e97acc546ecbee8fa27fa8dbf025ef61493a876c`, run against the official Spider test-suite SQLite
databases, scored **839/1,034 (81.14%) test-suite execution accuracy**. Difficulty counts were
easy 233/248, medium 375/446, hard 148/174, extra 83/166. The prediction file retained model
SQL literals; `--plug_value` was not used. This secondary Spider-only metric does not test the
product's multi-source Knowledgebase joins. Suite archive SHA-256:
`9ec24ea8debc6bd04abfe137b5f1a739b5a8836f32c0464e4dfc94eb7f41da96`. Official evaluator output
SHA-256 `cf4458fbbcb5bedc46bbfc88df1e2c22624fb88cad804e0b5a553bfdbee2e669` is preserved in the
analysis temp directory.

Replay provenance is in the final checkpoint and summary, copied from the pod and hash-verified:
summary `05792b3a3304a40d5e119673813d71f20ef58f5ac461c431b0874d72dc8fe56a`, per-example
`1bcec9318fe3bbb6a430e9a401722a52e530255f45d67a01840ce1efae30ea26`, checkpoint
`0d9987aa6d21a64f6bef0982c853b147df866d2f29294e707ae60b2ae9860610`. The contract pins source
commit `0c7e98050f7ab9a88066b6197dccd684c700b9a8`, proposer SHA
`50840d65a753074a670d7929ca0a4b5d633b0a4b435f1a68b4a6fba26c4d18bb`, arbiter SHA
`fc84162a4dc9900f963bbea751ccf13f2d9218842a0f781ee1cb7e964584977d`, and effective eight
proposer threads. The source worktree was dirty; artifact hashes are the reproducibility
contract.

Latency remains an explicit caveat: full-evaluator prediction-time p50/p90/p95/p99/max was
7.513/17.614/21.686/129.397/231.588 seconds; **273/1,034** exceeded the evaluator's soft
12-second threshold (which does not cancel work). This is not the same path/load as the earlier
Cloud Run API smoke (8-vCPU/16-GiB candidate image; concurrent-8 max 11.648 seconds), so neither
result is substituted for the other. A privacy-safe targeted timing probe of the ten longest
rows finished: those exact examples now take 4.3–7.2 seconds; proposer spans are 3.7–6.5 seconds,
encode at most 1.56 seconds, and pool execution under 8 ms. The earlier 142–232-second spikes did
not reproduce in this controlled sample, so no root cause is asserted. A new full 1,034-row replay
with per-stage telemetry and fixed eight-core affinity is running to confirm whether the extreme
tail recurs.

The E2E browser suite remains **33/33 passing** against its isolated fixture server; the
production browser inspection and healthz check were read-only. Current RunPod pod
`h4h6uni6jbq23f` is this evaluation worker at `$0.96/hour`; its last billing read through
21:00Z was `$4.399270317167975` for this pod. Preserve the result artifacts, finish the full
timing replay, then remove only this task's temporary pod and restore the original SSH-key list.
PR #30 remains
open/draft on branch `codex/7b-production-main`; all six GitHub checks are green as of
`b87551b`. No merge, production deploy, or traffic change has occurred yet.

## 2026-09-28 — Direct-served replay checkpoint: 980/1,034; browser suite passed

The production-matched 8-thread replay is at **980/1,034 contiguous unique rows**, **812
strict-correct (82.9% prefix)**. By difficulty: easy 220/235, medium 350/416, hard 142/167,
extra 100/162. The three known no-AST failures are counted wrong. It needs 16/54 of the
remaining rows to reach the 80% gate of 828/1,034. Latency p50/p90/p95/p99/max is
7.52/17.72/29.62/131.06/231.59 seconds, with 253/980 exceeding the soft 12s budget. The
checkpoint is copied locally at `%TEMP%\\prereasoner-7b-analysis-20260928\\serving-8t.latest.json`,
SHA-256 `92de12c5e38195205198a83180d66dbf3b027c7be788f4b9192187aadb4bf3a4`; local and pod
hashes matched. The 8-thread evaluator is active. Accuracy is above the partial threshold, but
the long latency tail remains a cutover concern pending the completed replay and diagnosis.

Browser regression: `npm run test:browser` completed **33/33 Playwright tests passed** against
the isolated local fixture server (29.6 seconds). A read-only production browser inspection
confirmed the app page renders with its existing account/dataset, and `/api/healthz` returned
`ok`, `reason`, `world`, and `dimension` all true. I did not send synthetic requests through the
signed-in production account, which would create durable user history. The live product suite
against disposable seeded PostgreSQL and bounded candidate-image HTTP smoke remain the safe
write-path evidence (both previously passed on this source); the 7B-revision production browser
path is still pending deployment and separate approval for any user-account writes.

---

## 2026-09-28 — Direct-served replay checkpoint: 890/1,034; separate full local diagnostic

The production-matched RunPod replay has advanced to **890/1,034 contiguous unique rows**,
with **747 strict-correct (83.9% prefix)**. Difficulty counts: easy 216/231, medium 323/380,
hard 121/143, extra 87/136. The three known no-AST rows remain counted wrong. The cumulative
soft-12-second exceedance count is 191/890; latency p50/p90/p95/p99/max is
7.43/15.83/46.73/131.54/231.59 seconds. Current checkpoint SHA-256 is
`fe9b5156d7c5a923fcc57e6eb296c51636493e121c01b5faacb9db8b3b5cd395` on the pod. The
evaluator is still advancing, but the new 231.59-second maximum confirms the long-tail latency
gate is not yet met.

A separate local full-set diagnostic completed **865/1,034 strict (83.7%)**, with contiguous
unique coverage of every index and three explicit no-AST errors. Its artifact is preserved at
`%TEMP%\\prereasoner-7b-analysis-20260928\\serving-16t-final.json`, SHA-256
`3924ba112ea5e3d04d99447cdf9f77ddd41ad47a8911f6ff5b2d0798602184d5`. That run used a
different source commit (`8655010f60460039d41705887b70415cc8d119be`) and a `16t` run tag; its
per-example latency p50/p90/p95/p99/max is 17.08/34.93/39.72/45.71/62.06 seconds, with
828/1,034 above the soft 12-second budget. It is corroborating accuracy evidence, not a
replacement for the production-matched 8-thread run; its checkpoint contract does not record
the effective thread count.

RunPod billing for our CPU3c pod `h4h6uni6jbq23f` through the closed 20:00Z bucket is
`$3.432598` total (`$0.240048` in that bucket); current-hour usage is not yet posted. The
official test-suite archive remains downloaded but unextracted while the 8-thread latency run
is active.

---

## 2026-09-28 — Direct-served replay checkpoint: 840/1,034

The production-matched 8-thread RunPod replay is alive at **840/1,034 contiguous unique
rows**, with **706 strict-correct (84.0% prefix)**. Difficulty counts are easy 204/219,
medium 302/354, hard 113/135, and extra 87/132. Three missing-`strict` rows are explicit
`no connected AST candidate` failures (indices 104, 135, 136) and count as wrong. Current
latency p50/p90/p95/p99/max is 7.43/15.22/19.07/131.06/184.47 seconds; 178/840 exceed
the evaluator's soft 12-second budget. This is an interim diagnostic, not the final result.
The heavy tail is not explained by candidate count alone: several very slow `flight_2` and
`employee_hire_evaluation` rows have between 1 and 26 candidates. Prompt/model-stage timing
is not yet instrumented, so no root cause is claimed.

Checkpoint preserved outside the repo at `%TEMP%\\prereasoner-7b-analysis-20260928\\serving-8t.latest.json`,
SHA-256 `69f6daa6649dd7ca59d60eeb8e62d4f2afa48780c702a2f9fc3132903cba8603`. Its artifact
contract pins source commit `0c7e98050f7ab9a88066b6197dccd684c700b9a8`, Q4 proposer SHA
`50840d65a753074a670d7929ca0a4b5d633b0a4b435f1a68b4a6fba26c4d18bb`, arbiter SHA
`fc84162a4dc9900f963bbea751ccf13f2d9218842a0f781ee1cb7e964584977d`, and effective
`sql_proposer_threads=8`; it also reports `worktree_dirty=true`, so the per-artifact hashes,
not the Git commit alone, are the reproducibility contract. The RunPod CPU3c remains
`RUNNING` at `$0.96/hour`; billing through the 19:00Z bucket for this pod is `$3.030837`.
The official test-suite database archive has downloaded (1,269,456,098 bytes) but is not
being extracted during the latency run. No merge, deployment, or traffic change has occurred.

---

## 2026-09-28 — Direct-served replay checkpoint: 710/1,034

At this earlier checkpoint the replay was **710/1,034 unique; 598 strict-correct (84.2% partial)**. By difficulty: easy 175/189, medium 259/304, hard 98/115, extra 66/102. Latency p50 7.0s, p90 16.2s, p95 20.0s, p99 131.5s, max 184.5s; 175/710 exceeded the evaluator's soft 12s budget. The full result and long-tail release decision remain outstanding. Exact official Spider test-suite evaluator source has been pinned locally at commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`; its README identifies test-suite accuracy as Spider's official execution metric, and requires no `--plug_value` for a model that predicts literal values. The suite database archive was subsequently downloaded; see the latest checkpoint entry above.

---

## 2026-09-28 — Direct-served replay checkpoint: 600/1,034

At 600 unique Spider DEV rows, the integrated CPU-served candidate has **496 strict-correct (82.7% partial)**. Difficulty counts: easy 137/149, medium 215/256, hard 83/99, extra 61/96. The 12s soft-budget exceedances are 173/600 (28.8%); p50 7.0s, p90 16.7s, p95 29.8s, p99 142.1s, max 184.5s. Three no-connected-AST-candidate errors remain visible at indices 104, 135 and 136. Current checkpoint is preserved locally; SHA-256 `3f855a56d205fef37673883313293f65b8777aa8882033846afccaf22a840554`. Accuracy is promising but partial; latency still needs a measured decision before promotion.

---

## 2026-09-28 — Direct-served replay checkpoint: 500/1,034

The same immutable served-policy replay has checkpointed **500/1,034 unique indices; 423 strict correct (84.6% on this partial prefix)**. By difficulty: easy 107/115, medium 188/224, hard 77/89, extra 51/72. Three rows (`idx` 104, 135, 136) have no connected AST candidate and are incorrect. Latency remains the chief concern: p50 6.56s, p90 13.99s, p95 77.01s, p99 143.98s, max 184.47s; 95/500 exceed the evaluator's 12-second soft budget. These are prefix numbers, not a final claim. The run is alive and advancing; checkpoint copied locally again. No source/runtime edits were made while this contract is running.

---

## 2026-09-28 — Direct-served 7B replay checkpoint: 490/1,034

Latest exact-source CPU replay checkpoint: **490 unique Spider DEV examples, 416 strict-correct (84.90%)** under `selection=served`, on the integrated XiYanSQL Q4_K_M model plus neutral-sentinel arbiter. Per difficulty so far: easy 105/113, medium 183/217, hard 77/88, extra 51/72. This is a partial checkpoint, not the final score. The run uses 8 proposer threads on the RunPod CPU pod and source commit `0c7e980`; its artifact contract pins the dev set, engine files, proposer model and selector hashes.

The latency tail is materially problematic: at n=490, p50 is 6.55s, p90 13.92s, p95 46.73s, p99 143.98s, max 184.47s, with 91/490 above the evaluator's **soft** 12s budget. That soft budget does not cancel inference. Continue the complete matched run, then diagnose/iterate on CPU latency without describing this checkpoint as production-ready. RunPod MCP confirmed pod `h4h6uni6jbq23f` is RUNNING at `$0.96/hour`; itemized account billing through the 18:00Z bucket totals `$2.1951` across three pods, of which this pod accounts for `$2.0625` posted, with the current bucket not yet posted. A fresh copy of the partial checkpoint is in `%TEMP%\prereasoner-7b-analysis-20260928\serving-8t.partial.json`. PR #30's latest GitHub checks, including Build/SBOM/critical-vulnerability scan, are all green on the doc-only head `165edcf`. No merge, deploy, or traffic change has occurred.

---

## 2026-09-28 — 7B release verification actively progressing

Continuing the user's request to carry the 7B candidate through merge, release, deployment, and browser/product verification. The accuracy branch's authoritative ledger reports **865/1,034 strict DEV (83.7%)** and **832/1,034 official Spider test-suite (80.46%)** for the pinned XiYanSQL Q4_K_M model using the neutral-likelihood selector; DEV is a repeatedly consulted engineering/tuning set, not an unbiased holdout. Those scores are not, by themselves, proof of production readiness.

The release integration is isolated in `codex/7b-production-main`, PR [#30](https://github.com/ManiDoraisamy/prereasoner-data/pull/30), currently draft. On `0c7e980`, CI fixed a real failing-test issue: three new coverage tests imported the production spaCy POS tagger despite being deterministic checker tests. They now stub only the POS layer; production semantic behavior remains unchanged. Added a bounded `SQL_PROPOSER_THREADS` resolver and made full-evaluation checkpoints record effective thread count. Focused SQL AST tests and deterministic-emitter tests pass. GitHub CI is now fully green on `0c7e980`: Python hermetic suites, PostgreSQL numeric parity, secret scan, Terraform validation, browser journey, and image/SBOM/critical-vulnerability scan all succeeded.

The old 16-thread RunPod run did not match Cloud Run's 8-thread model contract; it was checkpointed at 190/1,034 and stopped (partial diagnostic only). The same pod `h4h6uni6jbq23f`, created for this replay, is now running a fresh, full `SQL_PROPOSER_THREADS=8` replay pinned to production source commit `0c7e980`, with affinity restricted to eight of its allowed CPUs. At 250/1,034, 202 are strict-correct (80.8%); this prefix is not a stable full-set estimate. It measures p50 6.57s, p90 9.32s, p95 12.41s, max 146.18s, with 14/250 above the 12-second soft target. Slow calls occur in several databases, including the first six `concert_singer` rows, index 85 (`pets_1`), index 136 (`car_1`, no connected AST candidate), and indices 182–184 (`flight_2`). This is not solely process startup; tail behavior is still being investigated. A paired prefix audit against the earlier neutral-likelihood pool run's selected-rank records agrees on 207/210 strict flags (one win, two losses), so the two runs appear broadly consistent on that slice; the full exact-source replay remains authoritative. The pod costs `$0.96/hour`; the latest itemized billing read is `$0.6936195356538519` through the 17:00Z bucket (the later bucket has not posted). The pod remains ours and will be terminated only after recovering/verifying final artifacts and restoring the SSH key list.

The exact-source release Cloud Build against `0c7e980` completed **SUCCESS**: build ID `ca63fdc5-a34f-411e-9ac8-cab8ff1e3d37`, immutable image digest `sha256:94618a33b605f27119500a54e68d4db211b557dc9c463feb6dbc4caf134714a9`. Offline regression, image startup/health, the bounded 8-vCPU/16-GiB API smoke, and full live product suite against disposable seeded PostgreSQL all passed. The browser CI release journey also passed; on the doc-only `e99693f` update all checks had passed except the still-running SBOM/critical-vulnerability job at the last read. No Cloud Run traffic changed. Production remains on `prereasoner-api-00236-noz` until the exact 7B CPU and end-to-end gates pass.

The exact 8-thread direct-served replay is still running on the RunPod pod created for this evaluation (`h4h6uni6jbq23f`, `$0.96/hour`). At 18:17Z it had checkpointed 320/1,034 with 266 strict correct (83.1% on this non-final prefix). Partial difficulty counts: easy 63/70, medium 111/133, hard 48/55, extra 44/62. Latency at that prefix was p50 6.45s, p90 10.82s, p95 94.26s, max 184.47s; 29/320 exceeded the 12-second *soft* budget. The earlier apparent single-row stall was a burst of several very slow rows (flight_2 and employee_hire_evaluation), not a dead process; it has resumed and the last completed row was strict-correct. A checkpoint copy is preserved locally at `%TEMP%\prereasoner-7b-analysis-20260928\serving-8t.partial.json`, SHA-256 `b7180d…6f7655`. The soft budget does not terminate native inference. This latency tail is a production blocker to diagnose and fix even if final accuracy clears 80%. Latest itemized billing totals `$1.4180686611216515` through the 18:00Z bucket; later usage has not yet posted. The model/checkpoint remains intact; no stop/restart has been issued.

Next: diagnose the >12-minute active inference without discarding the valid checkpoint; obtain a complete, contract-verified full Spider result and per-difficulty/runtime tails; finish the last CI security/SBOM job; then fix and remeasure any CPU gate failure. The image build and disposable product gate are complete. No merge, production traffic change, or deployment has occurred yet.

---

## 2026-09-28 — Combined-tree release rehearsal passed; production unchanged

I continued the user's explicit request to take concrete steps toward merge readiness instead of
returning only a status note. In isolated worktree `merge-ready`, I merged current `main` with
`codex/prod-readiness`, resolved the duplicate `KnowledgeReasoner` setup in `tests/test_world.py`,
fixed a stale `build_context.py` expectation in `tests/test_community_deploy.py`, and corrected the
`cloudbuild.yaml` release example. I did not alter `main`, the original product branch, or the 7B
accuracy worktree. Rehearsal commit: `ba130b77ab5a67bfb800f1751c2b0628923fe811` (0 behind main).

Validation: all 31 configured offline suites passed with the ignored model bundle present; notably
`test_complex_datasets` ran 7 cases with 0 skips. Focused deploy/release suites passed 21/21 and
40/40. A fresh exact-source release build, Cloud Build
`1e04710b-2b5e-4c9e-a490-1fb25126d952`, passed offline product regression, startup/health, the full
configured live product suite against a disposable public-seed PostgreSQL database, and the actual
CPU-only HTTP multi-source join smoke. Runtime sample: serial p50 1.276s/max 4.954s (n=3);
concurrency 2/4/8 p50 1.502/2.805/5.197s, max 2.773/5.284/10.496s; memory 6.699/16 GiB.
This is bounded evidence, not a sustained-load SLA. The gated build published only the rehearsal
tag `engine:merge-ba130b7`, digest `sha256:6868b1b869caff21257dc05f6853cd1528c289fdea2a2c6979311acf98f4a219`.

Read-only check confirms production is still revision `prereasoner-api-00236-noz` at 100% traffic
on digest `sha256:3cbb0037832a06630dc1e0d0a450e44b415e4e5f0ac4ef92b7607de2862891dd`. Nothing was
deployed or merged. This validates the existing 0.5B product implementation only—not the research
7B Spider accuracy candidate, which still lacks a matched selector/runtime integration and its own
CPU/product gates. Remaining launch checks are production-data acceptance, external authenticated
orchestrator coverage, and a defined sustained-load latency/SLA target. See
`PRODUCTION_READINESS.md` for the full evidence and boundaries.

---

## 2026-09-28 — Corrected release build passed the complete product gate

I fixed the clean-build bug from `2c82e9f5-c3e1-4837-bd8f-a0ad676b21ef`: the release context now
includes the hermetic-suite source inventory (`tests/build_provenance.json`) as well as the
hash-verified runtime weights. Commit `39a70cf` passed the mandatory remote release build
`db2c9921-d638-45e3-963b-4478bce02943` (**SUCCESS**). Offline regression, startup, `pip check`,
Python 3.11 runtime pins, and every live suite on the isolated public-seed PostgreSQL database
passed: world joins, nongeo, routed engine, geography, schema probes, and dataset prompts/follow-
ups. The runner reported `ALL SUITES PASSED`. Verified runtime: Python 3.11.16, Torch 2.13.0+cpu,
Transformers 5.10.4, spaCy 3.8.13, sqlglot 30.18.0, psycopg2-binary 2.9.12.

The exact built image also passed the CPU-only API smoke on 8 vCPU/16 GiB. Serial n=3 p50/max
1.420/5.129s; concurrency 2/4/8 p50 1.499/2.922/5.691s and max 2.860/5.698/11.184s; observed
container memory 6.697/16 GiB. This was a single bounded concurrency burst, not an SLA. Build
published only the immutable community artifact
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine:community-39a70cf`,
digest `sha256:2b60c9c9859992cc0879c8e2bb5622d867e88d940f3b916dbb4cadf7b3ca80d9`, after the gates
passed. Production is unchanged: revision `prereasoner-api-00236-noz` still receives 100% traffic.

This closes the clean release-build and public-seed product-regression gap for the existing
0.5B product image. It does **not** validate or ship the requested +10.4-point 7B candidate. The
remaining gating work is still a frozen, model/pool/selector-matched 7B runtime package and its
own exact-image regression/CPU checks; then production-data acceptance (not run because it
touches customer data), and a defined sustained-load SLA. No merge, production image promotion,
or traffic change occurred. See `PRODUCTION_READINESS.md` for the consolidated release decision.

---

## 2026-09-28 — First full release build caught missing suite provenance

The new commit-tagged release build `2c82e9f5-c3e1-4837-bd8f-a0ad676b21ef` built its image,
passed startup/offline gates, restored the checksum-pinned public database, and reached the
integrated live-product runner. That runner failed closed because the `release` source context
did not emit `/app/tests/build_provenance.json`, which is needed to create the attested synthetic
Git snapshot. The actual CPU HTTP smoke still passed (29/29 exact calls), but the full release
gate is a failure; Cloud Build did not publish the image. I added the second suite inventory
provenance record to the release context and am rerunning local tests before the one permitted
retry. No production traffic or data was touched.

---

## 2026-09-28 — Full live-product gate is now part of the release build

I closed a release-pipeline gap rather than treating the earlier manual acceptance build as
enough: `deploy.sh` now requests a clean `release` build context containing the allowlisted test
sources plus the same manifest-validated weights, and supplies the no-filter upload contract so
`.gcloudignore` cannot silently remove live suite dependencies. `cloudbuild.yaml` now runs the
disposable, checksum-pinned public-seed product suite against the exact just-built image after
offline regression and startup health. Cloud Build only publishes its declared image after every
step succeeds. The build worker/timeout match the already exercised product lane (32-CPU worker,
90 minutes); no Cloud SQL is used.

Validation: release-contract suite **40/40**, Ruff `F,E9`, Cloud Build YAML parse, shell syntax,
and `git diff --check` all pass. The required Cloud Build from a clean checkout with the actual
weight bundle has not yet run, so this pipeline change is not yet remotely verified. Production
traffic and artifact publication remain untouched.

This advances the single-source release path, but does not solve the separate 7B candidate gap:
the production manifest is still 0.5B, and the latest committed 7B fold-1 DEV serving result is
755/1,034 strict with a mismatched selector. I left its active CPU screen and dirty worktree
untouched. There is still no valid 7B bundle to publish as the +10.4-point candidate.

---

## 2026-09-28 — Latest-source hermetic gate and concurrency-8 CPU check

After the exact-current-image full product pass, I extended the disposable `/api/reason` CPU smoke
to match Cloud Run's configured per-instance concurrency of 8. The first attempt failed with 429
because its 45 requests exceeded the API's intentional 30 requests/minute limit for the single
test principal; this was a test-rate-budget mistake, not a model/CPU failure. I corrected the test
to keep 29 requests total (one eight-request burst after 21 lower-concurrency calls), without
changing application rate-limit behavior. Focused release tests passed **39/39**. The corrected
smoke-only Cloud Build `ae908abb-962c-4e70-ac5a-38401fa79d9c`, source commit `423535b`, passed
all **8/8** simultaneous requests at concurrency 8 against the exact production image and a
disposable public-seed database. At concurrency 1/2/4/8, p50 was `1.367/1.578/3.768/5.435s`,
max `5.182/3.078/6.505/10.729s`; process peak was `8,012,288 KiB` and container snapshot
`6.709/16 GiB`. The n=8 p95 equals max. This is one bounded burst, not a sustained-load or
percentile SLA. No image was built or pushed.

Since the runner/test contract changed after the previous hermetic build, I reran the full
hermetic suite from latest source commit `016c83dce6f973da7c5546950f8002237adb8e69`. Cloud Build
`ec4f7b47-7f51-46f8-8354-f0230d12793d` completed **SUCCESS**: all **31 configured hermetic suites
exited 0**. As designed, `RUN_ENGINE_TESTS=0` and `RUN_ORCHESTRATOR_TESTS=0`; the live engine and
external Anthropic integration are separate gates, not implied passes. The exact-current-image
live dataset/product suite remains the pass from `17436fb4-3251-433d-9966-37da60eb9551` at source
`39b1aa9`; later commits changed only the smoke/test/docs, not product engine behavior.

No merge, image push, production DB query, traffic change, or deployment occurred. The blocker for
shipping the requested +10.4 Spider points is unchanged and verified: this branch's model contract
is Qwen2.5-0.5B, while the 7B candidate/pool/selector package is separate and not validated as a
production artifact. External-provider testing would incur Anthropic API cost; the harness
requires an API key, so it has not been invoked without explicit cost authority. Production
Cloud SQL acceptance and a sustained load/SLA decision are also outstanding.

## 2026-09-28 — Exact-current-image product and CPU gates passed

Cloud Build `17436fb4-3251-433d-9966-37da60eb9551` completed **SUCCESS** from source
commit `39b1aa9b94797d81ff580362339040283e2be128` using the clean allowlisted suite
context. It tested the exact currently serving image
`sha256:3cbb0037832a06630dc1e0d0a450e44b415e4e5f0ac4ef92b7607de2862891dd` against a
disposable PostgreSQL 16/pgvector database restored from the pinned public seed and refreshed
ECB data. Offline, world, nongeo, world joins, wired routing, geo, schema probes, and the full
dataset prompt/follow-up suite all exited 0. The runner summary says **ALL SUITES PASSED**.
The actual CPU-only `/api/reason` smoke returned the expected world join for all requests:
serial n=3 p50/max `1.214/4.251s`, concurrency 2 n=6 p50/max `1.187/2.374s`, concurrency 4
n=12 p50/max `2.531/4.859s`. Container memory was `6.693/16 GiB`; process peak RSS was
`8,024,748 KiB` (about 7.65 GiB). Small-sample p95 equals max and is not an SLA. The
Cloud Build produced no image (`images: -`); Cloud Run traffic/configuration is unchanged.
The exact image's runtime checks passed on Python `3.11.16`, Torch `2.13.0+cpu`, Transformers
`5.10.4`, spaCy `3.8.13`, sqlglot `30.18.0`, and psycopg2-binary `2.9.12`; `pip check` exited 0.

I compared the measured burst to Cloud Run's read-only service configuration: 8 vCPU, 16 GiB,
container concurrency 8, max scale 3. I extended the isolated smoke to the configured concurrency
cap. Its first attempt (`d1cd9876-d700-46cd-8170-d7eefa9eac8f`) correctly failed: the test sent 45
requests for one test principal inside a minute, exceeding the engine's intentional 30/minute
principal limit and receiving HTTP 429. Inspection confirmed this was the test over-running the
rate budget, not a CPU failure. The test now uses one 8-request burst after the existing 21 samples
(29 total, below the 30/minute gate). Focused release-contract tests pass **39/39**. The corrected
CPU-only smoke (`ae908abb-962c-4e70-ac5a-38401fa79d9c`, source `423535b`) passed all **8/8**
simultaneous requests at concurrency 8. At concurrency 1/2/4/8, p50 was `1.367/1.578/3.768/5.435s`
and max was `5.182/3.078/6.505/10.729s`; the n=8 p95 is its max. Process peak was `8,012,288 KiB`
(~7.64 GiB), container snapshot `6.709/16 GiB`. This checks an 8-request burst under the service's
rate contract; it is still not a sustained-load or percentile SLA. That smoke intentionally skipped
the already-passed full product modules and built/pushed no image.

The full hermetic suite also completed **SUCCESS** as Cloud Build
`66370892-0dd4-4a3c-84ce-237c41e35524`, using the same source commit and exact image. All **31
configured suites exited 0**, including `test_complex_datasets` and the MCP/orchestrator unit
suite. `RUN_ENGINE_TESTS=0` and `RUN_ORCHESTRATOR_TESTS=0` were set by the hermetic harness, so
this does not cover live external-engine or authenticated external-orchestrator behavior. Taken
together, the live product gate and hermetic gate are now green for the existing current
production candidate. They do **not** supply the missing pool-matched 7B selector or prove the
7B Spider DEV improvement is shippable. No merge, promotion, or deployment has happened.
Remaining release gates include an artifact/runtime package for any candidate change,
authenticated external-orchestrator coverage, production-database/operational acceptance, and a
sustained CPU latency/memory/load target. I am checking which can safely proceed without
disturbing the parallel accuracy jobs, rather than treating these passes as permission to claim
or ship the 7B delta.

I also checked the actual model provenance before any release action: this branch's
`engine/model_revisions.py` pins `Qwen/Qwen2.5-0.5B`, and `engine/data/weights_manifest.json`
pins the matching existing adapter bundle. The +10.4 Spider DEV result is from the distinct 7B
experiment; its selector/runtime bundle is not in this branch. Therefore these passing gates do
not make that 7B result releasable. I will not build or deploy a different model under the
7B result's label. The local 7B CPU screen and independent full-eval Python processes were still
active when checked (about 7.9 GiB and 4.2 GiB RSS; about 4.9 GiB host memory free); they were
left alone, and no further memory-heavy local test was started.

Read-only production verification after both builds: Cloud Run remains on
`prereasoner-api-00236-noz` at 100% traffic, and `/api/healthz` returned all four checks true.
No production database was queried; the product regression used only its disposable public seed.

## 2026-09-28 — Retargeting acceptance to the actual production digest

After the successful live suite and CPU sweep, I compared the test image to the live Cloud Run
revision instead of assuming they matched. They did not: `prereasoner-api-00236-noz` is serving
`sha256:3cbb0037832a06630dc1e0d0a450e44b415e4e5f0ac4ef92b7607de2862891dd`, while the earlier suite
used `57d49a…`. Traffic remains 100% on revision 00236; this check made no service changes. I have
retargeted both `cloudbuild.product.yaml` and `cloudbuild.hermetic.yaml`, plus the release contract
test, to the exact current production digest. The full isolated live and hermetic gates must pass
again against `3cbb003…`; until then, earlier live/CPU and hermetic results are valid only for
`57d49a…`. This distinction also does not bridge to the separate 7B/+10.4 Spider diagnostic, which
still has no matched selector/package in this production branch.

## 2026-09-28 — Disposable live product gate and CPU API smoke pass

Cloud Build `9c12a8ab-55dd-4469-817a-876c93b75db7` completed successfully on source commit
`78dbcd91bc10a8f0f5eeefc3de21705f58fd385e` (E2_HIGHCPU_32 worker) using the release image
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:57d49a5da4e5ec2fa03881969e9424bb5032584aaf94bc581b6bbfc74aeb8482`.
It restored the checksum-pinned public Community seed into disposable PostgreSQL 16/pgvector,
refreshed the public ECB source
(`2026-09-25`, content hash
`dfe9ab070c0679d42a174981e07b240131d2db19d20108a5d0e4653d9f61fc9f`), and ran migrations/grants
under the non-superuser serving role. The live regression, world/world-join/router/geo/schema-probe
modules, and the complete dataset prompt/follow-up suite all passed. `tests.test_datasets` finished
with exit 0; FX cases used the refreshed rates and passed the documented tolerance. The CPU smoke
then called the real CPU-only `/api/reason` entrypoint three times for a world join and returned the
expected France total each time.

The exact release image's Python 3.11 environment passed `pip check`; reported versions: Python
3.11.16, CPU Torch 2.13.0, Transformers 5.10.4, spaCy 3.8.13, sqlglot 30.18.0, and
psycopg2-binary 2.9.12. The initial 3-call serial smoke measured p50 **1.18 s**, max **4.29 s**,
container memory **6.63 GiB**, and process peak RSS **7.64 GiB**.

I then expanded the CPU check in commit `b7e97a9` and ran a bounded smoke-only Cloud Build
`c0f5e9d4-2d6c-412f-a522-6c2200b37afe` against the same immutable image and disposable DB. Serial
3-call p50/max were **1.20/4.68 s**; at concurrency 2 (6 calls) p50/max were **1.28/2.49 s**; at
concurrency 4 (12 calls) p50/max were **2.50/4.96 s**. All 21 world-join calls returned the exact
expected result. Container snapshot was **6.70/16 GiB**, process peak RSS **7.66 GiB**. The test
reports p95 as max for these small samples; it is a bounded sanity check, not a production
percentile, saturation, or sustained-load SLA.

This closes the isolated public-seed live-product and CPU-smoke blockers on the refactor branch.
It does not close merge/deploy readiness: authenticated external-orchestrator follow-ups were
explicitly disabled, the live source here is the public Community snapshot rather than production
Cloud SQL, and no traffic changed. The measured result is now recorded; remaining release
prerequisites are being checked without touching production or the separate 7B accuracy worktree.
Important scope boundary: these live/CPU measurements exercise the immutable 0.5B mainline image,
not the separate 7B Spider diagnostic that measured the +10.4-point DEV delta. That 7B bundle still
does not have a pool-matched selector/runtime contract here, so this successful product gate cannot
be represented as having promoted that accuracy gain.

Post-gate local checks also passed: CI's Ruff `F,E9` set across engine/db/deploy/training/tests/
orchestrator/MCP/regression sources, and `compileall` across the same Python source trees. No
production revision changed; read-only Cloud Run inspection still shows 100% traffic on
`prereasoner-api-00236-noz`. Remaining items are candidate-matched 7B packaging/selector evidence,
the external Anthropic-backed orchestrator integration (not run because it can incur API spend),
and any production-data acceptance decision. I am not treating the 0.5B public-seed pass as
validation of the 7B accuracy lift.

## 2026-09-28 — Live gate exposed worker-memory and stale-FX fixture blockers

The corrected disposable build `d635705f-58e5-401a-bf7a-c580fcd92af3` passed the offline 13-case
regression and both curated world goldens (`270` France total; at least `2` France customers). The
live engine run then reported `tests.test_world` and `tests.test_geo` with exit `-9` (SIGKILL), while
`tests.test_datasets` hit its 600-second timeout. The world-join suite itself passed **6/6** and
`test_route_wired` passed; the geo logs also showed FX tests joining on `as_of=2026-09-28` against
the static Community dump with no row for that date, so FX derivations correctly clarified instead
of fabricating a conversion. The API smoke was skipped because this gate version exited on suite
failure. These are not a live acceptance pass.

I tightened the next run against the evidence: request Cloud Build `E2_HIGHCPU_32` (the prior
`E2_HIGHCPU_8` worker has 8 GB host memory despite our 16-GB test-container setting); refresh only
the public ECB release and rebuild its bounded rate calendar inside the disposable DB, logging the
release hash/date range; allow up to 1,800 seconds per live suite; continue to the CPU HTTP smoke
even if a suite fails, then retain the failure exit code. The HTTP smoke now tests the actual
`/api/reason` customers+orders → France world join (expected `270`), not just an own-table sum.
`tests.test_world` now reuses one `KnowledgeReasoner` and its composed planner/router to match the
production single-model lifecycle and avoid redundant model copies. Local release contract tests
remain **39/39**, Bash syntax and Python compilation pass. Next: rerun the disposable lane and record
each live suite, runtime dependency check, join latency, and peak CPU-server memory. Still no Cloud
SQL, merge, promotion, or deployment.

## 2026-09-28 — Disposable live-world rerun found a test identity fixture bug

Cloud Build `f736404e-20cf-428c-a768-eba399d74be4` restored the public seed and passed the
offline regression, then failed both curated world cases before query execution. The live-suite
container set `AUTH_TEST_SUB=localdev`; that makes `regress.live_schema()` treat the value as an
external override and skip registering the `chat.conversation`/owner rows. The production serving
role then correctly rejected its `chat.working_table` insert with a foreign-key violation. This is
a live-test fixture bug, not evidence about world-join accuracy. Removed that fixed override from
the test-suite container (keeping it only on the isolated `/api/reason` auth-bypass smoke), and
added a contract test against recurrence. `tests.test_release` passes **39/39**. Next: commit this
fix and rerun the disposable gate; do not report live product acceptance until world cases and all
live suites complete, and do not connect to production Cloud SQL.

## 2026-09-28 — Live product gate wired with a disposable database

I continued past the hermetic pass and added a dedicated test-only Cloud Build lane. It restores the
public Community dump into a temporary PostgreSQL 16/pgvector Docker container, verifies the
installer-pinned SHA-256, creates a non-superuser `serving` role, runs current migrations/grants,
requires `regress.run_regression --require-world`, and runs every configured live world/dataset suite
with `RUN_ENGINE_TESTS=1`. It runs the immutable production image's Python 3.11 environment without
installing/upgrading dependencies, then starts its real CPU-only HTTP server under the 8-vCPU/16-GiB
limit and checks three known `/api/reason` totals while collecting latency and container stats.

The DB, volume, network, and server are build-local and removed by an exit trap; no Cloud SQL API or
production connection is used. Docker Hub's pgvector/pg16 Linux/amd64 image is digest-pinned. First
Cloud Build `5d25edb8-ac1b-4443-a3fe-42b31cd17311` downloaded the seed, initialized the DB, loaded
the model bundle, and passed all 13 offline cases; it then exited **137** when the live regression
constructed a second model while retaining the offline model. I fixed this test-runner memory bug
by releasing/collecting the offline engine before starting the world tier, and capped PostgreSQL at
2 GiB after its restore-only HNSW build.

Second attempt `5d9f8849-c7c7-4a42-b33c-c07eba0ab227` confirmed it passed the OOM point and reached
world execution, but emitted no new logs after model initialization for over ten minutes. I cancelled
that disposable build to avoid leaving a silent billable worker; it is incomplete, not a pass. Its
two curated world responses exposed a null-result grading bug, now fixed to print clarification/error
details instead of raising. I added bounded per-regression/per-suite timeouts and changed the next
run to execute curated world goldens once, then all seven live engine suites (the 31-suite hermetic
tier already passed separately). Local release contracts are now **39/39**. The next build should
identify the exact slow or failing live module. No production image/configuration changed.

## 2026-09-28 — Clean full hermetic suite passes in the pinned target image

I completed the production-readiness follow-up rather than stopping at a status report. Cloud Build
`3e11903b-948f-4d3e-9279-ebc1e8ed5df8` passed **all 31 configured `tests.run_all` suites** against
the digest-pinned 0.5B mainline image
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:57d49a5da4e5ec2fa03881969e9424bb5032584aaf94bc581b6bbfc74aeb8482`.
This includes the clean model-backed complex-dataset suite **7/7**. The Schema.org coverage ratchet
passed **15/15** and request-limit tests **18/18** locally; release tests are **37/37**. The helper
Node image was pulled as `node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c`,
and is now explicitly digest-pinned in the harness.

The initial remote failures were actual harness/source-boundary problems, which I fixed rather than
counting as passes: `.gcloudignore` was re-filtering the pre-allowlisted archive; test source and
locked CI dependencies were absent; the tests' Git-provenance checks had no repository metadata;
one coverage test referenced the removed hard-coded `WEIGHTS` table instead of the authoritative
manifest; and a mocked chat test lacked a fake key. The final harness supplies the entire explicit
source inventory, preserves only the base image's model artifacts, installs CI dependencies in the
ephemeral test container, and creates a synthetic Git snapshot from the attested file list. No
serving image was built/pushed by this test-only run, and Cloud Run traffic/configuration was
untouched.

Still open and intentionally not conflated with this gate: live seeded-Postgres/world product
tests (no safe staging database/credentials identified), the experimental 7B proposer still has no
matched selector, and complete-engine latency/memory needs a request-level run beyond the offline
13-case timing sample. The release image remains the mainline-matched 0.5B bundle. No merge,
promotion, or deployment has happened.

## 2026-09-27 — CPU envelope gate measured on the production container limits

The follow-up build constrained both offline regression and the actual server container to the
Cloud Run service's configured **8 vCPU / 16 GiB** limits. Commit
`597300ad10e4e8f17328345a9cdf53c423362071` passed Cloud Build
`3477f8e6-5a02-49b0-a21e-17a1f680fdf4`: offline product cases **13/13**, per-case latency
**p50 5.28s / p95 12.84s / max 12.84s**, Python process peak RSS **5,373.4 MiB**, then actual
server startup and health in **16 seconds**. Registry digest:
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:57d49a5da4e5ec2fa03881969e9424bb5032584aaf94bc581b6bbfc74aeb8482`.
The focused release suite passes **36/36**; refreshed SQL AST, provenance, and decomposition tests
pass **124/124**, **11/11**, and **14/14**, with compileall and `git diff --check` clean. This
supports CPU capacity for the **current mainline-matched 0.5B bundle**, but the 13-case sample is not
a hard latency SLA and it does not validate 7B. The live revision `prereasoner-api-00236-noz` still
uses a different digest (`3cbb0037…`); this workstream changed no traffic or service config. Live
world joins/datasets still need an explicitly safe seeded DB. Ruff is not installed in this checkout,
so lint is not claimed.

## 2026-09-27 — Expanded the release smoke to import the real server entrypoint

I added a post-regression Cloud Build step that launches the actual image entrypoint and polls its
HTTP health contract, instead of stopping at importability. Commit
`d30d1872d8ca4b2c3730f5d89b90fdfa6240c972` passed Cloud Build
`11282f3a-6606-4f36-a7db-a11176110a1b`: offline regression **13/13**, then `engine.server` loaded
and `/api/healthz` reported `ok/reason/world/dimension=true` in **22 seconds**. Artifact Registry
independently reports
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:05209bc7baf01efaed96445366042c5da4cc7e2f67e685835dd79e0d323e194a`.
This is an isolated Artifact Registry image tag only; no Cloud Run revision or traffic changed.
The health response demonstrates model startup only; it does not establish a live DB connection or
request-latency/memory SLA. The focused release-contract suite passes **36/36**, and refreshed SQL
AST, provenance, and decomposition tests pass **124/124**, **11/11**, and **14/14**. The independent
7B pool-oracle process remains active in its separate worktree and was left untouched. Live
world joins/datasets still need an explicitly safe seeded DB.

---

## 2026-09-27 — Clean Linux/Python 3.11 build and offline gate passed

Commit `4755ee67ce53f160d931eeadb5d08273a33bf623` produced a clean, allowlisted 109.7 MB build
context with the verified mainline bundle fingerprint. I submitted Cloud Build `4a688469-8c85-421a-a20f-b8bdff9a0afc`
to the supplied `prereasoner-inference` project under the isolated tag
`codex-prod-readiness-4755ee6`. The pinned Python 3.11 Linux image built, installed
`requirements.lock.txt` with `--require-hashes`, loaded the Qwen/adapter artifacts, passed the
Schema.org interpreter startup/load check, and passed the offline regression **13/13**. Build
status is SUCCESS; Artifact Registry independently reports
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:bd5a2be1435eab883b25a1908b973f29211d998bed7728c466cef4c266185330`.
This is the 0.5B mainline-matched package, not the unmatched 7B experiment. It is a tagged image only:
no Cloud Run revision, deployment, or traffic change.

Remaining blockers are still real: the full `tests.run_all` rerun was interrupted at the memory
safety floor during complex model tests while the separate 7B CPU screen ran; the live world/dataset
gate has no safe staging DB (the only Cloud SQL instance is production-named, and I did not use it);
the 7B proposer still has no model-matched arbiter; and full-engine CPU latency/peak memory have not
been measured. Its separate pool-oracle screen is active at **570/1,034** as of 22:45 local time.
The image build proves
the current 0.5B runtime/dependency path only, not 7B CPU serving or +10.4-point product lift.

---

## 2026-09-27 — Full-suite model tests remain memory-gated; no safe DB target found

I ran `tests.run_all` with the live-engine and external-orchestrator tiers disabled, using the
fetched, hash-verified 0.5B mainline bundle. The SQL AST, calculations, analysis, deterministic
emitters, and decomposition suites passed before `tests.test_complex_datasets` entered repeated
model loads. Once free memory fell to 1.1 GiB beside the still-running 7B accuracy job, I interrupted
only this full-suite runner. The run is **incomplete, not green**; the 7B job remains untouched. The
separate offline product regression has its own complete 13/13 pass.

The Cloud SQL inventory for the supplied `prereasoner-inference` project contains only the
`prereasoner-world` instance. I did not connect tests to it: the dataset suite may create/write its
own schema, and this is the production-named database with no separately identified staging target.
Locally, `KB_PG_PASSWORD` is absent and port 5432 is closed; Docker and WSL are unavailable. The
live product gate needs an explicitly safe seeded test database. A second release-hardening issue
was fixed and covered: source/revision validation now happens before the cached-bundle fast path, so
an explicit ignored override cannot return a misleading successful verification. The release suite
now reports **35/35**.

---

## 2026-09-27 — Manifested baseline offline product gate passed

After fetching and verifying the pinned mainline bundle in the production-readiness checkout, I
reran `python -m regress.run_regression --offline` with constrained BLAS threads while monitoring
the independent 7B CPU screen. The complete offline gate passed: engine startup invariants, the
Schema.org interpreter load/decode path, and all **13/13** non-world product cases. Its final
status explicitly says **world tier skipped**; this is an offline-only pass, not full product
acceptance. The first attempt was stopped before case evaluation when free memory fell under 1 GiB;
the second completed with about 2.6–3.1 GiB free and did not touch the 7B job.

The baseline proposer adapter hash equals the shipped arbiter fit hash (`d8939fea…`), with the
arbiter’s 4-beam/96-token pool contract. This confirms the existing mainline candidate is internally
matched; it does not transfer the selector to the experimental 7B proposer. The full offline suite
and complex-fixture tests are next. The live database tier remains blocked: the old dataset worker
is gone, no final output was captured, `KB_PG_PASSWORD` is absent, and port 5432 is closed.

---

## 2026-09-27 — Immutable artifact-source gate tightened; production bundle fetched

I continued the release work in the isolated `codex/prod-readiness` checkout; the separate
accuracy checkout remains untouched. The artifact fetch path now requires the requested Hugging
Face repository and full 40-character revision to equal the values in the checked-in manifest.
This closes the remaining mutable-source/CLI-override hole; tests cover a matching source and reject
repository changes, branch names, missing revisions, and another commit. The fetcher’s operator
docstring now matches its manifest-driven behavior and says to publish only manifested files.

Using that verified fetch path, I materialized the checkout’s exact public model bundle from
`prereasoner/prereasoner-weights@3455714f98cb253ec787473af8a5204c72ad3290`; full bundle validation
returned fingerprint `1400e39e1dec`. The release suite passed **34/34**, provenance **11/11**, SQL
AST/runtime contract **124/124**, and decomposition **14/14**; Ruff, compileall, and `git diff --check`
passed for the fetcher/test change.

I attempted the offline regression against the fetched baseline bundle. The 0.5B model loaded, but
the concurrent isolated 7B pool-oracle process reduced system free memory below 1 GiB during the
run. I interrupted only my own regression process to protect the 7B job; that attempt is **not** a
pass. The 7B process remains active in the `af15` accuracy checkout and its latest checkpoint was
written at 21:13 local. The earlier independent `tests.test_datasets` process is no longer running;
its final result was not captured in this checkout, so live database acceptance remains unverified.

Next executable gates: rerun the full offline regression and complex fixture suite after the 7B
screen exits; recover/rerun the live seeded-Postgres dataset suite; verify the production Linux/
Python 3.11 dependency/image path; then run model-matched candidate parity plus full-engine CPU
latency/memory. No model-mismatched bundle, merge, promotion, or deployment is authorized by these
passing unit gates.

---

## 2026-09-27 — Manifest-driven artifact fetch hardening

While advancing clean-install readiness, I found that `engine/fetch_weights.py` kept a hard-coded
list of nine artifacts separate from `weights_manifest.json`. That would omit newly packaged
runtime artifacts (for example, GGUF model files or a selector) on a fresh production install.
The fetcher now derives its download list from the manifest and rejects unsafe relative paths and
invalid SHA-256 values before fetching. A second pass applied the same path containment rules to
committed artifacts and the general bundle validator (including symlink resolution), closing a
traversal route in staging. `engine/data/README.md` now describes the manifest as the source of
truth. A second code review found that `package_gguf_diagnostic.py` copied the GGUF and tokenizer
files but did not list them under manifest `files`; the validator would accept a local bundle
while a clean build omitted those files. The builder now pins each GGUF/tokenizer file and marks
the generated bundle local-only with no claimed HF revision. A synthetic-package regression
constructs a tiny bundle and proves the files/hash contract. Verification: release suite **34/34**,
provenance tests **11/11**, SQL AST/runtime contract **124/124**, decomposition **14/14**, Ruff on
fetch/provenance/package/release files, Python 3.11 compileall, and `git diff --check` all pass.
Local PostgreSQL answers on 127.0.0.1:5432; the independent dataset integration test remains
active and untouched. The prior 7B served-selection screen exited, but a new 7B pool-oracle job
started in the accuracy worktree; it uses about 9.4 GB, alongside about 4.8 GB for the live dataset
test, leaving roughly 0.5 GB free at the latest check. I have not rerun the model-backed full/complex gates. No runtime artifact was
published, and the 7B diagnostic remains unmatched to its arbiter. Next: wait for the live suite to
finish, rerun the full offline product checks with final code, then proceed through matched
selector packaging, Python 3.11/Linux dependency verification, live DB, and target-like CPU
resource checks. No merge or promotion.

---

## 2026-09-27 — Complex-fixture product blocker fixed; full-suite rerun still resource-limited

The prior candidate-bundle run had genuine complex product failures, so I changed the engine
path rather than weakening a release gate. In `engine/decomposition.py`, leaf compilation now
tries the already-ranked pool in order when an option fails a guard or the supported dual
lowerer. Existing checks remain fail-closed. A second failure had no generated candidate at the
requested category grain: every candidate grouped category revenue by both category and
product. I added a deliberately narrow typed-AST projection for a cross-input measure ranking
only when the leaf wording uniquely identifies its entity head, the ranking orders by an
aggregate measure, the requested entity is projected, and the rewritten AST validates. It
keeps only the requested entity plus the ORDER BY aggregate, and records an evidence tag.
Ambiguous entity wording and other shapes are not rewritten.

Verification: decomposition **14/14**, SQL AST/runtime contract **124/124**, schema coverage
**15/15**, standalone complex-dataset suite **7/7**, and offline candidate-bundle product
regression **13/13**. The latter two were run against the self-contained 7B diagnostic bundle
with `APP_ENV=test`, not a release-ready matched bundle. A later complete `tests.run_all` run
passed 30 configured suites; `tests.test_complex_datasets` failed only its last two model-backed
cases because llama.cpp could not load the base model while a separate 7B CPU-screen process
was consuming memory. The standalone complex suite had passed 7/7 before that contended run.
This is not a green full-suite result; it needs a clean, adequately provisioned rerun. The latest
projection tweak additionally narrows retained aggregates to the ranking's ORDER BY measure;
the focused tests and a targeted rerun of the top-category fixture pass after that final tweak,
but the full standalone complex suite has not yet been repeated after it.

The default `engine/data` regression could not start in this isolated checkout because its
ignored 0.5B/model files are absent; I did not copy from or alter the main checkout. The live
Postgres/world tier remains unrun because credentials/seed DB are unavailable. The self-contained
candidate is still explicitly unmatched (one 7B proposal, neutral likelihood, old arbiter fit
on another proposer/pool contract), and must be refused outside development/test mode. No
merge, promotion, or deployment. Next: repeat complex/full offline checks on an idle runner,
then produce a correctly model/pool/scorer-matched package and validate target Python 3.11
dependency/image, live Postgres/world behavior, and full-engine CPU latency/memory before any
merge recommendation.

## 2026-09-27 — Isolated production-readiness refactor; no merge or promotion

User scope for this chat/worktree: pursue the clean single-source production path and run
product-regression/CPU-serving checks in isolation. Do not merge or promote it. The separate
worktree/chat owns the 80%+ Spider accuracy work.

Work is on managed branch `codex/prod-readiness`, based on main in a separate worktree; the
accuracy worktree and main checkout were left untouched. Detailed current state and remaining
gates: [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).

Added the GGUF CPU proposer path behind the existing `SQLProposer` interface, preserving the
legacy HF/PEFT path and its bundle contract. The review caught concrete hazards before any
candidate could be called serving-ready: one-prompt 7B diagnostic versus a four-beam arbiter,
old 0.5B arbiter versus 7B proposals, neutral versus fitted likelihood features, an unpinned
runtime manifest, and an implicit chat-template mode change. The loader now rejects mismatched
candidate counts/model identities/scoring protocols and requires `runtime.json` to be pinned
by the bundle manifest. Qwen's unspecified thinking mode is preserved, and multiline decoded
SQL is no longer truncated. Focused tests cover these invariants.

Verification on the unchanged main bundle: offline regression **13/13 passed**; the configured
offline `tests.run_all` suites **31/31 passed** (live-engine and external-orchestrator tiers
disabled); after the last runtime-manifest check, SQL AST **124/124 passed**. The world tier
needs live Postgres; `KB_PG_PASSWORD` is absent here. The only initial full-suite failure was
an environment-dependent request test lacking a dummy `ANTHROPIC_API_KEY`; with a placeholder
key (model call mocked), the suite passed. `ruff` passes on `engine/sql_proposer.py`; other
existing lint findings remain in `engine/encoder_overlay.py` and `tests/test_sql_ast.py`.

Accuracy comparison is separately scoped: current branch's best 7B Spider DEV diagnostic is
**755/1,034 (73.0%)** vs main's **647/1,034 (62.6%)**, +108 questions / **+10.44 pp**. It is
not a product score and not an untouched holdout result. It used one proposal with the old
four-beam 0.5B arbiter and neutral likelihoods. A self-contained 7B GGUF+LoRA diagnostic bundle
with a tokenizer snapshot and artifact hashes pinned was exercised through the offline product
entry point on CPU (`n_gpu_layers=0`): **13/13 non-world fixtures passed**. The focused SQL
AST/runtime-contract suite is **124/124**. The candidate/arbiter pair is deliberately unmatched,
so this ran in explicit test mode and does not clear the full product gate. Live Postgres/world
tests were skipped, and no serving latency or memory SLA was measured. llama.cpp emitted
CPU_REPACK fallbacks for LoRA tensors during initialization.

The initial candidate-bundle `tests.run_all` finished **29/31 suites passing**. One failure was
a bug in my diagnostic package builder: it listed `runtime.json` as downloadable rather than
committed, despite the fetcher copying committed artifacts. I fixed the builder, rebuilt the
bundle into a fresh directory, and reran the exact schema-coverage suite: **15/15 passed**.
The corrected-bundle rerun of `test_complex_datasets` still fails **3/7** cases, including
`paris_products` and `top_categories` decomposition/shape errors. The full `tests.run_all` suite
has not yet been repeated after the packaging fix, so I am not claiming a new full-suite score;
these complex fixtures remain the confirmed product blocker.

The bundle remains non-promotable: its one-proposal, neutral-likelihood distribution does not
match the arbiter's four-beam fitted-likelihood contract. The tokenizer is bundled and
hash-pinned, but `llama-cpp-python` is not yet pinned in the serving dependency lock or verified
in the production Python/image. The diagnostic bundle builder now represents `runtime.json` as
a committed artifact and the targeted manifest suite passes. Next is to diagnose the
complex-fixture misses, then repeat the full suite on the frozen intended candidate.
The live Postgres/world tier and target-runtime CPU latency/memory checks remain open. No merge,
promotion, deployment, or traffic change occurred.

## 2026-09-24 — Continued the single-path accuracy work after review

User direction: keep progressing toward higher accuracy and a cleaner integrated release; do not
stop at listing caveats or introduce parallel engine implementations. `CLAUDE.md` already makes
single ownership/non-duplication and serving-faithful evaluation explicit. No `agent.md` or
`AGENTS.md` exists; the repository says not to create a competing rules file. The additional
expectation is now explicit in `CLAUDE.md` under “Accuracy and forward progress”: actionable
findings lead to in-owner fixes, regression tests, evaluation, and the next measured bottleneck;
single-path architecture and authorization gates remain intact.

Codex changes in progress (uncommitted):
- Positive literal grounding now handles symmetric `literal = column` as well as `column = literal`.
- Exclusions (`!=`, `<>`, `NOT IN`) are no longer rejected solely because the excluded string is
  absent from the target column; regression cases pin this policy.
- Excel elapsed formats such as `[h]:mm` preserve the stored numeric duration instead of turning it
  into a date near Excel's 1899 epoch; regression case added.
- Updated the terms-page smoke assertion to tolerate the trademarked “Google Sheets™” wording.
- Proposer SFT now preflights complete train/validation prompt+SQL token budgets before loading model
  weights, appends the EOS token by ID, and refuses overlength targets instead of slicing them at
  the sequence boundary. The README now describes the gate and its default-384 caveat. No training
  or model download was started.

Checks: SQL AST 114/114, release suite 32/32, `npm run test:web` passed, Ruff on changed Python
owners passed, Python compile and `git diff --check` passed. The first web-suite run found the
stale exact-string terms assertion; it now checks the stable “Google Sheets” phrase.

A fresh serving-faithful Spider whole_db run is active under tag `codex_positive_grounding` to
measure the planner-selection effect. At 75/1,034: 48 strict, with 0 wins and 0 losses vs the prior
grounded run on those same examples (median 25.9s, p90 37.8s). This is only an early slice, not an
accuracy conclusion. The run was started before discovering that `full_eval.py` omitted
`sql_grounding.py` from its resume fingerprint. The evaluator now includes that owner and a release
test pins the contract, but the active process has the pre-fix fingerprint in memory; record this
run as diagnostic and use the exact grounder SHA256 `AA5977C15152B7E1CD8F69E278BB514288F426B649E6D2F2B80B3B348E460168`
alongside its dirty-tree provenance. The process had not checkpointed its first 25 examples at the
last inspection. No release, model promotion, deployment, or commit was made. Next: collect the
complete per-example transition, decide whether a clean fingerprinted confirmation is needed, then
prioritize the largest validated miss family in the same existing planner path.

## 2026-09-24 — Proposed 7B-led accuracy program with CPU-only serving

User request (excerpt, formatting normalized): "Ok, go ahead and propose the full plan for 7-8B-led accuracy
development, CPU-only serving as a requirement, and 3B as a measured alternative."

Canonical proposal: [training/proposer/README.md](training/proposer/README.md#proposed-cpu-only-accuracy-program-2026-09-24).
This is a documentation task; no training, paid jobs, model downloads, commits, or deployments
were started. Existing production ownership and the measured 645/1,034 baseline are unchanged.

Key planning decisions:
- Lead: Qwen2.5-Coder-7B-Instruct, Apache-2.0, upstream revision
  `c03e6d358207e414f1eca0bb1891e29f1db0e242`.
- Smaller comparison: SmolLM3-3B, Apache-2.0, upstream revision
  `a07cc9a04f16550a088caea529712d1d335b0ac1`, extended thinking disabled.
  Qwen Coder's 3B sibling has a research-only license and is excluded from the default public
  shortlist. This comparison changes model family as well as size; claims must reflect that.
- Test CPU feasibility first: local 8-core Ryzen / approximately 32 GB; Cloud Run job
  initially 8 vCPU / 24 GiB, actual complete-engine timings and quantized-runtime scores.
- Freeze new data splits and runtime contracts; preserve the consulted-set caveat. Fix the
  training pipeline's target-truncation risk before fitting longer-schema prompts.
- Refit selection on each actual new-model/quantization pool. Do not transplant old arbiter
  coefficients or assume llama.cpp offers the current beam and teacher-forced scoring APIs.
- Improve coverage and selection separately; optional 7B-to-3B distillation only after useful
  teacher measurements. Final result must satisfy both CPU and accuracy gates.
- Proposed $300 staged research cap and 1-3 week development-cycle estimate are planning
  assumptions, not approved spend or a guarantee of 80%. Earlier $25 approval is not reused.
- One final implementation and one promoted bundle; 100% replacement after image gates,
  no tagged public candidate or split traffic. Existing semantic encoder and world-grounding
  owners remain in place; a larger SQL proposer does not replace their shared base pin.

No new accuracy measurement. Validation for this update: documentation/link review and
`git diff --check`; runtime suites are unnecessary for documentation-only changes.
Pre-existing marketplace document/SVG and untracked served Spider summary are preserved.

## 2026-09-23 11:03 UTC — Complete semantic pilot: FAILED accuracy gate; pods=0

All 416 score records downloaded and passed exact question/candidate completeness,
provenance and finite-score checks. Same-pool results: **semantic 210/416 (50.5%)**,
matched feature control **237/416 (57.0%)**, likelihood219, pool oracle302.
Semantic vs matched control: **24 wins / 51 losses / 186 unchanged-correct /
155 unchanged-wrong**. Per DB semantic/control: csu28/32, manufactory53/63,
music_1 41/35, music_4 45/54, soccer43/53. Fixed >=247 / >=3 improving DBs /
max5-loss gates all fail. No dev run or serving integration is justified for this model.

GPU scorer-only runtime is good: **median0.234s, p95 0.575s**, RTX4090, fp32,
batch4, no truncation, total111.43s. Not proposer/execution latency, not CPU latency.
Inference-only recovery succeeded with byte-identical base, frozen code, exact
dependency versions and the existing completed checkpoint. Both pods terminated;
provider API verified **zero pods**. Estimated total compute **about USD0.47**,
not an invoice. No commit, push, deployment, promotion or baseline overwrite.

Authoritative measured ledger updated in `spider/results/RESULTS.md`. Full paired
report: `semantic_launch/comparison.json`; decision: `semantic_launch/decision.json`;
scores: `semantic_launch/score_recovery_download/val.scores.jsonl`. Weights preserved
only as the experiment candidate under `semantic_launch/download/checkpoint`.

### Next-step implications (not launched)

Do not replace the feature arbiter or optimize a switch on these validation labels.
The semantic model's 24 complementary wins support investigating a **fitting-side
trained hybrid with out-of-fold semantic features**, not a validation-derived rule.
A longer schedule is another distinct controlled hypothesis: this pilot sampled only
800 pairs and did not cover a full 1,359-question pairable epoch. Neither hypothesis
is established by this failed result; write a new fixed contract before spending.
Do not recover the missing relabel shard solely to feed another unchanged linear refit
(corrected d2225 and enlarged mixed234 did not improve meaningfully).

Coverage remains an independent hard limit: held-out mixed-pool302/416=72.6%, so80%
cannot come from selection alone. Even an oracle switch between the two measured
selectors reaches only261/416. Serving70% needs724/1034,80% needs828/1034; the current
d2-beam oracle825 cannot reach80%. Keep these distributions distinct.

Current tests: SQL107/107, compose14/14, semantic66/66; compile and diff checks passed.
Production engine files are unchanged from HEAD after removing the rejected coverage
trial. Remaining tracked edits: importer, pool-source identity, lease hardening, tests,
and results ledger. Preexisting/untracked semantic tooling and handoff are preserved.

## 2026-09-23 10:57 UTC — Training saved; CUDA scoring failure recovered as inference-only lease

The pilot completed **200/200 steps in 578.26 optimizer seconds**. All adapter,
config, training-metrics and experiment-contract hashes verify after download.
Checkpoint manifest SHA256:
`91ef6f0ed11f3460cad42d11f20801e0f5e927ab98637a4924e16d289455a1fe`.
Before any validation scores were produced, the next process failed with
`RuntimeError: No CUDA GPUs are available`. This is not an accuracy result; the
precise cause of CUDA becoming unavailable has not been established. Original pod
terminated successfully, and API inventory confirmed zero pods. Its roughly 50-minute
lease at USD 0.49/hour implies approximately USD 0.41 compute (not an invoice).

One **scoring-only** recovery lease is now active: `wjz5a36pyqd8u6`, USD 0.74/hour,
60-minute maximum (<=USD 0.74 compute), session `50946`, state/log names start
`semantic_launch/score_recovery_lease`. This finishes the same approved experiment,
not another training run. Original code archive and saved weights are staged;
the public pinned base is fetched directly and every file must match hashes from
the original training cache. Exact dependency versions and fp32 scoring remain
mandatory. No labels are available to selection and gates are unchanged.

Recovery output destination: `semantic_launch/score_recovery_download`. The downloaded
checkpoint is safely retained at `semantic_launch/download/checkpoint`; no model or
baseline overwritten, no promotion. Total additional compute so far plus this lease's
maximum is approximately USD 1.15, well inside the approved USD 25 cap.

## 2026-09-23 10:49 UTC — Rejected planner trial removed; semantic pilot unchanged

Removed only this task's uncommitted changes to `engine/sql_recursive.py` and
`engine/sql_constraints.py`, plus their two trial-specific tests. Both engine files
now have zero diff against HEAD. The measured +1 pool / -1 top-1 trial is retained
in RESULTS.md, with the caveat that historical A4 also has different evaluator/AST
fingerprints (not a perfectly isolated ablation). No dev-example lexical repair was
attempted. Existing SQL tests after removal: **107/107**; compileall and diff-check pass.
Independently tested self-join importer and shard-source fixes remain.

The semantic pilot is unchanged and was observed at **100/200 optimizer steps**.
Same pod, same lease deadline, same fixed acceptance gate; no validation score yet.

## 2026-09-23 10:47 UTC — Semantic fitting live; coverage gate not passed

The same owned pod `ltk1ng9626g4wo` is now fitting the semantic scorer (50/200
steps observed). Remote frozen-archive tests passed 64/64. The initial large cache
upload hit an SSH reset; recovered its partial bytes using SFTP resume and verified
the full SHA256 before extraction. No second pod was created. The existing lease
owner now supports ownership-checked resume without extending the original deadline
(approximately 12:04:48 UTC). Active local lease session: `86278`; log:
`training/rank/data/experiments/semantic_launch/resumed_lease.log`. Artifacts download
before final termination. Training or a successful score has NOT yet been claimed.

Current local semantic suite passes **66/66**; SQL suite **109/109**, compose **14/14**.
Mixed-pool enlarged partial linear refit is **234/416**, versus the matched original
mixed control **237/416**. Both exclude the full reserved DB bucket. More incomplete
labels alone have not demonstrated a selection gain; missing-shard recovery remains
deprioritized pending the semantic result.

Generic coverage repairs were evaluated fresh over all 1,034 dev questions through
the production planner: **pool 544**, **top-1 364**, compared with historical A4's
543/365. Paired pool outcomes: 1 win, 0 losses, 543 unchanged-correct, 490 unchanged-wrong.
Top-1: 0 wins, 1 loss, 364 unchanged-correct, 669 unchanged-wrong. This does NOT clear
the no-regression gate or support promotion. It is deterministic-only, not a new
measurement of the frozen proposer/arbiter's standing **645/1,034**.

The importer now preserves distinct self-join aliases without weakening validation;
two fitting-side examples previously rejected now round-trip and execute correctly.
Entity aggregate/intersection and positive-membership repairs recover three fitting
diagnostics, but that local promise did not translate into a material dev gain.
Next: finish the bounded semantic run, compare against 237 on identical fixed pools,
and isolate the planner ranking regression before deciding whether to retain repairs.

User-visible update: “The separate coverage run finished with only one extra correct
query in the pool and one fewer correct top-ranked answer, so it is not a promotion win.”

## 2026-09-23 10:08 UTC — Takeover authorized; first semantic GPU pilot launched

User asked Codex to take over all accuracy work toward 70%+ and then 80%+, and
explicitly approved **USD 25 additional RunPod spend** for semantic training and
missing-label recovery. No deployment, promotion, commit or push is authorized.
Claude's prior file-ownership split no longer blocks takeover edits; production
component ownership remains unchanged. No background automation has been created.

### Critical corrected finding

The interim 223/416 refit was NOT clean evidence that more labels cannot help:
`load_pools` used filenames as feature sources, so `shard1_d2beam`/`shard2_d2beam`
likelihoods were invisible to `vector`'s `d2beam` feature branch. Fixed canonical
source identity in the existing owner, with metadata consistency and duplicate
question/SQL rejection. Added a registered regression test.

Corrected local diagnostic refit: **225/416**, vs frozen pilot 224; still flat.
Available fit coverage: **4,672/6,262**, missing **1,590**, and all 416 pilot-val
questions present. Full 16-DB holdout remains excluded from fitting. Candidate
`relabel/arbiter_canonical_d2.json` and `refit_canonical_d2_report.json` are new;
the old artifacts are preserved. Mixed-pool pilot control refit reproduces **237/416**.
These cheap local logistic fits were within the user's explicit takeover/training
request. They are diagnostics, not fresh serving numbers or promoted artifacts.

### Semantic run and spending

Read-only RunPod inventory before launch returned zero pods. One owned pod created:
`ltk1ng9626g4wo`, **USD 0.49/hour**, 120-minute hard lease, <=USD 0.98 compute.
At this entry it is allocated and awaiting SSH, not yet training. Lease session
`11153`; log `training/rank/data/experiments/semantic_launch/lease.log`;
state file in the same directory. Use actual tool state rather than assuming success.
The provider watchdog and existing owner's finally cleanup are active. Added a
tested hourly-price check and best-effort artifact download before failure cleanup;
this does not guarantee durability if a pod is dead or its deadline has expired.

Full approved experiment contract and archive hashes are at
`semantic_launch/approved_contract.json`. Pinned base + rank-8 LoRA/scalar head,
14,015 fitting pairs from 15 DBs, seed 7, 200 optimizer steps max, four pairs/step,
1,800s optimizer budget. No validation checkpoint choice. Gate: >=247/416 on the
fixed mixed validation pool (>=10 over matched control237), gains on >=3 DBs, no
DB loses >5 examples; report paired counts and GPU scorer-only latency (provisional
median<=5s, p95<=15s). A pass funds serving integration, not a 70% claim or promotion.
The remote script installs the existing locked dependencies, runs toy tests, trains,
copies the completed checkpoint to the recovery directory, and scores validation.
Comparison is local after download. No extra paid lease is implied by this launch.

### Parallel local coverage work and documentation

Extended `training/proposer/import_gold.py` to preserve aliases only in repeated-table
scopes, using existing AST fields and unchanged validator. Ordinary query canonical
rendering is retained. Registered tests cover distinct employee roles, movie self-join,
ambiguous columns, alias collisions, disconnected joins and missing qualifiers.
Fitting examples4234 and2486 now import/render/execute with exact strict agreement.
This is importer coverage, not measured candidate-recall/top-1 improvement. The pod
uses its frozen earlier archive, so this separate repair does not alter its inputs.
SQL tests: **107 passed** before the final quoted-ambiguity contrastive addition;
that addition is being rerun. Semantic tests previously60 passed; four new lease tests
passed. Compileall and diff whitespace checks passed before the last coverage edit.

Recorded completed gold-table sanity **689/1,034** in RESULTS.md; whole-db standing
score stays **645/1,034**. No target reached yet. For dev, >=70% requires724 correct;
>=80% requires828. The frozen d2 beam oracle825 cannot support80%; coverage and
selection must improve independently. No claim that merely finishing shard0 will do it.

---

## 2026-09-22 23:39 UTC — Review of Claude fleet/refit progress: resolve three gates before refit/evaluation

User requested thoughts on Claude's latest pasted update. Review only; this log is
the sole edited file. No production owner, experiment code, split, jobs or artifacts
were modified. Acceptance for this review: distinguish locally checked findings from
reported fleet/device claims; no test run or live cloud inspection was needed.

Positive locally checked evidence: `relabel/shard1_d2beam.jsonl` and
`shard1_d4greedy.jsonl` each have 1,600 records, 1,600 unique question keys, and no
overlap with the five pilot validation DBs. This verifies that downloaded shard's
basic completeness/identity counts, not the still-pending full merge or every label.
Arithmetic importer fix remains narrowly scoped; import coverage is not pool recall
or top-1 accuracy. CPU/CUDA 152/152 agreement is reported in Claude's log, not rerun
here; it is benchmark evidence rather than proof for every future shard/model/code.

### 1. Mixed arbiter has a serving feature-contract mismatch (confirmed code)

`spider/probe/full_eval.py:449` documents and constructs a single `d2beam` source
inside `arbiter_select`. The caller scores the pool with one proposer. In contrast,
`pilot_selectors.vector` has separate d2beam/d4greedy likelihood and missingness
features, plus per-source ranks/counts. Training a mixed-source arbiter and passing
it unchanged to this serving path does not reproduce training features or pools.
Before an expensive dev run, either refit a d2-only control for the existing path,
or have Claude integrate genuine two-source pooling/scoring through the existing
owner and prove cached/live candidate, vector and selected-SQL parity. Do not just
rename the artifact or make absent d4 features look available. Dual-source latency
must be measured. Collection can continue while this is resolved.

### 2. Full split consumes previously reserved DBs (confirmed manifest)

`relabel/full_split.json` has 135 fit DBs and five disjoint validation DBs, but 11 fit
DBs belong to the prior md5 `% 10 == 0` holdout bucket:
architecture, cinema, flight_company, machine_repair, medicine_enzyme_interaction,
phone_market, product_catalog, scientist_1, soccer_1, train_station, wrestler.
This does not leak the five pilot validation DBs into fitting, but it conflicts with
the broader-holdout exclusion in Claude's own handoff and Codex's data guard. Keep
these out of fitting (124 fit DBs remain) unless changing that reservation is an
explicitly agreed protocol revision. Collecting their pools is not itself fitting.
Do not treat the five already-consulted validation DBs as untouched confirmation.

### 3. Shard provenance and mixed code revisions (confirmed headers)

Both downloaded shard1 headers have `source_commit: null`, `worktree_dirty: false`,
without content/model hashes in those headers. The old pilot d2 pool records
`source_commit: 0b7af4a...` and `worktree_dirty: true`; Claude states the fleet tarball
was rebuilt after the arithmetic importer change. Do not flatten these into one
claimed identical-code dataset or substitute current HEAD as historical provenance.
Recover exact staged archive/code, adapters and configuration hashes from preserved
launch evidence, preserve each constituent source's provenance, and explicitly mark
unrecoverable identity. Either document heterogeneous training-pool generation or
regenerate required matched controls under the chosen contract. A source completion
count alone cannot validate duplicate-free identity, labels or code equivalence.

Recommendation: continue useful collection and the two independent coverage/semantic
tracks, but resolve these gates before automatic mixed refit -> serving evaluation.
An expanded fitting set may improve accuracy; it does not guarantee the next result
exceeds 645. The current 302/416 mixed validation oracle still precludes 80% on those
fixed pools. The 93 table-complete misses prioritize SQL alternatives, but do not
exclude join-path, alias, or column-linkage errors.

---

## 2026-09-22 23:34 UTC — Semantic scorer implementation complete; no real fitting launched

### User direction and scope

User approved proceeding with the parallel implementation and then said "continue".
User-visible update: "This remains code and testing only—no paid training or promotion."
This section supersedes the earlier status that scorer implementation had not begun.
All edits remain in Codex-owned `training/rank/semantic_pilot/` and this log. No
Claude-owned evaluator, importer, proposer, test registry, production, or frozen
adapter/arbiter was edited. No commits, pushes, cloud leases, real-model training,
or real-model inference were performed by Codex during this work.

### Implemented

- `artifacts.py`: hash-verified package loading, question/candidate identity and
  denominator checks, split isolation, fitting-pair label/weight validation. Fitting
  never opens validation files; inference never opens correctness-label files.
- `model.py`: pinned Qwen/Qwen2.5-0.5B revision `060db6499f32faf8b98477b0a26969ef7d8b9987`,
  joint question/full-schema/SQL sequence classifier, scalar head and rank-8 LoRA.
  No sampled values or numerical S2 features in this initial semantic branch.
  FP32, deterministic-algorithm enforcement, stable padding and local-cache-only loads.
  Adapter/head, evidence, dependency and code hashes are checked on reload. Logits
  are ranking scores, not calibrated correctness probabilities.
- `train.py`: default model-free preflight; real fitting requires explicit `--execute`.
  Proposed fixed schedule: seed 7, 200 steps, four question-pair microbatches/step,
  learning rate 1e-4, 1,800-second optimizer-loop budget. Uniform-question sampling,
  pairwise softplus loss, no validation checkpoint selection. Deadline checks are
  between microbatches, not a hard lease; startup/tokenization are outside that timer.
  An interrupted partial accumulation is discarded. Existing outputs cannot be replaced.
- `score.py`: complete executable-candidate score maps, fixed empty/failed question
  records, preflight with no truncation, separate scorer-only latency. Partial score
  artifacts fail comparison completion checks. This is not serving integration.
- `baselines.py`: cached d2-only S2 replay calls the existing `full_eval.arbiter_select`;
  no duplicated arbiter math. Checks raw source identity/configuration, candidate
  membership, fitting-source compatibility and feature validity. Exports chosen IDs.
- `compare.py`: gold-separated selection, per-question/per-DB outcomes, oracle,
  wins/losses and scorer timing; accepts complete score files or cached S2 predictions.
  README contains preflight, approved-fit, score, replay and comparison commands.

### Measured verification

- Full local semantic-pilot suite: **60 tests passed**. Tiny random one-layer Qwen
  fixtures exercise real PEFT adapter/head save/reload and synthetic optimizer steps;
  these are not fitting the real pretrained model on Spider.
- Existing `python -m tests.test_sql_ast`: **105 passed, 0 failed**.
- `python -m compileall -q engine db training tests orchestrator mcp_server`: passed.
- Tokenizer-only fitting preflight: **31,442 executable candidates**, longest
  **1,755 tokens**, zero truncation against the 4,096-token contract. No pretrained
  model weights loaded. Validation token-length preflight is still performed by the
  scoring command, not independently claimed here.
- Real cached baseline plumbing independently reproduced: **416 questions**, mixed
  oracle **302**, d2 mean-likelihood **219**, cached d2-only S2 **224**. S2 has **14 wins,
  9 losses** against likelihood. No semantic-model accuracy has been measured.
- Input package manifest SHA-256:
  `86b6d34eaf3f1c62210fa97b4e809c4bb37096a7be7e7e25b298674b3feae929`.
  Frozen S2 SHA-256:
  `7591770f54128edaabab082efdebb1e5ff3f5edfa4497ae5ada0f03143908c84`.

### Interpretation and next approval boundary

The S2 replay uses its **d2-only** pool and recorded feature precision. Comparing a
mixed-pool semantic scorer against it mixes coverage and selection effects. Report a
same-pool control before attributing any improvement specifically to semantic scoring.
Cached comparisons are not a fresh serving-faithful run. Validation DBs are excluded
from fitting but already diagnostically consulted; they are not an untouched holdout.
Live-Postgres dataset tests and real-model CUDA/latency equivalence were not run.
Strict dependency-version matching currently also constrains CPU/GPU portability.

Ready for an explicitly approved, separately budgeted semantic pilot; the proposed
schedule above is not a measured runtime estimate or a funding authorization. Before
launch: agree numerical continuation/regression/latency gates, stage the pinned cache,
verify GPU placement/memory, and impose an external hard lease limit. Do not fold this
into the relabel spending cap by implication. No automatic promotion is implemented.
The present mixed validation oracle is 302/416 (72.6%): even perfect selection on this
fixed pool cannot reach 80%; Claude's coverage work remains independently necessary.

### Read from Claude's latest log; not independently rerun here

Claude reports CPU/CUDA agreement on **152/152 selections and candidate sets** across
two benchmark DBs, with maximum likelihood difference 0.0004 over 3,302 candidates.
This supports that tested proposer/arbiter path, not universal device equivalence or
equivalence for the new semantic scorer. Claude also reports the first fleet attempt
produced no downloads and a monitored fleet was relaunched; Codex did not query live
cloud state. His coverage interpretation should remain qualified: having all gold
tables in a pool member does not establish correct joins or rule out linkage errors.

---

## 2026-09-22 20:21 UTC — Parallel-work status clarified

User asked: "Are you going to work on anything in parallel to what claude is doing?"

Read Claude's latest handoff again. Ownership and reported running jobs are unchanged
in that file; live cloud state was not queried. Codex's completed work remains the
coverage audit, semantic-input builder, prepared pairs, and tests. There is no hidden
background training or semantic-model implementation in progress.

The next work on Codex's agreed track is the semantic scorer implementation, its
bounded training entry point, and offline comparison tests under
`training/rank/semantic_pilot/`. Implementing and testing that code does not depend on
the fleet finishing and does not itself require approval to spend on training. The
explicit approval boundary is launching an actual fitting/cloud experiment, not
preparing its code. No additional source code was changed during this status reply.

Intended experiment must score question + schema + candidate SQL, preserve the
gold-separated input boundary, and compare against likelihood ranking and the frozen
S2 baseline on matched pools. Backbone, compute budget, and acceptance gates still need
to be written into the experiment contract before a training launch. No accuracy gain
is claimed for the prepared inputs alone.

---

## 2026-09-22 19:07 UTC — Shared handoff established; Claude's latest update read

### Request and scope

The user requested this root-level file so Claude can read Codex's transcripts,
progress, and results without repeated copy/paste. This is an explicitly requested
handoff document, not an additional agent-rule file. No source-code changes, model
training, cloud activity, commits, pushes, or promotions are part of this update.

Repository HEAD when inspected: `652ba75` (Claude's handoff update), following
`7852182` (numeric arithmetic importer extension). The pre-existing untracked
`training/rank/semantic_pilot/` directory contains Codex's earlier work and is preserved.
No existing `chatgpt-handoff.md` was present; this is a new file.

### Read from Claude; not independently remeasured this cycle

Source: root `claude-handoff.md` and the user's pasted Claude update.

- Claude reports three relabel pods running, a projected approximately $6 run within
  the stated $25 relabel cap, and approximately 3.6 seconds/example on the GPU benchmark.
  Codex has not checked live pod state, charges, throughput, or fleet completeness.
- CPU/CUDA equivalence is **pending**, not passed. The CPU relabel was still running
  when Claude first checked its buffered file. Compare complete matching question sets,
  candidate likelihoods, and selected SQL under matched code/model/prompt contracts.
- The arbiter gold-tables sanity run is pending in Claude's report; no final gold result
  has been verified here.
- Commit `7852182` reportedly extends numeric projection/ORDER BY arithmetic through
  existing typed `BinaryExpr` support while retaining rejection of date subtraction.
  Reported sample import coverage moves 90.0% -> 91.3%. This is **import acceptance**,
  not a measured improvement in pool recall or selected execution accuracy. Codex has
  not independently reviewed or rerun this patch yet.
- Claude remains the integrator for engine, proposer, evaluator, and shared test changes.

### Messages / cautions for Claude

1. **The coverage finding is narrower than "NOT table retrieval."** In the historical
   audit, 93/114 uncovered validation questions had gold SQL accepted by the importer
   and at least one candidate containing every gold-referenced table. That supports
   investigating predicates/projections/aggregation/composition first. It does not
   prove the correct join conditions/path are present, that extra tables are harmless,
   or that an equivalent answer must use exactly the gold tables.
2. **FP32 is a dtype contract, not a CPU/CUDA equivalence proof.** Record complete-set
   coverage, actual maximum/quantile likelihood differences, candidate-set differences,
   and selection agreement. If code changed between the two runs, disentangle the code
   change from device effects before calling the result device equivalence.
3. **Keep generations of labels explicit.** The coverage audit reflects `50ec590` and
   the recorded importer hash, before `7852182`. Preserve it as historical evidence;
   do not silently reinterpret its importer-rejection counts as current. For fleet/pilot
   merges, record the importer/proposer/serializer hashes and scoring device per source
   or shard; existing old/new populations must not be presented as one regenerated pool.
4. **Semantic training remains a separate decision.** Data preparation is done; no
   semantic model/backbone, optimizer, trained weights, or serving hook exists yet on
   Codex's track. The relabel budget does not authorize that separate training run.

### Current Codex state

- No pods launched, no paid jobs dispatched, no training or model promotion performed.
- No Claude-owned source files modified.
- New semantic-input code is uncommitted; generated input packages and coverage reports
  are gitignored. A plain repository archive therefore does **not** contain those data
  artifacts. Upload them explicitly if a later approved training job needs them.
- Next authorized work can build on the prepared input contract. Actual model fitting
  requires a written experiment contract and explicit user approval under repository rules.

### User-visible transcript excerpts from this cycle

User request (verbatim):

> In addition to your regular updates to md files, can your write your transcripts,
> progress, results to chatgpt-handoff.md so that claude can look at it regularly?

Codex response (verbatim):

> Yes. I’ll maintain `chatgpt-handoff.md` at the repo root with timestamped progress,
> results, decisions, and messages for Claude. I’ll read his handoff first and record
> the current state, clearly separating verified results from reported or pending work.

---

## Previous work, recorded 2026-09-22 — Parallel input preparation and coverage audit

This entry summarizes the preceding completed Codex work. It is a retrospective record,
not a new evaluation run. Work began at `50ec590`; Claude's concurrent dependency commit
advanced HEAD to `70ddc49` before the semantic input package was written. Artifact hashes
identify the actual inputs and code.

### Ownership accepted

Codex may create new files under `training/rank/semantic_pilot/` and analysis outputs
under `training/rank/data/experiments/coverage/`. Coverage diagnosis is read-only;
specific engine/importer repair specifications go to Claude.

Claude owns `engine/**`, `spider/probe/**`, `tests/test_sql_ast.py`,
`training/proposer/**`, `training/tools/**`, and the existing rank builder, head trainer,
and pilot selector. Claude is the single evaluator/test-registry integrator.

### Implemented: semantic input preparation, NOT a trained semantic scorer

New files:

- `training/rank/semantic_pilot/__init__.py`
- `training/rank/semantic_pilot/data.py`
- `training/rank/semantic_pilot/test_contracts.py`
- `training/rank/semantic_pilot/README.md`

The preparation code reuses the existing pool merger. It checks source completeness,
duplicate question/SQL records, consistent successful execution labels, provenance,
finite features, split isolation, and deterministic ordering. It rejects fitting DBs in
the proposer's validation hash bucket. Input hashes are checked before and after reads;
shared-merger code changes during execution fail closed. Existing output packages are
never overwritten.

Semantic input is allowlisted to **question + full database schema (types, primary and
foreign keys) + candidate SQL**. Gold SQL and correctness labels are separate. No
gold-selected table subset is used. Numerical proposal features stay namespaced by source
and are available separately, but are not inserted into the semantic text. Sampled cell
values are **not** included in this initial semantic prompt. Token-length preflight and
model implementation still need to be done; no silent context truncation is permitted.

Generated package:

`training/rank/data/experiments/coverage/semantic_inputs/`

| Split | Questions | DBs | Candidates | Oracle-correct | Empty pools |
|---|---:|---:|---:|---:|---:|
| Fit | 1,625 | 15 | 31,457 | 1,486 | 4 |
| Validation | 416 | 5 | 7,614 | 302 | 1 |

There are **14,015 contrastive pairs across 1,359 pairable fitting questions**.
All positives are retained; up to four strong executable d2-likelihood negatives are
selected per question. Pair weights sum to one per question. No validation pairs exist.
Pair-less and empty questions remain in the input/label files and evaluation denominator.

The five output files and their hashes are listed in
`training/rank/data/experiments/coverage/semantic_inputs/manifest.json`. All five output
hashes and five original input hashes were independently rechecked after creation.
The package is approximately 59 MB and contains no trained model weights.

### Completed: read-only coverage diagnosis

Artifacts:

- `training/rank/data/experiments/coverage/pilot_coverage_audit.md`
- `training/rank/data/experiments/coverage/pilot_coverage_audit.json`

The JSON contains the analysis source, input/code/database hashes, diagnostic limitations,
and all 253 uncovered fitting/validation examples. No candidate SQL was relabeled and no
model was fit. The saved mixed S2 score of 237/416 was read from its report, not regenerated;
the mixed fitted model artifact was not available for independent replay.

Exclusive, priority-ordered diagnostic buckets:

| Bucket | Fit uncovered | Validation uncovered |
|---|---:|---:|
| Empty pool | 4 | 1 |
| Nonempty pool, gold import/validation rejected | 73 | 18 |
| Gold importable, no candidate contains all gold tables | 2 | 2 |
| Gold importable, candidate contains gold tables, answer absent | 60 | 93 |
| Total | 139 | 114 |

The raw fitting importer-rejection count is 75 because two empty-pool examples also
reject import. Historical fitting-side investigation targets include:

- 54 grouped-column validation rejections: investigate safe equivalence/functional
  dependencies, not weakened validation or mechanical GROUP BY changes.
- 10 scalar subtraction importer rejections; nonnumeric/date cases need distinct semantics.
- Seven repeated-table/self-join importer rejections.
- Importable compositional misses at example indices 525, 526, 530, and 3275.
- Predicate-literal-set-absence examples 3294 and 3443.

These counts are diagnostics, not guaranteed recoverable wins. Equivalent SQL may use
different tables; importer acceptance does not prove execution equivalence. Validation
questions have now been consulted diagnostically and cannot be called an untouched
confirmation set. Develop repairs on fitting-side families; preserve a separate final
frozen evaluation.

### Verification at the time of this work

- `python -m unittest training.rank.semantic_pilot.test_contracts -v`: **18 passed**.
  ResourceWarnings came from the existing shared merger's unclosed read handles; that
  Claude-owned file was not changed.
- `python -m tests.test_sql_ast`: **104 passed, 0 failed** at that repository state.
  This is not a claim about tests added subsequently by Claude.
- `python -m compileall -q engine db training tests orchestrator mcp_server`: passed.
- `git diff --check`: passed.
- No live Postgres dataset suite, production deployment evaluation, model fitting, or
  new accuracy experiment was run by Codex in this preparation task.

### Prior user-visible conclusion (excerpt)

> Accepted—and the first parallel deliverables are ready. I left every Claude-owned
> file untouched.

> The semantic model itself is not implemented or trained yet. The data foundation is
> ready. Before fitting it, we still need an explicit experiment approval covering the
> backbone, training budget, and acceptance gate; Claude’s proposed relabel spending cap
> does not automatically cover that separate training run.

---

## Earlier review context — Measured baseline and interpretation

These results were checked in earlier Codex reviews, not rerun for this log:

- Frozen d2 greedy policy: **587/1,034** whole_db strict.
- Evaluator-injected S2 over d2-beam pools: **645/1,034 (62.4%)**, with **125 wins /
  67 losses** versus the frozen policy. Not a promoted production configuration.
- Pilot frozen-policy validation: **189/416**; d2 likelihood ranking **219/416**;
  d2-only learned S2 **224/416**. S2 versus likelihood had **14 wins / 9 losses**.
- Mixed-pool pilot S2: **237/416** reported; mixed oracle **302/416 (72.6%)** reproduced.
- Measured CPU arbiter latency: median **26.5 seconds**, p95 **47.8 seconds**; frozen
  policy median **12.7 seconds**, p95 **28.3 seconds**. Soft-budget overruns count toward
  accuracy; this is not accuracy achieved within a hard 12-second deadline.
- Spider dev has been consulted repeatedly for engineering choices. It is not an
  unbiased second holdout. The official test remains reserved for the final frozen run.

Interpretation: arbitration has a measured benefit and merits further work. Reaching
80%+ needs improved pool coverage as well as selection. The illustrative requirement
90% coverage x 90% conditional selection = 81% is arithmetic, not a forecast.
