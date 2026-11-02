"""The names the reader fills: the name set's coordinates, one head output each, and the closed heads of a question.

The name set is engine/data/named_coordinates.json, written by training/factory/build_names.py. The head holds its structure tier and its vocabulary pack, in sorted order, plus any schema.org name a training set's labels use: 1,521 properties would cost index size for names the labels barely reach (7 uses on validation). A bundle records its own list and the sha of that list, so an index built by one head cannot be read by another.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from engine.program.names import (AGGREGATES, CALC_OPS, COMPARISONS, DIRECTIONS, FORMS, PROGRAM_ANSWER_TYPES, RANK_ENTITIES, ROW_OPS, SIGNED, SIZES, SLOT_AGGREGATE, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FORM, SLOT_ROW_OP, SLOT_SIZE, WORD_ROLES, slot_of)

NAME_SET_PATH = Path(__file__).resolve().parents[1] / "data" / "named_coordinates.json"
NONE = "none"
LEFT = "left"
RIGHT = "right"
MAX_CONDITIONS = 6

# One choice per question. A head with NONE may stay silent; the rest always say something.
CLOSED_HEADS: dict[str, tuple[str, ...]] = {
    SLOT_FORM: FORMS,
    SLOT_AGGREGATE: (NONE, *AGGREGATES),
    SLOT_DIRECTION: (NONE, *DIRECTIONS),
    SLOT_SIZE: SIZES,
    SLOT_ENTITY: (NONE, *RANK_ENTITIES),
    SLOT_COMPARISON: (NONE, *COMPARISONS),
    SLOT_ROW_OP: (NONE, *ROW_OPS),
    SLOT_CALC_OP: (NONE, *CALC_OPS),
    SLOT_ANSWER_TYPE: PROGRAM_ANSWER_TYPES,
    SLOT_BLANK: (NONE, "blank", "filled", "number"),
    SLOT_CONDITIONS: tuple(str(count) for count in range(MAX_CONDITIONS + 1)),
}
_DEFAULTS = {SLOT_SIZE: SIGNED, SLOT_CONDITIONS: "0"}
# One choice per word.
WORD_ROLE_CLASSES = (NONE, *WORD_ROLES)
CALC_SIDES = (NONE, LEFT, RIGHT)


def closed_labels(coordinates: Iterable[str]) -> dict[str, str]:
    """A gold program's coordinates as one class per closed head. In a calc, its first aggregate, comparison and the like: the left side's, as the program states them first."""
    out = {head: _DEFAULTS.get(head, NONE) for head in CLOSED_HEADS}
    seen: set[str] = set()
    for name in coordinates:
        slot = slot_of(name)
        if slot not in CLOSED_HEADS or slot in seen:
            continue
        body = name.split(":", 1)[1]
        value = body.rsplit("=", 1)[1] if slot == SLOT_CONDITIONS else body.split("@", 1)[0]
        if value in CLOSED_HEADS[slot]:
            out[slot] = value
            seen.add(slot)
    return out


@dataclass(frozen=True)
class NameSet:
    """The head's coordinates in order. at() is a coordinate's output index; a name the head lacks has none."""
    names: tuple[str, ...]
    positions: dict[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if len(set(self.names)) != len(self.names):
            raise ValueError("a name twice in a name set")
        object.__setattr__(self, "positions", {name: at for at, name in enumerate(self.names)})

    def __len__(self) -> int:
        return len(self.names)

    def at(self, name: str) -> int | None:
        return self.positions.get(name)

    @property
    def sha(self) -> str:
        return hashlib.sha256("\n".join(self.names).encode("utf-8")).hexdigest()


def load_name_set(path: str | Path = NAME_SET_PATH, extra: Iterable[str] = ()) -> NameSet:
    """The head's names from the name set: the structure tier, the vocabulary, and the extra names a training set's labels use."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("unsupported name set")
    tiers = document["tiers"]
    names = {row["name"] for row in tiers["structure"]["names"]} | {row["name"] for row in tiers["meaning"]["vocabulary"]["names"]} | set(extra)
    return NameSet(tuple(sorted(names)))


def meaning_groups(path: str | Path = NAME_SET_PATH) -> dict[str, tuple[str, ...]]:
    """The vocabulary's groups: the classes a table can be read as, each the superposition of its names."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = {}
    for row in document["tiers"]["meaning"]["vocabulary"]["names"]:
        groups.setdefault(row["group"], []).append(row["name"])
    return {group: tuple(sorted(members)) for group, members in sorted(groups.items())}


__all__ = ["CALC_SIDES", "CLOSED_HEADS", "LEFT", "MAX_CONDITIONS", "NAME_SET_PATH", "NONE", "NameSet", "RIGHT", "WORD_ROLE_CLASSES", "closed_labels", "load_name_set", "meaning_groups"]
