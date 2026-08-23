"""Held-out calculation intent/operand retrieval evaluation for the shared Qwen LoRA."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import torch

from training.lib.encoder import LiveQwen
from training.props.train_props_gpu import calculation_contrastive_accuracy


HERE = Path(__file__).resolve().parent
TRAIN_DIR = Path(os.environ.get("PREREASONER_TRAIN_DIR", str(HERE)))
DATA = TRAIN_DIR / "data"
ENGINE_DATA = Path(os.environ.get(
    "PREREASONER_ENGINE_DATA", str(HERE.parent.parent / "engine" / "data")
))


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ckpt", choices=("engine", "props"), default="engine")
    parser.add_argument("--lora", default=None)
    parser.add_argument("--eval", default=str(DATA / "calculation_contrastive_eval.jsonl"))
    parser.add_argument("--min-accuracy", type=float, default=0.80)
    args = parser.parse_args()
    rows = _load(Path(args.eval))
    lora = Path(args.lora) if args.lora else (
        ENGINE_DATA / "qwen_lora" if args.ckpt == "engine" else DATA / "qwen_lora_props"
    )
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    encoder = LiveQwen(device, warm_lora=str(lora), serving=True)
    accuracy, details = calculation_contrastive_accuracy(encoder, rows, device)
    print(f"ckpt={args.ckpt} device={device} rows={len(rows)} accuracy={accuracy:.3f}")
    for key, value in details.items():
        print(f"  {key:36s} {value:.3f}")
    if accuracy < args.min_accuracy:
        print(f"FAIL: accuracy {accuracy:.3f} is below {args.min_accuracy:.3f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

