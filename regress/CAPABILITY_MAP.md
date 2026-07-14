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

## 🐞 Bugs (fixable — silently wrong or broken, NOT model limits)

| # | bug | evidence | severity |
|---|---|---|---|
| B1 | **All `country`-column world joins broken** | delegate joins `lower(country.qid)=lower(canon)` where canon is the NAME → 0 rows → empty; composed path *also* crashes `duplicate column name: country` | **high** (whole entity type) |
| B2 | **Element measures silently wrong** | "avg atomic mass" → `SELECT AVG("kg") FROM upload` — averages the *uploaded* column, never joins `world.Elements.mass` → 3.0 vs 105.45 | **high** (silent-wrong) |
| B3 | **Entity-noun clarify gate** | "total amount for **cities** in France" → clarify (drops "cities") though `original_sql` already has `WHERE city.country='Q142'`; "total amount in France" works | **high** (most natural phrasings) |
| B4 | **Group-BY a world attribute** | "total sales **by continent**" over a city column → the 2-hop join builds but the per-continent breakdown is empty (scalar filter works, group-by doesn't) | med |
| B5 | **Superlative over a world group** | "which continent has the most sales" → `SELECT DISTINCT continent …` → None (no argmax) | med |
| B6 | **Operator misfire in world path** | "total sales in Asia" (currency col present) → `SELECT COUNT(*)` = 2, not `SUM(sales)` | med |
| B7 | **Currency-name filter/group unsupported** | "sales where currency is euro" / "total sales by currency" → clarify; no code path filters an uploaded column by `currency_name` | low/med |

## 🚧 Genuine model limits (correct to refuse / out of scope — NOT bugs)

- **FX currency conversion** (amounts in local currencies → one USD/INR total): **no exchange-rate table exists
  anywhere** in the world DB. The model knows each entity's *currency name*, not rates. Correctly clarifies.
  *(your probe #1 — a real data boundary, not fixable without an FX source.)*
- **Population as a FILTER threshold** ("big cities" = pop > 1M): numeric world attributes are **measures, not
  filter dimensions**, so this is architecturally unsupported — and worse, "big cities" is silently
  reinterpreted as top-N-by-the-metric. *(your probe #2 — architectural; would need a numeric-world-filter
  primitive.)*
- **Out-of-model entity types** (a product column) and **non-existent entities**: correctly clarify / drop.

## Priorities

1. **B1 country join-key** (align the country name-bridge to join on `.name`, or give country a qid bridge) —
   unblocks every country-column question. Also fix the composed-path `duplicate column name: country`.
2. **B3 entity-noun clarify gate** — stop dropping the entity-type noun ("cities"/"countries") in the binder.
3. **B2 element world-measure** — wire the element mass/atomic_number attribute into the aggregate path.

FX conversion and population-filter are **product/architecture decisions**, not bugs to patch.
