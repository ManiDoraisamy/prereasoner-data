# Readers training

This package trains the models behind the own-data readers in `engine/readers/`:

- the stage-1 schema-item model, which scores tables and columns;
- the stage-2 reading models, which read every fact the deterministic builder needs;
- the re-scorer, which picks among the pooled candidates;
- the refusal fit on the re-scorer's confidence.

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
| `sources.py` | Knows where Spider, BIRD and Spider-Syn questions, schemas and databases are. Reads every database capped at 5,000 rows per table, as the evaluator does. | `--spider-zip`, `--db-dir` | nothing |
| `splits.py` | The database split (seed 20261019: 20 calibration databases of train_spider; the 40 test databases untouched) and stage-1 schema-item labels read from Spider's own parse. | Spider zip | `<dir>/manifest.json`, `labels.jsonl`, `schemas.json` |
| `gold.py` | Gold readings: gold SQL becomes a typed AST, then readings. Checked by the perfect-readings oracle (gold's rows against the AST's rows and the rebuilt query's rows), with printed totals. | split manifest, Spider or BIRD | `readings_{train,calibration,bird}.jsonl` |
| `labels_share.py` | Share readings for BIRD's percentage questions. Each one is checked by execution: gold must equal 100 x built. | BIRD train | `readings_bird_share.jsonl` |
| `labels_syn.py` | Gives Spider-Syn's rewordings their original question's gold readings. Leaves out the calibration databases. | split manifest, Spider-Syn, `readings_train.jsonl` | `readings_syn.jsonl` |
| `examples.py` | Builds stage-1 rows (one per question and table) and stage-2 rows (one per question, gold-pruned like serving), split `train`/`calibration`. `--no-anchors` serializes without the question's values. | readings files, Spider, optional BIRD and Syn | `<dir>/stage1.jsonl`, `<dir>/stage2.jsonl` |
| `train.py` | The trainers. `schema_items` is stage 1; `readings` is stage 2 and keeps `--eval-from` and `--checkpointing`. | `stage1.jsonl` / `stage2.jsonl` | run dir: `model.pt`, `best.json`, `log.jsonl`, `tokenizer/` (with the encoder config), calibration predictions or outputs |
| `outputs.py` | `load_run`; `Stage1` (serving's `schema_items`, `prune` and `row` over a run); `serving_row`; and, as a command, sharded CPU stage-2 outputs for a rows file. | a readings run, rows | outputs `.jsonl` |
| `pruned.py` | Stage-2 rows through serving's stage-1 pruning, for calibration (with labels) or the Spider test set (without). | a stage-1 run, Spider | rows `.jsonl` |
| `relayout.py` | Converts a stage-2 model trained before the share outputs (v5, v6) to the current layout. The new outputs never fire. | `model.pt` | `model.pt` |
| `folds.py` | Splits the training rows into cross-fold files by database (4 folds, seed 20261019). | `stage2.jsonl` | `fold<i>.jsonl` |
| `candidates.py` | Decodes the k best readings per question, builds and runs each, and labels them by execution against gold. | rows, outputs, Spider (+ `--bird`) | candidates `.jsonl` |
| `union.py` | Pools several models' candidates per question, each SQL once, as serving pools. | candidates files | candidates `.jsonl` |
| `rescorer.py` | The re-scorer trainer: listwise loss plus a yes/no term, checkpoint chosen on calibration candidates. | candidates files | run dir |
| `choose.py` | Scores the first, execution-guided and re-scorer choices on calibration candidates. On the Spider test set it reports totals only. | candidates or rows + outputs | stdout |
| `rescorer_data.py` | The re-scorer's training file: union pools of cross-fold candidates, plus the BIRD share questions' pools. | fold and share candidates | `.jsonl.gz` |
| `refusal.py` | Fits the logistic confidence by database; `match` sets its threshold so the readers answer no more wrongly than the planner they replace. | calibration candidates, a re-scorer run; two full_eval runs | `refusal.json` |
| `shrink_state.py` | Shrinks a fine-tuned XLM-R state dict for a slow link, and expands it back. | `model.pt` / `small.pt` | `small.pt` / `model.pt` |
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

### 9. Refusal

```
python -m training.readers.refusal $E/candidates/v7a5_cal.jsonl --rescorer $E/rescorer_f/out/f --target 0.25 --out $E/bundle/refusal.json
python -m training.readers.refusal match $E/bundle/refusal.json <engine calibration per-example.json> <readers calibration per-example.json>
```

- The first fits the confidence model on the shipped pair's calibration candidates (out of fold by database).
- The second sets its threshold from two serving-faithful `spider.probe.full_eval` runs on calibration: the planner
  being replaced, and the readers with the threshold off. The threshold is the lowest at which the readers answer no
  more questions wrongly than that planner did; it is recorded with its counts (`matched`).

### 10. Candidate bundle and promotion

`promote.py` reads one candidate directory, for example `$E/bundle`, laid out like the runtime:

- `stage1/` from the stage-1 run;
- `stage2_<name>/` for each shipped reading model, with a `reader.json` of `{"anchored": true}` or
  `{"anchored": false}`;
- `rescorer/` from the re-scorer run;
- `refusal.json` from step 9.

Each model directory holds `model.pt` next to `config.json`, `tokenizer.json`, `tokenizer_config.json` and
`sentencepiece.bpe.model`. A run keeps those files in `tokenizer/`, so copy them up beside `model.pt`. Some runs
lack files:

- Runs tokenized on the pods lack `sentencepiece.bpe.model`. Copy it from the base model folder
  (`C:/work/datasets/models/xlm-roberta-base`).
- The recorded re-scorer runs saved no `config.json`. Take the base model's.

The shipped bundle: `stage1/` the "v2" stage-1 run; `stage2_a/` reader v7a (`{"anchored": true}`); `stage2_b/`
reader v5 after the relayout (`{"anchored": false}`); `rescorer/` the "f" re-scorer; `refusal.json` from step 9.

Then, with the owner's approval, install it locally, run the gates, publish exactly the manifest's `readers/` files
to the weight repository, and promote again at the immutable commit that holds them (the promotion downloads every
file at that revision and refuses one whose hash differs):

```
python -m training.readers.promote $E/bundle --local-only
python -m training.readers.promote $E/bundle --revision <immutable-hf-commit>
```

The shipped bundle is published at revision `b9bb6521799ca603845a8e41248ff3abfe70fb40`.

The Spider test set appears in none of these steps. `pruned test` and `choose test` score it in totals only, once
per milestone.
