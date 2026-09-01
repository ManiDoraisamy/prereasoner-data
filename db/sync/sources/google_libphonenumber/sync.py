"""Synchronize Google's libphonenumber metadata without claiming number reachability."""
from __future__ import annotations

import argparse
import io
import json
from defusedxml import ElementTree as ET
import zipfile
from dataclasses import dataclass

from db.sync._conn import connect
from db.sync.sources.common import (
    SourceRelease, download_http, insert_rows, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

DEFAULT_VERSION = "9.0.31"
URL_TEMPLATE = "https://codeload.github.com/google/libphonenumber/zip/refs/tags/v{version}"
LICENSE_NAME = "Apache License 2.0"
LICENSE_URL = "https://github.com/google/libphonenumber/blob/master/LICENSE"


@dataclass(frozen=True)
class LibPhoneData:
    territories: tuple[tuple, ...]
    number_patterns: tuple[tuple, ...]
    formats: tuple[tuple, ...]

    def counts(self) -> dict[str, int]:
        return {
            "territory": len(self.territories),
            "number_pattern": len(self.number_patterns),
            "number_format": len(self.formats),
        }


def _text(node: ET.Element | None) -> str:
    return "" if node is None else (node.text or "").strip()


def parse_archive(archive: bytes, *, enforce_minimums: bool = True) -> LibPhoneData:
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        names = [name for name in source.namelist()
                 if name.endswith("resources/PhoneNumberMetadata.xml")]
        if len(names) != 1:
            raise ValueError(f"libphonenumber archive must contain primary metadata, found {names!r}")
        root = ET.fromstring(source.read(names[0]))
    territories = []
    patterns = []
    formats = []
    for territory in root.findall("./territories/territory"):
        attrs = territory.attrib
        territory_id = attrs["id"]
        calling_code = attrs["countryCode"]
        territories.append((
            territory_id, calling_code, attrs.get("mainCountryForCode") == "true",
            attrs.get("leadingDigits", ""), attrs.get("internationalPrefix", ""),
            attrs.get("preferredInternationalPrefix", ""), attrs.get("nationalPrefix", ""),
            attrs.get("nationalPrefixForParsing", ""), attrs.get("nationalPrefixTransformRule", ""),
            attrs.get("preferredExtnPrefix", ""),
            attrs.get("mobileNumberPortableRegion") == "true",
        ))
        for desc in territory:
            if desc.tag in {"availableFormats", "references"}:
                continue
            possible = desc.find("possibleLengths")
            patterns.append((
                territory_id, calling_code, desc.tag, _text(desc.find("nationalNumberPattern")),
                "" if possible is None else possible.attrib.get("national", ""),
                "" if possible is None else possible.attrib.get("localOnly", ""),
                _text(desc.find("exampleNumber")),
            ))
        available = territory.find("availableFormats")
        if available is not None:
            for source_order, number_format in enumerate(available.findall("numberFormat")):
                formats.append((
                    territory_id, calling_code, source_order, number_format.attrib["pattern"],
                    json.dumps([_text(e) for e in number_format.findall("leadingDigits")]),
                    _text(number_format.find("format")),
                    number_format.attrib.get("nationalPrefixFormattingRule", ""),
                    number_format.attrib.get("domesticCarrierCodeFormattingRule", ""),
                    number_format.attrib.get("nationalPrefixOptionalWhenFormatting") == "true",
                    _text(number_format.find("intlFormat")),
                ))
    result = LibPhoneData(tuple(territories), tuple(patterns), tuple(formats))
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: LibPhoneData, *, enforce_minimums: bool = True) -> None:
    territory_ids = [(row[0], row[1]) for row in data.territories]
    if len(territory_ids) != len(set(territory_ids)):
        raise ValueError("libphonenumber territory/calling-code keys are not unique")
    known = set(territory_ids)
    if any((row[0], row[1]) not in known for row in data.number_patterns + data.formats):
        raise ValueError("libphonenumber child row refers to an unknown territory")
    pattern_keys = [(row[0], row[1], row[2]) for row in data.number_patterns]
    if len(pattern_keys) != len(set(pattern_keys)):
        raise ValueError("libphonenumber number-pattern keys are not unique")
    if enforce_minimums and (len(data.territories) < 240 or len(data.number_patterns) < 1_000):
        raise ValueError(f"libphonenumber metadata is unexpectedly small: {data.counts()}")


