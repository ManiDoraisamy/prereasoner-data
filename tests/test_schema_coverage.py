"""COVERAGE RATCHET for the Schema.org named-dimension model.

This suite exists because of a real defect that shipped silently: when the corpus moved to multi-source
Schema.org instances, learned coverage ROTATED instead of growing — 22 new source-shaped property dims came
in (codeValue, currentExchangeRate, latitude, ...) while 33 Wikidata-ENTITY dims (birthDate, gender,
hasOccupation, nationality, taxonName, taxonRank, ...) fell to ~zero corpus support and dropped out of the
trained basis. Trained dims went 71 -> 60. Every per-class held-out metric still looked perfect, because the
starved classes simply stopped being evaluated. Nothing failed.

So the ratchet asserts the thing the metrics could not: the basis must not shrink, and the entity properties
must actually carry corpus support. Reads only committed artifacts + the corpus manifest — no torch, no
Postgres.

python -m tests.test_schema_coverage
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from engine.artifact_provenance import canonical_json_sha256, sha256_file
from engine.config import DATA_DIR
from engine.fetch_weights import WEIGHTS

MANIFEST = Path("training/schema_org/data/semantic_manifest.json")
MODEL_META = DATA_DIR / "schema_property_model.json"

# The pre-existing Wikidata-derived basis this model must not regress below. `alloc.json` is the shipped
# 9-family router's basis: 90 content dims = 9 structural + 71 schema.org property + 10 intent.
LEGACY_PROPERTY_DIMS = 71
_STRUCT = {"is_str", "is_num", "num_frac", "is_time", "is_bool", "is_enum", "is_key", "is_ref", "currency"}

# Entity properties whose starvation caused Country/City/Hospital/Taxon/MusicGroup to fail calibration.
# `birthDate` survives because it is carried by the class-free Horse pool, where no class domain constrains
# it; `hasOccupation` and `nationality` do not, and are declared unreachable below.
ENTITY_DIMS = ("birthDate", "taxonRank", "parentTaxon", "geo")

# Dims deliberately NOT required, each for a measured reason rather than convenience. They are listed —
# not deleted — so the exclusions stay visible and are re-checked by test_excluded_dims_stay_excluded.
#
#   GeoCoordinates  a schema.org CLASS, never a property. The old basis carried it as a property dim only
#                   because that pipeline resolved bare names without validating against the ontology.
#                   `geo` (P625) is the property-level replacement and IS required above.
#   taxonName       not present in Schema.org v30 in any form — it is a Bioschemas term. Same root cause:
#                   the old basis never validated. For Taxon the entity label IS the P225 string, so the
#                   evidence is carried by `name`.
#   gender          P21's seven values are bare QIDs that resolve to no label anywhere in the snapshot.
#                   Under the evidence rule (never label a property whose value the encoder cannot read)
#                   it is unreachable from this data. The old model had it only from a hardcoded
#                   PERSON_MODAL stamp over a file that no longer exists in the repo.
UNREACHABLE_DIMS = {
    "GeoCoordinates": "schema.org class, not a property; replaced by `geo`",
    "taxonName": "absent from Schema.org v30 (Bioschemas term); evidence carried by `name`",
    "gender": "P21 values are 7 unresolvable QIDs; 0% renderable in this snapshot",
    # Data-reason exclusions are added ONLY after a retrain shows the dim actually starved. Predicting
    # them is how four dims (hasOccupation, nationality, affiliation, birthPlace) were briefly excluded
    # while the model was still learning them — an exclusion that protects a learnable dim is a dodge.
    #
    # The active bounded `wikidata.entity_label` release made actor, director, productionCompany,
    # operatingSystem and brand learnable. The remaining block still has too few independent labeled
    # groups in at least one split. THE REMEDY IS DATA, NOT A LOWER GATE: broadening the reviewed label
    # snapshot makes an entry reachable, at which point test_excluded_dims_stay_excluded fails it as stale.
    **{name: "values remain QID references with insufficient independent labeled groups in this snapshot"
       for name in ("character", "award", "editor", "lyricist",
                    "founder", "affiliation", "hasOccupation", "nationality", "locationCreated",
                    "programmingLanguage", "servesCuisine", "connectedTo",
                    "distance", "previousItem", "partOfSeries", "asin", "color", "license", "startTime",
                    "uploadDate")},
}

# Support floors the trainer enforces (training/schema_org/train_property_head.py). Held-out floors are
# additionally counted in DISTINCT GROUPS, because column instances add instances without adding
# independent observations.
MIN_TRAIN, MIN_VALIDATION, MIN_TEST = 25, 10, 5
MIN_VALIDATION_GROUPS, MIN_TEST_GROUPS = 10, 5


def _uri(name):
    return f"https://schema.org/{name}"


def _legacy_property_names():
    alloc = json.loads((DATA_DIR / "alloc.json").read_text(encoding="utf-8"))
    return {d["name"] for d in alloc["dims"]
            if d["name"] not in _STRUCT and not d["name"].startswith("intent_")}


def test_legacy_basis_is_the_documented_size():
    # Pins the baseline this ratchet measures against, so a change to alloc.json can't silently move the bar.
    names = _legacy_property_names()
    assert len(names) == LEGACY_PROPERTY_DIMS, \
        f"legacy basis changed: alloc.json has {len(names)} property dims, ratchet assumes {LEGACY_PROPERTY_DIMS}"
    print(f"  PASS  legacy basis pinned at {LEGACY_PROPERTY_DIMS} schema.org property dims")


def test_trained_basis_covers_the_legacy_one():
    # THE ratchet, as SET CONTAINMENT rather than cardinality.
    #
    # Counting was the original mistake repeated: the defect this suite exists to catch was a ROTATION —
    # 22 dims in, 33 out — and a count cannot see that. A count also let a real bug pass unnoticed: a
    # domain filter that silently never ran inflated the basis to 95 dims, 20 of which had no admissible
    # class, and `>= 71` was satisfied by the inflation. Containment cannot be satisfied that way: every
    # legacy property must either still be trained or be explicitly listed as unreachable with a reason.
    meta = json.loads(MODEL_META.read_text(encoding="utf-8"))
    trained = {uri.rsplit("/", 1)[1] for uri in meta["trained_properties"]}
    legacy = _legacy_property_names()
    missing = sorted(legacy - trained - set(UNREACHABLE_DIMS))
    assert not missing, (
        f"COVERAGE ROTATION: {len(missing)} legacy property dims dropped out of the trained basis without "
        f"being declared unreachable: {missing}. Either restore their corpus support, or add each to "
        f"UNREACHABLE_DIMS with a measured reason."
    )
    retained = len(legacy & trained)
    print(f"  PASS  trained basis ({len(trained)}) covers the legacy basis: "
          f"{retained}/{len(legacy)} legacy dims RETAINED ({100 * retained // len(legacy)}%), "
          f"{len(legacy & set(UNREACHABLE_DIMS))} declared unreachable with measured reasons, "
          f"{len(trained - legacy)} newly gained")


def test_entity_properties_are_trained_not_starved():
    # The specific dims whose loss silently un-served Country/City/Hospital/Taxon/MusicGroup.
    meta = json.loads(MODEL_META.read_text(encoding="utf-8"))
    trained = set(meta["trained_properties"])
    missing = [name for name in ENTITY_DIMS if _uri(name) not in trained]
    assert not missing, (
        f"entity properties absent from the trained basis: {missing} — the corpus is starving them, so any "
        f"class whose signature needs them cannot be served (this is exactly the 71->60 rotation)."
    )
    print(f"  PASS  all {len(ENTITY_DIMS)} entity properties trained: {', '.join(ENTITY_DIMS)}")


def test_entity_properties_clear_the_support_floor():
    # Being 'trained' is necessary but not sufficient — a dim calibrated on 3 positives produced 81 false
    # positives before. Assert real support in every split, counted in INDEPENDENT GROUPS as well as
    # instances so clone inflation cannot satisfy a held-out floor.
    meta = json.loads(MODEL_META.read_text(encoding="utf-8"))
    metrics = meta["property_metrics"]
    thin = []
    for name in ENTITY_DIMS:
        entry = metrics.get(_uri(name)) or {}
        support, groups = entry.get("support"), entry.get("group_support")
        if not support:
            thin.append(f"{name}: untrained")
        elif (support["train"] < MIN_TRAIN or support["validation"] < MIN_VALIDATION
                or support["test"] < MIN_TEST):
            thin.append(f"{name}: instances {support}")
        elif groups and (groups["validation"] < MIN_VALIDATION_GROUPS
                         or groups["test"] < MIN_TEST_GROUPS):
            thin.append(f"{name}: only {groups} independent groups")
    assert not thin, f"entity properties below the support floor {(MIN_TRAIN, MIN_VALIDATION, MIN_TEST)}: {thin}"
    print(f"  PASS  entity properties clear the ({MIN_TRAIN}/{MIN_VALIDATION}/{MIN_TEST}) floor in both "
          f"instances and independent groups")


def test_targeted_wikidata_dimensions_clear_calibration_floor():
    """The bounded Wikidata sampler must retain legacy dimensions under a fresh split."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    targets = {"author", "isBasedOn", "owns", "parentTaxon", "producer"}
    overrides = manifest["caps"].get("per_property_entity_overrides", {})
    missing_policy = sorted(name for name in targets if overrides.get(name, 0) < 250)
    assert not missing_policy, f"targeted Wikidata sampling policy missing or too small: {missing_policy}"

    group_support = manifest["property_group_support"]
    thin = {
        name: group_support.get(_uri(name), {})
        for name in sorted(targets)
        if group_support.get(_uri(name), {}).get("validation", 0) < MIN_VALIDATION_GROUPS
    }
    assert not thin, f"targeted Wikidata dimensions still below the validation-group floor: {thin}"
    print(f"  PASS  targeted Wikidata dimensions clear the {MIN_VALIDATION_GROUPS}-group calibration floor")


