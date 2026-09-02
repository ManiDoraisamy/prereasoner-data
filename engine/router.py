"""Schema.org named-property routing for uploaded columns.

The learned output is the promoted URI-indexed Schema.org property profile. A
class is a calibrated superposition of those properties, and this module maps a
decoded class to the coarse resolver family using the pinned ontology graph.
Unsupported or uncalibrated classes abstain. SQL and source-key grounding remain
deterministic consumers of this evidence.
"""
from __future__ import annotations

import json

from engine.config import DATA_DIR
from engine.schema_decode import ClassDecoder
from engine.schema_model import SchemaInterpreter, summarize_table
from engine.schema_org import load_contract, schema_uri


MAXVALS = 40

# Specific CreativeWork subtypes must be checked before their broad parents.
_FAMILY_ROOTS = (
    ("film", ("Movie",)),
    ("music", ("MusicGroup", "MusicRecording", "MusicComposition", "MusicAlbum")),
    ("software", ("SoftwareApplication",)),
    ("publication", ("Periodical", "Book", "Article", "CreativeWork")),
    ("product", ("Product",)),
    ("organism", ("Taxon",)),
    ("place", ("Place",)),
    ("org", ("Organization",)),
    ("person", ("Person",)),
)
_FAMILY_OVERRIDES = {
    schema_uri("Hospital"): "place",
    schema_uri("School"): "place",
    schema_uri("CollegeOrUniversity"): "place",
}
_GEO_FAMILY = frozenset({"place"})


class Router:
    """Decode one column through calibrated Schema.org properties and classes."""

    def __init__(self, shared=None, interpreter: SchemaInterpreter | None = None):
        # ``shared`` is (qwen, tokenizer, relational_readout). The generalized
        # head needs only the first two and therefore reuses the serving Qwen.
        self._shared = shared
        self._interpreter = interpreter
        self.contract = load_contract()
        self.decoder = interpreter.decoder if interpreter is not None else ClassDecoder()
        meta = interpreter.meta if interpreter is not None else json.loads(
            (DATA_DIR / "schema_property_model.json").read_text(encoding="utf-8")
        )
        self.thresholds = {key: float(value) for key, value in meta["thresholds"].items()}
        self.qualified = frozenset(meta.get("qualified_properties", ()))
        self.model_artifact_sha256 = meta["artifact_sha256"]
        self.world_leaves = tuple(sorted(
            row["name"] for uri, row in self.decoder.classes.items()
            if row.get("servable") and self._family_for_class(uri) is not None
        ))

    def _load(self) -> SchemaInterpreter:
        if self._interpreter is None:
            shared = None if self._shared is None else self._shared[:2]
            self._interpreter = SchemaInterpreter(shared=shared)
        return self._interpreter

    def _family_for_class(self, class_uri: str) -> str | None:
        if class_uri in _FAMILY_OVERRIDES:
            return _FAMILY_OVERRIDES[class_uri]
        for family, roots in _FAMILY_ROOTS:
            if any(self.contract.is_subclass(class_uri, root) for root in roots):
                return family
        return None

    def _profile(self, values, header):
        rows = [[str(value)] for value in values if value is not None and str(value).strip()][:MAXVALS]
        if not rows:
            return None
        table = {
            "name": str(header or "column"),
            "columns": [str(header or "value")],
            "rows": rows,
        }
        return self._load().profile_text(summarize_table(table))

    def _class_evidence(self, profile, class_uri):
        return self.decoder.evidence(profile, class_uri, self.thresholds)

    def route(self, values, header=None, world_only=False, min_fire=0.0):
        """Return calibrated class/family evidence or ``None`` for explicit abstention.

        ``world_only`` remains an interface-compatibility argument; source-table
        availability and value grounding are enforced by the deterministic caller.
        """
        del world_only
        profile = self._profile(values, header)
        if profile is None:
            return None
        candidates = []
        for evidence in self.decoder.decode(
            profile, property_thresholds=self.thresholds, top_k=25,
        ):
            family = self._family_for_class(evidence.class_uri)
            if family is not None and evidence.score >= float(min_fire):
                candidates.append((evidence.score, evidence.class_uri, family, evidence))
        if not candidates:
            return None
        score, class_uri, family, evidence = max(candidates, key=lambda item: (item[0], item[1]))
        return {
            "family": family,
            "frac": round(float(score), 6),
            "geo": family in _GEO_FAMILY,
            "is_entity": True,
            "class": class_uri,
            "class_name": evidence.class_name,
            "scores": {class_uri: round(float(score), 6)},
            "evidence": [*evidence.fired, *evidence.missing],
            "ontology_version": self.contract.version,
            "model_artifact_sha256": self.model_artifact_sha256,
        }


def main():
    router = Router()
    examples = {
        "city": ["Paris", "Tokyo", "London", "Berlin"],
        "hospital": ["Mayo Clinic", "Cleveland Clinic", "Mount Sinai"],
        "amount": ["120.25", "80.10", "45.00"],
    }
    for header, values in examples.items():
        result = router.route(values, header=header)
        label = "ABSTAIN" if result is None else f"{result['class_name']} -> {result['family']}"
        print(f"  {header:9s} -> {label}")


if __name__ == "__main__":
    main()
