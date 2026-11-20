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

Schema.org 30.0 is the required common semantic coordinate system. The current implementation uses it
for property/class evidence and world-resolution proposals, but own-data noun/column/value bindings
still use the dedicated readers' opaque embeddings. The required shared interface is not implemented
across those roles. Wikidata and publisher-owned releases provide observations projected into the
vocabulary; they do not define it. Mutable facts remain in
versioned database releases and are not intended to be memorized in model weights.

## Required shared semantic interface and current gap

ACE noun meanings, column meanings and contextualized values must be weighted profiles over the same
ordered, selected Schema.org **property** URIs. Reuse the compact basis selected by `training/props/`:
67 original property coordinates, expanded to 71 in the current 90-coordinate allocation. The 1,521
ontology properties are a vocabulary catalog, not a requirement for 1,521 learned outputs. Historical
non-property names require explicit migration before calling the selected basis URI-valid. A coordinate
must have the same meaning and calibrated scale in
all three roles. Profiles must participate causally in semantic binding, with their actual contributions
reported. SQL operation/intent coordinates remain explicit and separate from the property vocabulary.
Physical table/column/cell pointers, literal values, entity/source keys and relationship roles remain
explicit: similar city profiles cannot identify Paris as Lyon, or distinguish departure from arrival
without their roles. Unsupported meanings or insufficient evidence require calibrated abstention.

The production readers below do not meet this semantic-interface requirement. The interrupted CLEF
generic-AST trial also does not: its inputs contain names, SQL types and samples, and its head combines
opaque contextual/lexical projections and residual scores under AST/action supervision. There are no
shared property-profile targets or cross-role evidence masks. Valid ACE program renderings, AST
schemas and inspectable SQL decisions do not repair that gap. That trial is historical evidence, not a
shared-Schema.org model or a promoted replacement.

