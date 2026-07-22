# training/ — rebuilding the PreReasoner model

The shipped PreReasoner model is a **schema.org-property consensus model**: ONE LoRA-fine-tuned Qwen2.5-0.5B
encoder + a 10-layer bidirectional **RelBlock** readout reads a fixed set of schema.org *property* dimensions off
each column, and a column's **family** is decoded by consensus (the fraction of a family's distinctive properties
that fire). Nothing is anchored as a "type" — the type emerges from the properties. The serving engine (`engine/`)
loads this model from `engine/data/`.

That property model is rebuilt by the vendored pipeline in **[`training/props/`](props/)** — see
[`training/props/pipeline.md`](props/pipeline.md) for the runnable DAG and command order, and
[`docs/TRAINING.md`](../docs/TRAINING.md) for the conceptual walkthrough + the add-a-type runbook.

The property fine-tune does not start from scratch: it **warm-starts** from a prebuilt **gen20 taxonomy** LoRA +
RelBlock. That older gen20 pipeline still lives under this directory (`train/`, `corpus/`, `anchor/`, `taxonomy/`,
`lib/`, `world/`) but is now **legacy / unmaintained** — it is kept only because its LoRA + RelBlock are the base
the property fine-tune resumes from. See [Legacy: the gen20 taxonomy pipeline](#legacy-the-gen20-taxonomy-pipeline-warm-start-base)
below. Do not treat gen20 as the shipped model.

> **Naming note.** Comments and docstrings under the gen20 subtrees refer to `genN` (gen9 … gen20) — the Nth
> internal research iteration. `gen20` is the warm-start base for the shipped property model, not the shipped
> model itself.

## Do you need to retrain?

**Almost certainly not.** Every artifact the engine serves ships with the release and can simply be downloaded.
These are the property model's artifacts (`engine/data/`):

| artifact | what it is | produced by |
|---|---|---|
| `alloc.json` | the **86**-dim allocation — 9 struct + 67 property + 10 intent (names/families/ids) | `training/props/build_from_props.py` |
| `encoder.pt` | RelBlock readout weights — the **property** fine-tune of the gen20 RelBlock | `training/props/train_props_gpu.py` |
| `encoder_meta.pt` | `{alloc, cfg}` companion the loader reads | `training/props/train_props_gpu.py` |
| `qwen_lora/` | LoRA adapter — the **property** fine-tune of the gen20 LoRA | `training/props/train_props_gpu.py` |
| `families.json` | family → distinctive schema.org props + geo/join tables (8 families) | `training/props/build_families.py` |
| `props_thr.json` | per-property Youden-J firing thresholds | `training/props/calibrate_props.py` |
| `anchor_assignment.npz` | per-dim thresholds for the legacy `/api/dimension` readout (**not** used by the property router) | gen20 `anchor/anchor_head.py` |

Retraining is only needed to (a) verify the paper's numbers from scratch, (b) add or change a type / property dim
(worked example — **software** — in [`docs/TRAINING.md`](../docs/TRAINING.md)), or (c) swap the base model.

## Rebuilding the shipped (property) model

The full runnable pipeline is documented in **[`training/props/pipeline.md`](props/pipeline.md)** (per-stage
consumes/produces, required external inputs, env vars) and **[`docs/TRAINING.md`](../docs/TRAINING.md)** (the
architecture + the add-a-type runbook). In brief, from the repo root:

```bash
python -m training.props.build_assignment_pg       # per-instance schema.org props from Postgres capped.entity
python -m training.props.build_assignment21_v2     # select the property basis (a prop is a dim iff >=25 instances carry it)
python -m training.props.build_from_props          # write the encoder corpus: alloc.json (nc), units_{train,test}.jsonl
cp training/props/data/alloc.json training/props/data/alloc20.json   # the alloc swap the trainer reads
python -m training.props.train_props_gpu --steps 600 --lr 2e-4       # GPU: un-freeze + fine-tune the gen20 LoRA/RelBlock
python -m training.props.build_families            # family-decode table -> engine/data/families.json
python -m training.props.calibrate_props           # per-property Youden-J thresholds -> engine/data/props_thr.json
```

Stage 3 (`train_props_gpu.py`) warm-starts from the prebuilt gen20 LoRA + RelBlock (which ship in `engine/data/`
as `qwen_lora/`, `encoder_meta.pt`, `encoder.pt`); Stage 3's outputs, copied under the engine's names, become the
shipped `encoder.pt` / `encoder_meta.pt` / `qwen_lora/`. See `pipeline.md` for the exact copy step.

## Environment

```
HF_TOKEN            Hugging Face token (Qwen/Qwen2.5-0.5B download) — read from env, never hardcoded
RUNPOD_API_KEY      only for tools/runpod_api.py (GPU pod driver)
WORLD_PG_PASSWORD   Postgres password for capped.entity / capped.entity_type / capped.type (host via WORLD_PG_HOST)
KB_PG_PASSWORD      Postgres password for knowledgebase.human / knowledgebase.taxon (host via KB_PG_HOST)
DEVICE              cuda | cpu (default: cuda when available)
BASE_MODEL_ID       default Qwen/Qwen2.5-0.5B
PREREASONER_TRAIN_DIR    root that holds the props pipeline's data/ (default: training/props/)
PREREASONER_ENGINE_DATA  where build_families / calibrate_props stage their outputs (default: engine/data/)
```

Install: `pip install -r training/requirements.txt` (see the CUDA note inside for GPU training).
Run every command from the repo root, e.g. `python -m training.props.build_from_props`.

## Legacy: the gen20 taxonomy pipeline (warm-start base)

**Unmaintained.** The gen20 subtrees (`train/`, `corpus/`, `anchor/`, `taxonomy/`, `lib/`, `world/`) are the
*previous* generation of the model — a 93-dim **taxonomy** allocation (9 struct + 74 Wikidata-P279 taxonomy + 10
intent) whose type was anchored rather than emergent. They are kept for exactly one reason: they produced the
**base LoRA + RelBlock that the property fine-tune warm-starts from** (`train/train_unified.py` →
`qwen_lora/` + `unified_model.pt`, shipped equivalently as `engine/data/{qwen_lora, encoder.pt, encoder_meta.pt}`).

Because the property fine-tune resumes from those weights, the gen20 warm-start artifacts are a **hard dependency**
of the property build and are retained as prebuilt inputs. The gen20 scripts are otherwise not exercised, not kept
in sync with the shipped model, and **do not reproduce the shipped model** (they produce the taxonomy model, a
different, retired allocation). Treat this section as historical lineage, not a build path.

The gen20 chain, for reference only:

```
 (0) world DB provisioning  [world/ — Wikidata → world.types / world.words]
 (1) corpus + taxonomy discovery  [corpus/ discover_csv_types, cluster_columns; taxonomy/ reconcile, rollup, build_alloc]
 (2) encoder training  [GPU — train/ train_multitask → train_unified → train_taxonomy → qwen_lora + encoder.pt]
 (3) re-anchor + anchor head  [anchor/ reanchor, anchor_head → encoder.pt, anchor_assignment.npz]
 (4) calibrate + gates  [tools/pipeline.py: calibrate_route/dims, validate_data/route]
```

The `train_unified` step is the one whose outputs matter today; the rest of the chain is dormant.

## Hardware, time and cost

- **GPU (Stage 3, `train_props_gpu.py`):** trained on **RunPod** single-GPU pods — RTX 4090 class (pod driver's
  priority list: RTX 4090 / RTX A5000 / L4 / A4000 / RTX 3090 / A4500), image
  `runpod/pytorch:2.4.0-py3.11-cuda12.4.1`, 25 GB container disk, 1 GPU. The 0.5B base with LoRA
  (r=16, ~2M trainable params) + bf16 autocast + gradient checkpointing fits comfortably in 24 GB. ~600 steps is
  a same-day run; at typical RunPod 4090 rates (~$0.35–0.70/hr) a retrain is a **few dollars**. Drive pods with
  `python -m training.tools.runpod_api create|status|term` (reads `RUNPOD_API_KEY` / `HF_TOKEN` from `.env`)
  — **always terminate pods after use**.
- **CPU (Stages 0–2, 4–5):** the Postgres corpus builds, `build_families`, and `calibrate_props` run anywhere.
- **Serving needs no GPU** — the engine runs the encoder on CPU.
