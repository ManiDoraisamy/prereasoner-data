"""Version-pinned Schema.org vocabulary used by training and serving.

The compiled artifact is deliberately model-independent: every Schema.org class and
property is representable even when no trained evidence exists for it.  Training adds
support/calibration in separate artifacts; absence there means abstain, not absence from
the ontology.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from engine.config import DATA_DIR


SCHEMA_ORG_VERSION = "30.0"
SCHEMA_ORG_SOURCE = (
    "https://schema.org/version/30.0/schemaorg-current-https.jsonld"
)
CONTRACT_PATH = DATA_DIR / "schema_org_v30.json"


def schema_uri(value: str) -> str:
    """Return the canonical https Schema.org URI for a compact or bare term."""
    value = str(value).strip()
    if value.startswith("https://schema.org/"):
        return value
    if value.startswith("http://schema.org/"):
        return "https://schema.org/" + value.rsplit("/", 1)[-1]
    if value.startswith("schema:"):
        return "https://schema.org/" + value.split(":", 1)[1]
    if ":" in value or not value:
        return value
    return "https://schema.org/" + value


def schema_name(value: str) -> str:
    value = str(value)
    return value.rsplit("/", 1)[-1] if "/" in value else value.split(":", 1)[-1]


@dataclass(frozen=True)
class SchemaProperty:
    uri: str
    name: str
    label: str
    comment: str
    domains: tuple[str, ...]
    ranges: tuple[str, ...]
    superseded_by: str = ""


@dataclass(frozen=True)
class SchemaClass:
    uri: str
    name: str
    label: str
    comment: str
    parents: tuple[str, ...]
    ancestors: tuple[str, ...]
    direct_properties: tuple[str, ...]
    compatible_properties: tuple[str, ...]
    superseded_by: str = ""


@dataclass(frozen=True)
class SchemaContract:
    version: str
    source_url: str
    source_sha256: str
    contract_sha256: str
    properties: Mapping[str, SchemaProperty]
    classes: Mapping[str, SchemaClass]
    property_order: tuple[str, ...]
    class_order: tuple[str, ...]

    def property(self, value: str) -> SchemaProperty | None:
        return self.properties.get(schema_uri(value))

    def schema_class(self, value: str) -> SchemaClass | None:
        return self.classes.get(schema_uri(value))

    def is_subclass(self, child: str, parent: str) -> bool:
        item = self.schema_class(child)
        target = schema_uri(parent)
        return bool(item and (item.uri == target or target in item.ancestors))


_CACHE: dict[Path, SchemaContract] = {}


def load_contract(path: str | Path = CONTRACT_PATH) -> SchemaContract:
    path = Path(path).resolve()
    cached = _CACHE.get(path)
    if cached is not None:
        return cached
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported Schema.org contract schema_version")
    if payload.get("version") != SCHEMA_ORG_VERSION:
        raise ValueError(
            f"Schema.org contract is {payload.get('version')!r}, expected {SCHEMA_ORG_VERSION}"
        )
    props = {
        row["uri"]: SchemaProperty(
            row["uri"], row["name"], row.get("label", ""), row.get("comment", ""),
            tuple(row.get("domains", ())), tuple(row.get("ranges", ())),
            row.get("superseded_by", ""),
        )
        for row in payload["properties"]
    }
    classes = {
        row["uri"]: SchemaClass(
            row["uri"], row["name"], row.get("label", ""), row.get("comment", ""),
            tuple(row.get("parents", ())), tuple(row.get("ancestors", ())),
            tuple(row.get("direct_properties", ())),
            tuple(row.get("compatible_properties", ())), row.get("superseded_by", ""),
        )
        for row in payload["classes"]
    }
    contract = SchemaContract(
        payload["version"], payload["source_url"], payload["source_sha256"],
        payload["contract_sha256"], MappingProxyType(props), MappingProxyType(classes),
        tuple(payload["property_order"]), tuple(payload["class_order"]),
    )
    if set(contract.property_order) != set(props) or set(contract.class_order) != set(classes):
        raise ValueError("Schema.org contract order does not match its records")
    _CACHE[path] = contract
    return contract

