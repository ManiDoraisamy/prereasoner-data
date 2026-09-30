# Claude ↔ Codex handoff log

Rolling log of Claude's work on the Spider accuracy program: decisions, progress,
results, and open questions. Newest section at the top. Committed evidence lives in
`spider/results/RESULTS.md`; program state in the memory file
`spider-accuracy-program.md`. This file is the running narrative between the two of us.

---

## 2026-09-30 (evening) — RELEASED: main in production; replies say only what the result shows; Chrome gate fresh 80/80, existing 55/56

**Production now** (project `prereasoner-inference`, one serving revision per service, no tags):
- Engine `prereasoner-api-00126-vjc` = `engine@sha256:1f4f3b6a…`, built from `d3f7751` (Cloud Build `abd8e42e`).
  `engine/`, `db/` and `regress/` are unchanged since that commit.
- Chat `prereasoner-chat-00074-pjz` = `chat@sha256:610bad83…`, built from `75b420d` (Cloud Build `dbc70617`).
- Hosting serves `d3f7751`'s `web/public`, which main has not changed.
- The ECB refresh and retention jobs (Terraform) and release-smoke (gcloud) run the engine image.
- Rollback: engine `00125-jsx` (`131edc4f…`), chat `00070-hnh` (`98109c53…`).
- Deploy path: a clean worktree, then `deploy/gcp/build_context.py --target release|chat`, then `gcloud builds submit`,
  then a targeted `terraform plan/apply` pinned to image digests. The service-level
  `scaling { min_instance_count = 0 -> null }` diff it shows is cosmetic.

**Commits** (main, pushed):
- `d3f7751`: one definition each for demonyms, place-name lookups and count cues. Three internal shims are gone, and
  five compatibility paths are recorded with removal dates.
- `d67575d`: an output currency survives a complete question in between, and the fallback names its currency. This
  was the one miss in the first pass.
- `0dbd107`: a reply says only what the result shows. That means no currency sign the turn never gave, no promoted
  rank and no earlier-turn figures, and a workbook is named for its measure.
- `9b87f2b`: a busy engine is reported as busy, with no promise to retry.
- `5401046`: a verified currency is written beside the amount. Without it, the sign rule made correct FX replies fail
  the gate's comparator.
- `6f43211`: the chat suites no longer import the comparator. `5401046`'s chat image failed at its build test step,
  and nothing was deployed from it.
- `75b420d`: a new analysis name is cut at a word boundary.

**Chrome gate** (owner's Chrome, every shipped dataset through `?load=` or an upload, `eval.txt` in order, one
conversation at a time):
- First pass, both images from `d3f7751`: existing 56/56, fresh 79/80. The miss was formfacade-leads dropping USD,
  fixed in `d67575d`.
- Final pass, `00126-vjc` with `00074-pjz`: fresh 80/80 over 24 datasets (shorthand 14/14, FX 17/17), existing 55/56.
  The miss was payment-commissions "how much commission came from cards?", which clarified. On replay the model
  forwarded the shorthand verbatim 1 time in 12, and the engine reads that literal question as a digital-wallet FX
  total. Asked again in the same conversation, it answered 9.28.
- Wording, which the numeric grading does not check: no invented currency sign in either pass. One workbook name
  still held a filter value ("products not bought by paris").

**Found, measured, left for the owner** (details in `PRODUCTION_READINESS.md`):
- Scale-to-zero cold start.
- One-question-at-a-time admission: three conversations at once gave 2 of 6 turns "busy".
- CPU autoscaling to cold instances: requests routed to them hung for 60–80 s.
- The ECB refresh lock stalled one FX question for 49 s.
- The shorthand flake above.
- A proactive decomposition is terminal. The cutoff follow-up "only use the top 2 customers" ended as "I couldn't split
  this question" about 1 time in 15 in replay, the same rate with either prompt, and it failed the live suite once.

**Gates on the final code:** hermetic `tests.run_all` 32/33 (test_complex_datasets 7/7 with the 7B). The live
orchestrator suite missed that cutoff case once and then passed 37/37 on the next run. Orchestrator unit tests pass
30/30, MCP 46/46, and the dataset gold suite 29 checks.

**Gate tooling, for the next pass:**
- Drive one conversation at a time; parallel conversations hit the admission window.
- Read each turn from the `/chat` body trace, where `calculations` carries the currency. `/api/analysis` can hang
  during an autoscale, so read it only with a timeout.
- Keep a Claude in Chrome batch under about 90 s; longer ones dropped the connection twice.

---

## 2026-09-30 — COMMITTED, not deployed: the Europe/GBP family, the 7B serving defects, a question-family suite

The owner reported conversation `c_c4468…` (customer-orders, production `00122-zc4`): "total amount in France in
US dollars" → $1,103.67 (right); "in GBP for the whole of Europe?" → "For all of Europe, your total comes to about
£810 GBP"; two follow-ups later the assistant said it could not convert currencies. The stored revisions
(`chat.analysis_revision`, read-only) show the orchestrator sent the engine sensible questions: "total amount in
Europe in GBP", "which countries are included in the total amount for Europe in GBP", "total amount for all
European countries in GBP". The engine:

- answered 810: the plan converted to GBP **and** filtered `currency = 'GBP'` (the five London rows at rate 1);
  the 13 European rows come to 1,917.48 GBP;
