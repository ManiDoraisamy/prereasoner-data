---
base_model: Qwen/Qwen2.5-0.5B
library_name: peft
license: apache-2.0
tags:
  - base_model:adapter:Qwen/Qwen2.5-0.5B
  - lora
---

# Prereasoner Qwen Adapters

This revision of the
[Prereasoner runtime bundle](https://huggingface.co/prereasoner/prereasoner-weights) carries two LoRA
adapters on `Qwen/Qwen2.5-0.5B`:

- `qwen_lora/` adapts the base as an encoder whose representation feeds named semantic readouts.
  Current Prereasoner loads it.
- `sql_proposer/` is a retired causal-LM adapter that decoded candidate SQL for own-data questions in
  an earlier design. Current Prereasoner loads neither it nor any other SQL model: a deterministic
  search builds every query. The source manifest does not list the adapter, so the fetcher does not
  download it; it remains in this published revision.

Install and validate the complete compatible bundle through the source repository:

```bash
python -m engine.fetch_weights
```

Do not copy this subdirectory independently or mix it with a different bundle revision. The source
manifest pins the adapter, readouts, thresholds, and ontology artifacts together. Training data,
evaluation boundaries, known provenance gaps, intended use, and third-party notices are documented
in the [source model card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/MODEL_CARD.md)
and [`THIRD_PARTY.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/THIRD_PARTY.md).
