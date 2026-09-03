# Spider Benchmark — Diagnose and Measure

> **Current planner note.** The own-data SQL layer is a **single deterministic typed-AST planner**
> (`engine/tables.py:search_ast` → `engine/sql_search.py`) with subqueries, aliases, self-joins, set
> operations, nested aggregation, and bounded AST search. Ranking is hand-written and deterministic.
> There is no separate slot-filler, `--planner` flag, or learned SQL ranker. Start with
> [`docs/SQL_AST.md`](../docs/SQL_AST.md) for the implementation contract; this document explains the
> benchmark and its failure taxonomy.

> **Current whole-database result.** The serving-faithful config (`--selection serving_top1
> --max-candidates 25`, byte-for-byte `engine/tables.py:_serve_ast`) scores, over all 1,034 Spider dev
> examples with denotation evaluation, **34.7% strict / 43.8% lenient / 54.9% scalar-gold**
> (359/1,034, 453/1,034, 224/408). This is the current gold-blind comparison number. The oracle
> `gold_tables` ablation must be rerun whenever the planner changes; its previous 41.0% strict result
> is historical, not a current release claim.
>
> The accuracy is entirely the deterministic planner's. Earlier profile-expansion / trained-proposer
> "pool recall" experiments have been removed from the tree and are not reproducible from HEAD; see the
> historical note in [`docs/SQL_AST.md`](../docs/SQL_AST.md).

> **Audience: contributors.** This is both the reproducible benchmark contract and a diagnostic guide.
> The goal is to localize *why* PreReasoner scores low on Spider before changing anything. This is
> **v2**: the v1 spec was written against stale internals (`relate11.py`, `model11.py`, `runtime20/*`,
> a `/reason` view-DAG) that no longer exist, and against a **guiding hypothesis that the executed
> probes below partly refute**. v2 is rewritten against the *actual* engine (`engine/*`) and carries
> the results of a probe suite that was **built and run** — see [`results/RESULTS.md`](results/RESULTS.md).
>
> **Code wins where this doc disagrees.** Every architectural claim here was checked against
> `engine/compose.py`, `engine/tables.py`, `engine/knowledge_query.py`, `engine/knowledge_compose.py`,
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

There are **two SQL routes** and a router between them (`engine/knowledge_compose.py :: _composed`):

| Path | File | Emits | Executes on |
|---|---|---|---|
| **Compose** (view-stacking) | `engine/compose.py` + `engine/primitives.py` | one flattened base (single table / FK star-join / world join) → `filter · time_filter · group_agg · having · topn · sort · yoy · running · share · divide`, agg ∈ COUNT/SUM/AVG/MIN/MAX | in-memory **SQLite** |
| **Typed-AST planner** (own-data) | `engine/tables.py :: TableQuery.search_ast` → `engine/sql_search.py` | a bounded search over valid SQL ASTs: projections + `DISTINCT`, `WHERE`, aggregates, `GROUP BY`/`HAVING`, `ORDER BY`/`LIMIT`, multi-hop and self-joins, scalar subqueries, `IN`/`EXISTS`, derived tables, and `UNION`/`INTERSECT`/`EXCEPT` — ranked by hand-written deterministic features | in-memory **SQLite** (offline) / **Postgres** (live) |

Routing: a question whose **learned primitive head** fires a *depth* primitive
(`EXCL/RATIO/TOPN/SHARE/TIME/HAVING/SORT/DIVIDE/RUNNING`) → **Compose**; everything else → the
deterministic typed-AST planner for a self-contained table. The live full stack (`KnowledgeReasoner →
ComposedKnowledgeQuery → KnowledgeQuery`) additionally does world resolution + a **clarify gate**, and
executes on **Postgres**.

**Note on the original diagnostic.** The route this diagnostic measured through `engine/tables.py` was an
earlier template/slot-filler delegate with **no nested-subquery, set-operation
(`INTERSECT/UNION/EXCEPT`), or self-join** support — those were hard structural ceilings for that route.
That delegate has since been **replaced by the deterministic typed-AST planner**, which does cover
subqueries, set operations, and self-joins; the ceilings below are the earlier route's, not the current
planner's.

---

## 1. Reproduction is a GATE, not step 1

The v1 spec said "reproduce the 12%." Do that **only if** the same stack can run; otherwise the
discrepancy is itself finding #0. In *this* environment the full live number **cannot** be reproduced:
the live path is **Postgres-gated** (`kb_pg_password()` raises without a seeded world DB; the seed is
a 15–45 min Wikidata sync), and `sentence_transformers`/`pgvector` are absent. So we do **not** fabricate
a "12%." Instead we run the parts that *are* faithful and Postgres-free:

- **Both SQL routes ran on SQLite** (compose via `ComposeEngine.run(..., world=None)`; the delegate — at
  the time of this diagnostic, the earlier slot-filler — via its `plan → assemble → execute` path; the
  current tree runs the deterministic typed-AST planner here instead). For self-contained Spider DBs `world` **is** `None`, so the
  **assembled SQL is identical to what the live system would emit** — only the *executor* differs
  (SQLite vs Postgres), which does not change denotation. This yields a **faithful offline reproduction**
  of the system's SQL (Probe D+), missing only: world resolution (irrelevant to Spider) and the clarify
  gate (which converts some wrong answers into refusals — still scored wrong on Spider, so its absence
  makes our number an **upper bound**, never a flattering lower one).
- The **column-typing router** runs standalone on CPU (Probe C).
- The **static** envelope + coverage analyses need nothing but the data (Probes A, B).

For a clean release measurement, run from a clean commit and retain the generated JSON manifest. The
manifest records the source commit, dirty-worktree flag, code hashes, model hashes, and evaluator settings.

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
  live own-data path (the compose route and the deterministic typed-AST planner), executes the emitted
  SQL on SQLite, and compares denotation to the real gold rows. Reports the three-outcome split + a
  stage-attributed error histogram.

Two input configs are reported:
- **`whole_db`** — feed all of the DB's tables (product-realistic, gold-blind). This is **standard
  Spider** — it pays the cost of table selection + FK-join flattening, and is the number to compare
  against other Spider systems.
- **`gold_tables`** — feed only the tables the gold SQL references (oracle table selection). Isolates
  *reasoning/binding* quality; an **upper bound** relative to standard Spider, and the closer analogue to
  the product (where the user uploads exactly the relevant sheets).

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
python -m spider.probe.fetch_data          # dev.json, tables.json, 20 dev SQLite DBs -> spider/data
python -m spider.probe.static_probe        # Probe A + B

# Probe D+ — the serving-faithful deterministic typed-AST planner (engine/tables.py:_serve_ast).
# Standard Spider (the headline comparison number):
python -m spider.probe.full_eval --dbs spider/data/dbs --config whole_db --selection serving_top1 --max-candidates 25
# Oracle table selection (the product-analogue upper bound): same command with --config gold_tables:
python -m spider.probe.full_eval --dbs spider/data/dbs --config gold_tables --selection serving_top1 --max-candidates 25

python -m spider.probe.typing_probe --dbs spider/data/dbs   # Probe C
```