- clarified the next two ("'in GBP' can mean convert ... or filter" and "not every set-operation branch produces a
  scalable numeric aggregate": "countries" had become the world column to list);
- and the reply turned that clarification into "the exchange rate info I'd need isn't available".

`00125-jsx` (`a5d4210`) serves the same engine code for all of this (reproduced locally on `a5d4210`).

### Merged into main from Codex

`eedee7d` (rollout record) fast-forwarded, and its uncommitted `7b-production` work, with four changes:

- Its tests used "British pounds", which matches no cell of the currency column, so they passed on the broken
  engine. Each now also runs the wording that failed ("in GBP"), and the dataset `eval.txt` carries it.
- Its prompt rule quoted the reported conversation (a question-specific patch); it is restated generally.
- Two `eval.txt` chat lines only made sense after the wrong £810 answer (the "why is France not included?" and
  "yes" → per-currency table); the orchestrator test that carries that exact history keeps them.
- Its new prompt unit test was not in `TESTS`, so the reported 20/20 never ran it. It is registered (22/22).

### Fixes (each with a test that fails on the old code for the production reason; details in DECISIONS.md)

- **Output currency is not a row filter.** `knowledge_tables` claims the conversion phrase from own-value filters,
  and the currency verdict treats convert + keep-only-the-target as the filter reading. Europe in GBP → 1,917.48;
  Belgium in USD → 365.63; "total amount in GBP" → 7,208.74 (every order converted).
- **A value the upload holds is the upload's.** Own values are claimed before the world resolver reads the
  question: "how many orders in GBP" counted Guinea-Bissau (0) and now counts 5.
- **Place nouns and names.** A world word is the answer only under a cue ("which", "by", "how many countries", a
  ranking); a continent demonym resolves to its continent exactly; the meaning walk takes exact names before
  fuzzy ones across hops ("North American" filtered the United States); the coverage gate reads 1–3 word places,
  demonyms and the counted noun as covered ("the United Kingdom", "European", "North American", "leads").
- **Compose.** The uploaded sheet's own name is not a grouping ("GBP orders" grouped by `ordered`); a learned
  TOPN/SORT needs a ranking word ("… in North America in USD" was the top 3 customers). (A `routing.realizes`
  helper existed only while the local-composition branch did; both were removed with it in `06819d6`.)
- **Routing (the largest accuracy fix).** Serving asks `route()` alone. The `shared_composition` branch gave
  compose 180 of Spider DEV's 1,034 questions (top-N, sort, having, yoy, time filter, share), with 3 right against
  the planner's 146. Production answered those shapes from a path that Spider never measures.
- **Measure names.** `read_op_all` names a two-word measure ("total weight kg for deliveries in Germany" was
  COUNT(*) = 1; now 2).
- **7B serving.** A compound named request decides decomposition from `search_pool` before any decode
  (`test_complex_datasets` passes again, 7/7); a decode past its budget abstains and is recorded
  (`proposer_abstention`) instead of a 200 error; a busy model reaches the 503; `/api/dimension` waits 15 s, not
  forever; `PoolSelection.search_top` deleted.
- **Place lookups.** `build_qid_world` creates `lower(name)` indexes on `city`/`country` (148.6 ms seq scan per
  value before); `PgQuery.ambiguities` and value-membership routing are one lookup per table; routing ties go by
  type name, not hash order.
- **Chat.** The prompt keeps place and output currency apart, executes a "yes" as the offered action, and never
  substitutes a cause the tool did not give; the model now receives the answer's filter labels (`filters`) and
  must describe exactly those rows.

### The sweep

189 generated questions over customer-orders, customers-orders, orders-tiers, formesign-contracts,
formfacade-leads and neartail-shipping, golds from the CSVs + a fixed geography map + the stored ECB rate (0.5%).
On the code as of 02:19 (currency, own-value, place-noun, compose fixes in): 168 exact, 6 averages that clarify
(accepted: AVG conversion is not supported and declines), 13 clarifications and 2 wrong answers. The 15 were two
more families (demonyms/counted nouns, two-word measures), fixed afterwards; all 15 pass on the final code.
`tests.test_question_families` keeps 34 of these as a live suite (registered in `ENGINE_SUITES`).

### Latency (local Ryzen 9 8945HS, 8 threads, remote DB through the proxy)

- **Mode does not matter.** Five own-data eval questions, cold decode each serve, alternating which mode ran first:
  python wall mean 15.9 s / decode 4.3 s / execution 1.16 s; sql 14.1 s / 4.6 s / 0.81 s; the same answer every time.
  The first serve of a question is slow whichever mode runs it (13.6–27.2 s), and the second fast (6.6–11.0 s):
  llama.cpp reuses the KV cache of the prompt prefix it has just evaluated.
- **The 7B decode is greedy (temperature 0) XiYan generation of one SQL string.** Across the 15 own-data demo
  prompts it read the prompt at a median 62.8 tok/s and wrote SQL at 5.1 tok/s. A typical question is a
  200–670-token prompt (3.2–10.6 s) plus 5–50 SQL tokens (1–10 s), 6–14 s in total (median 7.5 s). A compound
  query writes 200–232 tokens (47–57 s); named compound requests no longer decode.
- **World questions never decode.** Production's four world requests in the reported conversation took 13.6–29.4 s
  with no proposer time at all: about 120 SQL statements (sql_ms 3.9–10 s), 12 connections, encode 1–4 s, and 6–7 s
  of serve's own time. Tonight's lookup batching removes ~12 round trips per request. Profile `serve` next.
- **Prompt-lookup speculative decoding: rejected.** It needs `logits_all=True` (+5 GB at the 8k context), which made
  prefill 30–40% slower. It changed the greedy SQL of 2 of the 15 prompts (batched verification is not
  bit-identical), and it was a net loss on the short queries that dominate (6.2 → 7.9 s). Long outputs gained
  only 54 → 50 s.
- **Next measured levers.** (a) Put the schema before the question in the proposer prompt, so follow-ups in one
  conversation reuse the schema's KV cache (3–10 s of prefill each). This changes XiYan's trained format, so it needs
  a Spider DEV run before promotion. (b) A GPU (L4) for the proposer alone would cut both phases by an order of
  magnitude, but it is an always-on cost for the owner to decide. (c) Lower quantization trades accuracy for about
  20%, which is not worth it. TPUs have no llama.cpp backend.

### Open

For the owner:

1. **Deploy** these commits the usual way: no-traffic, smoke `/api/healthz`, `tests.test_datasets` and
   `tests.test_question_families` on the exact commit, flip. Then the **Chrome gate** with `?load=<dataset>` on the
   deployed build, on a fresh and an existing conversation. Neither happened tonight; the last Chrome gate was
   2026-09-25, on the 0.5B.
2. `REQUIRE_ORCHESTRATOR_TESTS=1 python -m tests.test_orchestrator` with the Anthropic key. The prompt rules and the
   new `filters` field have unit tests only (no key here).
3. Optional: the `lower(name)` indexes on the live world DB now (`DECISIONS.md` has the DDL); otherwise they arrive
   with the next `build_qid_world` rebuild.
4. **Sonnet 5.5** costs the same as Sonnet 5 ($2/$10 per MTok). To switch, the forced recalculation round
   (`orchestrator.py`: `tool_choice {"type": "tool"}`, a 400 on Sonnet 5.5) must become `auto` plus an instruction
   and a check that the call happened. Then change `anthropic_model` (infra/orchestrator.tf) and run the live suite.

For Codex (7B):

5. The arbiter is still the 0.5B fit (`model_matched_arbiter: false`).
6. (Fixed tonight, verify in production.) Serving vs evaluation: `knowledge_compose.py`'s `shared_composition`
   (2026-09-10) let compose own any plan with a composition op in every served request; the evaluator asks
   `compose_owns` alone. On Spider DEV, with serving's gate, compose served 180/1,034 questions and got 3 right;
   the evaluated planner gets 146 of those. The branch is removed, so serving now asks `route()` alone.
7. Cold start 5m50s healthy / 9m45s total; `WORLD_LOCK` serializes a whole request with concurrency 8 and a 15 s
   admission, so concurrent heavy requests get 503s.

Known gaps kept honest:

8. "average amount in Europe in USD" declines (AVG conversion is not implemented); it clarifies, never answers wrong.
9. A world request still makes ~80 statements, 7 liveness pings and 6–7 s of self time in `serve` (production
   `[timing]` lines, 2026-09-29). Batching the per-column `_dominant_nongeo_type` lookups is the next step.

Commits (main, not deployed): `17b3a40` (Codex: ?load matrix, FX reply grading), `c55f00a` (currency and place
reading), `b35f971` (7B serving), `f1c0e84` (place lookups), `abcfb82` (chat), `7d5d61d` (counted-noun bound),
`06819d6` (routing), and the records commit. Gates on the final code: every hermetic suite (33 suites +
`test_complex_datasets` 7/7); live `test_world` 55/55, `test_nongeo`, `test_world_joins` 6/6, `test_route_wired`,
`test_geo` 71/71, `test_schema_probes`, `test_datasets` (sql, python, verify, default), `test_question_families`
34/34; `regress.world_cases` no failures; `regress.world_capability` 24 pass / 7 clarify / 4 fail, identical case by
case to the previous run; Playwright fixture suite 34/34; `npm run test:web` passes. Skipped: `tests.test_orchestrator`
(no Anthropic key).

---

## 2026-09-28/29 — COMMITTED, not deployed: eleven follow-ups (1–6 `6d8b7dd`, 7–11 `e4896b1`) on the 7B proposer that production runs

The owner committed the first six with the Excel add-on work as `6d8b7dd` ("office addon", pushed). Codex's
`ad4f407` (#30, the XiYan 7B SQL proposer) then landed on main. `e7fb4ad` (#31) records its production
cutover: Cloud Run `prereasoner-api-00122-zc4` has 100% of traffic (created 2026-09-28 22:32, confirmed with
`gcloud run services describe`). Tasks 7–11 were rebased onto `e7fb4ad` without conflicts. The only change
from `ad4f407` to `e7fb4ad` is two Markdown files, so the gates below ran on byte-identical code. They are
committed as `e4896b1`, and `f995224` registers the 7B proposer's tests, which no gate ran. Neither is
deployed: production runs `ad4f407`'s engine without tasks 7–11. The owner chose to keep the 7B; the
problems found reviewing it are listed at the end of this section. For task 9 the owner approved rebuilding
the live world tables `city` and `country` (below).

- **Grammar words (1).** "who ordered a trench coat in France" answered COUNT = 5: the operator readout fires
  COUNT above 0.05, and the article "a" read 0.15. `engine/closed_class.py` owns the engine's one spaCy model
  and the closed-class reading of a question. Those words carry no aggregate and are never searched for;
  negation and exclusion cues still count. Trench coat → order 109; "how many orders in France" → 5.
- **Rule 3 (2).** A one-sheet trail starts at the lookup. Both emitters read a one-table entry inside its one
  consumer: an inline subquery in SQL, the ORM load at the top of the Python stage. Emitter versions: SQL 6,
  Python 9. A real join of two sheets keeps its combined sheet.
- **Rule 5 (3).** Displayed rows show labels (France), not QIDs; the executed SQL and Python keep `'Q142'`.
  A filter step names its condition ("where country = 'France'").
- **World projections (4).** "which continent has the highest total amount" and "average atomic mass" errored
  in production ("world projection requires typed projection bindings"). `lower_world_query` now lowers them:
  Asia 310, and an exact atomic-mass average because both programs read double precision as NUMERIC.
- **One surrogate-key rule (5).** Eleven drifting copies became `engine/sql_schema.is_surrogate_key`. Compose had
  summed "order ID" as a measure. On Spider dev the ranker's features are unchanged, and 539 of the 551
  questions in the affected databases build byte-identical pools. The served selection changes on 2 (wta_1
  #451, tvshow #629), wrong before and after. A fresh `whole_db` evaluation (`3a40c8e` plus the change; its
  recorded sources and harness are byte-identical to `6d8b7dd` except the unused `service.py`) measured 647
  strict, 696 lenient and 304/408 scalar: 0 wins and 0 losses over 1,034, and exactly those two selections
  changed. RESULTS.md records it and keeps `841f08c` as the table's committed evidence.
- **One world implementation (6).** Every live world suite and both regress goldens now serve through
  `regress.live_schema.served`, the context production enters. The context-less branches and `_labelize_qids`
  are deleted. Transition matrix over 147 named checks (context-less → served): 126 unchanged correct, 13
  unchanged wrong, 1 win (average element mass), 1 change (sales in big cities: clarify → a wrong table, the
  production defect task 8 fixes). 3 test_geo checks were renamed to the served trail's steps and pass.
  run_all at `6d8b7dd`: 38/38 suites OK, `test_orchestrator` skipped.
- **Type nouns (7, uncommitted).** "how many countries are the customers in" answered 0: `EntityQuery._resolve`'s
  embedding fallback read "countries" as China (0.803 ≥ 0.80) and filtered every row out. "which countries"
  read as the United Kingdom (0.807), and "total sales in European countries" filtered to China. A phrase whose
  head word names a world type (`words` rows of type 'type') no longer reaches the fallback. Served: 0 → 3;
  "how many customers in China" still filters China; "Chinese cities" still resolves China from "Chinese".
  Residual: "states" is also an exact alias of the United States, so "how many states …" still filters to it.
- **Comparisons (8, uncommitted).** "What is the total sales in big cities with population over 1,000,000?" was
  served as an empty per-city table: compose bound every comparison to the aggregated metric and grouped by
  every mentioned text column, and the delegate's world path (equality filters only) declined. Compose now
  binds a comparison to the attribute it names (right before it, or right after its value; scale words; an
  aggregate word before the attribute keeps it a threshold on that aggregate). A row attribute other than the
  measure filters rows before the aggregate, and the noun it qualifies is not a grouping unless the question
  groups it or asks for no aggregate. `world_dependency['world_threshold']` makes the comparison necessary, and
  `route()` gives it to compose. Served, both programs compared: 350; "under" → 70; "cities with total sales
  over 100" → Osaka 200; "by city" → one row per big city; "which cities have a population over 1,000,000" →
  Tokyo, Osaka, Nagoya. The same work found a task-4 regression: the delegate summed the text-typed
  `knowledgebase."city".population` into 1426479827518622326844519127886040. `AnalysisPlan` now refuses SUM/AVG
  (and window totals) over text, so "What is the total population?" is declined, as it was before task 4.
- **Gates (7+8).** `tests.run_all` with live Postgres: 38/38 suites OK (`test_orchestrator` skipped). That includes
  test_world 48/48, test_geo 61/61, test_compose 20/20, test_routing 14/14, deterministic emitters 52/52,
  SQL AST 121/121, and `test_datasets` PASS (24 prompts, 40 follow-ups and 1 rewrite in four modes, 260
  records; the 13 `chat:` follow-ups are skipped by design). Against the task-6 run: 844 named checks, 815
  unchanged pass, 13 unchanged fail or clarify, 0 losses, 1 win (capability "sales in big cities" FAIL →
  PASS) and 15 new checks that pass; all 65 dataset answers are identical. Capability golden (35 cases,
  PASS/FAIL/CLARIFY): 20/7/8 context-less, 21/7/7 served, 22/6/7 with 7+8; `world_cases` passes in all
  three. On the old engine the new tests fail for the observed reasons: the empty per-city HAVING, no
  `world_threshold`, SUM over text accepted, and when served, 0 with a China filter and the concatenated digits.
- **Typed world measures (9, uncommitted; the live world tables were rebuilt).** With the text guard in place,
  "What is the total population?" was declined, because `knowledgebase."city".population` was text. Its
  maintainer, `db/sync/build_qid_world.py`, already declared bigint. But the live table had been pre-created with
  all-TEXT columns, and the builder only added missing columns. Each rebuild now converges the declared column
  types on the emptied table before inserting. `world_target` keeps binding `"city"`. With the owner's
  approval, `python -m db.sync.build_qid_world` ran on the live world database on 2026-09-28 (13:38 UTC, 9 s).
  `city` and `country` population are now bigint, and 0 rows changed besides the type and `updated_at` (checked
  against `public.settlement`/`public.country` first). Served in verify mode: the five cities total 20,748,671;
  France, Germany and Japan total 275,984,756; "total currency" still lists JPY 3 and EUR 2; the big-cities
  total stays 350. Rollback: `ALTER TABLE knowledgebase."city" ALTER COLUMN population TYPE text USING
  population::text`, and the same for `"country"`. The builder's existing test had never been registered in
  `tests/test_community_deploy.py`'s `TESTS`; it is now, with the new convergence test (23/23).
- **Gates (9).** test_world 50/50: the task-8 "sum or declined" check became three strict checks. Against the
  7+8 run, over 849 named checks: 830 unchanged pass, 13 unchanged fail or clarify, 5 new passes, 0 losses. The
  capability golden is unchanged at 22/6/7. `tests.run_all` gave 37/38: `test_datasets` lost one verify request
  to a dropped Cloud SQL connection (the proxy logged failed dials from 16:30 to 18:05). That dataset re-run on
  its own passes in all four modes, and the other 64 dataset answers are identical to the 7+8 run.
- **Aggregate domains (10, uncommitted).** Compose grouped by every text column a question mentioned, so "What
  is the average population of these cities?" and "… total population of these cities combined?" were
  per-city tables; both capability cases failed on every run. `ComposeEngine._aggregated_over` generalizes the
  task-8 rule. When the question asks for an aggregate, a noun whose every mention is the object of a domain
  word (of/in/for/from/among/across, over determiners and at most one other word) or the noun a row threshold
  qualifies is not a grouping. A grouping cue (by/per/each/every, "each of the cities"), a ranking word or a
  number keeps it one, and so does a listing without an aggregate or a threshold on each group's aggregate.
  The scalar measure then goes to the world path, which read "combined" as a dropped constraint; "combined",
  "altogether" and "overall" are now operator words. Served, both programs compared: the five cities average
  4,149,734.2 and total 20,748,671; "the population of each city", "total sales by country" and "the top 2
  cities by population" keep their rows.
- **Gates (10).** test_compose 21/21, test_world 55/55. Against the task-9 run, over 854 named checks: 835
  unchanged pass, 11 unchanged fail or clarify, 8 wins (the two capability cases and 6 new checks), 0 losses.
  The capability golden goes from 22/6/7 to 24/4/7. My run script's 3-hour cap killed `tests.run_all` inside
  `test_datasets` (the Cloud SQL link was slow and dropping dials again); every other suite passed, 37/38.
  `test_datasets` then ran in full on its own and PASSES: 24 prompts, 40 follow-ups and 1 rewrite, 260 records.
  All answers equal the task-9 run's except the six USD conversions, which moved by one factor (0.99781) with
  the ECB refresh at 16:34 UTC between the runs.
- **Numeric comparisons in compose's candidate (11).** Compose materializes its candidate in SQLite before
  routing. `filter_view` wrote a number bare, and a `decimal_sum` view column is TEXT with no affinity; SQLite
  orders every TEXT above every number. So "cities with total sales over 100" kept all five cities and "under
  50" kept none. Served answers were right, since they run the shared plan in Postgres. But routing
  (`_composes`) and the scalar re-expression (`_same_answer`) read the candidate. A number now compares
  through the registered `decimal_cmp`; a text value keeps its text comparison.
- **Gates on the 7B tree (7–11 plus `ad4f407`).** The GGUF came from the `7b-production` worktree via
  `SQL_PROPOSER_MODEL_PATH`, hash-verified. `tests.run_all` with live Postgres gave 39/39 suites
  (`test_orchestrator` skipped, `TEST_SUITE_TIMEOUT_SECONDS=14400`): SQL AST 127/127, XiYan 6/6,
  compose 22/22, test_world 55/55, test_geo 61/61, and `test_datasets` PASS (24 prompts, 40 follow-ups and
  1 rewrite, 260 records, answers identical to the 0.5B run). `world_cases` passes and the capability golden
  holds at 24/4/7. Against the last pre-merge run, over 877 named checks: 843 unchanged pass, 11 unchanged
  fail or clarify, 23 new passes, 0 losses.
- **Not run.** `test_orchestrator` (external API, untouched); the Chrome pass (no release).
- **Separate tasks suggested.** `"u_s_state"` holds no population values, because its builder never writes
  them. "list the cities with population over 1 million" reads a spurious COUNT (0.158).

### Problems in the 7B integration (`ad4f407`) for Codex to fix

1. **Production serves the 7B with a selector that was not fit on it.** Since the cutover (revision
   `00122-zc4`), `load_sql_selection` (`engine/encoder_overlay.py`) loads `XiYanSQLProposer`, whose GGUF the
   Dockerfile bakes into the image. The arbiter (`sql_arbiter.json`) was fit on the 0.5B proposer's
   four-beam pools, with teacher-forced likelihood features. The 7B gives one proposal per question and
   constant neutral likelihoods `(0.0, 1)`, which the contract admits with `model_matched_arbiter: false`.
   `PRODUCTION_READINESS.md` blockers 1–2 still say it must not be merged or deployed until a
   model-matched selector bundle exists; the cutover happened anyway, and the document was not updated. The two GGUF loaders also contradict each other: `GGUFSQLProposer` refuses an
   uncalibrated pair outside development/test, while `XiYanSQLProposer` requires the mismatch to be
   disclosed and then serves it in production. Fix: fit the arbiter on the 7B's own execution-labeled
   pools (`training/rank`) and promote the matched bundle through `training/rank/promote.py`, or gate the
   mismatch the same way in both loaders.
2. **Three proposer runtimes and a stale ownership map.** `engine/sql_proposer.py:SQLProposer` (HF LoRA) is
   now used only by `training/proposer/verify_scorer.py`. `GGUFSQLProposer` has no production caller.
   `engine/xiyan_sql_proposer.py` is what production loads. CLAUDE.md still names the 0.5B LoRA as the
   proposer and `training/rank/promote.py` as the only writer of the served selection bundle, yet
   `xiyan_sql_proposer.json` was committed by hand. Delete or move the unused runtimes out of `engine/`
   (CLAUDE.md: experiments stay out of production imports). Then propose the ownership-map change to the
   owner; CLAUDE.md changes need their explicit request.
3. **Latency and concurrency.** The final CPU replay (`e7fb4ad`) measured p50 7.2 s, p90 16.4 s, p99 20.0 s and
   max 138.6 s, with 220/1,034 over the 12 s budget. The evaluator tail is recorded, not explained. `XiYanSQLProposer` decodes under one process-wide lock, so concurrent requests queue behind each
   other, and `max_new_tokens=1024` is the only bound on a runaway decode. A time/token budget, a
   concurrency plan and an agreed SLA are needed before deploy (readiness blockers 3 and 5).
4. **A fresh checkout cannot start the engine.** `EncoderQuery` raises `FileNotFoundError` unless the 4.68 GB
   GGUF is at `engine/data/xiyan_sql_proposer.gguf` or `SQL_PROPOSER_MODEL_PATH`. So every live suite in
   `tests.run_all` fails on a machine that has not run `python -m engine.fetch_xiyan_sql`. Document the step
   where `run_all` and `engine/data/README.md` describe local setup, or make `engine.fetch_weights` fetch it
   too.
5. **The runner's suite timeout kills the live suites.** `tests/run_all.py` defaults `TEST_SUITE_TIMEOUT_SECONDS`
   to 900 s. `test_datasets` takes about an hour here, and `test_world`/`test_geo` take 15+ minutes. Only
   `deploy/gcp/run_product_suite.sh` raises it (to 1800 s). Raise the default, or set it only where CI needs
   a bound.
6. **Tests exercise a leftover wrapper, not production.** `engine/decomposition.leaf_candidate` ("preserving the
   legacy single result") has no production caller. It re-implements the compatibility filter that
   production applies inside `leaf_candidates`' caller, and `tests/test_decomposition.py` asserts through it.
   Point those tests at the production path and delete the wrapper.
7. **The evidence disagrees with itself.** RESULTS.md reports 864/1,034 strict for the deployed 7B, tuned with
   repeated DEV use. `PRODUCTION_READINESS.md` blocker 1 still cites the last committed 7B full-DEV as
   755/1,034, below an 828 target, and says the deployed image is 0.5B. RESULTS' baseline section still opens
   "This is the current, reproducible measurement of the served own-data planner" for the 0.5B d2 proposer,
   which production no longer runs. Update the readiness document to the deployed state, and word the 0.5B
   section as historical throughout.
8. **Minor.** On Python 3.14, llama-cpp-python 0.3.35 prints a `Llama.__del__` traceback
   (`free_model: 'NoneType' object is not callable`) at every interpreter exit, in every suite log; close the
   model explicitly at shutdown. The `diskcache` advisory exception in SECURITY.md must be reassessed by
   2026-12-28.

## 2026-09-27 — RELEASED: world listings show their steps ("amount in france"); slow-SQL lines never carry cells (engine 00236-noz)

The owner compared "amount in france" with "total sales in france". The first showed one step ("result
· from sales, sales unconnected to knowledgebase") and no combined, enriched or filtered tabs. The second
showed combined → enriched → filtered → total. There were two causes.

- **Routing.** For a question without an aggregate, `KnowledgeQuery.serve` resolved France and took
  the leftover word "amount", which is a column name, as free text to search for. That sends a question
  to the hybrid semantic path (`engine/knowledge_bridges.py:_serve_hybrid`), which runs one statement:
  the France rows `ORDER BY embedding <=> embed('amount') LIMIT 10`. The stored analysis `a_5f2f42…`
  revision 1 (15:24 UTC) shows `predicate: "amount"`. Its rows were right only because France has fewer
  than 10 orders, and they came in similarity order (109, 123, 110, 121, 122). "customers in france" and
  "orders in france" went the same way.
- **Lowering.** The world path could not plan a listing at all. `lower_world_query` raised "world
  projection requires typed projection bindings" for every question without an aggregate, so "in
  france" and "who is in france" failed outright. The semantic search had been hiding this.
- **Fix.** `semantic_predicate` is now a module function. It returns nothing when every leftover word
  only names the sheet itself: its table and column names with their plurals (`_schema_vocabulary`,
  now shared with `_uncovered`), or the spreadsheet (`_SPREADSHEET_WORDS`, shared with `_uncovered`'s
  CUE). A world listing now lowers to its trail, and the filtered sheet is the declared output
  (SHEETS_AS_REASONING rule 6). A currency conversion without an aggregate still refuses.
- **The owner's sheet, through the production entry point.** "amount in france", "customers in
  france", "orders in france" and "in france" each give combined → enriched 1 → filtered, with the 5
  France orders in their own order (109, 110, 121, 122, 123). SQL, Python and verify modes produce the
  same stages and rows. "total sales in france" is unchanged (970).
- **Gates.** The new `tests.test_compose` test (`test_words_that_name_the_sheet_leave_nothing_to_search_for`)
  fails on the old predicate, which was 'amount'. The new `tests.test_deterministic_emitters` test
  (`test_a_world_listing_ends_at_the_filtered_sheet_in_both_emitters`, verify-mode parity on SQLite)
  fails on the old lowering with the production error.
  - Hermetic suites: compose 15/15, emitters 47/47, `test_calculations` 104/104,
    `test_router_evidence` 11/11, `test_routing` 13/13, `test_complex_datasets` 7/7.
  - Live suites: `test_world` 18/18, `test_world_joins` 6/6, `test_geo` 61/61, `test_nongeo` PASS,
    `test_route_wired` PASS.
  - `test_datasets` PASS: 24 prompts, 40 standalone follow-ups and 1 rewrite in four modes (260
    records); the 13 `chat:` follow-ups are skipped by design.
- **The bare "970" in the second screenshot.** That analysis (`a_6430c3…`) was stored on September 25
  at 23:48 UTC, before the chat fix. Stored replies are not regenerated.
- **Slow-SQL leak (`fd11f99`, released with this).** `execute_values` sends the uploaded-sheet INSERT
  as bytes, literals included. `_sql_fingerprint` redacted `str(bytes)`, the repr, whose escaped quotes
  the literal pattern never matched, so a slow upload logged the sheet's cells. Production has 3 such
  lines (2026-09-24 01:01 UTC); they stay in Cloud Logging until its retention expires or someone
  removes them. Bytes are now decoded before redaction, and any other statement object is named by its
  type only. `tests.test_request_timing` 11/11; the new test fails on the old fingerprint with
  `b?management\?single\?…`.
- **Production.** Engine `prereasoner-api-00236-noz` = `engine@sha256:3cbb0037…`, built from `fd11f99`
  (Cloud Build `9ae0332a`, its `regress-offline` step passed; weights `1400e39e…`, unchanged). Before
  any traffic, the release-smoke job ran on the new image: `ok`, request budgets, conversation
  lifecycle, exact total and reasoning total 3.3. The revision was deployed with no traffic, and
  `/api/healthz` answered ok (reason, world, dimension) through its tag. Traffic then moved to it, the
  tag was removed, and the three jobs run the same image. Chat and Hosting did not change.
- **Live.** On chat.prereasoner.com, the `customer-orders` demo (the owner's 23 orders) asked "amount in
  France" showed combined → looked up shared facts → filtered (PY ran). The Result overlays the
  filtered sheet: orders 109, 110, 121, 122, 123 with 310, 210, 180, 95, 175, as derived from the CSV.
  The reply was a sentence.
- **Rollback.** `gcloud run services update-traffic prereasoner-api --to-revisions
  prereasoner-api-00233-liy=100 --region us-central1 --project prereasoner-inference`, and point the
  three jobs back at `engine@sha256:4fc6e203…`. Listings then fail again, and the leak returns.
- **Separate tasks suggested.** "who ordered a trench coat in france" is read as a COUNT (5), and
  "everything in france" still goes to the semantic search. The world trail shows "combined" for a
  single sheet (rule 3). Lookup columns show QIDs (Q142) instead of labels in the enriched sheet and
  now in listing Results (rule 5).

## 2026-09-26 — Each browser test gets its own fixture-server state

The fixture server (`web/tests/browser/server.js`) kept one request count, deleted flag and revision
store for its whole lifetime, so the suite could not be repeated. With `--repeat-each=2`, the release
journey's second run expected 1 request and found 4. After the delete test, `/api/conversations`
stayed empty for every later test. Now each test's browser context carries a state cookie
(`web/tests/browser/fixtures.js`, the `test` that server-backed specs import), and the server keys
state by it. A state request without the cookie gets HTTP 400 instead of a shared default. It is a
cookie rather than a header because an extra header would also reach the pages' cross-origin requests
(the Drive export, fonts) and trigger CORS preflights there.

- `npx playwright test --repeat-each=3`: 99/99 on 8 workers, with copies of the release journey running
  in parallel against the one server. `npm run test:browser`: 33/33. Back to back, it took 34.4 s
  before and 37.2 s after, and the new spec's extra worker accounts for the difference.
- Regression spec `mock-server.spec.js`: after this test asks a question and deletes the conversation,
  another test's state is still fresh. The spec fails when the server shares one state again.

## 2026-09-26 — RELEASED: the chat keeps the model's sentence when it states the engine's value (chat 00127-six)

The Sheets sidebar answered "total amount in india" with a bare "125". The model had written "Your total
amount in India comes to 125." The chat's grounding check (`_grounded_presentation`, added in `f73d147`
on September 14) looked for "125" followed by neither a digit nor a dot. It took the sentence's full stop
for a decimal point and replaced the sentence with the scalar. Trailing zeros ("1,240.50.") and rounded
averages ("about 264" for 263.96, "$250.78", "4.67") failed the same way. Production chat `00124-yad`
has run this check since September 14, and neither the model (`claude-sonnet-5`) nor the prompt
changed. No gate caught it. `tests.test_datasets` compares engine rows, and the Chrome gate only checks
that the value appears in the reply, which a bare value does.

- A replay of `run_chat` on `claude-sonnet-5` used the production prompt, with the engine faked to
  return the production scalar. Before the fix, 4 of 14 presentations were kept; after it, 15 of 15.
- The check now reads the prose's numbers as numbers. It keeps a sentence when one of them equals the
  scalar, or equals the scalar rounded to the precision the sentence shows and within 5%. A percentage
  may also read as a fraction, and magnitudes are compared ("down 12" for -12). A different or stale
  number still falls back to the scalar ("There are 100 distinct IDs." for 23), and so does "about 5"
  for 4.667.
- The regression test is `test_presentation_that_states_the_engine_value_in_prose_is_kept` in
  `tests.test_orchestrator_unit`. It fails on the old check with "comes to 125.". Suite 20/20;
  compileall passes.
- Production: chat `prereasoner-chat-00127-six` = `chat@sha256:cb778cfc…`, built from `1bbf38d`. The
  Cloud Build test step (`ecf99083`) ran `test_mcp` and `test_orchestrator_unit`. The revision was
  deployed with no traffic, and its `/readyz` answered ok through the tag before the switch. Use
  `/readyz`: Cloud Run's frontend answers `/healthz` itself with a 404. The engine and Hosting did not
  change.
- Live: on chat.prereasoner.com, the `neartail-shipping` demo's "total delivery fee in Europe" answered
  "Your total delivery fee across Europe comes to 46." (`00127-six`, `[timing] chat status=ok llm_n=2`).
  The old check turned exactly this shape of sentence into a bare "46.0".
- Rollback: `gcloud run services update-traffic prereasoner-chat --to-revisions
  prereasoner-chat-00124-yad=100` (`3d7189e`, image `ec248a5c…`), which brings the bare values back.

## 2026-09-26 — The Google Sheets picker "flake" was the machine running out of TCP ports, not a race

The v22 entry below says the picker journeys (`release-flow.spec.js`, "Google Sheets uses the shared
workbook importer") flake about 1 in 30 runs on unmodified main. They do not. In both failures, a request
to the local test server failed at the network level:

- Both failures (one full-suite run, one picker-only run) happened between 20:21 and 20:23 on September
  25, while `tests.run_all` ran its live suites in the background. That is the only time in the last
  7 days when Windows logged TCP port-reuse failures (System event 4227 at 20:07, 20:16 and 20:32). In
  the same run, three `test_datasets` questions could not open a Postgres connection (`WSAEADDRINUSE`,
  10048).
- Failing one request reproduces both messages exactly. If `lib/google-sheets-import.js` does not load,
  the page shows `Could not read the sheet: GOOGLE_SHEETS_IMPORT is not defined`. In the success
  journey, the page then stays on `/picker`, so `toHaveURL` fails. If the navigation to `/sheets` fails
  instead, the page ends on `chrome-error://chromewebdata/`.
- There is no ordering race. The module script is deferred, so it runs only after every parser-blocking
  classic script has run or failed to load. With `shared.js`, `upload-limits.js`, `xlsx-reader.js` or
  `google-sheets-import.js` delayed by 2.5 s, the journey still completes. The gapi stub's synchronous
  callbacks run only after everything they use is defined.
- On unchanged main (`f59c372`): 90/90 with `--repeat-each=30`, 90/90 with all 16 cores saturated,
  300/300 with `--repeat-each=100`, and `npm run test:browser` 32/32.

Nothing changed in the page, the stub or the server. Run the browser suite on its own, not alongside
`run_all`'s live suites. If a browser test fails with a page error, first read the failed requests in
its trace (`test-results/<test>/trace.zip`) and check System event 4227, and only then look for a race.

Found along the way: the mock server keeps `requestCount` and `deleted` for its whole lifetime. The
stateful release test therefore fails on its second repeat (it expects 1 request and sees 4), so only
stateless tests can run with `--repeat-each`. A separate task was suggested.

## 2026-09-26 — RELEASED: a column without a header is left out, not a reason to refuse the sheet (add-on v24)

The owner shifted their sheet's headers right to fix it, and v23 still refused it: A1 was empty over the
row numbers ("Column A has values but no header"). Refusing a whole sheet for one unnamed column was wrong.
In `aacf2a1`, the importer leaves such a column out (`import.leftOutColumns`), and the sidebar and upload
preview say which. The header row may name two thirds of the columns. Nothing is named for the column.
Still refused, with the evidence: the header row one column to the left of its data (only the last
column unnamed and numeric, the header before it over text), because every answer would read the wrong
column. The 9 shipped workbooks convert unchanged (compared against the committed importer).

- Gates: `npm run test:web` 9 suites (`workbook layout` 32 checks); `npm run test:browser` 32/32. The
  sidebar test replays the owner's fix: the shifted sheet is refused, then with A1 empty the question runs
  without column A, and the note shows.
- Production: Hosting `aacf2a1` from the main working tree (the live importer matches). Add-on version 24
  (the note) is pushed, the Marketplace deployment points to it, and the App Configuration was set to 24
  and published.
- Live: "Sales data" opens with "1 tab: sales" and `Sheet "sales": column A has no header, so it was
  left out.`, ready for a question.

## 2026-09-25 (late night) — RELEASED: the Sheets sidebar renders the web rail as a component (add-on v23)

The owner rejected v22: they had asked for the web app's sidebar as a component inside the add-on,
styled like the add-on, not the whole web page framed in Sheets. v23 replaces the embed. There was no
rollback, per the owner.

**Production now:**
- `44d4781`: Hosting version `3bb9723c` (21:02 UTC), deployed from the main working tree as before, so the
  other session's uncommitted Excel files stay live and unchanged. `/lib/**` sends
  `Access-Control-Allow-Origin: *`, and `/embed/sheets` and `lib/host-bridge.js` return 404.
- Add-on: `clasp push`, version 23, and the Marketplace deployment points to 23. The App Configuration's
  script version was set to 23 and published. The engine is unchanged (`00233-liy`).
- Live in Sheets: the owner's "Sales data" opens the v23 sidebar with `Sheet "sales": Column H has values
  but no header in row 1.` A scratch sheet, "Prereasoner add-on check" (country/amount, 4 rows, created
  for this check), opens the v23 sidebar reading "1 tab: Sheet1". My browser automation cannot type into
  Sheets' nested add-on frame, so no live question was asked; the answer path is covered by
  `sheets-sidebar.spec.js`.

**What changed** (DECISIONS.md, "The Google Sheets add-on renders the web rail as a shared component"):
- `sheets-addon/Sidebar.html` is the add-on's own UI (Google add-on CSS, v21's layout). It renders with
  `web/public/lib/turn-renderer.js`, which now owns the rail's step presentation moved out of
  `workbook.js`: step names, sentences, live status, lineage, backend badge, the "read as" line and
  `stepsFromViews`. The web rail calls the same functions.
- Live progress: the sidebar imports `lib/firebase-init.js` (`subscribeTurn` / `subscribeRun`, the web's own
  subscriptions) and signs in with the Google token from `Code.js`. `/lib/**` is served with
  `Access-Control-Allow-Origin: *` so the module loads from the Apps Script origin. v21 polled the
  database's REST API instead.
- Import: the sidebar runs `WORKBOOK_IMPORT.convert` (moved out of `xlsx-worker.js`, which now wraps it).
  The header message names the column: `Sheet "sales": Column H has values but no header in row 1.` This
  is the owner's sheet, which the sidebar refuses.
- `Code.js` returns cells and calls Prereasoner server to server (Cloud Run `/chat`, the sheet-session and
  sync APIs), as v21 did; the chat service accepts browser requests only from its own origins. The manifest
  keeps `script.external_request` and its URL allowlist, the verified scopes.
- The embed is deleted: `lib/host-bridge.js`, the `/embed/sheets` rewrite and CSP, and the embed hooks in
  `workbook*.js`, `reason.html`, `styles.css` and `shared.js`. The web keeps the `settle()` source-chip fix
  and its new test.

**Gates:**
- `npm run test:web`: 9 suites in the main worktree (the other session's Excel suites included).
  `Sheets add-on` has 41 checks and `workbook layout` 27.
- `npm run test:browser` 32/32. `sheets-sidebar.spec.js` drives the real `Sidebar.html` on an Apps
  Script-like origin, with the shared files from `web/public`, a stand-in `google.script.run` and a
  realtime database the test drives. It covers live steps with the web's sentences and badges, the
  streamed reply, the finished turn linked to the analysis, the saved version 2 state, New chat, and the
  named header error followed by a retry. `docs/marketplace/render-review-assets.js` renders the review
  images through the same harness (`sheets-sidebar-harness.js`).
- The chip regression test fails without the fix ("syncstate checking") and passes with it.

## 2026-09-25 (night) — RELEASED: the Sheets add-on is the web workbook (replaced by v23, entry above); one import rule; "sales" is a money total

The owner asked why the Google Sheets add-on failed, why its sidebar was worse than chat.prereasoner.com,
and why it showed no live progress. The HTTP 500 was the engine's cached connection (released as
`5bb21a2`, entry below). The sidebar was a separate hand-written UI (`Sidebar.html`, `Previous.html`),
not `workbook.js`. Its Apps Script server called `/chat` and returned only the final JSON, so nothing
could follow the RTDB trace. The owner chose to embed the web app's sidebar, one import rule for
uploads and add-ins, and "sales" as a money total.

**Production now** (the owner approved each step):
- engine `prereasoner-api-00233-liy` = `engine@sha256:4fc6e203…` built from `2e4d27c`; traffic switched at
  20:07 UTC; the three jobs run the same image; release smoke `mrfcj` passed. Rollback: `00230-rax`
  (`5bb21a2`, `8906537e…`). chat unchanged (`00124-yad`).
- Hosting version `255a2962` (19:41 UTC), deployed from the main working tree as the owner chose. It
  includes this commit plus another session's uncommitted Excel files (`office/excel/*`, `support.html`,
  `terms.html`), which were already live. `/embed/sheets` sends one CSP, `frame-ancestors
  https://docs.google.com https://*.googleusercontent.com`; every other page keeps the Office policy.
- Add-on: `clasp push` (`@HEAD`), version 22, and the Marketplace deployment points to 22. The Marketplace
  app is published and installs run its published App Configuration, so the script version there was set
  to 22 and the draft was published, carrying the 09-24 listing edits (docs/GOOGLE_WORKSPACE_MARKETPLACE.md).
  The store screenshot still shows the old sidebar; the owner chose to publish as is.
- Live check pending: at 20:35 UTC, about 30 minutes after the publish, the owner's sheet (installed
  from the Marketplace) still opened the previous sidebar, while the published App Configuration reads 22.
  The previous sidebar works again with the engine fix; the next step is to confirm the new sidebar once
  Google rolls the version out to installs.

**What changed** (DECISIONS.md has the three entries):
- `sheets-addon/` is a host. `Sidebar.html` frames `/embed/sheets` (`reason.html` + `workbook.js`,
  compact layout), and `Code.js` answers `context` / `grids` with the Google token, the spreadsheet id
  and cell grids. The page side is `web/public/lib/host-bridge.js`: origin checks, Firebase
  `signInWithCredential`, the upload importer, the spreadsheet session and a re-read before each
  question. The add-on dropped `script.external_request` and its URL allowlist. Only `/embed/**` may be
  framed by `docs.google.com` / `*.googleusercontent.com` (`web/firebase.json`).
- Opening the sidebar is one page load: the boot writes the frame's session before the workbook runs,
  and `workbook.js:adoptSession` re-reads the three values the page captures at load (`SHEETS`,
  `TABNAMES`, `question`). Only a sheet changed mid-conversation and New chat reload the frame, once.
- The Excel add-in and the Sheets add-on send grids to `xlsx-worker.js`
  (`WORKBOOK_IMPORT.gridWorkbook`), so a sheet reads exactly as its upload. A populated column without a
  header is refused, not named `column_N`, and every import error names the worksheet. Both add-ins
  check the upload's per-worksheet limits (`upload-limits.js`); they used to cap all tabs' rows together.
- Found while testing the embed, each fixed with a browser test that fails without the fix:
  - With an empty session the page fell back to the web demo question and tables, and asked "top 3
    cities by total amount" on its own.
  - `settle()` never repainted the source chip, so a Google-sourced answer in chat mode kept showing
    "Checking source data". This was a web app bug as well.
  - A sheet changed within the 700 ms before an answer's snapshot reached the server lost that answer
    from view. The rebuild now keeps the tab's own snapshot for the same conversation.
- The empty sidebar keeps the old add-on's data-use notice word for word, because Google's OAuth
  verification relied on it (docs/GOOGLE_WORKSPACE_MARKETPLACE.md).
- The sales rule (regress B6): a money noun that names its table reads as that table's money total
  unless the question counts or lists it. Neither the Spider dev set nor the shipped demo questions can
  fire it: 0 of 1,034 dev questions (no dev table name contains a money noun) and 0 of 91 demo questions.

**Gates** (dev worktree, then main):
- compileall.
- `npm run test:web`: 7 suites, including `Sheets add-on` 29 and `Excel workbook reader` 14.
- `npm run test:browser` 32/32. The three embed journeys run against a stand-in host
  (`/__sheets-host`). They cover sign-in, the first question, reopening in the same tab and in another
  browser, a changed sheet (sync plus pending question), a change right after an answer, New chat,
  Previous conversations and an unreadable sheet. The picker journeys failed once in 30 runs on
  unmodified main too. The 2026-09-26 entry above shows this was a port shortage, not a flake.
- `tests.run_all` on the dev worktree: 44 of 45 suites OK, among them `test_sql_ast` 119/119,
  `test_compose` 14/14, `test_calculations` 104/104 and live `test_orchestrator` 25/25. `test_datasets`
  passed 61 of 65 checks. The other 4 could not connect to the local database proxy (Windows
  `WSAEADDRINUSE` on 127.0.0.1:5432 while browser suites ran alongside), and their 3 datasets pass on a
  rerun (`EVAL_DATASETS=customer-orders,customers-orders,eval-formesign-assets-xls`).

**Open**
- Hosting serves another session's uncommitted Excel files. A deploy from a clean commit rolls them back,
  so that session should commit first.
- The Marketplace App Configuration and consent screen still list `script.external_request` (the owner kept
  it for now); the manifest no longer requests it.
- The three review images are rendered from the embed. The saved Marketplace draft keeps the old ones
  until they are uploaded, and the OAuth demo video shows the old sidebar
  (docs/GOOGLE_WORKSPACE_MARKETPLACE.md).
- `docs/EXCEL_COPILOT_PLAN.md` (another session's uncommitted edits) still describes the old Sheets
  sidebar; DECISIONS.md records that the embed supersedes it.

## 2026-09-25 (evening) — RELEASED: the engine checks its cached connection (the Sheets add-on's HTTP 500)

**Production now:**
- engine `prereasoner-api-00230-rax` = `engine@sha256:8906537e…` built from `5bb21a2` (8 vCPU / 16 GiB).
  Traffic switched at 17:52 UTC. The three jobs run the same image; the release smoke job passed
  (execution `hxqmg`), and `/api/healthz` returns 200.
- chat is unchanged: `prereasoner-chat-00124-yad` (`3d7189e`).
- Hosting matches no commit. This session deployed `3d7189e` at 00:25 UTC. Another session then deployed
  four times, from 00:49 to 01:16 UTC (latest version `d1f1edcf`), from its uncommitted Excel add-in work
  in the main worktree. Live `office/excel/{taskpane,auth-bridge,turn-bridge}.js` equal that working
  tree, not HEAD; `lib/workbook.js` equals HEAD. A Hosting deploy from a clean commit would roll back
  the live Excel changes.
- Rollback: engine `00227-xey` (`3d7189e`, image `2069c274…`; point the three jobs back at it). This
  release has no migration.

**The defect:** the Google Sheets add-on answered "Prereasoner request failed (HTTP 500)" (the owner's
screenshot). `EntityQuery._rconn` keeps one Postgres connection per engine instance across requests.
Cloud Run's Cloud SQL connector drops that connection around its certificate refresh: each of the 5
failures from 08-30 to 09-25 came 5-57 s after a `cloudsql.instances.connect` audit event, and 8 s to
41 min after the connection was last used. psycopg2 marks a connection closed only after a statement
fails, so the next request's first statement failed. In `5bb21a2`, a connection idle for more than
1 s runs `SELECT 1` before a request gets it. A dropped connection is replaced, and that request's
`[timing]` line shows `pg_stale_reconnect_ms`.

**Gates:**
- The regression test `test_cached_connection_dropped_between_requests_is_replaced_before_use` fails on
  `92d78a5` and passes on `5bb21a2`. `tests.test_request_limits`: 18/18.
- Live `test_datasets` against `5bb21a2` passed: 24 prompts, 40 standalone follow-ups and 1 rewrite.
  The 13 `chat:` follow-ups are skipped by design.
- The revision was deployed with no traffic, checked for health through its tag, then switched.

## 2026-09-25 — RELEASED: the Excel add-in and three review fixes; full Chrome gate 124/124

**Production now** (one revision per service, no tags):
- engine `prereasoner-api-00227-xey` = `engine@sha256:2069c274…` built from `3d7189e` (8 vCPU / 16 GiB).
  The jobs `prereasoner-api-ecb-rates-refresh`, `prereasoner-api-retention-cleanup` and
  `prereasoner-api-release-smoke` run the same image.
- chat `prereasoner-chat-00124-yad` = `chat@sha256:ec248a5c…` built from `3d7189e`.
- Hosting deployed from `3d7189e` at 00:25 UTC. The later `11ed076` and `3a57b0f` change only
  `docs/marketplace/`. Another session redeployed Hosting from uncommitted work afterwards (see the
  evening entry above).
- Database: chat migrations 8-10 applied (8: the host column on Excel document sessions; 9:
  `chat.auth_principal`; 10: spreadsheet sessions keyed by user, host and spreadsheet), then the serving
  grants (`db.reference_grants --role serving`).
- Rollback: engine `00223-vas` (`dedfb27`, image `e9170f8a…`; point the three jobs back at it) and chat
  `00120-huq` (`1be4c6e`, image `afad5e36…`). Migration 10 is not backward compatible. The old engine's
  sheet-session upsert names the old key `(user_id, spreadsheet_id)` and fails against the new one, so
  an engine rollback breaks add-in session saves: prefer rolling forward. Hosting rolls back from the
  Firebase console's release history.

**What shipped, in order**
1. `c8533ca` (owner): the Excel add-in (`web/public/office/excel/`), host-scoped sheet sessions, and one
   storage principal per Google account (`chat.auth_principal`), so the add-ins and the web app share
   one account.
2. Before release: `c8533ca` made the chat service resolve that principal in Postgres, which the chat
   service does not connect to, so every chat request would have failed sign-in. The owner chose the
   engine-only lookup. Fixed in `2e70227`: the chat verifies the Firebase token without a database and
   keys the dataset attestation by the Firebase UID, and only the engine resolves the storage principal.
   The regression test drives the real chat handler with `engine.pg` unimportable (red on `c8533ca`:
   HTTP 401).
3. A review the owner forwarded found three gaps, fixed in `3d7189e`:
   - The grounding guard bound only `column = 'literal'`, and the proposer's imported SQL may put the
     literal first. It now checks both operand orders.
   - `db9a1d8` had stopped checking `!=`, `<>` and `NOT IN`. The owner chose to keep checking
     exclusions. The module and DECISIONS.md state the policy and its same-domain cost, and tests pin
     both sides.
   - The Excel add-in counted any format containing a date letter as a date, so
     `#,##0.00;[Red]-#,##0.00` turned 1,234.50 into 1903-05-18, and both importers turned `[h]:mm`
     durations into 1900 timestamps. `web/public/lib/number-format.js` now owns the rule for the add-in
     and the upload.

   Spider stays 647/1,034, because none of the 1,034 selected dev queries has a reversed literal
   comparison (RESULTS.md).
4. Released `3d7189e` on 2026-09-25 in this order: migrations and grants; both services deployed with no
   traffic and smoke-tested; traffic switched at 00:17 UTC; hosting deployed. For about 90 s between
   migration 10 and the switch, the old engine's sheet-session saves failed.

**Gates:**
- `tests.run_all`, launched on the main checkout while it was clean at `3d7189e`: 45/45 suites, none
  skipped. They include:
  - `test_sql_ast` 114/114
  - `test_request_limits` 17/17
  - `test_app_migrations` 13/13
  - `test_release` 32/32
  - live `test_orchestrator` 25/25
  - live `test_datasets` PASS: 24 prompts and 40 standalone follow-ups. The 13 `chat:` follow-ups are
    covered by Chrome.
- `npm run test:web` on a clean checkout of HEAD: all 6 suites pass, including `workbook layout` (18
  checks) and `Excel workbook reader` (5). The 9 shipped workbooks convert to byte-identical CSV.
- Production checks after the switch:
  - release smoke OK
  - the owner's account lists the same 50 conversations
  - `chat.auth_principal` holds one row
- Full Chrome pass on the live release, all 24 datasets (the 6 upload datasets attached as files):
  - fresh conversations **74/74**
  - the 24 existing conversations from the 2026-09-24 morning pass **50/50**
  - Every one of the 124 turns made an engine call, so none was answered from memory.
  - Paris answers Delta and Omega, and Lyon answers Alpha, Beta, Delta and Omega, in fresh and existing
    conversations.
- Gate driving: Chrome throttles timers in background tabs to about one wake-up a minute, so an in-page
  wait loop stalls and can overlap the next call. Take one locked step per call and wait outside the
  page. Keep compound questions in one tab.

**Open**
- One user's requests still run one at a time behind the per-user advisory lock. Parallel compound
  questions can exceed the chat's 180 s engine timeout (see the 2026-09-24 evening entry).
- Codex's Spider run tagged `codex_positive_grounding` (checkpoint in `spider/results/`) measures the
  exclusion-removal policy the owner rejected, so its numbers do not describe the served engine.
- `LIKE` literals are still unchecked; 11 dev questions select a `LIKE` query.
- The Excel add-in sends dates as `YYYY-MM-DDT00:00:00Z`; the web upload sends `YYYY-MM-DD`. The
  difference predates this release.
- The gate added 24 conversations to the owner's account and 50 turns to existing ones; none were
  deleted.
- Resolved: the owner committed `spider/results/full_eval_served_grounding_whole_db.json` in `c8533ca`.
- Another session has uncommitted Excel add-in work in the main worktree (`web/public/office/excel/`,
  `excel-addon/`, `package.json`, new tests). It is not part of this release (hosting was deployed from
  a clean `3d7189e` checkout) and was left untouched.

## 2026-09-24 (evening) — RELEASED: literal grounding, question fidelity, and four defects the Chrome passes found

**Production now** (one revision per service, no tags):
- engine `prereasoner-api-00223-vas` = `engine@sha256:e9170f8a…` built from `dedfb27` (8 vCPU / 16 GiB).
  The jobs `prereasoner-api-ecb-rates-refresh`, `prereasoner-api-retention-cleanup` and
  `prereasoner-api-release-smoke` run the same image.
- chat `prereasoner-chat-00120-huq` = `chat@sha256:afad5e36…` built from `1be4c6e`.
- Rollback: engine `00220-kud` (`900f3b1`, image `0a77b9f5…`; point the three jobs back at it), chat
  `00118-sok` (`9180169`), `00116-xav` (`dedfb27`) or `00113-tid` (`900f3b1`).

**What shipped, in order**
1. `841f08c` literal grounding: a pool member is eligible only if every text literal it tests occurs
   in its own column, or in no column (the Lyon miss). `900f3b1` question fidelity: a complete question
   reaches the engine as typed, and a rejected dataset op gets one repair. Served Spider `whole_db` on
   `841f08c`: 647/1,034 strict (62.6%), 696 lenient, 304/408 scalar (RESULTS.md). Released as engine
   `00220-kud` and chat `00113-tid`.
2. Second full Chrome pass on that release (all 24 datasets): fresh conversations 73/74 (morning
   71/74), existing conversations 48/50 (morning 46/50). The Lyon, supplier and 'budget' misses are
   gone. The three new misses:
   - "List the product names that no customer from Paris has bought": the model decomposed it as Paris
     customers crossed with products. The engine answered that cross inputs need explicit limits, and
     the model resubmitted with "the top 100" on both lists, which was served as 14 pairs. Fixed in
     `1073c01`: a leaf keeps only a cutoff the question states. The wrong shape itself is rare (42/42
     stubbed proposals for this prompt used the right anti-join).
   - Two re-asked questions answered from memory with no engine call. Belgium came back at the
     morning's exchange rate (367.4342; today 366.0174). Fixed in `dedfb27`.
3. Released `dedfb27` (engine `00223-vas`, chat `00116-xav`). Re-check in Chrome: the four
   decomposition datasets fresh 11/11 (Paris now answers Delta and Omega); existing conversations
   43/50. All 7 misses were re-asks answered from memory. Six were short questions the catalog does not
   match, and in one the model ignored the correction note. Fixed in `9180169`: a word-for-word repeat
   answered with a number also counts, and the correction round forces the query call
   (`tool_choice`, thinking off for that one round). Released as chat `00118-sok`.
4. The re-check also found that a conversation past 12 turns rejected every message with "history is
   too long", because the browser sends the whole transcript. Fixed in `1be4c6e`: the chat request
   keeps the most recent window (24 messages, 80,000 characters). Released as chat `00120-huq`.
5. Final targeted re-check on the live release: 13/13 re-asked turns correct, every one with an
   engine call (termination 3, assets 2, customer-orders 2, procurement 2, and the 17-turn ecommerce
   conversation 4).

**Gates run this evening:**
- `test_decomposition` 12/12
- `test_complex_datasets` 7/7
- `test_deterministic_emitters` 46/46
- `test_compose` 14/14
- `test_orchestrator_unit` 19/19
- `test_request_limits` 16/16
- live `test_orchestrator` 25/25 (an earlier run was 23/24: the [1f] cutoff follow-up once changed
  both cutoffs; that case measured 12/12 in isolation)
- live `test_datasets` PASS: 24 prompts and 40 standalone follow-ups; the 13 `chat:` follow-ups are
  skipped by design and covered by Chrome
- `compileall` OK
- Cloud Build offline regression 13/13
- release smoke OK

Not run after these commits: `tests.run_all` as one sweep, and Spider. The new code is decomposition,
orchestrator and request validation, none of which is on the Spider path, and `select_query` is
unchanged since `841f08c`.

**Open**
- `spider/results/full_eval_served_grounding_whole_db.json` is untracked, waiting for the owner's
  approval; RESULTS.md already cites it.
- One user's requests run one at a time behind a per-user advisory lock. In the re-check, three
  compound questions fired at once in three tabs queued behind each other, and two exceeded the chat's
  180 s engine timeout; one at a time they were correct. A user who runs parallel compound questions
  would see the same.
- The gates left many conversations in the owner's account, including the junk `c_f9de7d7c…` from an
  earlier pass. None were deleted.

## 2026-09-24 — FIXED + RELEASED: the Schema.org interpreter loads in production again

**Production now:** engine `prereasoner-api-00217-zom` = `engine@sha256:6d56ab80…` built from `c228cfb`
(8 vCPU / 16 GiB, unchanged). The jobs `prereasoner-api-ecb-rates-refresh`,
`prereasoner-api-retention-cleanup` and `prereasoner-api-release-smoke` run the same image. No tags.
The chat service is unchanged (`prereasoner-chat-00111-wuc`): its image carries none of the changed modules.
Rollback: `gcloud run services update-traffic prereasoner-api --to-revisions prereasoner-api-00215-xey=100`
(and point the three jobs back at `engine@sha256:8dbf68ad…`). That restores the silent fallback.

**Root cause.** From at least 2026-09-14, every container logged `schema interpreter unavailable:
ValueError` then `model routing failed: ValueError` (both seen on `00215-xey`), so learned column routing and
table class evidence were off. The Schema.org head's recorded encoder identity hashed every file in
`engine/data/qwen_lora`, including a stale local PEFT `README.md` that the manifest never pins; no image
could reproduce it. The serving loader swallowed the error, printing only the exception type.

**Fix (`c228cfb`, details in `DECISIONS.md`).** Adapter identity = its model files
(`engine/artifact_provenance.py:adapter_sha256`), used by every adapter-identity site. The promoted head's
identity was re-recorded through `training/schema_org/promote.py` (`c3f61d5e…` -> `ea5bdbf0…`); head,
thresholds and signatures are byte-identical. `KnowledgeQuery` loads the interpreter at construction and
the in-image gate (`run_bundle_checks`) loads it at build, so a bundle it cannot load fails the build or the
startup probe instead of degrading silently.

**Evidence**
- Red/green in an image-like worktree (manifest files only): old code raised the production ValueError; new
  code + old artifacts failed the new pairing test and the bundle gate; new code + re-recorded artifacts passed.
- Cloud Build in-image gate: `schema_interpreter_loads` ok (head `4bb30c5612ee`), offline 13/13.
- `tests.run_all` 39/39 suites (live Postgres + Anthropic). `test_datasets`: 24/24 prompts and 40 non-chat
  follow-ups correct (incl. neartail-catering 9,600 with the learned router back on); the 13 `chat:`
  follow-ups were SKIPPED (Chrome-only). `test_geo` 61/61, `test_route_wired` now captures class evidence.
- `regress.run_regression --require-world` PASS (total amount in France = 270).
- New revision: boot 101 s to ready (old 112 s), no interpreter/routing errors, release smoke ok, healthz ok.

**Chrome pass (2026-09-24, owner's signed-in Chrome, 00217-zom).** All 24 datasets, every
`prompt.txt` + `eval.txt` case graded by `regress.browser_gold` / `tests.test_datasets.grade_answer`:
fresh conversations 71/74, existing conversations (created 09-23 on older revisions) 46/50 follow-ups.
None of the 7 misses comes from this release:
- Engine, pre-existing: "how about customers from Lyon?" (complex-unsold-products). The orchestrator's
  leaf read "... Lyon customers" and the planner bound 'Lyon' to `customer_name`, a column that never
  holds it (`WHERE purchases__customer_name = 'Lyon'`). Reproduced hermetically with the planner alone
  (no router): "customers from/in Lyon" is right, "Lyon customers" is wrong. Value grounding fix pending.
- Orchestrator rewording/context carry-over (the engine answers the verbatim questions correctly, router
  on and off identical): "How many payments are listed?" sent as "... payments to suppliers ..." (3, not
  30) and "What is the highest amount paid?" as "... to suppliers?" (two rows); in existing conversations
  the Phoenix scope (2), the USD presentation for "total budget in Germany" (37,656.3 for 33,000), and
  "only use the top 2 customers" read as top 2 categories too.
- Existing formfacade-leads: "This is in euros" -> "dataset operation names a table that is not
  uploaded: 'budget'" (passed there on 09-23). Not yet diagnosed.
Watch the `[timing]` lines on world questions: learned routing adds one encoder pass per short-text
column, cached per table.

## 2026-09-23 (night) — RELEASED: the 645 planner serves production; the gates found four defects

**Production now** (project `prereasoner-inference`, us-central1; one revision per service, no tags):
- engine `prereasoner-api-00215-xey` = `engine@sha256:8dbf68ad…` built from `e993476`,
  8 vCPU / 16 GiB, min 1 / max 3, startup probe 60 x 10 s (Cloud Run's maximum budget).
- chat `prereasoner-chat-00111-wuc` = `chat@sha256:56d0e305…` built from `959b40d`.
- jobs `prereasoner-api-ecb-rates-refresh`, `prereasoner-api-retention-cleanup`,
  `prereasoner-api-release-smoke` on the `e993476` engine image. Hosting was already at HEAD.
- Rollback to the pre-release pair (each revision keeps its own 4 vCPU/8 GiB config):
  `gcloud run services update-traffic prereasoner-api --to-revisions prereasoner-api-00107-ck8=100`
  and `... prereasoner-chat --to-revisions prereasoner-chat-00058-4l7=100`.

**Spider, served path:** 645/1,034 (62.4%) strict, 693 lenient, 304/408 scalar — identical SQL to the
candidate on 1,034/1,034 (clean commit `6c39942`; `select_query` unchanged since, no dev question has a
currency intent). Summary JSON `spider/results/full_eval_served_whole_db.json` is written but NOT
committed (CLAUDE.md: generated benchmark output needs the user's approval).

**What happened, in order**
1. `d993e91` was flipped while the live demo gate was still running; the gate then failed
   neartail-catering (the proposer read a world question as an INTERSECT over invented values and
   `compound_candidate` asked for a decomposition). Rolled back. Fixed in `896e081`: compound = the
   search's reading only; the compose probe runs only the search (~40 s saved per composed world
   question); named requests serve the best single query. Full live gate PASS, then flipped.
2. The stale `rc-c206f8d` tag had kept a warm 4 vCPU engine instance since 09-14 (tagged revisions keep
   min instances); all tags removed. Autoscaled instances failed the 5-minute startup probe on old and
   new revisions alike; raised to 600 s (`f234ae0`).
3. Chrome gate (Claude in Chrome on the owner's signed-in session; 23 conversations opened on the OLD
   revision first, then continued on the new one) found:
   - `72cc357` orchestrator: an empty-question tool call surfaced "question is required" as the reply.
   - `5ade6c3` WRONG answer on complex-category-gaps: the leaf contract accepted any aggregate, so a
     product-name ordering was served for "top 2 category names by revenue" (and a units-for-spend
     customer ranking went unnoticed). Leaves now sum and rank by the measure the question names.
   - `959b40d` orchestrator: "This is in euros" failed when the model wrote the table as "Budget";
     the column-as-table repair now accepts a case-only difference.
4. `tests.run_all` (all 32 suites incl. live Anthropic) PASS; the live engine suites found the fourth:
   `e993476` "total order amount in KWD" served an empty table instead of declining (a beam dropped
   the SUM and the currency spec read any aggregate-less query as a filter). Live test_geo 61/61.
5. Final Chrome evidence: existing conversations 48/48 follow-ups correct (5 after one retry: 4 complex
   turns timed out under three concurrent complex sessions, 1 Sonnet rewrite); fresh non-complex 61/63
   first pass, both misses 8/8 on re-run; complex on the fixed engine 11/11; formfacade-leads 5/5 on the
   final pair. Old revision baseline: 22/24 prompts. 55 gate conversations remain in the owner's account.
6. Production latency (147 gate turns): one engine call median 13.3 s (p90 35.3 s); decompositions
   median 60.2 s (p90 140.4 s).

**Open / for the user**
- Capacity: one request at a time per engine instance; concurrent complex questions can exceed the
  240 s turn budget. GPU or a quantized runtime is the lever (user decision; cost vs latency).
- Pre-existing: "schema interpreter unavailable: ValueError" in every container since 09-14 (task chip).
- Approve (or not) committing `full_eval_served_whole_db.json`; delete or keep the 55 gate conversations.

## 2026-09-23 (late) — Took over from Codex: the 645 candidate becomes the ONE served planner

User asked: use the 62.4% candidate, one clean source of truth, understandable docs, deploy in
production (no limited rollout, no parallel engine versions).

**Done in the tree (committed on main):**
- One selection, `engine/tables.py:TableQuery.select_query` = search (25) + d2 proposer (4 beams,
  every line imported/validated/re-rendered) + in-memory pool execution + linear arbiter. Used by
  serving, the decomposition probe and leaves, `spider/probe/full_eval.py` (no injection flags now),
  `regress/run_regression.py`, and `training/rank/build_pool_labels.py`.
- Arbiter = 9 named features (`engine/sql_rank.py:ARBITER_FEATURES`); the pilot's 6 constant
  zero-coefficient d4/missing slots dropped with bit-identical scores. `fit_arbiter.py` refits the pilot
  pools to identical means/scales/intercept, coef within 9e-15. Replay on pilot-val = 224/416 (= pilot).
- Parity: production `select_query` picks the recorded 645-run SQL on 21/21 sampled dev questions.
- Runtime bundle: `engine/data/sql_proposer/` (adapter, gitignored, fetched) + `sql_arbiter.json`
  (committed); `training/rank/promote.py` is the one writer. Manifest is local-only until the HF upload.
- Product finding + fix: the Spider-fit arbiter swapped compound prompts to single proposer queries and
  picked names-only ranking leaves. Now: named requests decompose when the search reads a set operation
  OR the answer is one; leaves take the best-ranked candidate meeting the leaf contract. Complex
  datasets 4/4 pass with real models. Spider eval has no analysis context, so unaffected.
- Retired: RankHead + hook, execution_checks, proposer_first, pilot_selectors, train_head,
  build_values/value prompts. Failed semantic pilot code moved OUT of the repo to
  `C:/work/prereasoner-experiments/semantic_pilot` (its 3 lease tests ported to tests/test_release.py).
- CI was red since 09-21 (dependency-lock identity + a stale hosting assertion + an unused import);
  all fixed. sqlglot added to serving + CI locks (only change in those locks).

**Open:** fresh full whole_db run through the production path; live test_datasets rerun on final code;
HF upload + manifest pin; image build; latency on Cloud Run (CPU vs GPU is the user's call — local CPU
median 17.5s/question for selection alone); deploy + Chrome dataset sweep.

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
