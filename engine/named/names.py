"""The names the reader fills: the structure tier's closed names, one head output each; the meaning tier's schema.org properties, one each, read as how near a unit stands to the property's own label and comment; and the closed heads of a question.

The structure tier is engine/data/named_coordinates.json, written by training/factory/build_names.py from the program's and the sheet's closed lists. The meaning tier is schema.org 30.0's 1,521 properties, read from their contract (engine/data/schema_org_v30.json) in its order: a property is a named dimension, the same in every language, its meaning its own text. A table's class is a schema.org class, the superposition of its properties (class_signatures). No vocabulary of any trade or language is kept.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from engine.program.names import (A_CODE, A_COMPANY, A_COUNT, A_DATE, A_LIST, A_MONEY, A_NAME, A_NUMBER, A_PERCENT, A_PERSON, A_PLACE, A_QUANTITY, AGGREGATES, CALC_OPS, COMPARISONS, DIRECTIONS, FORMS, RANK_ENTITIES, ROW_OPS, SIGNED, SIZES, SLOT_AGGREGATE, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FORM, SLOT_ROW_OP, SLOT_SIZE, WORD_ROLES, coarse_type, slot_of)

DATA = Path(__file__).resolve().parents[1] / "data"
NAME_SET_PATH = DATA / "named_coordinates.json"
SCHEMA_PATH = DATA / "schema_org_v30.json"
MEANING_PREFIX = "schema:"
CLASS_PREFIX = "class:"
NONE = "none"
LEFT = "left"
RIGHT = "right"
MAX_CONDITIONS = 6
# What a question asks for, as finely as it says: what a program can state is coarse (engine/program/names.py:coarse_type), and the head's classes add up to that.
ANSWER_READINGS = (A_NUMBER, A_MONEY, A_QUANTITY, A_COUNT, A_PERCENT, A_DATE, A_NAME, A_PERSON, A_COMPANY, A_CODE, A_PLACE, A_LIST)
# The fine answer types as schema.org classes: a value of the class, or of a class under it, is of that type. A property whose every range is one of them holds it.
FINE_CLASSES = {A_PERSON: "Person", A_COMPANY: "Organization", A_PLACE: "Place", A_MONEY: "MonetaryAmount", A_QUANTITY: "QuantitativeValue"}
# A table's class from its properties: each property the class itself is a domain of adds this much to its log odds over a bias that an empty table stays under.
CLASS_WEIGHT = 4.0
CLASS_BIAS = -4.0
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_MARKUP = re.compile(r"<[^>]*>|\[\[|\]\]")


@functools.lru_cache(maxsize=1)
def schema_contract() -> dict:
    """The ontology contract (engine/schema_org.py's file): properties and classes in a fixed order."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _name(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def meaning_names() -> tuple[str, ...]:
    """The meaning tier, one name per schema.org property, in the contract's order."""
    return tuple(f"{MEANING_PREFIX}{row['name']}" for row in schema_contract()["properties"])


def property_texts() -> tuple[str, ...]:
    """Each property's own text, as the backbone reads it: its label in words and its comment, markup cut. A property's coordinate is how near a unit stands to this text."""
    return tuple(f"{_CAMEL.sub(' ', row['label']).lower()}: {' '.join(_MARKUP.sub('', row['comment']).split())}" for row in schema_contract()["properties"])


@functools.lru_cache(maxsize=1)
def _fines() -> dict[str, str]:
    contract = schema_contract()
    ancestors = {row["name"]: {row["name"], *(_name(uri) for uri in row["ancestors"])} for row in contract["classes"]}

    def fine_of_class(name: str) -> str | None:
        found = {fine for fine, held in FINE_CLASSES.items() if held in ancestors.get(name, ())}
        return found.pop() if len(found) == 1 else None

    out = {f"{CLASS_PREFIX}{name}": fine for name in ancestors if (fine := fine_of_class(name))}
    for row in contract["properties"]:
        found = {fine_of_class(_name(uri)) for uri in row["ranges"]}
        if len(found) == 1 and None not in found:
            out[f"{MEANING_PREFIX}{row['name']}"] = found.pop()
    return out


def fine_of(name: str) -> str | None:
    """The fine answer type a property or a class name holds by the ontology alone, or None."""
    return _fines().get(name)


def fine_answer_type(coarse: str, names_of_answer: Iterable[str]) -> str:
    """A gold program's coarse type made finer by the schema.org names of what it answers with (a property, or its values' class), where those agree with it. No such name leaves the coarse type."""
    for name in names_of_answer:
        fine = fine_of(name)
        if fine is not None and coarse_type(fine) == coarse:
            return fine
    return coarse


def class_signatures() -> dict[str, dict]:
    """Every schema.org class with a property of its own, as the superposition of those properties: {class:<name>: {bias, signature: [{property, weight}]}}."""
    out = {}
    for row in schema_contract()["classes"]:
        own = sorted(f"{MEANING_PREFIX}{_name(uri)}" for uri in row["direct_properties"])
        if own:
            out[f"{CLASS_PREFIX}{row['name']}"] = {"bias": CLASS_BIAS, "signature": [{"property": name, "weight": CLASS_WEIGHT} for name in own]}
    return out


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
    """The head's names: the structure tier from the name set, sorted, then the meaning tier, schema.org's properties in the contract's order unless given."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 2:
        raise ValueError("unsupported name set")
    structure = tuple(sorted(row["name"] for row in document["tiers"]["structure"]["names"]))
    return NameSet(structure + tuple(meaning_names() if meaning is None else meaning), len(structure))


__all__ = ["ANSWER_READINGS", "CALC_SIDES", "CLASS_PREFIX", "CLOSED_HEADS", "FINE_CLASSES", "LEFT", "MAX_CONDITIONS", "MEANING_PREFIX", "NAME_SET_PATH", "NONE", "NameSet", "RIGHT", "WORD_ROLE_CLASSES", "class_signatures", "closed_labels", "fine_answer_type", "fine_of", "load_name_set", "meaning_names", "property_texts", "schema_contract"]
