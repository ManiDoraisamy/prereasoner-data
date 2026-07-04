# PreReasoner

**Interpretable data reasoning: natural-language questions over your spreadsheets, answered as
inspectable SQL over a world-knowledge database — with the full reasoning trace streamed live.**

Upload a CSV, ask a question in plain English. PreReasoner types your columns with a trained
encoder whose dimensions are *named and inspectable*, resolves cell values to Wikidata entities,
joins your data against a world-knowledge Postgres database, and answers by building a stack of
SQL views you can read, audit, and re-run. No chain-of-thought to trust on faith — the reasoning
*is* the SQL.

- **Research claims and novelty:** [docs/RESEARCH.md](docs/RESEARCH.md)
- **How the system works:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **Why the repo looks the way it does:** [DECISIONS.md](DECISIONS.md)

## Repo layout

| Path | What it is |
|---|---|
| `engine/` | The reasoning engine — one Python service: `POST /api/reason`, `POST /api/world`, `POST /api/dimension`, `GET /healthz` |
| `web/` | Frontend (static pages on Firebase Hosting, streams live traces from Firebase RTDB) |
| `db/` | World-knowledge Postgres: `init.sql` schema contract + Wikidata sync scripts |
| `training/` | Reproduce the encoder and LoRA adapter (GPU; see its README) |
| `infra/` | Terraform: one Cloud Run service + Cloud SQL (pgvector) on your own GCP project |
| `tests/` | End-to-end suites against a live seeded database |

## Model artifacts

The trained weights (`encoder.pt` ~70 MB, `encoder_meta.pt`, `qwen_lora/`) are not stored in
git. Place them in `engine/data/` — see [engine/data/README.md](engine/data/README.md) for the
artifact table and download location. <!-- TODO before publish: upload artifacts to Hugging Face Hub and link here. -->

## Run and test it locally (no GCP account needed)

```bash
cp .env.example .env          # defaults are fine for local
docker compose up --build     # Postgres (pgvector) + engine on :8080
docker compose --profile seed run --rm seed   # one-time world-data seed, ~15–45 min
```

Then ask a question (local mode bypasses Firebase auth via `AUTH_TEST_SUB`):

```bash
curl -s localhost:8080/api/reason -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev" \
  -d '{"tables":[{"name":"cities","data":"city,population\nParis,2100000\nLyon,520000"}],
       "question":"which city has the largest population?"}'
```

**[docs/TESTING.md](docs/TESTING.md) is the full local test guide**: running the engine
without Docker (`python -m engine.server`), testing every endpoint with curl, driving the
web UI in a browser against the local engine (two console toggles — no Google sign-in, no
deploy), the browser regression suite, and the end-to-end Python suites. Database seeding
options are in [db/README.md](db/README.md), frontend details in [web/README.md](web/README.md).

## Deploy on your own GCP project

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=GITHUB_URL_PLACEHOLDER&cloudshell_tutorial=infra/README.md)

One Cloud Run service + Cloud SQL Postgres (pgvector), stood up by `terraform apply`. The
full walkthrough — including the manual Firebase project steps for auth/hosting/streaming and
a cost table (~$56/month, dominated by Cloud SQL) — is in [infra/README.md](infra/README.md).

## Reproducing the paper

`training/` contains the complete pipeline (corpus → encoder training → anchoring →
calibration) with an honest statement of which artifacts are downloadable versus retrainable.
See [training/README.md](training/README.md).

## Citing

See [CITATION.cff](CITATION.cff). <!-- TODO before publish: add paper reference. -->

## License

[Apache 2.0](LICENSE)
