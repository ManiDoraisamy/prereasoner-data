"""Bounded, release-pinned adapters for registered enrichment sources.

Adapters do not synchronize data, access the network, or activate a dataset. They turn a
registry lookup into a typed outcome and preserve ambiguity, policy, and provenance.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from engine.enrichment.registry import (
    Activation,
    AmbiguityPolicy,
    Capability,
    DatasetDefinition,
    REGISTRY,
    SnapshotPin,
    TemporalKind,
)
from engine.enrichment.store import LoadedDataset, SnapshotStore


class LookupDisposition(str, Enum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    POLICY_BLOCKED = "policy_blocked"
    INELIGIBLE = "ineligible"


class LookupPurpose(str, Enum):
    METADATA = "metadata"
    RENDER = "render"


@dataclass(frozen=True)
class AdapterResult:
    dataset_name: str
    disposition: LookupDisposition
    reason: str
    snapshot: SnapshotPin | None = None
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()
    warnings: tuple[str, ...] = ()
    provenance: Mapping[str, str] = MappingProxyType({})

    @property
    def matched(self) -> bool:
        return self.disposition == LookupDisposition.MATCHED


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


class SourceAdapters:
    """Policy-aware dispatcher over one SnapshotStore and one immutable registry."""

    def __init__(self, store: SnapshotStore, registry=REGISTRY):
        self.store = store
        self.registry = registry

    def lookup(self, dataset_name: str, keys, *, evidence=(), context=None,
               purpose: LookupPurpose = LookupPurpose.METADATA,
               allow_disabled: bool = False,
               snapshot: SnapshotPin | None = None) -> AdapterResult:
        definition = self.registry.get(dataset_name)
        try:
            purpose = LookupPurpose(purpose)
        except (TypeError, ValueError) as exc:
            raise ValueError("purpose must be 'metadata' or 'render'") from exc
        if context is not None and not isinstance(context, Mapping):
            raise ValueError("context must be a mapping")
        if definition is None:
            return self._result(dataset_name, LookupDisposition.INELIGIBLE,
                                "dataset is not registered")
        if definition.activation == Activation.DISABLED and not allow_disabled:
            return self._result(definition.name, LookupDisposition.INELIGIBLE,
                                "dataset is disabled")
        if definition.commercial_use != "approved" or definition.privacy_class != "public_reference":
            return self._result(definition.name, LookupDisposition.POLICY_BLOCKED,
                                "dataset policy does not permit serving")
        if isinstance(evidence, str):
            raise ValueError("evidence must be a collection")
        if not definition.eligibility.eligible(evidence):
            return self._result(definition.name, LookupDisposition.INELIGIBLE,
                                "required value and request evidence is absent")
        if definition.temporal.kind != TemporalKind.SNAPSHOT:
            return self._result(definition.name, LookupDisposition.INELIGIBLE,
                                "temporal planner semantics are not implemented")
        if purpose == LookupPurpose.RENDER and definition.capability != Capability.RIGHTS_BEARING_DOCUMENT_GRAPH:
            return self._result(definition.name, LookupDisposition.INELIGIBLE,
                                "rendering is not a capability of this dataset")

        pin = snapshot or self.store.active_snapshot(definition)
        loaded = self.store.load_by_keys(definition, pin, keys)
        if not loaded.rows:
            return self._loaded_result(loaded, LookupDisposition.NOT_FOUND,
                                       "lookup key was not found")
        if purpose == LookupPurpose.RENDER and self._denied_rows(definition, loaded):
            return self._loaded_result(loaded, LookupDisposition.POLICY_BLOCKED,
                                       "source row prohibits rendering", rows=())

        rows = loaded.rows
        if definition.ambiguity_policy == AmbiguityPolicy.REQUIRE_CONTEXT and len(rows) > 1:
            rows = self._apply_context(loaded, context or {})
            if len(rows) != 1:
                reason = "lookup requires disambiguating context"
                if context:
                    reason = "supplied context did not resolve the lookup uniquely"
                return self._loaded_result(loaded, LookupDisposition.AMBIGUOUS, reason, rows=rows)
        return self._loaded_result(loaded, LookupDisposition.MATCHED, "lookup matched", rows=rows)

    @staticmethod
    def _apply_context(loaded: LoadedDataset, context: Mapping[str, object]) -> tuple[tuple, ...]:
        allowed = {
            "place_name", "admin_name1", "admin_code1", "admin_name2", "admin_code2",
            "admin_name3", "admin_code3",
        }
        requested = {
            name: str(value).strip().casefold() for name, value in context.items()
            if name in allowed and value is not None and str(value).strip()
        }
        if not requested:
            return loaded.rows
        positions = {name: loaded.columns.index(name) for name in requested if name in loaded.columns}
        if len(positions) != len(requested):
            return loaded.rows
        return tuple(row for row in loaded.rows if all(
            str(row[positions[name]]).strip().casefold() == expected
            for name, expected in requested.items()
        ))

    @staticmethod
    def _denied_rows(definition: DatasetDefinition, loaded: LoadedDataset) -> bool:
        positions = [loaded.columns.index(column) for column in definition.usage.row_denial_columns]
        return any(_truthy(row[position]) for row in loaded.rows for position in positions)

    @staticmethod
    def _warnings(definition: DatasetDefinition) -> tuple[str, ...]:
        warnings = list(definition.usage.warnings)
        if definition.usage.attribution_required:
            warnings.append("Source attribution is required.")
        if definition.usage.advisory_only:
            warnings.append("Result is advisory only.")
        return tuple(warnings)

    @classmethod
    def _loaded_result(cls, loaded: LoadedDataset, disposition: LookupDisposition,
                       reason: str, *, rows=None) -> AdapterResult:
        definition = loaded.definition
        return cls._result(
            definition.name, disposition, reason, snapshot=loaded.snapshot,
            columns=loaded.columns, rows=loaded.rows if rows is None else tuple(rows),
            warnings=cls._warnings(definition),
            provenance={
                "source": definition.source,
                "release_id": loaded.snapshot.release_id,
                "schema_version": str(loaded.snapshot.schema_version),
                "definition_id": definition.definition_id,
                "license": definition.license,
            },
        )

    @staticmethod
    def _result(dataset_name: str, disposition: LookupDisposition, reason: str, *,
                snapshot=None, columns=(), rows=(), warnings=(), provenance=None) -> AdapterResult:
        return AdapterResult(
            dataset_name, disposition, reason, snapshot, tuple(columns), tuple(rows),
            tuple(warnings), MappingProxyType(dict(provenance or {})),
        )
