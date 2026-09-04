# World-table naming: the two families (and the city/country migration)

> Historical migration record for the still-live legacy names. Use
> [../ARCHITECTURE.md](../ARCHITECTURE.md) for current request ownership and
> [../../db/README.md](../../db/README.md) for the supported database workflow.

> **Why this doc exists.** The engine routes an uploaded column to a *world table* by name, and there are
> **two naming conventions** for those tables living side by side. If you don't know that, the route
> values look inconsistent (`city` for one column, `States in the World` for another) and tests that
> assert one convention fail against an engine that emits the other. This is the record of the discrepancy,
> its root cause, and the current contract.

> **Schema update (2026-07).** Both families now live in ONE schema, **`knowledgebase`** — the former
> `world` and `wikipedia` schemas were consolidated (see `docs/notes/db.md`). So `knowledgebase."city"`
> (qid-keyed) and `knowledgebase."Cities in the World"` (friendly) sit side by side in the same schema.
> The *naming* discrepancy below is unchanged; only the schema qualifier moved from `world.`/`wikipedia.`
> to `knowledgebase.` throughout.

## The two families

| Family | Example table names | Key | Where the tables live | Which columns route here |
|---|---|---|---|---|
| **qid-keyed** | `city`, `country`, `u_s_state` | `qid` (PK/FK) | `knowledgebase."<type>"` (exact Wikidata label; `u_s_state` is the aggregate), on `search_path` | `city`, `country`, `u_s_state` |
| **friendly, name-keyed** | `Cities in the World`, `Countries in the World`, `States in the World`, `Elements in the World` | `name` (mostly) | `knowledgebase."<Friendly>"` (+ base `knowledgebase."Cities"` etc.) | `element`, and the value-membership fallback for everything |

Both families exist in the live DB (e.g. `knowledgebase."city"` **and** `knowledgebase."Cities in the World"` are
both present and populated) — that is the source of the confusion, not a missing table.

## Why the split exists (root cause)

**City and country** were moved onto the qid-keyed `knowledgebase."<type>"` schema first — see
[ARCHITECTURE.md](../ARCHITECTURE.md) ADR #6 ("QID-keyed schema, lazy-synced"): homonym-free joins on `qid`,
and 2-hop world filters (`city.country.continent`). **`u_s_state` was migrated later** (an aggregate qid-keyed
table — see below). **Element and the remaining families were *not* migrated** — they remain on the older
friendly, name-keyed `knowledgebase."<Friendly>"` tables (e.g. `knowledgebase."Elements in the World"`, joined
on `name`).

The naming for each family is set in **different places**, which is why the route values can look
inconsistent:

- **`engine/knowledge_tables.py`** — `WORLD_NAMES = {"word_city": "city", "word_country": "country",
  "word_state": "u_s_state"}`. `load_word_tables()` remaps these logical slugs to the qid-keyed table names.
  `word_element` is **not** in `WORLD_NAMES`, so it is not remapped here.
- **`engine/resolve_base.py`** — `FRIENDLY15` + `ROUTE_ORDER` (`"Cities in the World"`, `u_s_state`,
  `"Elements in the World"`, …). The **value-membership fallback** routes to these names.
- **`engine/entities.py`** — `WORLD_TABLE_TYPE = {"city": "city", "country": "country", "u_s_state": "state"}`
  and `TYPE_TO_FRIENDLY = {v: k …}`. This is the **downstream contract**: a route value only works on the
  qid-keyed join path if it is a key of `WORLD_TABLE_TYPE` (i.e. `city`/`country`/`u_s_state`).

## How `route()` picks a name

`engine/knowledge_query.py:route()` runs two paths and the model path wins (`setdefault`, never overridden):

1. **Model-driven** (`engine/router.py`): the trained property model DECODES a *family* for the column by
   schema.org-property consensus (see docs/TRAINING.md — nothing is anchored as a "type"; the family emerges
   from which distinctive properties fire). A geo family then GROUNDS its cells against the qid-keyed geo types
   (`city`/`country`/`state`, gated by `_grounds`): a grounded column routes to
   `TYPE_TO_FRIENDLY.get(wtype)` — `"city"` / `"country"` / `"u_s_state"` — all of which are in `self.words`
   (via `WORLD_NAMES`). A column whose best family fires below the abstain threshold (a literal — amount/id) is
   left untyped.
