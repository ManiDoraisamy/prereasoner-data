"""Synchronize a bounded, reproducible snapshot of the Nager.Date holiday API."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from db.sync._conn import connect
from db.sync.sources.common import (
    SourceRelease, USER_AGENT, insert_rows, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

API_ROOT = "https://date.nager.at/api/v4"
COUNTRIES_URL = "https://date.nager.at/api/v3/AvailableCountries"
LICENSE_NAME = "Nager.Date API terms; community-maintained data"
LICENSE_URL = "https://date.nager.at/Legal/Terms"


@dataclass(frozen=True)
class NagerData:
    countries: tuple[tuple[str, str], ...]
    holidays: tuple[tuple, ...]
    subdivisions: tuple[tuple, ...]
    types: tuple[tuple, ...]

    def counts(self) -> dict[str, int]:
        return {
            "country": len(self.countries), "holiday": len(self.holidays),
            "holiday_subdivision": len(self.subdivisions), "holiday_type": len(self.types),
        }


def _get_json(url: str):
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=90)
    response.raise_for_status()
    return response.json()


def fetch_snapshot(year_start: int, year_end: int, *, workers: int = 16) -> bytes:
    if year_start > year_end:
        raise ValueError("holiday year_start must not exceed year_end")
    countries = _get_json(COUNTRIES_URL)
    jobs = [(country["countryCode"], year)
            for country in countries for year in range(year_start, year_end + 1)]

    def fetch(job):
        country, year = job
        return country, year, _get_json(f"{API_ROOT}/Holidays/{country}/{year}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        holiday_sets = list(pool.map(fetch, jobs))
    payload = {
        "api_root": API_ROOT, "year_start": year_start, "year_end": year_end,
        "countries": countries,
        "holiday_sets": [
            {"countryCode": country, "year": year, "holidays": holidays}
            for country, year, holidays in holiday_sets
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def parse_snapshot(snapshot: bytes, *, enforce_minimums: bool = True) -> NagerData:
    payload = json.loads(snapshot)
    countries = tuple(sorted(
        (row["countryCode"], row["name"]) for row in payload["countries"]
    ))
    holidays = []
    subdivisions = []
    types = []
    for holiday_set in payload["holiday_sets"]:
        country = holiday_set["countryCode"]
        year = int(holiday_set["year"])
        for source_order, row in enumerate(holiday_set["holidays"]):
            if row["countryCode"] != country or int(row["date"][:4]) != year:
                raise ValueError("Nager.Date response does not match requested country/year")
            holiday_id = f"{country}:{year}:{source_order}"
            holidays.append((
                holiday_id, country, row["date"], source_order, row["name"],
                bool(row.get("nationalHoliday", False)),
            ))
            subdivisions.extend(
                (holiday_id, subdivision)
                for subdivision in (row.get("subdivisionCodes") or [])
            )
            types.extend((holiday_id, kind) for kind in row.get("holidayTypes", []))
    result = NagerData(tuple(countries), tuple(holidays), tuple(subdivisions), tuple(types))
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: NagerData, *, enforce_minimums: bool = True) -> None:
    country_ids = [row[0] for row in data.countries]
    holiday_ids = [row[0] for row in data.holidays]
    if len(country_ids) != len(set(country_ids)):
        raise ValueError("Nager.Date country codes are not unique")
    if len(holiday_ids) != len(set(holiday_ids)):
        raise ValueError("Nager.Date holiday ids are not unique")
    known_countries = set(country_ids)
    if any(row[1] not in known_countries for row in data.holidays):
        raise ValueError("Nager.Date holiday refers to an unknown country")
    if enforce_minimums and len(data.countries) < 190:
        raise ValueError(f"Nager.Date country coverage is unexpectedly small: {len(data.countries)}")


DDL = """
CREATE TABLE IF NOT EXISTS nager_date.country (
  release_id text NOT NULL REFERENCES nager_date.release(release_id),
  country_code text NOT NULL CHECK (country_code ~ '^[A-Z]{2}$'),
  name text NOT NULL,
  PRIMARY KEY (release_id, country_code)
);
CREATE TABLE IF NOT EXISTS nager_date.holiday (
  release_id text NOT NULL,
  holiday_id text NOT NULL,
  country_code text NOT NULL,
  holiday_date date NOT NULL,
  source_order integer NOT NULL,
  name text NOT NULL,
  national_holiday boolean NOT NULL,
  PRIMARY KEY (release_id, holiday_id),
  FOREIGN KEY (release_id, country_code) REFERENCES nager_date.country(release_id, country_code)
);
CREATE TABLE IF NOT EXISTS nager_date.holiday_subdivision (
  release_id text NOT NULL,
  holiday_id text NOT NULL,
  subdivision_code text NOT NULL,
  PRIMARY KEY (release_id, holiday_id, subdivision_code),
  FOREIGN KEY (release_id, holiday_id) REFERENCES nager_date.holiday(release_id, holiday_id)
);
CREATE TABLE IF NOT EXISTS nager_date.holiday_type (
  release_id text NOT NULL,
  holiday_id text NOT NULL,
  holiday_type text NOT NULL,
  PRIMARY KEY (release_id, holiday_id, holiday_type),
  FOREIGN KEY (release_id, holiday_id) REFERENCES nager_date.holiday(release_id, holiday_id)
);
"""


def synchronize(conn, version: str, snapshot: bytes, data: NagerData,
                year_start: int, year_end: int) -> str:
    release = SourceRelease(
        schema="nager_date", version=version, source_url=API_ROOT,
        content_sha256=sha256_bytes(snapshot), completeness="bounded_snapshot",
        import_scope={
            "countries": f"all returned by {COUNTRIES_URL}",
            "year_start": year_start, "year_end": year_end,
            "authority": "community-maintained; not a substitute for jurisdictional legal advice",
        }, license_name=LICENSE_NAME, license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "nager_date", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows(cur, "nager_date.country", ("release_id", "country_code", "name"),
                    ((release.release_id, *row) for row in data.countries))
        insert_rows(cur, "nager_date.holiday", (
            "release_id", "holiday_id", "country_code", "holiday_date", "source_order",
            "name", "national_holiday",
        ), ((release.release_id, *row) for row in data.holidays))
        insert_rows(cur, "nager_date.holiday_subdivision",
                    ("release_id", "holiday_id", "subdivision_code"),
                    ((release.release_id, *row) for row in data.subdivisions))
        insert_rows(cur, "nager_date.holiday_type",
                    ("release_id", "holiday_id", "holiday_type"),
                    ((release.release_id, *row) for row in data.types))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year-start", type=int, default=2025)
    parser.add_argument("--year-end", type=int, default=2027)
    parser.add_argument("--snapshot")
    parser.add_argument("--write-snapshot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.snapshot:
        snapshot = open(args.snapshot, "rb").read()
    else:
        snapshot = fetch_snapshot(args.year_start, args.year_end)
    if args.write_snapshot:
        with open(args.write_snapshot, "wb") as target:
            target.write(snapshot)
    data = parse_snapshot(snapshot)
    version = f"{args.year_start}-{args.year_end}"
    print(f"validated Nager.Date {version}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(snapshot)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, version, snapshot, data, args.year_start, args.year_end)
    finally:
        conn.close()
    print(f"active Nager.Date release: {release_id}")


if __name__ == "__main__":
    main()
