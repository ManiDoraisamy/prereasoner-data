# training/ — reproducing the PreReasoner model

This directory is the **paper-reproduction pipeline** for the shipped PreReasoner model: the LoRA-fine-tuned
Qwen2.5-0.5B unified encoder (`qwen_lora`), the 10-layer bidirectional **RelBlock** readout (`encoder.pt`), the
93 named-dimension allocation (`alloc.json`), and the anchor / threshold / taxonomy configs the serving engine
(`engine/`) loads. It is **self-contained**: everything it imports lives under `training/` (the shared runtime
modules are vendored into `training/lib/`), so it runs without the serving package.

> **Naming note.** Comments and docstrings refer to `genN` (gen9 … gen20) — the Nth internal research iteration
> of the model. The shipped model is **gen20**; earlier generations appear only as warm-start lineage.

## Do you need to retrain?

**Almost certainly not.** Every artifact the engine serves ships with the release and can simply be downloaded:

| artifact | what it is | produced by |
|---|---|---|
| `qwen_lora/` | LoRA adapter — the unified encoder (metric space + anchored readout base) | `train/train_taxonomy.py` |
| `encoder.pt` | RelBlock weights (the readout model) | `anchor/reanchor.py` |
| `encoder_meta.pt` | `{alloc, cfg}` companion the loader reads | `anchor/reanchor.py` |
| `alloc.json` | the 93 named dims (9 struct + 74 taxonomy + 10 intent) | `corpus/build_from_entity.py` |
| `taxonomy.csv` | the Wikidata P279 taxonomy (nodes, status, world tables) | taxonomy chain (below) |
| `anchor_assignment.npz` | ridge head `W`/`thr`/`dims` (base per-dim thresholds) | `anchor/anchor_head.py` |
| `route_thresholds.json`, `dim_thresholds.json` | calibrated serving thresholds | `calibrate/*` |
| `assignment.csv`, `inference.csv`, `units_{train,test}.jsonl` | training/review corpus (entity-disjoint split) | `corpus/build_from_entity.py` |
| `world.words` pgvector index / `words.db` | bge-small entity-resolution embeddings | `world/` provisioning |

Retraining is only needed to (a) verify the paper's numbers from scratch, (b) change the taxonomy / dims, or
(c) swap the base model.

## Environment

```
HF_TOKEN            Hugging Face token (Qwen/Qwen2.5-0.5B download) — read from env, never hardcoded
RUNPOD_API_KEY      only for tools/runpod_api.py (GPU pod driver)
KB_PG_HOST       Postgres world DB host (default localhost; a /path is treated as a Cloud SQL unix socket)
KB_PG_PORT       default 5432
KB_PG_DB         default world
KB_PG_USER       default postgres
KB_PG_PASSWORD   required for any DB step (corpus build, world provisioning, route grounding)
KB_PG_SSLMODE    default require (use disable for a local dev Postgres)
DEVICE              cuda | cpu (default: cuda when available)
BASE_MODEL_ID       default Qwen/Qwen2.5-0.5B
CSV_CORPUS_DIR      raw CSV corpus for the discovery/clustering phase (default training/data/csv_corpus)
WIKIMEDIA_CONTACT   contact string for the Wikidata API User-Agent
```

Install: `pip install -r training/requirements.txt` (see the CUDA note inside for GPU training).
Run every command below **from the repo root**, e.g. `python -m training.train.train_taxonomy --smoke`.
All artifacts read/write `training/data/`.

## Pipeline

```
                 (0) WORLD DB PROVISIONING  [training/world/ — one-time, feeds both training + serving]
  Wikidata ──► fetch_properties ─► sync_wikidata_world / mirror_world_schema ─► build_wikipedia_schema
           └─► sync_world_types ─► unify_words_qid          (world.types / world.words, qid-keyed)
                       │
                       ▼
 (1) CORPUS + TAXONOMY DISCOVERY                    (2) TAXONOMY BUILD
  discover_csv_types (CSV corpus → value P31)        reconcile_taxonomy (clusters+renames → taxonomy.csv)
  cluster_columns    (bge + MiniBatchKMeans)   ──►   rollup_taxonomy    (roll up over-specific leaves)
  split_for_rename   (LLM renamer work-chunks)       build_alloc        (taxonomy → alloc dims)
  cluster_coherence  (garbage-bucket gate)           coverage_list      (audit: unrepresented clusters)
                       │
                       ▼
 (3) TRAINING CORPUS                                 capped.entity = a per-leaf-capped sample of the full
  build_from_entity  (capped.entity → assignment/     Wikidata dump loaded by a separate Cloud Run job
   inference/units_{train,test}.jsonl/alloc.json)     (not in this repo — its output CSVs ship instead)
  build_corpus / build_review / fetch_type_instances  (earlier-generation corpus, kept for train_taxonomy)
                       │
                       ▼
 (4) ENCODER TRAINING  [GPU]
  train_multitask  (frozen Qwen + RelBlock; CSV+SQL+JOIN joint anchoring)      → multitask_model.pt
  train_unified    (un-freeze Qwen via LoRA; InfoNCE altLabels + MSE anchor)   → unified_model.pt + LoRA
  train_taxonomy   (same recipe re-anchored to the taxonomy dims)              → qwen_lora + encoder.pt
                       │
                       ▼
 (5) READOUT RE-ANCHOR + ANCHOR HEAD  [CPU-bounded]
  reanchor     (freeze encoder; retrain RelBlock readout on clean capped.entity units) → encoder.pt
  anchor_head  (ridge-probe the named dims; write anchor_assignment.npz + inference.csv PASS)
                       │
                       ▼
 (6) CALIBRATION + GATES
  calibrate_route / calibrate_dims  (Youden-J thresholds on the trained model's own readout)
  validate_data  (data invariants gate)   validate_route  (served-model demo-distribution gate)
                       │
                       ▼
 (7) PACKAGE → engine/data/
  tools/pipeline.py runs (5b)+(6) transactionally; copy the artifacts into engine/data/ to ship.
```

