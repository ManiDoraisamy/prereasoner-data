"""The reader: engine/encoder_model.py's relational attention over units, one head of named coordinates read by both sheet and question, the closed heads of a question, the word heads, and an open channel set apart.

A column's coordinates and a word's coordinates come from the same head, so a coordinate means the same on both sides, and a link is their product. The hidden state is working memory: nothing outside this module reads it except through a named head or, for proposals only, the open channel.
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


@dataclass(frozen=True)
class ReaderConfig:
    backbone: str = "bge-small"
    backbone_layers: int | None = None
    hidden: int = 384
    blocks: int = 3
    heads: int = 6
    open_dim: int = 64
    keep: int = 8
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

    def __init__(self, backbone_dim: int, coordinates: int, config: ReaderConfig):
        super().__init__()
        self.config = config
        self.relational = RelationalModel(in_dim=backbone_dim + FEATURES, H=config.hidden, layers=config.blocks, heads=config.heads, nc=config.hidden, n_edge=N_EDGE)
        self.coordinates = nn.Linear(config.hidden, coordinates)
        nn.init.constant_(self.coordinates.bias, _RARE)
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

    def coordinate_logits(self, hidden: torch.Tensor) -> torch.Tensor:
        return self.coordinates(hidden)

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
