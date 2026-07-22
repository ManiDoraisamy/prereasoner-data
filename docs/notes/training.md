# training/ — the reproduction pipeline

`training/` is the self-contained pipeline that produces the model artifacts the engine serves
(the `qwen_lora` adapter, the `encoder.pt` readout, the router/dimension calibration files). This
note maps the packages and scripts and records the pipeline order and the from-scratch caveats.
Connection params are env-driven (`KB_PG_HOST` default `localhost`, plus `_PORT`/`_DB`/`_USER`/
`_SSLMODE`; `lib/pg.py` documents the full contract); the Qwen base is `BASE_MODEL_ID`.

## Package map (what each part contains)

### `training/lib/` — vendored runtime modules (shared with the encoder)
| module | responsibility |
|---|---|
| `lib/walker.py` | unit-graph builder (`build_from_units`) |
| `lib/edges.py` | `edges()`; 10 edge types, `N_EDGE` |
| `lib/relblock.py` | `RelBlockModel` (the relational-content block) |
| `lib/encoder.py` | `LiveQwen`; base model id from `BASE_MODEL_ID` |
| `lib/embedder.py` | bge-small retrieval embedder + `normalize_surface` |
| `lib/router.py` | the column Router (needed by calibrate/validate — see the two-copies note below) |
| `lib/wdqs.py` | the WDQS client (`wdqs/V/qid_of/parse_point/ENDPOINT/UA`); UA contact from env `WIKIMEDIA_CONTACT` |
| `lib/pg.py` | Postgres connection helper, fully env-driven |

### `training/corpus/` — column discovery + training-corpus building
`discover_csv_types.py` (CSV corpus path from env `CSV_CORPUS_DIR`), `cluster_columns.py`,
`split_for_rename.py`, `cluster_coherence.py`, `build_review.py`, `build_corpus.py`,
`build_from_entity.py`, `fetch_type_instances.py`.

### `training/taxonomy/` — taxonomy construction
`organize_taxonomy.py`, `reconcile_taxonomy.py`, `rollup_taxonomy.py`, `coverage_list.py`,
`build_alloc.py`.

### `training/train/` — encoder training (GPU/RunPod)
`train_multitask.py`, `train_unified.py`, `train_taxonomy.py`.

### `training/anchor/` — readout anchoring
`reanchor.py` (produces `encoder.pt`), `anchor_head.py`.

### `training/calibrate/` — calibration + validation gates
`calibrate_route.py`, `calibrate_dims.py`, `validate_data.py`, `validate_route.py`.

### `training/world/` — offline world-DB build helpers
`fetch_properties.py`, `sync_wikidata_world.py`, `mirror_world_schema.py`,
`build_wikipedia_schema.py`, `sync_world_types.py`, `unify_words_qid.py`. (These overlap with
`db/sync/`; the world DB is normally built from `db/sync/`.)

### `training/tools/`
`pipeline.py` (orchestrates the reproduction loop transactionally), `runpod_api.py` (RunPod pod
driver; pod name/key/paths are genericized).

### Artifact names (must stay consistent with engine/)
`encoder.pt` (the readout state_dict) + `encoder_meta.pt` (`{alloc, cfg}`) are the shipped encoder;
`alloc.json` is the dim allocation. The training pipeline also emits intermediate `unified`/
`multitask`/`sql_base` checkpoints (the warm-start chain) and `route_eval.json`. Internal iteration
numbers are written `genN` in prose (README explains).

## Pipeline order (from the tools/pipeline.py reproduction loop + README)

1. **World DB** (`world/`): fetch_properties → sync_wikidata_world / mirror_world_schema → build_wikipedia_schema → sync_world_types → unify_words_qid.
2. **Discovery** (`corpus/`): discover_csv_types → cluster_columns → split_for_rename → (external LLM rename) → cluster_coherence.
3. **Taxonomy** (`taxonomy/`): reconcile_taxonomy → rollup_taxonomy → build_alloc (audit: coverage_list).
4. **Training corpus** (`corpus/`): build_from_entity (reads the capped entity-instances table; run ONCE — its query has no ORDER BY, so a re-run desyncs the shipped units). Earlier-generation corpus: build_review / build_corpus / fetch_type_instances (inputs to train_taxonomy).
5. **Encoder training** (`train/`, GPU/RunPod): train_multitask → train_unified → train_taxonomy (produces the `qwen_lora` adapter).
6. **Readout** (`anchor/`): reanchor (produces `encoder.pt`) → anchor_head.
7. **Calibration + gates** (`calibrate/`): calibrate_route, calibrate_dims, validate_data, validate_route — orchestrated transactionally by `tools/pipeline.py`.
8. **Packaging**: copy `training/data` artifacts → `engine/data/`.

## Is train_multitask still required?

**Yes for a from-scratch reproduction; no for the practical loop.**

- `train_unified` imports helpers from `train_multitask` (`load`, `auc`, `fam_report`) and warm-starts its
  RelBlock from `train_multitask`'s checkpoint. `train_taxonomy` in turn warm-starts from `train_unified`'s
  `qwen_lora` + checkpoint and saves the shipped `qwen_lora` / readout. So `train_multitask` is a live link in
  the shipped encoder's warm-start chain.
- The practical reproduction loop (`tools/pipeline.py`) treats full encoder training as **legacy**: it starts
  from the already-trained `qwen_lora` and only re-runs
  `build_from_entity → anchor → reanchor → calibrate → validate`. `train_taxonomy` has a hard guard that exits
  unless the historical bootstrap artifacts exist.
- Residual caveat: `train_multitask` itself warm-starts from an even earlier SQL-base checkpoint and consumes
  earlier-generation corpora (`unit_emb.npy`, `sql_graphs_*`, `join_graphs_*`) whose *generator* scripts are
  not in scope — those ship as data artifacts. A truly-from-nothing rerun of phase 4 is therefore not possible
  from `training/` alone; this is stated plainly in `training/README.md`.

## Other notes

- **No embedded HF tokens anywhere**; HF auth is env/.env based (`tools/runpod_api.py` reads `HF_TOKEN` from the
  repo-root `.env` and injects it into the pod env).
- Connection params are env-driven everywhere (no hardcoded host/IP): `KB_PG_HOST` defaults to `localhost`,
  with dbname/user/port/sslmode also from env (`lib/pg.py` documents the full contract).
- **The capped entity-instances table** (`build_from_entity`'s input) is produced by a separate offline job that
  streams the full Wikidata dump and keeps ≤CAP instances per taxonomy leaf. That job is out of scope for
  `training/`; its output CSVs ship with the release as an external input (documented in `training/README.md`).
- Hardware provenance: training was done on RunPod (RTX 4090); the pod driver's GPU priority list is
  4090/A5000/L4/A4000/3090/A4500 with the `runpod/pytorch:2.4.0-py3.11-cuda12.4.1` image.
- `DEVICE` overrides every `torch.device(...)` site; `BASE_MODEL_ID` overrides the Qwen base.
- **Two Router copies:** `lib/router.py` is the serving router vendored into training because
  calibrate_route / calibrate_dims / validate_route calibrate against the *served* readout by construction.
  Keep it reconciled with `engine/router.py` (risk: drift between the two).
- **Artifact-name coupling:** the `{alloc, cfg}` companion to `encoder.pt` is named `encoder_meta.pt`; if the
  engine loader ever expects a different name, align the engine or rename in `anchor/reanchor.py` +
  `tools/pipeline.py`.
