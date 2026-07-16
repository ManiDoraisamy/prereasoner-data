# Spider Benchmark — Diagnose Before Fixing (v2)

> **Current planner note.** This document records the original diagnostic study of the slot-filler
> and compose routes. The repository now also contains a typed deterministic AST planner with
> subqueries, aliases, self-joins, set operations, nested aggregation, and bounded AST search. Start
> with [`docs/SQL_AST.md`](../docs/SQL_AST.md) for the current implementation and results; use this
> document for the historical probe methodology and legacy-route baseline.

> **Current accuracy-research tools.** `probe/mine_ast_failures.py` compares recursive
> gold SQL profiles with the complete typed-AST pool and writes
> `results/ast_failure_analysis.json`. `probe/build_ast_proposal_data.py` converts all
> Spider-train gold trees into database-disjoint sketch, schema-link, and literal targets;
> `probe/train_ast_proposer.py` fits deterministic top-k sketch and role-aware schema heads.
> Their audits are `results/ast_proposal_data.json`, `results/ast_proposer.json`, and
> `results/ast_proposer_ablation.json`. The compressed profile beam preserves 39.5% top-1 strict,
> raises top-10 strict oracle from 44.5% to 51.2% and full-pool strict oracle from 45.0% to 53.5%,
> with 22.02 average candidates. The broader 55.6% pool-recall experiment is retained as an
> ablation because it regressed top-1 and averaged 78.71 candidates. A profile-aware deterministic
> ranker preserves top-1 under the safeguard and raises top-10 strict oracle again to 52.1%; raw
> reranking remains unsafe. These tools belong to the
> current AST planner work; the rest of this document preserves the earlier route diagnosis.

> **Audience: Claude Code / whoever runs this next.** This is a **diagnostic** spec, not a fix spec.
> The goal is to localize *why* PreReasoner scores low on Spider before changing anything. This is
> **v2**: the v1 spec was written against stale internals (`relate11.py`, `model11.py`, `runtime20/*`,
> a `/reason` view-DAG) that no longer exist, and against a **guiding hypothesis that the executed
> probes below partly refute**. v2 is rewritten against the *actual* engine (`engine/*`) and carries
> the results of a probe suite that was **built and run** — see [`results/RESULTS.md`](results/RESULTS.md).
>
> **Code wins where this doc disagrees.** Every architectural claim here was checked against
> `engine/compose.py`, `engine/tables.py`, `engine/world_query.py`, `engine/world_compose.py`,
> `engine/router.py`, `engine/primitive_head.py`, `engine/encoder_overlay.py`, `engine/taxonomy.py`.

---

## 0. What PreReasoner actually is (the mental model the v1 spec got wrong)

PreReasoner is **not** a general NL→SQL model. It is an *interpretable spreadsheet-QA* engine. For a
question over uploaded tables it:

1. **Types columns** with a trained encoder (Qwen2.5-0.5B + LoRA + a relational readout) to a **42-leaf
   Wikidata taxonomy** (`engine/taxonomy.py`, `data/taxonomy.csv`). Of those 42 leaves, **only two are
   backed by world tables: `city` and `country`.**
2. **Resolves values** to Wikidata entities (bge) and can **join to a world-knowledge Postgres** (which
   country a city is in, population, …). This is the product's differentiator.
3. **Answers as a stack of SQL views** it can show the user.

There are **two SQL generators** and a router between them (`engine/world_compose.py :: _composed`):

| Path | File | Emits | Executes on |
|---|---|---|---|
| **Compose** (view-stacking) | `engine/compose.py` + `engine/primitives.py` | one flattened base (single table / FK star-join / world join) → `filter · time_filter · group_agg · having · topn · sort · yoy · running · share · divide`, agg ∈ COUNT/SUM/AVG/MIN/MAX | in-memory **SQLite** |
| **Slot-filler** (delegate) | `engine/tables.py :: TableQuery` | `SELECT`/projection, value-matched `WHERE`, `>`/`<`, dates, `GROUP BY`, `ORDER BY`, `LIMIT`, superlative argmax, **one** FK `JOIN`, agg | in-memory **SQLite** (offline) / **Postgres** (live) |

Routing: a question whose **learned primitive head** fires a *depth* primitive
(`EXCL/RATIO/TOPN/SHARE/TIME/HAVING/SORT/DIVIDE/RUNNING`) → **Compose**; everything else → the delegate
(→ slot-filler for a self-contained table). The live full stack (`WorldReasoner → ComposedWorldQuery →
WorldQuery`) additionally does world resolution + a **clarify gate**, and executes on **Postgres**.

**Neither legacy generator has a nested-subquery primitive, a set-operation
(`INTERSECT/UNION/EXCEPT`) primitive, or a self-join.** Those were hard ceilings for the two routes
measured by this diagnostic. They are not ceilings of the newer AST planner.