### How to run each phase

| # | phase | command(s) | needs | output (training/data/) |
|---|---|---|---|---|
| 0 | world DB | `python -m training.world.fetch_properties` → `sync_wikidata_world --qid Q6256 --label country` → `mirror_world_schema` → `build_wikipedia_schema` → `sync_world_types` → `unify_words_qid` | Postgres, WDQS | `world.*` / `wikipedia.*` schemas, `properties.csv` |
| 1 | discovery | `python -m training.corpus.discover_csv_types` → `cluster_columns` → `split_for_rename` → (LLM renames the cluster chunks → `cluster_renames.json`) → `cluster_coherence` | CSV corpus (`CSV_CORPUS_DIR`) | `discovered_types.json`, `columns.csv`, `clusters.json` |
| 2 | taxonomy | `python -m training.taxonomy.reconcile_taxonomy` → `rollup_taxonomy` → `build_alloc` (audit: `coverage_list`) | WDQS | `taxonomy.csv`, `mapped_columns.json`, `alloc.json` |
| 3 | corpus | `python -m training.corpus.build_from_entity` | `capped` schema in Postgres | `assignment.csv`, `inference.csv`, `units_{train,test}.jsonl`, `alloc.json` |
| 4 | encoder | `python -m training.train.train_multitask --steps 1200` → `train_unified --steps 1500 --lam 1.0` → `train_taxonomy --steps 1500 --lam 1.0` (each has a `--smoke`/CPU mode) | GPU, HF_TOKEN, historical corpora (see caveat) | `multitask_model.pt`, `unified_model.pt`, `qwen_lora/`, `encoder.pt` |
| 5 | re-anchor | `python -m training.anchor.reanchor --steps 1500` then `python -m training.anchor.anchor_head` | CPU ok | `encoder.pt`, `encoder_meta.pt`, `anchor_assignment.npz` |
| 6 | calibrate + gates | `python -m training.tools.pipeline` (runs anchor_head → calibrate_route → calibrate_dims → validate_data → validate_route with snapshot/rollback) | Postgres for the route gates | `route_thresholds.json`, `dim_thresholds.json`, `route_eval.json` |
| 7 | package | copy artifacts into `engine/data/` (table above) | — | — |

**Caveat on phase 4 (from-scratch encoder training).** The warm-start chain is
`sql_base_model.pt (gen10)` → `train_multitask` → `train_unified` → `train_taxonomy`, and it consumes historical
corpora (`unit_emb.npy`, `sql_graphs_*.jsonl`, `join_graphs_*.jsonl`, `altlabel_pairs.jsonl`) that ship as data
artifacts, not as scripts. Reproducing the shipped model **bit-for-bit from nothing** therefore isn't possible from
this directory alone — start from the shipped `qwen_lora` + corpora and run phases 5–6, which is exactly how the
final shipped model was produced (`build_from_entity → reanchor → anchor_head → calibrate → validate`).

## Hardware, time and cost

- **GPU phases (4):** trained on **RunPod** single-GPU pods — RTX 4090 class (the pod driver's priority list:
  RTX 4090 / RTX A5000 / L4 / A4000 / RTX 3090 / A4500), image `runpod/pytorch:2.4.0-py3.11-cuda12.4.1`,
  25 GB container disk, 1 GPU. The 0.5B base with LoRA (r=16, ~2M trainable params) + bf16 autocast +
  gradient checkpointing fits comfortably in 24 GB. 1,500 joint steps is a same-day run; at typical RunPod
  4090 rates (~$0.35–0.70/hr) a full retrain is a **few dollars**. Drive pods with
  `python -m training.tools.runpod_api create|status|term` (reads `RUNPOD_API_KEY` / `HF_TOKEN` from `.env`)
  — **always terminate pods after use**.
- **CPU phases (0–3, 5–6):** run anywhere; `reanchor` encodes each unit text once (disk-cached) then trains
  only the RelBlock, so it completes on a laptop CPU. The full-corpus clustering (phase 1 at 100k CSVs) is an
  overnight CPU job or minutes on a GPU.
- **Serving needs no GPU** — the engine runs the encoder on CPU.

## Gates (what "done" means)

`validate_data` (exit non-zero on: contradictory targets, train/test leaks, duplicate rows, un-trained taxonomy
leaves, dim/alloc mismatch) and `validate_route` (the served model must route famous city/country/state columns to
the right world leaf and clear non-geo columns to None) must both pass. `tools/pipeline.py` snapshots
`training/data` and rolls back if any step fails, so the artifact set is never left half-written.
