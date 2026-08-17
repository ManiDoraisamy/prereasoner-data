"""Hermetic tests for M0 knowledge-enrichment foundations (docs/KNOWLEDGE_ENRICHMENT_ROADMAP.md).

Covers: registry validation + snapshot pinning; per-dataset required/optional/disqualifying
eligibility (an exact key needs no companion); deterministic value-typing; the integration
spike (a selected dataset enters the canonical discover_fks/AST-joinable path); ineligible
datasets never selected; selection independent of compose routing; ExecutionManifest replay
identity (pins private references, not just snapshots); and the benchmark acceptance rule
(passes the real dataset, rejects an over-eager one). No Postgres, no model, no network.

Run: python -m tests.test_enrichment
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from engine.domain_profiles import DOMAIN_PROFILE_VERSION, PROFILES
from engine.domain_typing import detect_profiles, detect_roles
from engine.relations import discover_fks
from engine.tables import TableQuery, table_from_rows
from engine.sql_schema import SchemaGraph
from engine.enrichment import (
    REGISTRY, REGISTRY_VERSION, select_datasets, to_tabs, detect_column,
    AcceptanceThresholds, Activation, AmbiguityPolicy, Capability, DatasetDefinition,
    DateSelection, EmbeddedStorage, Eligibility, ExecutionManifest, ExplicitKeyEdge, LookupCardinality,
    PostgresStorage, QualifiedRelation, SnapshotPin, SnapshotStore, SourceContractError,
    TemporalContract, TemporalKind, UsagePolicy, LoadedDataset,
    LookupDisposition, LookupPurpose, SourceAdapters,
    requested_attribute_evidence, requested_attributes,
    EnrichmentRuntime, RuntimeIdentity, deployment_dataset_allowlist, table_versions,
)
from engine.enrichment.registry import (
    ATTR_ASSESSMENT, ATTR_COUNTRY_METADATA, ATTR_COUNTRY_NAME, ATTR_CURRENCY_METADATA, ATTR_EXCHANGE_RATE,
    ATTR_HOLIDAY, ATTR_MEDICAL_METADATA, ATTR_PHONE_METADATA, ATTR_POSTAL_CONTEXT,
    ATTR_TIMEZONE, ATTR_UNIT_METADATA, ATTR_VAT_RULE,
    CURRENCY_CODE, DATE, ISO_COUNTRY, GTIN, LEI, POSTAL,
)
from engine.enrichment.registry import registry_version
from regress.enrichment import run_benchmark, BENCHMARK_CORPUS
from regress.domain_enrichment import run_domain_benchmark, run_serving_benchmark
from regress.product_templates import load_private_template_corpus, run_public_template_benchmark
from regress.source_activation import run_iana_country_activation_benchmark


def _t(name, columns, rows):
    return {"name": name, "columns": columns, "rows": rows}


def _select(tables, **kwargs):
    return select_datasets(tables, request_evidence={ATTR_CURRENCY_METADATA}, **kwargs)


def _dataset(**over):
    keys = over.pop("keys", ("k",))
    attributes = over.pop("attributes", ("a",))
    rows = over.pop("rows", ())
    source_version = over.pop("source_version", "1")
    temporal_model = over.pop("temporal_model", "snapshot")
    if temporal_model == "snapshot":
        temporal = TemporalContract()
    elif temporal_model == "series":
        temporal = TemporalContract(TemporalKind.EFFECTIVE_SERIES,
                                    DateSelection.LATEST_ON_OR_BEFORE, "effective_date")
    else:
        temporal = TemporalContract(TemporalKind(temporal_model))
    kw = dict(
        name="x", capability=Capability.EXACT_DIMENSION,
        identity_key=keys, lookup_key=keys, attributes=attributes,
        cardinality=LookupCardinality.ONE, ambiguity_policy=AmbiguityPolicy.UNIQUE,
        temporal=temporal, storage=EmbeddedStorage(source_version, rows), source="s",
        license="L", redistribution="r", commercial_use="approved",
        privacy_class="public_reference", eligibility=Eligibility(),
        thresholds=AcceptanceThresholds(0.9, 0.9, 0.01, 0.9),
        activation=Activation.EVALUATION,
    )
    kw.update(over)
    return DatasetDefinition(**kw)


def test_registry_validates_and_pins():
    ds = REGISTRY["currency_iso4217"]
    assert ds.temporal.kind == TemporalKind.SNAPSHOT and ds.privacy_class == "public_reference"
    assert ds.commercial_use in {"approved", "restricted", "paid", "blocked"}
    assert ds.embedded_snapshot_id and REGISTRY_VERSION                         # non-empty
    # a malformed dataset is rejected at construction
    for bad in [
        dict(keys=()),                                                            # no key
        dict(temporal_model="live"),                                             # bad temporal model
        dict(privacy_class="secret"),                                            # bad privacy class
        dict(commercial_use="maybe"),                                            # undecided commercial use
        dict(keys=("k",), attributes=("k",)),                                    # key repeated as attribute -> dup column
        dict(keys="key"),                                                       # string is not a column collection
        dict(rows=("not-a-row",)),                                              # string is not a row collection
        dict(rows=(("USD", "x"), ("USD", "y"))),                              # duplicate materialized key
        dict(rows=((None, "x"),)),                                               # empty materialized key
    ]:
        try:
            _dataset(**bad); raise AssertionError(f"expected validation failure for {bad}")
        except ValueError:
            pass
    # thresholds reject non-fractions AND booleans (isinstance(True, int) must not slip through)
    for badth in [dict(min_precision=True), dict(min_coverage=False),
                  dict(min_precision=1.5), dict(max_harmful_rate=-0.1),
                  dict(min_positive_cases=0), dict(min_negative_cases=True)]:
        th = dict(min_precision=0.9, min_selection_recall=0.9,
                  max_harmful_rate=0.01, min_coverage=0.9)
        th.update(badth)
        try:
            AcceptanceThresholds(**th); raise AssertionError(f"expected threshold rejection for {badth}")
        except ValueError:
            pass


def test_snapshot_and_registry_ids_are_content_complete():
    # cross-object stability: two independently-built datasets with identical content share an id
    a = _dataset(rows=(("USD", "d"),))
    b = _dataset(rows=(("USD", "d"),))
    assert a.embedded_snapshot_id == b.embedded_snapshot_id
    assert a.embedded_snapshot_id.startswith("1+sha256:")
    # Canonical serialization distinguishes types and separators. Full SHA-256 supplies the
    # collision-resistant content identity; unlike the old test, this does not call a hash injective.
    assert _dataset(rows=(("k", 2),)).embedded_snapshot_id != _dataset(rows=(("k", "2"),)).embedded_snapshot_id
    assert _dataset(rows=(("a\x1f", "b"),)).embedded_snapshot_id != _dataset(rows=(("a", "\x1fb"),)).embedded_snapshot_id
    # Schema and policy changes must alter the identities that replay pins.
    assert _dataset(rows=(("USD", "d"),), attributes=("description",)).embedded_snapshot_id != a.embedded_snapshot_id
    strict = _dataset(rows=(("USD", "d"),),
                      eligibility=Eligibility(required=frozenset({CURRENCY_CODE})))
    assert registry_version({"x": strict}) != registry_version({"x": a})


def test_eligibility_required_optional_disqualifying():
    # an EXACT key needs no companion column (the finding): required=one tag, nothing else
    assert Eligibility(required=frozenset({GTIN})).eligible(frozenset({GTIN}))
    assert Eligibility(required=frozenset({LEI})).eligible(frozenset({LEI}))
    # missing required -> abstain
    assert not Eligibility(required=frozenset({CURRENCY_CODE})).eligible(frozenset({ISO_COUNTRY}))
    # disqualifying evidence blocks even when required present
    el = Eligibility(required=frozenset({CURRENCY_CODE}), disqualifying=frozenset({ISO_COUNTRY}))
    assert el.eligible(frozenset({CURRENCY_CODE}))
    assert not el.eligible(frozenset({CURRENCY_CODE, ISO_COUNTRY}))
    # required and disqualifying may not overlap
    try:
        Eligibility(required=frozenset({GTIN}), disqualifying=frozenset({GTIN})); raise AssertionError
    except ValueError:
        pass
    try:
        Eligibility(optional=frozenset({GTIN}), disqualifying=frozenset({GTIN})); raise AssertionError
    except ValueError:
        pass
    try:
        Eligibility(required=GTIN); raise AssertionError
    except ValueError:
        pass


def test_value_typing_is_conservative():
    assert CURRENCY_CODE in detect_column(["USD", "EUR", "GBP", "INR"])
    assert ISO_COUNTRY in detect_column(["US", "GB", "IN", "DE"]) and CURRENCY_CODE not in detect_column(["US", "GB", "DE", "IN"])
    assert detect_column(["shipped", "pending", "cancelled"]) == frozenset()      # free text -> abstain
    # ASCII-only digit classes: full-width Unicode digits must NOT type as GTIN/DATE (conservative)
    assert detect_column(["１２３４５６７８"]) == frozenset()
    assert detect_column(["２０２４-０１-０１"]) == frozenset()
    assert GTIN in detect_column(["4006381333931", "9501234600000"])
    assert GTIN not in detect_column(["4006381333932", "9501234600004"])
    assert LEI in detect_column(["5493001KJTIIGC8Y1R12", "213800D1EI4B9WTWWD28"])
    assert LEI not in detect_column(["5493001KJTIIGC8Y1R13", "ABCDEFGHIJKLMNOPQRST"])
    assert detect_column(["2024-99-99", "2025-02-30"]) == frozenset()
    for threshold in (-0.1, 1.1, True, float("nan")):
        try:
            detect_column(["USD"], threshold); raise AssertionError(threshold)
        except ValueError:
            pass


def test_integration_spike_enters_discover_fks_path():
    # a differently-named currency column -> select produces an EXPLICIT key edge
    orders = _t("orders", ["order_id", "amount", "currency"],
                [[1, 10, "USD"], [2, 20, "EUR"], [3, 30, "GBP"], [4, 40, "INR"]])
    sel = _select([orders])
    assert len(sel) == 1 and sel[0].dataset.name == "currency_iso4217"
    assert sel[0].explicit_edge.as_foreign_key(1.0)["from_col"] == "currency"
    assert sel[0].explicit_edge.to_col == "currency_code"
    assert sel[0].key_confidence == sel[0].row_coverage == 1.0 and sel[0].snapshot_id
    tabs = to_tabs(sel)
    assert len(tabs) == 1 and tabs[0]["name"] == "currency_iso4217" and tabs[0]["columns"][0] == "currency_code"
    # canonical discovery: when the source column NAME aligns with the dataset key, the SAME
    # discover_fks the master path uses finds the edge, proving the materialized tab is AST-joinable.
    # (the FK child column must repeat -> many-to-one, to be distinguished from a candidate key)
    orders_named = table_from_rows("orders", ["order_id", "currency_code"],
                                   [[1, "USD"], [2, "EUR"], [3, "USD"], [4, "GBP"], [5, "EUR"], [6, "INR"]])
    fks = discover_fks([orders_named, tabs[0]])
    assert any(e["from_table"] == "orders" and e["to_table"] == "currency_iso4217"
               and e["to_col"] == "currency_code" for e in fks), fks
    try:
        to_tabs(sel, existing_names={"currency_iso4217"}); raise AssertionError
    except ValueError:
        pass


def test_renamed_column_edge_is_not_yet_bridged_by_discover_fks():
    """HONEST BOUNDARY: for a genuinely renamed column ('ccy'), select_datasets emits the correct
    explicit_edge, but discover_fks (name+value based) does NOT rediscover it — so a consumer that
    HONORS explicit_edge is required. That consumer is the serving-wiring milestone (roadmap §8),
    not M0. This test pins the current gap so it can't be silently mistaken for working."""
    invoices = _t("invoices", ["id", "ccy"],
                  [[1, "USD"], [2, "EUR"], [3, "USD"], [4, "GBP"], [5, "EUR"], [6, "INR"]])
    sel = _select([invoices])
    assert len(sel) == 1 and sel[0].explicit_edge.from_col == "ccy"           # select produces the edge
    tab = to_tabs(sel)[0]
    inv_named = table_from_rows("invoices", ["id", "ccy"], invoices["rows"])
    fks = discover_fks([inv_named, tab])
    assert not any(e["to_table"] == "currency_iso4217" for e in fks), fks     # discover_fks can't bridge it yet