2. **Value-membership fallback** (`resolve_base.route`): fills columns the model left untyped, using the
   **friendly names** (`Elements in the World`, …).

**Net result — the current contract:**

| Uploaded column | Routes to | Family |
|---|---|---|
| `city` | `city` | qid-keyed `knowledgebase."city"` |
| `country` | `country` | qid-keyed `knowledgebase."country"` |
| `u_s_state` | `u_s_state` | qid-keyed aggregate `knowledgebase."u_s_state"` (migrated — see below) |

This is self-consistent (city/country migrated first; u_s_state migrated later). It only *looked* wrong
before, because the tests expected the pre-migration friendly names for city/country.

## What was actually broken vs. correct

- **The engine was correct.** No engine change was needed for the city/country split; the route values match
  the join paths (`city`/`country` → qid-keyed join; `Elements in the World` → friendly join).
- **The tests were stale.** `tests/test_route_wired.py` and `tests/test_world_joins.py` asserted
  `"Cities in the World"` / `"Countries in the World"` for city/country — the pre-migration names. They now
  assert `"city"` / `"country"`; `u_s_state` asserts `"u_s_state"` after its later migration.

## u_s_state migration (done)

`u_s_state` is now on the qid-keyed path too. State/province/region spans many Wikidata types (U.S. state,
region of Italy, German state, …), so — unlike city (Q515) / country (Q6256) — there is no single
`knowledgebase."<type>"` table. Instead the aggregate **qid-keyed `knowledgebase."u_s_state"`** (state qid PK,
`country`/`continent` as **qid FKs**) is the target. What was changed:

- **Data:** [`db/sync/build_u_s_state.py`](../../db/sync/build_u_s_state.py) populates `knowledgebase."u_s_state"`
  from the already-present `knowledgebase."States"` (name-keyed) + the `words` index — **no WDQS calls** — resolving
  each state name → state qid and each country name → country qid.
- **Wiring:** `word_state` → `u_s_state` in `WORLD_NAMES` (knowledge_tables.py) and `FRIENDLY15`/`ROUTE_ORDER`/
  `ROUTE_CONCEPTS` (resolve_base.py); `u_s_state → state` added to `WORLD_TABLE_TYPE` (entities.py);
  `word_state.json` gains the `continent` filter attr and qid FK links.
- **Join key:** `word_state.json` keeps `key: "name"`. The persisted bridge resolves non-city cells to the
  canonical **name** (CONN_DDL: "qid for city, canonical otherwise"), so `u_s_state` joins on `name`; the
  qid PK + `country`/`continent` qid FKs make the **filter** exact. (This is the real fix: the old friendly
  `knowledgebase."States"` stored `country` as a *name*, so a `country = 'Q38'` filter matched nothing.)

Verified end-to-end against the live DB: a state column routes to `u_s_state` and `total amount in Italy`
→ **130** (Lombardy + Sicily), with city/country routing unchanged.

**Still on the friendly name-keyed family:** `element`/`continent` (low demand; same migration recipe
applies if needed). Elements are now source-grounded by the value-membership fallback and use the
`Elements in the World` view. The non-`u_s_state` friendly tables also still carry `country` as a name, so a country
**filter** on those is a known gap — migrate them the same way (qid FK table) when needed.

## Separate issue (historical): value undercounts were WDQS lazy-fill, not naming

`test_world_joins` value mismatches seen locally in 2026-08 (France 180 vs 220, empty state result) were
**not** the naming issue — they were Wikidata (WDQS) lazy-fill timeouts from a dev machine. Request-time
fill was removed on 2026-09-04 (see DECISIONS.md "Serving is read-only on shared facts"): the projections
are now built offline by `db/sync/build_qid_world.py`, a resolution miss abstains, and this failure mode
cannot recur. Kept here because the diagnosis explains historical undercounts in old logs.
