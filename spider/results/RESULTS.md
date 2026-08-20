# Spider Results — canonical current report

The current `whole_db` row was measured from source commit `2360e26` with a clean worktree. The exact planner,
evaluator, dataset, and encoder hashes are recorded in the result JSON at
`spider/results/deployment_hardening_20260818/whole_db/full_eval_whole_db/`. Numbers below are the serving-faithful
measurement: `spider/probe/full_eval.py` running the exact production selector on **Spider
dev (1034 examples, 20 DBs)**, real trained encoder (Qwen2.5-0.5B + LoRA + relational readout, `engine/data/`)
on **CPU**, denotation compared against the real gold rows. Difficulty labels are the official `eval_hardness`
(vendored verbatim).

The own-data path is now a **single deterministic typed-AST planner** (`engine/tables.py:search_ast` →
`engine/sql_search.py`); there is no slot-filler, no trained proposer, and no learned ranker. The
`ComposeEngine` is a world-enrichment component that the shared router (`engine/routing.py:route`) invokes
ONLY on a *necessary* world dependency — and Spider is world-less, so **every Spider question routes to the
typed-AST planner** (`routed = {ast: 1034}`; zero compose). Accuracy here is entirely the planner's.

## Headline

| Configuration | strict | lenient | scalar-gold |
|---|---:|---:|---:|
| **whole_db** — gold-blind, all DB tables fed (**standard Spider**) | **359/1034 (34.7%)** | 453/1034 (43.8%) | 224/408 (54.9%) |
| **gold_tables** — oracle table selection, last verified before this planner change | **424/1034 (41.0%)** | 544/1034 (52.6%) | 240/408 (58.8%) |

`whole_db` is the number to compare against other Spider systems (gold-blind; it also pays the cost of table
selection). `gold_tables` feeds only the tables the gold SQL references — the closer analogue to the product,
where a user uploads exactly the relevant sheets — and is an upper bound relative to standard Spider, not a
standard-Spider result. **strict** = exact row-set equality (harsh LB); **lenient** = containment (generous
UB); **scalar-gold** = the clean unambiguous single-value subset (n=408).

## By difficulty

**whole_db** (standard Spider):

| difficulty | n | answered | strict | lenient | scalar |
|---|--:|--:|--:|--:|--:|
| easy | 248 | 243 | 121 | 138 | 107/173 |
| medium | 446 | 432 | 128 | 179 | 54/101 |
| hard | 174 | 163 | 63 | 88 | 46/77 |
| extra | 166 | 162 | 47 | 48 | 17/57 |
| **all** | **1034** | **1000** | **359** | **453** | **224/408** |

**gold_tables** (oracle tables):

This row is the last verified oracle ablation from commit `37570f8`; it was not rerun for the current planner
change and must not be used to calculate a current table-selection gap.

| difficulty | n | answered | strict | lenient | scalar |
|---|--:|--:|--:|--:|--:|
| easy | 248 | 238 | 126 | 150 | 106/173 |
| medium | 446 | 430 | 161 | 240 | 60/101 |
| hard | 174 | 159 | 85 | 97 | 54/77 |
| extra | 166 | 159 | 52 | 57 | 20/57 |
| **all** | **1034** | **986** | **424** | **544** | **240/408** |

## Reproduction

```bash
python spider/probe/fetch_data.py --include-train        # one-time: Spider dev + tables + sqlite DBs
cd spider/probe
python full_eval.py --dbs ../data/dbs --config whole_db    --selection serving_top1 --max-candidates 25
python full_eval.py --dbs ../data/dbs --config gold_tables --selection serving_top1 --max-candidates 25
```

`--selection serving_top1` reproduces the live top-1 selector byte-for-byte (`engine/tables.py:_serve_ast`).
There is no `--planner`, `--proposer-model`, `--ranker-model`, or `--profile-expansion` — the planner is
unconditional. The summary JSON records the code fingerprint, the encoder-adapter hash, and (as of this
report) the source commit + dirty-worktree flag, so a run is traceable to an exact tree.

## Known limitations

- **Structural floor.** Spider's hard/extra tiers are ~50% nested subqueries + set operations; the typed-AST
  planner covers many via `sql_recursive`/`sql_constraints`, but deep multi-step nesting remains the dominant
  miss on those tiers (see the difficulty tables — extra tops out ~28%).
- **Table selection remains a major whole-db lever.** The previous oracle ablation was 424 versus 340 on an older
  tree, with many gold-blind misses caused by unnecessary tables and joins. The oracle row has not yet been rerun on
  the current planner, so its current gap is deliberately not claimed here.
- **Language-to-role binding remains the main easy/medium failure source.** The current gain came from count
  scope, identity, and source/destination role fixes; projection identity and relationship direction still account
  for many candidate-ranking misses outside those families.

## Current transition

Against the previous accepted `release_review_final5_20260802` row (358 strict / 451 lenient / 224 scalar), the
current run has one strict gain, two lenient gains, no scalar change, and **zero losses**. Three selected SQL strings
changed. The gains remove spurious related-table projections and joins for document-template and contestant-name
questions. The accepted artifact is `deployment_hardening_20260818`; earlier evaluation directories are retained in
Git history rather than duplicated in a public checkout.

### Schema.org named-dimension evidence — non-regression re-measurement

`schema_evidence_20260819` re-measured `whole_db` after the Schema.org named-property head, class decoder, and
typing-evidence capture were added (`engine/schema_*.py`, `training/schema_org/`, evidence surfaced through
`engine/knowledge_query.py` and `engine/knowledge.py`). The result is **identical on every reported figure**:

| Run | strict | lenient | scalar-gold | routed | answered |
|---|---:|---:|---:|---|---:|
| `deployment_hardening_20260818` (accepted) | 359 | 453 | 224/408 | `{ast: 1034}` | 1000 |
| `schema_evidence_20260819` | 359 | 453 | 224/408 | `{ast: 1034}` | 1000 |

Every difficulty tier matches exactly (easy 121/138, medium 128/179, hard 63/88, extra 47/48 strict/lenient), as
do the error count (34, all `ast_search`) and over-budget count (3). The recorded `encoder`/`encoder_meta` hashes
equal the promoted `weights_manifest.json` entries, confirming the measurement ran against the promoted runtime
bundle. This is the expected outcome rather than a lucky one: the additions are evidence-only — Spider imports
none of the new modules, and the changed engine files delete no decision logic (the family-consensus, abstain,
and grounding boundaries are untouched). The new head is **not** part of the promoted bundle.

## Historical

Earlier diagnostics for the **retired** slot-filler + compose-routing era (the "13.5% strict", the tier-1/
`gate` routing experiments, the 59%-to-compose mis-routing analysis, and the proposer/ranker pool studies) are
**superseded** by the single-planner architecture and the world-grounded routing boundary above. They are
preserved in git history (see commits through `d14ee80`) and are intentionally not carried here — this file is
the current report, not a changelog.
