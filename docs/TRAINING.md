# Training the typing model — and how to add a new type

This document explains how the schema.org-**property** typing model is trained, and gives a validated,
step-by-step runbook for adding a new entity type (worked example: **software / SoftwareApplication**).

> Status: the property pipeline is **vendored** into this repo at [`training/props/`](../training/props/) — it
> rebuilds the shipped model standalone. [`training/props/pipeline.md`](../training/props/pipeline.md) is the
> operational contract (per-stage consumes/produces, external inputs, exact command order); this doc is the
> conceptual walkthrough + the add-a-type runbook. The encoder-train step (Stage 3) needs a GPU.

## What the model does (the architecture, in one paragraph)

The router (`engine/router.py`) does **superposition-decode**: ONE trained encoder (Qwen-0.5B + a
`RelationalModel` readout) reads a fixed set of **schema.org property dimensions** off each column, and the
column's **family** is decoded by *consensus* — the fraction of a family's DISTINCTIVE properties that fire,
calibrated by per-property Youden-J thresholds. Nothing is anchored as a "type"; the type **emerges** from the
properties. A column that fires no family's distinctive props (a literal — amount/id/status) **abstains**.
The shipped model has **9 families** (film, music, org, organism, person, place, product, publication,
software) over a **71-property / 90-content-dim** basis: `alloc.json` is **nc=90 = 9 struct + 71 property +
10 intent**. (The worked software example below is the add-a-type run that grew the basis from the earlier
8-family / 67-property / nc=86 state to this one.)

## The artifacts the engine loads (`engine/data/`)

| Artifact | What it is | Produced by |
|---|---|---|
| `alloc.json` | the dim allocation (names/families/ids), nc=90 (9 struct + 71 property + 10 intent) | `training/props/build_from_props.py` |
| `encoder.pt` | RelBlock readout state_dict — the property fine-tune of the gen20 RelBlock | `training/props/train_props_gpu.py` |
| `encoder_meta.pt` | `{alloc, cfg}` (constructor config) | `training/props/train_props_gpu.py` |
| `qwen_lora/` | the fine-tuned LoRA adapter — the property fine-tune of the gen20 LoRA | `training/props/train_props_gpu.py` |
| `props_thr.json` | per-property Youden-J firing thresholds | `training/props/calibrate_props.py` |
| `families.json` | family → distinctive props + join tables | `training/props/build_families.py` |
| `anchor_assignment.npz` | per-dim thresholds for the legacy `/api/dimension` readout (**not** used by the property router) | gen20 `anchor/anchor_head.py` |

> On-disk detail: in `alloc.json` the 71 property dims carry the internal `family` tag `"taxonomy"` — a
> warm-start/harness-compatibility label inherited from the gen20 lineage (`build_from_props.py` keeps it so the
> trainer runs unchanged; `build_families.py` selects the props via `family == "taxonomy"`). The router keys on the
> property **name**, not this tag, so the literal on-disk label differs harmlessly from the "property" term used here.

## The pipeline (DAG)

The runnable, parameterized pipeline lives at [`training/props/`](../training/props/); see
[`training/props/pipeline.md`](../training/props/pipeline.md) for the full per-stage contract. In outline:

```
build_assignment_pg.py    ─► data/pg_per_instance.jsonl   (per-instance props from Postgres capped.entity)
build_assignment21_v2.py  ─► data/alloc21_dims.json       (◄ THE property basis is selected here: a prop
                                                            becomes a dim iff ≥25 training instances carry it)
build_from_props.py       ─► data/{alloc.json (nc), units_{train,test}.jsonl, assignment.csv, inference.csv}
   ── cp data/alloc.json data/alloc20.json  (the trainer reads alloc20.json) ──
train_props_gpu.py  (GPU) ─► data/{encoder_props.pt, encoder_props_meta.pt, qwen_lora_props/}   (un-freezes + fine-tunes the gen20 LoRA)
build_families.py  ─► data/families.json + engine/data/families.json
calibrate_props.py ─► data/props_thr.json + engine/data/props_thr.json
```

## Where the property basis is selected

`training/props/build_assignment21_v2.py` (`PER_TYPE, MIN_SUPPORT = 250, 25`; dim-selection ~L130–132): a
schema.org prop becomes a model dim **iff ≥25 training instances carry it** (`MIN_SUPPORT=25`). Instances (with
their properties) come from Postgres `capped.entity`, mapped Wikidata-P-id → schema.org name via `bridge_prop.csv`
(committed at [`training/props/bridge_prop.csv`](../training/props/bridge_prop.csv)). **Which types are pulled** is
controlled by the `TYPES` map (`build_assignment21_v2.py:41–46`, Wikidata qid → coarse family).

## Runbook: add a new type (worked example — software) — VALIDATED on CPU

> Steps 1–4 below have been **run and validated** (see `training/props/README.md` for the evidence). The basis
> grew **67 → 71 props**, the corpus regenerated at **nc=90**, and software came out with a distinctive property
> **`operatingSystem` (0.27 in-family vs 0.00 out)** — so consensus decoding will type it. Only the GPU
> encoder-train (step 5) remains.

