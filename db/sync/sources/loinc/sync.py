"""Import a user-downloaded, licensed LOINC Complete release archive.

The synchronizer intentionally requires ``--archive``. LOINC download authentication and
license acceptance happen on loinc.org; this command never scrapes credentials or creates an
empty schema when the licensed archive is absent.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from dataclasses import dataclass

from db.sync._conn import connect
from db.sync.sources.common import (
    SourceRelease, insert_rows_batched, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

DEFAULT_VERSION = "2.82"
SOURCE_URL = "https://loinc.org/downloads/"
LICENSE_NAME = "LOINC License"
LICENSE_URL = "https://loinc.org/license/"


@dataclass(frozen=True)
class LoincData:
    terms: tuple[tuple, ...]
    answers: tuple[tuple, ...]
    answer_links: tuple[tuple, ...]
    panels: tuple[tuple, ...]
    parts: tuple[tuple, ...]
    part_links: tuple[tuple, ...]

    def counts(self) -> dict[str, int]:
        return {
            "term": len(self.terms), "answer": len(self.answers),
            "answer_list_link": len(self.answer_links), "panel_form": len(self.panels),
            "part": len(self.parts), "part_link": len(self.part_links),
        }


def _find(source: zipfile.ZipFile, suffix: str, *, required: bool = True) -> str | None:
    names = [name for name in source.namelist() if name.replace("\\", "/").endswith(suffix)]
    if len(names) == 1:
        return names[0]
    if not names and not required:
        return None
    raise ValueError(f"LOINC archive expected one {suffix}, found {names!r}")


def _rows(source: zipfile.ZipFile, name: str | None):
    if name is None:
        return []
    with source.open(name) as member:
        return list(csv.DictReader(io.TextIOWrapper(member, encoding="utf-8-sig", newline="")))


def _first(row: dict, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None:
            return value
    return ""


def _raw(row: dict) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=True)


def parse_archive(archive: bytes, *, enforce_minimums: bool = True) -> LoincData:
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        term_rows = _rows(source, _find(source, "/LoincTable/Loinc.csv"))
        answer_rows = _rows(source, _find(source, "/AccessoryFiles/AnswerFile/AnswerList.csv"))
        answer_link_rows = _rows(
            source, _find(source, "/AccessoryFiles/AnswerFile/LoincAnswerListLink.csv")
        )
        panel_rows = _rows(
            source, _find(source, "/AccessoryFiles/PanelsAndForms/PanelsAndForms.csv")
        )
        part_rows = _rows(source, _find(source, "/AccessoryFiles/PartFile/Part.csv"))
        part_link_rows = _rows(
            source, _find(source, "/AccessoryFiles/PartFile/LoincPartLink_Primary.csv")
        )
    terms = tuple((
        row["LOINC_NUM"], row.get("LONG_COMMON_NAME", ""), row.get("COMPONENT", ""),
        row.get("PROPERTY", ""), row.get("TIME_ASPCT", ""), row.get("SYSTEM", ""),
        row.get("SCALE_TYP", ""), row.get("METHOD_TYP", ""), row.get("CLASS", ""),
        row.get("STATUS", ""), row.get("VersionLastChanged", ""), _raw(row),
    ) for row in term_rows)
    answers = tuple((
        _first(row, "AnswerListId", "ANSWER_LIST_ID"),
        _first(row, "AnswerStringId", "ANSWER_STRING_ID"),
        _first(row, "SequenceNo", "SEQUENCE_NO"),
        _first(row, "DisplayText", "DISPLAY_TEXT"),
        _first(row, "AnswerCode", "ANSWER_CODE"), _raw(row),
    ) for row in answer_rows)
    answer_links = tuple((
        _first(row, "LoincNumber", "LOINC_NUM"),
        _first(row, "AnswerListId", "ANSWER_LIST_ID"),
        _first(row, "AnswerListLinkTypeName", "ANSWER_LIST_LINK_TYPE_NAME"), _raw(row),
    ) for row in answer_link_rows)
    panels = tuple((
        source_order, _first(row, "PARENT_ID", "ParentLoinc"),
        _first(row, "LOINC_NUM", "LoincNumber"),
        _first(row, "SEQUENCE", "Sequence"), _first(row, "DISPLAY_NAME_FOR_FORM", "DisplayName"),
        _first(row, "ANSWER_ID", "AnswerListId"), _raw(row),
    ) for source_order, row in enumerate(panel_rows))
    parts = tuple((
        _first(row, "PartNumber", "PART_NUMBER"), _first(row, "PartName", "PART_NAME"),
        _first(row, "PartTypeName", "PART_TYPE_NAME"), _first(row, "Status", "STATUS"), _raw(row),
    ) for row in part_rows)
    part_links = tuple((
        _first(row, "LoincNumber", "LOINC_NUM"), _first(row, "PartNumber", "PART_NUMBER"),
        _first(row, "PartTypeName", "PART_TYPE_NAME"), _raw(row),
    ) for row in part_link_rows)
    result = LoincData(terms, answers, answer_links, panels, parts, part_links)
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: LoincData, *, enforce_minimums: bool = True) -> None:
    term_ids = [row[0] for row in data.terms]
    if not all(term_ids) or len(term_ids) != len(set(term_ids)):
        raise ValueError("LOINC term identifiers must be present and unique")
    if enforce_minimums and len(data.terms) < 100_000:
        raise ValueError(f"LOINC release is unexpectedly small: {data.counts()}")


DDL = """
CREATE TABLE IF NOT EXISTS loinc.term (
  release_id text NOT NULL REFERENCES loinc.release(release_id), loinc_num text NOT NULL,
  long_common_name text NOT NULL, component text NOT NULL, property text NOT NULL,
  time_aspect text NOT NULL, system text NOT NULL, scale_type text NOT NULL,
  method_type text NOT NULL, class_code text NOT NULL, status text NOT NULL,
  version_last_changed text NOT NULL, source_row jsonb NOT NULL,
  PRIMARY KEY (release_id, loinc_num)
);
CREATE TABLE IF NOT EXISTS loinc.answer (
  release_id text NOT NULL REFERENCES loinc.release(release_id), answer_list_id text NOT NULL,
  answer_string_id text NOT NULL, sequence_no text NOT NULL, display_text text NOT NULL,
  answer_code text NOT NULL, source_row jsonb NOT NULL,
  PRIMARY KEY (release_id, answer_list_id, answer_string_id)
);
CREATE TABLE IF NOT EXISTS loinc.answer_list_link (
  release_id text NOT NULL REFERENCES loinc.release(release_id), loinc_num text NOT NULL,
  answer_list_id text NOT NULL, link_type text NOT NULL, source_row jsonb NOT NULL,
  PRIMARY KEY (release_id, loinc_num, answer_list_id, link_type)
);
CREATE TABLE IF NOT EXISTS loinc.panel_form (
  release_id text NOT NULL REFERENCES loinc.release(release_id), source_order integer NOT NULL,
  parent_loinc_num text NOT NULL, loinc_num text NOT NULL, sequence_no text NOT NULL,
  display_name text NOT NULL, answer_list_id text NOT NULL, source_row jsonb NOT NULL,
  PRIMARY KEY (release_id, source_order)
);
CREATE TABLE IF NOT EXISTS loinc.part (
  release_id text NOT NULL REFERENCES loinc.release(release_id), part_number text NOT NULL,
  part_name text NOT NULL, part_type text NOT NULL, status text NOT NULL, source_row jsonb NOT NULL,
  PRIMARY KEY (release_id, part_number)
);
CREATE TABLE IF NOT EXISTS loinc.part_link (
  release_id text NOT NULL REFERENCES loinc.release(release_id), loinc_num text NOT NULL,
  part_number text NOT NULL, part_type text NOT NULL, source_row jsonb NOT NULL,
  PRIMARY KEY (release_id, loinc_num, part_number, part_type)
);
"""


def synchronize(conn, version: str, archive: bytes, data: LoincData) -> str:
    release = SourceRelease(
        schema="loinc", version=version, source_url=SOURCE_URL,
        content_sha256=sha256_bytes(archive), completeness="full_declared_scope",
        import_scope={
            "package": "LOINC Complete", "tables": [
                "LoincTable/Loinc.csv", "AnswerFile", "PanelsAndForms",
                "PartFile/Part.csv", "PartFile/LoincPartLink_Primary.csv",
            ], "rights": "licensed source; preserve attribution and per-assessment rights",
        }, license_name=LICENSE_NAME, license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "loinc", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        specs = (
            ("term", ("loinc_num", "long_common_name", "component", "property", "time_aspect",
                      "system", "scale_type", "method_type", "class_code", "status",
                      "version_last_changed", "source_row"), data.terms),
            ("answer", ("answer_list_id", "answer_string_id", "sequence_no", "display_text",
                        "answer_code", "source_row"), data.answers),
            ("answer_list_link", ("loinc_num", "answer_list_id", "link_type", "source_row"),
             data.answer_links),
            ("panel_form", ("source_order", "parent_loinc_num", "loinc_num", "sequence_no", "display_name",
                            "answer_list_id", "source_row"), data.panels),
            ("part", ("part_number", "part_name", "part_type", "status", "source_row"), data.parts),
            ("part_link", ("loinc_num", "part_number", "part_type", "source_row"), data.part_links),
        )
        for table, columns, rows in specs:
            insert_rows_batched(cur, f"loinc.{table}", ("release_id", *columns),
                                ((release.release_id, *row) for row in rows))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True,
                        help="LOINC Complete ZIP downloaded after accepting the LOINC license")
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with open(args.archive, "rb") as source:
        archive = source.read()
    data = parse_archive(archive)
    print(f"validated LOINC {args.version}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(archive)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, args.version, archive, data)
    finally:
        conn.close()
    print(f"active LOINC release: {release_id}")


if __name__ == "__main__":
    main()
