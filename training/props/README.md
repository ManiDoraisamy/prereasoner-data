# training/props — the schema.org-property typing model

This is the (in-progress) vendored home of the property-typing training pipeline. See `docs/TRAINING.md`
for the full architecture + runbook. The keystone data file `bridge_prop.csv` (Wikidata-P-id →
schema.org-property URI, 182 rows) lives here so the pipeline is reproducible from this repo.

## Status: adding "software" — VALIDATED end-to-end on CPU (GPU train pending)

The engine's 8-family property router could not type a **software** column (it abstained). This was fixed by
adding software to the training basis. Every CPU step is done + validated; only the GPU encoder-train remains.

**What was done + proven (live, this repo's Postgres `capped.entity`):**
1. `bridge_prop.csv` here now maps the 5 software P-ids: `P306`→operatingSystem, `P277`→programmingLanguage,
   `P348`→softwareVersion, `P178`→author, `P400`→runtimePlatform.
2. Added `"Q7397": "software"` to `TYPES` in `build_assignment21_v2.py` (Q7397 = "software", 13,958 instances;
   note Q166142 has **0** — use Q7397).
3. Rebuilt the basis: **67 → 71 property dims** — `operatingSystem, softwareVersion, programmingLanguage, author`
   entered (they clear the ≥25-instance floor; `runtimePlatform` did not).
4. Regenerated the corpus (`build_from_props`): **nc = 90** (9 struct + 71 prop + 10 intent), software family
   present, corpus written to `runtime20/data/units_{train,test}.jsonl` (~1.1 MB).
5. **PROVED software now has a distinctive property: `operatingSystem` (0.27 in-family vs 0.00 out-of-family).**
   So a software column will fire `operatingSystem` → the consensus router decodes the `software` family → it
   types. (The earlier "software has no distinctive props" was purely the missing bridge mappings.)

**What remains (the mechanical finish):**
6. **GPU encoder-train** (`runtime20/scripts/train_props_gpu.py --steps 600`) on the nc=90 corpus — the one step
   that back-props through Qwen-0.5B; needs a GPU (e.g. RunPod). ~20 MB up (corpus + base LoRA), ~90 MB back.
   Remember the `cp runtime20/data/alloc.json runtime20/data/alloc20.json` swap first.
7. `build_families` (software gets `operatingSystem` as its distinctive prop) + `calibrate_props`
   (Youden-J thresholds for the 4 new dims).
8. Copy `encoder.pt`/`encoder_meta.pt`/`qwen_lora/`/`alloc.json`/`families.json`/`props_thr.json` into
   `engine/data/`, then `python -m tests.test_world` + `test_geo` — the software assertion must pass.

## Independence — remaining vendor work

The training/model scripts still live in `prereasoner-flat-data` (`runtime21/*`, `runtime20/model11.py`,
`runtime20/scripts/train_props_gpu.py`) with hardcoded paths. To make the model rebuildable standalone, move
those in here and parameterize the two engine-write paths (`build_families.py`, `calibrate_props.py`). This
`bridge_prop.csv` is the first piece vendored.
