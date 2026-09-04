# Model Card

## Summary

Prereasoner is a tabular question-answering system with a learned semantic layer and a deterministic
SQL layer. Learned components provide embeddings, Schema.org property probabilities, class scores,
and structural relevance signals. Typed SQL search, route ownership, source-key authorization,
calculation semantics, SQL rendering, and execution are deterministic for fixed inputs and pinned
artifacts.

Schema.org 30.0 is the semantic coordinate system. Wikidata and publisher-owned releases provide
observations projected into that vocabulary; they do not define it. Mutable facts remain in
versioned database releases and are not intended to be memorized in model weights.

## Runtime Components

### Shared semantic encoder

- Base: `Qwen/Qwen2.5-0.5B`, encoder-only, pinned at Hugging Face revision
  `060db6499f32faf8b98477b0a26969ef7d8b9987`.
- Adaptation: LoRA plus `engine.encoder_model.RelationalModel`.
- Active uses: structural intent, deterministic candidate-ranking features, calculation operand
  retrieval, and the representation consumed by the Schema.org property head.
- Historical compatibility output: `alloc.json` still exposes 71 property-named coordinates. Two
  labels in that old basis are not valid Schema.org 30.0 properties (`GeoCoordinates` is a class;
  `taxonName` is absent). They are not the active class-routing vocabulary.
- Limitation: the historical encoder/LoRA training run predates the current immutable corpus
  manifest. Its exact runtime bytes and base revision are pinned, but its original source corpus and
  split provenance are incomplete. Do not claim a from-scratch reproduction of that checkpoint.

### Generalized Schema.org named-dimension model

- Head: a linear multi-label classifier over 80 named Schema.org property URIs using the frozen
  shared encoder representation.
- Corpus: 48,507 semantic instances; 39,173 train, 4,745 validation, and 4,589 untouched test.
- Property release: 56 of 80 trained properties pass validation qualification. All 1,521 ontology
  properties remain representable; untrained or unqualified properties abstain.
- Class computation: `sigmoid(bias + sum(property_probability * signed_weight))`. The bias, weights,
  property probabilities, thresholds, and individual contributions are surfaced as the actual
  computation path.
- Class release: all 926 Schema.org 30.0 classes are represented in the signature artifact. Eleven
  pass validation-only selection and untouched-test release gates: `DefinedTerm`,
  `ExchangeRateSpecification`, `MedicalCode`, `Movie`, `MusicRecording`, `Periodical`,
  `PostalAddress`, `Product`, `PropertyValueSpecification`, `Taxon`, and
  `UnitPriceSpecification`. Every other class explicitly abstains.
- Active serving use: `engine/router.py` maps a calibrated servable class through the pinned ontology
  to a coarse resolver family. A class can propose a family, but it cannot authorize a join by
  itself. `engine/knowledge_query.py` requires exact source-key grounding and retains a deterministic
  membership fallback when the class model abstains.
- Boundary: class evidence does not invent SQL, source facts, arithmetic, or joins. Typed planners
  and deterministic guards own those decisions.

The exact per-property and per-class support, thresholds, precision, recall, F1, and confusion
counts are in `engine/data/schema_property_model.json`. Training identity, dependencies, source
releases, split policy, seed, and metrics are in `engine/data/schema_training_manifest.json`.

### Entity-resolution embedder

- Model: `BAAI/bge-small-en-v1.5`, pinned at revision
  `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`.
- Use: embedding fallback after exact normalized entity lookup.
- Boundary: similarity is a retrieval proposal. Grounding and typed joins remain explicit.

## Artifact Identity

`engine/data/weights_manifest.json` pins every external runtime file by SHA-256 and binds the
committed Schema.org artifacts. Runtime validation rejects missing or mismatched files.

The public bundle is
[`prereasoner/prereasoner-weights`](https://huggingface.co/prereasoner/prereasoner-weights) at
immutable revision `0b5c2a5d4de3e488cd366633d95ce922755d3900`. The promoted property-head SHA-256 is
`cef8a43cfa1c5f719b9b1a7ef6e977197890d4c86236035049e1be91e9550e0c`.
`python -m engine.fetch_weights` downloads that revision without a token and verifies every digest.

## Intended Uses

- Natural-language analysis over uploaded tabular data.
- Inspectable SQL planning with explicit semantic and structural evidence.
- Public-entity grounding and governed reference-data joins.
- Research on deterministic planning informed by learned named dimensions.

## Out-Of-Scope Uses

- Treating a class score as an authoritative factual classification.
- Generating mutable factual answers from model weights.
- High-stakes medical, legal, tax, financial, or safety decisions without source-specific review.
- Inferring private identity or joining unrelated user data.
- Claiming service support for every representable Schema.org class.

## Evaluation And Limitations

Determinism removes decoder sampling variance; it does not guarantee correctness. Remaining error
classes include ambiguous schema linking, missing AST candidates, ranking errors, incomplete entity
resolution, source gaps, and unsupported Schema.org coordinates.

The Schema.org head has group-disjoint validation and untouched-test metrics. The shared historical
encoder does not have equivalent original-training provenance, so their metrics must not be merged.
SQL accuracy belongs in `spider/results/RESULTS.md`, and source-backed behavior belongs in
`world_eval/RESULTS.md`; neither is implied by the class-head metrics.

## Checkpoint Release Contract

1. Generate a corpus with pinned source releases and derivation-group splits.
2. Train only into an ignored experiment directory.
3. Select thresholds and classes using training and validation data only.
4. Evaluate once on untouched test data and record failures by source.
5. Record generator and trainer commits, dependency versions, model fingerprints, seed, splits,
   corpus hash, artifact hashes, and metrics in one manifest.
6. Publish the exact weight bundle at an immutable revision.
7. Promote through `training.schema_org.promote`, which verifies the bundle and updates the runtime
   artifacts atomically.
8. Run serving-faithful probes and release regression tests before tagging the source commit.
