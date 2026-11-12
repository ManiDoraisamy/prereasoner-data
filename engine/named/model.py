"""The reader: engine/encoder_model.py's relational attention over units, one head of named coordinates read by both sheet and question, the closed heads of a question, the word heads, and an open channel set apart.

A column's coordinates and a word's coordinates come from the same head, so a coordinate means the same on both sides, and a link is their product. The head has two tiers. The structure tier: one sigmoid per closed name of the program and the sheet. The meaning tier: one share per property of the owner's selected schema.org basis (engine/data/alloc.json, never the 1,521-property catalogue), a softmax over them and a slot for none; a property's logit is how near the unit stands to the property's own text (names.property_texts, read by the backbone), from where the unit's own text puts it plus what the relational read adds. So a property no label ever named still reads by its meaning, and training moves where units stand, never what a property means. The hidden state is working memory: nothing outside this module reads it except through a named head or, for proposals only, the open channel.
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
MEANING_SCALE = 20.0              # the meaning tier's starting sharpness: a softmax over cosines at temperature 0.05, the zero-shot reading that ranked best
NONE_LEVEL = 0.8                  # the cosine at which none of the properties starts as likely as one of them
MEANING_FLOOR = 0.01              # served, a property's share under this says nothing: an index and a question keep only what is over it


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
        # where the relational read moves a unit from its own text, in the backbone's space: nothing at first
        self.place = nn.Linear(config.hidden, backbone_dim)
        nn.init.zeros_(self.place.weight)
        nn.init.zeros_(self.place.bias)
        self.register_buffer("descriptions", torch.zeros((meaning, backbone_dim)))
        self.log_meaning_scale = nn.Parameter(torch.tensor(math.log(MEANING_SCALE)))
        self.meaning_prior = nn.Parameter(torch.zeros(meaning))
        self.none_level = nn.Parameter(torch.tensor(NONE_LEVEL))
        self.none_read = nn.Linear(config.hidden, 1)
        nn.init.zeros_(self.none_read.weight)
        nn.init.zeros_(self.none_read.bias)
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

    def set_descriptions(self, vectors: torch.Tensor) -> None:
        """The properties' own texts as the backbone reads them, one row each in the meaning tier's order."""
        if vectors.shape != self.descriptions.shape:
            raise ValueError(f"descriptions of shape {tuple(vectors.shape)} for a head of {tuple(self.descriptions.shape)}")
        self.descriptions.copy_(nn.functional.normalize(vectors.float(), dim=-1))

    def meaning_logits(self, hidden: torch.Tensor, base: torch.Tensor) -> torch.Tensor:
        """[units, properties + 1]: each property's logit, then none's. base: the units' own texts as the backbone read them."""
        scale = self.log_meaning_scale.exp()
        place = nn.functional.normalize(base.float() + self.place(hidden), dim=-1)
        properties = scale * place @ self.descriptions.T + self.meaning_prior
        none = scale * self.none_level + self.none_read(hidden)
        return torch.cat([properties, none], dim=-1)

    def coordinate_probabilities(self, hidden: torch.Tensor, base: torch.Tensor) -> torch.Tensor:
        """[units, coordinates] in the name set's order: the structure tier's own probabilities, then each property's share (none's share left out)."""
        meaning = torch.softmax(self.meaning_logits(hidden, base), dim=-1)[..., :-1]
        return torch.cat([torch.sigmoid(self.structure(hidden)), meaning], dim=-1)

    def served_probabilities(self, hidden: torch.Tensor, base: torch.Tensor) -> torch.Tensor:
        """What an index and a question read keep: coordinate_probabilities with each property's share under MEANING_FLOOR at zero. Training reads them whole."""
        read = self.coordinate_probabilities(hidden, base)
        meaning = read[..., self.structure_count:]
        return torch.cat([read[..., :self.structure_count], torch.where(meaning >= MEANING_FLOOR, meaning, torch.zeros_like(meaning))], dim=-1)

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
