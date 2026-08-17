"""Source-cited product-template development corpus for domain-role recognition.

This corpus is derived from public product/template pages. It is deliberately separate from
``engine.domain_profiles`` and is not a substitute for opted-in held-out customer metadata.
The generic sheet names exercise the shape actually exported by Google Forms.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from engine.domain_profiles import PROFILES
from engine.domain_typing import detect_profiles, detect_roles


@dataclass(frozen=True)
class ProductTemplateCase:
    case_id: str
    product: str
    source_url: str
    template: str
    expected_profile: str
    expected_roles: tuple[str, ...]
    columns: tuple[str, ...]
    question: str
    gold_ast_shape: str
    table_name: str = "Form Responses 1"

    def table(self) -> dict:
        rows = [
            [f"sample_{row}_{column}" for column in range(len(self.columns))]
            for row in range(2)
        ]
        return {"name": self.table_name, "columns": list(self.columns), "rows": rows}


@dataclass(frozen=True)
class ProductTemplateMetrics:
    profile_precision: float
    profile_recall: float
    role_precision: float
    role_recall: float
    cases: int
    failures: tuple[str, ...]


@dataclass(frozen=True)
class PrivateTemplateCorpus:
    """Validated, consented metadata-only cases and their replay identity."""

    corpus_id: str
    digest: str
    cases: tuple[ProductTemplateCase, ...]


_PRIVATE_ROOT_FIELDS = frozenset(("schema_version", "corpus_id", "consent", "cases"))
_PRIVATE_CONSENT_FIELDS = frozenset(("opted_in", "metadata_only", "contains_row_values"))
_PRIVATE_CASE_FIELDS = frozenset((
    "case_id", "product", "cohort", "table_name", "columns", "expected_profile",
    "expected_roles", "question", "gold_ast_shape",
))
_PRIVATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _exact_fields(value: dict, expected: frozenset[str], context: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        raise ValueError(
            f"{context} fields differ: missing={sorted(missing)} unknown={sorted(unknown)}"
        )


def _nonempty_string(value, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value.strip()


def _private_id(value, context: str) -> str:
    value = _nonempty_string(value, context)
    if not _PRIVATE_ID.fullmatch(value):
        raise ValueError(f"{context} must contain only letters, digits, dot, underscore, or hyphen")
    return value


def load_private_template_corpus(path: str | Path) -> PrivateTemplateCorpus:
    """Load an opted-in metadata-only corpus; reject payloads that could contain row data."""
    raw = Path(path).read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("private corpus must be UTF-8 JSON") from exc
    _exact_fields(payload, _PRIVATE_ROOT_FIELDS, "corpus")
    if payload["schema_version"] != 1:
        raise ValueError("private corpus schema_version must be 1")
    corpus_id = _private_id(payload["corpus_id"], "corpus_id")

    consent = payload["consent"]
    _exact_fields(consent, _PRIVATE_CONSENT_FIELDS, "consent")
    if consent != {"opted_in": True, "metadata_only": True, "contains_row_values": False}:
        raise ValueError("private corpus requires explicit opted-in, metadata-only consent")
    if not isinstance(payload["cases"], list) or not payload["cases"]:
        raise ValueError("private corpus must contain at least one case")

    cases = []
    seen_ids = set()
    for index, item in enumerate(payload["cases"]):
        context = f"cases[{index}]"
        _exact_fields(item, _PRIVATE_CASE_FIELDS, context)
        case_id = _private_id(item["case_id"], f"{context}.case_id")
        if case_id in seen_ids:
            raise ValueError(f"duplicate private corpus case_id: {case_id}")
        seen_ids.add(case_id)
        profile_name = _nonempty_string(item["expected_profile"], f"{context}.expected_profile")
        profile = PROFILES.get(profile_name)
        if profile is None:
            raise ValueError(f"unknown expected profile: {profile_name}")
        columns = item["columns"]
        if not isinstance(columns, list) or len(columns) < 2:
            raise ValueError(f"{context}.columns must contain at least two names")
        columns = tuple(_nonempty_string(value, f"{context}.columns") for value in columns)
        if len(set(columns)) != len(columns):
            raise ValueError(f"{context}.columns must be unique")
        expected_roles = item["expected_roles"]
        if not isinstance(expected_roles, list) or not expected_roles:
            raise ValueError(f"{context}.expected_roles must be a non-empty list")
        expected_roles = tuple(
            _nonempty_string(value, f"{context}.expected_roles") for value in expected_roles
        )
        known_roles = {role.name for role in profile.roles}
        unknown_roles = set(expected_roles) - known_roles
        if unknown_roles:
            raise ValueError(f"roles are not in {profile_name}: {sorted(unknown_roles)}")
        cases.append(ProductTemplateCase(
            case_id=case_id,
            product=_nonempty_string(item["product"], f"{context}.product"),
            source_url=f"opted-in://{corpus_id}/{case_id}",
            template=_nonempty_string(item["cohort"], f"{context}.cohort"),
            expected_profile=profile_name,
            expected_roles=expected_roles,
            columns=columns,
            question=_nonempty_string(item["question"], f"{context}.question"),
            gold_ast_shape=_nonempty_string(item["gold_ast_shape"], f"{context}.gold_ast_shape"),
            table_name=_nonempty_string(item["table_name"], f"{context}.table_name"),
        ))
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PrivateTemplateCorpus(corpus_id, digest, tuple(cases))


FORMESIGN_INTAKE = "https://formesign.com/intake-forms/intake.html"
FORMESIGN_SIGNATURE = "https://formesign.com/signature-forms/"
NEARTAIL_CATALOG = "https://neartail.com/order-forms/grocery.html"
NEARTAIL_ORDERS = "https://neartail.com/google-order-forms/"
NEARTAIL_PAYMENTS = "https://neartail.com/payment/"
FORMFACADE_SCORE = "https://formfacade.com/calculate/"
FORMFACADE_WEBSITE = "https://formfacade.com/website/"


CASES = (
    # Shared party/location structures found across all three products.
    ProductTemplateCase("common-contact", "Formfacade", FORMFACADE_WEBSITE, "Contact form",
        "common_party_location", ("person",),
        ("Timestamp", "Full name", "Email address", "Phone number"),
        "How many contacts submitted the form?", "count_rows"),
    ProductTemplateCase("common-customer", "Neartail", NEARTAIL_ORDERS, "Customer checkout",
        "common_party_location", ("customer",),
        ("Timestamp", "Customer number", "Customer name", "Customer email", "Mobile phone"),
        "List customer names and email addresses", "project"),
    ProductTemplateCase("common-organization", "Formfacade", FORMFACADE_WEBSITE, "Business inquiry",
        "common_party_location", ("organization",),
        ("Timestamp", "Company ID", "Legal business name", "Company website", "Contact email"),
        "How many organizations submitted an inquiry?", "count_rows"),
    ProductTemplateCase("common-address", "Neartail", NEARTAIL_ORDERS, "Delivery address",
        "common_party_location", ("address",),
        ("Timestamp", "Address line 1", "Address line 2", "City", "Postal code", "Country"),
        "List delivery cities and postal codes", "project"),
    ProductTemplateCase("common-location", "Formesign", FORMESIGN_SIGNATURE, "Work location",
        "common_party_location", ("address", "location"),
        ("Timestamp", "Site name", "Street address", "City", "State or region", "Country"),
        "Count responses by city", "group_count"),

    # Neartail food-commerce exports and reports.
    ProductTemplateCase("food-grocery-order", "Neartail", NEARTAIL_CATALOG, "Grocery order form",
        "food_commerce", ("order",),
        ("Timestamp", "Order number", "Customer name", "Order date", "Order total", "Order status"),
        "What is the total order amount?", "sum"),
    ProductTemplateCase("food-line-items", "Neartail", NEARTAIL_ORDERS, "Order line items report",
        "food_commerce", ("order_item",),
        ("Order line ID", "Order number", "Product code", "Product name", "Quantity", "Line total"),
        "Which product has the highest quantity sold?", "group_sum_argmax"),
    ProductTemplateCase("food-menu", "Neartail", NEARTAIL_ORDERS, "A la carte menu",
        "food_commerce", ("menu_item",),
        ("Menu item code", "Item name", "Menu category", "Unit price", "Available"),
        "List available menu items and prices", "project_filter"),
    ProductTemplateCase("food-payment", "Neartail", NEARTAIL_PAYMENTS, "Order payment report",
        "food_commerce", ("payment",),
        ("Payment ID", "Order number", "Payment method", "Amount paid", "Payment status", "Paid at"),
        "What is the total amount paid?", "sum"),
    ProductTemplateCase("food-fulfillment", "Neartail", NEARTAIL_ORDERS, "Pickup and delivery list",
        "food_commerce", ("fulfillment",),
        ("Fulfillment ID", "Order number", "Pickup or delivery", "Delivery address", "Pickup time", "Status"),
        "How many deliveries are pending?", "count_filter"),

    # Neartail/Formfacade registration, booking, and membership workflows.
    ProductTemplateCase("registration-event", "Neartail", NEARTAIL_PAYMENTS, "Event registration form",
        "registration_booking", ("registration",),
        ("Timestamp", "Registration ID", "Event name", "Attendee name", "Attendee email", "Registration status"),
        "How many attendees registered?", "count_rows"),
    ProductTemplateCase("registration-course", "Neartail", NEARTAIL_CATALOG, "Course registration form",
        "registration_booking", ("registration",),
        ("Enrollment number", "Course code", "Student name", "Enrolled on", "Enrollment status"),
        "Count enrollments by course", "group_count"),
    ProductTemplateCase("registration-booking", "Neartail", NEARTAIL_CATALOG, "Hotel reservation form",
        "registration_booking", ("booking",),
        ("Reservation number", "Guest name", "Check-in date", "Check-out date", "Room type", "Booking status"),
        "How many reservations are confirmed?", "count_filter"),
    ProductTemplateCase("registration-membership", "Neartail", NEARTAIL_CATALOG, "Membership application form",
        "registration_booking", ("membership",),
        ("Membership ID", "Member name", "Membership type", "Start date", "Renewal date", "Status"),
        "Count active memberships by type", "group_count_filter"),
    ProductTemplateCase("registration-appointment", "Formfacade", FORMFACADE_WEBSITE, "Appointment request",
        "registration_booking", ("appointment",),
        ("Appointment ID", "Customer name", "Appointment date", "Appointment time", "Service requested", "Status"),
        "List upcoming appointment times", "project_order"),

    # Formfacade customer-facing forms and scoring workflows.
    ProductTemplateCase("lead-inquiry", "Formfacade", FORMFACADE_WEBSITE, "Customer inquiry form",
        "lead_crm", ("lead",),
        ("Timestamp", "Lead ID", "Contact name", "Work email", "Company", "Inquiry details", "Lead status"),
        "How many new leads are there?", "count_filter"),
    ProductTemplateCase("lead-score", "Formfacade", FORMFACADE_SCORE, "Lead scoring form",
        "lead_crm", ("lead_score",),
        ("Lead ID", "Company size score", "Budget score", "Need score", "Overall score", "Score category"),
        "Which leads have the highest overall score?", "order_limit"),
    ProductTemplateCase("lead-application", "Formfacade", FORMFACADE_WEBSITE, "Vendor application form",
        "registration_booking", ("registration",),
        ("Application ID", "Applicant name", "Business name", "Submitted at", "Application status", "Reviewer notes"),
        "Count submitted applications by status", "group_count"),
    ProductTemplateCase("lead-follow-up", "Formfacade", FORMFACADE_WEBSITE, "Sales follow-up tracker",
        "lead_crm", ("follow_up",),
        ("Follow-up ID", "Lead ID", "Assigned owner", "Follow-up date", "Next action", "Outcome"),
        "List overdue follow-ups by owner", "project_filter"),
    ProductTemplateCase("lead-assessment", "Formfacade", FORMFACADE_SCORE, "Business assessment",
        "lead_crm", ("lead_score",),
        ("Lead ID", "Respondent email", "Section score", "Overall score", "Recommendation", "Submitted at"),
        "What is the average overall score?", "avg"),

    # Formesign signatures, approvals, and consent.
    ProductTemplateCase("signature-consent", "Formesign", FORMESIGN_SIGNATURE, "Video consent form",
        "signature_approval", ("consent_record",),
        ("Consent ID", "Participant name", "Authorization granted", "Signed at", "Signature", "Consent status"),
        "How many consent records were signed?", "count_filter"),
    ProductTemplateCase("signature-ack", "Formesign", FORMESIGN_SIGNATURE, "Policy acknowledgement form",
        "signature_approval", ("signature_request",),
        ("Signature request ID", "Document ID", "Employee name", "Acknowledged at", "Signature", "Status"),
        "How many signature requests are complete?", "count_filter"),
    ProductTemplateCase("signature-agreement", "Formesign", FORMESIGN_SIGNATURE, "Equipment rental agreement",
        "signature_approval", ("document",),
        ("Document ID", "Agreement title", "Document version", "Effective date", "Owner organization"),
        "List agreement titles and versions", "project"),
    ProductTemplateCase("signature-signer", "Formesign", FORMESIGN_SIGNATURE, "Reference request",
        "signature_approval", ("signer",),
        ("Signer ID", "Reference request ID", "Applicant email", "Reference name", "Signer email", "Signed at"),
        "List signers who completed the request", "project_filter"),
    ProductTemplateCase("signature-approval", "Formesign", FORMESIGN_SIGNATURE, "Purchase request approval",
        "signature_approval", ("approval_step",),
        ("Approval step ID", "Document ID", "Approver name", "Approval sequence", "Approval status", "Approved at"),
        "Which approval steps are pending?", "project_filter"),

    # Formesign healthcare intake and assessment templates.
    ProductTemplateCase("health-patient", "Formesign", FORMESIGN_INTAKE, "Patient intake form",
        "healthcare_intake", ("patient", "patient_intake"),
        ("Timestamp", "Patient ID", "Patient name", "Date of birth", "Emergency contact name", "Primary health concern"),
        "How many patient intakes were submitted?", "count_rows"),
    ProductTemplateCase("health-medical", "Formesign", FORMESIGN_INTAKE, "Medical intake form",
        "healthcare_intake", ("patient_intake",),
        ("Patient ID", "Current health concerns", "Medical history", "Allergies", "Current medications", "Submitted at"),
        "How many patients reported allergies?", "count_filter"),
    ProductTemplateCase("health-assessment", "Formesign", FORMESIGN_INTAKE, "Patient Health Questionnaire-9",
        "healthcare_intake", ("assessment",),
        ("Assessment ID", "Patient ID", "Questionnaire name", "Overall score", "Severity category", "Submitted at"),
        "What is the average assessment score?", "avg"),
    ProductTemplateCase("health-condition", "Formesign", FORMESIGN_INTAKE, "Dermatology intake form",
        "healthcare_intake", ("condition",),
        ("Diagnosis ID", "Patient ID", "Condition name", "ICD code", "Diagnosed on", "Condition status"),
        "Count conditions by ICD code", "group_count"),
    ProductTemplateCase("health-medication", "Formesign", FORMESIGN_INTAKE, "Medication history",
        "healthcare_intake", ("medication",),
        ("Medication ID", "Patient ID", "Medication name", "Dose", "Frequency", "Started on"),
        "List current medications by patient", "project"),

    # Formesign safety and compliance templates.
    ProductTemplateCase("safety-incident", "Formesign", FORMESIGN_SIGNATURE, "Incident report form",
        "safety_compliance", ("incident",),
        ("Incident ID", "Work site", "Employee ID", "Date and time of incident", "Severity", "Incident status"),
        "Count incidents by severity", "group_count"),
    ProductTemplateCase("safety-near-miss", "Formesign", FORMESIGN_SIGNATURE, "Near miss report form",
        "safety_compliance", ("incident",),
        ("Report ID", "Site ID", "Reported by", "Near miss date", "Potential severity", "Follow-up status"),
        "How many near misses require follow-up?", "count_filter"),
    ProductTemplateCase("safety-inspection", "Formesign", FORMESIGN_SIGNATURE, "Workplace safety inspection",
        "safety_compliance", ("inspection",),
        ("Inspection ID", "Site ID", "Equipment ID", "Inspector name", "Inspected at", "Inspection status"),
        "Count failed inspections by site", "group_count_filter"),
    ProductTemplateCase("safety-finding", "Formesign", FORMESIGN_SIGNATURE, "Forklift inspection checklist",
        "safety_compliance", ("finding",),
        ("Finding ID", "Inspection ID", "Observation", "Risk severity", "Finding status", "Photo link"),
        "List open high-severity findings", "project_filter"),
    ProductTemplateCase("safety-capa", "Formesign", FORMESIGN_SIGNATURE, "CAPA form",
        "safety_compliance", ("corrective_action",),
        ("Corrective action ID", "Finding ID", "Action owner", "Due date", "Completion date", "Action status"),
        "Which corrective actions are overdue?", "project_filter"),
)


def run_template_benchmark(cases, *, require_all_profiles: bool = False) -> ProductTemplateMetrics:
    if not cases:
        raise ValueError("template corpus must not be empty")
    if require_all_profiles and {case.expected_profile for case in cases} != set(PROFILES):
        raise ValueError("template corpus must cover every registered domain profile")
    profile_tp = profile_fp = profile_fn = role_tp = role_fp = role_fn = 0
    failures = []
    for case in cases:
        table = case.table()
        actual_profiles = {item.profile for item in detect_profiles((table,))}
        expected_profile = {case.expected_profile}
        profile_tp += len(actual_profiles & expected_profile)
        profile_fp += len(actual_profiles - expected_profile)
        profile_fn += len(expected_profile - actual_profiles)
        actual_roles = {
            item.role for item in detect_roles((table,)) if item.profile == case.expected_profile
        }
        expected_roles = set(case.expected_roles)
        role_tp += len(actual_roles & expected_roles)
        role_fp += len(actual_roles - expected_roles)
        role_fn += len(expected_roles - actual_roles)
        if actual_profiles != expected_profile or actual_roles != expected_roles:
            failures.append(
                f"{case.case_id}: profiles={sorted(actual_profiles)} roles={sorted(actual_roles)}"
            )
    precision = lambda tp, fp: tp / (tp + fp) if tp + fp else 0.0
    recall = lambda tp, fn: tp / (tp + fn) if tp + fn else 0.0
    return ProductTemplateMetrics(
        precision(profile_tp, profile_fp), recall(profile_tp, profile_fn),
        precision(role_tp, role_fp), recall(role_tp, role_fn), len(cases), tuple(failures),
    )


def run_public_template_benchmark(cases=CASES) -> ProductTemplateMetrics:
    return run_template_benchmark(cases, require_all_profiles=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-corpus", help="opted-in metadata-only JSON corpus")
    args = parser.parse_args()
    if args.private_corpus:
        corpus = load_private_template_corpus(args.private_corpus)
        metrics = run_template_benchmark(corpus.cases, require_all_profiles=True)
        print(f"corpus_id={corpus.corpus_id} digest={corpus.digest}")
    else:
        metrics = run_public_template_benchmark()
    print(metrics)
    raise SystemExit(1 if metrics.failures else 0)


if __name__ == "__main__":
    main()
