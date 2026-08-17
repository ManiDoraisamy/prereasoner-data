"""Serving-shaped domain-role and request-local enrichment evaluation."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from engine.domain_profiles import PROFILES
from engine.domain_typing import detect_profiles, detect_roles
from engine.enrichment.runtime import EnrichmentRuntime, RuntimeIdentity
from engine.enrichment.store import SnapshotStore
from engine.sql_search import SQLSearcher


@dataclass(frozen=True)
class DomainCase:
    case_id: str
    tables: tuple[dict, ...]
    gold_profiles: frozenset[str]
    gold_roles: frozenset[tuple[str, str, str]]


@dataclass(frozen=True)
class DomainMetrics:
    precision: float
    recall: float
    positive_cases: int
    negative_cases: int
    passed: bool


@dataclass(frozen=True)
class ServingMetrics:
    no_intent_unchanged: bool
    selected_dataset: bool
    strict_candidate_pool_recall: float
    top1_execution_correct: bool
    manifest_complete: bool
    passed: bool


_PROFILE_FIXTURES = {
    "common_party_location": (
        ("customer", "customers", ("customer_id", "name", "email", "country")),
        ("person", "contacts", ("contact_id", "full_name", "phone", "email")),
        ("address", "postal_addresses", ("address_id", "street", "city", "postal_code")),
        ("location", "locations", ("location_id", "latitude", "longitude", "country")),
    ),
    "food_commerce": (
        ("order", "orders", ("order_id", "customer_id", "ordered_at", "total", "currency")),
        ("order_item", "order_items", ("order_item_id", "order_id", "product_id", "quantity")),
        ("menu_item", "menu_items", ("menu_item_id", "menu_id", "name", "price")),
        ("product", "products", ("product_id", "sku", "name", "price")),
        ("payment", "payments", ("payment_id", "order_id", "amount", "status")),
    ),
    "registration_booking": (
        ("event", "events", ("event_id", "title", "start_date", "location_id")),
        ("course_session", "course_sessions", ("session_id", "course_id", "starts_at", "capacity")),
        ("registration", "registrations", ("registration_id", "event_id", "person_id", "status")),
        ("booking", "reservations", ("reservation_id", "customer_id", "check_in", "status")),
        ("membership", "memberships", ("membership_id", "person_id", "start_date", "status")),
    ),
    "lead_crm": (
        ("lead", "leads", ("lead_id", "name", "email", "company", "source")),
        ("inquiry", "inquiries", ("inquiry_id", "subject", "message", "service_id")),
        ("campaign", "campaigns", ("campaign_id", "name", "channel", "starts_at")),
        ("service_request", "service_requests", ("request_id", "service_id", "status", "message")),
    ),
    "signature_approval": (
        ("document", "documents", ("document_id", "title", "version", "status")),
        ("signature_request", "signature_requests", ("request_id", "document_id", "status", "created_at")),
        ("signer", "signers", ("signer_id", "request_id", "name", "email")),
        ("approval_step", "approval_steps", ("step_id", "document_id", "sequence", "status")),
        ("consent_record", "consent_records", ("consent_id", "patient_id", "signed_at", "status")),
    ),
    "healthcare_intake": (
        ("patient", "patients", ("patient_id", "name", "date_of_birth", "postal_code")),
        ("provider", "providers", ("provider_id", "npi", "name", "specialty")),
        ("patient_intake", "intake_forms", ("intake_id", "patient_id", "submitted_at", "status")),
        ("assessment", "assessments", ("assessment_id", "patient_id", "form_id", "score")),
        ("condition", "conditions", ("condition_id", "patient_id", "icd_code", "description")),
    ),
    "safety_compliance": (
        ("inspection", "inspections", ("inspection_id", "site_id", "inspected_at", "status")),
        ("incident", "incidents", ("incident_id", "site_id", "occurred_at", "severity")),
        ("finding", "findings", ("finding_id", "inspection_id", "severity", "status")),
        ("corrective_action", "corrective_actions", ("action_id", "finding_id", "due_date", "status")),
        ("equipment", "equipment", ("equipment_id", "name", "serial_number", "site_id")),
    ),
}


def _fixture_table(name: str, columns: tuple[str, ...], sequence: int) -> dict:
    rows = []
    for row_index in range(3):
        rows.append([
            sequence * 10 + row_index if column == "id" or column.endswith("_id")
            else f"{column}_{sequence}_{row_index}"
            for column in columns
        ])
    return {"name": name, "columns": list(columns), "rows": rows}


def build_domain_corpus() -> tuple[DomainCase, ...]:
    cases = []
    for profile_name in sorted(_PROFILE_FIXTURES):
        fixtures = _PROFILE_FIXTURES[profile_name]
        for sequence in range(25):
            role, name, columns = fixtures[sequence % len(fixtures)]
            table = _fixture_table(name, columns, sequence)
            cases.append(DomainCase(
                f"{profile_name}:{sequence}", (table,), frozenset({profile_name}),
                frozenset({(profile_name, role, table["name"])}),
            ))
    generic = (
        ("data", ("id", "name", "status")),
        ("records", ("code", "value", "date")),
        ("summary", ("category", "amount", "count")),
        ("responses", ("question", "answer", "score")),
        ("archive", ("key", "label", "updated_at")),
    )
    for index, (name, columns) in enumerate(generic):
        cases.append(DomainCase(
            f"generic:{index}", ({"name": name, "columns": list(columns),
                                  "rows": [["1"] * len(columns), ["2"] * len(columns)]},),
            frozenset(), frozenset(),
        ))
    return tuple(cases)


DOMAIN_CORPUS = build_domain_corpus()


def run_domain_benchmark(corpus=DOMAIN_CORPUS) -> dict[str, DomainMetrics]:
    expected = set()
    actual = set()
    expected_profiles = set()
    actual_profiles = set()
    for index, case in enumerate(corpus):
        for profile, role, table in case.gold_roles:
            expected.add((index, profile, role, table))
        for item in detect_roles(case.tables):
            actual.add((index, item.profile, item.role, item.table))
        expected_profiles.update((index, profile) for profile in case.gold_profiles)
        actual_profiles.update((index, item.profile) for item in detect_profiles(case.tables))

    metrics = {}
    for profile_name in sorted(PROFILES):
        exp = {item for item in expected if item[1] == profile_name}
        act = {item for item in actual if item[1] == profile_name}
        tp, fp, fn = len(exp & act), len(act - exp), len(exp - act)
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        positive_ids = {item[0] for item in exp}
        negatives = len(corpus) - len(positive_ids)
        profile_exp = {item for item in expected_profiles if item[1] == profile_name}
        profile_act = {item for item in actual_profiles if item[1] == profile_name}
        profile_ok = profile_exp <= profile_act and not (profile_act - profile_exp)
        passed = (len(positive_ids) >= 25 and negatives >= 100
                  and precision >= 0.99 and recall >= 0.95 and profile_ok)
        metrics[profile_name] = DomainMetrics(
            precision, recall, len(positive_ids), negatives, passed,
        )
    return metrics


def _execute(tables, sql):
    connection = sqlite3.connect(":memory:")
    try:
        for table in tables:
            columns = table["columns"]
            connection.execute(
                'CREATE TABLE "' + table["name"] + '" ('
                + ", ".join('"' + column + '"' for column in columns) + ")"
            )
            placeholders = ",".join("?" for _ in columns)
            connection.executemany(
                'INSERT INTO "' + table["name"] + '" VALUES (' + placeholders + ')',
                table["rows"],
            )
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def run_serving_benchmark() -> ServingMetrics:
    tables = ({
        "name": "orders", "columns": ["order_id", "currency", "amount"],
        "rows": [[1, "USD", 20], [2, "EUR", 30], [3, "GBP", 40]],
    },)
    runtime = EnrichmentRuntime(
        SnapshotStore(lambda: None), allow_evaluation=True,
        identity=RuntimeIdentity("benchmark", "sha256:benchmark-model"),
    )
    baseline = runtime.prepare(tables, "Total amount by currency", as_of="2026-08-17")
    unchanged = not baseline.used and baseline.tables == tables and not baseline.explicit_fks

    plan = runtime.prepare(
        tables, "Show the currency symbol for every order", as_of="2026-08-17",
        private_reference_versions={"merchant_catalog": "sha256:catalog"},
    )
    fks = [edge.as_foreign_key(1.0) for edge in plan.explicit_fks]
    candidates = SQLSearcher.from_tables(plan.tables, fks).search(
        "Show the currency symbol for every order"
    )
    gold = [("USD", "$"), ("EUR", "EUR"), ("GBP", "GBP")]
    strict = [candidate for candidate in candidates if _execute(plan.tables, candidate.sql) == gold]
    top1 = bool(candidates and _execute(plan.tables, candidates[0].sql) == gold)
    record = plan.manifest.record() if plan.manifest else {}
    complete = bool(
        record.get("digest") and record.get("domain_profile_version")
        and record.get("dataset_snapshots", {}).get("currency_iso4217")
        and record.get("private_reference_versions", {}).get("merchant_catalog")
    )
    recall = 1.0 if strict else 0.0
    selected = plan.added_tables == ("currency_iso4217",) and len(plan.explicit_fks) == 1
    passed = unchanged and selected and recall >= 0.95 and top1 and complete
    return ServingMetrics(unchanged, selected, recall, top1, complete, passed)
