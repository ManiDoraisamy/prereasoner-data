"""Synchronize a dated EU VAT-rate snapshot from the Commission TEDB SOAP service."""
from __future__ import annotations

import argparse
import datetime as dt
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

import requests

from db.sync._conn import connect
from db.sync.migrations import latest_schema_version
from db.sync.sources.common import (
    SourceRelease, USER_AGENT, insert_rows, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

SOURCE_URL = "https://ec.europa.eu/taxation_customs/tedb/ws/"
WSDL_URL = SOURCE_URL + "VatRetrievalService.wsdl"
LICENSE_NAME = "European Commission reuse policy; TEDB information is non-binding"
LICENSE_URL = "https://commission.europa.eu/legal-notice_en"
MEMBER_STATES = (
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK", "XI",
)
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
MESSAGE_NS = "urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService"
TYPE_NS = MESSAGE_NS + ":types"


@dataclass(frozen=True)
class TedbData:
    country_metadata: tuple[tuple[str, bool, bool], ...]
    rates: tuple[tuple, ...]
    cn_codes: tuple[tuple, ...]
    cpa_codes: tuple[tuple, ...]

    def response_status(self, requested_member_states=MEMBER_STATES) -> tuple[tuple, ...]:
        requested = frozenset(requested_member_states)
        metadata = {row[0]: row[1:] for row in self.country_metadata}
        rate_counts = Counter(row[1] for row in self.rates)
        member_states = sorted(requested | set(metadata) | set(rate_counts))
        return tuple((
            member_state, member_state in requested, rate_counts[member_state],
            metadata.get(member_state, (None, None))[0],
            metadata.get(member_state, (None, None))[1],
            member_state in metadata,
        ) for member_state in member_states)

    def counts(self, requested_member_states=MEMBER_STATES) -> dict[str, int]:
        return {
            "response_status": len(self.response_status(requested_member_states)),
            "vat_rate": len(self.rates),
            "vat_rate_cn_code": len(self.cn_codes), "vat_rate_cpa_code": len(self.cpa_codes),
        }


def _request_xml(situation_on: str, member_states: tuple[str, ...]) -> bytes:
    codes = "".join(f"<t:isoCode>{code}</t:isoCode>" for code in member_states)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:req="{MESSAGE_NS}" xmlns:t="{TYPE_NS}">
  <soapenv:Header/><soapenv:Body><req:retrieveVatRatesReqMsg>
    <t:memberStates>{codes}</t:memberStates>
    <t:situationOn>{situation_on}</t:situationOn>
  </req:retrieveVatRatesReqMsg></soapenv:Body>
</soapenv:Envelope>'''.encode()


def fetch_snapshot(situation_on: str, member_states: tuple[str, ...] = MEMBER_STATES) -> bytes:
    response = requests.post(
        SOURCE_URL, data=_request_xml(situation_on, member_states), timeout=180,
        headers={
            "User-Agent": USER_AGENT, "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": (
                "urn:ec.europa.eu:taxud:tedb:services:v1:"
                "VatRetrievalService/RetrieveVatRates"
            ),
        },
    )
    response.raise_for_status()
    return response.content


def _text(parent: ET.Element, name: str) -> str:
    node = parent.find(f"{{{TYPE_NS}}}{name}")
    return "" if node is None else (node.text or "").strip()


def parse_snapshot(snapshot: bytes, *, enforce_minimums: bool = True) -> TedbData:
    # TEDB currently emits a few legacy bytes that contradict its UTF-8 response header.
    root = ET.fromstring(snapshot.decode("utf-8", errors="replace"))
    fault = root.find(f".//{{{SOAP_NS}}}Fault")
    if fault is not None:
        raise ValueError("TEDB SOAP fault: " + " ".join(fault.itertext()).strip())
    response = root.find(f".//{{{MESSAGE_NS}}}retrieveVatRatesRespMsg")
    if response is None:
        raise ValueError("TEDB response message is missing")
    country_metadata = []
    for country in response.findall(f".//{{{TYPE_NS}}}additionalInformation/"
                                    f"{{{TYPE_NS}}}countries/{{{TYPE_NS}}}country"):
        country_metadata.append((
            _text(country, "isoCode"), _text(country, "cnCodeProvided") == "true",
            _text(country, "cpaCodeProvided") == "true",
        ))
    rates = []
    cn_codes = []
    cpa_codes = []
    for source_order, row in enumerate(response.findall(f"{{{TYPE_NS}}}vatRateResults")):
        rate_node = row.find(f"{{{TYPE_NS}}}rate")
        category = row.find(f"{{{TYPE_NS}}}category")
        value_text = "" if rate_node is None else _text(rate_node, "value")
        rates.append((
            source_order, _text(row, "memberState"), _text(row, "type"),
            "" if rate_node is None else _text(rate_node, "type"),
            None if not value_text else Decimal(value_text), _text(row, "situationOn")[:10],
            "" if category is None else _text(category, "identifier"),
            "" if category is None else _text(category, "description"), _text(row, "comment"),
        ))
        for kind, target in (("cnCodes", cn_codes), ("cpaCodes", cpa_codes)):
            container = row.find(f"{{{TYPE_NS}}}{kind}")
            if container is None:
                continue
            for code in container.findall(f"{{{TYPE_NS}}}code"):
                target.append((source_order, _text(code, "value"), _text(code, "description")))
    result = TedbData(tuple(country_metadata), tuple(rates), tuple(cn_codes), tuple(cpa_codes))
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: TedbData, *, enforce_minimums: bool = True) -> None:
    rate_ids = [row[0] for row in data.rates]
    if len(rate_ids) != len(set(rate_ids)):
        raise ValueError("TEDB VAT source-order ids are not unique")
    metadata_states = [row[0] for row in data.country_metadata]
    if not all(metadata_states) or len(metadata_states) != len(set(metadata_states)):
        raise ValueError("TEDB country metadata keys must be present and unique")
    if any(not row[1] for row in data.rates):
        raise ValueError("TEDB VAT rows require a member state")
    if any(row[4] is not None and row[4] < 0 for row in data.rates):
        raise ValueError("TEDB VAT rate values cannot be negative")
    if enforce_minimums and (len(data.rates) < 100 or len({row[1] for row in data.rates}) < 20):
        raise ValueError(f"TEDB VAT snapshot is unexpectedly small: {data.counts()}")


DDL = """
CREATE TABLE IF NOT EXISTS ec_tedb.response_status (
  release_id text NOT NULL REFERENCES ec_tedb.release(release_id),
  member_state text NOT NULL,
  requested boolean NOT NULL,
  returned_rate_count integer NOT NULL CHECK (returned_rate_count >= 0),
  cn_code_provided boolean,
  cpa_code_provided boolean,
  metadata_present boolean NOT NULL,
  PRIMARY KEY (release_id, member_state)
);
CREATE TABLE IF NOT EXISTS ec_tedb.vat_rate (
  release_id text NOT NULL REFERENCES ec_tedb.release(release_id),
  source_order integer NOT NULL,
  member_state text NOT NULL,
  rate_class text NOT NULL,
  rate_type text NOT NULL,
  rate_percent numeric,
  effective_date date NOT NULL,
  category_id text NOT NULL,
  category_description text NOT NULL,
  comment text NOT NULL,
  PRIMARY KEY (release_id, source_order)
);
CREATE TABLE IF NOT EXISTS ec_tedb.vat_rate_cn_code (
  release_id text NOT NULL,
  source_order integer NOT NULL,
  code text NOT NULL,
  description text NOT NULL,
  PRIMARY KEY (release_id, source_order, code),
  FOREIGN KEY (release_id, source_order) REFERENCES ec_tedb.vat_rate(release_id, source_order)
);
CREATE TABLE IF NOT EXISTS ec_tedb.vat_rate_cpa_code (
  release_id text NOT NULL,
  source_order integer NOT NULL,
  code text NOT NULL,
  description text NOT NULL,
  PRIMARY KEY (release_id, source_order, code),
  FOREIGN KEY (release_id, source_order) REFERENCES ec_tedb.vat_rate(release_id, source_order)
);
"""


def synchronize(conn, version: str, snapshot: bytes, data: TedbData,
                situation_on: str) -> str:
    release = SourceRelease(
        schema="ec_tedb", version=version, source_url=WSDL_URL,
        content_sha256=sha256_bytes(snapshot), completeness="bounded_snapshot",
        import_scope={
            "member_states": list(MEMBER_STATES), "requested_situation_on": situation_on,
            "coverage": "VAT rates returned by TEDB for the requested date",
            "legal_status": "non-binding; national law remains authoritative",
        }, license_name=LICENSE_NAME, license_url=LICENSE_URL,
        schema_version=latest_schema_version("ec_tedb"),
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "ec_tedb", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows(cur, "ec_tedb.response_status", (
            "release_id", "member_state", "requested", "returned_rate_count",
            "cn_code_provided", "cpa_code_provided", "metadata_present",
        ), ((release.release_id, *row) for row in data.response_status(MEMBER_STATES)))
        insert_rows(cur, "ec_tedb.vat_rate", (
            "release_id", "source_order", "member_state", "rate_class", "rate_type",
            "rate_percent", "effective_date", "category_id", "category_description", "comment",
        ), ((release.release_id, *row) for row in data.rates))
        insert_rows(cur, "ec_tedb.vat_rate_cn_code",
                    ("release_id", "source_order", "code", "description"),
                    ((release.release_id, *row) for row in data.cn_codes))
        insert_rows(cur, "ec_tedb.vat_rate_cpa_code",
                    ("release_id", "source_order", "code", "description"),
                    ((release.release_id, *row) for row in data.cpa_codes))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--situation-on", default=dt.date.today().isoformat())
    parser.add_argument("--snapshot")
    parser.add_argument("--write-snapshot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    snapshot = (open(args.snapshot, "rb").read() if args.snapshot
                else fetch_snapshot(args.situation_on))
    if args.write_snapshot:
        with open(args.write_snapshot, "wb") as target:
            target.write(snapshot)
    data = parse_snapshot(snapshot)
    print(f"validated EC TEDB {args.situation_on}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(snapshot)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, args.situation_on, snapshot, data, args.situation_on)
    finally:
        conn.close()
    print(f"active EC TEDB release: {release_id}")


if __name__ == "__main__":
    main()
