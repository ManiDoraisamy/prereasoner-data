"""Deterministic reference-enrichment foundations.

The package has one policy registry for embedded and source-backed definitions,
conservative value typing, release-pinned bounded storage access, and request-local
materialization. Source-backed definitions require both code approval and an explicit
deployment allowlist. This is not a second planner, router, model registry, or network-fetch path.
"""
from engine.enrichment.registry import (
    REGISTRY, REGISTRY_VERSION, AcceptanceThresholds, Activation, AmbiguityPolicy,
    Capability, DatasetDefinition, DateSelection, Eligibility, EmbeddedStorage,
    ExecutionManifest, LookupCardinality, PostgresStorage, QualifiedRelation,
    SnapshotPin, TemporalContract, TemporalKind, UsagePolicy, evidence_vocab,
    registry_version,
)
from engine.enrichment.value_types import detect_column
from engine.enrichment.select import ExplicitKeyEdge, SelectedDataset, select_datasets, to_tabs
from engine.enrichment.store import LoadedDataset, SnapshotStore, SourceContractError
from engine.currency_intent import (
    currency_conversion_target, currency_conversion_words, currency_rate_attribute,
)
from engine.enrichment.intents import IntentEvidence, requested_attribute_evidence, requested_attributes
from engine.enrichment.adapters import (
    AdapterResult, LookupDisposition, LookupPurpose, SourceAdapters,
)
from engine.enrichment.runtime import (
    EnrichmentPlan, EnrichmentRuntime, RuntimeIdentity, deployment_dataset_allowlist,
    request_hash, table_versions,
)

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
