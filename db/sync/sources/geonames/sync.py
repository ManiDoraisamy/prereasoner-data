"""Synchronize GeoNames worldwide postal codes and the cities5000 place extract."""
from __future__ import annotations

import argparse
import datetime as dt
import io
import zipfile
from dataclasses import dataclass

from db.sync._conn import connect
from db.sync.migrations import latest_schema_version
from db.sync.sources.common import (
    SourceRelease, download_http, insert_rows_batched, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

POSTAL_URL = "https://download.geonames.org/export/zip/allCountries.zip"
PLACE_URL = "https://download.geonames.org/export/dump/cities5000.zip"
LICENSE_NAME = "Creative Commons Attribution 4.0"
LICENSE_URL = "https://www.geonames.org/export/"


@dataclass(frozen=True)
class GeoNamesData:
    postal_codes: tuple[tuple, ...]
    places: tuple[tuple, ...]

    def counts(self) -> dict[str, int]:
        return {"postal_code": len(self.postal_codes), "place": len(self.places)}


def _member_lines(archive: bytes):
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        names = [name for name in source.namelist()
                 if not name.endswith("/") and not name.upper().startswith("README")]
        if len(names) != 1:
            raise ValueError(f"GeoNames archive must contain one data member, found {names!r}")
        with source.open(names[0]) as member:
            for raw in member:
                yield raw.decode("utf-8").rstrip("\r\n")


def parse_archives(postal_archive: bytes, place_archive: bytes,
                   *, enforce_minimums: bool = True) -> GeoNamesData:
    postal_codes = []
    for source_order, line in enumerate(_member_lines(postal_archive)):
        fields = line.split("\t")
        if len(fields) != 12:
            raise ValueError(f"invalid GeoNames postal row with {len(fields)} fields")
        postal_codes.append((source_order, *fields))
    places = []
    for line in _member_lines(place_archive):
        fields = line.split("\t")
        if len(fields) != 19:
            raise ValueError(f"invalid GeoNames place row with {len(fields)} fields")
        places.append(tuple(fields))
    result = GeoNamesData(tuple(postal_codes), tuple(places))
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: GeoNamesData, *, enforce_minimums: bool = True) -> None:
    place_ids = [row[0] for row in data.places]
    if len(place_ids) != len(set(place_ids)):
        raise ValueError("GeoNames place ids are not unique")
    if any(len(row[1]) != 2 for row in data.postal_codes):
        raise ValueError("GeoNames postal country codes must be alpha-2 values")
    if enforce_minimums and (len(data.postal_codes) < 1_000_000 or len(data.places) < 50_000):
        raise ValueError(f"GeoNames extracts are unexpectedly small: {data.counts()}")


DDL = """
CREATE TABLE IF NOT EXISTS geonames.postal_code (
  release_id text NOT NULL REFERENCES geonames.release(release_id),
  source_order integer NOT NULL,
  country_code text NOT NULL,
  postal_code text NOT NULL,
  place_name text NOT NULL,
  admin_name1 text NOT NULL,
  admin_code1 text NOT NULL,
  admin_name2 text NOT NULL,
  admin_code2 text NOT NULL,
  admin_name3 text NOT NULL,
  admin_code3 text NOT NULL,
  latitude double precision,
  longitude double precision,
  accuracy integer,
  PRIMARY KEY (release_id, source_order)
);
CREATE INDEX IF NOT EXISTS ix_geonames_postal_release_lookup
  ON geonames.postal_code (release_id, country_code, postal_code);
CREATE TABLE IF NOT EXISTS geonames.place (
  release_id text NOT NULL REFERENCES geonames.release(release_id),
  geoname_id bigint NOT NULL,
  name text NOT NULL,
  ascii_name text NOT NULL,
  alternate_names text NOT NULL,
  latitude double precision NOT NULL,
  longitude double precision NOT NULL,
  feature_class text NOT NULL,
  feature_code text NOT NULL,
  country_code text NOT NULL,
  alternate_country_codes text NOT NULL,
  admin_code1 text NOT NULL,
  admin_code2 text NOT NULL,
  admin_code3 text NOT NULL,
  admin_code4 text NOT NULL,
  population bigint NOT NULL,
  elevation integer,
  dem integer NOT NULL,
  timezone_id text NOT NULL,
  modified_on date NOT NULL,
  PRIMARY KEY (release_id, geoname_id)
);
"""


def _nullable(value: str):
    return value if value != "" else None


def synchronize(conn, version: str, postal_archive: bytes, place_archive: bytes,
                data: GeoNamesData) -> str:
    identity = (
        f"postal:{sha256_bytes(postal_archive)}\nplaces:{sha256_bytes(place_archive)}\n"
    ).encode()
    release = SourceRelease(
        schema="geonames", version=version, source_url=";".join((POSTAL_URL, PLACE_URL)),
        content_sha256=sha256_bytes(identity), completeness="full_declared_scope",
        import_scope={
            "postal_code": "complete allCountries.zip artifact",
            "place": "complete cities5000.zip extract; not the allCountries place dump",
            "quality": "source accuracy field retained; postal coverage varies by country",
        }, license_name=LICENSE_NAME, license_url=LICENSE_URL,
        schema_version=latest_schema_version("geonames"),
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "geonames", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows_batched(cur, "geonames.postal_code", (
            "release_id", "source_order", "country_code", "postal_code", "place_name",
            "admin_name1", "admin_code1", "admin_name2", "admin_code2", "admin_name3",
            "admin_code3", "latitude", "longitude", "accuracy",
        ), ((release.release_id, *row[:10], _nullable(row[10]), _nullable(row[11]),
             _nullable(row[12])) for row in data.postal_codes))
        insert_rows_batched(cur, "geonames.place", (
            "release_id", "geoname_id", "name", "ascii_name", "alternate_names", "latitude",
            "longitude", "feature_class", "feature_code", "country_code",
            "alternate_country_codes", "admin_code1", "admin_code2", "admin_code3",
            "admin_code4", "population", "elevation", "dem", "timezone_id", "modified_on",
        ), ((release.release_id, *row[:15], _nullable(row[15]), row[16], row[17], row[18])
            for row in data.places))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--postal-archive")
    parser.add_argument("--place-archive")
    parser.add_argument("--version", default=dt.date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    postal = (open(args.postal_archive, "rb").read() if args.postal_archive
              else download_http(POSTAL_URL, timeout=300))
    places = (open(args.place_archive, "rb").read() if args.place_archive
              else download_http(PLACE_URL, timeout=300))
    data = parse_archives(postal, places)
    print(f"validated GeoNames {args.version}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"postal-sha256:{sha256_bytes(postal)}")
        print(f"place-sha256:{sha256_bytes(places)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, args.version, postal, places, data)
    finally:
        conn.close()
    print(f"active GeoNames release: {release_id}")


if __name__ == "__main__":
    main()
