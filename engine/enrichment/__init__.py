"""Public API for deterministic reference enrichment.

The policy registry is intentionally lightweight: database bootstrap and grant tools
import it without installing the inference stack. Serving components remain available
from this package and are loaded only when requested.
"""
from __future__ import annotations

from importlib import import_module

from engine.enrichment.registry import (
    REGISTRY,
    REGISTRY_VERSION,
    AcceptanceThresholds,
    Activation,
    AmbiguityPolicy,
    Capability,
    DatasetDefinition,
    DateSelection,
    Eligibility,
    EmbeddedStorage,
    ExecutionManifest,
    LookupCardinality,
    PostgresStorage,
    QualifiedRelation,
    SnapshotPin,
    TemporalContract,
    TemporalKind,
    UsagePolicy,
    evidence_vocab,
    registry_version,
)

_LAZY_EXPORTS = {
    "detect_column": "engine.enrichment.value_types",
    "ExplicitKeyEdge": "engine.enrichment.select",
    "SelectedDataset": "engine.enrichment.select",
    "select_datasets": "engine.enrichment.select",
    "to_tabs": "engine.enrichment.select",
    "LoadedDataset": "engine.enrichment.store",
    "SnapshotStore": "engine.enrichment.store",
    "SourceContractError": "engine.enrichment.store",
    "currency_conversion_target": "engine.currency_intent",
    "currency_conversion_words": "engine.currency_intent",
    "currency_rate_attribute": "engine.currency_intent",
    "IntentEvidence": "engine.enrichment.intents",
    "requested_attribute_evidence": "engine.enrichment.intents",
    "requested_attributes": "engine.enrichment.intents",
    "AdapterResult": "engine.enrichment.adapters",
    "LookupDisposition": "engine.enrichment.adapters",
    "LookupPurpose": "engine.enrichment.adapters",
    "SourceAdapters": "engine.enrichment.adapters",
    "EnrichmentPlan": "engine.enrichment.runtime",
    "EnrichmentRuntime": "engine.enrichment.runtime",
    "RuntimeIdentity": "engine.enrichment.runtime",
    "deployment_dataset_allowlist": "engine.enrichment.runtime",
    "request_hash": "engine.enrichment.runtime",
    "table_versions": "engine.enrichment.runtime",
}

__all__ = [
    "REGISTRY", "REGISTRY_VERSION", "DatasetDefinition", "Eligibility",
    "AcceptanceThresholds", "Capability", "Activation", "LookupCardinality",
    "AmbiguityPolicy", "TemporalKind", "DateSelection", "TemporalContract",
    "UsagePolicy", "EmbeddedStorage", "PostgresStorage", "QualifiedRelation",
    "SnapshotPin", "ExecutionManifest", "evidence_vocab", "registry_version", "detect_column",
    "ExplicitKeyEdge", "SelectedDataset", "select_datasets", "to_tabs",
    "SnapshotStore", "LoadedDataset", "SourceContractError",
    "IntentEvidence", "currency_conversion_target", "currency_conversion_words",
    "currency_rate_attribute", "requested_attribute_evidence", "requested_attributes",
    "AdapterResult", "LookupDisposition", "LookupPurpose", "SourceAdapters",
    "EnrichmentPlan", "EnrichmentRuntime", "RuntimeIdentity", "deployment_dataset_allowlist",
    "request_hash", "table_versions",
]


def __getattr__(name: str):
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