---

## 1. Reproduction is a GATE, not step 1

The v1 spec said "reproduce the 12%." Do that **only if** the same stack can run; otherwise the
discrepancy is itself finding #0. In *this* environment the full live number **cannot** be reproduced:
the live path is **Postgres-gated** (`world_pg_password()` raises without a seeded world DB; the seed is
a 15–45 min Wikidata sync), and `sentence_transformers`/`pgvector` are absent. So we do **not** fabricate
a "12%." Instead we run the parts that *are* faithful and Postgres-free:

- **Both SQL generators run on SQLite** (compose via `ComposeEngine.run(..., world=None)`; slot-filler via
  `TableQuery.plan → assemble → execute`). For self-contained Spider DBs `world` **is** `None`, so the
  **assembled SQL is identical to what the live system would emit** — only the *executor* differs
  (SQLite vs Postgres), which does not change denotation. This yields a **faithful offline reproduction**
  of the system's SQL (Probe D+), missing only: world resolution (irrelevant to Spider) and the clarify
  gate (which converts some wrong answers into refusals — still scored wrong on Spider, so its absence
  makes our number an **upper bound**, never a flattering lower one).
- The **column-typing router** runs standalone on CPU (Probe C).
- The **static** envelope + coverage analyses need nothing but the data (Probes A, B).

If you later stand up the seeded world Postgres, re-run the *live* stack and compare against Probe D+.

---

## 2. Revised guiding hypothesis (and why v1's was a false binary)

v1 asserted: *"errors are a schema-linking deficit — NOT SQL-generation, NOT architecture; the leak is
out-of-taxonomy column typing."* The probes **partly refute this**:

- "Linking vs architecture" is a **false binary**. A large, *measured* share of Spider is **structurally
  outside both generators** (nesting, set-ops, self-joins) — that *is* architecture. And within the
  reachable set, the dominant leaks are **projection selection, operator choice, join-flattening, and
  mis-routing** — compositional/structural, not "typing an out-of-taxonomy column."
- **Column typing / the 42-leaf taxonomy is almost irrelevant to Spider** (Probe B): Spider DBs are
  self-contained, so **zero** gold queries need world knowledge, and the world join (city/country) is
  dead weight — at best a no-op, at worst an over-reach.

So the revised hypothesis to test is: **the loss is dominated by (a) a structural-envelope ceiling, and
(b) compositional binding — projection, operator, join, routing — on the reachable remainder; column
typing is a minor factor.** The probes confirm this (see RESULTS).

---

## 3. The probe suite (what was built and run)

All under `spider/probe/`. Data (`dev.json`, `tables.json`, the 20 dev SQLite DBs) is fetched into
`spider/data/` (gitignored).

- **Probe A — architectural envelope** (`static_probe.py`, needs only gold SQL). Uses the **official
  `eval_hardness`** (vendored verbatim in `hardness.py`) so difficulty labels match Spider's evaluator.
  Classifies each gold query as **hard-blocked** (nesting / set-op / self-join — no planner can emit) vs
  **reachable**, and histograms the reachable set's shape (join, group-by, order+limit, multi-select,
  multi-where). The reachable % is the **optimistic structural ceiling**.
- **Probe B — taxonomy / world-knowledge coverage** (`static_probe.py`). How many gold queries require
  world knowledge (structurally **0** for self-contained Spider); how many columns could even type to
  `city`/`country`. Quantifies that the differentiator is inert on Spider.
- **Probe C — typing-router behaviour** (`typing_probe.py`, model on CPU). Runs `engine.router.Router`
  over representative Spider columns to test the v1 Phase-4 worry: does the router **abstain** on
  out-of-taxonomy columns, or **silently mis-fire** (false resolution)?
- **Probe D+ — full offline system** (`full_eval.py`, model on CPU; **the headline**). Reproduces the
  live routing (compose vs slot-filler), executes the emitted SQL on SQLite, and compares denotation to
  the real gold rows. Reports the three-outcome split + a stage-attributed error histogram.
  (`compose_eval.py` is the compose-path-only variant used to isolate that generator.)

Two input configs are reported:
- **`gold_tables`** — feed only the tables the gold SQL references (oracle table selection). Isolates
  *reasoning/binding* quality; an **upper bound**.
- **`whole_db`** — feed all of the DB's tables (product-realistic, gold-blind). Measures the extra cost
  of table selection + the FK-join-flattening behaviour.

---

## 4. The three-outcome decomposition (keep this — it is the honest frame)

v1's best idea, kept. Every example lands in exactly one bucket:

- **answered-correct** — SQL emitted, executes to the gold denotation.
- **answered-wrong** — SQL emitted, wrong denotation (or execution error).
- **refused** — the clarify gate declined (live only; **not reproducible here** — noted, not faked).

