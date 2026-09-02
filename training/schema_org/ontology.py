"""Compile the official Schema.org JSON-LD release into a deterministic engine artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

from engine.schema_org import (
    CONTRACT_PATH,
    SCHEMA_ORG_SOURCE,
    SCHEMA_ORG_VERSION,
    schema_uri,
)

USER_AGENT = "prereasoner-schema-compiler/1.0"


def _ids(value) -> tuple[str, ...]:
    if value is None:
        return ()
    rows = value if isinstance(value, list) else [value]
    return tuple(sorted({schema_uri(row["@id"]) for row in rows if isinstance(row, dict)
                         and str(row.get("@id", "")).startswith("schema:")}))


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("@value", "")).strip()
    if isinstance(value, list):
        english = next((item for item in value if isinstance(item, dict)
                        and item.get("@language") == "en"), None)
        return _text(english or (value[0] if value else ""))
    return ""


def _canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def compile_payload(raw: bytes, *, source_url: str = SCHEMA_ORG_SOURCE) -> dict:
    document = json.loads(raw)
    graph = document.get("@graph")
    if not isinstance(graph, list):
        raise TypeError("Schema.org JSON-LD must contain an @graph list")

    class_nodes = {
        schema_uri(node["@id"]): node for node in graph
        if node.get("@type") == "rdfs:Class" and str(node.get("@id", "")).startswith("schema:")
    }
    property_nodes = {
        schema_uri(node["@id"]): node for node in graph
        if node.get("@type") == "rdf:Property" and str(node.get("@id", "")).startswith("schema:")
    }
    if len(class_nodes) < 900 or len(property_nodes) < 1500:
        raise ValueError(
            f"Schema.org {SCHEMA_ORG_VERSION} unexpectedly small: "
            f"{len(class_nodes)} classes, {len(property_nodes)} properties"
        )

    parents = {uri: _ids(node.get("rdfs:subClassOf")) for uri, node in class_nodes.items()}

    def ancestors(uri: str, stack=()) -> tuple[str, ...]:
        if uri in stack:
            raise ValueError(f"Schema.org class cycle: {' -> '.join(stack + (uri,))}")
        found = set()
        for parent in parents.get(uri, ()):
            if parent in class_nodes:
                found.add(parent)
                found.update(ancestors(parent, stack + (uri,)))
        return tuple(sorted(found))

    ancestry = {uri: ancestors(uri) for uri in class_nodes}
    direct_by_class = {uri: set() for uri in class_nodes}
    for prop_uri, node in property_nodes.items():
        for domain in _ids(node.get("schema:domainIncludes")):
            if domain in direct_by_class:
                direct_by_class[domain].add(prop_uri)

    properties = []
    for uri, node in sorted(property_nodes.items()):
        superseded = _ids(node.get("schema:supersededBy"))
        properties.append({
            "uri": uri, "name": uri.rsplit("/", 1)[-1],
            "label": _text(node.get("rdfs:label")),
            "comment": _text(node.get("rdfs:comment")),
            "domains": list(_ids(node.get("schema:domainIncludes"))),
            "ranges": list(_ids(node.get("schema:rangeIncludes"))),
            "superseded_by": superseded[0] if superseded else "",
        })

    classes = []
    for uri, node in sorted(class_nodes.items()):
        compatible = set(direct_by_class[uri])
        for ancestor in ancestry[uri]:
            compatible.update(direct_by_class.get(ancestor, ()))
        superseded = _ids(node.get("schema:supersededBy"))
        classes.append({
            "uri": uri, "name": uri.rsplit("/", 1)[-1],
            "label": _text(node.get("rdfs:label")),
            "comment": _text(node.get("rdfs:comment")),
            "parents": list(parents[uri]), "ancestors": list(ancestry[uri]),
            "direct_properties": sorted(direct_by_class[uri]),
            "compatible_properties": sorted(compatible),
            "superseded_by": superseded[0] if superseded else "",
        })

    payload = {
        "schema_version": 1,
        "version": SCHEMA_ORG_VERSION,
        "source_url": source_url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "property_order": [row["uri"] for row in properties],
        "class_order": [row["uri"] for row in classes],
        "properties": properties,
        "classes": classes,
    }
    payload["contract_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def download(url: str = SCHEMA_ORG_SOURCE) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def write_contract(payload: dict, path: str | Path = CONTRACT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                               separators=(",", ":")) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="local Schema.org 30.0 JSON-LD; defaults to official download")
    parser.add_argument("--output", default=str(CONTRACT_PATH))
    args = parser.parse_args()
    raw = Path(args.input).read_bytes() if args.input else download()
    payload = compile_payload(raw, source_url=SCHEMA_ORG_SOURCE)
    write_contract(payload, args.output)
    print(
        f"Schema.org {payload['version']}: {len(payload['classes'])} classes, "
        f"{len(payload['properties'])} properties -> {args.output}\n"
        f"source sha256:{payload['source_sha256']}\n"
        f"contract sha256:{payload['contract_sha256']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