def test_ineligible_datasets_never_selected():
    # no currency column at all
    assert _select([_t("orders", ["order_id", "status"],
                                [[1, "shipped"], [2, "pending"]])]) == []
    # a country column (typed ISO_COUNTRY, not CURRENCY_CODE) must not select the currency dataset
    assert _select([_t("leads", ["name", "country"],
                                [["a", "US"], ["b", "GB"], ["c", "IN"]])]) == []
    # invalid pseudo-codes are not currency-typed -> abstain on typing
    assert _select([_t("o", ["id", "ccy"], [[1, "XXX"], [2, "XTS"]])]) == []
    # REAL ISO codes that are absent from the embedded snapshot -> currency-typed but NOT joinable -> abstain
    assert _select([_t("o", ["id", "ccy"], [[1, "MXN"], [2, "KRW"], [3, "RUB"]])]) == []
    # Pattern alone never enriches; explicit request intent is mandatory.
    assert select_datasets([_t("o", ["id", "ccy"], [[1, "USD"], [2, "EUR"]])]) == []
    try:
        select_datasets([], request_evidence=ATTR_CURRENCY_METADATA); raise AssertionError
    except ValueError:
        pass
    policy = Eligibility(required=frozenset({CURRENCY_CODE, ATTR_CURRENCY_METADATA}))
    blocked = _dataset(eligibility=policy, commercial_use="blocked",
                       rows=(("USD", "x"), ("EUR", "x")))
    series = _dataset(eligibility=policy, temporal_model="series",
                      rows=(("USD", "x"), ("EUR", "x")))
    source = [_t("o", ["id", "ccy"], [[1, "USD"], [2, "EUR"]])]
    assert _select(source, registry={"x": blocked}) == []
    assert _select(source, registry={"x": series}) == []


