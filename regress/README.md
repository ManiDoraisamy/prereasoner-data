# regress/ — the pre-deploy regression gate

One command that runs **both** halves of what Prereasoner does and fails on any regression:

```
python -m regress.run_regression            # offline tier + world tier (world runs iff KB_PG_PASSWORD)
python -m regress.run_regression --offline  # offline tier only (no Postgres)
python -m regress.run_regression --require-world   # fail (not skip) if the world tier can't run
```

## Why this exists

The Spider work (`spider/`) measured the **non-world** text-to-SQL path and drove three engine fixes — but
those fixes touched shared live code and, with no combined gate, **shipped two silent regressions** (a
dropped relationship-named FK join, and an integer-year filter that counted every row). Spider's
denotation-only, `tablename_id`-convention data structurally could not see either. This gate closes that:
it runs the world-model-join cases **and** the non-world cases together, so a change that helps one and
breaks the other is caught before deploy.

## Tiers

| tier | file | needs | what it guards |
|---|---|---|---|
| **unit invariants** | `run_regression.py::run_unit_checks` | weights only | `engine.joins` FK discovery (compose path): relationship-named FKs resolve; the child's self-id does *not* spuriously join |
| **offline** (non-world) | `offline_cases.py` | torch + weights | routing (compose vs slot), COUNT/SUM, projection, value/year `WHERE`, multi-sheet slot join — run through the real engine on in-memory SQLite |
| **world** (world-model-join) | `world_cases.py` | seeded world **Postgres** | the `city→country` world join (`total amount in France`=270, France customer count) via live `KnowledgeReasoner`, plus the maintained oracle suites in `tests/` |

Each `regress`-flagged offline case encodes a regression that must stay fixed. Cases are golden: expected
scalar / contains / min-rows / forbidden-value assertions on the executed denotation.

## How it's wired

- **cloudbuild.yaml** runs the **offline tier inside the freshly-built image** (real weights, no Postgres)
  right after `build`; a failure blocks the image push. See the `regress-offline` step.
- The **world tier** needs the seeded Cloud SQL + `KB_PG_PASSWORD` (Secret Manager) + `tests/` in the
  image; wire it with a Cloud SQL proxy step running `--require-world`, or run it pre-deploy against a
  seeded dev/staging DB. **A skipped world tier is not a pass** (`--require-world` enforces this).
- **GitHub CI** (`.github/workflows/ci.yml`) runs the hermetic suites, PostgreSQL numeric parity,
  web checks, Terraform validation, and builds all three release images from the public weight bundle.
  It also generates SBOMs and blocks critical known vulnerabilities. The model-backed regression tiers
  still run against a seeded database before deployment.

## Adding a case

Append to `offline_cases.CASES` (self-contained sheets + question + assertion) or `world_cases.CURATED`.
When you fix a bug, add the failing case first, watch it go red, then land the fix — the gate and the fix
arrive together.
