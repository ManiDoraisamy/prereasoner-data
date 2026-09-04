# Training and model promotion

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
    |                         +-> structural/ranking/calculation signals
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

## The Two Model Tracks

### Shared encoder (`training/props/`)

This track produced the Qwen LoRA and relational readout loaded by serving. It supplies structural
intent, ranking features, calculation operand retrieval, and the representation consumed by the
Schema.org head.

Its `alloc.json` retains a historical 90-content-dimension allocation: 9 structural, 71
property-named, and 10 intent dimensions. Two old labels are not valid Schema.org 30.0 properties.
Those coordinates are compatibility and diagnostic outputs, not the active class vocabulary.

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
behavior. Never use an ad hoc pod-creation command for repository training.

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
| `engine/data/weights_manifest.json` | public weight revision and every runtime SHA-256 |

The public bundle is `prereasoner/prereasoner-weights` at immutable revision
`0b5c2a5d4de3e488cd366633d95ce922755d3900`. `python -m engine.fetch_weights` downloads and verifies
it without requiring a token.

## Changing The Model

Do not train directly into `engine/data/`, edit thresholds by hand, or leave multiple active
checkpoints. A model change is complete only when the candidate is promoted atomically, the old
runtime path is removed or explicitly retained for a documented compatibility responsibility, the
model/data cards match the manifest, and serving-faithful regression tests pass.
