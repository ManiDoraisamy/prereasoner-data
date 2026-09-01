# engine/data — runtime model + typing artifacts

Everything the serving engine opens at runtime lives here (override the location with
`PREREASONER_DATA_DIR`). Large binaries are **gitignored** (the repo `.gitignore` excludes `*.pt`, `*.db`,
`*.npz` and `qwen_lora/`) and must be fetched/produced separately; the small CSV/JSON artifacts are committed.

**Provision the weights on a fresh clone:**
```
python -m engine.fetch_weights
```
This downloads `encoder.pt`, `encoder_meta.pt`, `qwen_lora/`, `anchor_assignment.npz`,
`primitives.npz`, and `schema_property_head.pt` into this directory (see
`engine/fetch_weights.py`). `weights_manifest.json` pins the
source revision and SHA-256 of every runtime weight; both existing and downloaded bundles
must validate completely before use. The default source repo is the public
**[`prereasoner/prereasoner-weights`](https://huggingface.co/prereasoner/prereasoner-weights)**;
no account or token is required. Override it with `PREREASONER_WEIGHTS_REPO`; set `HF_TOKEN` only when
the replacement repository requires authentication. After retraining, first run
`python -m training.props.promote --local-only` and complete local gates. Upload those exact large files,
then run `python -m training.props.promote --revision <immutable-hf-commit>` and commit the updated
manifest. A local-only manifest intentionally refuses fresh-clone download. To retrain from scratch, see
`docs/TRAINING.md`.

| File | Size | Purpose | In git? |
|---|---|---|---|
| `qwen_lora/` | ~17 MB | LoRA adapter for the Qwen2.5-0.5B unified encoder (the trained metric space). Loaded by `engine.encoder_overlay`, `engine.dimension`, `engine.router`. | no (gitignored) |
| `encoder.pt` | ~72 MB | State_dict of the trained relational readout (`engine.encoder_model.RelationalModel`). Plain `state_dict` — no pickled classes. | no (gitignored) |
| `encoder_meta.pt` | 8 KB | `{"alloc": …, "cfg": …}` — the dim allocation (names/families/ids) + the RelationalModel constructor config. Contains tensors and primitive containers only; loaded with `torch.load(..., weights_only=True)`. | no (gitignored, `*.pt`) |
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
- Filter/entity data (`knowledgebase."words"`, Wikidata-backed world tables, and `public.settlement`) lives in PostgreSQL, populated
  by the `db/sync` pipeline — not in this directory.
- Training corpora, embedding caches and intermediate build artifacts intentionally do not ship (see
  docs/notes/engine.md for the dropped-files list).

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

A working copy that has not fetched weights still degrades safely rather than crashing:
`SchemaInterpreter` fails to construct, `engine/knowledge_query` logs it loudly, and answers are unaffected
— which is why `tests/test_route_wired.py` asserts the class evidence only when the head is present.