def test_class_evidence_excludes_every_universal_property():
    # REGRESSION: the class-evidence gate keeps a facet's class when it shows any NON-generic property, and
    # "generic" was a hand-picked set of six. It missed `identifier` — also domain Thing — so 2,785 facets
    # asserted PropertyValueSpecification / GeoCoordinates / Place / Country on an opaque id column alone,
    # which is the exact pathology the gate exists to prevent. Derived from the ontology it cannot be
    # incomplete: a property whose only domain is Thing applies to everything and establishes nothing.
    from engine.schema_org import load_contract
    from training.schema_org.source_adapters import generic_evidence
    contract = load_contract()
    generic = generic_evidence(contract)
    missing = [name for name in ("name", "description", "image", "alternateName", "mainEntityOfPage",
                                 "url", "identifier")
               if _uri(name) not in generic]
    assert not missing, f"universal (domain-Thing) properties absent from the generic set: {missing}"
    for uri in generic:
        domains = tuple(contract.properties[uri].domains)
        assert domains == (_uri("Thing"),), \
            f"{uri} is treated as generic but its domain is {domains} — it DOES establish a class"
    # and a class-bearing property must never be swept in
    for name in ("codeValue", "currentExchangeRate", "latitude", "taxonRank"):
        assert _uri(name) not in generic, f"{name} is class-bearing evidence and must not be generic"
    print(f"  PASS  generic set derived from the ontology: {len(generic)} domain-Thing properties, "
          f"including identifier")


