"""Canonical market-led domain profiles and internal table-role contracts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class RoleDefinition:
    name: str
    aliases: tuple[str, ...]
    column_groups: tuple[tuple[str, ...], ...] = ()
    schema_org_classes: tuple[str, ...] = ()
    distinctive: bool = True
    structural_min_groups: int = 0


@dataclass(frozen=True)
class DomainProfile:
    name: str
    roles: tuple[RoleDefinition, ...]
    schema_org_classes: tuple[str, ...]


def _role(name, aliases, columns=(), classes=(), *, distinctive=True, structural=None):
    groups = tuple(tuple(group) for group in columns)
    structural = len(groups) if structural is None else structural
    return RoleDefinition(name, tuple(aliases), tuple(tuple(group) for group in columns),
                          tuple(classes), distinctive, structural)


_PROFILES = (
    DomainProfile("common_party_location", (
        _role("person", ("people", "person", "persons", "contacts"),
              (("name", "full_name", "first_name", "contact_name"),
               ("email", "email_address", "phone", "phone_number", "address")),
              ("Person", "ContactPoint"), distinctive=False),
        _role("customer", ("customers", "customer", "clients", "client"),
              (("customer_id", "customer_number", "client_id", "id"),
               ("customer_name", "name", "customer_email", "email", "mobile_phone", "phone")),
              ("Person", "Organization"), distinctive=False),
        _role("organization", ("organizations", "organization", "companies", "company", "businesses"),
              (("organization_id", "company_id", "company_number", "id"),
               ("company_name", "business_name", "name", "legal_name", "legal_business_name")),
              ("Organization", "LocalBusiness"), distinctive=False),
        _role("address", ("addresses", "address", "postal_addresses"),
              (("street", "street_address", "address_line1", "address_line_1", "line1"),
               ("city", "postal_code", "zip", "country")),
              ("PostalAddress",), distinctive=False),
        _role("location", ("locations", "location", "places", "place"),
              (("location_id", "site_name", "latitude", "lat", "city"),
               ("street_address", "longitude", "lng", "country", "state_or_region")),
              ("Place", "City", "Country", "AdministrativeArea", "GeoCoordinates"), distinctive=False),
        _role("service", ("services", "service"),
              (("service_id", "id"), ("name", "description")), ("Service",), distinctive=False),
    ), ("Person", "Organization", "LocalBusiness", "ContactPoint", "PostalAddress", "Place",
        "Country", "AdministrativeArea", "City", "GeoCoordinates", "Service")),
    DomainProfile("food_commerce", (
        _role("merchant", ("merchants", "merchant", "restaurants", "restaurant", "stores", "store"),
              (("merchant_id", "restaurant_id", "store_id", "id"), ("name", "address")),
              ("FoodEstablishment", "Restaurant", "Bakery", "GroceryStore", "LocalBusiness")),
        _role("product", ("products", "product", "catalog_items", "catalog_item"),
              (("product_id", "sku", "gtin", "id"), ("name", "price")), ("Product",)),
        _role("product_group", ("product_groups", "product_group", "categories", "category"),
              (("group_id", "category_id", "id"), ("name", "title")), ("ProductGroup",)),
        _role("variant", ("variants", "variant", "product_variants", "menu_item_variants"),
              (("variant_id", "sku", "id"), ("product_id", "menu_item_id")), ("ProductGroup",)),
        _role("offer", ("offers", "offer", "prices", "pricing"),
              (("offer_id", "id"), ("price", "currency")), ("Offer", "PriceSpecification")),
        _role("menu", ("menus", "menu"), (("menu_id", "id"), ("name", "title")), ("Menu",)),
        _role("menu_item", ("menu_items", "menu_item", "dishes", "dish"),
              (("menu_item_id", "menu_item_code", "item_id", "id"),
               ("menu_id", "menu_category", "unit_price", "price")), ("MenuItem",)),
        _role("order", ("orders", "order", "sales_orders", "purchase_orders"),
              (("order_id", "order_number", "id"),
               ("customer_id", "order_date", "ordered_at", "order_total", "total")), ("Order",)),
        _role("order_item", ("order_items", "order_item", "line_items", "order_lines"),
              (("order_item_id", "order_line_id", "line_id", "id"),
               ("order_id", "order_number"),
               ("product_id", "product_code", "quantity")),
              ("OrderItem", "QuantitativeValue")),
        _role("invoice", ("invoices", "invoice"),
              (("invoice_id", "id"), ("amount", "total", "currency")), ("Invoice", "MonetaryAmount")),
        _role("payment", ("payments", "payment", "payment_records", "transactions"),
              (("payment_id", "transaction_id", "id"),
               ("amount", "amount_paid", "payment_status", "status")),
              ("PaymentChargeSpecification", "MonetaryAmount")),
        _role("fulfillment", ("fulfillments", "fulfillment", "deliveries", "delivery", "shipments"),
              (("fulfillment_id", "delivery_id", "shipment_id", "id"),
               ("order_id", "order_number", "pickup_or_delivery", "delivery_address", "status")),
              ("ParcelDelivery", "DeliveryChargeSpecification")),
    ), ("Product", "ProductGroup", "Offer", "Order", "OrderItem", "Invoice", "PriceSpecification",
        "UnitPriceSpecification", "PaymentChargeSpecification", "DeliveryChargeSpecification",
        "QuantitativeValue", "MonetaryAmount", "ParcelDelivery", "Menu", "MenuItem",
        "FoodEstablishment", "Restaurant", "Bakery", "GroceryStore")),
    DomainProfile("registration_booking", (
        _role("event", ("events", "event", "education_events"),
              (("event_id", "id"), ("name", "title"), ("start_date", "starts_at", "date")),
              ("Event", "EducationEvent")),
        _role("course", ("courses", "course", "classes", "class"),
              (("course_id", "id"), ("name", "title")), ("Course",)),
        _role("course_session", ("course_sessions", "course_session", "sessions", "session"),
              (("session_id", "id"), ("course_id", "event_id"), ("start_date", "starts_at")),
              ("CourseInstance", "Schedule")),
        _role("registration", ("registrations", "registration", "event_registrations", "applications"),
              (("registration_id", "registration_number", "application_id", "enrollment_id",
                "enrollment_number", "id"),
               ("event_id", "event_name", "course_id", "course_code", "attendee_name",
                "applicant_name", "person_id")),
              ("EventReservation",)),
        _role("booking", ("bookings", "booking", "reservations", "reservation"),
              (("booking_id", "reservation_id", "reservation_number", "id"),
               ("start_date", "check_in", "check_in_date", "check_out_date", "event_id", "room_type")),
              ("Reservation", "FoodEstablishmentReservation", "LodgingReservation")),
        _role("membership", ("memberships", "membership", "members", "member"),
              (("membership_id", "member_id", "id"),
               ("person_id", "member_name", "membership_type", "start_date", "renewal_date", "status")),
              ("ProgramMembership",)),
        _role("appointment", ("appointments", "appointment", "appointment_requests"),
              (("appointment_id", "id"),
               ("appointment_date", "appointment_time", "service_requested", "status")),
              ("Schedule",)),
        _role("education_provider", ("schools", "school", "colleges", "universities", "education_providers"),
              (("provider_id", "school_id", "id"), ("name", "address")),
              ("EducationalOrganization", "School", "CollegeOrUniversity", "NGO")),
        _role("lodging_provider", ("hotels", "hotel", "lodging_providers"),
              (("hotel_id", "provider_id", "id"), ("name", "address")),
              ("LodgingBusiness", "Hotel")),
    ), ("Event", "EducationEvent", "Course", "CourseInstance", "Schedule", "Reservation",
        "EventReservation", "FoodEstablishmentReservation", "LodgingReservation", "ProgramMembership",
        "EducationalOrganization", "School", "CollegeOrUniversity", "NGO", "LodgingBusiness", "Hotel")),
    DomainProfile("lead_crm", (
        _role("lead", ("leads", "lead", "prospects", "prospect"),
              (("lead_id", "prospect_id", "id"),
               ("contact_name", "name", "work_email", "email", "company", "lead_status")),
              ("Person", "Organization", "ContactPoint")),
        _role("inquiry", ("inquiries", "inquiry", "enquiries", "contact_requests"),
              (("inquiry_id", "request_id", "id"), ("message", "subject", "service_id")),
              ("Service",)),
        _role("campaign", ("campaigns", "campaign"),
              (("campaign_id", "id"), ("name", "channel")), ("Offer",)),
        _role("service_request", ("service_requests", "service_request", "support_requests", "tickets"),
              (("request_id", "ticket_id", "id"), ("service_id", "status")), ("Service",)),
        _role("lead_score", ("lead_scores", "lead_score", "qualification_scores"),
              (("lead_id", "prospect_id"),
               ("overall_score", "qualification_score", "score_category")), ()),
        _role("follow_up", ("follow_ups", "follow_up", "sales_follow_ups"),
              (("follow_up_id", "id"),
               ("lead_id", "follow_up_date", "next_action", "outcome")), ()),
    ), ("Person", "Organization", "ContactPoint", "Service", "Offer", "PostalAddress")),
    DomainProfile("signature_approval", (
        _role("document", ("documents", "document", "agreements", "contracts"),
              (("document_id", "agreement_id", "id"),
               ("agreement_title", "document_title", "document_version", "title", "name", "version")),
              ("DigitalDocument",)),
        _role("signature_request", ("signature_requests", "signature_request", "signing_requests"),
              (("request_id", "signature_request_id", "id"),
               ("document_id", "signature", "acknowledged_at", "status")), ()),
        _role("signer", ("signers", "signer", "signatories", "signatory"),
              (("signer_id", "id"),
               ("request_id", "reference_request_id", "document_id"),
               ("signer_name", "reference_name", "name", "signer_email", "email", "signed_at")), ("Person",)),
        _role("approval_step", ("approval_steps", "approval_step", "approvals", "approval"),
              (("step_id", "approval_step_id", "approval_id", "id"),
               ("document_id", "request_id", "approver_name"),
               ("approval_status", "status", "approval_sequence", "sequence")),
              ("DigitalDocumentPermission",)),
        _role("consent_record", ("consent_records", "consent_record", "authorizations", "consents"),
              (("consent_id", "authorization_id", "id"),
               ("person_id", "patient_id", "participant_id", "participant_name"),
               ("authorization_granted", "signed_at", "signature", "consent_status", "status")),
              ("AuthorizeAction",)),
    ), ("DigitalDocument", "DigitalDocumentPermission", "AuthorizeAction", "Person", "Organization")),
    DomainProfile("healthcare_intake", (
        _role("patient", ("patients", "patient"),
              (("patient_id", "medical_record_number", "mrn", "id"), ("name", "date_of_birth", "dob")),
              ("Patient",)),
        _role("provider", ("providers", "provider", "physicians", "doctors", "clinicians"),
              (("provider_id", "npi", "id"), ("name", "specialty")), ("Physician",)),
        _role("provider_organization", ("practices", "clinics", "hospitals", "medical_organizations"),
              (("organization_id", "practice_id", "clinic_id", "hospital_id", "id"), ("name", "address")),
              ("MedicalOrganization", "MedicalClinic", "Hospital")),
        _role("patient_intake", ("patient_intakes", "patient_intake", "intake_forms", "intake_submissions"),
              (("intake_id", "submission_id", "timestamp", "submitted_at", "id"),
               ("patient_id", "patient_name", "date_of_birth"),
               ("primary_health_concern", "current_health_concerns", "medical_history",
                "intake_status", "status")), ()),
        _role("assessment", ("assessments", "assessment", "assessment_responses", "questionnaires"),
              (("assessment_id", "response_id", "id"),
               ("patient_id", "form_id", "questionnaire_name"),
               ("score", "overall_score", "severity_category", "submitted_at")), ()),
        _role("condition", ("conditions", "condition", "diagnoses", "diagnosis"),
              (("condition_id", "diagnosis_id", "id"), ("patient_id",), ("icd_code", "code")),
              ("MedicalCondition",)),
        _role("medication", ("medications", "medication", "drugs", "prescriptions"),
              (("medication_id", "drug_id", "id"), ("patient_id",),
               ("medication_name", "name", "dose", "frequency")), ("Drug",)),
        _role("procedure", ("procedures", "procedure", "medical_procedures"),
              (("procedure_id", "id"), ("patient_id",), ("code", "performed_at")), ("MedicalProcedure",)),
        _role("medical_test", ("medical_tests", "medical_test", "lab_tests", "test_results"),
              (("test_id", "result_id", "id"), ("patient_id",), ("name", "value")), ("MedicalTest",)),
    ), ("Patient", "MedicalOrganization", "MedicalClinic", "Physician", "Hospital", "MedicalCondition",
        "Drug", "MedicalProcedure", "MedicalTest", "DigitalDocument", "AuthorizeAction")),
    DomainProfile("safety_compliance", (
        _role("employee", ("employees", "employee", "workers", "worker"),
              (("employee_id", "worker_id", "id"), ("name", "site_id")), ("Person",)),
        _role("site", ("sites", "site", "worksites", "facilities"),
              (("site_id", "facility_id", "id"), ("name", "address")), ("Place",)),
        _role("equipment", ("equipment", "assets", "asset"),
              (("equipment_id", "asset_id", "id"), ("name", "serial_number")), ("Product",)),
        _role("vehicle", ("vehicles", "vehicle", "fleet"),
              (("vehicle_id", "id"), ("registration_number", "vin")), ("Vehicle",)),
        _role("inspection", ("inspections", "inspection", "safety_inspections"),
              (("inspection_id", "id"), ("site_id", "equipment_id"), ("inspected_at", "status")), ()),
        _role("incident", ("incidents", "incident", "accidents", "near_misses"),
              (("incident_id", "near_miss_id", "report_id", "id"),
               ("site_id", "work_site", "employee_id", "reported_by"),
               ("occurred_at", "incident_date", "near_miss_date", "severity", "potential_severity")), ()),
        _role("finding", ("findings", "finding", "observations", "violations"),
              (("finding_id", "id"), ("inspection_id", "incident_id"),
               ("risk_severity", "severity", "finding_status", "status")), ()),
        _role("corrective_action", ("corrective_actions", "corrective_action", "remediations"),
              (("action_id", "corrective_action_id", "id"),
               ("finding_id", "incident_id", "action_owner"),
               ("due_date", "completion_date", "action_status", "status")), ()),
        _role("report", ("reports", "report", "safety_reports"),
              (("report_id", "id"), ("incident_id", "inspection_id"), ("title", "created_at")),
              ("Report", "DigitalDocument")),
    ), ("Report", "Vehicle", "Person", "Organization", "Place", "Product", "DigitalDocument")),
)


PROFILES: Mapping[str, DomainProfile] = MappingProxyType({profile.name: profile for profile in _PROFILES})


def profiles() -> tuple[DomainProfile, ...]:
    return tuple(PROFILES[name] for name in sorted(PROFILES))


def _record(profile: DomainProfile) -> dict:
    return {
        "name": profile.name,
        "schema_org_classes": profile.schema_org_classes,
        "roles": [role.__dict__ for role in profile.roles],
    }


DOMAIN_PROFILE_VERSION = "sha256:" + hashlib.sha256(json.dumps(
    [_record(PROFILES[name]) for name in sorted(PROFILES)], sort_keys=True,
    separators=(",", ":"), ensure_ascii=True,
).encode("utf-8")).hexdigest()
