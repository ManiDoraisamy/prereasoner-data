"""Canonical contracts and registry for deterministic reference enrichment.

The registry describes policy and shape. Source schemas own facts and release state;
``SnapshotStore`` resolves those releases at request time without network access. Nothing
in this module enables a dataset in serving by itself.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, TypeAlias


EMAIL = "email"
PHONE = "phone"
CURRENCY_CODE = "currency_code"
DATE = "date"
POSTAL = "postal_candidate"
GTIN = "gtin"
ISO_COUNTRY = "iso_country"
LEI = "lei"
MEDICAL_CODE = "medical_code"
UNIT_CODE = "unit_code"
GEONAME_ID = "geoname_id"
ATTR_COUNTRY_METADATA = "attr:country_metadata"
ATTR_COUNTRY_NAME = "attr:country_name"
ATTR_CURRENCY_METADATA = "attr:currency_metadata"
ATTR_UNIT_METADATA = "attr:unit_metadata"
ATTR_PLACE_METADATA = "attr:place_metadata"
ATTR_TIMEZONE = "attr:timezone"
ATTR_PHONE_METADATA = "attr:phone_metadata"
ATTR_POSTAL_CONTEXT = "attr:postal_context"
ATTR_EXCHANGE_RATE = "attr:exchange_rate"
ATTR_VAT_RULE = "attr:vat_rule"
ATTR_HOLIDAY = "attr:holiday"
ATTR_MEDICAL_METADATA = "attr:medical_metadata"
ATTR_ASSESSMENT = "attr:assessment"

VALUE_TYPES = frozenset({
    EMAIL, PHONE, CURRENCY_CODE, DATE, POSTAL, GTIN, ISO_COUNTRY, LEI, MEDICAL_CODE,
    UNIT_CODE, GEONAME_ID,
})
REQUEST_ATTRIBUTES = frozenset({
    ATTR_COUNTRY_METADATA, ATTR_COUNTRY_NAME, ATTR_CURRENCY_METADATA, ATTR_UNIT_METADATA, ATTR_PLACE_METADATA,
    ATTR_TIMEZONE, ATTR_PHONE_METADATA,
    ATTR_POSTAL_CONTEXT, ATTR_EXCHANGE_RATE, ATTR_VAT_RULE, ATTR_HOLIDAY,
    ATTR_MEDICAL_METADATA, ATTR_ASSESSMENT,
})

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_PRIVACY_CLASSES = frozenset({"public_reference", "contains_pii", "regulated"})
_COMMERCIAL = frozenset({"approved", "restricted", "paid", "blocked"})


class Capability(str, Enum):
    EXACT_DIMENSION = "exact_dimension"
    AMBIGUOUS_RELATION = "ambiguous_relation"
    PATTERN_METADATA = "pattern_metadata"
    TEMPORAL_SERIES = "temporal_series"
    TEMPORAL_RULE_SET = "temporal_rule_set"
    BOUNDED_CALENDAR = "bounded_calendar"
    TERMINOLOGY_HIERARCHY = "terminology_hierarchy"
    RIGHTS_BEARING_DOCUMENT_GRAPH = "rights_bearing_document_graph"


class Activation(str, Enum):
    DISABLED = "disabled"
    EVALUATION = "evaluation"
    ACTIVE = "active"


class LookupCardinality(str, Enum):
    ONE = "one"
    MANY = "many"


class AmbiguityPolicy(str, Enum):
    UNIQUE = "unique"
    RETURN_ALL = "return_all"
    REQUIRE_CONTEXT = "require_context"


class TemporalKind(str, Enum):
    SNAPSHOT = "snapshot"
    VALIDITY_INTERVAL = "validity_interval"
    EFFECTIVE_SERIES = "effective_series"
    EFFECTIVE_RULES = "effective_rules"
    BOUNDED_DATES = "bounded_dates"


class DateSelection(str, Enum):
    NONE = "none"
    CONTAINS_DATE = "contains_date"
    LATEST_ON_OR_BEFORE = "latest_on_or_before"
    EXACT_DATE = "exact_date"


def evidence_vocab() -> frozenset[str]:
    return VALUE_TYPES | REQUEST_ATTRIBUTES


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False)


def _sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value


def _identifier(value: str, label: str) -> str:
    _required_text(value, label)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SQL identifier, got {value!r}")
    return value


def _column_tuple(value, label: str, *, required: bool = True) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError(f"{label} must be a column collection, not a string")
    result = tuple(value)
    if required and not result:
        raise ValueError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicate columns")
    for column in result:
        _identifier(column, f"{label} column")
    return result


def _frozen_text_mapping(value: Mapping[str, str], label: str) -> Mapping[str, str]:
    normalized = {}
    for key, item in value.items():
        normalized[_required_text(key, f"{label} key")] = _required_text(
            item, f"{label}[{key!r}]"
        )
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class Eligibility:
    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    disqualifying: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("required", "optional", "disqualifying"):
            value = getattr(self, name)
            if isinstance(value, str):
                raise ValueError(f"eligibility {name} must be a collection of tags")
            if not isinstance(value, frozenset):
                object.__setattr__(self, name, frozenset(value))
        if ((self.required & self.optional) or (self.required & self.disqualifying)
                or (self.optional & self.disqualifying)):
            raise ValueError("eligibility evidence groups must be disjoint")
        if any(not isinstance(tag, str) or not tag.strip()
               for tags in (self.required, self.optional, self.disqualifying)
               for tag in tags):
            raise ValueError("eligibility tags must be non-empty strings")

    def eligible(self, evidence) -> bool:
        supplied = frozenset(evidence)
        return self.required <= supplied and not (self.disqualifying & supplied)


@dataclass(frozen=True)
class AcceptanceThresholds:
    min_precision: float
    min_selection_recall: float
    max_harmful_rate: float
    min_coverage: float
    min_positive_cases: int = 1
    min_negative_cases: int = 1

    def __post_init__(self) -> None:
        for name in ("min_precision", "min_selection_recall", "max_harmful_rate", "min_coverage"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not 0.0 <= float(value) <= 1.0):
                raise ValueError(f"threshold {name} must be a real number in [0,1]")
        for name in ("min_positive_cases", "min_negative_cases"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"threshold {name} must be a positive integer")


@dataclass(frozen=True)
class QualifiedRelation:
    schema_name: str
    table_name: str

    def __post_init__(self) -> None:
        _identifier(self.schema_name, "schema_name")
        _identifier(self.table_name, "table_name")

    @property
    def qualified_name(self) -> str:
        return f'{self.schema_name}.{self.table_name}'


@dataclass(frozen=True)
class TemporalContract:
    kind: TemporalKind = TemporalKind.SNAPSHOT
    selection: DateSelection = DateSelection.NONE
    effective_from: str = ""
    effective_to: str = ""

    def __post_init__(self) -> None:
        if self.kind == TemporalKind.SNAPSHOT:
            if self.selection != DateSelection.NONE or self.effective_from or self.effective_to:
                raise ValueError("snapshot temporal contract cannot declare date columns")
            return
        if self.selection == DateSelection.NONE:
            raise ValueError("temporal datasets require a date-selection policy")
        if not self.effective_from:
            raise ValueError("temporal datasets require effective_from")
        _identifier(self.effective_from, "effective_from")
        if self.effective_to:
            _identifier(self.effective_to, "effective_to")


@dataclass(frozen=True)
class UsagePolicy:
    attribution_required: bool = False
    advisory_only: bool = False
    row_denial_columns: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "row_denial_columns",
                           _column_tuple(self.row_denial_columns, "row_denial_columns", required=False))
        if isinstance(self.warnings, str):
            raise ValueError("warnings must be a collection")
        object.__setattr__(self, "warnings", tuple(
            _required_text(warning, "usage warning") for warning in self.warnings
        ))


@dataclass(frozen=True)
class EmbeddedStorage:
    source_version: str
    rows: tuple[tuple, ...]

    def __post_init__(self) -> None:
        _required_text(self.source_version, "embedded source_version")
        if isinstance(self.rows, (str, bytes)):
            raise ValueError("embedded rows must be row collections")
        object.__setattr__(self, "rows", tuple(tuple(row) for row in self.rows))


@dataclass(frozen=True)
class PostgresStorage:
    relation: QualifiedRelation
    related_relations: tuple[QualifiedRelation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.relation, QualifiedRelation):
            raise ValueError("postgres storage requires a qualified primary relation")
        if isinstance(self.related_relations, QualifiedRelation):
            raise ValueError("related_relations must be a collection")
        related = tuple(self.related_relations)
        if any(not isinstance(relation, QualifiedRelation) for relation in related):
            raise ValueError("related_relations must contain QualifiedRelation values")
        if any(relation.schema_name != self.relation.schema_name for relation in related):
            raise ValueError("related source relations must share one source schema")
        object.__setattr__(self, "related_relations", related)


Storage: TypeAlias = EmbeddedStorage | PostgresStorage


@dataclass(frozen=True)
class DatasetDefinition:
    name: str
    capability: Capability
    identity_key: tuple[str, ...]
    lookup_key: tuple[str, ...]
    attributes: tuple[str, ...]
    cardinality: LookupCardinality
    ambiguity_policy: AmbiguityPolicy
    temporal: TemporalContract
    storage: Storage
    source: str
    license: str
    redistribution: str
    commercial_use: str
    privacy_class: str
    eligibility: Eligibility
    thresholds: AcceptanceThresholds
    compatible_roles: frozenset[str] = frozenset()
    usage: UsagePolicy = UsagePolicy()
    activation: Activation = Activation.DISABLED

    def __post_init__(self) -> None:
        _identifier(self.name, "dataset name")
        for name in ("identity_key", "lookup_key", "attributes"):
            object.__setattr__(self, name, _column_tuple(
                getattr(self, name), f"{self.name}.{name}", required=name != "attributes"
            ))
        if not set(self.lookup_key) <= set(self.identity_key + self.attributes):
            raise ValueError(f"{self.name}: lookup key columns must be present in the dataset")
        if set(self.attributes) & set(self.identity_key):
            raise ValueError(f"{self.name}: identity key and attributes overlap")
        if self.cardinality == LookupCardinality.ONE and self.ambiguity_policy != AmbiguityPolicy.UNIQUE:
            raise ValueError(f"{self.name}: one-row lookup must use unique ambiguity policy")
        if self.cardinality == LookupCardinality.MANY and self.ambiguity_policy == AmbiguityPolicy.UNIQUE:
            raise ValueError(f"{self.name}: multi-row lookup must declare a multi-row policy")
        if not isinstance(self.storage, (EmbeddedStorage, PostgresStorage)):
            raise ValueError(f"{self.name}: unknown storage contract")
        if self.commercial_use not in _COMMERCIAL:
            raise ValueError(f"{self.name}: commercial_use must be one of {_COMMERCIAL}")
        if self.privacy_class not in _PRIVACY_CLASSES:
            raise ValueError(f"{self.name}: privacy_class must be one of {_PRIVACY_CLASSES}")
        for label in ("source", "license", "redistribution"):
            _required_text(getattr(self, label), f"{self.name}.{label}")
        if not isinstance(self.eligibility, Eligibility):
            raise ValueError(f"{self.name}: eligibility must be Eligibility")
        if not isinstance(self.thresholds, AcceptanceThresholds):
            raise ValueError(f"{self.name}: thresholds must be AcceptanceThresholds")
        if isinstance(self.compatible_roles, str):
            raise ValueError(f"{self.name}: compatible_roles must be a collection")
        object.__setattr__(self, "compatible_roles", frozenset(self.compatible_roles))
        if any(not isinstance(role, str) or not _IDENTIFIER.fullmatch(role)
               for role in self.compatible_roles):
            raise ValueError(f"{self.name}: compatible_roles must contain internal role identifiers")
        if not isinstance(self.temporal, TemporalContract) or not isinstance(self.usage, UsagePolicy):
            raise ValueError(f"{self.name}: temporal and usage contracts are required")
        if isinstance(self.storage, EmbeddedStorage):
            self._validate_embedded_rows()

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.identity_key + self.lookup_key + self.attributes))

    @property
    def is_embedded(self) -> bool:
        return isinstance(self.storage, EmbeddedStorage)

    def _validate_embedded_rows(self) -> None:
        assert isinstance(self.storage, EmbeddedStorage)
        positions = [self.columns.index(column) for column in self.identity_key]
        seen = set()
        for row in self.storage.rows:
            if len(row) != len(self.columns):
                raise ValueError(f"{self.name}: row arity {len(row)} != {len(self.columns)}")
            if any(isinstance(value, (list, tuple, dict, set)) for value in row):
                raise ValueError(f"{self.name}: rows must contain scalar values")
            key = tuple(row[index] for index in positions)
            if any(value is None or str(value).strip() == "" for value in key):
                raise ValueError(f"{self.name}: identity key values cannot be empty")
            encoded = _canonical_json(key)
            if encoded in seen:
                raise ValueError(f"{self.name}: duplicate identity key {key!r}")
            seen.add(encoded)

    @property
    def definition_id(self) -> str:
        return f"sha256:{_sha256(self.registry_record(include_identity=False))}"

    @property
    def embedded_snapshot_id(self) -> str:
        if not isinstance(self.storage, EmbeddedStorage):
            raise ValueError(f"{self.name}: database snapshots are resolved by SnapshotStore")
        payload = {
            "definition_id": self.definition_id,
            "source_version": self.storage.source_version,
            "rows": self.storage.rows,
        }
        return f"{self.storage.source_version}+sha256:{_sha256(payload)}"

    def registry_record(self, *, include_identity: bool = True) -> dict:
        storage = (
            {"kind": "embedded", "source_version": self.storage.source_version}
            if isinstance(self.storage, EmbeddedStorage) else {
                "kind": "postgres",
                "relation": self.storage.relation.qualified_name,
                "related_relations": [r.qualified_name for r in self.storage.related_relations],
            }
        )
        record = {
            "name": self.name,
            "capability": self.capability.value,
            "identity_key": self.identity_key,
            "lookup_key": self.lookup_key,
            "attributes": self.attributes,
            "cardinality": self.cardinality.value,
            "ambiguity_policy": self.ambiguity_policy.value,
            "temporal": {
                "kind": self.temporal.kind.value,
                "selection": self.temporal.selection.value,
                "effective_from": self.temporal.effective_from,
                "effective_to": self.temporal.effective_to,
            },
            "storage": storage,
            "source": self.source,
            "license": self.license,
            "redistribution": self.redistribution,
            "commercial_use": self.commercial_use,
            "privacy_class": self.privacy_class,
            "activation": self.activation.value,
            "eligibility": {
                "required": sorted(self.eligibility.required),
                "optional": sorted(self.eligibility.optional),
                "disqualifying": sorted(self.eligibility.disqualifying),
            },
            "thresholds": self.thresholds.__dict__,
            "compatible_roles": sorted(self.compatible_roles),
            "usage": {
                "attribution_required": self.usage.attribution_required,
                "advisory_only": self.usage.advisory_only,
                "row_denial_columns": self.usage.row_denial_columns,
                "warnings": self.usage.warnings,
            },
        }
        if include_identity:
            record["definition_id"] = self.definition_id
        return record


@dataclass(frozen=True)
class SnapshotPin:
    source_schema: str
    release_id: str
    schema_version: int
    contract_hash: str

    def __post_init__(self) -> None:
        _identifier(self.source_schema, "snapshot source_schema")
        _required_text(self.release_id, "snapshot release_id")
        _required_text(self.contract_hash, "snapshot contract_hash")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int) or self.schema_version < 1:
            raise ValueError("snapshot schema_version must be a positive integer")

    def record(self) -> dict:
        return {
            "source_schema": self.source_schema,
            "release_id": self.release_id,
            "schema_version": self.schema_version,
            "contract_hash": self.contract_hash,
        }


def _frozen_snapshot_mapping(value: Mapping[str, SnapshotPin]) -> Mapping[str, SnapshotPin]:
    normalized = {}
    for name, pin in value.items():
        _required_text(name, "dataset_snapshots key")
        if not isinstance(pin, SnapshotPin):
            raise ValueError("dataset_snapshots values must be SnapshotPin values")
        normalized[name] = pin
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True)
class ExecutionManifest:
    engine_build: str
    request_hash: str
    planner_config: str
    ranker_config: str
    registry_version: str
    domain_profile_version: str = "unprofiled"
    schema_org_version: str = "30.0"
    dataset_snapshots: Mapping[str, SnapshotPin] = field(default_factory=dict)
    private_reference_versions: Mapping[str, str] = field(default_factory=dict)
    model_artifact_hash: str = ""
    as_of: str = ""

    def __post_init__(self) -> None:
        for name in ("engine_build", "request_hash", "planner_config", "ranker_config",
                     "registry_version", "domain_profile_version", "schema_org_version",
                     "model_artifact_hash"):
            _required_text(getattr(self, name), f"manifest {name}")
        if not isinstance(self.as_of, str):
            raise ValueError("manifest as_of must be a string")
        object.__setattr__(self, "dataset_snapshots",
                           _frozen_snapshot_mapping(self.dataset_snapshots))
        object.__setattr__(self, "private_reference_versions",
                           _frozen_text_mapping(self.private_reference_versions,
                                                "private_reference_versions"))

    def record(self) -> dict:
        payload = {
            "engine_build": self.engine_build,
            "request_hash": self.request_hash,
            "planner_config": self.planner_config,
            "ranker_config": self.ranker_config,
            "registry_version": self.registry_version,
            "domain_profile_version": self.domain_profile_version,
            "schema_org_version": self.schema_org_version,
            "model_artifact_hash": self.model_artifact_hash,
            "as_of": self.as_of,
            "dataset_snapshots": {
                name: pin.record() for name, pin in self.dataset_snapshots.items()
            },
            "private_reference_versions": dict(self.private_reference_versions),
        }
        payload["digest"] = f"sha256:{_sha256(payload)}"
        return payload

    def digest(self) -> str:
        payload = self.record()
        return payload["digest"]


_STRICT = AcceptanceThresholds(0.99, 0.95, 0.005, 0.90, 2, 4)
_SOURCE_STRICT = AcceptanceThresholds(0.99, 0.95, 0.005, 0.90, 25, 100)


def _pg(schema: str, table: str, *related: str) -> PostgresStorage:
    return PostgresStorage(
        QualifiedRelation(schema, table),
        tuple(QualifiedRelation(schema, name) for name in related),
    )


def _source_definition(name: str, capability: Capability, schema: str, table: str,
                       identity_key: tuple[str, ...], lookup_key: tuple[str, ...],
                       attributes: tuple[str, ...], *,
                       cardinality: LookupCardinality = LookupCardinality.ONE,
                       ambiguity: AmbiguityPolicy = AmbiguityPolicy.UNIQUE,
                       temporal: TemporalContract = TemporalContract(),
                       related: tuple[str, ...] = (), evidence: frozenset[str] = frozenset(),
                       compatible_roles: frozenset[str] = frozenset(),
                       usage: UsagePolicy = UsagePolicy(), license: str,
                       redistribution: str = "source terms apply",
                       activation: Activation = Activation.DISABLED) -> DatasetDefinition:
    return DatasetDefinition(
        name=name, capability=capability, identity_key=identity_key, lookup_key=lookup_key,
        attributes=attributes, cardinality=cardinality, ambiguity_policy=ambiguity,
        temporal=temporal, storage=_pg(schema, table, *related), source=schema,
        license=license, redistribution=redistribution, commercial_use="approved",
        privacy_class="public_reference", eligibility=Eligibility(required=evidence),
        thresholds=_SOURCE_STRICT, compatible_roles=compatible_roles,
        usage=usage, activation=activation,
    )


_CURRENCY = DatasetDefinition(
    name="currency_iso4217", capability=Capability.EXACT_DIMENSION,
    identity_key=("currency_code",), lookup_key=("currency_code",),
    attributes=("currency_name", "symbol", "minor_unit"),
    cardinality=LookupCardinality.ONE, ambiguity_policy=AmbiguityPolicy.UNIQUE,
    temporal=TemporalContract(),
    storage=EmbeddedStorage("2024", (
        ("USD", "US Dollar", "$", 2), ("EUR", "Euro", "EUR", 2),
        ("GBP", "Pound Sterling", "GBP", 2), ("JPY", "Yen", "JPY", 0),
        ("INR", "Indian Rupee", "INR", 2), ("CAD", "Canadian Dollar", "$", 2),
        ("AUD", "Australian Dollar", "$", 2), ("CHF", "Swiss Franc", "Fr", 2),
        ("CNY", "Yuan Renminbi", "CNY", 2), ("SGD", "Singapore Dollar", "$", 2),
        ("HKD", "Hong Kong Dollar", "$", 2), ("SEK", "Swedish Krona", "kr", 2),
        ("NOK", "Norwegian Krone", "kr", 2), ("NZD", "New Zealand Dollar", "$", 2),
        ("ZAR", "Rand", "R", 2), ("BRL", "Brazilian Real", "R$", 2),
        ("AED", "UAE Dirham", "AED", 2), ("THB", "Baht", "THB", 2),
    )),
    source="ISO 4217 public code facts; temporary M0 fixture",
    license="ISO code facts; verbatim list text excluded",
    redistribution="code facts only", commercial_use="approved",
    privacy_class="public_reference",
    eligibility=Eligibility(required=frozenset({CURRENCY_CODE, ATTR_CURRENCY_METADATA})),
    thresholds=_STRICT,
    compatible_roles=frozenset({"order", "order_item", "invoice", "payment", "offer", "product"}),
    activation=Activation.EVALUATION,
)


# A non-temporal USD reference-rate snapshot so `amount * rate_to_usd` conversion (M3c) can fire on
# date-less order/invoice data. The date-aware series lives in `ecb_exchange_rate` (TEMPORAL_SERIES);
# this is the single-key latest-rate variant for the common "total in USD" question.
_CURRENCY_FX_USD = DatasetDefinition(
    name="currency_fx_usd", capability=Capability.EXACT_DIMENSION,
    identity_key=("currency_code",), lookup_key=("currency_code",),
    attributes=("rate_to_usd",),
    cardinality=LookupCardinality.ONE, ambiguity_policy=AmbiguityPolicy.UNIQUE,
    temporal=TemporalContract(),
    storage=EmbeddedStorage("2026-08-fixture", (
        ("USD", 1.0), ("EUR", 1.08), ("GBP", 1.27), ("JPY", 0.0067),
        ("INR", 0.012), ("CAD", 0.74), ("AUD", 0.66), ("CHF", 1.12),
        ("CNY", 0.14), ("SGD", 0.74), ("HKD", 0.128), ("SEK", 0.096),
        ("NOK", 0.093), ("NZD", 0.61), ("ZAR", 0.055), ("BRL", 0.20),
        ("AED", 0.272), ("THB", 0.028),
    )),
    source="static USD reference-rate snapshot; temporary fixture (temporal series: ecb_exchange_rate)",
    license="reference-rate facts; snapshot fixture",
    redistribution="rate facts only", commercial_use="approved",
    privacy_class="public_reference",
    eligibility=Eligibility(required=frozenset({CURRENCY_CODE, ATTR_EXCHANGE_RATE})),
    thresholds=_STRICT,
    compatible_roles=frozenset({"order", "order_item", "invoice", "payment", "offer", "product"}),
    activation=Activation.EVALUATION,
)


_SOURCE_DEFINITIONS = (
    _source_definition(
        "iana_country", Capability.EXACT_DIMENSION, "iana", "country_code",
        ("alpha2",), ("alpha2",), ("name",),
        evidence=frozenset({ISO_COUNTRY, ATTR_COUNTRY_NAME}),
        license="IANA tzdb public-domain notice",
        activation=Activation.ACTIVE,
    ),
    _source_definition(
        "iana_country_timezone", Capability.AMBIGUOUS_RELATION, "iana", "country_zone",
        ("country_alpha2", "timezone_id"), ("country_alpha2",), (),
        cardinality=LookupCardinality.MANY, ambiguity=AmbiguityPolicy.RETURN_ALL,
        related=("zone", "zone_alias", "zone_location"),
        evidence=frozenset({ISO_COUNTRY, ATTR_TIMEZONE}),
        license="IANA tzdb public-domain notice",
    ),
    _source_definition(
        "iana_timezone", Capability.EXACT_DIMENSION, "iana", "zone",
        ("timezone_id",), ("timezone_id",), (),
        related=("zone_alias", "zone_location", "country_zone"),
        evidence=frozenset({ATTR_TIMEZONE}),
        license="IANA tzdb public-domain notice",
    ),
    _source_definition(
        "cldr_territory", Capability.EXACT_DIMENSION, "cldr", "territory_code",
        ("territory_code",), ("territory_code",), ("numeric_code", "alpha3", "fips10"),
        related=("territory_alias", "territory_name"),
        evidence=frozenset({ISO_COUNTRY, ATTR_COUNTRY_METADATA}),
        license="Unicode License v3",
    ),
    _source_definition(
        "cldr_currency", Capability.EXACT_DIMENSION, "cldr", "currency_code",
        ("currency_code",), ("currency_code",), ("numeric_code",),
        related=("currency_name", "currency_symbol", "currency_fraction"),
        evidence=frozenset({CURRENCY_CODE, ATTR_CURRENCY_METADATA}),
        license="Unicode License v3",
    ),
    _source_definition(
        "cldr_territory_currency", Capability.TEMPORAL_RULE_SET, "cldr", "territory_currency",
        ("territory_code", "source_order"), ("territory_code",),
        ("currency_code", "valid_from", "valid_to", "tender"),
        cardinality=LookupCardinality.MANY, ambiguity=AmbiguityPolicy.RETURN_ALL,
        temporal=TemporalContract(TemporalKind.VALIDITY_INTERVAL, DateSelection.CONTAINS_DATE,
                                  "valid_from", "valid_to"),
        evidence=frozenset({ISO_COUNTRY, ATTR_CURRENCY_METADATA}),
        license="Unicode License v3",
    ),
    _source_definition(
        "cldr_unit_conversion", Capability.EXACT_DIMENSION, "cldr", "unit_conversion",
        ("source_unit",), ("source_unit",),
        ("base_unit", "factor_expression", "offset_expression", "special_function",
         "systems", "description"),
        related=("unit_prefix", "unit_constant", "unit_quantity", "unit_alias",
                 "unit_preference"),
        evidence=frozenset({UNIT_CODE, ATTR_UNIT_METADATA}),
        license="Unicode License v3",
    ),
    _source_definition(
        "libphonenumber_plan", Capability.PATTERN_METADATA, "google_libphonenumber", "territory",
        ("territory_id", "country_calling_code"),
        ("territory_id", "country_calling_code"),
        ("main_country_for_code", "leading_digits", "international_prefix", "national_prefix"),
        related=("number_pattern", "number_format"),
        evidence=frozenset({PHONE, ATTR_PHONE_METADATA}),
        compatible_roles=frozenset({"person", "customer", "lead", "signer", "patient"}),
        license="Apache License 2.0",
        usage=UsagePolicy(warnings=("Number metadata does not establish reachability or ownership.",)),
    ),
    _source_definition(
        "geonames_postal", Capability.AMBIGUOUS_RELATION, "geonames", "postal_code",
        ("source_order",), ("country_code", "postal_code"),
        ("country_code", "postal_code", "place_name", "admin_name1", "admin_code1",
         "admin_name2", "admin_code2", "admin_name3", "admin_code3",
         "latitude", "longitude", "accuracy"),
        cardinality=LookupCardinality.MANY, ambiguity=AmbiguityPolicy.REQUIRE_CONTEXT,
        evidence=frozenset({POSTAL, ATTR_POSTAL_CONTEXT}),
        compatible_roles=frozenset({"address", "location", "customer", "merchant", "patient",
                                    "site", "lodging_provider"}),
        license="Creative Commons Attribution 4.0",
        usage=UsagePolicy(attribution_required=True,
                          warnings=("Postal coverage and source accuracy vary by country.",)),
    ),
    _source_definition(
        "geonames_place", Capability.EXACT_DIMENSION, "geonames", "place",
        ("geoname_id",), ("geoname_id",),
        ("name", "ascii_name", "latitude", "longitude", "feature_class", "feature_code",
         "country_code", "admin_code1", "admin_code2", "population", "timezone_id",
         "modified_on"),
        evidence=frozenset({GEONAME_ID, ATTR_PLACE_METADATA}),
        license="Creative Commons Attribution 4.0",
        usage=UsagePolicy(attribution_required=True),
    ),
    _source_definition(
        "ecb_exchange_rate", Capability.TEMPORAL_SERIES, "ecb", "exchange_rate",
        ("effective_date", "quote_currency"), ("effective_date", "quote_currency"),
        ("units_per_eur",),
        temporal=TemporalContract(TemporalKind.EFFECTIVE_SERIES,
                                  DateSelection.LATEST_ON_OR_BEFORE, "effective_date"),
        evidence=frozenset({CURRENCY_CODE, DATE, ATTR_EXCHANGE_RATE}),
        license="ECB reuse policy",
        usage=UsagePolicy(advisory_only=True,
                          warnings=("ECB reference rates are not transaction rates.",)),
    ),
    _source_definition(
        "ec_tedb_vat_rule", Capability.TEMPORAL_RULE_SET, "ec_tedb", "vat_rate",
        ("source_order",), ("member_state", "category_id"),
        ("member_state", "category_id", "rate_class", "rate_type", "rate_percent",
         "effective_date", "category_description", "comment"),
        cardinality=LookupCardinality.MANY, ambiguity=AmbiguityPolicy.REQUIRE_CONTEXT,
        temporal=TemporalContract(TemporalKind.EFFECTIVE_RULES,
                                  DateSelection.LATEST_ON_OR_BEFORE, "effective_date"),
        related=("response_status", "vat_rate_cn_code", "vat_rate_cpa_code"),
        evidence=frozenset({ISO_COUNTRY, DATE, ATTR_VAT_RULE}),
        compatible_roles=frozenset({"order", "order_item", "invoice", "offer", "product"}),
        license="European Commission reuse policy",
        usage=UsagePolicy(advisory_only=True,
                          warnings=("TEDB data is non-binding; national law is authoritative.",)),
    ),
    _source_definition(
        "nager_holiday", Capability.BOUNDED_CALENDAR, "nager_date", "holiday",
        ("holiday_id",), ("country_code", "holiday_date"),
        ("country_code", "holiday_date", "name", "national_holiday"),
        cardinality=LookupCardinality.MANY, ambiguity=AmbiguityPolicy.RETURN_ALL,
        temporal=TemporalContract(TemporalKind.BOUNDED_DATES, DateSelection.EXACT_DATE,
                                  "holiday_date"),
        related=("holiday_subdivision", "holiday_type"),
        evidence=frozenset({ISO_COUNTRY, DATE, ATTR_HOLIDAY}),
        license="MIT licensed API/software; community data provenance retained",
        usage=UsagePolicy(advisory_only=True,
                          warnings=("Calendar coverage is bounded and community-maintained.",)),
    ),
    _source_definition(
        "cdc_icd10cm", Capability.TERMINOLOGY_HIERARCHY, "cdc", "icd10cm_code",
        ("code",), ("code",),
        ("description", "parent_code", "depth", "is_leaf", "effective_from", "effective_to"),
        temporal=TemporalContract(TemporalKind.VALIDITY_INTERVAL, DateSelection.CONTAINS_DATE,
                                  "effective_from", "effective_to"),
        evidence=frozenset({MEDICAL_CODE, ATTR_MEDICAL_METADATA}),
        compatible_roles=frozenset({"condition"}),
        license="U.S. Government work; source notices apply",
        usage=UsagePolicy(advisory_only=True,
                          warnings=("Terminology lookup must not infer a diagnosis.",)),
    ),
    _source_definition(
        "nlm_cde", Capability.RIGHTS_BEARING_DOCUMENT_GRAPH, "nlm_cde", "cde",
        ("tiny_id",), ("tiny_id",),
        ("version", "nih_endorsed", "archived", "preferred_name", "datatype"),
        related=("cde_designation", "cde_permissible_value"),
        evidence=frozenset({ATTR_ASSESSMENT}),
        compatible_roles=frozenset({"assessment", "patient_intake"}),
        license="NLM CDE Repository source-specific record terms",
        usage=UsagePolicy(warnings=("Availability does not imply clinical suitability.",)),
    ),
    _source_definition(
        "nlm_form", Capability.RIGHTS_BEARING_DOCUMENT_GRAPH, "nlm_cde", "form",
        ("tiny_id",), ("tiny_id",),
        ("version", "nih_endorsed", "archived", "is_copyrighted",
         "no_render_allowed", "preferred_name"),
        related=("form_element",), evidence=frozenset({ATTR_ASSESSMENT}),
        compatible_roles=frozenset({"assessment", "patient_intake"}),
        license="NLM CDE Repository source-specific record terms",
        usage=UsagePolicy(row_denial_columns=("no_render_allowed",),
                          warnings=("Per-form copyright and rendering restrictions apply.",)),
    ),
)

REGISTRY: Mapping[str, DatasetDefinition] = MappingProxyType({
    definition.name: definition for definition in (_CURRENCY, _CURRENCY_FX_USD) + _SOURCE_DEFINITIONS
})


def registry_version(registry: Mapping[str, DatasetDefinition]) -> str:
    for name, definition in registry.items():
        if name != definition.name:
            raise ValueError(f"registry key {name!r} does not match definition name")
    return f"sha256:{_sha256([registry[name].registry_record() for name in sorted(registry)])}"


REGISTRY_VERSION = registry_version(REGISTRY)


def get(name: str) -> DatasetDefinition | None:
    return REGISTRY.get(name)


def datasets() -> list[DatasetDefinition]:
    return list(REGISTRY.values())
