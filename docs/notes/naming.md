# World-table naming: the two families (and the city/country migration)

> **Why this doc exists.** The engine routes an uploaded column to a *world table* by name, and there are
> **two naming conventions** for those tables living side by side. If you don't know that, the route
> values look inconsistent (`city` for one column, `States in the World` for another) and tests that
> assert one convention fail against an engine that emits the other. This is the record of the discrepancy,
> its root cause, and the current contract.

## The two families

| Family | Example table names | Key | Where the tables live | Which columns route here |
|---|---|---|---|---|
| **qid-keyed wikipedia** | `city`, `country` | `qid` (PK/FK) | `wikipedia."<type>"` (exact Wikidata label), on `search_path` | `city`, `country` |
| **friendly, name-keyed** | `Cities in the World`, `Countries in the World`, `States in the World`, `Elements in the World` | `name` (mostly) | `world."<Friendly>"` (+ base `world."Cities"` etc.) | `u_s_state`, `element`, and the value-membership fallback for everything |

Both families exist in the live DB today (e.g. `world."city"` **and** `world."Cities in the World"` are
both present and populated) — that is the source of the confusion, not a missing table.

## Why the split exists (root cause)

The monorepo consolidation (`ade1c0b`) moved **city and country** onto the qid-keyed `wikipedia."<type>"`
schema — see [ARCHITECTURE.md](../ARCHITECTURE.md) ADR #6 ("QID-keyed `wikipedia` schema, lazy-synced"):
homonym-free joins on `qid`, and 2-hop world filters (`city.country.continent`). **State, element, and the
other families were *not* migrated** — they remain on the older friendly, name-keyed `world."<Friendly>"`
tables (e.g. `world."States in the World"`, joined on `name`).

The naming for each family is set in **different files**, which is why they disagree:

- **`engine/world_tables.py:44`** — `WORLD_NAMES = {"word_city": "city", "word_country": "country"}`.
  `load_word_tables()` remaps only these two logical slugs to the wikipedia exact-label names. `word_state`
  / `word_element` are **not** in `WORLD_NAMES`, so they are **not** remapped here.
- **`engine/resolve_base.py:25,30`** — `FRIENDLY15` + `ROUTE_ORDER` (`"Cities in the World"`, …). The
  **value-membership fallback** routes to these friendly names.
- **`engine/entities.py:36`** — `WORLD_TABLE_TYPE = {"city": "city", "country": "country"}` and
  `TYPE_TO_FRIENDLY = {v: k …}` (an identity map). This is the **downstream contract**: a route value only
  works on the qid-keyed join path if it is a key of `WORLD_TABLE_TYPE` (i.e. `city`/`country`).

## How `route()` picks a name

`engine/world_query.py:route()` runs two paths and the model path wins (`setdefault`, never overridden):

1. **Model-driven** (the trained router types the column): for `city`/`country` it emits the **wikipedia
   name** (`friendly = TYPE_TO_FRIENDLY.get(wtype)` → `"city"`, and `"city" in self.words` because
   `WORLD_NAMES` put it there). For `u_s_state` the wtype is `state`, which is **not** in
   `TYPE_TO_FRIENDLY`, so the model path skips it.
2. **Value-membership fallback** (`resolve_base.route`): fills columns the model didn't type, using the
   **friendly names** (`States in the World`, …).

**Net result — the current contract:**

| Uploaded column | Routes to | Family |
|---|---|---|
| `city` | `city` | qid-keyed `wikipedia."city"` |
| `country` | `country` | qid-keyed `wikipedia."country"` |
| `u_s_state` | `u_s_state` | qid-keyed aggregate `world."u_s_state"` (migrated — see below) |

This is self-consistent (city/country migrated first; u_s_state migrated later). It only *looked* wrong
before, because the tests expected the pre-migration friendly names for city/country.

## What was actually broken vs. correct

- **The engine was correct.** No engine change was needed; the route values match the join paths
  (`city`/`country` → qid-keyed join; `States in the World` → friendly join).
- **The tests were stale.** `tests/test_route_wired.py` and `tests/test_world_joins.py` asserted
  `"Cities in the World"` / `"Countries in the World"` for city/country — the pre-migration names. They were
  updated to assert `"city"` / `"country"` (u_s_state stays `"States in the World"`).

## u_s_state migration (done)

`u_s_state` is now on the qid-keyed path too. State/province/region spans many Wikidata types (U.S. state,
region of Italy, German state, …), so — unlike city (Q515) / country (Q6256) — there is no single
`wikipedia."<type>"` table. Instead the aggregate **qid-keyed `world."u_s_state"`** (state qid PK,
`country`/`continent` as **qid FKs**) is the target. What was changed:

- **Data:** [`db/sync/build_u_s_state.py`](../../db/sync/build_u_s_state.py) populates `world."u_s_state"`
  from the already-present `world."States"` (name-keyed) + the `words` index — **no WDQS calls** — resolving
  each state name → state qid and each country name → country qid.
- **Wiring:** `word_state` → `u_s_state` in `WORLD_NAMES` (world_tables.py) and `FRIENDLY15`/`ROUTE_ORDER`/
  `ROUTE_CONCEPTS` (resolve_base.py); `u_s_state → state` added to `WORLD_TABLE_TYPE` (entities.py);
  `word_state.json` gains the `continent` filter attr and qid FK links.
- **Join key:** `word_state.json` keeps `key: "name"`. The persisted bridge resolves non-city cells to the
  canonical **name** (CONN_DDL: "qid for city, canonical otherwise"), so `u_s_state` joins on `name`; the
  qid PK + `country`/`continent` qid FKs make the **filter** exact. (This is the real fix: the old friendly
  `world."States"` stored `country` as a *name*, so a `country = 'Q38'` filter matched nothing.)

Verified end-to-end against the live DB: a state column routes to `u_s_state` and `total amount in Italy`
→ **130** (Lombardy + Sicily), with city/country routing unchanged.

**Still on the friendly name-keyed family:** `element`/`continent` (low demand; same migration recipe
applies if needed). The non-`u_s_state` friendly tables also still carry `country` as a name, so a country
**filter** on those is a known gap — migrate them the same way (qid FK table) when needed.

## Separate issue: value undercounts are WDQS lazy-fill, not naming

`test_world_joins` value mismatches seen locally (France 180 vs 220, empty state result) are **not** the
naming issue — they are **Wikidata (WDQS) lazy-fill timeouts** from a dev machine
(`[entities] city lazy-fill failed: The read operation timed out`, `engine/world_sync.py`). The tests'
synthetic entities/2-hop facts aren't in the seed, so the engine calls WDQS to fill them; those calls time
out locally and the sums undercount. In prod Cloud Run (warm DB, reliable egress) the fills succeed and the
values match. The lazy-fill timeout/retry was widened for local robustness; it is still network-dependent.
