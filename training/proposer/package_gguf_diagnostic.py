"""Build an isolated, explicitly non-promotable GGUF proposer bundle for product diagnostics.

This does not fit or bless an arbiter. It copies the supplied engine data bundle, changes only
the copied arbiter's output-count/token contract to the requested experiment, and marks the
GGUF proposer/arbiter pair as unmatched in a manifest-pinned runtime.json. Production mode
rejects that pair. The package is self-contained, including the tokenizer snapshot.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from engine.artifact_provenance import (
    sha256_file,
    sha256_tree,
    validate_weight_bundle,
    write_json_artifact,
)


def package(args: argparse.Namespace) -> Path:
    source = args.source_bundle.resolve()
    output = args.output_bundle.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("output bundle must be separate from and outside the source bundle")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic bundle: {output}")
    if not (source / "weights_manifest.json").is_file():
        raise FileNotFoundError(f"source bundle lacks weights_manifest.json: {source}")
    if validate_weight_bundle(source) is None:
        raise ValueError("source bundle is not hash-verifiable")

    for path in (args.base_gguf, args.lora_gguf, args.tokenizer_snapshot,
                 args.source_contract):
        if not path.exists():
            raise FileNotFoundError(path)
    if not args.tokenizer_snapshot.is_dir():
        raise ValueError("tokenizer snapshot must be a directory")
    contract = json.loads(args.source_contract.read_text(encoding="utf-8"))
    selected_adapter = contract.get("selected_peft_adapter_sha256")
    if not isinstance(selected_adapter, str):
        raise TypeError("source contract lacks selected_peft_adapter_sha256")
    if sha256_file(args.base_gguf) != contract.get("base_gguf_sha256"):
        raise ValueError("base GGUF does not match the frozen source contract")
    if sha256_file(args.lora_gguf) != contract.get("lora_gguf_sha256"):
        raise ValueError("LoRA GGUF does not match the frozen source contract")
    if (contract.get("base_model") != args.tokenizer_model_id
            or contract.get("base_revision") != args.tokenizer_revision):
        raise ValueError("tokenizer identity differs from the frozen source contract")
    if contract.get("variant_count") != len(args.prompt_variant):
        raise ValueError("runtime prompt count differs from the frozen source contract")
    if contract.get("max_new_tokens") != args.max_new_tokens:
        raise ValueError("runtime token limit differs from the frozen source contract")

    shutil.copytree(source, output)
    model_dir = output / "sql_proposer" / "diagnostic_gguf"
    model_dir.mkdir(parents=True)
    shutil.copy2(args.base_gguf, model_dir / "base.gguf")
    shutil.copy2(args.lora_gguf, model_dir / "adapter.gguf")
    shutil.copytree(args.tokenizer_snapshot, model_dir / "tokenizer")

    arbiter_path = output / "sql_arbiter.json"
    arbiter = json.loads(arbiter_path.read_text(encoding="utf-8"))
    pool = arbiter.get("pool")
    if not isinstance(pool, dict):
        raise TypeError("copied arbiter is missing its pool contract")
    pool["proposer_beams"] = len(args.prompt_variant)
    pool["proposer_max_new_tokens"] = args.max_new_tokens
    write_json_artifact(arbiter_path, arbiter, indent=2)

    runtime_path = output / "sql_proposer" / "runtime.json"
    runtime = {
        "version": 1,
        "backend": "llama-cpp-gguf-lora",
        "protocol": "isolated-product-diagnostic-v1",
        "likelihood_protocol": "neutral-sentinel-v1",
        "model_matched_arbiter": False,
        "model_file": "sql_proposer/diagnostic_gguf/base.gguf",
        "model_file_sha256": sha256_file(model_dir / "base.gguf"),
        "lora_file": "sql_proposer/diagnostic_gguf/adapter.gguf",
        "lora_file_sha256": sha256_file(model_dir / "adapter.gguf"),
        "tokenizer_dir": "sql_proposer/diagnostic_gguf/tokenizer",
        "tokenizer_dir_sha256": sha256_tree(model_dir / "tokenizer"),
        "tokenizer_model_id": args.tokenizer_model_id,
        "tokenizer_revision": args.tokenizer_revision,
        "selected_peft_adapter_sha256": selected_adapter,
        "arbiter_fit_proposer_sha256": arbiter.get("fit", {}).get(
            "proposer_adapter_sha256"
        ),
        "arbiter_sha256": sha256_file(arbiter_path),
        "source_contract_sha256": sha256_file(args.source_contract),
        "max_new_tokens": args.max_new_tokens,
        "context_tokens": args.context_tokens,
        "cpu_threads": args.cpu_threads,
        "lora_scale": args.lora_scale,
        "system_prompt": args.system_prompt,
        "prompt_variants": args.prompt_variant,
        "thinking": args.thinking,
    }
    write_json_artifact(runtime_path, runtime, indent=2)

    weights_path = output / "weights_manifest.json"
    weights = json.loads(weights_path.read_text(encoding="utf-8"))
    files = weights.setdefault("files", {})
    committed = weights.setdefault("committed_artifacts", {})
    arbiter_sha = sha256_file(arbiter_path)
    if "sql_arbiter.json" in files:
        files["sql_arbiter.json"] = arbiter_sha
    elif "sql_arbiter.json" in committed:
        committed["sql_arbiter.json"]["sha256"] = arbiter_sha
    else:
        raise ValueError("source bundle does not pin sql_arbiter.json")
    committed["sql_proposer/runtime.json"] = {
        "note": "isolated diagnostic GGUF proposer runtime contract",
        "sha256": sha256_file(runtime_path),
    }
    # Every runtime dependency must be in the same artifact contract consumed by the
    # image builder and clean-clone fetcher. Pin tokenizer files individually so a
    # partial/missing snapshot cannot pass bundle validation.
    for artifact in sorted(path for path in model_dir.rglob("*") if path.is_file()):
        relative = artifact.relative_to(output).as_posix()
        files[relative] = sha256_file(artifact)
    # These artifacts have not been published at this HF revision. Prevent the ordinary
    # installer from silently fetching the old 0.5B bundle under this 7B runtime contract.
    weights["revision"] = None
    weights["unpublished_local"] = True
    write_json_artifact(weights_path, weights, indent=2)

    if validate_weight_bundle(output) is None:
        raise ValueError("diagnostic bundle failed manifest validation")
    return runtime_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output-bundle", type=Path, required=True)
    parser.add_argument("--base-gguf", type=Path, required=True)
    parser.add_argument("--lora-gguf", type=Path, required=True)
    parser.add_argument("--tokenizer-snapshot", type=Path, required=True)
    parser.add_argument("--source-contract", type=Path, required=True)
    parser.add_argument("--tokenizer-model-id", required=True)
    parser.add_argument("--tokenizer-revision", required=True)
    parser.add_argument("--system-prompt", required=True)
    parser.add_argument("--prompt-variant", action="append", required=True)
    parser.add_argument("--thinking", choices=("none", "false", "true"), default="none")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--context-tokens", type=int, default=8192)
    parser.add_argument("--cpu-threads", type=int, default=8)
    parser.add_argument("--lora-scale", type=float, default=1.0)
    args = parser.parse_args()
    args.thinking = {"none": None, "false": False, "true": True}[args.thinking]
    if not 1 <= args.max_new_tokens <= 256:
        parser.error("--max-new-tokens must be in 1..256")
    if not 512 <= args.context_tokens <= 8192:
        parser.error("--context-tokens must be in 512..8192")
    if not 1 <= args.cpu_threads <= 16:
        parser.error("--cpu-threads must be in 1..16")
    runtime_path = package(args)
    print(runtime_path)
    print(runtime_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