def test_selection_policy_floors_and_tie_break():
    # 1) evidence floor: a single non-empty currency cell is too little evidence -> abstain
    assert _select([_t("o", ["id", "ccy"], [[1, "USD"]])]) == []
    # 2) a mono-currency column with enough evidence -> enrich (single-currency shops are legitimate)
    assert len(_select([_t("o", ["id", "ccy"], [[1, "USD"], [2, "USD"], [3, "USD"]])])) == 1
    # 3) sparse overlap below the canonical 0.9 FK threshold -> abstain
    assert _select([_t("o", ["id", "ccy"],
                                [[1, "USD"], [2, "MXN"], [3, "KRW"], [4, "RUB"], [5, "TRY"], [6, "IDR"]])]) == []
    # 4) tie-break: BOTH columns are currency-typed candidates (a at conf 0.5, b at conf 1.0);
    #    selection must pick the HIGHER key_confidence column b, not the leftmost a
    sel = _select([_t("o", ["a", "b"],
                              [["USD", "USD"], ["EUR", "EUR"], ["MXN", "GBP"], ["KRW", "INR"]])])
    assert len(sel) == 1 and sel[0].source_column == "b" and sel[0].key_confidence == 1.0
    # 5) distinct values can look healthy while most rows would be dropped by the exact join.
    weighted = [[i, "USD" if i == 0 else "EUR" if i == 1 else "MXN"] for i in range(20)]
    assert _select([_t("o", ["id", "ccy"], weighted)]) == []
    # 6) typing is case-insensitive, SQL equality is not. Do not claim a lower-case key is executable.
    assert _select([_t("o", ["id", "ccy"], [[1, "usd"], [2, "eur"]])]) == []
    for threshold in (-1, 2, True, float("nan")):
        try:
            select_datasets([], min_key_confidence=threshold); raise AssertionError(threshold)
        except ValueError:
            pass


