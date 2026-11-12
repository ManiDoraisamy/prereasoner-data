"""The reader: engine/encoder_model.py's relational attention over units, one head of named coordinates read by both sheet and question, the closed heads of a question, the word heads, and an open channel set apart.

A column's coordinates and a word's coordinates come from the same head, so a coordinate means the same on both sides, and a link is their product. The head has two tiers. The structure tier: one sigmoid per closed name of the program and the sheet. The meaning tier: one named output per property of the owner's selected schema.org basis (engine/data/alloc.json, never the 1,521-property catalogue), each its own sigmoid like a structure name, so a unit can hold several properties at once and a class reads as their mixture. A property's output reads the unit's own text as the backbone reads it (a weight row that starts as the property's own label and comment, names.property_texts, at MEANING_SCALE) plus what the relational read adds (zero at first); the labels of training anchor it to its name. The hidden state is working memory: nothing outside this module reads it except through a named head or, for proposals only, the open channel.
Link weights are parameters here while training (softplus, so a shared name always supports a link) and plain numbers in a bundle: see links.py.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import torch
from torch import nn

from engine.encoder_model import RelationalModel
from engine.fk_edges import N_EDGE
from engine.named.links import RULES
from engine.named.names import CALC_SIDES, CLOSED_HEADS, WORD_ROLE_CLASSES
from engine.named.units import FEATURES, UnitsConfig

_ONE = math.log(math.e - 1.0)   # softplus(_ONE) == 1
_RARE = math.log(0.01 / 0.99)    # a coordinate is rare: its output starts where that is true, so training does not spend itself getting there
LINK_KINDS = ("column", "value", "line", "band", "table")
MEANING_SCALE = 20.0              # a property's starting sharpness over the cosine of a unit's text to the property's own text
MEANING_LEVEL = 0.75              # the cosine at which a property starts even: a unit's text that far from the property's starts at one half


@dataclass(frozen=True)
class ReaderConfig:
    backbone: str = "bge-small"
    backbone_layers: int | None = None
    hidden: int = 384
    blocks: int = 3
    heads: int = 6
    open_dim: int = 64
    keep: int = 16
    text_tokens: int = 32
    question_tokens: int = 64
    sample_rows: int = 8
    window_columns: int = 48
    row_mates: int = 3
    value_cap: int = 2000

    @property
    def units(self) -> UnitsConfig:
        return UnitsConfig(self.sample_rows, self.window_columns, self.row_mates, self.value_cap)

    def record(self) -> dict:
        return asdict(self)


class NamedReader(nn.Module):
    """Everything after the backbone."""

    def __init__(self, backbone_dim: int, structure: int, meaning: int, config: ReaderConfig):
        super().__init__()
        self.config = config
        self.structure_count, self.meaning_count = structure, meaning
        self.relational = RelationalModel(in_dim=backbone_dim + FEATURES, H=config.hidden, layers=config.blocks, heads=config.heads, nc=config.hidden, n_edge=N_EDGE)
        self.structure = nn.Linear(config.hidden, structure)
        nn.init.constant_(self.structure.bias, _RARE)
        # one output per basis property over the unit's own text (rows start as the properties' own texts, set_descriptions) and over the relational read (zero at first)
        self.meaning_text = nn.Linear(backbone_dim, meaning)
        nn.init.zeros_(self.meaning_text.weight)
        nn.init.constant_(self.meaning_text.bias, _RARE)
        self.meaning_read = nn.Linear(config.hidden, meaning)
        nn.init.zeros_(self.meaning_read.weight)
        nn.init.zeros_(self.meaning_read.bias)
        coordinates = structure + meaning
        self.closed = nn.ModuleDict({head: nn.Linear(config.hidden, len(classes)) for head, classes in CLOSED_HEADS.items()})
        self.word_role = nn.Linear(config.hidden, len(WORD_ROLE_CLASSES))
        self.calc_side = nn.Linear(config.hidden, len(CALC_SIDES))
        self.open_word = nn.Linear(config.hidden, config.open_dim)
        self.open_target = nn.Linear(config.hidden, config.open_dim)
        self.raw_coordinate_weights = nn.Parameter(torch.full((coordinates,), _ONE))
        self.raw_rule_weights = nn.Parameter(torch.full((len(RULES),), _ONE))
        self.raw_role_weight = nn.Parameter(torch.tensor(_ONE))
        self.raw_closed_weight = nn.Parameter(torch.tensor(_ONE))
        self.raw_link_bias = nn.Parameter(torch.full((len(LINK_KINDS),), _ONE))

    def hidden(self, inputs: torch.Tensor, edges: torch.Tensor, pad: torch.Tensor) -> torch.Tensor:
        return self.relational(inputs, edges, pad)["content"]

    @torch.no_grad()
    def set_descriptions(self, vectors: torch.Tensor) -> None:
        """Start each property's output at its own text: its weight row the property's text as the backbone reads it, normalized, at MEANING_SCALE; its bias where a unit MEANING_LEVEL near starts at one half. Training moves them from there, anchored by labels."""
        if vectors.shape != self.meaning_text.weight.shape:
            raise ValueError(f"descriptions of shape {tuple(vectors.shape)} for a head of {tuple(self.meaning_text.weight.shape)}")
        self.meaning_text.weight.copy_(MEANING_SCALE * nn.functional.normalize(vectors.float(), dim=-1))
        self.meaning_text.bias.fill_(-MEANING_SCALE * MEANING_LEVEL)

    def meaning_logits(self, hidden: torch.Tensor, base: torch.Tensor) -> torch.Tensor:
        """[units, properties]: each basis property's own logit. base: the units' own texts as the backbone read them."""
        return self.meaning_text(nn.functional.normalize(base.float(), dim=-1)) + self.meaning_read(hidden)

    def coordinate_probabilities(self, hidden: torch.Tensor, base: torch.Tensor) -> torch.Tensor:
        """[units, coordinates] in the name set's order: every coordinate its own probability, the structure tier's and each basis property's."""
        return torch.cat([torch.sigmoid(self.structure(hidden)), torch.sigmoid(self.meaning_logits(hidden, base))], dim=-1)

    def coordinate_weights(self) -> torch.Tensor:
        return nn.functional.softplus(self.raw_coordinate_weights)

    def rule_weights(self) -> torch.Tensor:
        return nn.functional.softplus(self.raw_rule_weights)

    def role_weight(self) -> torch.Tensor:
        return nn.functional.softplus(self.raw_role_weight)

    def closed_weight(self) -> torch.Tensor:
        return nn.functional.softplus(self.raw_closed_weight)

    def link_biases(self) -> torch.Tensor:
        """What a link must reach, per kind of target, to say more than that the word links to nothing."""
        return nn.functional.softplus(self.raw_link_bias)

    def closed_logits(self, words_hidden: torch.Tensor, pad: torch.Tensor) -> dict[str, torch.Tensor]:
        """A question's closed heads read the mean of its words."""
        keep = (~pad).unsqueeze(-1).float()
        pooled = (words_hidden * keep).sum(1) / keep.sum(1).clamp(min=1.0)
        return {head: layer(pooled) for head, layer in self.closed.items()}


__all__ = ["LINK_KINDS", "NamedReader", "ReaderConfig"]
