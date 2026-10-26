# Readers training

This package trains the models behind the own-data readers in `engine/readers/`:

- the stage-1 schema-item model, which scores tables and columns;
- the stage-2 reading models, which read every fact the deterministic builder needs;
- the re-scorer, which picks among the pooled candidates;
- the choice, a logistic model over the re-scorer's logit and the pool's evidence that ranks the candidates and
  refuses below its matched threshold.

The models, their inputs and their outputs are defined once, in `engine/readers/models.py`. The trainers here add
only targets, losses, metrics and the training loop. The schema serialization (`engine/readers/text.py`), the
pruning and the stage-2 row (`engine/readers/search.py`) are also imported from serving, never copied.

Run every step as a module from the repository root: `python -m training.readers.<module> ...`.

## Rules

- Training writes only under `training/readers/data/experiments/<id>/`. That path is gitignored by
  `training/readers/.gitignore`. Labels, examples, candidates, runs and lease logs all go there.
- Nothing here writes `engine/data/readers` except the promotion step, `training/readers/promote.py`. It installs
  an assembled candidate bundle (step 10) as a whole and records its hashes in `engine/data/weights_manifest.json`.
- Paid GPU leases, training and promotion each need the owner's explicit approval and an experiment contract
  (`CLAUDE.md`, "Model and training rules").

## Modules

