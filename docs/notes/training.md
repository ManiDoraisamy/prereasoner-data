# training/ migration notes (runtime20 → open-source release)

Source: `C:\work\prereasoner-flat-data\runtime20` (train11.py, train17.py, scripts/) + `csv7/scripts/runpod_api.py`.
Destination: `training/` — self-contained, version-free names, banned strings verified zero
(`34.123.19.176`, `hf_…` token literals, `runtime20`, `runtime1*` — checked case-insensitively, incl. docstrings).

## Script map (old → new)

### Vendored runtime modules → `training/lib/`
| old | new | note |
|---|---|---|
| `runtime20/walker7.py` | `lib/walker.py` | unit-graph builder (`build_from_units`) |
| `runtime20/edges11.py` | `lib/edges.py` | `edges11()` renamed `edges()`; 10 edge types, `N_EDGE` |
| `runtime20/model11.py` | `lib/relblock.py` | class `Runtime11Model` renamed `RelBlockModel` |
| `runtime20/encoder19.py` | `lib/encoder.py` | `LiveQwen`; `MODEL_ID` now env `BASE_MODEL_ID` |
| `runtime20/embed16.py` | `lib/embedder.py` | bge-small retrieval embedder + `normalize_surface` |
| `runtime20/route19.py` | `lib/router.py` | column Router (needed by calibrate/validate); ROOT depth fixed |
| `runtime20/build_world_wdqs.py` (client part only) | `lib/wdqs.py` | trimmed to `wdqs/V/qid_of/parse_point/ENDPOINT/UA`; the bulk geo fetchers + `build_world_pg` dep were NOT carried (serving-side world import). UA contact now env `WIKIMEDIA_CONTACT` |
| `runtime20/query14.py` (`_pg` only) | `lib/pg.py` | new module; fully env-driven (`KB_PG_HOST` default `localhost`, `_PORT`, `_DB`, `_USER`, `_SSLMODE`) |

### Pipeline scripts
| old | new |
|---|---|
| `scripts/discover_csv_types.py` | `corpus/discover_csv_types.py` (CSV corpus path now env `CSV_CORPUS_DIR`) |
| `scripts/cluster_columns_scale.py` | `corpus/cluster_columns.py` |
| `scripts/split_for_rename.py` | `corpus/split_for_rename.py` |
| `scripts/cluster_coherence.py` | `corpus/cluster_coherence.py` |
| `scripts/build_review.py` | `corpus/build_review.py` |
| `scripts/build_corpus19.py` | `corpus/build_corpus.py` |
| `scripts/build_from_entity.py` | `corpus/build_from_entity.py` |
| `scripts/fetch_type_instances.py` | `corpus/fetch_type_instances.py` |
| `scripts/organize_taxonomy.py` | `taxonomy/organize_taxonomy.py` |
| `scripts/reconcile_taxonomy.py` | `taxonomy/reconcile_taxonomy.py` |
| `scripts/rollup_taxonomy.py` | `taxonomy/rollup_taxonomy.py` |
| `scripts/coverage_list.py` | `taxonomy/coverage_list.py` |
| `scripts/build_alloc19.py` | `taxonomy/build_alloc.py` |
| `runtime20/train11.py` | `train/train_multitask.py` |
| `runtime20/train17.py` | `train/train_unified.py` |
| `scripts/train19.py` | `train/train_taxonomy.py` |
| `scripts/anchor20.py` | `anchor/anchor_head.py` |
| `scripts/reanchor20.py` | `anchor/reanchor.py` |
| `scripts/calibrate_route.py` | `calibrate/calibrate_route.py` |
| `scripts/calibrate_dims.py` | `calibrate/calibrate_dims.py` |
| `scripts/validate19.py` | `calibrate/validate_data.py` |
| `scripts/validate_route19.py` | `calibrate/validate_route.py` |
| `scripts/fetch_properties.py` | `world/fetch_properties.py` |
| `scripts/sync_wikidata_world.py` | `world/sync_wikidata_world.py` |
| `scripts/mirror_world_schema.py` | `world/mirror_world_schema.py` |
| `scripts/build_wikipedia_schema.py` | `world/build_wikipedia_schema.py` |
| `scripts/sync_world_types.py` | `world/sync_world_types.py` |
| `scripts/unify_words_qid.py` | `world/unify_words_qid.py` |
| `scripts/regen20.py` | `tools/pipeline.py` (build_svc19 step removed — bundle assembly is engine-side now) |
| `csv7/scripts/runpod_api.py` | `tools/runpod_api.py` (pod name/key/paths genericized) |