def test_excluded_dims_stay_excluded():
    # The exclusions above are load-bearing claims, not conveniences. Re-verify each against the compiled
    # ontology so an exclusion can never quietly become a way to dodge the ratchet: a dim excluded as
    # "not a property" must really be absent from contract.properties.
    from engine.schema_org import load_contract
    contract = load_contract()
    wrong = [name for name in ("GeoCoordinates", "taxonName")
             if _uri(name) in contract.properties]
    assert not wrong, f"excluded as non-properties but present in the v30 contract: {wrong} — the exclusion is invalid"
    assert _uri("geo") in contract.properties, "`geo` must be a real property (it replaces GeoCoordinates)"
    # The DATA-reason exclusions must be falsifiable against the corpus, not merely asserted against a
    # literal defined three lines above. If one of these ever gains real support, the exclusion is stale and
    # must be removed rather than quietly protecting a dim the model could now learn.
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    groups = manifest["property_group_support"]
    stale = []
    for name, reason in UNREACHABLE_DIMS.items():
        if _uri(name) not in contract.properties:
            continue                                   # the ontology-reason exclusions, checked above
        support = groups.get(_uri(name), {})
        if (support.get("train", 0) >= MIN_TRAIN and support.get("validation", 0) >= MIN_VALIDATION_GROUPS
                and support.get("test", 0) >= MIN_TEST_GROUPS):
            stale.append(f"{name}: now has {support} independent groups, but is excluded as {reason!r}")
    assert not stale, "exclusions that the corpus no longer justifies:\n  " + "\n  ".join(stale)
    print(f"  PASS  {len(UNREACHABLE_DIMS)} exclusions re-verified: ontology-reasons against the contract, "
          f"data-reasons against actual corpus support")


