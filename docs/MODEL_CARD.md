# Model Card

## Summary

Prereasoner is a tabular question-answering system with a learned semantic layer and a typed SQL
layer. Learned components provide embeddings, Schema.org property probabilities, class scores,
structural relevance signals, and the own-data readers' readings of a question. No model writes SQL:
a deterministic builder makes every own-data query a typed AST from the readers' readings, and the
engine validates, renders, and executes it. Selection, route ownership, source-key authorization,
calculation semantics, SQL rendering, and execution are deterministic for fixed inputs and pinned
artifacts. Gemini, an optional external model, serves the chat orchestrator, starter questions and
reference generation; it takes no part in own-data query selection
([Gemini](#gemini-external-optional)).

Schema.org 30.0 is the semantic coordinate system. Wikidata and publisher-owned releases provide
observations projected into that vocabulary; they do not define it. Mutable facts remain in
versioned database releases and are not intended to be memorized in model weights.

## Runtime Components

### Shared semantic encoder

- Base: `Qwen/Qwen2.5-0.5B`, encoder-only, pinned at Hugging Face revision
  `060db6499f32faf8b98477b0a26969ef7d8b9987`.
- Adaptation: a LoRA adapter (rank 32, alpha 64, on the q/k/v/o projections of all 24 layers,
  `engine/data/qwen_lora/`) trained jointly with `engine.encoder_model.RelationalModel`: one
  896→384 projection and 10 relational-attention blocks (6 heads, 384 wide). Both are frozen at
  serving. No decoder is used.
- Named coordinates: 90 output coordinates carry names (`encoder_meta.pt["alloc"]`, mirrored in
  `engine/data/alloc.json`): 9 structural
  types (`is_str`, `is_num`, `is_time`, `is_key`, `currency`, ...), 71 property names such as
  `addressCountry`, `birthDate`, `brand`, and `postalCode`, and 10 query intents (`intent_agg_sum`,
  `intent_filter_gt`, `intent_sort_desc`, `intent_limit`, ...).
- Active uses: structural intent for composition and the world routes (the operator from
  `read_op_model`, the primitive head), column typing, and the representation consumed by the
  Schema.org property head. The own-data SQL planner does not use it; the readers below read those
  questions.
- Historical compatibility output: `alloc.json` still exposes 71 property-named coordinates. Two
  labels in that old basis are not valid Schema.org 30.0 properties (`GeoCoordinates` is a class;
  `taxonName` is absent). They are not the active class-routing vocabulary.
- Limitation: the historical encoder/LoRA training run predates the current immutable corpus
  manifest. Its exact runtime bytes and base revision are pinned, but its original source corpus and
  split provenance are incomplete. Do not claim a from-scratch reproduction of that checkpoint.

### Own-data readers

- Models (`engine/data/readers/`): a stage-1 schema-item model (XLM-R base) that scores each table's and
  column's use; stage-2 reading models (XLM-R large, one directory per reader, `stage2_*`) that score
  every reading the deterministic builder uses; a re-scorer (XLM-R base) that orders the candidates that
  ran; and the choice, a logistic model over the re-scorer's logit and the pool's own evidence that ranks the
  candidates and refuses below its threshold (`choice.json`). Each model is an
  encoder with linear heads, defined once in `engine/readers/models.py`; serving runs the encoders in
  bfloat16. No decoder is used.
- Inputs: the question and the schema as the engine receives it: table and column names, inferred types,
  sample values, and the question's own values beside the columns that hold them
  (`engine/readers/text.py`).
- Boundary: the readers emit readings, never SQL. `engine/readers/readings.py:build` makes each reading
  a typed AST, the engine runs and grounds every candidate, and when the choice is not confident the
  reply says how the question was read ("I read this as …") instead of serving an answer.
- Training: Spider, BIRD and Spider-Syn questions with gold readings derived from gold SQL, plus
  questions Gemini on Vertex AI wrote on the training databases (rewordings of training questions,
  questions over lookup tables, and questions for queries built in the structures the readers
  misread), each keeping its query's gold readings; no calibration or test database is used.
  `training/readers/` trains them, and only `training/readers/promote.py` installs them, recording each
  file's SHA-256 in `engine/data/weights_manifest.json`. The recipe, inputs and split are in
  [training/readers/README.md](../training/readers/README.md).

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

### Gemini (external, optional)

- Model: Gemini on Vertex AI (`GEMINI_MODEL`, default `gemini-3.8-flash`), called through
  `engine/llm.py`. It is an external service, not part of the weight bundle, and Prereasoner does not
  train or fine-tune it.
- Gate: it runs only when the operator enabled external models (`EXTERNAL_LLM_ENABLED`, default
  off; the guided Community deployment turns it on together with chat) and configured a Vertex AI
  project.
- Uses: the chat orchestrator's tool loop, starter questions, and explicit reference-table generation.
  All sit outside the SQL planner; see [MCP.md](MCP.md). Own-data query selection calls no external
  model.

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
immutable revision `3455714f98cb253ec787473af8a5204c72ad3290`. The promoted property-head SHA-256 is
`cef8a43cfa1c5f719b9b1a7ef6e977197890d4c86236035049e1be91e9550e0c`.
`python -m engine.fetch_weights` downloads that revision without a token and verifies every digest.
That revision also holds a retired `sql_proposer/` LoRA adapter from an earlier design. The manifest
does not list it, the fetcher does not download it, and current Prereasoner loads no SQL model.

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

Determinism removes sampling variance; it does not guarantee correctness. Remaining error classes
include misread questions, shapes the readings form cannot hold, ranking and refusal errors,
incomplete entity resolution, source gaps, and unsupported Schema.org coordinates. Recorded accuracy
and latency are in `spider/results/RESULTS.md`.

The Schema.org head has group-disjoint validation and untouched-test metrics. The shared historical
encoder does not have equivalent original-training provenance, so their metrics must not be merged.
SQL accuracy belongs in `spider/results/RESULTS.md`, and source-backed behavior belongs in
`world_eval/RESULTS.md`; neither is implied by the class-head metrics. The readers' training sources
include Spider's dev questions, so a Spider DEV result is not a held-out measurement of them; the Spider
test set appears in no readers training step.

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
