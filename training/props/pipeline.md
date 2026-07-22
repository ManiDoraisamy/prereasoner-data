# training/props — the schema.org-property pipeline (the vendored DAG)

This is the **runnable** vendored home of the property-typing pipeline that rebuilds the shipped
PROPERTY-consensus model in `engine/data/` (`alloc.json`, `families.json`, `props_thr.json`, `encoder.pt`,
`qwen_lora/`). For the **conceptual** explanation — what the model does, why the type emerges from properties,
how the basis is selected — see [`docs/TRAINING.md`](../../docs/TRAINING.md). This file is the operational
contract: what each stage consumes/produces, the required external inputs, the env vars, and the exact command
order.

> The five stages are **not run here** (GPU + Postgres needed). Nothing below is executed at build time.

## The DAG

```
                          bridge_prop.csv (here, 181 mappings)   Postgres capped.entity (WORLD_PG_PASSWORD)
                                   │                                   │
              ┌────────────────────┴───────────┐         ┌────────────┴─────────────┐
              ▼                                ▼         ▼                          ▼
   [upstream] build_assignment_pg.py     (1) build_assignment21_v2.py
              │                                │   reads data/columns.csv (external input, see below)
              ▼                                ▼
   data/pg_per_instance.jsonl          data/alloc21_dims.json  (+ assignment21.csv, inference21.csv, report)
              │                                │
              └───────────────┬────────────────┘
                              ▼
                  (2) build_from_props.py   ── Postgres knowledgebase.{human,taxon} (KB_PG_PASSWORD)
                              │   also reads base gen20 corpus in data/: assignment.csv, inference.csv, alloc.json
                              ▼
        data/{alloc.json (nc), units_train.jsonl, units_test.jsonl, assignment.csv, inference.csv}
                              │
                     (cp data/alloc.json data/alloc20.json)   ← the alloc swap the trainer needs
                              │
       sql_graphs_train.jsonl │ join_graphs_train.jsonl
                    └──► (2c) augment_intent.py ──► data/intent_aug_train.jsonl (→ train pool)
                              │                      data/intent_eval.jsonl      (→ held-out selection, never trained)
                              ▼
                  (3) train_props_gpu.py  (GPU)  ── warm-starts from the base gen20 LoRA + RelBlock in data/
                              │   keep-best selects on property AUC + held-out intent op-accuracy (eval_intent.py)
                              │                       (qwen_lora/, unified_meta.pt, unified_model.pt) + HF weights
                              ▼
        data/{encoder_props.pt, encoder_props_meta.pt, qwen_lora_props/}
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
   (4) build_families.py            (5) calibrate_props.py
       reads data/{alloc.json,           reads data/{encoder_props*.pt, qwen_lora_props/, units_test.jsonl}
                 assignment.csv,
                 type_table_map.csv}
              │                               │
              ▼                               ▼
   data/families.json  +  engine/data/families.json     data/props_thr.json  +  engine/data/props_thr.json
```

Everything reads and writes a **single** `training/props/data/` directory (the two flat-data runtimes shared a
data dir via cross-references; here that is collapsed to one). Override it with `PREREASONER_TRAIN_DIR`.

## Per-stage consumes / produces

