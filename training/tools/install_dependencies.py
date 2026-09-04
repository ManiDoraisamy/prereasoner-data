"""Install the reproducible CPU or GPU training dependency environment."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "training" / "requirements.lock.txt"
INPUT = ROOT / "training" / "requirements.txt"


def _expected_torch_version() -> str:
    match = re.search(r"(?m)^torch==([^\s#]+)", INPUT.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("training/requirements.txt must pin torch exactly")
    return match.group(1)


def _without_torch(lock_text: str) -> str:
    lines = lock_text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.startswith("torch==")), None)
    if start is None:
        raise RuntimeError("training lock does not contain torch")
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        end += 1
    return "".join(lines[:start] + lines[end:])


def _pip_install(requirements: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--require-hashes",
            "--no-deps",
            "-r",
            str(requirements),
        ],
        check=True,
    )


def install_gpu() -> None:
    import torch

    expected = _expected_torch_version()
    actual = torch.__version__.split("+", 1)[0]
    if actual != expected or not torch.cuda.is_available():
        raise SystemExit(
            f"GPU runner must provide CUDA-enabled torch {expected}; "
            f"found {torch.__version__} with cuda={torch.cuda.is_available()}"
        )
    with tempfile.TemporaryDirectory(prefix="prereasoner-training-") as directory:
        gpu_lock = Path(directory) / "requirements-gpu.lock.txt"
        gpu_lock.write_text(_without_torch(LOCK.read_text(encoding="utf-8")), encoding="utf-8")
        _pip_install(gpu_lock)


def install_cpu() -> None:
    _pip_install(LOCK)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("cpu", "gpu"))
    args = parser.parse_args()
    install_gpu() if args.target == "gpu" else install_cpu()


if __name__ == "__main__":
    main()
