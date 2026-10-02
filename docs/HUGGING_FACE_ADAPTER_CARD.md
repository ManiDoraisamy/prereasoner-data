---
base_model: Qwen/Qwen2.5-0.5B
library_name: peft
license: apache-2.0
tags:
  - base_model:adapter:Qwen/Qwen2.5-0.5B
  - lora
---

# Prereasoner Qwen Adapter

This directory of the
[Prereasoner runtime bundle](https://huggingface.co/prereasoner/prereasoner-weights) holds the LoRA
adapter that turns `Qwen/Qwen2.5-0.5B` into an encoder whose representation feeds named semantic
readouts. Prereasoner uses the base without its decoder and loads no SQL-generating model: a
deterministic search builds every query. A retired SQL proposer adapter (`sql_proposer/`) from an
earlier design was removed from this repository on 2026-10-02 and remains in its history.

Install and validate the complete compatible bundle through the source repository:

```bash
python -m engine.fetch_weights
```

Do not copy this subdirectory independently or mix it with a different bundle revision. The source
manifest pins the adapter, readouts, thresholds, and ontology artifacts together. Training data,
evaluation boundaries, known provenance gaps, intended use, and third-party notices are documented
in the [source model card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/MODEL_CARD.md)
and [`THIRD_PARTY.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/THIRD_PARTY.md).