def test_single_key_datasets_only():
    # a multi-key dataset must ABSTAIN (never emit a wrong single-column edge)
    multi = _dataset(name="pair", keys=("a", "b"), attributes=("x",),
                     rows=(("USD", "x", "y"), ("EUR", "x", "y"), ("GBP", "x", "y")))
    assert _select([_t("o", ["id", "ccy"], [[1, "USD"], [2, "EUR"], [3, "USD"]])],
                   registry={"pair": multi}) == []


def test_selection_is_independent_of_compose_routing():
    import engine.routing as routing
    orig = routing.route
    try:
        def _boom(*a, **k):
            raise AssertionError("enrichment selection must not invoke compose routing")
        routing.route = _boom
        sel = _select([_t("orders", ["id", "currency"], [[1, "USD"], [2, "EUR"]])])
        assert len(sel) == 1                                                     # succeeded without touching route()
    finally:
        routing.route = orig


def test_execution_manifest_pins_private_references():
    currency = REGISTRY["currency_iso4217"]
    pin = SnapshotPin("embedded", currency.embedded_snapshot_id, 1, currency.definition_id)
    base = dict(engine_build="e1", request_hash="request1", planner_config="p1", ranker_config="r1",
                registry_version=REGISTRY_VERSION, model_artifact_hash="m1", as_of="2026-08-16",
                dataset_snapshots={"currency_iso4217": pin})
    m1 = ExecutionManifest(**base, private_reference_versions={"ordered": "h1"})
    m2 = ExecutionManifest(**base, private_reference_versions={"ordered": "h1"})
    m3 = ExecutionManifest(**base, private_reference_versions={"ordered": "h2"})   # a mutable master ref changed
    assert m1.digest() == m2.digest()
    assert m1.digest() != m3.digest(), "manifest must pin private reference versions, not just snapshots"
    # insertion-order independence: the digest must be stable regardless of dict build order (replay identity)
    fwd = ExecutionManifest(**{**base, "dataset_snapshots": {}},
                            private_reference_versions={"a": "1", "b": "2", "c": "3"})
    rev = ExecutionManifest(**{**base, "dataset_snapshots": {}},
                            private_reference_versions={"c": "3", "b": "2", "a": "1"})
    assert fwd.digest() == rev.digest(), "digest must not depend on mapping insertion order"
    # injectivity: a key/value containing the old '='/';' separators must not collide
    other = SnapshotPin("embedded", "b=c", 1, currency.definition_id)
    e1 = ExecutionManifest(**{**base, "dataset_snapshots": {"a=b": pin}})
    e2 = ExecutionManifest(**{**base, "dataset_snapshots": {"a": other}})
    assert e1.digest() != e2.digest()
    # Frozen means nested mappings cannot mutate the digest after construction.
    try:
        m1.dataset_snapshots["currency_iso4217"] = "changed"; raise AssertionError
    except TypeError:
        pass
    assert ExecutionManifest(**{**base, "request_hash": "request2"}).digest() != ExecutionManifest(**base).digest()


def test_benchmark_accepts_currency_and_rejects_overeager():
    report = run_benchmark()
    cur = report["currency_iso4217"]
    assert cur.passed, cur
    assert cur.precision >= 0.99 and cur.harmful_rate <= 0.005, cur
    # an over-eager dataset (matches anything, keys overlap the abstain cases) must FAIL the rule
    loose = _dataset(
        name="loose", keys=("code",), eligibility=Eligibility(),
        thresholds=AcceptanceThresholds(min_precision=0.99, min_selection_recall=0.9,
                                        max_harmful_rate=0.0, min_coverage=0.9,
                                        min_positive_cases=1, min_negative_cases=1),
        rows=(("US", "x"), ("GB", "x"), ("IN", "x"), ("DE", "x"),
              ("shipped", "x"), ("USD", "x")))
    loose_corpus = [BENCHMARK_CORPUS[1],
                    ([_t("orders", ["id", "code"], [[1, "USD"], [2, "USD"]])],
                     frozenset({ATTR_CURRENCY_METADATA}),
                     {("loose", "orders", "code")})]
    rep2 = run_benchmark(registry={"loose": loose}, corpus=loose_corpus)
    assert not rep2["loose"].passed, rep2["loose"]


def test_coverage_is_row_level_and_can_fail_independently():
    # a dataset selected CORRECTLY (precision/recall 1.0) but joining few ROWS must fail ONLY on coverage,
    # proving coverage is a genuine row-level measure and not an alias of selection recall
    covered = _dataset(
        name="covered", keys=("code",),
        eligibility=Eligibility(required=frozenset({CURRENCY_CODE})),
        thresholds=AcceptanceThresholds(min_precision=0.9, min_selection_recall=0.9,
                                        max_harmful_rate=0.1, min_coverage=0.95),
        rows=tuple((code, "x") for code in
                   ("USD", "EUR", "GBP", "INR", "CAD", "AUD", "CHF", "CNY", "SGD")))
    corpus = [([_t("orders", ["id", "ccy"],
                  [[i, value] for i, value in enumerate(
                      ["USD", "EUR", "GBP", "INR", "CAD", "AUD", "CHF", "CNY", "SGD", "MXN"])])],
               frozenset({ATTR_CURRENCY_METADATA}),
               {("covered", "orders", "ccy")}),
              ([_t("orders", ["id", "status"], [[1, "open"], [2, "closed"]])],
               frozenset({ATTR_CURRENCY_METADATA}), set())]
    m = run_benchmark(registry={"covered": covered}, corpus=corpus)["covered"]
    assert m.precision == 1.0 and m.selection_recall == 1.0        # selection is correct
    assert m.coverage == 0.9 and not m.passed                     # but row coverage misses its stricter gate


