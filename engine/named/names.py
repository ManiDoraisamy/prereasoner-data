"""The names the reader fills: the structure tier's closed names, one head output each; the meaning tier's basis properties, one each, read as how near a unit stands to the property's own label and comment; and the closed heads of a question.

Both tiers are engine/data/named_coordinates.json, written by training/factory/build_names.py. The meaning tier is the owner's selected schema.org property basis, never the 1,521-property catalogue (docs/ARCHITECTURE.md): engine/data/alloc.json's "taxonomy" dimensions in that file's order, migrated to schema.org 30.0 properties. Each is a named dimension, the same in every language, its meaning its own text. A table's class is a schema.org class, the superposition of the basis properties it is a domain of. Nothing here reads the catalogue; no vocabulary of any trade or language is kept.
"""
from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from engine.program.names import (A_CODE, A_COMPANY, A_COUNT, A_DATE, A_LIST, A_MONEY, A_NAME, A_NUMBER, A_PERCENT, A_PERSON, A_PLACE, A_QUANTITY, AGGREGATES, CALC_OPS, COMPARISONS, DIRECTIONS, FORMS, RANK_ENTITIES, ROW_OPS, SIGNED, SIZES, SLOT_AGGREGATE, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FORM, SLOT_ROW_OP, SLOT_SIZE, WORD_ROLES, coarse_type, slot_of)

NAME_SET_PATH = Path(__file__).resolve().parents[1] / "data" / "named_coordinates.json"
MEANING_PREFIX = "schema:"
CLASS_PREFIX = "class:"
NONE = "none"
LEFT = "left"
RIGHT = "right"
MAX_CONDITIONS = 6
# What a question asks for, as finely as it says: what a program can state is coarse (engine/program/names.py:coarse_type), and the head's classes add up to that.
ANSWER_READINGS = (A_NUMBER, A_MONEY, A_QUANTITY, A_COUNT, A_PERCENT, A_DATE, A_NAME, A_PERSON, A_COMPANY, A_CODE, A_PLACE, A_LIST)
# The fine answer types as schema.org classes: a value of the class, or of a class under it, is of that type; a property whose every range is one of them holds it (written by build_names.py).
FINE_CLASSES = {A_PERSON: "Person", A_COMPANY: "Organization", A_PLACE: "Place", A_MONEY: "MonetaryAmount", A_QUANTITY: "QuantitativeValue"}


@functools.lru_cache(maxsize=4)
def _meaning(path: str = str(NAME_SET_PATH)) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 3:
        raise ValueError("unsupported name set")
    return document["tiers"]["meaning"]


def meaning_names() -> tuple[str, ...]:
    """The meaning tier, one name per basis property, in the basis's order."""
    return tuple(row["name"] for row in _meaning()["properties"])


def property_texts() -> tuple[str, ...]:
    """Each basis property's own text, as the backbone reads it. A coordinate is how near a unit stands to this text."""
    return tuple(row["text"] for row in _meaning()["properties"])


@functools.lru_cache(maxsize=1)
def _fines() -> dict[str, str]:
    meaning = _meaning()
    return {**{row["name"]: row["fine"] for row in meaning["properties"] if row["fine"]}, **meaning["fine_classes"]}


def fine_of(name: str) -> str | None:
    """The fine answer type a basis property or a class name holds by the ontology alone, or None."""
    return _fines().get(name)


def fine_answer_type(coarse: str, names_of_answer: Iterable[str]) -> str:
    """A gold program's coarse type made finer by the schema.org names of what it answers with (a property, or its values' class), where those agree with it. No such name leaves the coarse type."""
    for name in names_of_answer:
        fine = fine_of(name)
        if fine is not None and coarse_type(fine) == coarse:
            return fine
    return coarse


def class_signatures() -> dict[str, dict]:
    """Every schema.org class that is a domain of a basis property, as the superposition of those properties: {class:<name>: {bias, signature: [{property, weight}]}}."""
    return {row["name"]: {"bias": row["bias"], "signature": row["signature"]} for row in _meaning()["classes"]}


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
    SLOT_ANSWER_TYPE: ANSWER_READINGS,
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
    """The head's coordinates in order: the structure tier first, then the meaning tier from meaning_start on. at() is a coordinate's output index; a name the head lacks has none."""
    names: tuple[str, ...]
    meaning_start: int | None = None          # None: where the schema.org names begin
    positions: dict[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.meaning_start is None:
            object.__setattr__(self, "meaning_start", sum(not name.startswith(MEANING_PREFIX) for name in self.names))
        if len(set(self.names)) != len(self.names):
            raise ValueError("a name twice in a name set")
        if any(name.startswith(MEANING_PREFIX) for name in self.names[:self.meaning_start]) or not all(name.startswith(MEANING_PREFIX) for name in self.names[self.meaning_start:]):
            raise ValueError("a name set's meaning tier is its schema.org names, after the structure tier")
        object.__setattr__(self, "positions", {name: at for at, name in enumerate(self.names)})

    def __len__(self) -> int:
        return len(self.names)

    @property
    def meaning(self) -> int:
        return len(self.names) - self.meaning_start

    def at(self, name: str) -> int | None:
        return self.positions.get(name)

    @property
    def sha(self) -> str:
        return hashlib.sha256("\n".join(self.names).encode("utf-8")).hexdigest()


def load_name_set(path: str | Path = NAME_SET_PATH, meaning: Iterable[str] | None = None) -> NameSet:
    """The head's names: the structure tier from the name set, sorted, then the meaning tier, the basis in its order unless given."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 3:
        raise ValueError("unsupported name set")
    structure = tuple(sorted(row["name"] for row in document["tiers"]["structure"]["names"]))
    basis = tuple(row["name"] for row in document["tiers"]["meaning"]["properties"])
    return NameSet(structure + (basis if meaning is None else tuple(meaning)), len(structure))


__all__ = ["ANSWER_READINGS", "CALC_SIDES", "CLASS_PREFIX", "CLOSED_HEADS", "FINE_CLASSES", "LEFT", "MAX_CONDITIONS", "MEANING_PREFIX", "NAME_SET_PATH", "NONE", "NameSet", "RIGHT", "WORD_ROLE_CLASSES", "class_signatures", "closed_labels", "fine_answer_type", "fine_of", "load_name_set", "meaning_names", "property_texts"]
