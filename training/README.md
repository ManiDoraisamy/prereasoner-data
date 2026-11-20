# Training

This directory contains the model lineage and the active training and promotion paths. Start with
[`../docs/TRAINING.md`](../docs/TRAINING.md) for the architecture, then use
[`schema_org/README.md`](schema_org/README.md) for the runnable generalized Schema.org pipeline.

## What Ships

Serving uses one bundle:

- a Qwen2.5-0.5B LoRA encoder and relational readout from `props/`;
- an 80-URI Schema.org property head from `schema_org/`;
- deterministic class superposition, calibration thresholds, and source-grounding gates; and
- one hash manifest that binds the public artifacts to an immutable Hugging Face revision.

The encoder's 71 older property-named coordinates remain for compatibility. They are not the active
class vocabulary. The promoted Schema.org head represents all 1,521 properties and 926 classes,
releases only classes that pass evidence and heldout gates, and abstains on the rest.

Nothing here trains a model that writes SQL. Own-data questions take the Decisions path
(`engine/program`): stock bge-m3, untrained here, shortlists the options, and Gemini's Decisions API, an
external service like the chat orchestrator's Gemini, answers closed questions over them; no artifact from
this directory serves it. The trained readers it replaced (2026-11-20) are in git history.

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

`training/readers/data/experiments/<id>/` holds every measured run, the readers' and the Decisions path's
(gitignored), and `training/readers/lease.py` the one GPU lease loop `training/named` uses
([`readers/README.md`](readers/README.md)).

## CPU And GPU Environments

`training/requirements.txt` is the maintained CPU input. `training/requirements.lock.txt` is its
Linux, Python 3.11, hash-locked closure. The property-head optimizer and calibration run on CPU; the
expensive encoder pass can use a GPU.

GPU runs go through `training/tools/runpod_api.py lease`. It creates a time-bounded pod from the
digest-pinned runner image, verifies the image's CUDA-enabled Torch version, installs every other
dependency from the committed lock, and terminates the pod on success, failure, timeout, interruption,
or process exit. `--keep` is the explicit exceptional behavior, and a command that succeeded but whose
download did not finish keeps its pod until its provider deadline (exit 5): the pod holds the only copy.
The same lease with `--resume`, its `--download` and no command brings back what is missing, then
terminates the pod. Large `.jsonl` and `.pt` outputs travel gzipped and are restored on arrival.

```powershell
python -m training.tools.runpod_api lease --help
```

Never create an unowned training pod or train directly into `engine/data/`.

## Directory Map

| Path | Responsibility |
|---|---|
| `schema_org/` | Active corpus, URI property head, class calibration, and promotion |
| `readers/` | The experiments folder and the one GPU lease loop; the readers' pipeline went with the readers |
| `props/` | Shared encoder lineage and historical property-router pipeline |
| `tools/` | Bounded remote execution and reproducible dependency installation |
| `lib/` | Encoder and graph components retained by the shared representation |
| `train/`, `corpus/`, `taxonomy/`, `anchor/`, `calibrate/` | Historical gen20 warm-start lineage; not a second serving path |

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