| # | script | consumes | produces |
|---|--------|----------|----------|
| — | `build_assignment_pg.py` | `bridge_prop.csv`, Postgres `capped.entity` | `data/pg_per_instance.jsonl` (per-instance schema.org props) |
| 1 | `build_assignment21_v2.py` | `bridge_prop.csv`, `data/columns.csv`, Postgres `capped.entity` | `data/alloc21_dims.json` (**the property basis** — a prop is a dim iff ≥25 instances carry it), `assignment21.csv`, `inference21.csv`, `assignment21_report.json` |
| 2 | `build_from_props.py` | `data/alloc21_dims.json`, `data/pg_per_instance.jsonl`, base gen20 `data/{assignment.csv, inference.csv, alloc.json}`, Postgres `knowledgebase.{human,taxon}` | `data/alloc.json` (nc), `units_train.jsonl`, `units_test.jsonl`, `assignment.csv`, `inference.csv` (base corpus backed up to `*.taxbak` on first run) |
| 2c | `augment_intent.py` | `data/{sql_graphs_train,join_graphs_train}.jsonl` | `data/intent_aug_train.jsonl` (anchors the SERVING phrasings: "how many"/"number of"→COUNT, "sum of"/"how much"→SUM, "in ‹place›"→NONE), `data/intent_eval.jsonl` (hash-held-out variants + serving probes — **never trained on**) |
| 3 | `train_props_gpu.py` (GPU) | `data/alloc20.json`, base LoRA+RelBlock `data/{qwen_lora/, unified_meta.pt, unified_model.pt}`, `units_{train,test}.jsonl`, `sql_graphs_train.jsonl`, `join_graphs_train.jsonl`, `intent_aug_train.jsonl` (pool), `intent_eval.jsonl` (selection), HF Qwen2.5-0.5B | `data/encoder_props.pt`, `encoder_props_meta.pt`, `qwen_lora_props/` — keep-best selects on **property AUC + held-out intent op-accuracy** (`eval_intent.intent_metrics`, a `read_op_model` mirror) |
| 4 | `build_families.py` | `data/alloc.json`, `data/assignment.csv`, `data/type_table_map.csv` | `data/families.json`, `engine/data/families.json` |
| 5 | `calibrate_props.py` | `data/{encoder_props_meta.pt, encoder_props.pt, qwen_lora_props/, units_test.jsonl}`, HF Qwen2.5-0.5B | `data/props_thr.json`, `engine/data/props_thr.json` |

The engine-shipped `encoder.pt` / `encoder_meta.pt` / `qwen_lora/` are the Stage-3 outputs
`encoder_props.pt` / `encoder_props_meta.pt` / `qwen_lora_props/` copied into `engine/data/` under the engine's
names (final deploy step; see `docs/TRAINING.md`).

## Model / harness code — reused, not re-vendored

The property trainer + calibrator import the gen20 encoder, model, and training harness rather than duplicating
them (they are byte-equivalent to flat-data's `runtime20/*`):

- `training.lib.encoder.LiveQwen`   (= flat-data `runtime20/encoder19.py`)
- `training.lib.relblock.RelBlockModel`   (= flat-data `runtime20/model11.py` `Runtime11Model`)
- `training.lib.edges.fam_dims_map`   (= flat-data `runtime20/edges11.py`)
- `training.train.train_unified.{pack, pack_csv, collate_live, evaluate}`   (= flat-data `runtime20/train17.py`)
- `training.train.train_multitask.{load, fam_report}`   (= flat-data `runtime20/train11.py`)
- `training.lib.walker.build_from_units` (via `pack_csv`)   (= flat-data `runtime20/walker7.py`)

No distinct property model class was vendored — the property fine-tune only differs in the corpus, the LoRA
un-freeze, and the held-out keep-best loop, all of which live in `train_props_gpu.py` itself.

## The intent guard (why stages 2c + the combined keep-best exist)

The engine reads the aggregate operator off the encoder's **intent dims** (`engine/encoder_overlay.read_op_model`:
max over candidate question tokens — operand tokens excluded — gates COUNT 0.05 / SUM 0.30 / AVG 0.30, plus a
dominance arm). The base corpus anchors each op with exactly ONE cue token (`count`/`total`/`average`), and no
aggregate cue ever co-occurs with an "in ‹place›" filter — serving phrasings like "how many … in France" were
**emergent**, not trained. The first fine-tune run selected checkpoints on property AUC alone (its test set
has zero intent examples): property AUC hit 0.938 but the emergent intent behavior drifted — SUM (0.121) edged
COUNT (0.110) on "how many customers in France" and the None-class collapsed (0.769 → 0.138 held-out accuracy;
overall op-accuracy 0.808 → 0.697), so serving lost COUNT aggregates and gained spurious fires. Note the RelBlock readout is `h[:, :, -nc:]` (END-aligned channels): the 10 intent
dims land on the same channels across the nc=86→90 change, so they warm-start intact and are lost only to
*drift under weak anchoring* — exactly what 2c (anchoring) + the combined keep-best (selection) prevent.
`eval_intent.py` mirrors `read_op_model`'s gates + dominance arm + operand exclusion (deliberately without the
n-gram-span machinery, which `read_op_model` itself does not use — that lives in `primitive_head.py`): on the
drifted checkpoint it reproduces the live failure scores to three decimals (COUNT 0.110 vs SUM 0.121).

## Required external inputs (place in `training/props/data/` before running)

