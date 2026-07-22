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

## Runbook: add a new type (worked example — software)

**Validated preconditions (checked live):**
- `capped.entity` holds **13,958 SoftwareApplication instances** carrying genuinely distinctive props:
  `P178` developer (22%), `P306` operatingSystem (16%), `P348` softwareVersion (15%), `P277`
  programmingLanguage (12%), `P400` platform (6%). So software **is** typeable by property consensus — its
  props just need to reach the basis. (These clear `MIN_SUPPORT=25` easily.)

**Blockers to running it (must be resolved first):**
1. **GPU** — `train_props_gpu.py` back-props through Qwen-0.5B (~600 steps). CPU is a multi-hour job; use a
   GPU box (e.g. RunPod). Every other step is CPU/Postgres.
2. **`bridge_prop.csv` is missing** (it lived in a scratchpad temp dir; not in git). The Wikidata→schema.org
   prop mapping must be reconstructed, and it must map the software P-ids above
   (`P306`→`operatingSystem`, `P348`→`softwareVersion`, `P178`→`developer`, `P277`→`programmingLanguage`,
   `P400`→`applicationSubCategory`/`featureList`).
3. **Two Postgres DBs**: `WORLD_PG_PASSWORD`/`KB_PG_PASSWORD` (env) for `capped.entity` + `knowledgebase`.

**Steps:**
1. Code edits: add `"Q166142": "software"` to `TYPES` (`build_assignment21_v2.py:37–41`); make software its own
   family in `build_families.py` (`"software":"software"` + `FAM_SCHEMATYPES["software"]=["SoftwareApplication"]`);
   ensure the software P-ids are in `bridge_prop.csv`.
2. `python -m runtime21.build_assignment_pg` → `python -m runtime21.build_assignment21_v2` (rebuilds the basis;
   confirm the software props now appear in `alloc21_dims.json`).
3. `python -m runtime21.build_from_props` (writes the encoder corpus to `runtime20/data/`).
4. `cp runtime20/data/alloc.json runtime20/data/alloc20.json` (the trainer reads `alloc20.json` — easily missed).
5. **(GPU)** `python -m runtime20.scripts.train_props_gpu --steps 600 --lr 2e-4`.
6. Stage `encoder_props*.pt` + `qwen_lora_props/` into `runtime21/data/props_model/`, then
   `python -m runtime21.build_families` + `python -m runtime21.calibrate_props`.
7. Copy into `engine/data/`: `encoder_props.pt`→`encoder.pt`, `encoder_props_meta.pt`→`encoder_meta.pt`,
   `qwen_lora_props/*`→`qwen_lora/`, `runtime20/data/alloc.json`→`alloc.json`.
   (`families.json` + `props_thr.json` are written to `engine/data/` by steps 6.) Do **not** touch
   `anchor_assignment.npz` (legacy, unused by the router).
8. Verify: `python -m tests.test_world` + `python -m tests.test_geo` — the software assertion must pass, with no
   regression on the other 76 assertions.

## Independence — what must be vendored

For this repo to build the model standalone, the training pipeline must move in from `flat-data`. The hard
cross-repo couplings to fix:
- `runtime21/build_families.py:18` and `runtime21/calibrate_props.py:35` write directly into `engine/data`.
- `bridge_prop.csv` must be committed here (not a temp path).
- `runtime20/{model11.py, scripts/train_props_gpu.py}` (the model + trainer) + the corpus builder must be
  vendored as, e.g., `training/props/`.
- The two Postgres DBs are training-time inputs; document a from-scratch build (or ship the corpus).