def test_benchmark_requires_positive_and_negative_support():
    ds = _dataset(eligibility=Eligibility(required=frozenset({CURRENCY_CODE})),
                  rows=(("USD", "x"), ("EUR", "x")))
    no_positives = [([_t("orders", ["id", "status"], [[1, "open"], [2, "closed"]])],
                     frozenset({ATTR_CURRENCY_METADATA}), set())]
    metrics = run_benchmark(registry={"x": ds}, corpus=no_positives)["x"]
    assert metrics.positive_cases == 0 and not metrics.passed


def test_source_registry_models_measured_data_shapes_without_enabling_them():
    assert len(REGISTRY) == 17
    source_definitions = [definition for definition in REGISTRY.values()
                          if isinstance(definition.storage, PostgresStorage)]
    assert len(source_definitions) == 16
    assert {definition.name for definition in source_definitions
            if definition.activation == Activation.ACTIVE} == {"iana_country"}
    assert all(definition.activation == Activation.DISABLED for definition in source_definitions
               if definition.name != "iana_country")

    postal = REGISTRY["geonames_postal"]
    assert postal.storage.relation == QualifiedRelation("geonames", "postal_code")
    assert postal.lookup_key == ("country_code", "postal_code")
    assert postal.cardinality == LookupCardinality.MANY
    assert postal.ambiguity_policy == AmbiguityPolicy.REQUIRE_CONTEXT

    fx = REGISTRY["ecb_exchange_rate"]
    assert fx.capability == Capability.TEMPORAL_SERIES
    assert fx.temporal.selection == DateSelection.LATEST_ON_OR_BEFORE
    assert fx.usage.advisory_only

    form = REGISTRY["nlm_form"]
    assert form.capability == Capability.RIGHTS_BEARING_DOCUMENT_GRAPH
    assert form.usage.row_denial_columns == ("no_render_allowed",)
    assert "form_element" in {relation.table_name for relation in form.storage.related_relations}

    assert REGISTRY["iana_timezone"].storage.relation.table_name == "zone"
    assert REGISTRY["cldr_territory"].storage.relation.table_name == "territory_code"
    assert REGISTRY["cldr_unit_conversion"].storage.relation.table_name == "unit_conversion"
    assert REGISTRY["geonames_place"].storage.relation.table_name == "place"


def test_composite_explicit_edges_are_typed_for_the_planner():
    edge = ExplicitKeyEdge(
        "orders", ("country", "postal"), "geonames_postal",
        ("country_code", "postal_code"), LookupCardinality.MANY,
    )
    assert edge.from_columns == ("country", "postal")
    raw = edge.as_foreign_key(1.0)
    assert raw["from_cols"] == ("country", "postal")
    assert "from_col" not in raw
    try:
        _ = edge.from_col; raise AssertionError
    except ValueError:
        pass


def test_composite_edge_survives_canonical_table_ingestion():
    shipments = _t(
        "shipments", ["country", "postal", "amount"],
        [["US", "10001", 5], ["CA", "10001", 7]],
    )
    postal = _t(
        "geonames_postal", ["country_code", "postal_code", "place"],
        [["US", "10001", "New York"], ["CA", "10001", "Toronto"]],
    )
    edge = ExplicitKeyEdge(
        "shipments", ("country", "postal"), "geonames_postal",
        ("country_code", "postal_code"), LookupCardinality.MANY,
    )
    planner = TableQuery.__new__(TableQuery)
    tables, fks = planner.ingest([shipments, postal], explicit_fks=(edge,))
    assert fks[0]["explicit"] and fks[0]["from_cols"] == ("country", "postal")
    graph = SchemaGraph.from_tables(tables, fks)
    assert graph.foreign_keys[0].is_composite


def test_requested_attribute_extraction_is_explicit_and_contrastive():
    cases = {
        "Show the ISO 3166 country code": ATTR_COUNTRY_METADATA,
        "Show the country name for each code": ATTR_COUNTRY_NAME,
        "What is the currency symbol for each code?": ATTR_CURRENCY_METADATA,
        "Which timezone is used for this country?": ATTR_TIMEZONE,
        "Validate and format these phone numbers": ATTR_PHONE_METADATA,
        "Find the city for each postal code": ATTR_POSTAL_CONTEXT,
        "Use the exchange rate to convert the amount": ATTR_EXCHANGE_RATE,
        "What VAT rate is applicable?": ATTR_VAT_RULE,
        "Is this date a public holiday?": ATTR_HOLIDAY,
        "Give the ICD-10 description and parent": ATTR_MEDICAL_METADATA,
        "Show the common data element metadata": ATTR_ASSESSMENT,
        "Convert units from kilograms": ATTR_UNIT_METADATA,
    }
    for question, expected in cases.items():
        assert expected in requested_attributes(question), question
        evidence = requested_attribute_evidence(question)
        assert any(item.attribute == expected and item.phrase for item in evidence)
    for question in (
        "Total amount by currency", "Count customers by country",
        "Group orders by postal code", "List customer phone numbers",
        "Sales by timezone", "Holiday sales revenue", "Total tax amount",
    ):
        assert requested_attributes(question) == frozenset(), question


