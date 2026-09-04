# Training

This directory contains the model lineage and the one active training and promotion path. Start with
[`../docs/TRAINING.md`](../docs/TRAINING.md) for the architecture, then use
[`schema_org/README.md`](schema_org/README.md) for the runnable generalized Schema.org pipeline.

## What Ships

Serving uses one bundle:

- a Qwen2.5-0.5B LoRA encoder and relational readout from `props/`;
- an 80-URI Schema.org property head from `schema_org/`;
- deterministic class superposition, calibration thresholds, and source-grounding gates; and
- one hash manifest that binds the public artifacts to an immutable Hugging Face revision.

The encoder's 71 older property-named coordinates remain compatibility and ranking signals. They are
not the active class vocabulary. The promoted Schema.org head represents all 1,521 properties and 926
classes, releases only classes that pass evidence and heldout gates, and abstains on the rest.

## Current Pipeline

From the repository root, after building the synchronized source corpus:

```powershell
pip install --require-hashes -r training/requirements.lock.txt
python -m training.schema_org.corpus
python -m training.schema_org.train_property_head
python -m training.schema_org.promote <corpus-prefix> --revision <immutable-hf-commit>
python -m tests.test_schema_coverage
python -m tests.test_schema_decode
python -m tests.test_route_wired
```

Training writes candidates under `training/schema_org/data/experiments/`. Only `promote.py` may write
the serving artifacts in `engine/data/`. Promotion validates corpus isolation, validation-only model
selection, untouched heldout evidence, source and runtime fingerprints, and every artifact hash.

## CPU And GPU Environments

`training/requirements.txt` is the maintained CPU input. `training/requirements.lock.txt` is its
Linux, Python 3.11, hash-locked closure. The property-head optimizer and calibration run on CPU; the
expensive encoder pass can use a GPU.

GPU runs go through `training/tools/runpod_api.py lease`. It creates a time-bounded pod from the
digest-pinned runner image, verifies the image's CUDA-enabled Torch version, installs every other
dependency from the committed lock, and terminates the pod on success, failure, timeout, interruption,
or process exit. `--keep` is the explicit exceptional behavior.

```powershell
python -m training.tools.runpod_api lease --help
```

Never create an unowned training pod or train directly into `engine/data/`.

## Directory Map

| Path | Responsibility |
|---|---|
| `schema_org/` | Active corpus, URI property head, class calibration, and promotion |
| `props/` | Shared encoder lineage and historical property-router pipeline |
| `tools/` | Bounded remote execution and reproducible dependency installation |
| `lib/` | Encoder and graph components retained by the shared representation |
| `train/`, `corpus/`, `taxonomy/`, `anchor/`, `calibrate/`, `world/` | Historical gen20 warm-start lineage; not a second serving path |

The historical directories are retained because they produced the shared encoder inputs. Their old
`genN` names and taxonomy procedures do not describe the active Schema.org class model. Maintainer
history is in [`../docs/notes/training.md`](../docs/notes/training.md); it is not the runtime contract.

## Required External Inputs

- pinned Qwen base-model revision and promoted LoRA/readout artifacts;
- versioned source releases described in [`../docs/SOURCE_DATA.md`](../docs/SOURCE_DATA.md);
- corpus rows generated locally and intentionally excluded from Git; and
- credentials supplied through environment variables for source databases, Hugging Face publication,
  or a bounded RunPod lease.

No token, customer corpus, database dump, or generated checkpoint belongs in Git. See
[`../docs/DATA_CARD.md`](../docs/DATA_CARD.md), [`../docs/MODEL_CARD.md`](../docs/MODEL_CARD.md), and
[`../docs/OPEN_SOURCE_RELEASE.md`](../docs/OPEN_SOURCE_RELEASE.md) for the public artifact boundary.
