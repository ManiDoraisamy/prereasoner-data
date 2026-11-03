"""A bundle: a reader's numbers as JSON (the chooser's, the alignment's weights under "align" and the readers that propose beside the decoder under "readers" where a bundle has them), its tensors as a state dict, in one directory. training/named writes candidates; promotion to the runtime is a separate, approved act.

The backbone's frozen word table stays out of a bundle: it is the named base model's, and a third to two thirds of the encoder's bytes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import torch

from engine.named.align import AlignWeights
from engine.named.backbone import BACKBONES, TextBackbone, load_backbone
from engine.named.chooser import PRIOR, Chooser
from engine.named.index import ClassSignatures
from engine.named.links import LinkWeights
from engine.named.model import NamedReader, ReaderConfig
from engine.named.names import NameSet
from engine.program.scoring import DEFAULT_WEIGHTS, Weights

BUNDLE_META = "named_bundle.json"
BUNDLE_STATE = "reader.pt"


@dataclass
class Bundle:
    """Everything a bundle brings back."""
    backbone: TextBackbone
    reader: NamedReader
    names: NameSet
    config: ReaderConfig
    weights: LinkWeights
    chooser: Chooser
    signatures: ClassSignatures
    slot_weights: Weights
    reader_id: str
    align: AlignWeights | None = None
    readers: str | None = None          # a folder of today's readers that propose beside the decoder (engine/named/proposals.py); its chooser weighs their score


def write_bundle(folder: str | Path, backbone: TextBackbone, reader: NamedReader, names: NameSet, config: ReaderConfig, weights: LinkWeights, *, chooser: Chooser | None = None, slot_weights: Weights | None = None, signatures: ClassSignatures | None = None, record: dict | None = None) -> None:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    frozen = {name for name, parameter in backbone.named_parameters() if not parameter.requires_grad}
    state = {f"backbone.{name}": value.cpu() for name, value in backbone.state_dict().items() if name not in frozen}
    state.update({f"reader.{name}": value.cpu() for name, value in reader.state_dict().items() if not name.startswith("raw_")})
    torch.save(state, folder / BUNDLE_STATE)
    document = {"config": config.record(), "names": list(names.names), "names_sha": names.sha, "weights": weights.record(), "slot_weights": dict(sorted((slot_weights or DEFAULT_WEIGHTS).by_slot.items())), "slot_bias": (slot_weights or DEFAULT_WEIGHTS).bias, "chooser": (chooser or PRIOR).record(), "signatures": dict((signatures or ClassSignatures({})).classes), "backbone": backbone.name, "record": record or {}}
    (folder / BUNDLE_META).write_text(json.dumps(document, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def bundle_id(folder: str | Path) -> str:
    """A bundle's identity: its tensors, its config, its names. An index built under one is not read under another."""
    folder = Path(folder)
    digest = hashlib.sha256()
    digest.update((folder / BUNDLE_STATE).read_bytes())
    document = json.loads((folder / BUNDLE_META).read_text(encoding="utf-8"))
    digest.update(json.dumps({key: document[key] for key in ("config", "names_sha", "backbone")}, sort_keys=True).encode())
    return digest.hexdigest()


def read_bundle(folder: str | Path, *, backbone: TextBackbone | None = None) -> Bundle:
    """A bundle back, float32. backbone: an encoder already in hand; else the named one from local files. One model load at a time."""
    folder = Path(folder)
    document = json.loads((folder / BUNDLE_META).read_text(encoding="utf-8"))
    config = ReaderConfig(**document["config"])
    names = NameSet(tuple(document["names"]))
    if names.sha != document["names_sha"]:
        raise ValueError("a bundle's names do not match their sha")
    if backbone is None:
        if config.backbone not in BACKBONES:
            raise ValueError(f"unknown backbone: {config.backbone}")
        backbone = load_backbone(config.backbone, layers=config.backbone_layers)
    state = torch.load(folder / BUNDLE_STATE, map_location="cpu", weights_only=True)
    backbone.load_state_dict({name[len("backbone."):]: value for name, value in state.items() if name.startswith("backbone.")}, strict=False)
    reader = NamedReader(backbone.dim, len(names), config).float()
    fit = reader.load_state_dict({name[len("reader."):]: value for name, value in state.items() if name.startswith("reader.")}, strict=False)
    if any(not name.startswith("raw_") for name in fit.missing_keys) or fit.unexpected_keys:
        raise ValueError(f"a bundle's state does not fit its reader: {fit}")
    align = AlignWeights.from_record(document["align"]) if document.get("align") else None
    readers = (document.get("readers") or {}).get("folder")
    return Bundle(backbone, reader, names, config, LinkWeights.from_record(document["weights"]), Chooser.from_record(document["chooser"]), ClassSignatures(document.get("signatures") or {}), Weights(dict(document["slot_weights"]), float(document.get("slot_bias", 0.0))), bundle_id(folder), align, readers)


__all__ = ["BUNDLE_META", "BUNDLE_STATE", "Bundle", "bundle_id", "read_bundle", "write_bundle"]