class _AdapterStore:
    def __init__(self, rows_by_dataset):
        self.rows_by_dataset = rows_by_dataset

    @staticmethod
    def active_snapshot(definition):
        source = definition.storage.relation.schema_name
        return SnapshotPin(source, "test-release", 1, definition.definition_id)

    def load_by_keys(self, definition, snapshot, keys):
        rows = tuple(self.rows_by_dataset.get(definition.name, ()))
        return LoadedDataset(definition, snapshot, definition.columns, rows)


def _source_row(definition, **values):
    return tuple(values.get(column) for column in definition.columns)


def test_source_adapters_preserve_activation_temporal_and_ambiguity_outcomes():
    country = REGISTRY["iana_country"]
    postal = REGISTRY["geonames_postal"]
    postal_rows = (
        _source_row(postal, source_order=1, country_code="US", postal_code="02139",
                    place_name="Cambridge", admin_name1="Massachusetts"),
        _source_row(postal, source_order=2, country_code="US", postal_code="02139",
                    place_name="Boston", admin_name1="Massachusetts"),
    )
    adapters = SourceAdapters(_AdapterStore({
        "iana_country": (_source_row(country, alpha2="FR", name="France"),),
        "geonames_postal": postal_rows,
    }))
    matched = adapters.lookup("iana_country", [("FR",)], evidence=country.eligibility.required)
    assert matched.matched and matched.rows[0][country.columns.index("name")] == "France"
    assert matched.provenance["release_id"] == "test-release"

    ambiguous = adapters.lookup(
        "geonames_postal", [("US", "02139")], evidence={POSTAL, ATTR_POSTAL_CONTEXT},
        allow_disabled=True,
    )
    assert ambiguous.disposition == LookupDisposition.AMBIGUOUS
    resolved = adapters.lookup(
        "geonames_postal", [("US", "02139")], evidence={POSTAL, ATTR_POSTAL_CONTEXT},
        context={"place_name": "Cambridge"}, allow_disabled=True,
    )
    assert resolved.matched and len(resolved.rows) == 1
    assert "attribution" in " ".join(resolved.warnings).lower()

    temporal = adapters.lookup(
        "ecb_exchange_rate", [("2026-08-17", "USD")],
        evidence={CURRENCY_CODE, DATE, ATTR_EXCHANGE_RATE}, allow_disabled=True,
    )
    assert temporal.disposition == LookupDisposition.INELIGIBLE
    assert "temporal planner" in temporal.reason


def test_source_adapter_enforces_row_render_policy():
    form = REGISTRY["nlm_form"]
    row = _source_row(form, tiny_id="F1", version="1", no_render_allowed=True,
                      preferred_name="Restricted form")
    adapters = SourceAdapters(_AdapterStore({"nlm_form": (row,)}))
    metadata = adapters.lookup(
        "nlm_form", [("F1",)], evidence={ATTR_ASSESSMENT}, allow_disabled=True,
    )
    assert metadata.matched
    render = adapters.lookup(
        "nlm_form", [("F1",)], evidence={ATTR_ASSESSMENT}, allow_disabled=True,
        purpose=LookupPurpose.RENDER,
    )
    assert render.disposition == LookupDisposition.POLICY_BLOCKED and not render.rows


def test_domain_profile_registry_and_typing_are_conservative():
    assert len(PROFILES) == 7 and DOMAIN_PROFILE_VERSION.startswith("sha256:")
    assert "Patient" in PROFILES["healthcare_intake"].schema_org_classes
    assert "Order" in PROFILES["food_commerce"].schema_org_classes
    assert "DigitalDocument" in PROFILES["signature_approval"].schema_org_classes
    tables = [
        _t("orders", ["order_id", "customer_id", "total"], [[1, 2, 3], [2, 3, 4]]),
        _t("order_items", ["order_item_id", "order_id", "product_id", "quantity"],
           [[1, 1, 4, 2], [2, 2, 5, 1]]),
    ]
    roles = detect_roles(tables)
    assert {(item.profile, item.role) for item in roles} == {
        ("food_commerce", "order"), ("food_commerce", "order_item"),
    }
    profiles = detect_profiles(tables)
    assert profiles[0].profile == "food_commerce"
    assert detect_roles([_t("data", ["id", "name", "status", "amount"], [[1, "a", "x", 2]])]) == ()


def test_domain_and_serving_faithful_benchmarks_pass_their_contracts():
    domain = run_domain_benchmark()
    assert set(domain) == set(PROFILES)
    assert all(metric.passed and metric.positive_cases >= 25 and metric.negative_cases >= 100
               for metric in domain.values()), domain
    serving = run_serving_benchmark()
    assert serving.passed and serving.strict_candidate_pool_recall == 1.0, serving


def test_public_product_template_development_corpus_is_source_independent_and_green():
    metrics = run_public_template_benchmark()
    assert metrics.cases == 35
    assert metrics.profile_precision == metrics.profile_recall == 1.0
    assert metrics.role_precision == metrics.role_recall == 1.0
    assert not metrics.failures


