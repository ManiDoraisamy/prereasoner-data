# World-model capability map

35 DB-grounded CSV+question cases (designed by the `world-model-capability-suite` workflow, expected values
computed from the live world DB) run end-to-end through the live `WorldReasoner` against the seeded Cloud SQL.
Run: `AUTH_TEST_SUB=cap_probe python -m regress.world_capability`. Raw: `world_capability_results.json`.

Prediction accuracy (designer vs reality): **28/35**. The map below is what the world model *actually* does.

## ✅ Works (the solid core) — resolve an uploaded entity, join a world attribute, aggregate

- city → **country** filter (`total amount in France` = 180)
- city → **continent** filter, 2-hop city→country→continent (`total sales in Europe` = 250, `…Asia` = 23)
- same-name **disambiguation** with an upload country column (Paris FR vs Paris US = 100; +continent = 160)
- **aggregate / average / share BY continent** (2-hop group-by), **top-N cities within a continent**
- **per-capita** (uploaded metric ÷ world population)
- **population as a MEASURE** (avg / sum / top-N rank)
- 2-hop **scalar** continent filter over a city column (= 260); garbage-tolerant routing; plain non-world control

## 🐞 Bugs

### ✅ Fixed — verified live, no regression, full deploy suite green (7/7 engine suites, test_world_joins 6/6)
| # | bug | fix | verified |
|---|---|---|---|
| B1 | **All `country`-column world joins empty** (`lower(country.qid)=lower(name)` → 0 rows) | `entities.py` `_world_joins`: for qid-keyed tables, map the resolved canonical → qid via `world."words"` (`DISTINCT ON (canonical)` to avoid altLabel fan-out) and join on `qid`. Type-aware (keyed on `right_col=="qid"`, so `u_s_state` name-join is untouched) — **completes the qid migration**. | country→Asia=80/Europe=150; China-robust (Asia=180/Europe=90) ✓ |
| B1b | composed country path crashed `duplicate column name: country` | `world_compose.py`: drop an enriched attr colliding with the geo column | no crash ✓ |
| B3 | **Entity-noun clarify gate** ("cities in France" → clarify though the SQL was correct) | `world_query.py` `_uncovered`: exclude entity-type nouns + schema-word plurals from the dropped-word set | "cities in France"=180, no clarify ✓ |

### ✅ Fixed (architectural — planner routing/composition, verified live, full suite green)
| # | bug | fix | verified |
|---|---|---|---|
| B4 | **Group-BY a world attribute** — "total sales **by continent**" / "per country" → empty | (1) `world_compose.py`: add `GROUP` to `DEPTH_PRIMS` so a "by X" aggregation gates to the engine; (2) `serve()` STANDS on a genuine world group-by (world_join + a *grouped* agg, cols≥2, non-empty) instead of discarding it; (3) `compose.py`: `has["GROUP"] |= bool(dims)` so a named grouping dim fires GROUP even when the head misfires TIME on "continent". Scalar aggregates still defer to the delegate. | by continent = {Europe:190, Asia:140}; per country = {France:120,…}; scalars unchanged ✓ |
| B5 | **Superlative over a world group** — "which continent has the most sales" | now groups + ranks via the B4 fixes (returns the continent breakdown sorted desc). Nit: "the most" still returns top-3, not top-1 — a small `_topn` "most/least→n=1" tweak remains. | grouped+ranked ✓ (argmax nit open) |

### ⏭ Deferred — architectural (planner: `_world_link` "numeric attrs are measures, not filter dims")
| # | bug | why architectural |
|---|---|---|
| B2 | **Element measures silently wrong** — "avg atomic mass" → `AVG("kg")` over the *uploaded* column | `_world_link` measure-selection prefers a competing upload column over `world.Elements.mass` |
| B7 | **Currency-name filter/group** — "sales where currency is euro" → clarify | `currency_name` isn't a filter dimension (new dimension type) |

### 🤷 Not a clean bug (ambiguous phrasing)
| B6 | "total **sales** in Asia" → `COUNT(*)`=2 when the table is named `sales` and the metric column is `amount` — a defensible read; no deterministic right answer. |

## 🚧 Genuine model limits (correct to refuse / out of scope — NOT bugs)

- **FX currency conversion** (amounts in local currencies → one USD/INR total): **no exchange-rate table exists
  anywhere** in the world DB. The model knows each entity's *currency name*, not rates. Correctly clarifies.
  *(your probe #1 — a real data boundary, not fixable without an FX source.)*
- **Population as a FILTER threshold** ("big cities" = pop > 1M): numeric world attributes are **measures, not
  filter dimensions**, so this is architecturally unsupported — and worse, "big cities" is silently
  reinterpreted as top-N-by-the-metric. *(your probe #2 — architectural; would need a numeric-world-filter
  primitive.)*
- **Out-of-model entity types** (a product column) and **non-existent entities**: correctly clarify / drop.

## Status

**B1, B1b, B3 fixed** (non-architectural) — verified live, zero regression across the 35-case suite.
The remaining bugs (B2, B4, B5, B7) all route through `_world_link`'s "numeric world attributes are measures,
never filter dimensions" rule — the **planner architecture** change to do next (does **not** require retraining:
the model already types the columns + reads the aggregate intent; the gaps are deterministic planner wiring +,
for FX, world data). B6 is ambiguous phrasing, not a bug. Population-as-filter and FX are architecture/data
decisions, not patches.
