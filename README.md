# PreReasoner

**Interpretable AI for spreadsheets.** Ask a question about your data in plain English and get an
answer you can *audit* — because the AI's reasoning is shown to you as ordinary spreadsheet tabs,
and every number is real SQL run over your data, never a guess.

Unlike a chatbot that might invent a total, PreReasoner types your columns with a trained model
whose categories are named and inspectable, resolves your values to real-world entities
(Wikidata), joins your data against a world-knowledge database, and answers by building a stack of
plain SQL views. **The reasoning isn't a paragraph of prose to trust on faith — it *is* the SQL,
and you can read every step.**

Live at **[prereasoner.com](https://prereasoner.com)**.

---

## What it feels like to use

You upload a spreadsheet (or attach a Google Sheet) and ask a question. The screen is a
**workbook** — a familiar multi-tab spreadsheet — and the AI's work appears as more tabs:

- 🟢 **Your data** (green tabs) — the tables you uploaded. The AI never writes into these.
- 🔵 **Each reasoning step** (blue tabs) — one tab per step, named for what it does
  (`join orders + customers` → `where country = 'France'` → `SUM(amount)`), each showing the
  intermediate table and, on demand, the exact SQL that produced it.
- ⚪ **The facts it looked up** (grey tabs) — the world-knowledge rows it used (e.g. which country
  a city is in), so you can see *what* external facts the answer depended on.

A chat panel on the right holds the conversation: ask a follow-up and it re-runs over the same
data. Your past conversations are saved and reopenable from the menu. The answer is always a real
value with a visible derivation — click back through the blue tabs to the grey facts and your
green source rows.

For example, over `customers.csv` + `orders.csv`, *"total amount in France"* becomes four visible
steps and the answer **270** — with the France filter coming from a world-knowledge lookup of each
city's country, not from the model guessing.

---

## How it works, in one picture

```
   You ─ upload CSV(s) + a question ─▶  Workbook UI (web/)
                                          │  Google sign-in; POST /api/reason
                                          ▼
                               PreReasoner engine (engine/)  ── one Python service
                                          │
             types your columns ─┐        │        ┌─ resolves values to Wikidata entities
             (interpretable model)│       │        │  and joins against a world database
                                  ▼       ▼        ▼
                          builds a stack of SQL views ─▶ the answer + the full step trace
                                          │
                                          ▼
                             Postgres (your tables + a shared world-knowledge DB)
```

Three ideas do the work:

1. **Interpretable column typing.** A small trained encoder labels each column with *named*
   dimensions (city, country, currency, amount, …) instead of opaque features — so routing a
   question to the right column is itself inspectable.
2. **World knowledge as a database.** Values resolve to Wikidata entities and join against a
   shared world-knowledge Postgres schema (which country a city is in, a currency, a population),
   filled from Wikidata on demand. That's how PreReasoner answers questions your spreadsheet alone
   can't.
3. **Reasoning as SQL views.** The engine decomposes the question into a small stack of simple
   SQL views and runs them on your actual data. That stack *is* the reasoning trace — the same
   thing the workbook shows as blue tabs.

Full technical detail is in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**; the typed SQL planner
has a focused guide in **[docs/SQL_AST.md](docs/SQL_AST.md)**; the research thesis and how it differs
from RAG / agentic text-to-SQL is in **[docs/RESEARCH.md](docs/RESEARCH.md)**.

---

## Repository layout

| Path | What it is |
|---|---|
| `engine/` | The reasoning engine — one Python service: `POST /api/reason`, `/api/world`, `/api/dimension`, plus conversation endpoints and `/healthz`. |
| `web/` | The workbook frontend — static pages on Firebase Hosting; no build step. |
| `db/` | The world-knowledge Postgres: `init.sql` schema contract + Wikidata sync scripts. |
| `training/` | Reproduce the trained encoder and LoRA adapter from scratch (GPU). |
| `infra/` | Terraform to stand the whole thing up on your own GCP project. |
| `tests/` | End-to-end suites against a live, seeded database. |
| `docs/` | Architecture, deterministic SQL planner, research, and local testing guides. |
| `spider/` | Spider benchmark data tooling, evaluation harnesses, ranker training, and recorded results. |

**Model weights** (`encoder.pt` ~70 MB, `encoder_meta.pt`, `qwen_lora/`) aren't in git — put them
in `engine/data/` (see [engine/data/README.md](engine/data/README.md)).
<!-- TODO before publish: upload artifacts to Hugging Face Hub and link here. -->

---

## Run it locally (no GCP account needed)

```bash
cp .env.example .env          # defaults are fine for local
docker compose up --build     # Postgres (pgvector) + the engine on :8080
docker compose --profile seed run --rm seed   # one-time world-data seed (~15–45 min)
```

Ask a question directly (local mode skips Google sign-in via `AUTH_TEST_SUB`):

```bash
curl -s localhost:8080/api/reason -X POST \
  -H "Content-Type: application/json" -H "Authorization: Bearer dev" \
  -d '{"tables":[{"name":"cities","data":"city,population\nParis,2100000\nLyon,520000"}],
       "question":"which city has the largest population?"}'
```

The response carries the full trace: the `plan`, each step's `views` (with SQL and rows), and the
`result`. **[docs/TESTING.md](docs/TESTING.md)** is the complete guide — running the engine without
Docker, driving the workbook UI in a browser against your local engine, and the end-to-end suites.

---

## Deploy on your own GCP project

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=GITHUB_URL_PLACEHOLDER&cloudshell_tutorial=infra/README.md)

One Cloud Run service + Cloud SQL Postgres (pgvector), stood up by `terraform apply`. The full
walkthrough — including the manual Firebase steps for auth, hosting, and live streaming, plus a
cost table (~$56/month, dominated by Cloud SQL) — is in **[infra/README.md](infra/README.md)**.

---

## Reproducing the paper

`training/` has the complete pipeline (corpus → encoder training → anchoring → calibration), with
an honest statement of which artifacts you can just download versus retrain. See
[training/README.md](training/README.md).

## Citing

See [CITATION.cff](CITATION.cff). <!-- TODO before publish: add paper reference. -->

## License

[Apache 2.0](LICENSE)