def test_deployment_allowlist_requires_registry_approval_and_controls_active_source():
    assert deployment_dataset_allowlist(None) == frozenset()
    assert deployment_dataset_allowlist(" iana_country ") == frozenset({"iana_country"})
    for value in ("missing", "cldr_currency"):
        try:
            deployment_dataset_allowlist(value); raise AssertionError
        except ValueError:
            pass

    country = REGISTRY["iana_country"]
    store = _AdapterStore({
        "iana_country": (
            _source_row(country, alpha2="FR", name="France"),
            _source_row(country, alpha2="US", name="United States"),
        ),
    })
    tables = [_t("customers", ["id", "country_code"], [[1, "FR"], [2, "US"]])]
    off = EnrichmentRuntime(
        store, identity=RuntimeIdentity("build", "model", "planner", "ranker")
    ).prepare(tables, "Show the country name for each customer")
    assert not off.used
    on = EnrichmentRuntime(
        store, enabled_datasets={"iana_country"},
        identity=RuntimeIdentity("build", "model", "planner", "ranker"),
    ).prepare(tables, "Show the country name for each customer")
    assert on.added_tables == ("iana_country",)
    assert on.manifest.dataset_snapshots["iana_country"].source_schema == "iana"


def test_iana_country_clears_the_dataset_activation_gate():
    metrics = run_iana_country_activation_benchmark()
    assert metrics.passed, metrics
    assert metrics.positive_cases == 25 and metrics.negative_cases == 100
    assert metrics.selection_precision == metrics.selection_recall == 1.0
    assert metrics.harmful_selection_rate == 0.0
    assert metrics.strict_candidate_pool_recall == metrics.top1_accuracy == 1.0
    assert metrics.deterministic_replay


def test_runtime_is_noop_until_activation_and_emits_complete_manifest_in_evaluation():
    tables = [_t("orders", ["order_id", "currency", "amount"],
                 [[1, "USD", 20], [2, "EUR", 30], [3, "GBP", 40]])]
    store = SnapshotStore(lambda: None)
    production = EnrichmentRuntime(
        store, identity=RuntimeIdentity("test", "sha256:model")
    ).prepare(tables, "Show the currency symbol for every order", as_of="2026-08-17")
    assert not production.used and production.tables == tuple(tables)
    assert not production.outcomes and production.manifest is None

    evaluation = EnrichmentRuntime(
        store, allow_evaluation=True, identity=RuntimeIdentity("test", "sha256:model")
    ).prepare(
        tables, "Show the currency symbol for every order", as_of="2026-08-17",
        private_reference_versions=table_versions([_t("catalog", ["sku"], [["A"]])]),
    )
    assert evaluation.used and evaluation.added_tables == ("currency_iso4217",)
    assert evaluation.explicit_fks[0].from_columns == ("currency",)
    record = evaluation.manifest.record()
    assert record["domain_profile_version"] == DOMAIN_PROFILE_VERSION
    assert record["dataset_snapshots"]["currency_iso4217"]["source_schema"] == "embedded"
    assert record["private_reference_versions"]["catalog"].startswith("sha256:")
    assert record["digest"] == evaluation.manifest.digest()

    baseline = EnrichmentRuntime(
        store, allow_evaluation=True, identity=RuntimeIdentity("test", "sha256:model")
    ).prepare(tables, "Total amount by currency")
    assert not baseline.used and baseline.request_attributes == frozenset()
    wrong_domain = EnrichmentRuntime(
        store, allow_evaluation=True, identity=RuntimeIdentity("test", "sha256:model")
    ).prepare(
        [_t("data", ["id", "currency"], [[1, "USD"], [2, "EUR"]])],
        "Show the currency symbol",
    )
    assert not wrong_domain.used, "domain-specific compatibility must be a real selection gate"


def test_runtime_edges_use_planner_table_names_and_do_not_invent_as_of():
    tables = [{
        "name": "Order Items",
        "columns": ["order_id", "currency"],
        "rows": [[1, "USD"], [2, "EUR"]],
    }]
    runtime = EnrichmentRuntime(
        SnapshotStore(lambda: None), allow_evaluation=True,
        identity=RuntimeIdentity("build", "model", "planner", "ranker"),
    )
    plan = runtime.prepare(tables, "Show the currency symbol for every order")
    assert plan.used
    assert plan.explicit_fks[0].from_table == "Order_Items"
    assert plan.manifest is not None and plan.manifest.as_of == ""
    normalized, fks = TableQuery().ingest(plan.tables, explicit_fks=plan.explicit_fks)
    assert {table["name"] for table in normalized} >= {"Order_Items", "currency_iso4217"}
    assert any(fk["from_table"] == "Order_Items" and fk.get("explicit") for fk in fks)


def test_runtime_materializes_once_and_connects_every_eligible_table():
    tables = [
        _t("orders", ["id", "currency"], [[1, "USD"], [2, "EUR"]]),
        _t("invoices", ["id", "currency_code"], [[1, "GBP"], [2, "EUR"]]),
    ]
    plan = EnrichmentRuntime(
        SnapshotStore(lambda: None), allow_evaluation=True,
        identity=RuntimeIdentity("build", "model", "planner", "ranker"),
    ).prepare(tables, "Show the currency symbol for orders and invoices")
    assert plan.added_tables == ("currency_iso4217",)
    assert {(edge.from_table, edge.from_col) for edge in plan.explicit_fks} == {
        ("orders", "currency"), ("invoices", "currency_code"),
    }
    _, fks = TableQuery().ingest(plan.tables, explicit_fks=plan.explicit_fks)
    assert sum(bool(fk.get("explicit")) for fk in fks) == 2