The existing 80-property head is a useful vocabulary and artifact reference, but its 896-dimensional
Qwen inputs and table-property calibration are not a drop-in CLEF interface. Missing observed facts
must not become negative labels for expected concept meanings. A migration needs explicit contextual
supervision for every role, held-out schema/entity/rewording tests, and interventions showing that the
binding computation actually uses the named coordinates. No opaque residual semantic score may choose
the binding independently. The migration contract and current owner boundaries are in
[ARCHITECTURE.md](ARCHITECTURE.md#shared-property-binding-migration).

### Recursive shared-head candidate (2026-11-07)

The recursive experiment freezes **46,952 train / 5,125 calibration / 2,162 held-out entity test
records**. It visits every one of 32,798 question parents and rejects 499 whose target literal or
limit is absent from the input-derived candidates; these targets are not injected into inputs.
There are 32,160 answerable programs and 139 clarification records. Test entity records are excluded
from the fit reader. The artifact and code hashes, exclusions and exact learning objective are in
`shared-semantic-grammar-20261106/experiment-contract.json`.

Those are the frozen experiment's historical counts. The November 8 baseline
audit visited all 32,798 question parents: 32,512 non-clarification targets passed
schema/round-trip/native APE checks, 32,091 passed full-source answer agreement,
292 had binding rejections and 129 timed out in SQL. Separate corrected overlays
pass 158/158 grouping and 154/154 video-game-profile pairs. Seven previously empty
databases have been restored from author sources (249,185,435 rows); their 4,235
affected questions have fresh populated-source checks. Private audit copies have
complete typed-row and schema identity proofs. The completed whole-corpus
binding traversal and reviewed overlays now contain 519 direct label repairs:
32,469 executable targets, 297 clarifications and 32 binding rejections across
all 32,798 questions. The current per-pair export has native APE evidence for
all 32,501 of its non-clarification programs and full-source SQL answer checks
for 32,423; 46 SQL timeouts remain. Actual preparation on a distributed 116-question,
84-database sample prepares 84 and rejects the same 32 known binding failures.
The new 296-decision goalie program requires more than the prior 256-position
setting and prepares at 512; whole-corpus capacity is not yet certified.
A later 119-question/86-database preparation check admits 95 and rejects 24.
Across the original 116 identities, eight rejections recover, all 84 prior
admissions remain, and there are no losses; three added regressions also admit.
This combines input changes with reviewed labels, not model predictions.
Its 540.70-second elapsed time includes concurrent full-corpus preparation and
is not an isolated performance comparison. Consolidating the later overlays with
the same code gives 128 questions across 88 databases: 105 prepare and 23 reject.
Nine original rejections recover, all 84 prior admissions remain and all 12 added
records admit. These are preparation outcomes, not trained-model accuracy.
The staged labels now number 536; the 528-label full preparation run remains in
progress. A new full-corpus finite
numeric-bound diagnostic has zero remaining flags after two interval repairs.
All 20 recent reviewed questions pass actual preparation and mechanical/control
checks, with native ACE/schema/full-source SQL checks for their 19 executable
programs and one clarification. Correct citation direction and catalog membership
exclude dangling paper identities. A covering index resolves the three SQL
timeouts within the same 120-second bound; complete typed-row/table identity
proofs cover all 147,416,275 source rows. The later year-superlative and authorship
repairs also pass. These overlapping staged results do not replace the completed
whole-corpus counts above.
These results do not certify complete English meaning. Every shared
question remains quarantined. No model was fit, promoted or benchmarked as part
of these corrections. Current artifacts and the gated execution plan are in
`training/readers/README.md`.

One prefill and the shared 215-coordinate head supply recursive conditional role/operator choices
to the existing typed-AST owner. Named values, contributions, input bindings, ACE projection and
serving transforms can replay without an encoder. Candidate injection uses the same selector and
executor as serving. The 71 expected-property coordinates train jointly, but noun/column/value
property alignment does not yet causally drive this recursive readout. Native encoder internals
remain opaque. The canonical ACE application view is not an independently verified English
reformulation. No recursive candidate accuracy or full objective achievement is established here.

The full-corpus LoRA attempt retained 128 updates before its measured cost forecast exceeded the
experiment ceiling. A separate controlled frozen-head fit uses those exact encoder bytes and the
same corpus, with a single 1,101,015-parameter named projection. Its 28-record training diagnostic
completes and reloads exactly; its two held-out programs show no raw field-choice accuracy gain.
The full frozen-head run was cancelled before full-fit optimizer progress was confirmed; its Pod
and dedicated network volume were deleted. No completed full fit or new Spider/Enron accuracy
result exists. A further experiment requires the owner's review of the local corpus audit,
verification of the actual available starting checkpoint bytes, and separate paid-compute
authorization. The gated execution plan is in `training/readers/README.md` under "Local audit and
gated CLEF execution plan". Native prefills use pinned
numerical settings and no encoder gradient graph. No production model was replaced, and the real
property-alignment and English-meaning limitations above remain open.

The complete benchmark input audit reports no construction errors: all 1,053 questions of the
standing database-held-out Spider calibration split fit the current format (median 9,873, maximum
13,850 tokens), as do all 186 all-tab Enron questions (median 7,697, maximum 23,814). These are
resource coverage measurements, not predictions or evidence of correct answers. The original
graders and all benchmark questions remain outside gradient training.

### Earlier shared-head feasibility fits (2026-11-06)

The candidate vocabulary is now fully defined: **215 = 71 properties + 64 SQL + 32 ACE frames +
32 structural/type/role + 16 dialogue**. All coordinates remain uncalibrated. Definitions, role inputs,
integer profiles, exact contribution-based binding/replay, masked loss and the CLEF shared-head
construction exist in the current owners. The promoted serving selector still uses the readers below;
the earlier shared-head pilot is trained, but is not qualified. The recursive candidate integration
above does not retroactively qualify that pilot.

Frozen preparation produced **47,445 training / 5,131 calibration / 2,162 held-out entity test records**.
This includes all 18,427 admitted source programs and 14,371 conversation records; overlapping variants
are not counted as independent source tasks. The remaining records supervise expected property
profiles for 27 annotated classes. Four classes share an all-zero quantized profile in the selected
basis and cannot be distinguished by that interface. Missing ACE-frame and English argument-alignment
labels are masked. Synthetic concept/column witnesses are not held-out real-column evidence.

A subsequent, untrained data revision annotates the bounded generator's dialogue controls only after
matching previous input/schema and checking its full source-reading delta. The source readings are
necessary because canonical MIN/MAX optimization can erase order/limit history. This does not certify
arbitrary dialogue or add ACE-frame/English span labels to the fitted pilot.

One native CLEF shared-head feasibility fit completed 256 updates in 521.47 seconds on an A40.
Saved adapter/head hashes and quantized reload agreement were verified. Short, median and longest
training inputs (42, 361 and 10,656 tokens) reproduced their quantized outputs on repeated forwards.
These checks do not establish CPU performance or determinism across different hardware/batching.

An independent label audit then found that omitted typed-AST defaults incorrectly labelled ascending
order and inner joins as absent. All 32,659 executable records were regenerated: 2,467 ASC labels and
14,638 INNER labels changed across 16,318 records; raw inputs and all other annotations stayed equal.
Every regenerated target passes Draft 2020-12 validation and an independent typed-field selector
audit. The first candidate remains an unqualified diagnostic artifact. Rescoring its fixed 64-record
predictions against corrected labels gives SQL-coordinate F1 **0.706** (114 TP, 38 FP, 57 FN), not SQL
answer accuracy. The controlled label-only repeat completed 256 updates in 519.34 seconds and passed
its quantized reload check. On the same diagnostic its SQL F1 is **0.720** (116 TP, 35 FP, 55 FN),
while MAE worsens from 0.04681 to 0.04896. INNER recall improves from 0/12 to 6/12; the one ASC
positive remains missed. Cross-run initial predictions differ on 460/13,760 quantized outputs despite
matching model source and dependency versions. Cross-run numerical identity remains unresolved.

Complete calibration subsequently evaluated all **5,131** records: SQL-coordinate F1 **0.74848**
(10,920 TP / 2,787 FP / 4,552 FN), expected-property binary F1 **0.98576**, and dialogue binary F1
**0.99589**. These sparse metrics conceal important failures: only **641/2,901** complete SQL
presence vectors match, and all **20 clarification cases** are predicted answerable. The seeded
256-update schedule contains no clarification, noun or column examples and only four SUM positives.
All 215 outputs remain unqualified. Serial versus full-calibration batched runs differ on
262/13,760 quantized coordinates; their separate allocation/load/order prevents causal attribution.
Both GPUs were terminated after recovering complete candidate artifacts and calibration predictions.

The fit has no real held-out noun/column/value binding qualification, no ACE-frame targets and no
trained recursive SQL-tree readout. No new candidate Spider/Enron accuracy has been established, and
no bundle has been promoted or deployed. Detailed hashes and denominators are in
[RESULTS.md](../spider/results/RESULTS.md).

The export/binding contract now requires explicit qualification per input role. Question-only
calibration cannot license column or value outputs; both the shared head and deterministic binder
enforce this, and the masks participate in the basis hash/replay. All saved pilot masks remain false.
The meaning head exports expected-concept evidence only; a neural prediction cannot attest an
observed instance fact. Independently verified observations retain their separate provenance.
The pinned CLEF configuration has 5,120 hidden features and 64 text layers (48 linear-attention,
16 full-attention); the named projection has 1,101,015 parameters and 215 outputs. Naming those outputs
does not remove the pretrained backbone's inference cost. CPU latency and memory remain unmeasured.

The original Qwen graph corpus also contains **6,481 question/program-token pairs**, recovered by
matching adjacent intent/role records and identical schema/metadata. Its **8,189** question-to-column
links resolve to program-token occurrences; **404** include exact literal-text identities. SQL strings
are not stored directly: role units contain unquoted identifiers/literals and omit filter literal types.
The original generators and quoted source records were subsequently recovered from prereasoner-flat-data
at `a2cb5dd30378a6ddcdb9aaace7cf334fb2ff8549`. All **6,481** match the saved graph annotations. Verified
original literal spellings and generator-declared numeric column requirements resolve the token-only
type exclusions. All pass SQL/ACE schema, codecs, synthetic changed-row parity, actual APE parsing
with exact tree agreement, and consistency with the stored ten-intent labels. The recovered eval
contains **670 unique pairs** after removing training question/SQL overlap, identical English questions
and eval duplicates; all pass the same gates. Separately, 118 of 874 original CSVs were recovered with
exact Git blob hashes. The original converter reproduces their historical row counts; schema identity
and source-SQL/AST/ACE parity pass for 812 training and 62 eval questions on their original row windows.
Another 756 CSVs await the archive's rate limit. Join schemas are synthetic. This is representation
and partial original-data evidence, not independent English-meaning certification. These sources
remain outside gradient training, and randomized source table aliases are not entity-class labels.
In addition, identical complete question/schema inputs have different gold output projections in
42 training and 16 eval records. Those require a projection policy, clarification or multiple
accepted programs before use as deterministic AST supervision. Representation validity does not
resolve this source-label ambiguity.

## Runtime Components

### Shared semantic encoder

- Base: `Qwen/Qwen2.5-0.5B`, encoder-only, pinned at Hugging Face revision
  `060db6499f32faf8b98477b0a26969ef7d8b9987`.
- Adaptation: a LoRA adapter (rank 32, alpha 64, on the q/k/v/o projections of all 24 layers,
  `engine/data/qwen_lora/`) trained jointly with `engine.encoder_model.RelationalModel`: one
  896→384 projection and 10 relational-attention blocks (6 heads, 384 wide). Both are frozen at
  serving. No decoder is used.
- Named coordinates: 90 output coordinates carry names (`encoder_meta.pt["alloc"]`; on this branch
  `engine/data/alloc.json` holds only the new reader's 83 schema.org properties): 9 structural
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
