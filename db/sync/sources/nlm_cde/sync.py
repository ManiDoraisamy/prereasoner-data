"""Synchronize public CDE and form documents from the NIH/NLM CDE Repository API."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from db.sync._conn import connect
from db.sync.sources.common import (
    SourceRelease, USER_AGENT, insert_rows, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

API_ROOT = "https://cde.nlm.nih.gov/api"
LICENSE_NAME = "Per-document rights and repository terms"
LICENSE_URL = "https://cde.nlm.nih.gov/about"


@dataclass(frozen=True)
class NlmData:
    cdes: tuple[tuple, ...]
    cde_designations: tuple[tuple, ...]
    cde_values: tuple[tuple, ...]
    forms: tuple[tuple, ...]
    form_elements: tuple[tuple, ...]

    def counts(self) -> dict[str, int]:
        return {
            "cde": len(self.cdes), "cde_designation": len(self.cde_designations),
            "cde_permissible_value": len(self.cde_values), "form": len(self.forms),
            "form_element": len(self.form_elements),
        }


def _search(kind: str, page: int, per_page: int = 100, **filters) -> dict:
    last_error = None
    for attempt in range(6):
        try:
            response = requests.post(
                f"{API_ROOT}/{kind}/search", timeout=120,
                headers={"User-Agent": USER_AGENT},
                json={"page": page, "resultPerPage": per_page, "searchTerm": "", **filters},
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == 5:
                break
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"NLM {kind} search page {page} failed after retries") from last_error


def _documents_for_query(kind: str, *, workers: int, **filters) -> tuple[list[dict], int]:
    first = _search(kind, 1, **filters)
    total = int(first["resultsTotal"])
    if total > 10_000:
        raise ValueError(f"NLM {kind} query exceeds the API's 10,000-result window: {filters!r}")
    pages = math.ceil(total / 100)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rest = list(pool.map(
            lambda page: _search(kind, page, **filters), range(2, pages + 1)
        ))
    documents = list(first["docs"])
    for response in rest:
        documents.extend(response["docs"])
    if len(documents) > total:
        raise ValueError(f"NLM {kind} pagination returned more documents than advertised")
    return documents, total


def fetch_snapshot(*, workers: int = 4) -> bytes:
    first = _search("de", 1, per_page=1)
    cde_total = int(first["resultsTotal"])
    datatype_partitions = [
        {"selectedDatatypes": [datatype]}
        for datatype in (
            "Date", "Dynamic Code List", "Externally Defined", "File", "Geo Location",
            "Number", "Text", "Time",
        )
    ]
    datatype_partitions.extend([
        {"selectedDatatypes": ["Value List"], "selectedStatuses": ["Standard"]},
        {
            "selectedDatatypes": ["Value List"], "selectedStatuses": ["Qualified"],
            "selectedOrg": "NINDS",
        },
        {
            "selectedDatatypes": ["Value List"], "selectedStatuses": ["Qualified"],
            "excludeOrgs": ["NINDS"],
        },
    ])
    by_id: dict[str, dict] = {}
    partition_counts = []
    for filters in datatype_partitions:
        documents, count = _documents_for_query("de", workers=workers, **filters)
        partition_counts.append({"filters": filters, "count": count})
        for document in documents:
            by_id[document["tinyId"]] = document
    cdes = list(by_id.values())
    if cde_total - len(cdes) > 100:
        raise ValueError(
            f"NLM CDE typed partitions returned only {len(cdes)} public documents of "
            f"{cde_total} advertised"
        )
    forms, form_total = _documents_for_query("form", workers=workers)
    payload = {
        "api_root": API_ROOT, "cde_advertised_total": cde_total,
        "cde_publicly_retrieved_total": len(cdes), "form_advertised_total": form_total,
        "form_publicly_retrieved_total": len(forms),
        "cde_partitions": partition_counts,
        "cdes": sorted(cdes, key=lambda row: (row.get("tinyId", ""), row.get("version", ""))),
        "forms": sorted(forms, key=lambda row: (row.get("tinyId", ""), row.get("version", ""))),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _preferred(items: list[dict], field: str) -> str:
    if not items:
        return ""
    for item in items:
        tags = {str(tag).lower() for tag in item.get("tags", [])}
        if any("preferred" in tag for tag in tags):
            return str(item.get(field, ""))
    return str(items[0].get(field, ""))


def _status(document: dict, key: str) -> str:
    return str((document.get("registrationState") or {}).get(key, ""))


def parse_snapshot(snapshot: bytes, *, enforce_minimums: bool = True) -> NlmData:
    payload = json.loads(snapshot)
    cdes = []
    designations = []
    values = []
    for document in payload["cdes"]:
        tiny_id = str(document.get("tinyId", ""))
        version = str(document.get("version", ""))
        value_domain = document.get("valueDomain") or {}
        cdes.append((
            tiny_id, version, str((document.get("stewardOrg") or {}).get("name", "")),
            _status(document, "registrationStatus"), _status(document, "administrativeStatus"),
            bool(document.get("nihEndorsed", False)), bool(document.get("archived", False)),
            _preferred(document.get("designations") or [], "designation"),
            _preferred(document.get("definitions") or [], "definition"),
            str(value_domain.get("datatype", "")),
            json.dumps(document, sort_keys=True, ensure_ascii=True),
        ))
        for source_order, designation in enumerate(document.get("designations") or []):
            designations.append((
                tiny_id, source_order, str(designation.get("designation", "")),
                json.dumps(designation.get("tags") or [], sort_keys=True),
                json.dumps(designation.get("sources") or [], sort_keys=True),
            ))
        for source_order, value in enumerate(value_domain.get("permissibleValues") or []):
            values.append((
                tiny_id, source_order, str(value.get("value", "")),
                str(value.get("meaning", value.get("valueMeaningName", ""))),
                json.dumps(value, sort_keys=True, ensure_ascii=True),
            ))

    forms = []
    form_elements = []
    for document in payload["forms"]:
        tiny_id = str(document.get("tinyId", ""))
        forms.append((
            tiny_id, str(document.get("version", "")),
            str((document.get("stewardOrg") or {}).get("name", "")),
            _status(document, "registrationStatus"), _status(document, "administrativeStatus"),
            bool(document.get("nihEndorsed", False)), bool(document.get("archived", False)),
            bool(document.get("isCopyrighted", False)), bool(document.get("noRenderAllowed", False)),
            _preferred(document.get("designations") or [], "designation"),
            json.dumps(document, sort_keys=True, ensure_ascii=True),
        ))

        def visit(elements: list[dict], parent_path: str = "") -> None:
            for index, element in enumerate(elements):
                path = f"{parent_path}.{index}" if parent_path else str(index)
                question = element.get("question") or {}
                cde = question.get("cde") or {}
                form_elements.append((
                    tiny_id, path, parent_path, str(element.get("elementType", "")),
                    str(element.get("label", "")), str(cde.get("tinyId", "")),
                    str(cde.get("version", "")), str(question.get("datatype", "")),
                    bool(question.get("required", False)),
                    json.dumps(element, sort_keys=True, ensure_ascii=True),
                ))
                visit(element.get("formElements") or [], path)

        visit(document.get("formElements") or [])

    result = NlmData(
        tuple(cdes), tuple(designations), tuple(values), tuple(forms), tuple(form_elements)
    )
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: NlmData, *, enforce_minimums: bool = True) -> None:
    cde_ids = [row[0] for row in data.cdes]
    form_ids = [row[0] for row in data.forms]
    if not all(cde_ids) or len(cde_ids) != len(set(cde_ids)):
        raise ValueError("NLM CDE tinyIds must be present and unique")
    if not all(form_ids) or len(form_ids) != len(set(form_ids)):
        raise ValueError("NLM form tinyIds must be present and unique")
    if enforce_minimums and (len(data.cdes) < 20_000 or len(data.forms) < 1_500):
        raise ValueError(f"NLM CDE snapshot is unexpectedly small: {data.counts()}")


DDL = """
CREATE TABLE IF NOT EXISTS nlm_cde.cde (
  release_id text NOT NULL REFERENCES nlm_cde.release(release_id),
  tiny_id text NOT NULL,
  version text NOT NULL,
  steward_organization text NOT NULL,
  registration_status text NOT NULL,
  administrative_status text NOT NULL,
  nih_endorsed boolean NOT NULL,
  archived boolean NOT NULL,
  preferred_name text NOT NULL,
  preferred_definition text NOT NULL,
  datatype text NOT NULL,
  source_document jsonb NOT NULL,
  PRIMARY KEY (release_id, tiny_id)
);
CREATE TABLE IF NOT EXISTS nlm_cde.cde_designation (
  release_id text NOT NULL,
  tiny_id text NOT NULL,
  source_order integer NOT NULL,
  designation text NOT NULL,
  tags jsonb NOT NULL,
  sources jsonb NOT NULL,
  PRIMARY KEY (release_id, tiny_id, source_order),
  FOREIGN KEY (release_id, tiny_id) REFERENCES nlm_cde.cde(release_id, tiny_id)
);
CREATE TABLE IF NOT EXISTS nlm_cde.cde_permissible_value (
  release_id text NOT NULL,
  tiny_id text NOT NULL,
  source_order integer NOT NULL,
  value text NOT NULL,
  meaning text NOT NULL,
  source_value jsonb NOT NULL,
  PRIMARY KEY (release_id, tiny_id, source_order),
  FOREIGN KEY (release_id, tiny_id) REFERENCES nlm_cde.cde(release_id, tiny_id)
);
CREATE TABLE IF NOT EXISTS nlm_cde.form (
  release_id text NOT NULL REFERENCES nlm_cde.release(release_id),
  tiny_id text NOT NULL,
  version text NOT NULL,
  steward_organization text NOT NULL,
  registration_status text NOT NULL,
  administrative_status text NOT NULL,
  nih_endorsed boolean NOT NULL,
  archived boolean NOT NULL,
  is_copyrighted boolean NOT NULL,
  no_render_allowed boolean NOT NULL,
  preferred_name text NOT NULL,
  source_document jsonb NOT NULL,
  PRIMARY KEY (release_id, tiny_id)
);
CREATE TABLE IF NOT EXISTS nlm_cde.form_element (
  release_id text NOT NULL,
  form_tiny_id text NOT NULL,
  element_path text NOT NULL,
  parent_path text NOT NULL,
  element_type text NOT NULL,
  label text NOT NULL,
  cde_tiny_id text NOT NULL,
  cde_version text NOT NULL,
  datatype text NOT NULL,
  required boolean NOT NULL,
  source_element jsonb NOT NULL,
  PRIMARY KEY (release_id, form_tiny_id, element_path),
  FOREIGN KEY (release_id, form_tiny_id) REFERENCES nlm_cde.form(release_id, tiny_id)
);
"""


def synchronize(conn, version: str, snapshot: bytes, data: NlmData) -> str:
    release = SourceRelease(
        schema="nlm_cde", version=version, source_url=API_ROOT,
        content_sha256=sha256_bytes(snapshot), completeness="bounded_snapshot",
        import_scope={
            "cdes": "all anonymously retrievable documents from audited typed search partitions",
            "forms": "all documents returned by blank public search",
            "count_semantics": "API may advertise access-restricted rows absent from page payloads",
            "rights": "rights remain per document; no_render_allowed is preserved",
        }, license_name=LICENSE_NAME, license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "nlm_cde", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows(cur, "nlm_cde.cde", (
            "release_id", "tiny_id", "version", "steward_organization", "registration_status",
            "administrative_status", "nih_endorsed", "archived", "preferred_name",
            "preferred_definition", "datatype", "source_document",
        ), ((release.release_id, *row) for row in data.cdes))
        insert_rows(cur, "nlm_cde.cde_designation", (
            "release_id", "tiny_id", "source_order", "designation", "tags", "sources",
        ), ((release.release_id, *row) for row in data.cde_designations))
        insert_rows(cur, "nlm_cde.cde_permissible_value", (
            "release_id", "tiny_id", "source_order", "value", "meaning", "source_value",
        ), ((release.release_id, *row) for row in data.cde_values))
        insert_rows(cur, "nlm_cde.form", (
            "release_id", "tiny_id", "version", "steward_organization", "registration_status",
            "administrative_status", "nih_endorsed", "archived", "is_copyrighted",
            "no_render_allowed", "preferred_name", "source_document",
        ), ((release.release_id, *row) for row in data.forms))
        insert_rows(cur, "nlm_cde.form_element", (
            "release_id", "form_tiny_id", "element_path", "parent_path", "element_type",
            "label", "cde_tiny_id", "cde_version", "datatype", "required", "source_element",
        ), ((release.release_id, *row) for row in data.form_elements))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot")
    parser.add_argument("--write-snapshot")
    parser.add_argument("--version", default=dt.date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    snapshot = open(args.snapshot, "rb").read() if args.snapshot else fetch_snapshot()
    if args.write_snapshot:
        with open(args.write_snapshot, "wb") as target:
            target.write(snapshot)
    data = parse_snapshot(snapshot)
    print(f"validated NLM CDE {args.version}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(snapshot)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, args.version, snapshot, data)
    finally:
        conn.close()
    print(f"active NLM CDE release: {release_id}")


if __name__ == "__main__":
    main()