Report **answered-accuracy = correct/(correct+wrong)**, **coverage = (correct+wrong)/total**, and
**refusal-rate** separately. A correct refusal is *not* a pipeline error but *is* scored wrong by Spider;
do not collapse it into the headline.

### Matcher transparency (the semantic-equivalence trap, made explicit)

A grammar-constrained engine emits **canonicalised** SQL whose *projection* rarely matches gold's column
list, so naive exact/row-set match under-counts. We therefore report a **bracket**, never one number:

- **scalar-gold accuracy** — on the clean subset where gold returns a single value (COUNT/SUM/AVG/…),
  denotation match is **unambiguous**. This is the most trustworthy number.
- **lenient containment** — every gold cell-value appears somewhere in the prediction. A **generous upper
  bound** (an over-broad answer that contains the gold values is credited).
- **strict row-set equality** — order-insensitive full-row multiset match. A **harsh lower bound**
  (projection/column-order differences fail it).

True accuracy sits between strict and lenient; the scalar subset pins it where it is clean. **Spot-check a
sample of answered-wrong by hand** for genuine semantic-equivalence before believing any bar.

---

## 5. Stage-attribution taxonomy (revised to the REAL failure modes)

v1's six categories assumed the leak was table/column linking. The traces show the actual attributable
stages (attribute each answered-wrong to the **first** divergence; note that single-stage attribution
biases toward the earliest stage, so also record contributing stages):

1. **Structural-impossible** — gold needs nesting / set-op / self-join. No fix short of new primitives.
2. **Join-flatten error** — the FK star-join raises `ambiguous column name` on tables that share a column
   name (pervasive in Spider). An *engineering bug*, not a linking gap.
3. **Mis-routing** — the depth-primitive gate over-fires (e.g. `SORT`/`TOPN` on "ordered by", "most"),
   sending a projection/filter query to the compose path, which cannot project or filter.
4. **Projection** — wrong / incomplete `SELECT` columns; the compose path cannot emit a plain multi-column
   projection at all (it always aggregates).
5. **Operator** — wrong aggregate (COUNT↔SUM/AVG), or missing/extra aggregation; multi-aggregate collapses
   to one.
6. **Filter/value** — wrong or missing `WHERE`. (Measured **small** in the slot-filler — it binds
   value-matched equality/`>`/`<` reasonably; large only where mis-routing sent it to the filter-less
   compose path.)

Cross-tab the histogram with difficulty. Column typing / taxonomy is **not** on this list because Probe B
shows it is inert on Spider — verify that claim, don't assume it.

---

## 6. Motivated-reasoning guard

The three-outcome frame can flatter: every wrong answer reclassified as "correct refusal" inflates the
honest number. Guards: (a) refusals are **not reproducible here**, so we make **no** refusal credit; (b)
the headline is the **scalar-gold** number (unambiguous) plus the strict↔lenient bracket, not a
cherry-picked bound; (c) `whole_db` is reported next to `gold_tables` so oracle-table-selection advantage
is visible; (d) hand spot-checks gate the histogram.

---

## 7. Done when

- Difficulty distribution + the **structural envelope** (hard-blocked vs reachable) are computed with the
  official `eval_hardness`. ✔ (Probe A)
- World-knowledge / taxonomy coverage is quantified. ✔ (Probe B)
- The full offline system is **run end-to-end** on all 1034 dev examples, bucketed, with the
  strict/lenient/scalar bracket and a **stage-attributed error histogram** cross-tabbed by difficulty. ✔
  (Probe D+ — see RESULTS)
- The typing router's abstain-vs-mis-fire behaviour is measured. ✔ (Probe C)
- A written **root-cause synthesis** names the biggest leak by stage, states the reproduction gate, and
  attributes each proposed fix to a stage — **nothing tuned or changed.** ✔ (RESULTS §Synthesis)

## 8. Explicitly out of scope (do NOT do yet)

- No encoder swap, MPNN, pooling retune, or "more anchoring." This spec finds *where* the leak is; the
  probes say it is mostly **compositional binding + structural envelope + an engineering join bug +
  routing**, none of which the encoder architecture fixes.
- Do not optimise for the raw Spider number. The deliverable is the **decomposition**.

---

## How to reproduce

```bash
# from repo root; deps already present: torch(cpu), transformers, peft. weights in engine/data/.
cd spider/probe
python fetch_data.py                       # dev.json, tables.json, 20 dev SQLite DBs -> ../data
python static_probe.py                     # Probe A + B
PYTHONPATH=../.. python full_eval.py --dbs ../data/dbs --config gold_tables --tag full   # Probe D+
PYTHONPATH=../.. python typing_probe.py --dbs ../data/dbs                                # Probe C
```