def test_corpus_carries_column_level_instances():
    # Unification: the head must see COLUMN-shaped instances, not only whole-table texts. Without them it
    # cannot type a column, which is the sole reason the 9-family router still owns Wikidata column typing.
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sources = manifest["counts"]["source"]
    column_sources = {name: n for name, n in sources.items() if "column" in name}
    assert column_sources, (
        f"no column-level instances in the corpus (sources: {sorted(sources)}) — the head can only type whole "
        f"tables, so column typing cannot unify with engine/router.py"
    )
    print(f"  PASS  corpus carries column-level instances: {column_sources}")


def test_split_is_drawn_per_derivation_group():
    """REGRESSION for the leakage repair, which had no test guarding it.

    Splits are drawn per DERIVATION GROUP — everything left of the first '#' — so a table, its columns, its
    presentation variants and its row windows share one draw. Keying on the whole instance id instead let a
    table sit in train while a column built from the SAME rows sat in test: the column's text is a
    byte-identical substring of the table's, and the head is one linear layer over a FROZEN encoder, so it
    could satisfy the held-out gate by memorising the surface form.

    `SemanticInstance.validate` re-derives the split with the same function, so it cannot catch a revert of
    `group_id`; nothing else in the suite could either. These assertions fail if the grouping is removed.
    """
    from training.schema_org.instances import (
        DERIVATION_SEP,
        SPLIT_SALT,
        deterministic_split,
        group_id,
    )
    parent = "geonames.place:100399..101322"
    derived = [f"{parent}#col=latitude", f"{parent}#facet=1", f"{parent}#v1", f"{parent}#v2+facet=2"]
    for instance_id in derived:
        assert group_id(instance_id) == parent, f"{instance_id} must reduce to its parent group"
        assert deterministic_split(instance_id) == deterministic_split(parent), (
            f"{instance_id} drew a different split from its parent — a column and the table it came from "
            f"would sit on opposite sides of the held-out boundary"
        )
    # the separator is what makes the grouping work; a group id may not contain it
    assert DERIVATION_SEP == "#" and SPLIT_SALT
    try:
        deterministic_split("has space")
    except ValueError:
        pass
    else:
        raise AssertionError("a group id with characters outside the id grammar must be rejected")
    # distinct groups must still land independently (the grouping must not collapse the corpus)
    draws = {deterministic_split(f"src.rel:{n}") for n in range(200)}
    assert draws == {"train", "validation", "test"}, f"grouping collapsed the split space: {draws}"
    print("  PASS  split drawn per derivation group; derived instances inherit their parent's draw")


