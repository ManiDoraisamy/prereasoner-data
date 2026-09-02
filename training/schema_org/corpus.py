"""Build the deterministic multi-source Schema.org semantic corpus from PostgreSQL."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

import engine.config  # noqa: F401 - loads the repository .env before engine.pg connects
from engine.artifact_provenance import canonical_json_sha256, sha256_file
from engine.pg import _pg
from engine.schema_org import CONTRACT_PATH, load_contract, schema_uri
from regress.product_templates import CASES
from engine.domain_profiles import PROFILES
from engine.schema_model import sample_values
from training.schema_org.instances import (
    SPLIT_SALT, SemanticInstance, group_id, write_jsonl,
)
from training.schema_org.paths import CORPUS_PATH, MANIFEST_PATH
from training.schema_org.source_adapters import (
    emit_instance,
    active_source_manifest, drop_counts, reset_drop_counts,
    source_column_instances, source_instances,
    wikidata_column_instances, wikidata_instances, wikidata_lookups,
)


DEFAULT_CORPUS = CORPUS_PATH
DEFAULT_MANIFEST = MANIFEST_PATH

_GENERATOR_INPUTS = (
    "training/schema_org/corpus.py",
    "training/schema_org/instances.py",
    "training/schema_org/source_adapters.py",
    "training/schema_org/source_catalog.py",
    "training/props/bridge_prop.csv",
    "engine/schema_model.py",
    "engine/data/schema_org_v30.json",
    "training/schema_org/fixtures/customers.csv",
    "training/schema_org/fixtures/orders.csv",
)


def _generator_identity() -> dict:
    """Fingerprint corpus code/data and bind it to a clean repository commit."""
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(
        ("git", "-C", str(root), "rev-parse", "HEAD"), text=True,
    ).strip()
    dirty = bool(subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"),
        text=True,
    ).strip())
    files = {name: sha256_file(root / name) for name in _GENERATOR_INPUTS}
    return {
        "entrypoint": "python -m training.schema_org.corpus",
        "repository_commit": commit,
        "worktree_clean": not dirty,
        "input_files": files,
        "input_files_sha256": canonical_json_sha256(files),
    }


_COLUMN_PROPERTIES = (
    (("email",), "email"), (("phone", "mobile"), "telephone"),
    (("postal", "zip"), "postalCode"), (("country",), "addressCountry"),
    (("city",), "addressLocality"), (("state", "region"), "addressRegion"),
    (("latitude", "lat"), "latitude"), (("longitude", "lng"), "longitude"),
    (("currency",), "currency"), (("price",), "price"),
    (("amount", "total"), "value"), (("quantity",), "value"),
    (("description", "details", "history", "concern", "observation"), "description"),
    (("name", "title"), "name"), (("start date", "started on"), "startDate"),
    (("end date", "completion date", "check out"), "endDate"),
    (("date", "submitted at", "timestamp", "signed at", "approved at"), "dateCreated"),
    (("version",), "version"), (("url", "website", "link"), "url"),
    (("code",), "identifier"), (("id", "number"), "identifier"),
)


def _evidence_columns(table, contract):
    """(header, values, property URIs) per column of a planner table — the shape emit_instance checks.

    Built from the SAME rows summarize_table renders, so the values checked for visibility are exactly the
    values the encoder will see."""
    out = []
    for index, column in enumerate(table["columns"]):
        uris = _column_properties([column], contract)
        if not uris:
            continue
        values = sample_values(row[index] if index < len(row) else None for row in table["rows"])
        if values:
            out.append((column, values, uris))
    return out


def _template_properties(columns, classes, contract) -> tuple[str, ...]:
    candidates = {schema_uri("name")}
    for column in columns:
        normalized = str(column).casefold().replace("_", " ")
        for needles, prop in _COLUMN_PROPERTIES:
            if any(needle in normalized for needle in needles):
                candidates.add(schema_uri(prop))
                break
    compatible = set()
    for class_name in classes:
        item = contract.schema_class(class_name)
        if item:
            compatible.update(item.compatible_properties)
    kept = candidates & compatible
    return tuple(sorted(kept or {schema_uri("name")}))


def product_template_instances(contract):
    """Public, source-cited metadata examples; they never count as real-row support."""
    for case in CASES:
        profile = PROFILES[case.expected_profile]
        role_classes = {
            schema_uri(class_name)
            for role in profile.roles if role.name in case.expected_roles
            for class_name in role.schema_org_classes
            if schema_uri(class_name) in contract.classes
        }
        if not role_classes:
            continue
        text = (
            f"table {case.table_name} | template: {case.template} | "
            f"columns: {'; '.join(case.columns)}"
        )
        # A template's text lists COLUMN NAMES rather than values, so the column name is what evidences the
        # property it was mapped from. Passing it as the "value" keeps these instances inside the one
        # evidence gate instead of exempting them from it.
        wanted = set(_template_properties(case.columns, role_classes, contract))
        columns = []
        for column in case.columns:
            uris = tuple(sorted(set(_column_properties([column], contract)) & wanted))
            if uris:
                columns.append((column, [column], uris))
        if not columns:
            continue
        instance = emit_instance(
            columns=columns, text=text, contract=contract,
            source="product_templates", release_id="public-corpus:v1",
            relation=f"product_templates.{case.expected_profile}",
            instance_id=f"product_templates.{case.case_id}",
            classes=role_classes, mapping_version="product-templates:v1",
        )
        if instance is not None:
            yield instance


_DEMO_FIXTURES = (
    ("CUST", "customers", Path("training/schema_org/fixtures/customers.csv")),
    ("ORD", "orders", Path("training/schema_org/fixtures/orders.csv")),
)
_DEMO_WINDOW = 6


def _demo_tables() -> dict[str, tuple[str, str]]:
    """Load training-owned consumer fixtures; website presentation is not a model-data API."""
    return {
        key: (name, path.read_text(encoding="utf-8"))
        for key, name, path in _DEMO_FIXTURES
    }


def _column_properties(columns, contract) -> tuple[str, ...]:
    """Header-mapped TRUE properties for a real uploaded table (no class filter — these instances carry no
    class label). Only certain mappings: a currency column IS schema:currency, an amount IS schema:value."""
    kept = set()
    for column in columns:
        normalized = str(column).casefold().replace("_", " ")
        for needles, prop in _COLUMN_PROPERTIES:
            if any(needle in normalized for needle in needles):
                uri = schema_uri(prop)
                if uri in contract.properties:
                    kept.add(uri)
                break
    return tuple(sorted(kept))


def demo_upload_instances(contract):
    """Committed consumer-shaped training fixtures as
    CLASS-FREE instances in the EXACT serving text shape (engine.schema_model.summarize_table over
    engine.tables.csv_table). These are the first tables live serving ever sees; without them the corpus has
    no consumer-shaped negatives, so a property like termCode calibrates 'perfect' in-corpus yet fires on
    short proper names (Ada/Bo/Sam) in the wild. Row windows expose several serving-sized views, but every
    derivative of one fixture shares one split group so overlapping rows never cross a split boundary."""
    from engine.schema_model import summarize_table
    from engine.tables import csv_table
    found = _demo_tables()
    release = "demo-uploads+sha256:" + hashlib.sha256(
        "\0".join(found[k][1] for k in sorted(found)).encode("utf-8")).hexdigest()
    for key in sorted(found):
        name, csv_text = found[key]
        table = csv_table(csv_text, name)
        rows = table["rows"]
        name_cols = [ci for ci, col in enumerate(table["columns"])
                     if "name" in str(col).casefold() or str(col).casefold() == "customer"]
        starts = range(0, max(1, len(rows) - _DEMO_WINDOW + 1))
        for start in starts:
            window_rows = rows[start:start + _DEMO_WINDOW]
            variants = [("", window_rows)]
            if name_cols:
                # SHORT-NAME variant of the same real rows ('Sherlock Holmes' -> 'Sherlock'): value shape a
                # real upload commonly has, and exactly the shape that false-fires code-like properties
                # (short proper names read as term codes) when the corpus lacks it.
                short = [[(str(v).split()[0] if ci in name_cols and str(v).strip() else v)
                          for ci, v in enumerate(row)] for row in window_rows]
                variants.append(("+short", short))
            for suffix, wrows in variants:
                window = {"name": table["name"], "columns": table["columns"], "rows": wrows}
                # ONE group per demo TABLE. Windows step by 1, so adjacent windows share 5 of their 6 rows;
                # with a per-window split they drew independently and the same values sat on both sides of
                # the boundary. Hanging every window off the table's group makes that impossible.
                instance = emit_instance(
                    columns=_evidence_columns(window, contract), text=summarize_table(window),
                    contract=contract,
                    source="demo_uploads", release_id=release,
                    relation="demo_uploads." + table["name"].replace(" ", "_"),
                    instance_id=f"demo_uploads.{key}#win={start:03d}{suffix}",
                    classes=(), mapping_version="demo-uploads:v2",
                )
                if instance is not None:
                    yield instance


def demo_column_instances(contract):
    """Per-column instances from the committed demo uploads — the consumer-shaped column negatives.

    A column that maps to no schema.org property (`amount`, `order ID`, `tier`) is SKIPPED rather than
    emitted label-free: SemanticInstance requires >=1 property, and the whole-table demo instances already
    supply the class-free consumer negatives."""
    from engine.schema_model import summarize_table
    from engine.tables import csv_table
    found = _demo_tables()
    release = "demo-uploads+sha256:" + hashlib.sha256(
        "\0".join(found[k][1] for k in sorted(found)).encode("utf-8")).hexdigest()
    for key in sorted(found):
        name, csv_text = found[key]
        table = csv_table(csv_text, name)
        for ci, column in enumerate(table["columns"]):
            properties = _column_properties([column], contract)
            if not properties:
                continue
            values = sample_values(row[ci] if ci < len(row) else None for row in table["rows"])
            if len(values) < 2:
                continue
            instance = emit_instance(
                columns=[(column, values, properties)], contract=contract,
                text=summarize_table({"name": table["name"], "columns": [column],
                                      "rows": [[v] for v in values]}),
                source="demo_uploads_columns", release_id=release,
                relation="demo_uploads." + table["name"].replace(" ", "_") + "." + column.replace(" ", "_"),
                instance_id=f"demo_uploads.{key}#col={column.replace(' ', '_')}",
                classes=(), mapping_version="demo-uploads:v2",
            )
            if instance is not None:
                yield instance


def _pin_wikidata(instances: list[SemanticInstance]) -> list[SemanticInstance]:
    payload = [
        {"relation": item.relation, "instance_id": item.instance_id,
         "text": item.text, "classes": item.classes, "properties": item.properties}
        for item in instances
    ]
    digest = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    release = "capped-sample+sha256:" + digest
    return [replace(item, release_id=release) for item in instances]


_SHARE_BOUNDS = {"train": (0.70, 0.90), "validation": (0.05, 0.16), "test": (0.05, 0.16)}


def _property_groups(instances) -> dict[str, dict[str, set]]:
    """property URI -> split -> the set of DISTINCT groups carrying it (independent observations)."""
    out: dict[str, dict[str, set]] = {}
    for item in instances:
        group = group_id(item.instance_id)
        for uri in item.properties:
            out.setdefault(uri, {}).setdefault(item.split, set()).add(group)
    return out


def _dedupe_text(instances):
    """Keep ONE instance per distinct text, deterministically (first in the already-sorted order).

    Column instances made surface collisions possible for the first time: two unrelated row groups can
    render the same column identically (cldr.currency_name for locales 'am' and 'as' both yield the same
    six currency codes). Different groups draw different splits, so an identical text would sit on both
    sides of the boundary — the frozen encoder would produce literally the same vector for a train and a
    test example. The duplicate also carries no information, so dropping it costs nothing."""
    seen: dict[str, str] = {}
    kept, dropped = [], []
    for item in instances:
        key = hashlib.sha256(item.text.encode("utf-8")).hexdigest()
        if key in seen:
            dropped.append((item.instance_id, seen[key]))
            continue
        seen[key] = item.instance_id
        kept.append(item)
    return kept, dropped


def _verify_splits(instances) -> None:
    """Refuse to WRITE a corpus that could leak. Each check is exact and O(n); none can false-positive.

    A corpus builds and trains perfectly well while leaking, and the held-out metrics look better for it —
    so these must be build-time errors, not advisory reports."""
    by_group: dict[str, str] = {}
    for item in instances:                                          # 1. one split per derivation group
        group = group_id(item.instance_id)
        seen = by_group.setdefault(group, item.split)
        if seen != item.split:
            raise ValueError(
                f"group {group!r} spans splits {seen!r} and {item.split!r} — instances derived from the "
                f"same rows must share one split (offender: {item.instance_id!r})"
            )
    by_text: dict[str, tuple[str, str]] = {}
    for item in instances:                                          # 2. identical text never spans splits
        key = hashlib.sha256(item.text.encode("utf-8")).hexdigest()
        prior = by_text.setdefault(key, (item.split, item.instance_id))
        if prior[0] != item.split:
            raise ValueError(
                f"identical text in splits {prior[0]!r} and {item.split!r}: {prior[1]!r} vs "
                f"{item.instance_id!r} — the frozen encoder would see the same vector on both sides"
            )
    by_provenance: dict[str, tuple[str, str]] = {}
    for item in instances:                                          # 3. one split per source entity/row
        for provenance_id in item.provenance_ids:
            prior = by_provenance.setdefault(provenance_id, (item.split, item.instance_id))
            if prior[0] != item.split:
                raise ValueError(
                    f"source row {provenance_id!r} occurs in splits {prior[0]!r} and {item.split!r}: "
                    f"{prior[1]!r} vs {item.instance_id!r}"
                )
    shares = Counter(item.split for item in instances)               # 4. grouping must not skew the ratios
    total = max(len(instances), 1)
    for split, (low, high) in _SHARE_BOUNDS.items():
        share = shares[split] / total
        if not low <= share <= high:
            raise ValueError(
                f"split {split!r} holds {share:.3f} of instances, outside [{low}, {high}] — group-level "
                f"hashing drifted the realized ratios ({dict(shares)} of {total})"
            )


def build(*, corpus_path: str | Path = DEFAULT_CORPUS,
          manifest_path: str | Path = DEFAULT_MANIFEST) -> dict:
    contract = load_contract()
    reset_drop_counts()
    conn = _pg()
    conn.set_session(readonly=True)
    cur = conn.cursor()
    try:
        bridge_path = Path("training/props/bridge_prop.csv")
        source = list(source_instances(cur, contract))
        source += list(source_column_instances(cur, contract))
        lookups = wikidata_lookups(cur, bridge_path, contract)      # loaded ONCE for both generators
        wikidata = list(wikidata_instances(cur, contract, bridge_path=bridge_path, lookups=lookups))
        wikidata = _pin_wikidata(wikidata)
        wikidata += list(wikidata_column_instances(cur, contract, bridge_path=bridge_path,
                                                   lookups=lookups))
        source_manifest = active_source_manifest(cur)
    finally:
        cur.close()
        conn.close()
    templates = list(product_template_instances(contract))
    demo = list(demo_upload_instances(contract)) + list(demo_column_instances(contract))
    instances = sorted(source + wikidata + templates + demo,
                       key=lambda item: (item.source, item.relation, item.instance_id))
    keys = [(item.source, item.instance_id) for item in instances]
    if len(keys) != len(set(keys)):
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        raise ValueError(f"duplicate semantic instance IDs: {duplicates[:10]}")
    instances, collisions = _dedupe_text(instances)
    _verify_splits(instances)
    count, digest = write_jsonl(instances, corpus_path)

    by_source = Counter(item.source for item in instances)
    by_split = Counter(item.split for item in instances)
    by_class = Counter(class_uri for item in instances for class_uri in item.classes)
    by_property = Counter(prop for item in instances for prop in item.properties)
    manifest = {
        "schema_version": 1,
        "generator": _generator_identity(),
        "ontology": {
            "version": contract.version,
            "source_sha256": contract.source_sha256,
            "contract_sha256": contract.contract_sha256,
            "classes": len(contract.classes), "properties": len(contract.properties),
        },
        "corpus": {"path": Path(corpus_path).resolve().relative_to(Path.cwd()).as_posix(),
                   "instances": count,
                   "sha256": digest},
        "source_manifest": source_manifest,
        "wikidata": {
            "status": "sample-pinned-from-legacy-capped-snapshot",
            "release_id": wikidata[0].release_id if wikidata else "",
            "instances": len(wikidata),
        },
        "unavailable_sources": {
            "who": "credentials-required", "loinc": "licensed-archive-required",
            "openfoodfacts": "not-materialized", "gleif": "not-materialized",
        },
        "counts": {
            "source": dict(sorted(by_source.items())),
            "split": dict(sorted(by_split.items())),
            "class": dict(sorted(by_class.items())),
            "property": dict(sorted(by_property.items())),
            "group": len({group_id(item.instance_id) for item in instances}),
        },
        "split_policy": {
            "salt": SPLIT_SALT,
            "key": "group_id(instance_id) = text left of the first '#'",
            "text_collisions_dropped": len(collisions),
            "text_collision_examples": [f"{a} == {b}" for a, b in collisions[:5]],
        },
        # The evidence invariant is enforced at emission (source_adapters._evidence_ok), where the
        # property's VALUES are still in hand and can be checked against the tokenizer-truncated text.
        # A corpus-level re-check is not possible from the instance alone: the record keeps property URIs,
        # not which value evidenced which property, and schema.org names are not the publisher's headers.
        "evidence_gate": "token-accurate at emission (Qwen/Qwen2.5-0.5B, 112-token budget)",
        # What the bounded sweeps discarded. A cap without a denominator reads as full coverage.
        "dropped": {**drop_counts(), "duplicate_text": len(collisions)},
        "caps": {"max_facets": 3, "per_property_entities": 100, "baseline_entities": 100,
                 "column_every": 8, "wikidata_columns_per_property": 6,
                 "max_values_per_property": 3, "variant_every": 4, "anon_every": 4},
        # Per-property DISTINCT-GROUP support per split, beside the instance counts the trainer's floors
        # use. Column instances add instances without adding groups, so a property can clear an
        # instance-count floor on clones of one group; this makes that inflation auditable.
        "property_group_support": {
            uri: {split: len(groups) for split, groups in sorted(splits.items())}
            for uri, splits in sorted(_property_groups(instances).items())
        },
    }
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=True,
                                        indent=2) + "\n", encoding="utf-8")
    print(
        f"semantic corpus: {count} instances sha256:{digest}\n"
        f"sources={dict(sorted(by_source.items()))}\n"
        f"splits={dict(sorted(by_split.items()))}\n"
        f"observed classes={len(by_class)}/{len(contract.classes)} "
        f"properties={len(by_property)}/{len(contract.properties)}",
        flush=True,
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args()
    if not CONTRACT_PATH.exists():
        raise SystemExit("compile Schema.org first: python -m training.schema_org.ontology")
    build(corpus_path=args.corpus, manifest_path=args.manifest)


if __name__ == "__main__":
    main()
