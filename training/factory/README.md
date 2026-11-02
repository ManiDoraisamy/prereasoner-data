# The training data factory

Offline only. Gemini writes data here; nothing in serving imports this package. It reads `engine/sheet` and `engine/program` as committed at a pinned commit and writes everything outside the repository, under `FACTORY` in `paths.py`.

## Stages

Each stage can run again: replies are journalled with their token counts, so a restart never pays twice.

1. `prepare.py`: read, screen (`screen.py`), compile; a tab's rows and compiled tables for Gemini.
2. `factory.py author-plan`, `llm_run.py`: the author writes questions, programs (`grammar.py`), links and column names over one tab, by a per-tab plan of shapes and styles (`prompts.py`).
3. `factory.py execute`: bind and run each program with `engine/program`; drop what does not execute or returns nothing.
4. `factory.py rewrite-plan`, `llm_run.py`: questions over the share that use a used column's header word are rewritten; the script rejects a rewrite that still does (`overlap.py`).
5. `factory.py route-plan`, `llm_run.py`, `route-run`: a second route answers from raw cells alone, by a short program in `sandbox_run.py`.
6. `assemble.py`: agreement (`agree.py`) is gold, disagreement is ambiguous with both readings, unanswerable on both routes is refusal; script noise (`noise.py`); quotas per family; links with named coordinates.

`run_batch.sh` runs 2 to 5 for the next tabs of a side. `ledger.py` adds up the tokens.

Beside them: `build_names.py` and `vocabulary.py` (the name set, `engine/data/named_coordinates.json`), `coverage.py` and `label_sample.py` (its coverage), `anchors.py` (label-beside-formula truth), `public.py` and `isolated.py` (other industries, under `python -I`), `emails_fetch.py` and `emails_pilot.py`, `audit.py`, `leakage.py`.

## Rules

- The split is `split.py`'s reading of `design/corpora_family_map.json`, seed 20261102. A frozen, studied or dropped workbook is never listed.
- A workbook that fires the screen sends no cell. `stacey_white__39035` is never opened.
- Run `llm_run.py` from the readers' worktree, whose `engine/llm.py` is the one Gemini client; it stops at 97% of the hard budget in `ledger.py`.
- No stage prints a cell or a question.