def test_artifacts_agree_on_corpus_identity():
    # The ratchet is meaningless if the metrics describe a different corpus than the manifest.
    meta = json.loads(MODEL_META.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert meta["corpus_sha256"] == manifest["corpus"]["sha256"], (
        f"model meta describes corpus {meta['corpus_sha256'][:16]} but the manifest is "
        f"{manifest['corpus']['sha256'][:16]} — retrain after rebuilding the corpus"
    )
    print(f"  PASS  model meta and manifest agree on corpus {meta['corpus_sha256'][:16]}")


def test_runtime_bundle_is_fully_fetchable_and_pinned():
    manifest = json.loads((DATA_DIR / "weights_manifest.json").read_text(encoding="utf-8"))
    assert set(WEIGHTS) == set(manifest["files"]), (
        "fetch_weights must provision every external file in the promoted manifest; "
        f"missing={sorted(set(manifest['files']) - set(WEIGHTS))}, "
        f"unmanifested={sorted(set(WEIGHTS) - set(manifest['files']))}"
    )
    meta = json.loads(MODEL_META.read_text(encoding="utf-8"))
    assert meta["weights_sha256"] == manifest["files"]["schema_property_head.pt"], (
        "Schema.org model metadata and the runtime manifest pin different property heads"
    )
    for name, record in manifest["committed_artifacts"].items():
        path = DATA_DIR / name
        assert b"\r" not in path.read_bytes(), f"committed JSON artifact is not LF-canonical: {name}"
        assert sha256_file(path) == record["sha256"], (
            f"committed runtime artifact {name} does not match weights_manifest.json"
        )
    training = json.loads((DATA_DIR / "schema_training_manifest.json").read_text(encoding="utf-8"))
    recorded_identity = training.pop("artifact_sha256")
    assert recorded_identity == canonical_json_sha256(training)
    for name, expected in training["artifacts"].items():
        path = DATA_DIR / name
        actual = sha256_file(path) if path.exists() else manifest.get("files", {}).get(name)
        assert actual == expected, (
            f"training provenance for {name} does not match the promoted runtime artifact"
        )
    print("  PASS  every promoted runtime artifact is fetchable or committed and hash-pinned")


def test_schema_embedding_cache_is_bound_to_its_encoder():
    from training.schema_org.train_property_head import _cached_embeddings

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cache.npz"
        np.savez(
            path,
            embeddings=np.array([[1.0, 2.0]], dtype=np.float32),
            text_hashes=np.array(["text-sha"]),
            encoder_artifact_sha256=np.array("encoder-a"),
        )
        assert set(_cached_embeddings(path, "encoder-a")) == {"text-sha"}
        assert not _cached_embeddings(path, "encoder-b"), (
            "embeddings from a different encoder adapter were reused"
        )
        legacy = Path(directory) / "legacy.npz"
        np.savez(
            legacy,
            embeddings=np.array([[1.0, 2.0]], dtype=np.float32),
            text_hashes=np.array(["text-sha"]),
        )
        assert not _cached_embeddings(legacy, "encoder-a"), (
            "an unversioned legacy embedding cache was accepted"
        )
    print("  PASS  Schema.org embedding caches are invalidated when the encoder changes")


def test_source_entity_cannot_span_semantic_splits():
    from training.schema_org.corpus import _verify_splits

    items = [
        SimpleNamespace(
            instance_id=f"group{i}", split=("train" if i < 80 else "validation" if i < 90 else "test"),
            text=f"text {i}", provenance_ids=(f"row:{i}",),
        )
        for i in range(100)
    ]
    items[-1].provenance_ids = items[0].provenance_ids
    try:
        _verify_splits(items)
    except ValueError as exc:
        assert "source row" in str(exc) and "occurs in splits" in str(exc), str(exc)
    else:
        raise AssertionError("the same source entity was accepted in train and test")
    print("  PASS  source entity identities cannot cross semantic splits")


TESTS = [
    test_legacy_basis_is_the_documented_size,
    test_trained_basis_covers_the_legacy_one,
    test_entity_properties_are_trained_not_starved,
    test_entity_properties_clear_the_support_floor,
    test_targeted_wikidata_dimensions_clear_calibration_floor,
    test_class_evidence_excludes_every_universal_property,
    test_excluded_dims_stay_excluded,
    test_corpus_carries_column_level_instances,
    test_split_is_drawn_per_derivation_group,
    test_artifacts_agree_on_corpus_identity,
    test_runtime_bundle_is_fully_fetchable_and_pinned,
    test_schema_embedding_cache_is_bound_to_its_encoder,
    test_source_entity_cannot_span_semantic_splits,
]

if __name__ == "__main__":
    print("=== schema.org named-dimension coverage ratchet ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_schema_coverage: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
