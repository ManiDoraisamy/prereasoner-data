# engine/data — runtime model + typing artifacts

Everything the serving engine opens at runtime lives here (override the location with
`PREREASONER_DATA_DIR`). Large binaries are **gitignored** (the repo `.gitignore` excludes `*.pt`, `*.db`,
`*.npz` and `qwen_lora/`) and must be fetched/produced separately; the small CSV/JSON artifacts are committed.

**Provision the weights on a fresh clone:**
```
HF_TOKEN=<read-token> python -m engine.fetch_weights
```
This downloads `encoder.pt`, `encoder_meta.pt`, `qwen_lora/`, `anchor_assignment.npz`, and `primitives.npz`
into this directory (see `engine/fetch_weights.py`). `weights_manifest.json` pins the
source revision and SHA-256 of every runtime weight; both existing and downloaded bundles
must validate completely before use. The default source repo is
**`prereasoner/prereasoner-weights`** (currently **private** — set `HF_TOKEN` to a token with read access;
override the repo with `PREREASONER_WEIGHTS_REPO`). To (re)publish after a retrain:
`huggingface_hub.upload_folder(folder_path='engine/data', repo_id='prereasoner/prereasoner-weights',
allow_patterns=['*.pt','*.npz','qwen_lora/*'])`. To (re)train from scratch, see `docs/TRAINING.md`.

| File | Size | Purpose | In git? |
|---|---|---|---|
| `qwen_lora/` | ~17 MB | LoRA adapter for the Qwen2.5-0.5B unified encoder (the trained metric space). Loaded by `engine.encoder_overlay`, `engine.dimension`, `engine.router`. | no (gitignored) |
| `encoder.pt` | ~72 MB | State_dict of the trained relational readout (`engine.encoder_model.RelationalModel`). Plain `state_dict` — no pickled classes. | no (gitignored) |
| `encoder_meta.pt` | 8 KB | `{"alloc": …, "cfg": …}` — the dim allocation (names/families/ids) + the RelationalModel constructor config. Plain dicts pickled by `torch.save`; loaded with `torch.load(..., weights_only=False)`. | no (gitignored, `*.pt`) |
| `alloc.json` | 10 KB | The dim allocation as JSON (same content as `encoder_meta.pt["alloc"]`), used by `engine.router` which stays torch-free at import. | yes |
| `anchor_assignment.npz` | 667 KB | Per-dim Youden-J firing thresholds from the anchor head (`dims`, `thr` arrays). Used by `engine.encoder_overlay.load_encoder` and `engine.dimension`. | no (gitignored, `*.npz`) |
| `dim_thresholds.json` | 2 KB | Threshold OVERRIDES calibrated on the trained model for the /api/dimension readout. | yes |
| `route_thresholds.json` | 44 B | Per-leaf firing gates for world column routing (calibrated, recall-favoring). | yes |
| `assignment.csv` | 3.7 MB | The training-token table; at runtime only used by `engine.router._supported_leaves` (which leaves have >=3 training rows). | yes |
| `taxonomy.csv` | 5 KB | The Wikidata P279 taxonomy (qid, category_1..N root->leaf, status, world_tables). Source of `engine.taxonomy.LEAF_PATH/LEAF_QID/LEAF_TABLES` and the non-geo type map. | yes |
| `primitives.npz` | 72 KB | The learned 10-primitive linear head (`W`, `prims`, `thr`) read by `engine.primitive_head.PrimitiveReader`. | no (gitignored, `*.npz`) |
| `weights_manifest.json` | 1 KB | Pinned weight-repository revision and immutable hashes for the complete runtime bundle. | yes |
| `word_city.json` | 5 KB | World word-table metadata (key/concepts/filter attrs/links) for the meaning-graph planner. | yes |
| `word_country.json` | 4 KB | ditto | yes |
| `word_state.json` | 1 KB | ditto | yes |
| `word_element.json` | 0.5 KB | ditto | yes |

Notes:

- `words.db` (a local SQLite mirror of the world word tables) is referenced by the SQLite fallback path in
  `engine.world_tables` but is NOT shipped — the live serving path executes on Postgres
  (`engine.pg`/`engine.entities`) and never opens it. It did not exist in the source deployment either.
- Filter/entity DATA (world."words", world/wikipedia tables, public.settlement) lives in Postgres, populated
  by the `db/sync` pipeline — not in this directory.
- Training corpora, embedding caches and intermediate build artifacts intentionally do not ship (see
  docs/notes/engine.md for the dropped-files list).
