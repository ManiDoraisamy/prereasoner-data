# Testing PreReasoner locally

Everything can be tested on one machine without deploying anything. Cloud Run is **not**
required. There are two local setups; both were used to verify this repo end to end.

## Option A — Docker (recommended, zero local Python)

```sh
cp .env.example .env                        # defaults are fine
docker compose up --build                   # pgvector Postgres + engine on :8080
docker compose --profile seed run --rm seed # one-time world-data seed (~15–45 min)
```

## Option B — native Python (what you have; no Docker needed)

Requires Python 3.11+ with the deps from `requirements.txt`. For the database, either run
any Postgres 16 with the `vector` and `pg_trgm` extensions and apply `db/init.sql` + the
seed from [db/README.md](../db/README.md), or point at an existing world database.

```sh
# bash / Git Bash
set -a; source .env; set +a
export AUTH_TEST_SUB=localdev          # dev-only: skip Firebase token verification
unset RTDB_URL                         # optional: no Firebase at all (JSON responses)
python -m engine.server                # serves :8080, loads models in ~10 s
```

`AUTH_TEST_SUB` makes `/api/reason` and `/api/knowledge` accept any bearer token and use the
fixed principal you name (its own isolated Postgres schema). Never set it in production.

## 1. API tests (curl)

```sh
curl -s localhost:8080/healthz
# {"ok": true, "reason": true, "world": true, "dimension": true}

# Reasoning over an uploaded sheet (world join + aggregate + top-N):
curl -s localhost:8080/api/reason -X POST \
  -H "Content-Type: application/json" -H "Authorization: Bearer dev" \
  -d '{"tables":[{"name":"cities","data":"city,population\nParis,2100000\nLyon,513000"}],
       "question":"which city has the largest population?"}'

# World-knowledge filtering:
curl -s localhost:8080/api/knowledge -X POST \
  -H "Content-Type: application/json" -H "Authorization: Bearer dev" \
  -d '{"tables":[{"name":"cities","data":"city,visitors\nParis,500\nBerlin,300"}],
       "question":"which of these cities are in France?"}'

# Stateless column typing (no auth):
curl -s localhost:8080/api/dimension -X POST -H "Content-Type: application/json" \
  -d '{"data":"city,population\nParis,2100000\nLyon,513000"}'
```

Every response contains the full inspectable trace: `plan`, per-step `views` with their SQL
and rows, and `result`.

## 2. Frontend in a browser

```sh
npm i -g firebase-tools
cd web && firebase serve --only hosting --project <your-project> --port 5057
```

Open `http://localhost:5057`, then in the devtools console (once per browser):

```js
localStorage.setItem('pr_api_base', 'http://localhost:8080')  // pages -> local engine
sessionStorage.setItem('pr_test_auth', '1')                   // skip Google sign-in (localhost only)
```

Then use the site normally: the landing page ships a demo (“total amount in France” over
`customers.csv` + `orders.csv`) — submit it and watch the view stack build
(`join → world → world filter → aggregate`, answer **270**). Without RTDB the trace renders
from the JSON response a few seconds after the engine finishes; with `RTDB_URL` set and
Google application-default credentials available, it streams step by step.

What to check on each page:
- `/` landing: demo chips attach, submit navigates to `/reason`.
- `/reason`: trace plays, scrubber works, ⓘ opens Warnings / Info / Debug.
- `/world`: same flow with world-knowledge resolution.
- `/clarify`: reached automatically when the engine can’t parse the question.
- `/sheets`: renders and starts the Google Picker (needs a real Google account to finish).

## 3. Regression suite (browser)

Open `/reason`, paste `web/tests/regression.js` into the console, run `await runRegression()`.
It asserts final answers against `/api/reason` for ~60 canonical questions.

## 4. End-to-end Python suites

`tests/` needs a seeded world database (they create/drop their own per-test schemas):

```sh
pip install -r requirements.txt
set -a; source .env; set +a
python -m tests.test_geo          # geo/NEARBY + live-SQL oracle checks
python -m tests.test_world        # world joins
python -m tests.test_nongeo      # non-geo lazy Wikidata fill (network-dependent)
python -m tests.test_world_joins
python -m tests.test_route_wired
```

## What was verified before release

- All three endpoints end to end against a live world database (correct answers, including
  the canonical France = 270 demo), from both curl and real Chrome.
- The full page sweep above, with a clean browser console.
- `terraform validate`, `docker`-less CI checks (`compileall`, `node --check`), and zero-hit
  greps for legacy names, hardcoded hosts, and secrets.
- Not yet machine-verified here: `docker compose up --build` (no Docker on the dev machine)
  and a fresh `terraform apply` into a clean GCP project — run those once before announcing.
