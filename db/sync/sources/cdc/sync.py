"""Synchronize the effective CDC/NCHS ICD-10-CM tabular hierarchy."""
from __future__ import annotations

import argparse
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

from db.sync._conn import connect
from db.sync.sources.common import (
    SourceRelease, download_http, insert_rows, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

DEFAULT_VERSION = "2026-04-01"
SOURCE_URL = (
    "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/"
    "2026-update/icd10cm-April-1-2026-XML.zip"
)
LICENSE_NAME = "CDC website policies; ICD classification rights retained by WHO"
LICENSE_URL = "https://www.cdc.gov/other/agencymaterials.html"


@dataclass(frozen=True)
class CdcData:
    codes: tuple[tuple[str, str, str, int, int, bool, str], ...]

    def counts(self) -> dict[str, int]:
        return {"icd10cm_code": len(self.codes)}


def _xml_value(node: ET.Element):
    children = list(node)
    if not children:
        return (node.text or "").strip()
    result: dict[str, object] = {}
    for child in children:
        if child.tag == "diag":
            continue
        value = _xml_value(child)
        existing = result.get(child.tag)
        if existing is None:
            result[child.tag] = value
        elif isinstance(existing, list):
            existing.append(value)
        else:
            result[child.tag] = [existing, value]
    if node.attrib:
        result["_attributes"] = dict(sorted(node.attrib.items()))
    return result


def parse_archive(archive: bytes, *, enforce_minimums: bool = True) -> CdcData:
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        names = [name for name in source.namelist() if "tabular" in name.lower() and name.endswith(".xml")]
        if len(names) != 1:
            raise ValueError(f"CDC archive must contain one tabular XML file, found {names!r}")
        root = ET.fromstring(source.read(names[0]))
    rows = []
    source_order = 0

    def visit(node: ET.Element, parent_code: str, depth: int) -> None:
        nonlocal source_order
        name = node.findtext("name", default="").strip()
        description = node.findtext("desc", default="").strip()
        if not name or not description:
            raise ValueError("CDC ICD-10-CM diag is missing name or description")
        source_order += 1
        child_diags = node.findall("diag")
        metadata = {
            child.tag: _xml_value(child)
            for child in node
            if child.tag not in {"name", "desc", "diag"}
        }
        rows.append((
            name, description, parent_code, depth, source_order, not child_diags,
            json.dumps(metadata, sort_keys=True, ensure_ascii=True),
        ))
        for child in child_diags:
            visit(child, name, depth + 1)

    for chapter in root.findall("chapter"):
        for section in chapter.findall("section"):
            for diag in section.findall("diag"):
                visit(diag, "", 0)
    result = CdcData(tuple(rows))
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: CdcData, *, enforce_minimums: bool = True) -> None:
    codes = [row[0] for row in data.codes]
    if len(codes) != len(set(codes)):
        raise ValueError("CDC ICD-10-CM codes are not unique")
    code_set = set(codes)
    if any(parent and parent not in code_set for _, _, parent, *_ in data.codes):
        raise ValueError("CDC ICD-10-CM hierarchy contains an unknown parent")
    if enforce_minimums and len(data.codes) < 45_000:
        raise ValueError(f"CDC ICD-10-CM hierarchy is unexpectedly small: {len(data.codes)}")


DDL = """
CREATE TABLE IF NOT EXISTS cdc.icd10cm_code (
  release_id text NOT NULL REFERENCES cdc.release(release_id),
  code text NOT NULL,
  description text NOT NULL,
  parent_code text,
  depth integer NOT NULL CHECK (depth >= 0),
  source_order integer NOT NULL,
  is_leaf boolean NOT NULL,
  metadata jsonb NOT NULL,
  effective_from date NOT NULL,
  effective_to date,
  PRIMARY KEY (release_id, code),
  FOREIGN KEY (release_id, parent_code) REFERENCES cdc.icd10cm_code(release_id, code)
);
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='cdc' AND table_name='icd10cm_code' AND column_name='is_billable'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='cdc' AND table_name='icd10cm_code' AND column_name='is_leaf'
  ) THEN
    ALTER TABLE cdc.icd10cm_code RENAME COLUMN is_billable TO is_leaf;
  END IF;
END $$;
"""


def synchronize(conn, version: str, archive: bytes, data: CdcData,
                effective_from: str, effective_to: str | None) -> str:
    release = SourceRelease(
        schema="cdc", version=version, source_url=SOURCE_URL,
        content_sha256=sha256_bytes(archive), completeness="full_declared_scope",
        import_scope={
            "classification": "ICD-10-CM", "artifact_member": "tabular XML hierarchy",
            "effective_from": effective_from, "effective_to": effective_to,
            "excluded_members": ["alphabetic index", "drug index", "neoplasm table", "external causes index"],
        },
        license_name=LICENSE_NAME, license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "cdc", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows(cur, "cdc.icd10cm_code", (
            "release_id", "code", "description", "parent_code", "depth", "source_order",
            "is_leaf", "metadata", "effective_from", "effective_to",
        ), ((release.release_id, code, description, parent or None, depth, order, leaf,
             metadata, effective_from, effective_to)
            for code, description, parent, depth, order, leaf, metadata in data.codes))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive")
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--effective-from", default="2026-04-01")
    parser.add_argument("--effective-to", default="2026-09-30")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    archive = open(args.archive, "rb").read() if args.archive else download_http(SOURCE_URL)
    data = parse_archive(archive)
    print(f"validated CDC ICD-10-CM {args.version}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(archive)}")
        return
    conn = connect()
    try:
        release_id = synchronize(
            conn, args.version, archive, data, args.effective_from, args.effective_to or None
        )
    finally:
        conn.close()
    print(f"active CDC release: {release_id}")


if __name__ == "__main__":
    main()
