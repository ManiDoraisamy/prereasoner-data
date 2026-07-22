# Training the typing model — and how to add a new type

This document explains how the schema.org-**property** typing model is trained, and gives a validated,
step-by-step runbook for adding a new entity type (worked example: **software / SoftwareApplication**).

> Status: the training pipeline currently lives in the private `prereasoner-flat-data` repo
> (`runtime21/` orchestration + `runtime20/` model/trainer). **Vendoring it into this repo is a tracked
> open-source-independence task** — see [Independence](#independence--what-must-be-vendored). The runbook
> below is validated against the real pipeline; the encoder-train step needs a GPU.

## What the model does (the architecture, in one paragraph)

The router (`engine/router.py`) does **superposition-decode**: ONE trained encoder (Qwen-0.5B + a
`RelationalModel` readout) reads a fixed set of **schema.org property dimensions** off each column, and the
column's **family** is decoded by *consensus* — the fraction of a family's DISTINCTIVE properties that fire,
calibrated by per-property Youden-J thresholds. Nothing is anchored as a "type"; the type **emerges** from the
properties. A column that fires no family's distinctive props (a literal — amount/id/status) **abstains**.
There are currently **8 families** (film, music, org, organism, person, place, product, publication) over a
**67-property / 86-content-dim** basis.

## The artifacts the engine loads (`engine/data/`)

| Artifact | What it is | Produced by |
|---|---|---|
| `alloc.json` | the dim allocation (names/families/ids), nc=86 | `runtime21/build_from_props.py` |
| `encoder.pt` | `RelationalModel` state_dict | `runtime20/scripts/train_props_gpu.py` |
| `encoder_meta.pt` | `{alloc, cfg}` (constructor config) | `train_props_gpu.py` |
| `qwen_lora/` | the fine-tuned LoRA adapter | `train_props_gpu.py` |
| `props_thr.json` | per-property Youden-J firing thresholds | `runtime21/calibrate_props.py` |
| `families.json` | family → distinctive props + join tables | `runtime21/build_families.py` |
| `anchor_assignment.npz` | per-dim thresholds for the `/api/dimension` readout (legacy; **not** used by the property router) | `runtime20/scripts/anchor_assignment.py` |

## The pipeline (DAG)

```
build_type_property_matrix.py ─► type_property_matrix.csv   (schema.org candidate props)
build_assignment_pg.py        ─► pg_per_instance.jsonl      (per-instance props from Postgres capped.entity)
build_assignment21_v2.py      ─► alloc21_dims.json          (◄ THE 67-prop basis is selected here: a prop
                                                              becomes a dim iff ≥25 training instances carry it)
build_from_props.py           ─► runtime20/data/{alloc.json (nc), units_*.jsonl, assignment.csv}
   ── cp alloc.json → alloc20.json  (the trainer reads alloc20.json) ──
train_props_gpu.py  (GPU)     ─► encoder_props{,_meta}.pt, qwen_lora_props/   (un-freezes + fine-tunes the LoRA)
calibrate_props.py            ─► props_thr.json      build_families.py ─► families.json
```

## Where the property basis is selected

`runtime21/build_assignment21_v2.py` (~L126–130): a schema.org prop becomes a model dim **iff ≥25 training
instances carry it** (`MIN_SUPPORT=25`). Instances (with their properties) come from Postgres `capped.entity`,
mapped Wikidata-P-id → schema.org name via `bridge_prop.csv`. **Which types are pulled** is controlled by the
`TYPES` map (`build_assignment21_v2.py:37–41`, Wikidata qid → coarse family).

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
2. ~~`bridge_prop.csv` is missing~~ — **RESOLVED**: it is now vendored at
   [`training/props/bridge_prop.csv`](../training/props/bridge_prop.csv) (182 rows) with the 5 software P-ids
   mapped (`P306`→operatingSystem, `P277`→programmingLanguage, `P348`→softwareVersion, `P178`→author,
   `P400`→runtimePlatform).
3. **Two Postgres DBs**: `WORLD_PG_PASSWORD`/`KB_PG_PASSWORD` (env) for `capped.entity` + `knowledgebase`.

**Steps (1–4 done, 5–8 remaining):**
1. ✅ Code edits: added `"Q7397": "software"` to `TYPES` (`build_assignment21_v2.py:42`); ensured the software
   P-ids are in `bridge_prop.csv`. Still TODO for step 6: make software its own family in `build_families.py`
   (`"software":"software"` + `FAM_SCHEMATYPES["software"]=["SoftwareApplication"]`).
2. ✅ `build_assignment_pg` → `build_assignment21_v2` — basis grew to **71 props**; the software props appear in
   `alloc21_dims.json`.
3. ✅ `build_from_props` — corpus written to `runtime20/data/units_{train,test}.jsonl` (nc=90, ~1.1 MB).
4. ✅ `cp runtime20/data/alloc.json runtime20/data/alloc20.json` (the trainer reads `alloc20.json`).
5. **(GPU — REMAINING)** `python -m runtime20.scripts.train_props_gpu --steps 600 --lr 2e-4`.
6. Stage `encoder_props*.pt` + `qwen_lora_props/` into `runtime21/data/props_model/`, then
   `python -m runtime21.build_families` + `python -m runtime21.calibrate_props`.
7. Copy into `engine/data/`: `encoder_props.pt`→`encoder.pt`, `encoder_props_meta.pt`→`encoder_meta.pt`,
   `qwen_lora_props/*`→`qwen_lora/`, `runtime20/data/alloc.json`→`alloc.json`.
   (`families.json` + `props_thr.json` are written to `engine/data/` by step 6.) Do **not** touch
   `anchor_assignment.npz` (legacy, unused by the router).
8. Verify: `python -m tests.test_world` + `python -m tests.test_geo` — the software assertion must pass, with no
   regression on the other 76 assertions.

## Independence — what must be vendored

For this repo to build the model standalone, the training pipeline must move in from `flat-data`. Progress +
the hard cross-repo couplings that remain:
- ✅ `bridge_prop.csv` is committed here at `training/props/bridge_prop.csv` (first piece vendored).
- `runtime21/{build_assignment_pg,build_assignment21_v2,build_from_props,build_families,calibrate_props}.py` +
  `runtime20/{model11.py, scripts/train_props_gpu.py}` (the model + trainer) must be vendored into
  `training/props/`, with their hardcoded `SP`/`ROOT`/`DATA` paths parameterized.
- `runtime21/build_families.py:18` and `runtime21/calibrate_props.py:35` write directly into `engine/data` —
  keep that as an explicit `--out engine/data` flag once vendored.
- The two Postgres DBs are training-time inputs; document a from-scratch build (or ship the corpus).