**Validated preconditions (checked live):**
- The software type is Wikidata **`Q7397` ("software"), 13,958 instances** — **not** `Q166142` (which has 0
  instances in `capped.entity`). Its instances carry distinctive props `P306` operatingSystem, `P277`
  programmingLanguage, `P348` softwareVersion, `P178` author, `P400` runtimePlatform. Of these,
  `operatingSystem/softwareVersion/programmingLanguage/author` clear `MIN_SUPPORT=25` and enter the basis.

**Blockers to running it:**
1. **GPU** — `train_props_gpu.py` back-props through Qwen-0.5B (~600 steps). CPU is a multi-hour job; use a
   GPU box (e.g. RunPod). Every other step is CPU/Postgres and is **done**.
2. **Two Postgres DBs**: `WORLD_PG_PASSWORD` for `capped.entity`, `KB_PG_PASSWORD` for `knowledgebase.*` (env).

**Steps (1–4 done, 5–8 remaining):**
1. ✅ Code edits: added `"Q7397": "software"` to `TYPES` (`training/props/build_assignment21_v2.py`); the 5
   software P-ids are mapped in `training/props/bridge_prop.csv` (`P306`→operatingSystem, `P277`→programmingLanguage,
   `P348`→softwareVersion, `P178`→author, `P400`→runtimePlatform). Also done: software is its own family in
   `training/props/build_families.py` (`TYPE_FAM["software"]="software"` +
   `FAM_SCHEMATYPES["software"]=["SoftwareApplication"]`, moved out of `product`).
2. ✅ `python -m training.props.build_assignment_pg` → `python -m training.props.build_assignment21_v2` — basis
   grew **67 → 71** props; the software props appear in `data/alloc21_dims.json`.
3. ✅ `python -m training.props.build_from_props` — corpus written to `data/units_{train,test}.jsonl`
   (**nc=90** = 9 struct + 71 prop + 10 intent, ~1.1 MB).
4. ✅ `cp training/props/data/alloc.json training/props/data/alloc20.json` (the trainer reads `alloc20.json`).
5. **(GPU — REMAINING)** `python -m training.props.augment_intent` (anchors the serving intent phrasings +
   writes the held-out intent eval), then `python -m training.props.train_props_gpu --steps 600 --lr 2e-4`.
   The trainer's keep-best selects on **property AUC + held-out intent op-accuracy** (a
   `read_op_model` mirror) — the first run selected on property AUC alone and regressed COUNT intent at
   serving; see `training/props/pipeline.md` § "The intent guard". Gate the checkpoint with
   `python -m training.props.eval_intent --ckpt props` — intent op-accuracy must be **≥ the 0.808
   engine baseline** AND the `howmany_customers_france` probe must be OK.
6. `python -m training.props.build_families` + `python -m training.props.calibrate_props` (both stage their
   outputs into `engine/data/`; override with `PREREASONER_ENGINE_DATA`).
7. Copy the GPU-train (`train_props_gpu`) outputs into `engine/data/` under the engine's names:
   `encoder_props.pt`→`encoder.pt`, `encoder_props_meta.pt`→`encoder_meta.pt`, `qwen_lora_props/*`→`qwen_lora/`,
   `data/alloc.json`→`alloc.json`. (`families.json` + `props_thr.json` are written to `engine/data/` by step 6.)
   Do **not** touch `anchor_assignment.npz` (legacy, unused by the router).
8. Verify: `python -m tests.test_world` + `python -m tests.test_geo` — the software assertion (`test_world.py:50`)
   must flip from `None` to a routed family, with no regression on the other assertions — **and**
   `python -m tests.test_route_wired` (the COUNT world-join aggregate "how many customers in France" must still
   answer, i.e. return the French-customer count; the first run passed test_world/test_geo but broke exactly this).

## Independence — what is vendored, what stays external

The property pipeline **is vendored** into [`training/props/`](../training/props/) — the repo builds the shipped
model standalone (`build_assignment_pg`, `build_assignment21_v2`, `build_from_props`, `train_props_gpu`,
`build_families`, `calibrate_props`, plus `bridge_prop.csv`). All the old hardcoded `runtime20`/`runtime21` paths
were collapsed onto one `training/props/data/` dir (`PREREASONER_TRAIN_DIR`); `build_families` and
`calibrate_props` write to `engine/data/` (`PREREASONER_ENGINE_DATA`). The remaining external inputs — not
committed because they are large and/or upstream-owned — are:
- **`columns.csv`** (Stage 1) and **`type_table_map.csv`** (Stage 4), dropped into `training/props/data/`.
- The **two Postgres DBs**: `world` (`capped.entity` etc.) and `knowledgebase` (`human`, `taxon`).
- The **gen20 warm-start artifacts** (Stage 3 resumes from them): the base LoRA + RelBlock, which ship in
  `engine/data/` as `qwen_lora/`, `encoder.pt`, `encoder_meta.pt`. This is the one hard dependency on the legacy
  gen20 pipeline — kept as a prebuilt input, not rebuilt here.
- **HF weights** — `Qwen/Qwen2.5-0.5B` (downloaded by `transformers`; `HF_TOKEN` if gated).

See [`training/props/pipeline.md`](../training/props/pipeline.md) for the full external-input list and env vars.
