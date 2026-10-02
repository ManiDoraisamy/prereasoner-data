# engine/data — runtime model + typing artifacts

Everything the serving engine opens at runtime lives here (override the location with
`PREREASONER_DATA_DIR`). Large binaries are **gitignored** (the repo `.gitignore` excludes `*.pt`, `*.db`,
`*.npz`, `*.previous` and `qwen_lora/`, and keeps the retired `*.gguf` and `sql_proposer/` out of git) and
must be fetched/produced separately; the small CSV/JSON artifacts are committed.

**Provision the encoder weights on a fresh clone:**
```
python -m engine.fetch_weights
```
This downloads `encoder.pt`, `encoder_meta.pt`, `qwen_lora/`, `anchor_assignment.npz`,
`primitives.npz`, and `schema_property_head.pt` into this directory (see
`engine/fetch_weights.py`). That is the whole runtime bundle: the engine loads no SQL model.
`weights_manifest.json` pins the source revision and SHA-256 of every runtime weight; both existing and downloaded bundles
must validate completely before use. The default source repo is the public
**[`prereasoner/prereasoner-weights`](https://huggingface.co/prereasoner/prereasoner-weights)**;
no account or token is required. Override it with `PREREASONER_WEIGHTS_REPO`; set `HF_TOKEN` only when
the replacement repository requires authentication. After retraining, first run
`python -m training.props.promote --local-only` and complete local gates. Upload exactly the paths
from `weights_manifest.json`'s `files` map (not a separate glob list), then run
`python -m training.props.promote --revision <immutable-hf-commit>` and commit the updated manifest.
A local-only manifest intentionally refuses fresh-clone download. To retrain from scratch, see
`docs/TRAINING.md`.

| File | Size | Purpose | In git? |
|---|---|---|---|
| `qwen_lora/` | ~17 MB | LoRA adapter for the Qwen2.5-0.5B unified encoder (the trained metric space). Loaded by `engine.encoder_overlay`, `engine.dimension`, `engine.router`. | no (gitignored) |
| `encoder.pt` | ~72 MB | State_dict of the trained relational readout (`engine.encoder_model.RelationalModel`). Plain `state_dict` — no pickled classes. | no (gitignored) |
| `encoder_meta.pt` | 8 KB | `{"alloc": …, "cfg": …}` — the dim allocation (names/families/ids) + the RelationalModel constructor config. Contains tensors and primitive containers only; loaded with `torch.load(..., weights_only=True)`. | no (gitignored, `*.pt`) |
| `alloc.json` | 10 KB | The dim allocation as JSON (same content as `encoder_meta.pt["alloc"]`). No `engine/` code reads this file; serving reads the allocation from `encoder_meta.pt`. | yes |
| `anchor_assignment.npz` | 667 KB | Per-dim Youden-J firing thresholds from the anchor head (`dims`, `thr` arrays). Used by `engine.encoder_overlay.load_encoder` and `engine.dimension`. | no (gitignored, `*.npz`) |
| `dim_thresholds.json` | 2 KB | Threshold OVERRIDES calibrated on the trained model for the /api/dimension readout. | yes |
| `route_thresholds.json` | 44 B | Per-leaf firing gates for world column routing (calibrated, recall-favoring). Not read by serving; written and read by `training/calibrate/` and `training/lib/router.py` (training/history). | yes |
| `assignment.csv` | 3.7 MB | The training-token table. Not read by serving; read by `training/` corpus, calibration and family builders (training/history). | yes |
| `families.json` | 6 KB | Wikidata type QID -> Schema.org family map on the router side, written by `training/props/build_families.py`. No `engine/` module reads it. | yes |
| `props_thr.json` | 2 KB | Calibrated property thresholds for the unified encoder, written by `training/props/calibrate_props.py` and installed by `python -m training.props.promote`. No `engine/` module reads it. | yes |
| `word_exchange_rate.json` | 1 KB | World word-table metadata for the ECB `exchange_rate` table; loaded with the other `word_*.json` files by `engine.knowledge_tables` and `engine.resolve_base`. | yes |
| `taxonomy.csv` | 5 KB | The Wikidata P279 taxonomy (qid, category_1..N root->leaf, status, world_tables). Source of `engine.taxonomy.LEAF_PATH/LEAF_QID/LEAF_TABLES` and the non-geo type map. | yes |
| `primitives.npz` | 72 KB | The learned 10-primitive linear head (`W`, `prims`, `thr`) read by `engine.primitive_head.PrimitiveReader`. | no (gitignored, `*.npz`) |
| `weights_manifest.json` | 1 KB | Pinned weight-repository revision and immutable hashes for the complete runtime bundle. | yes |
| `word_city.json` | 5 KB | World word-table metadata (key/concepts/filter attrs/links) for the meaning-graph planner, loaded by the `word_*.json` glob in `engine.knowledge_tables` and `engine.resolve_base`. | yes |
| `word_country.json` | 4 KB | ditto | yes |
| `word_state.json` | 1 KB | ditto | yes |
| `word_element.json` | 0.5 KB | ditto | yes |
| `schema_org_v30.json` | 3.3 MB | Compiled Schema.org vocabulary and inheritance, read by `engine.schema_org`. Hash-pinned under `committed_artifacts`. | yes |
| `schema_property_head.pt` | 289 KB | Schema.org named-property head (see below). | no (gitignored, `*.pt`; fetched) |
| `schema_property_model.json` | 110 KB | Property-head metadata, read by `engine.schema_model` and `engine.router`. | yes |
| `schema_class_signatures.json` | 424 KB | Class signatures for deterministic class scoring, read by `engine.schema_decode`. | yes |
| `schema_training_manifest.json` | 63 KB | Training provenance for the Schema.org head. Hash-pinned under `committed_artifacts`; no `engine/` module reads it beyond bundle validation. | yes |
| `sql_proposer/` | — | Retired 0.5B LoRA "d2" SQL adapter. It may still exist in old local copies (and is in the published bundle revision), but it is not in `weights_manifest.json`, `engine.fetch_weights` does not download it, and no code reads it. | no (gitignored) |

Notes:

- `words.db` (a local SQLite mirror of the world word tables) is the optional SQLite path in
  `engine/knowledge_tables.py` (`WORD_DIR / "words.db"`) but is NOT shipped — the live serving path executes on Postgres
  (`engine.pg`/`engine.entities`) and never opens it. It did not exist in the source deployment either.
- Filter/entity data (`knowledgebase."words"`, Wikidata-backed world tables, and `public.settlement`) lives in PostgreSQL, populated
  by the `db/sync` pipeline — not in this directory.
- Training corpora, embedding caches and intermediate build artifacts intentionally do not ship (see
  docs/notes/engine.md for the dropped-files list).
- A `xiyan_sql_proposer.gguf` (4.68 GB) from the retired SQL selection may remain in an old local copy
  of this directory, since it was gitignored. Nothing reads it; it can be deleted.

## Schema.org named-property head

`schema_property_head.pt`, `schema_property_model.json` and `schema_class_signatures.json` are produced by
`training/schema_org/` and installed by
`python -m training.schema_org.promote <corpus> --revision <immutable-bundle-commit>`, which is the only
writer of these three files. Training itself writes candidates to
`training/schema_org/data/experiments/<corpus-sha>/` and never touches this directory.

`schema_property_model.json`, `schema_class_signatures.json` and `schema_org_v30.json` are **committed**
and their hashes recorded under `committed_artifacts` in `weights_manifest.json` — they travel with the
source. `schema_property_head.pt` is **gitignored** (`engine/data/*.pt`) and, like every other weight, is
**published to the weights repo and pinned by sha256 in the `files` map**, so `python -m
engine.fetch_weights` retrieves it and `validate_weight_bundle` verifies it. It is also in the Dockerfile's
required-artifact assertion, so a container that somehow lacks it fails at start rather than serving
silently degraded.

The head records the encoder it was trained on (`encoder_artifact_sha256`):
`engine/artifact_provenance.py:semantic_encoder_fingerprint`, the base-model pin plus the adapter's model
files (`qwen_lora/adapter_config.json` and `adapter_model.safetensors`, never a README or other stray file).
`SchemaInterpreter` refuses any other adapter. The interpreter is part of the bundle, not an optional extra:
`KnowledgeQuery` loads it at construction, so a bundle it cannot load fails the container's startup probe,
and the in-image regression gate (`regress/run_regression.py:run_bundle_checks`) loads it at build, so such
an image is never pushed.