### Artifact renames (must stay consistent with engine/)
`runtime20_model.pt`→`encoder.pt` · `runtime20.pt`→`encoder_meta.pt` · `runtime17(_model).pt`→`unified(_model)/meta.pt` ·
`runtime11(_model).pt`→`multitask…` · `runtime10(_model).pt`→`sql_base…` · `alloc20.json`→`alloc.json` ·
`alloc11.json`→`alloc_multitask.json` · `route_eval19.json`→`route_eval.json` · env `RUNTIME10_MODEL_ID`→`BASE_MODEL_ID`.
Prose uses `genN` for the internal iteration numbers (README explains).

## Dropped scripts (and why)

| script | reason |
|---|---|
| `anchor_assignment.py` | superseded by `anchor20.py` (its own docstring: anchor_assignment reads the OLD bge cache, not the capped.entity data the shipped model trained on) |
| `anchor_taxonomy.py` | early hand-listed-taxonomy ridge experiment; superseded by the build_review/train/anchor path |
| `anchor_world_types.py` | 6-world-table dim prototype; superseded by the full taxonomy dims |
| `build20.py` | pruned the bge-derived node set; superseded by `build_from_entity.py` (the clean capped.entity builder regen20 canonizes) |
| `build_taxonomy_real.py` | 22 hand-picked leaves; superseded by the corpus-driven chain |
| `map_columns_dbpedia.py`, `build_taxonomy_from_corpus.py`, `build_taxonomy_grounded.py` | the DBpedia-mapping taxonomy generation; superseded by the clustering chain (cluster_columns → LLM rename → reconcile_taxonomy → rollup_taxonomy), which reconcile_taxonomy's docstring calls out as fixing its ~30% value-resolution errors |
| `cluster_columns.py` (2.5k-sample) | superseded by `cluster_columns_scale.py` (full ~100k-CSV run) — the scale version was carried, renamed `corpus/cluster_columns.py` |
| `clean_cache.py` | cleaned the old bge `wd_cache` labels; consumer path superseded by capped.entity; also imports `query17`, which no longer loads in the consolidated tree |
| `reanchor19.py` | superseded by `reanchor20.py` (nc=93, capped.entity units) |
| `regen19.py` | superseded by `regen20.py` |
| `consolidate20.py` | one-off package-flattening migration; not part of reproduction |
| `test1/build_regression20.py` | found (task asked to search): emits a browser regression for the LIVE /reason endpoint — serving QA, not training |
| `runtime20/build_svc19.py` | Cloud Run bundle assembly — engine/serving side |
| `runtime20/build_world_pg.py` | 95 GB parquet-dump world importer; its own successor's docstring says the WDQS path (`build_world_wdqs`) replaced it; only the WDQS client was vendored |
| `split_for_rename.py`'s `merge_renames.py` | referenced in a docstring but never existed in the source tree; reconcile_taxonomy reads `cluster_renames.json` directly |

## Pipeline order (as reconstructed from docstrings + regen20 + PHASE_HISTORY)

