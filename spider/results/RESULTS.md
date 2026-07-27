# Spider Results — canonical current report

**Pinned to commit `8e9187b` (+ the redundant-world-filter correction on top).** Numbers below are the
serving-faithful measurement: `spider/probe/full_eval.py` running the exact production selector on **Spider
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
| **whole_db** — gold-blind, all DB tables fed (**standard Spider**) | **340/1034 (32.9%)** | 437/1034 (42.3%) | 212/408 (52.0%) |
| **gold_tables** — oracle table selection, only gold-referenced tables fed | **424/1034 (41.0%)** | 544/1034 (52.6%) | 240/408 (58.8%) |

`whole_db` is the number to compare against other Spider systems (gold-blind; it also pays the cost of table
selection). `gold_tables` feeds only the tables the gold SQL references — the closer analogue to the product,
where a user uploads exactly the relevant sheets — and is an upper bound relative to standard Spider, not a
standard-Spider result. **strict** = exact row-set equality (harsh LB); **lenient** = containment (generous
UB); **scalar-gold** = the clean unambiguous single-value subset (n=408).

## By difficulty

**whole_db** (standard Spider):

| difficulty | n | answered | strict | lenient | scalar |
|---|--:|--:|--:|--:|--:|
| easy | 248 | 243 | 110 | 127 | 96/173 |
| medium | 446 | 432 | 123 | 177 | 54/101 |
| hard | 174 | 163 | 60 | 85 | 45/77 |
| extra | 166 | 162 | 47 | 48 | 17/57 |
| **all** | **1034** | **1000** | **340** | **437** | **212/408** |

**gold_tables** (oracle tables):

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
- **Table selection is the biggest whole_db lever.** gold_tables (424) vs whole_db (340) is a 84-example gap
  from feeding all tables; most of it is over-joining (unnecessary tables). Table-set retrieval is the next
  planned accuracy work — it is not yet implemented.
- **`what is the total number of X`** emits a grouped COUNT instead of the scalar (a documented planner gap,
  tracked in `regress/offline_cases.py`); the common phrasings (`how many X`, `count the X`) are correct.

## Historical

Earlier diagnostics for the **retired** slot-filler + compose-routing era (the "13.5% strict", the tier-1/
`gate` routing experiments, the 59%-to-compose mis-routing analysis, and the proposer/ranker pool studies) are
**superseded** by the single-planner architecture and the world-grounded routing boundary above. They are
preserved in git history (see commits through `d14ee80`) and are intentionally not carried here — this file is
the current report, not a changelog.
