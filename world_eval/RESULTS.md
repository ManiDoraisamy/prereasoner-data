# World-model eval — oracle joins the world tables

The complement to the Spider suite. Spider tests capability (1) **text-to-SQL over self-contained data**;
this suite tests capability (2) **world-model resolution + join** — the product's differentiator, which
scores 0 on Spider by construction (no Spider query needs world knowledge).

## Method — the gold is derived from the world DB, not hand-labelled

Each case is an uploaded CSV (or two) + a natural-language question + an **oracle SQL**. The oracle computes
the expected answer by **joining the upload against the clean world tables** (`knowledgebase."Countries"` /
`"Cities"` / `"Elements"`) at eval time — so the gold is ground truth from the world DB, and stays correct if
the data changes. PreReasoner runs its own resolve→route→view-stack path on the same upload+question, and
`run.py` compares (scalar / lenient / strict, the Spider yardstick).

- Oracle joins on **canonical entity names** that exist in the clean world tables (unambiguous ground truth).
- City name collisions (many "Paris"/"Berlin" in the US) are disambiguated by **max population** — the
  prominent city — matching how the resolver picks Paris→France.
- `world.Countries`(name, continent, currency), `world.Cities`(name, country, population),
  `world.Elements`(name, mass) are the clean joinable tables (the `wikipedia.*` per-type tables are the
  resolver's internal path; the oracle uses the clean tables as an independent check).

Run: `python world_eval/run.py` (needs `KB_PG_PASSWORD` from `.env`; loads the model once).

## Results (live KnowledgeReasoner + seeded Postgres) — 6/8 exact (75%)

| case | capability | oracle | PreReasoner | verdict |
|---|---|---|---|---|
| sales_by_continent | country→continent · group+sum | Asia 310 / Eur 200 / NA 300 / SA 90 | same | **PASS** |
| top_continent | country→continent · argmax | Asia | Asia (top of ranked list) | **PASS** |
| total_in_asia | country→continent · filter+sum | 310 | 310 | **PASS** |
| countries_in_europe | country→continent · filter+count | 2 | 2 | **PASS** |
| total_amount_france | city→country · filter+sum, 2-table | 270 | 270 | **PASS** |
| count_customers_france | city→country · filter+count | 2 | 2 | **PASS** |
| avg_atomic_mass | element→mass · avg | 9.67 | **2.0** | **FAIL** |
| amount_by_currency | country→currency · group+sum | CNY 200 / EUR 200 / USD 300 / … | names, US+JP missing | **FAIL** |

## The two failures are real product findings (not harness bugs)

1. **`avg_atomic_mass` — the B2 gap.** PreReasoner averaged the uploaded `qty` column (2,1,3 → 2.0) instead of
   joining `world.Elements.mass` (→ 9.67). The world-measure for a non-geo entity (element mass) is not wired
   as an aggregatable measure — the view re-expression mismatches and it falls back to the delegate, which
   aggregates the wrong column. This is the deferred architectural item; the eval now quantifies it.
2. **`amount_by_currency` — currency coverage + representation.** PreReasoner resolves country→currency by
   **name** (renminbi, euro) where the oracle uses the ISO **code** (CNY, EUR), *and* **United States (USD) and
   Japan (JPY) don't resolve at all** — only 4 of 6 currencies returned. A real coverage gap in the
   country→currency attribute, plus a code/name representation mismatch to reconcile.

## Notes / extensions
- The argmax comparison is lenient (PreReasoner returns the full ranked list; the oracle wants the top row —
  the top is correct). A strict top-1 check is a follow-up.
- Alias robustness (USA→United States, UK→United Kingdom) is untested here — the cases use canonical names so
  the oracle join is unambiguous. Add alias cases resolved via `world.words` to exercise the resolver.
- More entity types are available for cases (`world.States`, `world.Places` with hemisphere/population, and
  the `wikipedia.*` per-type tables).