These are large and/or upstream-owned, so they are **not** committed here:

- **`columns.csv`** (Stage 1) — the sampled CSV-column corpus (`name,n_columns,sample_values`). Produced by the
  gen20 column-clustering step; drop it into `data/columns.csv`.
- **`type_table_map.csv`** (Stage 4) — `schema_type,wikidata_qid,world_table,ambiguous`; the family→knowledgebase
  join-table map. Drop it into `data/type_table_map.csv`.
- **Base gen20 corpus** (Stage 2/3): `assignment.csv`, `inference.csv`, `alloc.json`, `sql_graphs_train.jsonl`,
  `join_graphs_train.jsonl`, `units_train.jsonl` — the taxonomy-model corpus the property re-anchor forks from.
- **Base gen20 warm-start** (Stage 3): `qwen_lora/`, `unified_meta.pt`, `unified_model.pt` — the prebuilt gen20
  LoRA + RelBlock the property fine-tune resumes from. These are the outputs of `training.train.train_unified`
  (shipped equivalently as `engine/data/{qwen_lora, encoder_meta.pt, encoder.pt}`). **This is the one hard
  dependency on the legacy gen20 pipeline: the property trainer cannot warm-start without it.** If retiring the
  gen20 docs, keep these artifacts available as prebuilt warm-start inputs. (`alloc20.json` is **not** a gen20
  input — Stage 2b creates it by copying the freshly built `data/alloc.json`.)
- **Postgres** — a `world` DB with `capped.entity`/`capped.entity_type`/`capped.type` (Stages 0/1) and
  `knowledgebase.human`/`knowledgebase.taxon` (Stage 2). Auth via `WORLD_PG_PASSWORD` / `KB_PG_PASSWORD`.
- **HF weights** — `Qwen/Qwen2.5-0.5B`, downloaded by `transformers` at train/calibrate time (`HF_TOKEN` if gated).

## Env vars

| var | used by | meaning |
|-----|---------|---------|
| `PREREASONER_TRAIN_DIR` | all | root that holds `data/` (default: `training/props/`) |
| `PREREASONER_ENGINE_DATA` | Stages 4, 5 | where to stage `families.json` / `props_thr.json` (default: `engine/data/`) |
| `WORLD_PG_PASSWORD` | `build_assignment_pg`, Stage 1 | Postgres password for `capped.*` (host via `WORLD_PG_HOST`) |
| `KB_PG_PASSWORD` | Stage 2 | Postgres password for `knowledgebase.*` (host via `KB_PG_HOST`) |
| `HF_TOKEN` | Stages 3, 5 | Hugging Face token (only if the Qwen weights are gated) |

## Command order (run from the repo root)

```bash
# 0. per-instance schema.org targets from Postgres  (needs WORLD_PG_PASSWORD)
python -m training.props.build_assignment_pg

# 1. select the property basis (≥25-instance floor) + assignment/inference tables  (needs WORLD_PG_PASSWORD, data/columns.csv)
python -m training.props.build_assignment21_v2

# 2. write the encoder corpus: alloc.json (nc), units_{train,test}.jsonl, assignment.csv  (needs KB_PG_PASSWORD)
python -m training.props.build_from_props

# 2b. the alloc swap the trainer reads
cp training/props/data/alloc.json training/props/data/alloc20.json

# 2c. intent augmentation + the held-out intent eval (anchors "how many"/"in <place>" etc.;
#     without it, keep-best falls back to property-AUC-only and CAN ship drifted intent — the
#     failure mode of the first fine-tune run)
python -m training.props.augment_intent

# 3. GPU: un-freeze qwen_lora + MSE-anchor the property corpus  (needs a GPU, base warm-start in data/, HF weights)
python -m training.props.train_props_gpu --steps 600 --lr 2e-4

#    (verify any checkpoint against the serving-mirror intent eval at any time:)
python -m training.props.eval_intent --ckpt props     # the fresh checkpoint
python -m training.props.eval_intent --ckpt engine    # the shipped model, as the baseline

# 4. family-decode table  (→ data/ + engine/data/)
python -m training.props.build_families

# 5. per-property Youden-J thresholds  (→ data/ + engine/data/)
python -m training.props.calibrate_props
```

Final deploy (copy Stage-3 outputs under the engine's names, then run the engine tests) is described in
`docs/TRAINING.md`.
