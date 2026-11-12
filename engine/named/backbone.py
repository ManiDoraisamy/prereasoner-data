"""The one text encoder of both sides: bge-small (33M parameters) or XLM-R base (278M), chosen by name.

A text's vector is the mean of its tokens' last hidden states; a question's words are the same, over each word's own tokens. float32 always. Distinct texts are read once, in an order fixed by the texts themselves, so a batch never depends on what else a sheet holds or on the sheet's order.
Only local files: nothing here downloads.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import torch
from torch import nn

WORD = re.compile(r"[^\W_]+")     # a word of any script, as engine/program/index.words_of reads the sheet


def question_words(question: str) -> list[str]:
    """A question's words, the factory's own reading of them (training/factory/assemble.py:words_in), which link spans index."""
    return WORD.findall(question.casefold())


@dataclass(frozen=True, slots=True)
class BackboneSpec:
    name: str
    hub: str
    dim: int
    layers: int
    parameters: int
    table_parameters: int
    revision: str


BACKBONES = {"bge-small": BackboneSpec("bge-small", "BAAI/bge-small-en-v1.5", 384, 12, 33_360_000, 11_720_448, "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"),
             "xlmr-base": BackboneSpec("xlmr-base", "FacebookAI/xlm-roberta-base", 768, 12, 278_043_648, 192_001_536, "e73636d4f797dec63c3081bb6ed5c7b0bb3f2089")}
_LOCAL = {"xlmr-base": "C:/work/datasets/models/xlm-roberta-base"}


def source_of(spec: BackboneSpec) -> tuple[str, bool]:
    """Where a backbone loads from, and whether only local files will do. NAMED_BACKBONE_DIR/<name> first, as a pod's own copy; on a machine with no copy, the hub, which only a lease may reach."""
    root = os.environ.get("NAMED_BACKBONE_DIR")
    if root and (Path(root) / spec.name).exists():
        return str(Path(root) / spec.name), True
    local = _LOCAL.get(spec.name)
    if local and Path(local).exists():
        return local, True
    return spec.hub, not os.environ.get("NAMED_BACKBONE_ONLINE")


class TextBackbone(nn.Module):
    """A transformers encoder and its tokenizer behind two calls: encode_texts and encode_questions. Load one at a time: a model build touches process-wide torch state."""

    def __init__(self, encoder: nn.Module, tokenizer, *, name: str = ""):
        super().__init__()
        self.encoder = encoder
        self.tokenizer = tokenizer
        self.name = name
        self.dim = int(encoder.config.hidden_size)

    def freeze_table(self) -> None:
        """The word table stays the base model's: a third of bge-small and two thirds of XLM-R base, never fine-tuned, so a checkpoint need not carry it."""
        self.encoder.get_input_embeddings().weight.requires_grad_(False)

    def forward_tokens(self, ids: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        return self.encoder(input_ids=ids, attention_mask=mask).last_hidden_state.float()

    def encode_texts(self, texts: list[str], *, max_tokens: int = 32, batch: int = 32) -> tuple[list[str], torch.Tensor]:
        """(the distinct texts in reading order, their vectors). Sorted by length then text, so the batches are the same whatever order or sheet they came from."""
        distinct = sorted(set(texts), key=lambda text: (len(text), text))
        device = next(self.encoder.parameters()).device
        parts = []
        for start in range(0, len(distinct), batch):
            encoded = self.tokenizer(distinct[start:start + batch], padding=True, truncation=True, max_length=max_tokens, return_tensors="pt")
            ids, mask = encoded["input_ids"].to(device), encoded["attention_mask"].to(device)
            hidden = self.forward_tokens(ids, mask)
            weight = mask.unsqueeze(-1).float()
            parts.append((hidden * weight).sum(1) / weight.sum(1).clamp(min=1.0))
        return distinct, (torch.cat(parts) if parts else torch.zeros((0, self.dim), device=device))

    def encode_questions(self, questions: list[str], *, max_tokens: int = 64) -> list[tuple[list[str], torch.Tensor]]:
        """Per question, (its words, one vector per word): the mean over the word's own tokens. A word past the token limit reads as zeros."""
        if not questions:
            return []
        device = next(self.encoder.parameters()).device
        encoded = self.tokenizer(questions, padding=True, truncation=True, max_length=max_tokens, return_tensors="pt", return_offsets_mapping=True)
        ids, mask = encoded["input_ids"].to(device), encoded["attention_mask"].to(device)
        hidden = self.forward_tokens(ids, mask)
        offsets = encoded["offset_mapping"].tolist()
        out = []
        for at, question in enumerate(questions):
            folded = question.casefold()
            matches = list(WORD.finditer(folded))
            spans = offsets[at]
            vectors = []
            for match in matches:
                tokens = [t for t, (start, end) in enumerate(spans) if end > start and start < match.end() and end > match.start()]
                vectors.append(hidden[at, tokens].mean(0) if tokens else torch.zeros(self.dim, device=device))
            out.append(([match.group(0) for match in matches], torch.stack(vectors) if vectors else torch.zeros((0, self.dim), device=device)))
        return out


def load_backbone(name: str, *, layers: int | None = None) -> TextBackbone:
    """A named backbone from local files, float32, its first ``layers`` kept when asked (speed_budget_tables.txt measures 3 and 6)."""
    from transformers import AutoModel, AutoTokenizer
    spec = BACKBONES[name]
    source, local_only = source_of(spec)
    revision = spec.revision if source == spec.hub else None          # a copy on disk is its own revision
    tokenizer = AutoTokenizer.from_pretrained(source, local_files_only=local_only, revision=revision)  # nosec B615: pinned above
    encoder = AutoModel.from_pretrained(source, local_files_only=local_only, add_pooling_layer=False, revision=revision).float()  # nosec B615: pinned above
    if layers is not None:
        encoder.encoder.layer = nn.ModuleList(list(encoder.encoder.layer)[:layers])
        encoder.config.num_hidden_layers = layers
    encoder.eval()
    return TextBackbone(encoder, tokenizer, name=name)


__all__ = ["BACKBONES", "BackboneSpec", "TextBackbone", "WORD", "load_backbone", "question_words", "source_of"]
