# Claude Repository Rules

This file is the single source of truth for AI-agent working rules in this
repository. Claude Code reads it automatically from the repository root.

Do not create additional agent rule files, phase-specific rule files, or
competing implementation plans. Amend this file when the working rules need to
change. Do not weaken, bypass, or rewrite these rules to complete another task;
changing this file requires an explicit user request.

Architecture facts belong in `docs/ARCHITECTURE.md`, durable structural
decisions belong in `DECISIONS.md`, and measured SQL results belong in
`spider/results/RESULTS.md`. Do not duplicate those documents here.

## Non-negotiable outcome

Every production responsibility has exactly one owner, one active
implementation, and, where applicable, one promoted model bundle.

A change is incomplete if it leaves behind an old implementation, fallback,
wrapper, flag, artifact, route, test, configuration key, or document that
competes with its replacement. "Keep both for now" requires explicit user
approval and a documented removal condition.

## Current ownership map

Extend these owners. Do not build parallel replacements.

| Responsibility | Production owner |
|---|---|
| Own-data typed SQL AST and rendering | `engine/sql_ast.py` and the focused `engine/sql_*.py` modules |
| Own-data AST search orchestration | `engine/sql_search.py`, called by `engine/tables.py:TableQuery._serve_ast` |
| Composition DAG, view execution, and the world-dependency record | `engine/compose.py` |
| World/compose routing decision (the ONE shared `route()`) | `engine/routing.py` |
| Compose serving host + world-grounding lookup | `engine/knowledge_compose.py` |
| World grounding and knowledge joins | `engine/knowledge_query.py` |
| Candidate scoring | `engine/sql_rank.py` |
| Table normalization and canonical planner table names | `engine/tables.py` |
| World-table maintenance catalog (what is maintained, its cadence, when it last refreshed) | `db/sync/schedule.py` — the ONE writer of `knowledgebase.schedule`, read at serving time by `engine/pg.py:PgQuery._table_freshness` |
| Private reference validation, persistence, and request selection | `engine/master.py` |
| Runtime model loading and overlay | `engine/encoder_overlay.py` |
| Runtime model bundle | `engine/data/`, pinned by `engine/data/weights_manifest.json` |
| Property-model training pipeline | `training/props/` |
| Schema.org ontology contract (compiled vocabulary + inheritance) | `engine/schema_org.py` + `engine/data/schema_org_v30.json` |
| Schema.org class decode + serving table interpretation (evidence-only) | `engine/schema_decode.py` + `engine/schema_model.py`, captured via `engine/knowledge_query.py` typing buffer |
| Schema.org semantic corpus + named-property-head training | `training/schema_org/` (candidates only, in `training/schema_org/data/experiments/<corpus>/`) |
| Wikidata type QID -> schema.org class, CORPUS side | `training/schema_org/source_adapters.py:WIKIDATA_MAPPINGS` (the snapshot's populated leaf QIDs) |
| Wikidata type QID -> schema.org class, ROUTER side | `engine/data/families.json` (canonical QIDs; 17 of its 27 resolve to zero rows in the snapshot) |
| Promotion of a Schema.org candidate into the runtime bundle | `training/schema_org/promote.py` — the ONE writer of `engine/data/schema_{property_head.pt,property_model.json,class_signatures.json}` |
| Serving-faithful Spider evaluation | `spider/probe/full_eval.py` |
| Hermetic SQL planner regression suite | `tests/test_sql_ast.py` |
| Workbook and reference-data browser lifecycle | `web/public/lib/workbook.js` |

The routing decision is `engine/routing.py:route()` — one pure function that both
`engine/knowledge_compose.py` (serving) and `spider/probe/full_eval.py` (evaluation)
import and call; neither mirrors it. `route()` decides on the EXPLICIT
`world_dependency` record emitted by `engine/compose.py:ComposeEngine.run` (which
world attribute/value the upload lacks), never on the presence of a `world_join` op.
Extend `route()` in place; do not reintroduce a mirrored routing predicate in the
evaluator.

If this map becomes inaccurate, update it in the same change that moves
ownership. Moving ownership means migrating all callers and deleting the old
owner; it does not mean adding another owner.

## Before editing: write the change contract

Before changing files, state this short contract in the task response:

```text
Goal:
Production owner being changed:
Files to modify:
Files/paths to remove:
Behavioral invariant:
Baseline and acceptance metric:
Tests/evaluation to run:
```

Then inspect before inventing:

1. Run `git status --short` and preserve unrelated user work.
2. Read the relevant owner, its callers, tests, and current documentation.
3. Use `rg` to find every implementation, symbol, route, config key, artifact
   path, and benchmark path for the responsibility.
4. Decide whether the change extends the owner or replaces it.
5. If replacement would leave two paths alive, stop and define the migration
   and deletion in the contract before coding.

Do not begin a new phase while the previous phase has temporary modules,
duplicated logic, stale flags, unregistered tests, or unclassified artifacts.
Consolidation is part of each phase, not a future cleanup phase.

## One implementation rule

- Modify the current owner in place when its abstraction is still correct.
- Extract a shared module only when two legitimate callers need the same
  behavior. Both callers must use it in the same change.
- A replacement must migrate all callers and tests and delete the replaced
  code, flags, artifacts, and documentation in the same coherent change.
- Do not add names such as `v2`, `new`, `old`, `legacy`, `backup`, `final`,
  `fixed`, `experimental`, or phase numbers to production code or artifacts.
- Do not add a second planner, ranker, router, evaluator, training pipeline,
  model loader, endpoint, or model bundle for comparison.
- Experiments belong outside production imports and must have an explicit
  expiry: promote one implementation or delete the experiment.
- Do not preserve dead code in comments or commented-out blocks. Git is the
  archive.
- Do not add broad `try new_path; except: old_path` fallbacks.
- Compatibility shims are allowed only for a real external contract. Record
  the consumer, removal condition, and removal date in `DECISIONS.md`.
- Do not copy constants or routing predicates with a "keep in sync" comment.
  Extract and import the shared definition.
- Existing historical naming or layout debt does not justify adding more, and
  must not be opportunistically rewritten outside the current task.

## SQL planner and routing rules

- The typed AST is the only own-data SQL representation. New SQL behavior must
  be expressed as typed AST nodes, constraints, expansions, and renderer
  support, with focused tests.
- Compose is a composition/view system, not a second general own-data SQL
  solver. Do not broaden its ownership merely to fix an AST accuracy miss.
- Routing must have one production predicate shared by serving and evaluation.
  The evaluator may inject dependencies, but it may not reimplement routing.
- Do not add question-specific lexical patches. Fix a named failure family and
  add positive, same-profile contrastive, and negative regression cases.
- Search and ranking must remain deterministic: stable candidate construction,
  explicit tie-breakers, no hash-order dependence, and fixed seeds wherever a
  seeded component exists.
- Execution success is a validity signal, not proof that the SQL is correct.
  Measure candidate-pool recall separately from top-1 ranking and routing.
- Do not use Spider gold SQL, gold tables, or answer-derived information in a
  serving decision. `whole_db` is the standard headline configuration;
  `gold_tables` is an explicitly labeled oracle ablation.

## Model and training rules

Do not train merely because accuracy regressed. First classify the regression
with serving-faithful ablations as routing, schema selection, candidate recall,
ranking, execution, model signal, or evaluation error.

Training requires an explicit user request in the current task and a written
experiment contract containing:

```text
Hypothesis:
Frozen baseline artifact and hash:
Training data source, version/hash, and split:
Objective and changed variables:
Seed(s):
Offline acceptance gates:
Serving/Spider regression gates:
Candidate output directory:
Promotion and rollback procedure:
```

The following are mandatory:

- Run one controlled experiment per hypothesis. Do not repeatedly retrain while
  also changing code, data, objective, and evaluation.
- Never train into `engine/data/`. Candidate weights go only to
  `training/props/data/experiments/<experiment-id>/`, which is disposable and
  gitignored.
- Never overwrite the promoted runtime bundle during an experiment.
- Never create `backup`, `old`, numbered, or side-by-side production model
  directories. A manifest and immutable hashes identify versions.
- Compare a candidate and baseline with the same code, data split, evaluator,
  configuration, and seeds.
- Record failed experiments as compact metrics and provenance, not duplicate
  checkpoints in the repository.
- Promotion is a separate, explicit action. It requires all gates to pass,
  explicit user approval, an atomic replacement of the one runtime bundle,
  an updated `weights_manifest.json`, and rollback instructions.
- Do not upload weights, publish a model, push, or deploy unless the user
  explicitly requested that action in the current task.

Changing model size is an experiment, not an architectural shortcut. A larger
encoder must beat the current model under the same controlled contract and must
justify its latency, memory, startup, and deployment costs.

## Evaluation and evidence rules

- Capture the baseline before changing behavior.
- Evaluate through the production entry point. Do not maintain a simplified
  evaluator implementation of serving behavior.
- Invalidate cached predictions when any serving code, routing code, model
  artifact, schema input, or evaluator contract changes.
- Every saved result must include the code commit, dirty-worktree state,
  evaluator/configuration, dataset identity, artifact hashes, and key flags.
- Never refresh only the aggregate JSON while leaving per-example results or
  documentation stale.
- Report denominators and absolute counts with percentages.
- For routing changes, report the transition matrix: wins, losses, unchanged
  correct, and unchanged wrong.
- For planner changes, report top-1 accuracy, candidate-pool recall, and useful
  strata such as schema width and failure family.
- Never describe `gold_tables` as standard Spider or compare it directly with
  gold-blind systems.
- Treat committed benchmark results as evidence, not runtime input.

## Test and validation rules

Tests must exercise the production owner. A copied test-only implementation is
not coverage.

Minimum validation:

- Planner/search/ranker change: `python -m tests.test_sql_ast`
- Compose/routing change: `python -m tests.test_compose`
- General Python change: `python -m compileall -q engine db training tests orchestrator mcp_server`
- Repository-wide change: `python -m tests.run_all`
- Spider behavior change: fresh serving-faithful `whole_db` evaluation; add
  `gold_tables` only when the oracle ablation answers a specific question

Run focused tests while developing and the full applicable set before claiming
completion. Report skipped live suites as skipped; do not call a partially
skipped run a full pass.

Every bug fix needs a regression test that fails for the observed reason.
Register tests in the repository's explicit `TESTS` lists where required.

## Definition of done

Before finishing:

1. Run `git diff --check`.
2. Review `git diff` for accidental generated files and unrelated edits.
3. Use `rg` to confirm removed symbols, paths, flags, and artifact names have no
   live callers or stale documentation.
4. Confirm there is exactly one production owner and one promoted artifact for
   each changed responsibility.
5. Delete superseded code, wrappers, configs, tests, checkpoints, and docs.
6. Update `docs/ARCHITECTURE.md`, `DECISIONS.md`, or
   `spider/results/RESULTS.md` only when their facts changed.
7. Run and report the applicable tests and evaluations with exact results.
8. Show the final `git status --short` and distinguish pre-existing files from
   files changed by this task.

Do not claim "clean", "complete", "production-ready", or "no regression"
without satisfying this checklist. If a required item cannot be completed,
state the precise gap and leave the repository in one coherent state.

## Actions requiring confirmation

Ask before:

- introducing a second production path or temporary compatibility path;
- changing the ownership map or public API;
- training or promoting a model;
- committing generated benchmark output;
- deleting user data or unrelated work;
- committing, pushing, publishing, or deploying.

When a request appears to require one of these actions, explain the single-path
design and migration first. Do not silently accumulate another version.
