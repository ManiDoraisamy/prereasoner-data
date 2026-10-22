---
license: apache-2.0
base_model:
  - Qwen/Qwen2.5-0.5B
  - FacebookAI/xlm-roberta-base
  - FacebookAI/xlm-roberta-large
library_name: peft
tags:
  - text-to-sql
  - table-question-answering
  - schema.org
  - lora
---

# Prereasoner Runtime Weights

This repository contains the public, manifest-pinned runtime weights for
[Prereasoner](https://github.com/ManiDoraisamy/prereasoner-data), an interpretable
table-question-answering system. Learned components produce semantic evidence: named coordinates
for columns and question words, Schema.org property and class scores, and the own-data readers'
readings of a question (which tables, columns, values, groups, orders and calculations it asks for).
A deterministic typed AST builder makes every query from those readings and owns joins, calculation
verification, rendering, and execution. No model in this bundle writes SQL, and the bundle is not a standalone text-to-SQL model.

## Install

No Hugging Face account or token is required:

```bash
git clone https://github.com/ManiDoraisamy/prereasoner-data.git
cd prereasoner-data
python -m engine.fetch_weights
```

`engine/data/weights_manifest.json` pins this repository at an immutable commit and records the
SHA-256 digest of every required file. The fetcher downloads into a temporary directory, verifies
the complete bundle, and only then installs it.

## Bundle Contents

| File | Purpose |
|---|---|
| `encoder.pt` | State dictionary for the relational semantic readout |
| `encoder_meta.pt` | Readout allocation and constructor configuration |
| `qwen_lora/adapter_model.safetensors` | LoRA adapter for `Qwen/Qwen2.5-0.5B` |
| `qwen_lora/adapter_config.json` | PEFT adapter configuration |
| `anchor_assignment.npz` | Calibrated named-dimension thresholds |
| `primitives.npz` | Learned primitive-composition head |
| `schema_property_head.pt` | Calibrated Schema.org named-property evidence head |
| `readers/stage1/` | Own-data schema-item model (XLM-R base): scores each table's and column's use |
| `readers/stage2_a/` to `readers/stage2_d/` | Four own-data reading models (XLM-R large); each `reader.json` says whether it reads the question's values beside their columns |
| `readers/rescorer/` | Re-scorer (XLM-R base): scores the readings' queries that ran |
| `readers/choice.json` | The readers' choice: a logistic model over the re-scorer's logit and how many readers build a query, fitted on held-out calibration databases; it ranks the queries and refuses below its threshold |

The source repository contains the small ontology, calibration, taxonomy, and manifest artifacts.
The base Qwen model is downloaded separately from its publisher. The readers' directories hold their
complete fine-tuned weights (encoders in bfloat16, heads in float32) and tokenizer files. `python -m engine.fetch_weights`
downloads only the files the source manifest lists. Prereasoner loads no SQL-generating model; the
retired SQL proposer adapter (`sql_proposer/`) was removed from this repository on 2026-10-02 and
remains in its history.

## Model Boundary

The encoder adapter emits named Schema.org property probabilities, calibrated class proposals, and
embeddings for column typing and the structural intent of composition and world questions. A released
class may propose a coarse resolver family, but no score can authorize a table, join, calculation, or
answer. Own-data questions are read by separate XLM-R readers (`engine/data/readers/`); a deterministic
builder makes each of their readings a typed query, and no model writes SQL. The optional Gemini model
serves only the chat orchestrator, starter questions and reference generation, and is not part of
this bundle.
Deterministic code applies ontology mapping, exact source grounding, typed constraints, abstention
rules, and execution checks.

Schema.org 30.0 supplies the named semantic coordinate system. Wikidata and publisher datasets
supply observations mapped into those coordinates; mutable source facts are not intended to be
memorized as answers.

## Evaluation And Provenance

See the source repository's
[model card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/MODEL_CARD.md),
[data card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/DATA_CARD.md), and
[Spider results](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/spider/results/RESULTS.md)
for component boundaries, denominators, and limitations.

The bytes in this bundle are immutable and hash-verified. The promoted Schema.org head has a
machine-readable corpus, split, seed, dependency, encoder, and held-out metric manifest. The shared
encoder's historical training run has less completely recorded source-corpus provenance; do not combine metrics from the two
tracks. All 926 Schema.org classes are representable, but only the released calibrated subset is
servable. Unsupported and under-calibrated coordinates abstain.

## Intended Use

- Semantic evidence for the matching Prereasoner source revision.
- Research on deterministic SQL planning informed by named learned dimensions.
- Local table and spreadsheet question answering with inspectable plans.

## Out Of Scope

- Standalone SQL or answer generation.
- Authoritative identity or entity classification.
- High-stakes medical, legal, tax, financial, or safety decisions without source-specific review.
- Using the files with an unverified or incompatible source revision.

## License

The Prereasoner weight bundle is released under Apache-2.0. The base model and source datasets retain
their own licenses and terms. The readers in `readers/` are fine-tuned from XLM-RoBERTa (MIT) on Spider's
train and dev questions (Yu et al., EMNLP 2018; CC BY-SA 4.0), BIRD train (Li et al., NeurIPS 2023;
CC BY-SA 4.0) and Spider-Syn (Gan et al., ACL 2021; MIT), and on questions Gemini on Vertex AI wrote
over those training databases; keep those attributions when redistributing them. The Spider test set
appears in no training step. The retired `sql_proposer/` adapter in earlier revisions was trained on
Spider TRAIN (Yu et al., EMNLP 2018; CC BY-SA 4.0); keep that attribution when redistributing it. See
[`THIRD_PARTY.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/THIRD_PARTY.md).
