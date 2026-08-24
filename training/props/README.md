# training/props — the schema.org-property typing model

This is the **vendored** home of the property-typing training pipeline: it rebuilds the shipped
PROPERTY-consensus model in `engine/data/` (`alloc.json`, `families.json`, `props_thr.json`, `encoder.pt`,
`encoder_meta.pt`, `qwen_lora/`) from this repo. The keystone data file `bridge_prop.csv` (Wikidata-P-id →
schema.org-property URI, 181 mappings) lives here.

- **The DAG + runbook** (per-stage consumes/produces, external inputs, env vars, exact command order):
  [`pipeline.md`](pipeline.md).
- **The architecture** (what the model does, why the type emerges from properties, how the basis is selected) +
  the add-a-type runbook: [`docs/TRAINING.md`](../../docs/TRAINING.md).

Stage 3 (`train_props_gpu.py`) warm-starts from the prebuilt **gen20** LoRA + RelBlock (shipped in `engine/data/`
as `qwen_lora/`, `encoder.pt`, `encoder_meta.pt`) — that gen20 taxonomy pipeline is legacy and kept only as this
warm-start base (see the repo-level [`training/README.md`](../README.md)).

## Software Experiment Status — Not Promoted

The router originally had eight families and abstained on **software** columns. The corpus and
family configuration were expanded to nine families, but the GPU candidate regressed shared intent
signals and was not promoted. The currently manifested stable weights therefore must not be
described as the completed software retrain merely because `alloc.json` and `families.json` contain
the new coordinate and family definitions.

**What was done + proven (live, this repo's Postgres `capped.entity`):**
1. `bridge_prop.csv` here maps the 5 software P-ids: `P306`→operatingSystem, `P277`→programmingLanguage,
   `P348`→softwareVersion, `P178`→author, `P400`→runtimePlatform.
2. Added `"Q7397": "software"` to `TYPES` in `build_assignment21_v2.py` (Q7397 = "software", 13,958 instances;
   note Q166142 has **0** — use Q7397), and made software its own family in `build_families.py`
   (`TYPE_FAM["software"]="software"` + `FAM_SCHEMATYPES["software"]=["SoftwareApplication"]`, moved out of `product`).
3. Rebuilt the basis: **67 → 71 property dims** — `operatingSystem, softwareVersion, programmingLanguage, author`
   entered (they clear the ≥25-instance floor, `MIN_SUPPORT=25`; `runtimePlatform` did not).
4. Regenerated the corpus (`build_from_props`): **nc = 90** (9 struct + 71 prop + 10 intent), software family
   present, corpus written to `data/units_{train,test}.jsonl` (~1.1 MB).
5. **PROVED software now has a distinctive property: `operatingSystem` (0.27 in-family vs 0.00 out-of-family).**
   So a software column will fire `operatingSystem` → the consensus router decodes the `software` family → it
   types. (The earlier "software has no distinctive props" was purely the missing bridge mappings.)

**First GPU run (2026-07-22): software typed, but held back.** The run reached prop mean-AUC **0.938** and the
deployed candidate passed both software assertions (test_world 15/15, test_geo 35/35) — but it **regressed
COUNT-aggregate intent** ("how many customers in France" → the free-text path instead of `COUNT(*)`), because
keep-best selected on property AUC alone while the un-frozen LoRA drifted the (weakly-anchored) intent dims.
The fix is in place — see "Representation guards" in [`pipeline.md`](pipeline.md): `augment_intent.py` anchors the
serving phrasings + holds out a serving-mirror intent eval, and the trainer's keep-best now selects on
**property AUC + intent op-accuracy** combined. `eval_intent.py` reproduces the live failure on the drifted
checkpoint (COUNT 0.110 vs SUM 0.121, to three decimals): shipped baseline **0.808**, drifted **0.697** (the
None class collapsed 0.769 → 0.138 — spurious fires), on a question-text-split eval with zero verbatim leakage.

**What remains before any promotion:**
6. `python -m training.props.augment_intent` (once per corpus refresh), then the **GPU encoder-train**
   (`python -m training.props.train_props_gpu --steps 600`) on the nc=90 corpus; needs a GPU (e.g. RunPod).
   ~20 MB up (corpus + base LoRA), ~90 MB back. Remember the `cp data/alloc.json data/alloc20.json` swap first.
   Gate the result: `python -m training.props.eval_intent --ckpt props` — intent op-accuracy must be **≥ the
   0.808 engine baseline** with the `howmany_customers_france` probe OK.
7. `python -m training.props.build_families` (software gets `operatingSystem` as its distinctive prop) +
   `python -m training.props.calibrate_props` (Youden-J thresholds for the 4 new dims).
8. Copy the Stage-3 outputs into `engine/data/` under the engine's names
   (`encoder.pt`/`encoder_meta.pt`/`qwen_lora/`/`alloc.json`; `families.json` + `props_thr.json` are already
   staged there by step 7), then `python -m tests.test_world` + `test_geo` (software must pass) **and**
   `python -m tests.test_route_wired` (the COUNT world-join must still answer — the French-customer count,
   3 with the current fixture; the test asserts ≥2).

## External inputs (not committed)

The pipeline is vendored, but a few large / upstream-owned inputs are not committed here — drop them into `data/`
before running: **`columns.csv`** (Stage 1), **`type_table_map.csv`** (Stage 4), the two Postgres DBs (`world` +
`knowledgebase`), and the prebuilt **gen20 warm-start** artifacts (`qwen_lora/`, `unified_model.pt`,
`unified_meta.pt` — the base LoRA + RelBlock, shipped in `engine/data/`). (`alloc20.json` is **not** an input —
Stage 2b generates it by copying the freshly built `data/alloc.json`.) Full list, env vars, and command order in
[`pipeline.md`](pipeline.md).
