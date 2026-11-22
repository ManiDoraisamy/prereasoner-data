# Training and model promotion

> **Status (2026-11-22).** The trained own-data readers (`engine/readers`, their weights in `engine/data/readers`, their training in `training/readers`) were replaced by the Decisions path (`engine/program`, `DECISIONS.md` 2026-11-20); they remain in git history before that change, and `training/readers/` keeps only the GPU lease loop. The Decisions path trains no question reader: it reads with stock bge-m3 and the engine's word reader, and Gemini's Decisions API (external) makes its closed choices; an answer most likely wrong is retried by one query Gemini 4 Neon writes (`engine/retry.py`). The current own-data path is in [PROMPT_TO_SQL.md](PROMPT_TO_SQL.md) and [ARCHITECTURE.md](ARCHITECTURE.md#own-data-sql-planner); what this document says of the readers is historical.

This page explains which model code runs today, how its data is built, and how one candidate becomes
the runtime bundle. Historical experiments live in `training/props/pipeline.md`; they are background,
not another serving path.

## What the model learns

Schema.org 30.0 supplies the vocabulary: 1,521 properties, 926 classes, inheritance, domains, and
ranges. It supplies names and relationships, not factual rows. Wikidata and publisher-owned source
releases provide observations that explicit adapters map to those coordinates.

The model learns relation and column shapes. Mutable answer facts remain in versioned PostgreSQL
source releases. The model does not invent a join, arithmetic rule, or factual value.

## Runtime Architecture

```text
table summary
    |
    v
pinned Qwen 0.5B + LoRA shared representation
    |                         |
    |                         +-> structural intent signals (composition, world routes)
    v
80-URI Schema.org property head
    |
    v
class_score = sigmoid(bias + sum(property_probability * signed_weight))
    |
    v
released Schema.org class -> coarse resolver family proposal
    |
    v
exact source-key grounding -> typed planner/join/calculation -> SQL execution
```

The learned class proposal and every named contribution are inspectable. Exact source-key grounding
authorizes access to world data. Typed code owns SQL and calculation semantics. When no calibrated
class qualifies, the model abstains; deterministic source membership can still recover a grounded
entity route.

## The Model Tracks

### Shared encoder (`training/props/`)

This track produced the Qwen LoRA and relational readout loaded by serving. It supplies structural
intent for composition and the world routes, column typing, and the representation consumed by the
Schema.org head. The own-data path (the Decisions path) does not use it.

Its allocation (`encoder_meta.pt["alloc"]`) retains a historical 90-content-dimension layout: 9
structural, 71 property-named, and 10 intent dimensions. Two old labels are not valid Schema.org 30.0
properties. Those coordinates are compatibility and diagnostic outputs, not the active class
vocabulary. On this branch `engine/data/alloc.json` no longer mirrors it: it holds only the schema.org
property basis of the new reader (the 71 and 12 additions with their evidence in
`training/props/basis_additions_20261112.json`), and the old props pipeline (`training/props/`,
`training/taxonomy/`) that reads the structural and intent entries runs from main until integration.

The exact runtime bytes are hash-pinned, but the original encoder training run predates the current
corpus manifest and does not have complete source/split provenance. Treat
`training/props/pipeline.md` as a historical reproduction contract, not proof that the original run
can be independently recreated byte for byte.

### Generalized named-dimension model (`training/schema_org/`)

This is the active ontology-validated property and class layer:

- 48,507 derivation-group-isolated instances
- 39,173 train, 4,745 validation, 4,589 untouched test
- 80 trained property URIs; 56 validation-qualified
- all 1,521 properties and 926 classes represented
- 11 classes released; every unsupported class abstains

The released classes are `DefinedTerm`, `ExchangeRateSpecification`, `MedicalCode`, `Movie`,
`MusicRecording`, `Periodical`, `PostalAddress`, `Product`, `PropertyValueSpecification`, `Taxon`,
and `UnitPriceSpecification`.

The class layer is deterministic once property probabilities are known. Each class has a fitted
bias, signed property weights, a validation-calibrated threshold, support, and validation/test
metrics. Selection uses train and validation only. Test is untouched until final evaluation.

### Own-data readers (`training/readers/`, removed 2026-11-20)

History. Until 2026-11-20 the own-data SQL planner (`engine/tables.py:TableQuery.select_query`) was
read by four learned parts: a stage-1 schema-item model (XLM-R base), stage-2 reading models (XLM-R
large), a re-scorer (XLM-R base), and a logistic choice fitted on held-out calibration databases that
ranked the candidates and refused below its threshold. They classified readings of a question; the
deterministic builder in `engine/readers/readings.py` made each reading a typed AST. `training/readers/`
trained them from Spider, BIRD and Spider-Syn questions whose gold SQL was converted to readings, plus
questions Gemini (`engine/llm.py`) wrote on the training databases: rewordings of training questions
(`rewordings.py`), questions over lookup tables built from them (`lookups.py`), and questions for
queries built in the structures the readers misread (`structures.py`); each kept the gold readings of
its query, and none touched a calibration or test database. `python -m training.readers.promote`
installed a bundle into `engine/data/readers/`. That pipeline is in git history before the change;
[`training/readers/README.md`](../training/readers/README.md) says what remains. The Decisions path
that replaced the readers trains no question reader (see the status note above).

History: earlier designs trained a 0.5B LoRA SQL proposer (`training/proposer/`) and fit a linear
arbiter on its pools (`training/rank/`). Both pipelines were removed with the models they served;
their Spider runs remain in `spider/results/RESULTS.md` as measurements of those designs.

## Corpus Build

`training/schema_org/corpus.py` reads only explicit source adapters and release records. Current
sources include Wikidata, IANA, Unicode CLDR, Google libphonenumber, GeoNames, ECB, European
Commission TEDB, Nager.Date, CDC/NCHS, NIH/NLM CDE, public product-template fixtures, and class-free
demo uploads.

Important invariants:

- Split assignment is by derivation group, never by rendered instance.
- Source identities and identical text cannot cross splits.
- A property label is kept only when its evidence survives tokenization and truncation.
- Wide tables become bounded related facets.
- Source release IDs, URLs, content hashes, licenses, mappings, drop reasons, and split counts are
  recorded in `training/schema_org/data/semantic_manifest.json`.
- Generated corpus rows are ignored; the committed manifest and code define their identity.

See `docs/DATA_CARD.md` for composition and limitations and `docs/SOURCE_DATA.md` for source terms.

## Build, Train, Promote

From a clean repository with the CPU environment in `training/requirements.lock.txt`:

```powershell
python -m training.schema_org.corpus
python -m training.schema_org.train_property_head
python -m training.schema_org.promote <corpus-prefix> --revision <immutable-hf-commit>
python -m tests.test_schema_coverage
python -m tests.test_schema_decode
python -m tests.test_route_wired
```

Candidates are written only under
`training/schema_org/data/experiments/<corpus-sha>/`. Training never writes runtime files.
`training.schema_org.promote` is the sole writer of the promoted property head, model metadata,
training manifest, and class signatures in `engine/data/`.

Promotion rejects a candidate unless it can prove:

1. the ontology, corpus, encoder, trainer source, seed, and dependency identity;
2. validation-only property and class qualification;
3. untouched-test release floors for every class being promoted;
4. valid finite logistic weights, bias, and thresholds;
5. no zero-evidence class can cross its threshold;
6. all candidate artifacts agree by hash; and
7. the exact property head is present at an immutable public weight revision.

## Compute And GPU Leases

Embedding-cache generation is the expensive encoder stage. A cache may be reused only when its text
and encoder fingerprints match. The linear property-head optimizer can run deterministically on CPU
and records Python, Torch, NumPy, scikit-learn, device, and platform versions.

For a GPU run, use `training/tools/run_schema_training.sh` through:

```powershell
python -m training.tools.runpod_api lease --help
```

The wrapper creates a bounded lease from a digest-pinned image and terminates the pod on success,
failure, timeout, interruption, and process exit. The runner verifies its CUDA-enabled Torch before
installing the rest of the hash-locked training closure. `--keep` is the explicit exceptional
behavior, and a command that succeeded but whose download did not finish keeps its pod until its
provider deadline (exit 5), because the pod holds the only copy. Never use an ad hoc pod-creation
command for repository training.

## Runtime Artifacts

| Artifact | Purpose |
|---|---|
| `engine/data/qwen_lora/` | shared encoder adapter |
| `engine/data/encoder.pt` | relational readout |
| `engine/data/encoder_meta.pt` | readout constructor and allocation metadata |
| `engine/data/schema_property_head.pt` | 80-property linear head |
| `engine/data/schema_property_model.json` | thresholds, metrics, fingerprints, release gates |
| `engine/data/schema_class_signatures.json` | all 926 class definitions and released class scores |
| `engine/data/schema_training_manifest.json` | immutable corpus/trainer/runtime/evaluation provenance |
| `engine/data/sheet_line_model.json` | the sheet compiler's header decision (installed only by `training/sheet/promote.py`) |
| `engine/data/weights_manifest.json` | public weight revision and every runtime SHA-256; under `base_models`, the hashes of the stock bge-m3 files the image bakes from the hub (not trained here) |

The public bundle is `prereasoner/prereasoner-weights` at immutable revision
`cd8a03abb8fd443442f9a3e691f717f4285fd263`. `python -m engine.fetch_weights` downloads and verifies
it without requiring a token. That revision also holds the retired readers' files (`readers/`); the
manifest does not list them and the fetcher does not download them.

## Changing The Model

Do not train directly into `engine/data/`, edit thresholds by hand, or leave multiple active
checkpoints. A model change is complete only when the candidate is promoted atomically, the old
runtime path is removed or explicitly retained for a documented compatibility responsibility, the
model/data cards match the manifest, and serving-faithful regression tests pass.
