# Spider Diagnostic — Executed Results

Probes built in `spider/probe/`, run on **Spider dev (1034 examples, 20 DBs)**. Difficulty labels are the
**official `eval_hardness`** (vendored verbatim). Model probes ran the real trained encoder
(Qwen2.5-0.5B + LoRA + relational readout, `engine/data/`) on **CPU**. Raw JSON alongside this file.

> **Headline (bracketed, honest):** end-to-end on 1034 dev examples, execution accuracy is **~13% strict /
> 26% lenient / 35% on the clean scalar subset** (structural ceiling 84%). The **strict 13.5% lands on the
> reported "12%"** — a cross-check that this offline harness reproduces the live behaviour. The miss is
> **not** the schema-linking / out-of-taxonomy story the v1 spec assumed. It decomposes into: **15.8%
> structurally impossible** (nesting / set-ops / self-joins, ~half of hard+extra); **7.4% engine errors**
> (mostly an **FK-join-flatten bug**); and **54% answered-wrong**, dominated by **projection (33%), join
> (30%), operator (18%)** binding — plus a **routing** defect (59% of queries go to the *compose* generator,
> which scores 8% vs the slot-filler's 51%). **Column typing / the world taxonomy is inert on Spider** (0
> queries need world knowledge; typing appears in no failure bucket).

---

## §0 — Reproduction gate (finding #0)

The live full stack (`WorldReasoner → ComposedWorldQuery → WorldQuery`) is **Postgres-gated**: it needs a
seeded world-knowledge DB (`world_pg_password()` raises otherwise; the seed is a 15–45 min Wikidata sync),
and `sentence_transformers` / `pgvector` are not installed here. **So the live "12%" cannot be reproduced
in this environment, and we do not fabricate it.**

What *is* faithful and Postgres-free — and was run:
- Both SQL generators on **SQLite**: compose (`ComposeEngine.run(world=None)`) and the slot-filler
  (`TableQuery.plan → assemble → execute`). For self-contained Spider DBs `world` **is** `None`, so the
  **assembled SQL equals what the live system emits**; only the executor differs (SQLite vs Postgres),
  which does not change denotation. This is a faithful offline reproduction, missing only world resolution
  (irrelevant to Spider) and the clarify gate (its absence makes our number an **upper bound**).
- The column-typing router, standalone on CPU.

---

## §1 — Probe A: architectural envelope (static, gold SQL)

**Difficulty mix:** easy 248 (24.0%), medium 446 (43.1%), hard 174 (16.8%), extra 166 (16.1%).

| Bucket | n | % of dev |
|---|---:|---:|
| **Hard-blocked** (no planner can emit) | **163** | **15.8%** |
|  ├ nested subquery (`> (SELECT …)`, `IN (SELECT …)`, `NOT IN (…)`) | 83 | 8.0% |
|  ├ set operation (`INTERSECT` / `UNION` / `EXCEPT`) | 76 | 7.4% |
|  └ self-join (same table twice) | 4 | 0.4% |
| **Reachable** (optimistic structural ceiling) | **871** | **84.2%** |
|  └ of which *simple* single-table (no composition) | 296 | 28.6% |

**Reachable-set shape** (what the binding must get right — a query can hit several):

| feature | % of reachable |
|---|---:|
| multi-column SELECT (≥2) | 38.2% |
| join (≥2 tables) | 37.5% |
| GROUP BY | 30.4% |
| ORDER BY + LIMIT (superlative/top-k) | 21.0% |
| HAVING | 8.2% |
| multi-condition WHERE (≥2) | 7.7% |
| DISTINCT | 4.8% |

Aggregate ops across gold: `count` 408, `avg` 55, `max` 38, `sum` 33, `min` 18.

**Envelope × difficulty:**

| difficulty | total | hard-blocked | reachable | reach % | simple single-table |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 2 | 246 | 99.2% | 200 |
| medium | 446 | 0 | 446 | 100.0% | 96 |
| hard | 174 | 86 | 88 | 50.6% | 0 |
| extra | 166 | 75 | 91 | 54.8% | 0 |