def test_runtime_source_failure_abstains_without_changing_own_data():
    class FailingStore:
        def load_by_keys(self, *args, **kwargs):
            raise RuntimeError("database unavailable")

    tables = [_t("orders", ["id", "currency"], [[1, "USD"], [2, "EUR"]])]
    plan = EnrichmentRuntime(
        FailingStore(), allow_evaluation=True,
        identity=RuntimeIdentity("build", "model", "planner", "ranker"),
    ).prepare(tables, "Show the currency symbol for every order")
    assert not plan.used and plan.tables == tuple(tables) and plan.manifest is None
    assert plan.warnings == (
        "currency_iso4217: source lookup failed (RuntimeError); enrichment abstained",
    )


class _FakeCursor:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls
        self.closed = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, rows, calls):
        self.cursor_value = _FakeCursor(rows, calls)
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def test_snapshot_store_pins_release_and_bounds_qualified_lookup():
    calls = []
    rowsets = [
        [("2026c+sha256:abc", 1)],
        [("FR", "France")],
    ]

    def factory():
        return _FakeConnection(rowsets.pop(0), calls)

    definition = REGISTRY["iana_country"]
    store = SnapshotStore(factory)
    pin = store.active_snapshot(definition)
    assert pin.source_schema == "iana" and pin.schema_version == 1
    loaded = store.load_by_keys(definition, pin, [("FR",)])
    assert loaded.rows == (("FR", "France"),) and not loaded.multi_match_keys
    assert 'FROM "iana"."release"' in calls[0][0]
    assert 'FROM "iana"."country_code"' in calls[1][0]
    assert calls[1][1][0] == pin.release_id and calls[1][1][-1] == 5001

    stale = SnapshotPin("iana", pin.release_id, 1, "sha256:stale")
    try:
        store.load_by_keys(definition, stale, [("FR",)]); raise AssertionError
    except ValueError:
        pass


def test_snapshot_store_detects_unique_contract_violation():
    definition = REGISTRY["iana_country"]
    pin = SnapshotPin("iana", "r1", 1, definition.definition_id)
    store = SnapshotStore(lambda: _FakeConnection(
        [("FR", "France"), ("FR", "Duplicate")], []
    ))
    try:
        store.load_by_keys(definition, pin, [("FR",)]); raise AssertionError
    except SourceContractError:
        pass


def test_private_product_corpus_is_metadata_only_consent_bound_and_replayable():
    payload = {
        "schema_version": 1,
        "corpus_id": "customer-metadata-2026-08",
        "consent": {"opted_in": True, "metadata_only": True, "contains_row_values": False},
        "cases": [{
            "case_id": "case-001",
            "product": "Neartail",
            "cohort": "food-order",
            "table_name": "Form Responses 1",
            "columns": ["Order number", "Customer name", "Order total"],
            "expected_profile": "food_commerce",
            "expected_roles": ["order"],
            "question": "What is the total order amount?",
            "gold_ast_shape": "sum",
        }],
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "corpus.json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        first = load_private_template_corpus(path)
        second = load_private_template_corpus(path)
        assert first == second
        assert first.digest.startswith("sha256:") and len(first.cases) == 1
        assert first.cases[0].source_url == "opted-in://customer-metadata-2026-08/case-001"

        for mutation in (
            lambda value: value["cases"][0].update({"rows": [["private value"]]}),
            lambda value: value["consent"].update({"contains_row_values": True}),
            lambda value: value["cases"][0].update({"expected_roles": ["not_a_role"]}),
        ):
            candidate = json.loads(json.dumps(payload))
            mutation(candidate)
            path.write_text(json.dumps(candidate), encoding="utf-8")
            try:
                load_private_template_corpus(path); raise AssertionError
            except ValueError:
                pass


TESTS = [
    test_registry_validates_and_pins,
    test_snapshot_and_registry_ids_are_content_complete,
    test_eligibility_required_optional_disqualifying,
    test_value_typing_is_conservative,
    test_integration_spike_enters_discover_fks_path,
    test_renamed_column_edge_is_not_yet_bridged_by_discover_fks,
    test_ineligible_datasets_never_selected,
    test_selection_policy_floors_and_tie_break,
    test_single_key_datasets_only,
    test_selection_is_independent_of_compose_routing,
    test_execution_manifest_pins_private_references,
    test_benchmark_accepts_currency_and_rejects_overeager,
    test_coverage_is_row_level_and_can_fail_independently,
    test_benchmark_requires_positive_and_negative_support,
    test_source_registry_models_measured_data_shapes_without_enabling_them,
    test_composite_explicit_edges_are_typed_for_the_planner,
    test_composite_edge_survives_canonical_table_ingestion,
    test_requested_attribute_extraction_is_explicit_and_contrastive,
    test_source_adapters_preserve_activation_temporal_and_ambiguity_outcomes,
    test_source_adapter_enforces_row_render_policy,
    test_domain_profile_registry_and_typing_are_conservative,
    test_domain_and_serving_faithful_benchmarks_pass_their_contracts,
    test_public_product_template_development_corpus_is_source_independent_and_green,
    test_deployment_allowlist_requires_registry_approval_and_controls_active_source,
    test_iana_country_clears_the_dataset_activation_gate,
    test_runtime_is_noop_until_activation_and_emits_complete_manifest_in_evaluation,
    test_runtime_edges_use_planner_table_names_and_do_not_invent_as_of,
    test_runtime_materializes_once_and_connects_every_eligible_table,
    test_runtime_source_failure_abstains_without_changing_own_data,
    test_snapshot_store_pins_release_and_bounds_qualified_lookup,
    test_snapshot_store_detects_unique_contract_violation,
    test_private_product_corpus_is_metadata_only_consent_bound_and_replayable,
]


def main():
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nenrichment M0: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
