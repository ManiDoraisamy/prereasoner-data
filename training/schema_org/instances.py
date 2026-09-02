"""Canonical, provenance-complete semantic examples shared by every source adapter."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from engine.schema_org import SchemaContract, schema_uri

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+#=-]{0,511}$")
_GROUP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+=-]{0,255}$")     # the separator itself is forbidden here
SPLITS = ("train", "validation", "test")

SPLIT_SALT = "schema-org-corpus:v6"     # fresh heldout split after reference-label and support audit
DERIVATION_SEP = "#"                    # everything right of the FIRST separator is invisible to the split
SPLIT_BOUNDARIES = ((80, "train"), (90, "validation"), (100, "test"))


def group_id(instance_id: str) -> str:
    """The SPLIT GROUP: everything left of the first derivation separator.

    Instances derived from the same underlying rows/entity (a table and its columns, a presentation
    variant, a row window, an anonymised twin) share one group and therefore ONE split draw."""
    return instance_id.split(DERIVATION_SEP, 1)[0]


def deterministic_split(instance_id: str) -> str:
    """Split by GROUP, not by instance.

    Keying on the instance let every derived instance draw independently, so a table could sit in train
    while a column built from the same rows sat in test — the column's text is a byte-identical substring
    of the table's, and its label is the same header->property mapping over the same column, so the head
    (one linear layer over a FROZEN encoder) could satisfy the held-out gate by memorising the surface
    form. `source` is deliberately NOT in the key: a derived family carries a different source string
    (`geonames` vs `geonames_columns`), which would split it away from its parent. Group ids are globally
    unique by construction instead."""
    group = group_id(instance_id)
    if not _GROUP_ID.fullmatch(group):
        raise ValueError(f"invalid split group id: {group!r}")
    bucket = int(hashlib.sha256(f"{SPLIT_SALT}\0{group}".encode()).hexdigest()[:8], 16) % 100
    for upper, name in SPLIT_BOUNDARIES:
        if bucket < upper:
            return name
    raise AssertionError("unreachable: bucket is bounded by the modulus")


@dataclass(frozen=True)
class SemanticInstance:
    source: str
    release_id: str
    relation: str
    instance_id: str
    text: str
    classes: tuple[str, ...]
    properties: tuple[str, ...]
    split: str
    mapping_version: str
    provenance_ids: tuple[str, ...] = ()

    @classmethod
    def create(cls, *, source: str, release_id: str, relation: str, instance_id: str,
               text: str, classes, properties, mapping_version: str,
               provenance_ids=()) -> SemanticInstance:
        return cls(
            source.strip(), release_id.strip(), relation.strip(), instance_id.strip(), text.strip(),
            tuple(sorted({schema_uri(value) for value in classes})),
            tuple(sorted({schema_uri(value) for value in properties})),
            deterministic_split(instance_id.strip()), mapping_version.strip(),
            tuple(sorted({str(value).strip() for value in provenance_ids if str(value).strip()})),
        )

    def validate(self, contract: SchemaContract) -> None:
        for label, value in (("source", self.source), ("release_id", self.release_id),
                             ("relation", self.relation), ("instance_id", self.instance_id),
                             ("mapping_version", self.mapping_version)):
            if not value or (label in {"source", "relation", "instance_id"} and not _ID.fullmatch(value)):
                raise ValueError(f"invalid semantic instance {label}: {value!r}")
        invalid_provenance = [value for value in self.provenance_ids if not _ID.fullmatch(value)]
        if invalid_provenance:
            raise ValueError(f"invalid semantic provenance ids: {invalid_provenance!r}")
        if not self.text or len(self.text) > 16_000:
            raise ValueError("semantic instance text must contain 1..16000 characters")
        if self.split not in SPLITS:
            raise ValueError(f"unknown semantic split: {self.split}")
        # The split must be DERIVED, never assigned. This makes a post-hoc `replace(..., split=...)`
        # patch — the previous way derived instances inherited a parent split, implemented twice in two
        # modules — fail the build instead of silently shipping a leaked corpus.
        if self.split != deterministic_split(self.instance_id):
            raise ValueError(
                f"split {self.split!r} is not derived from group {group_id(self.instance_id)!r} "
                f"(instance {self.instance_id!r}); derive it, do not assign it"
            )
        if not self.properties:
            raise ValueError("semantic instance requires at least one property")
        # classes MAY be empty: a class-free instance is a LABELED NEGATIVE — a real table that is none of
        # the ontology's classes (e.g. the demo uploads). It still supervises its property labels and puts
        # negative pressure on every class calibration; an instance with neither classes nor properties
        # would be vacuous and is still rejected above.
        unknown_classes = set(self.classes) - set(contract.classes)
        unknown_properties = set(self.properties) - set(contract.properties)
        if unknown_classes or unknown_properties:
            raise ValueError(
                f"semantic instance uses unknown terms: classes={sorted(unknown_classes)} "
                f"properties={sorted(unknown_properties)}"
            )

    def record(self) -> dict:
        return {
            "source": self.source, "release_id": self.release_id,
            "relation": self.relation, "instance_id": self.instance_id,
            "text": self.text, "classes": self.classes, "properties": self.properties,
            "split": self.split, "mapping_version": self.mapping_version,
            "provenance_ids": self.provenance_ids,
        }

    @classmethod
    def from_record(cls, record: dict) -> SemanticInstance:
        return cls(
            record["source"], record["release_id"], record["relation"], record["instance_id"],
            record["text"], tuple(record["classes"]), tuple(record["properties"]),
            record["split"], record["mapping_version"], tuple(record.get("provenance_ids", ())),
        )


def write_jsonl(instances, path: str | Path) -> tuple[int, str]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as target:
        for instance in instances:
            line = json.dumps(instance.record(), sort_keys=True, ensure_ascii=True,
                              separators=(",", ":")) + "\n"
            target.write(line)
            digest.update(line.encode("utf-8"))
            count += 1
    return count, digest.hexdigest()


def read_jsonl(path: str | Path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield SemanticInstance.from_record(json.loads(line))
