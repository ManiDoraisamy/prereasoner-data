"""Guarded request-local enrichment orchestration over the existing SQL planner."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from engine.artifact_provenance import canonical_json_sha256, load_weights_manifest, sha256_file
from engine.config import DATA_DIR
from engine.domain_profiles import DOMAIN_PROFILE_VERSION
from engine.domain_typing import ProfileEvidence, RoleEvidence, detect_profiles, detect_roles
from engine.enrichment.adapters import AdapterResult, LookupDisposition, SourceAdapters
from engine.enrichment.intents import requested_attributes
from engine.enrichment.registry import (
    Activation, DatasetDefinition, EmbeddedStorage, ExecutionManifest, LookupCardinality,
    REGISTRY, REGISTRY_VERSION, SnapshotPin, TemporalKind,
)
from engine.enrichment.select import ExplicitKeyEdge, select_datasets
from engine.enrichment.store import MAX_LOOKUP_KEYS, SnapshotStore
from engine.enrichment.value_types import detect_column
from engine.tables import normalize_table_name, table_from_rows


PLANNER_CONFIG_ID = "sha256:" + hashlib.sha256(b"typed-ast-search:v1").hexdigest()
RANKER_CONFIG_ID = "sha256:" + hashlib.sha256(b"deterministic-sql-ranker:v1").hexdigest()


@dataclass(frozen=True)
class RuntimeIdentity:
    engine_build: str
    model_artifact_hash: str
    planner_config: str = PLANNER_CONFIG_ID
    ranker_config: str = RANKER_CONFIG_ID

    @classmethod
    def current(cls) -> "RuntimeIdentity":
        manifest = load_weights_manifest(DATA_DIR)
        model_hash = (
            "sha256:" + canonical_json_sha256(manifest)
            if manifest else "sha256:" + hashlib.sha256(b"unmanifested-model").hexdigest()
        )
        engine_root = Path(__file__).resolve().parents[1]
        source_identity = {
            name: sha256_file(engine_root / name)
            for name in ("domain_profiles.py", "domain_typing.py", "sql_ast.py", "sql_search.py",
                         "sql_rank.py", "tables.py", "enrichment/runtime.py")
        }
        build = (os.environ.get("K_REVISION") or os.environ.get("GIT_COMMIT")
                 or "source-sha256:" + canonical_json_sha256(source_identity))
        return cls(
            build, model_hash,
            planner_config="sha256:" + sha256_file(engine_root / "sql_search.py"),
            ranker_config="sha256:" + sha256_file(engine_root / "sql_rank.py"),
        )


@dataclass(frozen=True)
class EnrichmentPlan:
    tables: tuple[dict, ...]
    added_tables: tuple[str, ...]
    explicit_fks: tuple[ExplicitKeyEdge, ...]
    request_attributes: frozenset[str]
    roles: tuple[RoleEvidence, ...]
    profiles: tuple[ProfileEvidence, ...]
    outcomes: tuple[AdapterResult, ...]
    warnings: tuple[str, ...]
    manifest: ExecutionManifest | None = None

    @property
    def used(self) -> bool:
        return bool(self.added_tables)

    def provenance(self) -> dict | None:
        if self.manifest is None:
            return None
        return {
            "profiles": [item.profile for item in self.profiles],
            "roles": [
                {"profile": item.profile, "role": item.role, "table": item.table,
                 "score": item.score, "evidence": list(item.evidence)}
                for item in self.roles
            ],
            "datasets": [
                {"name": item.dataset_name, "disposition": item.disposition.value,
                 "reason": item.reason, "provenance": dict(item.provenance)}
                for item in self.outcomes
            ],
            "manifest": self.manifest.record(),
        }


def request_hash(tables, question: str) -> str:
    payload = {
        "question": question,
        "tables": [
            {"name": table.get("name"), "columns": table.get("columns"),
             "rows": table.get("rows")}
            for table in tables
        ],
    }
    return "sha256:" + canonical_json_sha256(payload)


def table_versions(tables) -> Mapping[str, str]:
    return MappingProxyType({
        str(table["name"]): "sha256:" + canonical_json_sha256({
            "columns": table.get("columns") or (), "rows": table.get("rows") or (),
        })
        for table in sorted(tables, key=lambda item: str(item["name"]))
    })


def _cells(table: dict, column: str) -> tuple[str, ...]:
    columns = list(table.get("columns") or ())
    if column not in columns:
        return ()
    index = columns.index(column)
    return tuple(
        str(row[index]).strip() for row in (table.get("rows") or ())
        if index < len(row) and row[index] is not None and str(row[index]).strip()
    )


def deployment_dataset_allowlist(value: str | None, registry=REGISTRY) -> frozenset[str]:
    """Parse the deployment switch; registry approval remains a separate mandatory key."""
    names = frozenset(part.strip() for part in (value or "").split(",") if part.strip())
    unknown = names - set(registry)
    if unknown:
        raise ValueError(f"unknown enrichment datasets: {', '.join(sorted(unknown))}")
    unapproved = {name for name in names if registry[name].activation != Activation.ACTIVE}
    if unapproved:
        raise ValueError(f"datasets are not approved for activation: {', '.join(sorted(unapproved))}")
    return names


class EnrichmentRuntime:
    """Prepare planner tabs and trusted edges; never fetch or mutate a source."""

    def __init__(self, store: SnapshotStore, registry=REGISTRY, *,
                 allow_evaluation: bool = False, identity: RuntimeIdentity | None = None,
                 enabled_datasets=()):
        self.store = store
        self.registry = registry
        self.allow_evaluation = bool(allow_evaluation)
        self.identity = identity or RuntimeIdentity.current()
        self.adapters = SourceAdapters(store, registry)
        if isinstance(enabled_datasets, str):
            raise ValueError("enabled_datasets must be a collection, not a string")
        self.enabled_datasets = frozenset(enabled_datasets)
        unknown = self.enabled_datasets - set(registry)
        if unknown:
            raise ValueError(f"unknown enrichment datasets: {', '.join(sorted(unknown))}")
        unapproved = {
            name for name in self.enabled_datasets
            if registry[name].activation != Activation.ACTIVE
        }
        if unapproved:
            raise ValueError(f"datasets are not approved for activation: {', '.join(sorted(unapproved))}")

    def _enabled(self, definition: DatasetDefinition) -> bool:
        return (definition.activation == Activation.ACTIVE
                and definition.name in self.enabled_datasets) or (
            self.allow_evaluation and definition.activation == Activation.EVALUATION
        )
    def _single_key_candidates(self, tables, attributes, role_names):
        for definition in (self.registry[name] for name in sorted(self.registry)):
            if (not self._enabled(definition) or definition.temporal.kind != TemporalKind.SNAPSHOT
                    or definition.cardinality != LookupCardinality.ONE
                    or len(definition.lookup_key) != 1
                    or isinstance(definition.storage, EmbeddedStorage)
                    or (definition.compatible_roles
                        and not (definition.compatible_roles & role_names))):
                continue
            for table in tables:
                candidates = []
                for index, column in enumerate(table.get("columns") or ()):
                    values = _cells(table, column)
                    if len(values) < 2:
                        continue
                    evidence = detect_column(values) | attributes
                    if definition.eligibility.eligible(evidence):
                        candidates.append((index, str(column), values, evidence))
                if candidates:
                    yield definition, table, min(candidates, key=lambda item: item[0])

    def prepare(self, tables, question: str, *, as_of: str | None = None,
                private_reference_versions: Mapping[str, str] | None = None,
                table_budget: int = 4, row_budget: int = 5000) -> EnrichmentPlan:
        if (isinstance(table_budget, bool) or not isinstance(table_budget, int)
                or table_budget < 0):
            raise ValueError("table_budget must be a non-negative integer")
        if (isinstance(row_budget, bool) or not isinstance(row_budget, int)
                or row_budget < 1):
            raise ValueError("row_budget must be a positive integer")
        source_tables = tuple(tables or ())
        attributes = requested_attributes(question)
        roles = detect_roles(source_tables)
        profiles = detect_profiles(source_tables)
        role_names = frozenset(item.role for item in roles)
        if not attributes or table_budget == 0:
            return EnrichmentPlan(source_tables, (), (), attributes, roles, profiles, (), ())

        selections = select_datasets(
            source_tables,
            registry={name: definition for name, definition in self.registry.items()
                      if self._enabled(definition)
                      and (not definition.compatible_roles
                           or definition.compatible_roles & role_names)},
            request_evidence=attributes,
        )
        candidates = []
        for selection in selections:
            values = _cells(next(table for table in source_tables
                                 if table.get("name") == selection.source_table),
                            selection.source_column)
            evidence = detect_column(values) | attributes
            candidates.append((selection.dataset, selection.source_table,
                               selection.source_column, values, evidence, selection.snapshot))
        for definition, table, candidate in self._single_key_candidates(
                source_tables, attributes, role_names):
            _, column, values, evidence = candidate
            candidates.append((definition, str(table.get("name")), column, values, evidence, None))

        existing = {normalize_table_name(table.get("name")) for table in source_tables}
        added = []
        edges = []
        outcomes = []
        warnings = []
        pins: dict[str, SnapshotPin] = {}
        loaded_rows = 0
        grouped = {}
        for candidate in candidates:
            grouped.setdefault(candidate[0].name, []).append(candidate)
        for dataset_name in sorted(grouped):
            bindings = grouped[dataset_name]
            definition = bindings[0][0]
            all_values = tuple(
                value for _, _, _, binding_values, _, _ in bindings
                for value in binding_values
            )
            lookup_values = tuple(dict.fromkeys(all_values))
            evidence = frozenset().union(*(binding[4] for binding in bindings))
            supplied_pins = {binding[5] for binding in bindings if binding[5] is not None}
            if len(supplied_pins) > 1:
                raise ValueError(f"{definition.name}: conflicting snapshot selections")
            snapshot = next(iter(supplied_pins), None)
            if len(lookup_values) > MAX_LOOKUP_KEYS:
                warnings.append(
                    f"{definition.name}: {len(lookup_values)} distinct keys exceed the bounded "
                    f"lookup limit of {MAX_LOOKUP_KEYS}; enrichment abstained"
                )
                continue
            try:
                outcome = self.adapters.lookup(
                    definition.name, ((value,) for value in lookup_values), evidence=evidence,
                    snapshot=snapshot,
                )
            except Exception as exc:  # source enrichment is optional; preserve own-data serving
                warnings.append(
                    f"{definition.name}: source lookup failed ({type(exc).__name__}); "
                    "enrichment abstained"
                )
                continue
            outcomes.append(outcome)
            if outcome.disposition != LookupDisposition.MATCHED:
                warnings.append(f"{definition.name}: {outcome.reason}")
                continue
            key_index = outcome.columns.index(definition.lookup_key[0])
            matched = {str(row[key_index]).strip() for row in outcome.rows}
            coverage = sum(value in matched for value in all_values) / len(all_values)
            if coverage < definition.thresholds.min_coverage:
                warnings.append(
                    f"{definition.name}: exact row coverage {coverage:.3f} is below "
                    f"{definition.thresholds.min_coverage:.3f}"
                )
                continue
            if definition.name in existing:
                warnings.append(f"{definition.name}: request table name collision; enrichment abstained")
                continue
            if len(added) >= table_budget:
                warnings.append("request-local enrichment table budget was exhausted")
                break
            if loaded_rows + len(outcome.rows) > row_budget:
                warnings.append(f"{definition.name}: request-local enrichment row budget was exhausted")
                continue
            tab = table_from_rows(definition.name, list(outcome.columns),
                                  [list(row) for row in outcome.rows])
            added.append(tab)
            loaded_rows += len(outcome.rows)
            existing.add(definition.name)
            seen_bindings = set()
            for _, source_table, source_column, _, _, _ in bindings:
                binding = (normalize_table_name(source_table), source_column)
                if binding in seen_bindings:
                    continue
                seen_bindings.add(binding)
                edges.append(ExplicitKeyEdge(
                    binding[0], (binding[1],), definition.name,
                    definition.lookup_key, definition.cardinality,
                ))
            assert outcome.snapshot is not None
            pins[definition.name] = outcome.snapshot
            warnings.extend(outcome.warnings)

        if not added:
            return EnrichmentPlan(source_tables, (), (), attributes, roles, profiles,
                                  tuple(outcomes), tuple(dict.fromkeys(warnings)))
        manifest = ExecutionManifest(
            engine_build=self.identity.engine_build,
            request_hash=request_hash(source_tables, question),
            planner_config=self.identity.planner_config,
            ranker_config=self.identity.ranker_config,
            registry_version=REGISTRY_VERSION,
            domain_profile_version=DOMAIN_PROFILE_VERSION,
            schema_org_version="30.0",
            model_artifact_hash=self.identity.model_artifact_hash,
            as_of=as_of or "",
            dataset_snapshots=pins,
            private_reference_versions=private_reference_versions or {},
        )
        return EnrichmentPlan(
            source_tables + tuple(added), tuple(table["name"] for table in added),
            tuple(edges), attributes, roles, profiles, tuple(outcomes),
            tuple(dict.fromkeys(warnings)), manifest,
        )