1. **World DB** (`world/`): fetch_properties → sync_wikidata_world / mirror_world_schema → build_wikipedia_schema → sync_world_types → unify_words_qid.
2. **Discovery** (`corpus/`): discover_csv_types → cluster_columns → split_for_rename → (external LLM rename) → cluster_coherence.
3. **Taxonomy** (`taxonomy/`): reconcile_taxonomy → rollup_taxonomy → build_alloc (audit: coverage_list).
4. **Training corpus** (`corpus/`): build_from_entity (capped.entity; run ONCE — its query has no ORDER BY, a re-run desyncs the shipped units). Earlier-generation corpus: build_review / build_corpus / fetch_type_instances (inputs to train_taxonomy).
5. **Encoder training** (`train/`, GPU/RunPod): train_multitask → train_unified → train_taxonomy (produced the shipped `qwen_lora`).
6. **Readout** (`anchor/`): reanchor (produced the shipped `encoder.pt`) → anchor_head.
7. **Calibration + gates** (`calibrate/`): calibrate_route, calibrate_dims, validate_data, validate_route — orchestrated transactionally by `tools/pipeline.py`.
8. **Packaging**: copy `training/data` artifacts → `engine/data/`.

## Is train_multitask (train11) still required?

**Yes for a from-scratch reproduction; no for the practical loop.** Evidence:

- `train17` (train_unified) **imports code from train11** (`load`, `auc`, `fam_report`) and **warm-starts its RelBlock
  from `runtime11_model.pt`**, train11's output. `train19` (train_taxonomy) in turn warm-starts from train17's
  `qwen_lora` + `runtime17_model.pt` and **saved the shipped `qwen_lora` / `runtime20(_model).pt`** (its save block
  writes all three). So train11 is a live link in the shipped encoder's warm-start chain — it was copied, not dropped.
- However `regen20.py` (the canonical release pipeline) marks `train19` **"LEGACY, kept for reference"**: the
  runtime20 self-contained loop starts from the already-trained `qwen_lora` and only re-runs
  `build_from_entity → anchor20 → reanchor20 → calibrate → validate`. train19 even has a hard "LEGACY GUARD" that
  exits unless the historical bootstrap artifacts exist.
- Residual uncertainty: train11 itself warm-starts from **runtime10** checkpoints (`runtime10(_model).pt`, now
  `sql_base…`), and consumes gen9/10/11 corpora (`unit_emb.npy`, `sql_graphs_*`, `join_graphs_*`) whose *generator*
  scripts predate runtime20 and are not in the source scope — those are data artifacts. A truly-from-nothing rerun of
  phase 4 is therefore not possible from `training/` alone; this is stated plainly in training/README.md.

## Other findings / decisions

- **No embedded HF tokens anywhere** in the source (verified by regex); HF auth was always env/.env based
  (`csv7/scripts/runpod_api.py` reads `HF_TOKEN` from repo-root `.env` and injects it into the pod env).
- The hardcoded Cloud SQL IP `34.123.19.176` appeared as the `KB_PG_HOST` **default** in build_from_entity,
  build20, validate_route19, query14, build_world_pg → all carried copies now default to `localhost`;
  dbname/user/port/sslmode are env-driven too (`lib/pg.py` documents the full contract).
- `capped.entity` (build_from_entity's input) is loaded by a separate Cloud Run job (`capped1/` in the research
  repo: streams the ~102 GB Wikidata dump, keeps ≤CAP instances per taxonomy leaf). Not copied — out of the
  runtime20 script scope; documented in README as an external input whose output CSVs ship with the release.
- Hardware provenance: PHASE_HISTORY records RunPod **RTX 4090** training (runtime9 explicitly); the pod driver's
  GPU priority list is 4090/A5000/L4/A4000/3090/A4500 with the `runpod/pytorch:2.4.0-py3.11-cuda12.4.1` image.
- `DEVICE` env override added to every `torch.device(...)` site; `BASE_MODEL_ID` overrides the Qwen base.
- Layout note: `lib/router.py` (route19) is serving code vendored into training because calibrate_route /
  calibrate_dims / validate_route calibrate against the *served* readout by construction. When `engine/` lands,
  the two copies should be reconciled (risk: drift between engine's router and training's vendored one).
- Artifact-name risk: `runtime20.pt` was renamed `encoder_meta.pt` here; if the engine loader expects a different
  name for the `{alloc, cfg}` companion, align the engine or rename in `anchor/reanchor.py` + `tools/pipeline.py`.
