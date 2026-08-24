# Model Card

## Summary

PreReasoner is a neuro-symbolic system, not a text-generating SQL model. Learned components emit
semantic scores and embeddings. Typed planning, routing ownership, joins, calculation verification,
SQL rendering, and execution remain deterministic for fixed inputs and artifacts.

Schema.org 30.0 is the semantic coordinate system. Wikidata and publisher-owned datasets supply
training observations projected into those coordinates; they do not define the vocabulary.
Mutable source facts are not intended to be memorized as answers.

## Shipped Components

### Unified semantic encoder

- Base: `Qwen/Qwen2.5-0.5B`, used encoder-only under Apache-2.0. New builds pin Hugging
  Face revision `060db6499f32faf8b98477b0a26969ef7d8b9987`.
- Adaptation: LoRA plus `engine.encoder_model.RelationalModel`.
- Outputs: 90 content dimensions: 9 structural, 71 legacy property-named coordinates, and 10
  intent dimensions. Two legacy coordinates do not satisfy the pinned ontology contract:
  `GeoCoordinates` is a class, and `taxonName` is absent from Schema.org 30.0.
- Uses: deterministic ranking signals, column-family evidence, structural intent, and calculation
  operand retrieval.
- Training data: capped Wikidata entity/property observations mapped into Schema.org coordinates,
  structural SQL graphs, intent augmentation, and calculation contrastive pairs.
- Important limitation: the published stable checkpoint metadata records architecture and allocation
  but not a complete source-corpus hash, split identity, seed, or held-out metric report. Its bytes are
  pinned; independent training provenance is incomplete. This router remains authoritative for world
  table selection and is not yet the ontology-clean multi-source model described as the target architecture.
  The original checkpoint metadata did not record the Hugging Face revision, so the historical
  training run is not independently byte-reproducible even though new serving builds are pinned.

### Schema.org named-property evidence head

- Base: the frozen Qwen encoder representation.
- Head: linear multi-label classifier over 75 trained Schema.org property URIs.
- Training data: 45,772 grouped semantic instances from Wikidata, active publisher releases, and
  class-free demo negatives; 36,496 train, 4,553 validation, and 4,723 test.
- Uses: table-level property probabilities and deterministic class-signature evidence.
- Serving boundary: evidence only. A class prediction cannot choose a route, add a join, or release
  an answer.
- Coverage: the ontology artifact represents 1,521 properties and 926 classes. Unsupported or
  under-calibrated coordinates abstain. Only six classes are currently servable.
- Metrics: exact per-property and per-class support, thresholds, precision, recall, F1, and confusion
  counts are committed in `engine/data/schema_property_model.json`.
- Important limitation: the metadata does not bind the head or its embedding cache to an immutable
  encoder/adapter/base-model fingerprint. The current head-to-encoder compatibility therefore cannot
  be independently proven from committed artifacts.
- Important limitation: the historical artifact used its test split during class release
  qualification. Future training gates servability on validation only, but an untouched external
  audit set is still required for a final generalization claim.

### Entity-resolution embedder

- Model: `BAAI/bge-small-en-v1.5` under MIT, pinned at Hugging Face revision
  `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`.
- Use: embedding fallback after exact normalized entity lookup.
- Boundary: a similarity match is not sufficient for an answer; grounding and typed joins remain
  explicit.

## Artifact Identity

`engine/data/weights_manifest.json` pins the immutable external weight revision and SHA-256 of every
large runtime file. It also pins committed Schema.org model artifacts. Runtime validation rejects a
missing or mismatched file.

The current default weight repository is private. Publishing this source repository therefore does
not make the complete model-backed application reproducible. A public model release must contain
the exact manifested files at the exact immutable revision and preserve the base-model and data
notices in `THIRD_PARTY.md`.

## Intended Uses

- Natural-language analysis over uploaded tabular data.
- Inspectable SQL planning with explicit semantic evidence.
- Public-entity grounding and governed reference-data joins.
- Research on deterministic planning informed by learned semantic coordinates.

## Out-Of-Scope Uses

- Treating class or family scores as authoritative factual classifications.
- Generating factual answers directly from model weights.
- High-stakes medical, legal, tax, financial, or safety decisions without source-specific review.
- Inferring private identity or joining unrelated user data.
- Claiming support for all Schema.org classes because they are representable in the ontology.

## Evaluation And Limitations

Determinism is not correctness. Known error classes include ambiguous schema linking, missing
candidate shapes, ranking errors, incomplete entity resolution, source coverage gaps, and
under-supported Schema.org coordinates.

The multi-source property head has group-disjoint held-out metrics and explicit abstention. The
unified router checkpoint does not yet have equivalent complete provenance in the published
manifest. Do not combine their metrics or claim that the router was trained on the multi-source
corpus. Serving-faithful SQL metrics, with denominators and artifact hashes, belong in
`spider/results/RESULTS.md`.

## Release Requirements

Before publishing a new checkpoint:

1. train into a disposable experiment directory, never `engine/data/`;
2. record the ontology, source releases, mappings, corpus hash, split, seed, base model, objective,
   and full held-out gates;
3. compare against the frozen baseline through production serving;
4. obtain explicit promotion approval;
5. publish one complete bundle at an immutable revision;
6. update and validate the manifest atomically; and
7. document rollback to the prior immutable revision without retaining a second active bundle.
