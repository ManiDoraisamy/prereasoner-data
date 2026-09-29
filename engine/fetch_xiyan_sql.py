"""Fetch and verify the pinned public XiYanSQL GGUF used by the production SQL proposer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(contract_path: Path, output: Path) -> str:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    # Provision the exact tokenizer even when the large model is already cached.
    # Serving stays offline; Docker and local setup share this one source of pins.
    from transformers import AutoTokenizer

    AutoTokenizer.from_pretrained(
        contract["tokenizer"]["repository"], revision=contract["tokenizer"]["revision"],
        token=os.environ.get("HF_TOKEN") or None,
    )
    artifact = contract["gguf"]
    expected = artifact["sha256"]
    if output.is_file() and output.stat().st_size == artifact["size_bytes"]:
        actual = sha256(output)
        if actual == expected:
            return actual
    from huggingface_hub import hf_hub_download

    cached = Path(hf_hub_download(
        repo_id=artifact["repository"], filename=artifact["filename"],
        revision=artifact["revision"], token=os.environ.get("HF_TOKEN") or None,
    ))
    if cached.stat().st_size != artifact["size_bytes"]:
        raise RuntimeError("downloaded XiYanSQL GGUF has an unexpected size")
    actual = sha256(cached)
    if actual != expected:
        raise RuntimeError(f"XiYanSQL GGUF SHA-256 mismatch: {actual}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(cached, staged)
        if sha256(staged) != expected:
            raise RuntimeError("staged XiYanSQL GGUF failed its SHA-256 check")
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=Path(__file__).parent / "data" /
                        "xiyan_sql_proposer.json")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "data" /
                        "xiyan_sql_proposer.gguf")
    args = parser.parse_args()
    print(f"XiYanSQL GGUF ready: {fetch(args.contract, args.out)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
