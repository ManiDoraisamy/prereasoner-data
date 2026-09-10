# Deterministic execution review — 2026-09-10

Scope: shared-plan lowering, both source emitters, ORM execution, numeric comparison, request-local
mode selection, browser/MCP/chat propagation, tests, and the corresponding runtime documentation.
Baseline: `31af22b`. This record describes the review changes in the working tree; these changes
have not been deployed as part of this review.

## Findings addressed

| Finding | Change and evidence |
|---|---|
| Direct requests had no analysis context, so `use` could be ignored | Explicit direct requests now get a transient `query` context; nested empty contexts clear inherited records |
| Unsupported explicit Python/verification could be accepted as SQL | Final response enforcement rejects those answers and reports actual execution before analysis persistence |
| Chat response shaping discarded deterministic evidence | The adapter preserves execution metadata and generated source/manifests |
| The knowledge adapter dropped the own-data delegate's generated program and views | The actual serving adapter now preserves both, with a regression through `KnowledgeTableQuery.serve` |
| Coverage inspected only the final reduction and could report earlier filters as missing | Coverage now inspects the complete emitted SQL source, with an earlier-filter regression |
| SQL service results were limited to 50 rows | Full result materialization is independent of trace previews; all three modes return 75 rows in the regression fixture |
| Emitters and runtime duplicated join/attribute rules | The shared plan owns scalar storage names and connecting edges |
| Composite ORM FK inference was ambiguous | Generated relationships now specify every join pair explicitly; composite execution is tested |
| A missing intermediate reference raised an attribute error | Generated enrichment guards every hop, matching inner-join row elimination |
| Enrichment lost prior calculated values | Generated rows retain values from their predecessor; a combined/calculated/enriched/reduced fixture verifies parity |
| Raw or absent identity evidence could merge ORM facts | Lowering requires non-null uniqueness after numeric coercion and a proven scalar relationship target |
| Grouped output could follow GROUP BY order instead of SELECT order | Lowering validates the group projection and preserves selected column order |
| Decimal normalization could erase significant differences | Comparison normalizes without Decimal-context rounding; large-value mismatch and equivalent-scale cases are tested |
| Integer division used binary float and AVG depended on ambient precision | Python executes with a local Decimal context; division/average and emitted SQL share a 20-place rounding policy |
| Verification could read different PostgreSQL snapshots | Engine-owned runs share one REPEATABLE READ transaction; connection callers retain transaction responsibility |
| Plan sequence inputs could remain mutable; generated names could collide | Sequence fields are copied to tuples; ORM, module, and generated-symbol collisions are rejected |
| Unsupported collection/conflicting enrichment paths could be misinterpreted | Plan validation rejects collection traversal and conflicting/repeated paths requiring aliases |
| Example and Sheets picker navigation lost `use` | Query helpers preserve it; unknown modes reach server validation |
| An orchestrator test existed but never ran | The missing test is registered; emitter tests now use automatic discovery in their module runner |

Python and SQL emitter versions are now 2. Source changes naturally produce different hashes.
Existing saved revisions remain historical records; opening them does not execute the new emitters.

## Verification performed

- All 27 hermetic suites passed with `RUN_ENGINE_TESTS=0` and `RUN_ORCHESTRATOR_TESTS=0`.
- The expanded deterministic emitter suite passed 25 cases, including real generated-code execution
  and SQL comparison on SQLite fixtures. It was rerun after the final plan validation changes.
- All seven Playwright tests passed: the full release journey and each of `sql`, `py`, and `both`
  through direct and chat navigation and follow-up payloads, against a local fixture API.
- Home/demo/picker Node checks and all 11 workbook reference-state tests passed.
- Changed Python files passed Ruff's `F,E9` checks; `git diff --check` passed.

The browser fixture does not execute either backend. SQLite parity does not prove PostgreSQL numeric
equivalence. No authenticated production dataset matrix or external Anthropic evaluation was run
for this review. The prior deployment's PostgreSQL dataset attempt timed out before any cases ran;
it must not be described as a successful integration evaluation.

## Remaining boundaries

1. Specialized world/compose planners still need migration to the shared plan. Manually authored
   knowledgebase graph fixtures do not establish automatic serving support for geography/FX demos.
2. `tests.test_datasets` is not yet parameterized by execution mode and does not assert the backend.
   Add a PostgreSQL matrix with independent expected results before claiming all-dataset parity.
3. Explicit-mode enforcement is currently after route execution. A rejected fallback can already
   have executed SQL and emitted provisional progress. Capability checks should eventually happen
   before execution for each planner.
4. SQL temporary views can recompute predecessors; both backends retain full stage rows. Memory and
   timing benchmarks, join-size estimates, and bounded trace collection remain performance work.
5. The automatic dual manifest does not pin a knowledgebase release. Hand-authored runs can supply
   one; production source provenance and snapshot isolation are separate from release pinning.
6. The UI has no dedicated Python source viewer. Source exists in shared-plan response/snapshot
   records. Saved conversation URLs restore history rather than verifying it under a new mode.

These are documented limits and follow-up work, not implemented capabilities. The canonical behavior
is specified in [DETERMINISTIC_EMITTERS.md](../DETERMINISTIC_EMITTERS.md); testing claims are bounded
in [TESTING.md](../TESTING.md).