DDL = """
CREATE TABLE IF NOT EXISTS google_libphonenumber.territory (
  release_id text NOT NULL REFERENCES google_libphonenumber.release(release_id),
  territory_id text NOT NULL,
  country_calling_code text NOT NULL,
  main_country_for_code boolean NOT NULL,
  leading_digits text NOT NULL,
  international_prefix text NOT NULL,
  preferred_international_prefix text NOT NULL,
  national_prefix text NOT NULL,
  national_prefix_for_parsing text NOT NULL,
  national_prefix_transform_rule text NOT NULL,
  preferred_extension_prefix text NOT NULL,
  mobile_number_portable_region boolean NOT NULL,
  PRIMARY KEY (release_id, territory_id, country_calling_code)
);
CREATE TABLE IF NOT EXISTS google_libphonenumber.number_pattern (
  release_id text NOT NULL,
  territory_id text NOT NULL,
  country_calling_code text NOT NULL,
  number_type text NOT NULL,
  national_number_pattern text NOT NULL,
  possible_lengths text NOT NULL,
  local_only_lengths text NOT NULL,
  example_number text NOT NULL,
  PRIMARY KEY (release_id, territory_id, country_calling_code, number_type),
  FOREIGN KEY (release_id, territory_id, country_calling_code)
    REFERENCES google_libphonenumber.territory(release_id, territory_id, country_calling_code)
);
CREATE TABLE IF NOT EXISTS google_libphonenumber.number_format (
  release_id text NOT NULL,
  territory_id text NOT NULL,
  country_calling_code text NOT NULL,
  source_order integer NOT NULL,
  pattern text NOT NULL,
  leading_digits jsonb NOT NULL,
  format_template text NOT NULL,
  national_prefix_formatting_rule text NOT NULL,
  domestic_carrier_code_formatting_rule text NOT NULL,
  national_prefix_optional_when_formatting boolean NOT NULL,
  international_format text NOT NULL,
  PRIMARY KEY (release_id, territory_id, country_calling_code, source_order),
  FOREIGN KEY (release_id, territory_id, country_calling_code)
    REFERENCES google_libphonenumber.territory(release_id, territory_id, country_calling_code)
);
"""


def synchronize(conn, version: str, source_url: str, archive: bytes, data: LibPhoneData) -> str:
    release = SourceRelease(
        schema="google_libphonenumber", version=version, source_url=source_url,
        content_sha256=sha256_bytes(archive), completeness="full_declared_scope",
        import_scope={
            "artifact": "resources/PhoneNumberMetadata.xml",
            "semantics": "format and possible-number metadata; not reachability or subscriber identity",
        }, license_name=LICENSE_NAME, license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "google_libphonenumber", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows(cur, "google_libphonenumber.territory", (
            "release_id", "territory_id", "country_calling_code", "main_country_for_code",
            "leading_digits", "international_prefix", "preferred_international_prefix",
            "national_prefix", "national_prefix_for_parsing", "national_prefix_transform_rule",
            "preferred_extension_prefix", "mobile_number_portable_region",
        ), ((release.release_id, *row) for row in data.territories))
        insert_rows(cur, "google_libphonenumber.number_pattern", (
            "release_id", "territory_id", "country_calling_code", "number_type", "national_number_pattern",
            "possible_lengths", "local_only_lengths", "example_number",
        ), ((release.release_id, *row) for row in data.number_patterns))
        insert_rows(cur, "google_libphonenumber.number_format", (
            "release_id", "territory_id", "country_calling_code", "source_order", "pattern", "leading_digits",
            "format_template", "national_prefix_formatting_rule",
            "domestic_carrier_code_formatting_rule", "national_prefix_optional_when_formatting",
            "international_format",
        ), ((release.release_id, *row) for row in data.formats))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--archive")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    source_url = URL_TEMPLATE.format(version=args.version)
    archive = open(args.archive, "rb").read() if args.archive else download_http(source_url)
    data = parse_archive(archive)
    print(f"validated libphonenumber {args.version}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(archive)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, args.version, source_url, archive, data)
    finally:
        conn.close()
    print(f"active Google libphonenumber release: {release_id}")


if __name__ == "__main__":
    main()