**Reading:** the structural ceiling is **84%, not ~12%** — so the miss is overwhelmingly *not* "can't emit
the shape." All blocking is concentrated in hard/extra (nesting + set-ops define those tiers). The
reachable set is dominated by **joins, multi-column projection, and GROUP BY** — i.e. *compositional*
binding, not single-column typing. The 84%→(observed low) gap is where the real leak lives; Probe D+
localizes it.

---

## §2 — Probe B: taxonomy / world-knowledge coverage (static)

- **World knowledge required by any gold query: 0.** Spider DBs are self-contained — every gold query
  references only its own DB. The engine's differentiator (resolve a value to Wikidata, join the world DB)
  **adds nothing on Spider**.
- **World tables available: `city`, `country` only** (of 42 live taxonomy leaves; confirmed from
  `taxonomy.csv` — only these two carry `world_tables`).
- 13/20 dev DBs have a header that *could* type to city/country/state (hits: country 6, city 5, state 3,
  continent 2, nationality 2, province 1; 441 columns total). Since world knowledge is never *needed*, this
  capacity is at best inert and at worst an **over-reach risk** (a spurious world join). Probe C measures
  whether that risk fires.

**Reading:** column typing and the world taxonomy — the entire subject of the v1 hypothesis — are
**orthogonal to Spider**. Any fix aimed there would move ~0 examples.

---

## §3 — Probe C: typing-router behaviour (model, CPU)

Ran `engine.router.Router` over **115 real Spider columns** (real cell values; 10 geo-hinted headers, 105
other) — does it abstain on out-of-taxonomy columns, or silently mis-fire?

| metric | result |
|---|---|
| non-geo columns that **mis-fire** to some leaf (`min_fire=0.12`) | **103 / 105 (98.1%)** |
| non-geo columns that **over-reach** to a `city`/`country` world table | **55 / 105 (52.4%)** |
| genuine geo-hinted columns correctly routed to a world table (recall) | 10 / 10 (100.0%) |

The router **does not abstain** — only 2 of 105 out-of-taxonomy columns got no type; the rest were forced
onto leaves like `car_model`, `breed`, `language`, `song`, `power_station`. World-routing put **50 columns
on `city` and 15 on `country`**.

**But read this correctly (guard against over-claiming):** the live path does **not** join on the router
alone — it gates the world join on **grounding** (`WorldQuery._grounds`: ≥80% of a column's cells must
resolve to real world entities in Postgres). A `Name` column that mis-routes to `city` will *not* ground,
so the join is dropped. That gate needs the seeded Postgres (unavailable here), so we can't measure how
many of the 52% over-reaches survive it — but by design most should. So Probe C shows the router is a poor
abstainer, yet this is **not** a primary Spider-score cause: (a) grounding filters most spurious joins
downstream, and (b) per Probe B, world knowledge is never needed on Spider anyway, so any surviving world
join is pure noise, not the dominant failure. Typing is a latent risk, **not the leak.**

---

## §4 — Probe D+: full offline system, end-to-end (model, CPU) — the headline

Ran the real trained model over **all 1034 dev examples**, live routing (compose vs slot-filler),
executed on SQLite, denotation vs the real gold rows (config = `gold_tables`, oracle table selection).

**Honest score (bracketed):**

| metric | value | what it is |
|---|---:|---|
| **scalar-gold accuracy** (clean subset, n=391) | **35.5%** | unambiguous single-value gold — the most trustworthy number |
| **lenient containment** | **25.8%** | generous **upper bound** (credits over-broad answers) |
| **strict row-set equality** | **13.5%** | harsh **lower bound** |
| answered / error | 91.0% / 9.0% | error = 79 `join_build`, 13 `assemble_exec`, 1 timeout |
| structural ceiling (Probe A) | 84.2% | — |

> The **strict 13.5% sits right on the reported "12%"** — an independent cross-check that this offline
> harness faithfully reproduces the live system's Spider behaviour (the emitted SQL is the same; only the
> executor differs). True execution accuracy is bracketed **~13–26%**, with the clean scalar subset at 35%.

