# Production-readiness workstream

Status as of 2026-09-28: the isolated public-seed live product suites, current-source CPU API smoke,
Python 3.11 dependency check, and static CI lint/compile gates pass on branch `codex/prod-readiness`.
Nothing has been merged, promoted, or deployed. Release remains blocked for any claim that this
ships the +10.4 Spider DEV points: the measured 7B artifact has no pool-matched selector/runtime
package in this branch. The separate 80% Spider accuracy work remains in its own worktree.

### Latest release work (2026-09-28)

- **Latest live product gate passed:** Cloud Build `9c12a8ab-55dd-4469-817a-876c93b75db7` ran
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

## Not ready to claim or promote

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
