#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: $0 EXPECTED_COMMIT CORPUS LORA_DIR EMBEDDING_CACHE OUTPUT_DIR RUNNER_IMAGE" >&2
  exit 2
fi

expected_commit="$1"
corpus="$2"
lora_dir="$3"
embedding_cache="$4"
output_dir="$5"
runner_image="$6"
root="$(git rev-parse --show-toplevel)"

if [[ "$(git -C "$root" rev-parse HEAD)" != "$expected_commit" ]]; then
  echo "training checkout does not match expected commit $expected_commit" >&2
  exit 1
fi
if [[ -n "$(git -C "$root" status --porcelain)" ]]; then
  echo "training checkout is dirty before external artifacts are staged" >&2
  exit 1
fi
for required in "$corpus" "$lora_dir"; do
  if [[ ! -e "$required" ]]; then
    echo "missing training input: $required" >&2
    exit 1
  fi
done

python -m training.tools.install_dependencies gpu

mkdir -p "$root/training/schema_org/data" "$root/engine/data/qwen_lora" "$output_dir"
cp "$corpus" "$root/training/schema_org/data/semantic_instances.jsonl"
cp -a "$lora_dir/." "$root/engine/data/qwen_lora/"
if [[ -f "$embedding_cache" ]]; then
  cp "$embedding_cache" "$root/training/schema_org/data/schema_embeddings.npz"
fi

cd "$root"
python -m training.schema_org.signatures
python -m training.schema_org.train_property_head --runner-image "$runner_image"

candidate="$(python - <<'PY'
import json
from pathlib import Path

manifest = json.loads(Path("training/schema_org/data/semantic_manifest.json").read_text())
print(Path("training/schema_org/data/experiments") / manifest["corpus"]["sha256"][:12])
PY
)"
for artifact in \
  schema_property_head.pt \
  schema_property_model.json \
  schema_class_signatures.json \
  schema_training_manifest.json; do
  test -f "$candidate/$artifact"
  cp "$candidate/$artifact" "$output_dir/$artifact"
done
# Derived public-corpus embeddings are not a serving artifact, but returning the
# fingerprinted cache makes calibration iteration reproducible without another
# GPU pass. train_property_head validates the encoder hash before every reuse.
if [[ -f "$root/training/schema_org/data/schema_embeddings.npz" ]]; then
  cp "$root/training/schema_org/data/schema_embeddings.npz" "$output_dir/schema_embeddings.npz"
fi