| Module | What it does | Reads | Writes |
|---|---|---|---|
| `sources.py` | Knows where Spider, BIRD, Spider-Syn and the chosen SynSQL questions, schemas and databases are. Reads every database capped at 5,000 rows per table, as the evaluator does. | `--spider-zip`, `--db-dir` | nothing |
| `splits.py` | The database split (seed 20261019: 20 calibration databases of train_spider; the 40 test databases untouched) and stage-1 schema-item labels read from Spider's own parse. | Spider zip | `<dir>/manifest.json`, `labels.jsonl`, `schemas.json` |
| `gold.py` | Gold readings: gold SQL becomes a typed AST, then readings. Checked by the perfect-readings oracle (gold's rows against the AST's rows and the rebuilt query's rows), with printed totals. Sharded by database (`--shard`), resumable (`--resume`). | split manifest, Spider, BIRD or the SynSQL selection | `readings_{train,calibration,bird,synsql}.jsonl` |
| `synsql.py` | SynSQL-2.5M (Apache-2.0): streams its 9.4 GB data.json once (`compact`), then chooses the questions the readers train on (`select`: no definition note, six plain styles, no schema within half of a calibration or test schema's names, readable SQL shapes, 24 per database by seeded hash), and caps gold's readable ones per database (`readings`). | SynSQL's data.json, tables.json; split manifest; Spider's test schemas (names only) | `compact.jsonl`; `<dir>/questions.json`, `tables.json`, `manifest.json`; `readings_synsql_p<N>.jsonl` |
| `labels_share.py` | Share readings for BIRD's percentage questions. Each one is checked by execution: gold must equal 100 x built. | BIRD train | `readings_bird_share.jsonl` |
| `labels_syn.py` | Gives Spider-Syn's rewordings their original question's gold readings. Leaves out the calibration databases. | split manifest, Spider-Syn, `readings_train.jsonl` | `readings_syn.jsonl` |
| `rewordings.py` | Gemini rewords training questions into the families the readers misread, chosen from the gold readings: a measure named by an everyday word, an exclusion said as negated existence (both keep the original's readings), the opposite extreme (MIN and MAX and a limited sort's direction swapped in the readings; kept only when Gemini's named phrase swap holds), and a count of one table's rows asked as a person looking at the sheet would (the sheet, its rows below the header, a column's non-empty cells; keeps the original's readings). Kept only when every stated value survives. Never on calibration or test databases. | split manifest, readings files, Spider, optional BIRD | `<dir>/rewordings.jsonl`, `readings_reworded.jsonl` |
| `structures.py` | Questions for queries built on training databases in the five structures the readers misread: the rows that no referencing row matches (`NOT IN`, at most once filtered on a value of the referencing table or a table it references); groups ranked by the total or average of a number (kept only when that ranking's first groups differ from the ranking by row count and no tie crosses the cut); the lowest, highest, average or total of a number; each group beside its total or average, unranked; and every column of a table's rows, at most filtered and ordered (`SELECT *`) (`--shapes` builds some of them). Tables and columns come from foreign keys and values; choices by hash. Two Gemini-written questions per query (one a search box's few words), kept when they state the filter value and the number ranked. Never on calibration or test databases. | split manifest, Spider, optional BIRD | `<dir>/questions.json`, `readings_structure.jsonl` |
| `lookups.py` | Lookup-table questions on training databases: a categorical text column becomes codes, a new table names each code, and five query shapes (names, labelled, counted, filtered, total) get two Gemini-written questions each, kept when they state the query's value. | split manifest, Spider, optional BIRD | `<dir>/dbs/`, `tables.json`, `questions.json`, `readings_lookup.jsonl` |
| `examples.py` | Builds stage-1 rows (one per question and table) and stage-2 rows (one per question, gold-pruned like serving), split `train`/`calibration`. `--no-anchors` serializes without the question's values. | readings files, Spider, optional BIRD, Syn, SynSQL (`--synsql`), rewordings (`--reworded`), lookups (`--lookups`) and structures (`--structures`, comma-separated folders) | `<dir>/stage1.jsonl`, `<dir>/stage2.jsonl` |
| `train.py` | The trainers. `schema_items` is stage 1; `readings` is stage 2 and keeps `--eval-from` and `--checkpointing`. | `stage1.jsonl` / `stage2.jsonl` | run dir: `model.pt`, `best.json`, `log.jsonl`, `tokenizer/` (with the encoder config), calibration predictions or outputs |
| `outputs.py` | `load_run`; `Stage1` (serving's `schema_items`, `prune` and `row` over a run); `serving_row`; and, as a command, sharded CPU stage-2 outputs for a rows file. | a readings run, rows | outputs `.jsonl` |
| `pruned.py` | Stage-2 rows through serving's stage-1 pruning, for calibration (with labels) or the Spider test set (without). | a stage-1 run, Spider | rows `.jsonl` |
| `relayout.py` | Converts a stage-2 model trained before the share outputs (v5, v6) to the current layout. The new outputs never fire. | `model.pt` | `model.pt` |
| `folds.py` | Splits the training rows into cross-fold files by database (4 folds, seed 20261019); a `--train-only` source trains in every fold and is held out in none. | `stage2.jsonl` | `fold<i>.jsonl` |
| `candidates.py` | Decodes the k best readings per question, builds and runs each, and labels them by execution against gold (a reworded question against its original's). | rows, outputs, Spider (+ `--bird`, `--syn`, `--reworded`, `--lookups`, `--structures`) | candidates `.jsonl` |
| `union.py` | Pools several models' candidates per question, each SQL once, as serving pools. | candidates files | candidates `.jsonl` |
| `rescorer.py` | The re-scorer trainer: listwise loss plus a yes/no term, checkpoint chosen on calibration candidates. | candidates files | run dir |
| `choose.py` | Scores the first, execution-guided and re-scorer choices on calibration candidates. On the Spider test set it reports totals only. | candidates or rows + outputs | stdout |
| `rescorer_data.py` | The re-scorer's training file: union pools of cross-fold candidates, plus the BIRD share questions' pools. | fold and share candidates | `.jsonl.gz` |
| `choice.py` | Fits the readers' choice (serving's `choice_features`: re-scorer logit, log probability, best rank, the readers' agreement, empty result), cross-validated by database; `match` sets its threshold so the readers answer no more wrongly than the planner they replace. | a calibration union pool, each reader's candidates, a re-scorer run; two full_eval runs | `choice.json` |
| `shrink_state.py` | Shrinks a fine-tuned encoder's state dict for a slow link (its input embedding found by the base model, XLM-R's or ModernBERT's), and expands it back. | `model.pt` / `small.pt` | `small.pt` / `model.pt` |
| `pull_parts.sh` | Pulls a finished stage-2 run off a live pod in 8 parallel parts and checks md5. | ssh host and port | run dir |
| `lease.py` | GPU jobs through `training/tools/runpod_api.py lease`: `full` stage 2, `folds`, and `rescorer`. Tries SECURE first, then COMMUNITY. `--dry-run` prints the command. | data or candidates | `data/experiments/<id>/{out,code,lease_<cloud>.log}` |
| `promote.py` | The one writer of `engine/data/readers`: gates a candidate bundle, then installs it as a whole. | a candidate bundle | `engine/data/readers`, the weights manifest |

## Inputs

These are the recorded locations. Every step also takes its paths as arguments.

| Input | Recorded location | Used by |
|---|---|---|
| Spider, official `spider_data.zip` (sha256 `00636695...`): train_spider, train_others, dev, `tables.json`; test: `test.json`, `test_tables.json` | `C:/work/datasets/spider_official/spider_data.zip` (`--spider-zip`) | all Spider steps |
| Spider databases | the checkout's `spider/data/dbs` and `C:/work/datasets/spider_official/train_dbs_extra` (`--db-dir`, repeatable; a worktree has no `spider/data`) | examples, pruned, candidates |
| Spider test databases | `C:/work/datasets/spider_official/test_dbs_flat` (`--test-dbs`) | `pruned test`, `choose test` |
| BIRD train: `train.json`, `train_tables.json`, `train_databases/` | `C:/work/datasets/bird/extracted/train` | labels_share, `examples --bird`, `candidates --bird` |
| Spider-Syn: `train_spider.json`, `dev.json` | `C:/work/datasets/spider_syn` | labels_syn, `examples --syn` |
| SynSQL-2.5M (Hugging Face `seeklhy/SynSQL-2.5M`, Apache-2.0): `data.json` (9.36 GB), `tables.json`, `databases.zip` (16,583 SQLite databases of a row or two per table) | `C:/work/datasets/synsql` (`databases/` unzipped there); the selection in `C:/work/prereasoner-readers-artifacts/data/synsql` | `synsql`, `gold synsql`, `examples --synsql` |
| XLM-R tokenizer and base weights | `C:/work/datasets/models/xlm-roberta-{base,large}` locally; `FacebookAI/xlm-roberta-{base,large}` on pods | examples (`--tokenizer`), `shrink_state expand` |
Two more inputs are made here. `splits.py` writes the split manifest; the recorded one is scratch `phase2/split1/`
(1,053 calibration and 8,640 training questions). `gold.py` writes the gold readings; the recorded ones are scratch
`phase1/readings_*.jsonl` (sha256 `b6edc741d193` train, `303b440dda30` calibration, `6e9b6cd10360` BIRD).

## Recipe of the shipped bundle

The recipe comes from the experiment contracts (`readers_v2` to `readers_v7`, `rescorer_v1` and `rescorer_v2`),
the launchers and the lease logs. Write `$E` for `training/readers/data/experiments` and `$L` for the folder that
holds the gold readings and the labels. Every stage-2 recipe uses XLM-R large, batch 16, encoder lr 1e-5 and head
lr 1e-3, 5% warmup, bf16 autocast and seed 0. `lease full` and `lease folds` set these.

### 0. Split and gold readings

```
python -m training.readers.splits $L/split
python -m training.readers.gold $L/split calibration --out $L/readings_calibration.jsonl
python -m training.readers.gold $L/split train --out $L/readings_train.jsonl
python -m training.readers.gold $L/split bird --bird C:/work/datasets/bird/extracted/train --out $L/readings_bird.jsonl
```

- `splits.py` rebuilds the recorded `labels.jsonl` and `schemas.json` byte for byte. Its `manifest.json` matches
  the recorded one byte for byte once line endings are normalized: the recorded file was written in Windows text
  mode (CRLF), and `splits.py` writes LF on every platform.
- `gold.py` keeps the recorded question order (1,053 calibration, 8,640 train, 9,428 BIRD questions). A sample of
  143 records, covering every status, matched the recorded files except 3 train records. Those 3 have identical
  readings, but their status changed from `build` to `ok` (2) and `gold-ast-differs` (1), because the builder now
  builds them. `examples.py` reads only the readings, so the training rows are unchanged.
- The recorded oracle totals: train 8,232 readable of 8,640, calibration 1,028 of 1,053, BIRD 6,558 of 9,428.

### 1. Labels

```
python -m training.readers.labels_share C:/work/datasets/bird/extracted/train $L/readings_bird_share.jsonl
python -m training.readers.labels_syn $L/split C:/work/datasets/spider_syn $L/readings_train.jsonl $L/readings_syn.jsonl
```

The recorded counts are 195 shares (sha256 `dd02c8acab13`) and 4,756 rewordings (`9fcb382af9a8`).

### 2. Examples

```
python -m training.readers.examples $L/readings_train.jsonl $L/readings_calibration.jsonl $E/data_v2
python -m training.readers.examples $L/readings_train.jsonl $L/readings_calibration.jsonl $E/data_v4 --no-anchors
python -m training.readers.examples ... $E/data_v5 --bird <BIRD> --bird-readings $L/readings_bird.jsonl --no-anchors
python -m training.readers.examples ... $E/data_v6 --bird <BIRD> --bird-readings $L/readings_bird.jsonl
python -m training.readers.examples ... $E/data_v7a --bird <BIRD> --bird-readings $L/readings_bird.jsonl,$L/readings_bird_share.jsonl --syn C:/work/datasets/spider_syn --syn-readings $L/readings_syn.jsonl
python -m training.readers.examples ... $E/data_v7b (as v7a) --no-anchors
```

- `data_v2/stage1.jsonl` is the stage-1 data (recorded sha256 `ff8c0516...`). Its stage-1 text is unchanged
  through v7, and a spot check matched today's text.
- Recorded stage-2 sha256 prefixes: v4 `db8a5cae`, v5 `c1fb0642`, v6 `fa19b4eb`, v7a `cfae58832103`,
  v7b `1dfec10cbce5`. v7 adds Spider-Syn and the BIRD shares, for 19,741 training questions.
- The order of `--bird-readings` sets the row order, and the row order sets the training shuffle. The recorded v7
  files list the shares before the rest of BIRD, which is what `readings_bird.jsonl,readings_bird_share.jsonl`
  produces.
- Rebuilt with this module, 20 of 20 sampled rows matched the recorded `data_v7a` and `data_v7b` exactly.
- v4 and v5 were built before anchors and share outputs existed. `--no-anchors` is today's equivalent, and their
  rows now also carry `"share": 0`. Their exact bytes against the recorded hashes are not verified.

### 2b. Written training questions and the v11 data (two of the shipped readers' data)

Gemini (`engine/llm.py`; `EXTERNAL_LLM_ENABLED=1`, Vertex AI, model `gemini-3.8-flash`, thinking LOW) writes three
sets on the training databases, never on calibration or test ones:

```
python -m training.readers.rewordings $L/split1 $L/readings_train.jsonl $L/readings_bird.jsonl $E/readers_v8/rewordings --bird <BIRD> --workers 8 --families measure,exclusion
python -m training.readers.rewordings $L/split1 $L/readings_train.jsonl $L/readings_bird.jsonl $E/readers_v10/opposite --bird <BIRD> --workers 8 --families opposite
python -m training.readers.lookups $L/split1 $E/readers_v9/lookups --bird <BIRD> --workers 8 --db-dir <spider dbs> --db-dir <train_dbs_extra>
python -m training.readers.structures $L/split1 $E/readers_v11/structures --bird <BIRD> --workers 16 --db-dir <spider dbs> --db-dir <train_dbs_extra>
python -m training.readers.examples $L/readings_train.jsonl $L/readings_calibration.jsonl $E/readers_v11/data_v11a --bird <BIRD> --bird-readings $L/readings_bird.jsonl,$L/readings_bird_share.jsonl --syn C:/work/datasets/spider_syn --syn-readings $L/readings_syn.jsonl --reworded $E/readers_v8/rewordings/rewordings.jsonl,$E/readers_v10/opposite/rewordings.jsonl --reworded-readings $E/readers_v8/rewordings/readings_reworded.jsonl,$E/readers_v10/opposite/readings_reworded.jsonl --lookups $E/readers_v9/lookups --structures $E/readers_v11/structures --tokenizer C:/work/datasets/models/xlm-roberta-large
python -m training.readers.examples ... $E/readers_v11/data_v11b (as data_v11a) --no-anchors
```

- The v8 run's families were the module's two families of that day (measure, exclusion); the opposite family came
  later. The recorded sets: 4,393 rewordings (the v8 set, then the opposite set), 1,724 lookup questions on 173
  lookup databases, and 2,675 structure questions on 195 databases (582 unmatched, 475 unmatched filtered, 398
  ranked, 1,220 aggregate; half written as a search box's few words).
- Stage-2 rows: 29,561 (`data_v11a` sha256 `c47128d93e231671`, `data_v11b` `201007d4b4b4b928`); the stage-1 rows
  are not used (the shipped stage 1 is the "v2" run).
- Gemini at temperature 0 is not bit-reproducible across service versions; the recorded sets are the training
  data, kept with the experiment.

### 2c. The v12 data

v11's sets plus the grouped structure ("total revenue by title" was read as each title's number of rows):

```
python -m training.readers.structures $L/split1 $E/readers_v12/structures_grouped --shapes grouped --bird <BIRD> --workers 16 --db-dir <spider dbs> --db-dir <train_dbs_extra>
python -m training.readers.examples ... $E/readers_v12/data_v12a (as data_v11a, with --structures $E/readers_v11/structures,$E/readers_v12/structures_grouped)
python -m training.readers.examples ... $E/readers_v12/data_v12b (as data_v12a) --no-anchors
```

- 806 grouped questions on 195 databases (403 queries).
- Stage-2 rows: 30,367 (`data_v12a` sha256 `1d8d5ee67fa98a20`, `data_v12b` `307eb5855fd0f4f7`).

### 2d. The v13 data

The readings hold `SELECT *` since 2026-10-23 (the star shown plain), so the gold readings were regenerated with
today's builder, and two written sets were added:

```
python -m training.readers.gold $L/split1 train --out $E/readers_v13/gold/readings_train.jsonl (likewise calibration, bird)
python -m training.readers.labels_syn $L/split1 C:/work/datasets/spider_syn $E/readers_v13/gold/readings_train.jsonl $E/readers_v13/gold/readings_syn.jsonl
python -m training.readers.rewordings $L/split1 $L/readings_train.jsonl $L/readings_bird.jsonl $E/readers_v13/sheet --bird <BIRD> --workers 8 --families sheet --limit 1600
python -m training.readers.structures $L/split1 $E/readers_v13/structures_listing --shapes listing --bird <BIRD> --workers 16
python -m training.readers.examples $E/readers_v13/gold/readings_train.jsonl $E/readers_v13/gold/readings_calibration.jsonl $E/readers_v13/data_v13a (as data_v12a, with the regenerated BIRD and Syn readings, the sheet rewordings appended to --reworded, and --structures $E/readers_v11/structures,$E/readers_v12/structures_grouped,$E/readers_v13/structures_listing)
python -m training.readers.examples ... $E/readers_v13/data_v13b (as data_v13a) --no-anchors
```

- Regenerated gold: 46 Spider training, 7 calibration and 1 BIRD `SELECT *` questions became readable; 433 training
  questions an older builder could not rebuild now rebuild to gold's rows. Gemini (`gemini-3.8-flash`, thinking LOW)
  needs `EXTERNAL_LLM_ENABLED=1 GOOGLE_CLOUD_PROJECT=prereasoner-inference GEMINI_MODEL=gemini-3.8-flash
  GEMINI_LOCATION=global`; without them every call fails.
- 1,085 sheet rewordings (of 1,172 asked) and 1,124 listing questions on 195 databases (564 queries).
- Stage-2 rows: 32,677 (`data_v13a` sha256 `776fc6563c7be14e`, `data_v13b` `feb1312a9c920d22`).

### 2e. The v14 data (the shipped readers' data): SynSQL-2.5M's databases

The readers' first error on calibration is the tables they read (the first component of each reader's best reading
that differs from gold's: "tables" for 203 to 230 of 984 readable questions, then the shown columns), and they had read
260 databases. SynSQL-2.5M (Apache-2.0) adds a few questions from each of 13,537 synthetic databases:

```
python -m training.readers.synsql compact C:/work/datasets/synsql
python -m training.readers.synsql select C:/work/datasets/synsql C:/work/prereasoner-readers-artifacts/data/synsql --split $L/split1 --test-tables <spider_data/test_tables.json>
python -m training.readers.gold $L/split1 synsql --synsql C:/work/prereasoner-readers-artifacts/data/synsql --synsql-databases C:/work/datasets/synsql/databases --shard <i>/8 --out $E/readers_v14/gold/readings_synsql_s<i>.jsonl
python -m training.readers.synsql readings $E/readers_v14/gold/readings_synsql_s*.jsonl --out $E/readers_v14/gold/readings_synsql_p4.jsonl --per-db 4
python -m training.readers.examples $E/readers_v13/gold/readings_train.jsonl $E/readers_v13/gold/readings_calibration.jsonl $E/readers_v14/data_v14a (as data_v13a, with --synsql C:/work/prereasoner-readers-artifacts/data/synsql --synsql-databases C:/work/datasets/synsql/databases --synsql-readings $E/readers_v14/gold/readings_synsql_p4.jsonl)
python -m training.readers.examples ... $E/readers_v14/data_v14b (as data_v14a) --no-anchors
```

- Selection: 2,544,390 samples; 561,498 lean on a definition note and 285,327 are vague, metaphorical or dialogue;
  2,858 databases (282,469 samples) have column or table names half of which a calibration or test schema also has;
  673,445 are in SQL shapes the readings never hold; 3 repeat a Spider question. 24 per database by seeded hash:
  326,745 questions on 13,716 databases.
- Gold readings: 144,419 rebuild to gold's rows (the databases hold a row or two per table, so the check mostly says a
  query runs); the readers' vocabulary refuses 3 more (`HAVING ... LIKE`). Four per database: 51,929 questions on
  13,537 databases (47,557 single, 4,284 nested, 67 UNION, 19 INTERSECT, 2 EXCEPT).
- Stage-2 rows: 84,606 (`data_v14a` sha256 `e422a439d0dcb3b3`, `data_v14b` `8f03d1a1e79ca2a9`), fitted as serving
  fits them since 2026-10-25 (the samples go before a table). Every row passes the trainer's own batch builder before
  a lease.

### 3. Stage 1 (the "v2" stage-1 run)

```
python -m training.readers.train schema_items --examples $E/data_v2/stage1.jsonl --backbone FacebookAI/xlm-roberta-base --out $E/stage1_v2/stage1
```

- Recipe: XLM-R base with the defaults (3 epochs, batch 32, lr 3e-5 / 1e-3, max length 512, seed 0).
- The selected epoch was 3: table AP 0.932, column AP 0.854.
- It trained in the `readers_v2` lease together with v2's stage 2, which never shipped. That launcher was not kept,
  and `lease.py` has no stage-1 job. Run the trainer locally or wrap it in `runpod_api lease`.
- The recorded weights were rescued through `shrink_state` parts, and their `best.json` was rewritten from the pod
  log.

### 4. Stage-2 readers v5, v6, v7a, v7b

```
python -m training.readers.lease full --experiment readers_v5 --data $E/data_v5
python -m training.readers.lease full --experiment readers_v6 --data $E/data_v6
python -m training.readers.lease full --experiment readers_v7a --data $E/data_v7a --epochs 13
python -m training.readers.lease full --experiment readers_v7b --data $E/data_v7b --epochs 13
python -m training.readers.shrink_state expand $E/readers_v5/out/stage2/small.pt FacebookAI/xlm-roberta-large $E/readers_v5/out/stage2/model.pt
```

- v5 and v6 trained 15 epochs; v7a and v7b trained 13. All were evaluated from epoch 8, with checkpointing and a
  200-minute lease at a $1.00/h cap.
- Selected checkpoints (gold-pruned calibration, every reading right): v5 epoch 15 (0.140), v6 epoch 10 (0.167),
  v7a epoch 8 (0.202), v7b epoch 9 (0.183).
- The recorded pods were RTX 6000 Ada and A40 cards running torch 2.4.1+cu124. That is `lease.py`'s default image.
- v5's single-stream download broke, and its weights came back with `pull_parts.sh`.
- Readers that read without anchors (v5, v7b) need `{"anchored": false}` in their run's `reader.json`, which serving
  reads. The trainer does not write that file.

### 4b. Stage-2 readers v11a, v11a1, v11b, v11b1 (v11a and v11b shipped)

```
python -m training.readers.lease full --experiment readers_v11a --data $E/readers_v11/data_v11a --seed 0 --epochs 13 --eval-from 5 --minutes 260
python -m training.readers.lease full --experiment readers_v11a1 --data $E/readers_v11/data_v11a --seed 1 --epochs 13 --eval-from 5 --minutes 260
python -m training.readers.lease full --experiment readers_v11b --data $E/readers_v11/data_v11b --seed 0 --epochs 13 --eval-from 5 --minutes 260
python -m training.readers.lease full --experiment readers_v11b1 --data $E/readers_v11/data_v11b --seed 1 --epochs 13 --eval-from 5 --minutes 260
```

- Anchored (a, a1) and plain (b, b1), seeds 0 and 1; checkpoints evaluated from epoch 5 (v10a's best had been its
  first evaluated epoch, 8).
- Selected checkpoints (gold-pruned calibration, every reading right): v11a epoch 12 (0.210), v11a1 epoch 13
  (0.216), v11b epoch 12 (0.220), v11b1 epoch 8 (0.215).
- The pods were L40S (v11a, v11b), L40 (v11b1) and A40 (v11a1) cards. Three runs' single-stream downloads stalled; their weights came
  back with `pull_parts.sh` (md5-checked). The community host that restarted its containers is left out by
  leaving the RTX 6000 Ada out of community attempts.

### 4c. Stage-2 readers v12a, v12a1, v12b, v12b1

```
python -m training.readers.lease full --experiment readers_v12a --data $E/readers_v12/data_v12a --seed 0 --epochs 13 --eval-from 5 --minutes 260
python -m training.readers.lease full --experiment readers_v12a1 --data $E/readers_v12/data_v12a --seed 1 --epochs 13 --eval-from 5 --minutes 260
python -m training.readers.lease full --experiment readers_v12b --data $E/readers_v12/data_v12b --seed 0 --epochs 13 --eval-from 5 --minutes 260
python -m training.readers.lease full --experiment readers_v12b1 --data $E/readers_v12/data_v12b --seed 1 --epochs 13 --eval-from 5 --minutes 260
```

- Selected checkpoints (gold-pruned calibration, every reading right): v12a epoch 13 (0.228), v12a1 epoch 12
  (0.236), v12b epoch 12 (0.192), v12b1 epoch 11 (0.197).
- The pods were A40 (v12a), L40S (v12a1, v12b) and L40 (v12b1) cards. Every lease ran detached on its pod
  (`runpod_api` `_run_detached`); each run's weights came back with `pull_parts.sh`, after which its lease refuses to
  train it again.
- The bundle of `cb30447` kept v11a and v11b in slots `a` and `b` and put v12b1 in `c`, v12a1 in `d` (v13b1 and v13a1
  replaced them, 4d): of the four-reader pools compared, its choice is right on the most calibration questions (667 of 1,028 out of fold, against
  664 for the four v11 readers, 659 for the four v12 readers, and 655 for v11a with v12b, v12b1 and v12a1).

### 4d. Stage-2 readers v13a1 and v13b1 (shipped until 2026-10-25), v13a and v13b

```
python -m training.readers.lease full --experiment readers_v13a1 --data $E/readers_v13/data_v13a --seed 1 --epochs 13 --eval-from 5 --minutes 230 --max-hourly-cost 0.8
python -m training.readers.lease full --experiment readers_v13b1 --data $E/readers_v13/data_v13b --seed 1 --epochs 13 --eval-from 5 --minutes 230 --max-hourly-cost 0.8
```

- Selected checkpoints (gold-pruned calibration, every reading right): v13a1 epoch 12 (0.238; v12a1 0.236), v13b1
  epoch 12 (0.203; v12b1 0.197).
- Both pods were community L40S cards ($0.79/h); the two runs cost about $4. v13b1's single-stream download stalled
  at 99% and its weights came back with `pull_parts.sh`.
- Two more on the same data with seed 0 (`--experiment readers_v13a` / `readers_v13b`, `--seed 0`, `--minutes 260`,
  `--max-hourly-cost 0.9`): v13a epoch 13 (0.211), v13b epoch 13 (0.200). Not shipped: with all four v13 readers the
  choice is right on 649 of 1,028 calibration questions out of fold, against 668 for v11a, v11b, v13b1 and v13a1
  (v13a for v11a: 662; v13b for v11b: 660). Both single-stream downloads stalled; `pull_parts.sh` brought them back.

### 4e. Stage-2 readers v14a, v14b, v14a1, v14b1 (shipped)

```
python -m training.readers.lease full --experiment readers_v14a1 --data $E/readers_v14/data_v14a --seed 1 --epochs 8 --eval-from 4 --minutes 330 --max-hourly-cost 1.5 --cuda 12.4,12.5,12.6,12.7,12.8,12.9,13.0
python -m training.readers.lease full --experiment readers_v14b1 --data $E/readers_v14/data_v14b --seed 1 (as v14a1)
python -m training.readers.lease full --experiment readers_v14a --data $E/readers_v14/data_v14a --seed 0 (as v14a1)
python -m training.readers.lease full --experiment readers_v14b --data $E/readers_v14/data_v14b --seed 0 (as v14a1)
```

- Selected checkpoints (gold-pruned calibration, every reading right): v14a1 epoch 5 (0.263; v13a1 0.238), v14b1
  epoch 8 (0.235; v13b1 0.203), v14a epoch 8 (0.264), v14b epoch 8 (0.223).
- Serving-faithful calibration candidates (1,028 questions): right answer in a reader's candidates 740 (v14a1), 736
  (v14b1), 737 (v14a), 727 (v14b), against 674 (v13a1) and 658 (v13b1); its first candidate right 579, 561, 574, 545,
  against 536 and 508.
- Hosts with CUDA 13 drivers take the recorded CUDA 12.4 image; without `--cuda ...,13.0` every create failed with
  "no instances" on 2026-10-25. Two single-stream downloads stalled; `pull_parts.sh` brought v14b back.
- A ModernBERT-large arm (`readers_v14m1`, the plain data fitted in its tokens, lr 2e-5) never trained: its loss was
  NaN from the first logged step under the recorded image. XLM-R large stays.
- v11b in slot b instead of v14b: the pool holds 869 right answers and the choice 738 out of fold (against 865
  and 734), but every reader of that pool reads "how many orders not in USD" as the orders in USD, which
  fails the offline regression; the four v14 readers read the negation. All four ship.

### 5. Relayout of v5 and v6 (before the share outputs)

```
python -m training.readers.relayout $E/readers_v5/out/stage2/model.pt $E/readers_v5c/stage2/model.pt
python -m training.readers.relayout $E/readers_v6/out/stage2/model.pt $E/readers_v6c/stage2/model.pt
```

Copy `tokenizer/` and `best.json` beside each converted model, and write `{"anchored": false}` to
`readers_v5c/stage2/reader.json`. If v5 or v6 is retrained with today's trainer, the share heads train directly
(they get no positive labels in that data) and no relayout is needed.

### 6. Calibration rows, outputs and candidates of each stage-2 model

```
python -m training.readers.pruned calibration $E/stage1_v2/stage1 $E/pruned/calibration_anchored.jsonl --readings $L/readings_calibration.jsonl
python -m training.readers.pruned calibration $E/stage1_v2/stage1 $E/pruned/calibration_plain.jsonl --readings $L/readings_calibration.jsonl --no-anchors
python -m training.readers.outputs <run>/stage2 <rows> $E/<v>/cal_outputs_s<i>.jsonl --shard <i>/3     (then concatenate the shards in order)
python -m training.readers.candidates <rows> $E/<v>/cal_outputs.jsonl $E/candidates/<v>_cal.jsonl     (--shard i/4 likewise)
python -m training.readers.union $E/candidates/v5_cal.jsonl $E/candidates/v6_cal.jsonl $E/candidates/v56_cal.jsonl
```

- Anchored readers (v6, v7a) read `calibration_anchored`; the others (v5, v7b) read `calibration_plain`.
- The recorded plain rows (`data_pruned`, sha256 `0a8a37842901`) were built before anchors existed. The anchored
  rows are `data_pruned_v6` (`d268e55f6fcf`).
- `union` reproduces the recorded v5+v6 calibration pool `cand_v56_cal.jsonl` (`c65fcc032016`) byte for byte. That
  pool has a right candidate for 713 of 1,028 questions.

### 7. Cross-fold candidates for the re-scorer

```
python -m training.readers.lease folds --experiment readers_v4_folds_01 --data $E/data_v4 --folds 0,1
python -m training.readers.lease folds --experiment readers_v4_folds_23 --data $E/data_v4 --folds 2,3
python -m training.readers.lease folds --experiment readers_v6_folds_<i> --data $E/data_v6 --folds <i>      (one lease per fold)
python -m training.readers.folds $E/data_v4/stage2.jsonl $E/folds_v4                                       (the pod's fold files, rebuilt locally)
python -m training.readers.candidates $E/folds_v4/fold<i>.jsonl $E/readers_v4_folds_01/out/fold<i>/calibration_outputs.jsonl $E/candidates/v4_fold<i>.jsonl
python -m training.readers.candidates $E/folds_v6/fold<i>.jsonl $E/readers_v6_folds_<i>/out/fold<i>/calibration_outputs.jsonl $E/candidates/v6_fold<i>.jsonl --bird <BIRD>
```

- Fold models train 12 epochs and are evaluated from epoch 9, on 150-minute leases. Only their held-out outputs
  come back.
- `folds.py` is deterministic: on the recorded data it reproduces the recorded fold files byte for byte.
- The v6-recipe fold 0 returned nothing: SECURE had no instance, and the COMMUNITY log ends during training. So only
  folds 1 to 3 exist.
- The re-scorer v2 training file (`rescorer_train_v2.jsonl.gz`, 13,054 questions: 8,232 Spider and 4,822 BIRD) has
  a known make-up, checked against the file, but no recorded command. It holds the v6-recipe fold 1, 2 and 3
  candidates in that order. Then come the v4 fold 0 to 3 candidates for the questions those lack, which are the
  Spider questions of v6 fold 0's databases. The BIRD questions of those databases are absent. To rebuild it:

```python
import json
seen, rows = set(), []
for path in [f"v6_fold{i}.jsonl" for i in (1, 2, 3)] + [f"v4_fold{i}.jsonl" for i in range(4)]:
    for line in open(path, encoding="utf-8"):
        key = tuple(json.loads(line)[k] for k in ("source", "index"))
        if key not in seen:
            seen.add(key); rows.append(line)
open("rescorer_train_v2.jsonl", "w", encoding="utf-8", newline="\n").writelines(rows)
```

Then gzip it, and gzip `v56_cal.jsonl` too, because the lease unzips `*.jsonl.gz`.

### 8. The shipped re-scorer ("f")

The re-scorer trains on pools like the ones serving builds: the union of the two shipped recipes' cross-fold
candidates. Readers of the v7a recipe (anchored) and the v7b recipe (plain) are trained on four folds each; their
held-out candidates are pooled per question, the BIRD share candidates relabelled against 100 times gold
(`rescorer_data.ShareLabels`), and pools whose right answer is a share written five times.

```
python -m training.readers.lease folds --experiment readers_v7a_folds_01 --data $E/data_v7a --folds 0,1 --minutes 230
python -m training.readers.lease folds --experiment readers_v7a_folds_23 --data $E/data_v7a --folds 2,3 --minutes 230
python -m training.readers.lease folds --experiment readers_v7b_folds_01 --data $E/data_v7b --folds 0,1 --minutes 230
python -m training.readers.lease folds --experiment readers_v7b_folds_23 --data $E/data_v7b --folds 2,3 --minutes 230
python -m training.readers.folds $E/data_v7a/stage2.jsonl $E/folds_v7a           (and $E/data_v7b -> $E/folds_v7b)
python -m training.readers.candidates $E/folds_v7a/fold<i>.jsonl $E/readers_v7a_folds_<01|23>/out/fold<i>/calibration_outputs.jsonl $E/fold_candidates/v7a_fold<i>.jsonl --bird <BIRD> --syn <Spider-Syn> --db-dir <spider dbs> --db-dir <train_dbs_extra>
python -m training.readers.rescorer_data $E/rescorer_train_f.jsonl.gz --folds $E/fold_candidates/v7a_fold{0,1,2,3}.jsonl --folds $E/fold_candidates/v7b_fold{0,1,2,3}.jsonl --bird <BIRD> --share-weight 5
python -m training.readers.lease rescorer --experiment rescorer_f --job f=$E/rescorer_train_f.jsonl.gz=$E/candidates/v7a5_cal.jsonl.gz
python -m training.readers.shrink_state expand $E/rescorer_f/out/f/small.pt FacebookAI/xlm-roberta-base $E/rescorer_f/out/f/model.pt
```

- Fold readers: 12 epochs, evaluated from epoch 9; held-out strict metric 0.122 to 0.137 (v7a recipe) and 0.110 to
  0.116 (v7b recipe). The fold files hold 4,444, 4,550, 4,786 and 5,961 held-out questions.
- The training file has 19,741 union pools (12,661 with a right candidate) and 46 pools with a right share written
  five times: 19,925 records.
- The checkpoint is selected on the shipped pair's calibration pool (v7a + v5): epoch 3, 60.9%; it chooses 624 of
  1,028 right there, against 612 for the re-scorer trained on older recipes' pools ("us", the earlier bundle: union
  pools of the v4 and v6 recipes plus the share questions' pools, `rescorer_data --shares`) and 601 for re-scorer v2.
- Recipe: XLM-R base, 3 epochs, 8 questions per step, lr 2e-5 / 1e-3, seed 0.

### 8b. The second shipped re-scorer ("g")

The same recipe as "f" on the v14 recipes' cross-fold candidates. SynSQL trains in every fold and is held out in
none (`folds --train-only synsql`): its databases' execution labels would say little. One fold per lease (RunPod
caps a lease at 360 minutes).

```
python -m training.readers.lease folds --experiment readers_v14a_folds_<i> --data $E/readers_v14/data_v14a --folds <i> --train-only synsql --epochs 6 --eval-from 4 --minutes 300 --max-hourly-cost 1.5 --cuda 12.4,12.5,12.6,12.7,12.8,12.9,13.0   (and data_v14b)
python -m training.readers.folds $E/readers_v14/data_v14a/stage2.jsonl $E/folds_v14a --train-only synsql   (and data_v14b)
python -m training.readers.candidates $E/folds_v14a/fold<i>.jsonl $E/readers_v14a_folds_<i>/out/fold<i>/calibration_outputs.jsonl $E/fold_candidates_v14/v14a_fold<i>.jsonl --bird <BIRD> --syn <Spider-Syn> --reworded ... --reworded-readings ... --lookups $E/readers_v9/lookups --structures ...   (the v14 data's written sets, in its order)
python -m training.readers.rescorer_data $E/rescorer_train_g.jsonl.gz --folds $E/fold_candidates_v14/v14a_fold{0,1,2,3}.jsonl --folds $E/fold_candidates_v14/v14b_fold{0,1,2,3}.jsonl --bird <BIRD> --share-weight 5
python -m training.readers.lease rescorer --experiment rescorer_g --job g=$E/rescorer_train_g.jsonl.gz=$E/pairs/b_14a_11b_14b1_14a1.jsonl.gz
```

- Fold outputs are 300 to 430 MB; three of the eight came back cut by the lease deadline (anchored fold 0: 7,832 of
  8,004 questions; fold 3: 6,758 of 8,841; plain fold 0: 5,771 of 8,004) and are used as they are.
- The training file holds 31,470 union pools (21,855 with a right candidate) and 37 share pools written five times.
- Recipe as "f": epoch 3, chooses 67.2% of its calibration pool right. On the pool of the four v14 readers (1,035
  questions, the right answer in it for 865) the choice is right on 719 out of fold with "g" and 704 with "f"; but
  "g" alone answered three of the product's demo questions wrongly that "f" answers ("What share of the total amount
  comes from Paris?" read as a share of rows, "total commission amount" below the threshold). The two err apart, so
  the bundle keeps both (`rescorer_a` "f", `rescorer_b` "g") and the choice weighs both logits (step 9): 734 out of
  fold on that pool, which is the shipped readers'.
- An XLM-R large re-scorer on the same file ("gl", epoch 2) is not shipped: 699 alone (g: 698) but 708 in the
  choice, and it scores a pool several times slower on CPU.

### 9. The choice

```
python -m training.readers.union $E/candidates/v7a_cal.jsonl $E/candidates/v5c_cal.jsonl $E/candidates/v7b_cal.jsonl $E/candidates/v6c_cal.jsonl $E/candidates/v7ab56_cal.jsonl
python -m training.readers.choice fit $E/candidates/v7ab56_cal.jsonl $E/candidates/v7a_cal.jsonl $E/candidates/v5c_cal.jsonl $E/candidates/v7b_cal.jsonl $E/candidates/v6c_cal.jsonl --rescorer $E/rescorer_f/out/f --out $E/bundle/choice.json
python -m training.readers.choice match $E/bundle/choice.json <engine calibration per-example.json> <readers calibration per-example.json>
```

- The union pools the four readers' calibration candidates as serving does; `fit` weighs each re-scorer's logit
  (`--rescorer` repeated in the bundle's order, one `--logits` cache each) with each candidate's log probability,
  best rank, the readers' agreement and an empty result (out of fold by database:
  642 of 1,028 right against 628 for the re-scorer alone on the same pool, and 624 for the earlier two-reader
  bundle).
- `match` sets the threshold from two serving-faithful `spider.probe.full_eval` runs on calibration: the planner
  being replaced, and the readers with no threshold. The threshold is the lowest at which the readers answer no
  more questions wrongly than that planner did; it is recorded with its counts (`matched`).

### 10. Candidate bundle and promotion

`promote.py` reads one candidate directory, for example `$E/bundle`, laid out like the runtime:

- `stage1/` from the stage-1 run;
- `stage2_<name>/` for each shipped reading model, with a `reader.json` of `{"anchored": true}` or
  `{"anchored": false}`;
- `rescorer_<name>/` for each re-scorer run, in the order the choice was fitted on (`rescorer_a`, `rescorer_b`);
- `choice.json` from step 9.

Each model directory holds `model.pt` next to `config.json`, `tokenizer.json`, `tokenizer_config.json` and
`sentencepiece.bpe.model`. A run keeps those files in `tokenizer/`, so copy them up beside `model.pt`. Some runs
lack files:

- Runs tokenized on the pods lack `sentencepiece.bpe.model`. Copy it from the base model folder
  (`C:/work/datasets/models/xlm-roberta-base`).
- The recorded re-scorer runs saved no `config.json`. Take the base model's.

The shipped bundle (since 2026-10-26): `stage1/` the "v2" stage-1 run; `stage2_a/` reader v14a
(`{"anchored": true}`); `stage2_b/` reader v14b (`{"anchored": false}`); `stage2_c/` reader v14b1
(`{"anchored": false}`); `stage2_d/` reader v14a1 (`{"anchored": true}`); `rescorer_a/` the "f" re-scorer;
`rescorer_b/` the "g" re-scorer; `choice.json` from step 9 fitted on that pool over both re-scorers. It was assembled
from the previous bundle's layout with those models swapped in (`records/scripts/assemble_two_rescorers.py` in the
experiment records). The first bundle was v7a, v5, v7b and v6 with re-scorer "f".

Then, with the owner's approval, install it locally, run the gates, publish exactly the manifest's `readers/` files
to the weight repository, and promote again at the immutable commit that holds them (the promotion downloads every
file at that revision and refuses one whose hash differs):

```
python -m training.readers.promote $E/bundle --local-only
python -m training.readers.promote $E/bundle --revision <immutable-hf-commit>
```

The shipped bundle is published at revision `cd8a03abb8fd443442f9a3e691f717f4285fd263`.

The Spider test set appears in none of these steps. `pruned test` and `choose test` score it in totals only, once
per milestone.
