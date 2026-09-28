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