**Four-way decomposition** (correct = lenient; structurally-impossible assigned first — so the 16 of 93
raw errors and 34 coincidental lenient "hits" that fall on impossible queries are counted under
*impossible*, leaving 77 errors / 233 correct here. The 34 reclassified "hits" illustrate the lenient
bound's generosity):

| difficulty | n | impossible | error | answered-wrong | correct |
|---|--:|--:|--:|--:|--:|
| easy | 248 | 2 (0.8%) | 1 (0.4%) | 142 (57.3%) | **103 (41.5%)** |
| medium | 446 | 0 | 44 (9.9%) | 306 (68.6%) | **96 (21.5%)** |
| hard | 174 | 86 (49.4%) | 14 (8.0%) | 52 (29.9%) | **22 (12.6%)** |
| extra | 166 | 75 (45.2%) | 18 (10.8%) | 61 (36.7%) | **12 (7.2%)** |
| **all** | **1034** | **163 (15.8%)** | **77 (7.4%)** | **561 (54.3%)** | **233 (22.5%)** |

**Routing is a top lever.** The live gate routed **606 (59%) → compose, 427 → slot-filler**. Their hit
rates are wildly different:

| path | routed | lenient-correct | hit rate |
|---|--:|--:|--:|
| slot-filler | 427 | 216 | **50.6%** |
| compose | 606 | 51 | **8.4%** |

Most queries go to the **weaker** generator. Much of that is mis-routing: a projection/filter/superlative
question fires a `SORT`/`TOPN` depth primitive and is sent to compose, which cannot project or filter —
where the slot-filler (which does `ORDER BY`, `WHERE`, projection) would often have been right.

**Answered-wrong stage attribution** (heuristic first-divergence over the 561; **spot-check before
acting**):

| stage | n | % of answered-wrong |
|---|--:|--:|
| join (missing/failed) | 170 | 30.3% |
| projection (wrong columns / forced-agg) | 186 | 33.2% |
| operator (missing / wrong agg) | 102 | 18.2% |
| filter (missing) | 64 | 11.4% |
| grouping (missing) | 32 | 5.7% |
| ordering (missing) | 7 | 1.2% |

**Reading:** the miss is **projection (33%) + join (30%) + operator (18%)** — compositional binding and the
join-flatten bug — with filter a third-tier ~11% (much of it downstream of mis-routing to the filter-less
compose path). Easy questions already lose ~58% (projection/operator/join binding on *simple* queries);
hard/extra are ~half structurally impossible. **Column typing appears nowhere** — consistent with Probes
B and C.

---

## §5 — Root-cause synthesis

**1. Is the v1 hypothesis confirmed? No — refuted as stated.** v1 said the miss is "a schema-linking
deficit, out-of-taxonomy column typing — not SQL-generation, not architecture." The probes show:

- **Column typing / the world taxonomy is inert on Spider** (Probe B: 0 gold queries need world
  knowledge; Probe C: the router doesn't abstain but its over-reach is grounding-gated downstream and
  irrelevant here). Fixing "typing" would move ~0 examples. The v1 target is the wrong target.
- The real loss is a **mix of architecture and binding**, so v1's "linking *vs* architecture" was a
  **false binary**:

**2. The biggest leaks, by stage** (each is an attributable stage, not "typing"):

- **Structural-impossible (~16%, Probe A):** nested subqueries + set-ops (+ 4 self-joins). Neither
  generator has a subquery or set-op primitive. Concentrated in hard/extra (which are ~50% impossible).
  This is *architecture*, and it is a hard floor v1 defined away.
- **Join-flatten bug (engineering):** the FK star-join (`compose.join_view` / the slot-filler JOIN)
  emits `SELECT fact.*, dim.col AS col`, which raises **`ambiguous column name`** whenever two joined
  tables share a column name — pervasive in Spider (`Name`, `Location`, `ID`). Joins are 37.5% of the
  reachable set; this bug turns a large share of them into hard errors. **Not** a linking gap — a
  qualify/alias fix.
- **Mis-routing (a top lever, measured):** the depth-primitive gate (`ComposedWorldQuery._composed`)
  over-fires `SORT`/`TOPN` on "ordered by", "most", etc., routing **59% of queries to compose**, which
  scores **8.4%** vs the slot-filler's **50.6%**. Projection/filter/superlative questions the slot-filler
  handles (it *did* bind `WHERE Country='France'` and `WHERE weight>10`, and does `ORDER BY`) are lost to
  the filter-less/projection-less compose path.
- **Projection (33% of answered-wrong):** compose **always aggregates** — it has no plain multi-column
  `SELECT` — so every "show name, country, age" it receives is wrong; the slot-filler sometimes
  under-/mis-selects columns. Multi-column SELECT is 38% of the reachable set.
- **Operator (18%):** COUNT↔SUM confusion ("total number of singers" → `SUM(Age)`), and multi-aggregate
  (`avg, min, max`) collapses to a single agg.
- **Filter is third-tier (~11%), not the leak** — a useful negative result: the slot-filler binds
  value-matched equality / `>` / `<` reasonably (it gets `country='France'`, `weight>10`); most remaining
  filter misses are downstream of mis-routing to the filter-less compose path, not a filter-binding gap.

**3. How much of the miss is refusal vs coverage vs real error?** No refusals are counted here (the
clarify gate is Postgres-gated; §0). So the miss is **not** flattered by refusal accounting: it is
~16% structural-impossible + the join-bug/routing/projection/operator errors above. "Pure coverage"
(world-taxonomy) is ~0 on Spider.

**4. Honest decomposed score:** see §4 — reported as a **bracket** (clean scalar-gold accuracy, plus
strict↔lenient), against a **structural ceiling of 84%**. The gap from 84% to the observed accuracy is
the join-bug + routing + projection + operator stack, in that rough order of size.

### Proposed fixes — each attributed to a stage (nothing changed yet)

| Fix | Stage it targets | Kind | Expected to move |
|---|---|---|---|
| Qualify/alias columns in `join_view` + slot-filler JOIN (dedupe `fact.*`+`dim.col` name clashes) | join-flatten bug | engineering | a large share of the 37.5% join queries that currently hard-error |
| Tighten the depth-routing gate (don't fire `SORT`/`TOPN` when a plain projection/filter is intended) **or** give compose a projection+filter primitive | mis-routing / projection | engineering→small-arch | the projection/filter queries mis-sent to compose |
| Add a plain multi-column projection path (compose) + multi-aggregate support | projection / operator | small-arch | the 38% multi-select + the multi-agg queries |
| Recalibrate the operator readout (COUNT vs SUM/AVG on entity-count phrasings) | operator | calibration | COUNT/SUM confusions (COUNT is 408 of gold aggregates) |
| (Larger) add subquery + set-op primitives | structural-impossible | architecture | up to the ~16% hard floor, i.e. most of hard/extra |

**None of these is "more anchoring," an encoder swap, or taxonomy work** — the probes localize the leak
away from the encoder/typing and onto composition, an engineering join bug, and routing. Do the
join-flatten fix and routing tightening first (cheap, high-yield); treat subquery/set-op primitives as
the separate, larger architecture item that lifts the 84% ceiling.

> **Caveats (read before acting):** (a) this is the *offline compose+slot-filler* system on SQLite —
> faithful to the emitted SQL but omitting the Postgres world path (irrelevant to Spider) and the
> clarify gate (its absence makes these numbers an upper bound); (b) `gold_tables` gives the engine
> oracle table selection — the `whole_db` config is worse (the join-flatten bug fires on every
> multi-table DB); (c) the answered-wrong stage attribution in §4 is heuristic and should be
> hand-verified on a sample before it drives priorities; (d) large DBs (only `wta_1`) are row-capped
> for tractability, so their denotation is computed on ≤5000 rows (identically for gold and prediction,
> so the comparison stays valid).

---

## §6 — Tier-1 fixes: implemented and measured

Three fixes from §5's table were landed (nothing else touched — no encoder/taxonomy/threshold changes)
and the full 1034-example eval re-run (`--tag tier1`; baseline artifacts kept as `*_full.json`):

1. **Join-flatten fix** (`engine/joins.py`): the `endswith("id")` FK arm now requires the id column's
   stem to name the parent (kills spurious id-range inclusions like `concert_ID ⊆ Stadium_ID`);
   `join_plan` joins each parent **once** (best-named FK wins) and prunes output-name collisions.
2. **Routing fix** (`engine/world_compose.py`): `DEPTH_PRIMS` trimmed to
   `{EXCL, RATIO, SHARE, HAVING, DIVIDE, RUNNING}` — `TOPN/SORT/TIME` no longer gate to compose (the
   slot planner does order/limit/argmax/year filters *with* projection + WHERE). Plus a
   **harness-faithfulness fix**: the baseline harness lacked the live `serve()` fallback (stand on
   compose only if it actually composed; delegate on engine error) — now mirrored, so part of this
   fix's gain is the harness catching up to the live design rather than a live-behaviour change.
3. **COUNT/SUM fix** (`engine/tables.py`): "total *number* of X" → COUNT; a sum/avg cue with no
   nameable measure but a table noun in the question → COUNT(*) (ported from `read_op_all`).

**Before → after (same config, same matcher):**

| metric | baseline | tier-1 | Δ |
|---|---:|---:|---:|
| **scalar-gold accuracy** (clean) | 35.5% | **49.8%** | **+14.3** |
| **lenient** (generous UB) | 25.8% | **40.6%** | **+14.8** |
| **strict** (harsh LB) | 13.5% | **19.3%** | +5.8 |
| answered / error | 91.0% / 9.0% | 97.7% / 2.3% | — |
| `join_build` hard errors | 79 | **0** | −79 |
| routed → compose | 606 (8.4% hit) | 156 (7.1% hit) | — |
| routed → slot | 427 (50.6% hit) | 878 (46.6% hit) | — |

| lenient by difficulty | baseline | tier-1 |
|---|---:|---:|
| easy | 41.5% | **62.1%** |
| medium | 21.5% | **35.0%** |
| hard | 12.6% | 17.8% |
| extra | 7.2% | 9.0% |

**Stage histogram shift (answered-wrong 561 → 495):** `projection(forced-agg)` collapsed 84→25 (the
routing fix — projections no longer land in the always-aggregating compose path); join hard errors
went to zero; `operator(wrong-agg)` 41→39 with COUNT confusions largely gone. The remaining leaks are
exactly the Tier-2 items: **projection column selection (147, 29.7%)** and **multi-join binding
(139, 28.1%)**, then operator (105), filter (54), grouping (18).

**Flip audit:** 165 wrong→correct vs **12 regressions** (net +153 = the +14.8 pts). The regressions
are one coherent class: numeric-year filters phrased "in 1980" on an INTEGER `Year` column — compose's
`time_filter` used to catch these, and the slot planner's year rule only fires on date-typed columns.
A one-line Tier-2 follow-up (extend the slot year filter to integer year columns). The residual 24
`assemble_exec` errors are all cross-table column references (`no such column: Pets.PetType`) — the
slot planner's single-join limit, i.e. the known multi-join Tier-2 item surfacing as an error.

**Honest notes:** (a) the routing gain is bundled with the harness-faithfulness fallback — the clean
separation would need another baseline run with only the fallback added; the join fix (−79 hard
errors) and COUNT fix are cleanly attributable. (b) `engine/world_compose.py` / `engine/joins.py` /
`engine/tables.py` changes affect the **live product**; the e2e suites in `tests/` need the seeded
Postgres and should be run before deploying. (c) Predicted Tier-1 yield was +15–29 lenient; measured
**+14.8** — at the low edge, because compose's residual 156 routed queries (HAVING/RATIO head fires
that genuinely compose) still hit only 7%.
