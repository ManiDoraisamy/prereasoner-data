"""The owner's selected schema.org property basis as the reader's meaning tier, built for engine/data/named_coordinates.json (training/factory/build_names.py writes it): engine/data/alloc.json's "taxonomy" dimensions in that file's order, provenance training/props/, never the 1,521-property catalogue (docs/ARCHITECTURE.md, Shared Property Binding Migration). The catalogue is read here, through engine/schema_org.py, for each basis property's own text and ranges and for the classes the basis characterizes; nothing at serving reads it.

A property enters the basis only through alloc.json, when representing more schema.org classes needs it, with coverage and held-out evidence and the owner's OK.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BASIS = Path(__file__).resolve().parents[2] / "engine" / "data" / "alloc.json"
BASIS_FAMILY = "taxonomy"
# The documented migration of the allocation's two non-property names (main's engine/readers/layout.py:named_coordinate_contract, docs/ARCHITECTURE.md).
BASIS_MIGRATION = {"GeoCoordinates": "geo", "taxonName": "alternateName"}
# A table's class from the basis: each basis property the class itself is a domain of adds this much to its log odds over a bias an empty table stays under.
CLASS_WEIGHT = 4.0
CLASS_BIAS = -4.0


def basis() -> list[str]:
    """The selected properties, migrated, in alloc.json's dimension order."""
    with open(BASIS, encoding="utf-8") as source:
        document = json.load(source)
    return [BASIS_MIGRATION.get(row["name"], row["name"]) for row in sorted(document["dims"], key=lambda row: row["dim_id"]) if row["family"] == BASIS_FAMILY]


def _text(entry) -> str:
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", entry.label).lower()
    comment = " ".join(re.sub(r"<[^>]*>|\[\[|\]\]", "", entry.comment).split())
    return f"{words}: {comment}"


def property_text(name: str) -> str:
    """A current schema.org property's own text, written as the basis writes its own. An experiment's name set may hold a
    property outside the basis as a candidate (training/named/train.py --names); it enters the basis only through alloc.json."""
    from engine.named.names import MEANING_PREFIX
    from engine.schema_org import load_contract
    entry = load_contract().property(name.removeprefix(MEANING_PREFIX))
    if entry is None or entry.superseded_by:
        raise ValueError(f"not a current schema.org property: {name}")
    return _text(entry)


def meaning_tier() -> dict:
    """The basis as the runtime reads it: properties with their texts and fine types, classes as their superpositions, the fine types' classes."""
    from engine.named.names import FINE_CLASSES, MEANING_PREFIX, CLASS_PREFIX
    from engine.schema_org import load_contract, schema_name
    contract = load_contract()
    names = basis()
    if len(set(names)) != len(names):
        raise ValueError("a basis property twice")
    roots = {root: fine for fine, root in FINE_CLASSES.items()}

    def fine_of_class(name: str) -> str | None:
        found = {fine for root, fine in roots.items() if name == root or contract.is_subclass(name, root)}
        return found.pop() if len(found) == 1 else None

    properties = []
    for name in names:
        entry = contract.property(name)
        if entry is None:
            raise ValueError(f"a basis property outside schema.org {contract.version}: {name}")
        fines = {fine_of_class(schema_name(uri)) for uri in entry.ranges}
        properties.append({"name": f"{MEANING_PREFIX}{name}", "uri": entry.uri, "text": _text(entry), "fine": fines.pop() if len(fines) == 1 and None not in fines else None})
    held = set(names)
    classes = []
    for uri in contract.class_order:
        entry = contract.classes[uri]
        own = sorted(f"{MEANING_PREFIX}{schema_name(p)}" for p in entry.direct_properties if schema_name(p) in held)
        if own:
            classes.append({"name": f"{CLASS_PREFIX}{entry.name}", "bias": CLASS_BIAS, "signature": [{"property": p, "weight": CLASS_WEIGHT} for p in own]})
    fine_classes = {f"{CLASS_PREFIX}{contract.classes[uri].name}": fine for uri in contract.class_order if (fine := fine_of_class(contract.classes[uri].name))}
    return {"basis": {"source": "engine/data/alloc.json", "family": BASIS_FAMILY, "migration": BASIS_MIGRATION, "provenance": "training/props/", "schema_org": contract.version, "count": len(properties), "prefix": MEANING_PREFIX},
            "properties": properties, "classes": classes, "fine_classes": fine_classes}
