"""Synchronize a pinned IANA tzdb release into the source-owned ``iana`` schema.

The first materialization deliberately mirrors source structures:

* ``country_code`` comes from ``iso3166.tab``;
* ``zone_location`` and ``country_zone`` come from ``zone1970.tab``;
* ``zone`` and ``zone_alias`` come from the release's canonical source files.

It does not invent transition intervals. Those require compiling the pinned release and are
added only with a reproducible TZif compiler and temporal correctness tests.

Run: ``python -m db.sync.sources.iana.sync``
"""
from __future__ import annotations

import argparse
import io
import json
import tarfile
from dataclasses import dataclass

from db.sync._conn import connect
from db.sync.sources.common import download, insert_rows, sha256_bytes

DEFAULT_VERSION = "2026c"
URL_TEMPLATE = "https://data.iana.org/time-zones/releases/tzdata{version}.tar.gz"
ZONE_SOURCE_FILES = (
    "africa", "antarctica", "asia", "australasia", "europe", "northamerica",
    "southamerica", "etcetera", "factory",
)


@dataclass(frozen=True)
class IanaData:
    countries: tuple[tuple[str, str], ...]
    zones: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...]
    locations: tuple[tuple[str, str, str], ...]
    country_zones: tuple[tuple[str, str], ...]

    def counts(self) -> dict[str, int]:
        return {
            "country_code": len(self.countries),
            "zone": len(self.zones),
            "zone_alias": len(self.aliases),
            "zone_location": len(self.locations),
            "country_zone": len(self.country_zones),
        }


def _read_member(archive: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        try:
            member = tf.extractfile(name)
        except KeyError as exc:
            raise ValueError(f"IANA archive is missing {name}") from exc
        if member is None:
            raise ValueError(f"IANA archive is missing {name}")
        return member.read().decode("utf-8")


def parse_archive(archive: bytes, *, enforce_minimums: bool = True) -> IanaData:
    iso_text = _read_member(archive, "iso3166.tab")
    zone_text = _read_member(archive, "zone1970.tab")

    countries = []
    for line in iso_text.splitlines():
        if not line or line.startswith("#"):
            continue
        alpha2, name = line.split("\t", 1)
        countries.append((alpha2, name))

    locations = []
    country_zones = []
    for line in zone_text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) not in (3, 4):
            raise ValueError(f"invalid zone1970.tab row: {line!r}")
        country_list, coordinates, timezone_id = fields[:3]
        comment = fields[3] if len(fields) == 4 else ""
        locations.append((timezone_id, coordinates, comment))
        country_zones.extend((alpha2, timezone_id) for alpha2 in country_list.split(","))

    zones = []
    aliases = []
    for source_name in (*ZONE_SOURCE_FILES, "backward"):
        source_text = _read_member(archive, source_name)
        for line in source_text.splitlines():
            if not line or line.startswith("#") or line[0].isspace():
                continue
            fields = line.split()
            if fields[0] == "Zone" and len(fields) >= 2 and source_name != "backward":
                zones.append(fields[1])
            elif fields[0] == "Link" and len(fields) >= 3:
                aliases.append((fields[2], fields[1]))

    result = IanaData(
        countries=tuple(sorted(countries)),
        zones=tuple(sorted(set(zones))),
        aliases=tuple(sorted(set(aliases))),
        locations=tuple(sorted(locations)),
        country_zones=tuple(sorted(set(country_zones))),
    )
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: IanaData, *, enforce_minimums: bool = True) -> None:
    country_ids = [row[0] for row in data.countries]
    if len(country_ids) != len(set(country_ids)) or any(len(code) != 2 for code in country_ids):
        raise ValueError("IANA country codes must be unique alpha-2 values")
    zone_ids = set(data.zones)
    if len(zone_ids) != len(data.zones):
        raise ValueError("IANA canonical timezone ids must be unique")
    alias_ids = [row[0] for row in data.aliases]
    if len(alias_ids) != len(set(alias_ids)):
        raise ValueError("IANA timezone aliases must be unique")
    alias_targets = dict(data.aliases)
    if zone_ids.intersection(alias_targets):
        raise ValueError("IANA timezone ids cannot be both canonical zones and aliases")
    known_ids = zone_ids.union(alias_targets)
    if any(target not in known_ids for target in alias_targets.values()):
        raise ValueError("IANA timezone alias refers to an unknown target")
    for alias in alias_targets:
        seen = set()
        current = alias
        while current in alias_targets:
            if current in seen:
                raise ValueError("IANA timezone aliases contain a cycle")
            seen.add(current)
            current = alias_targets[current]
    location_ids = [row[0] for row in data.locations]
    if len(location_ids) != len(set(location_ids)):
        raise ValueError("IANA zone locations must be unique")
    if any(zone not in zone_ids for zone, _, _ in data.locations):
        raise ValueError("zone1970.tab refers to an unknown canonical timezone")
    country_set = set(country_ids)
    if any(country not in country_set for country, _ in data.country_zones):
        raise ValueError("zone1970.tab refers to an unknown country code")
    if enforce_minimums and (
        len(data.countries) < 240 or len(data.locations) < 300 or len(data.zones) < 300
    ):
        raise ValueError(f"IANA source is unexpectedly small: {data.counts()}")


DDL = """
CREATE SCHEMA IF NOT EXISTS iana;

CREATE TABLE IF NOT EXISTS iana.release (
  release_id text PRIMARY KEY,
  source_version text NOT NULL,
  source_url text NOT NULL,
  content_sha256 text NOT NULL,
  schema_version integer NOT NULL,
  table_counts jsonb NOT NULL,
  materialized_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL CHECK (status IN ('staged', 'active', 'retired', 'rejected')),
  UNIQUE (source_version, content_sha256)
);
ALTER TABLE iana.release ADD COLUMN IF NOT EXISTS completeness text NOT NULL
  DEFAULT 'full_declared_scope';
ALTER TABLE iana.release ADD COLUMN IF NOT EXISTS import_scope jsonb NOT NULL
  DEFAULT '{"files":["iso3166.tab","zone1970.tab","Zone/Link source records"],"excludes":["compiled transitions"]}'::jsonb;
ALTER TABLE iana.release ADD COLUMN IF NOT EXISTS license_name text NOT NULL
  DEFAULT 'IANA tzdb public-domain notice';
ALTER TABLE iana.release ADD COLUMN IF NOT EXISTS license_url text NOT NULL
  DEFAULT 'https://www.iana.org/time-zones';
CREATE UNIQUE INDEX IF NOT EXISTS ux_iana_release_active
  ON iana.release ((status)) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS iana.country_code (
  release_id text NOT NULL REFERENCES iana.release(release_id),
  alpha2 text NOT NULL CHECK (alpha2 ~ '^[A-Z]{2}$'),
  name text NOT NULL,
  PRIMARY KEY (release_id, alpha2)
);
CREATE TABLE IF NOT EXISTS iana.zone (
  release_id text NOT NULL REFERENCES iana.release(release_id),
  timezone_id text NOT NULL,
  PRIMARY KEY (release_id, timezone_id)
);
CREATE TABLE IF NOT EXISTS iana.zone_alias (
  release_id text NOT NULL REFERENCES iana.release(release_id),
  alias_id text NOT NULL,
  target_timezone_id text NOT NULL,
  PRIMARY KEY (release_id, alias_id)
);
CREATE TABLE IF NOT EXISTS iana.zone_location (
  release_id text NOT NULL,
  timezone_id text NOT NULL,
  coordinates text NOT NULL,
  comment text NOT NULL,
  PRIMARY KEY (release_id, timezone_id),
  FOREIGN KEY (release_id, timezone_id) REFERENCES iana.zone(release_id, timezone_id)
);
CREATE TABLE IF NOT EXISTS iana.country_zone (
  release_id text NOT NULL,
  country_alpha2 text NOT NULL,
  timezone_id text NOT NULL,
  PRIMARY KEY (release_id, country_alpha2, timezone_id),
  FOREIGN KEY (release_id, country_alpha2) REFERENCES iana.country_code(release_id, alpha2),
  FOREIGN KEY (release_id, timezone_id) REFERENCES iana.zone(release_id, timezone_id)
);
"""


def synchronize(conn, version: str, source_url: str, archive: bytes, data: IanaData) -> str:
    digest = sha256_bytes(archive)
    release_id = f"{version}+sha256:{digest}"
    counts = data.counts()
    cur = conn.cursor()
    try:
        cur.execute(DDL)
        cur.execute("SELECT status, table_counts FROM iana.release WHERE release_id=%s", (release_id,))
        existing = cur.fetchone()
        if existing and existing[0] == "active":
            if existing[1] != counts:
                raise ValueError("active IANA release counts do not match parsed source")
            conn.commit()
            return release_id
        if existing:
            raise ValueError(f"IANA release already exists with status {existing[0]!r}")

        cur.execute(
            "INSERT INTO iana.release "
            "(release_id,source_version,source_url,content_sha256,schema_version,table_counts,status) "
            "VALUES (%s,%s,%s,%s,1,%s::jsonb,'staged')",
            (release_id, version, source_url, digest, json.dumps(counts, sort_keys=True)),
        )
        insert_rows(cur, "iana.country_code", ("release_id", "alpha2", "name"),
                    ((release_id, *row) for row in data.countries))
        insert_rows(cur, "iana.zone", ("release_id", "timezone_id"),
                    ((release_id, zone) for zone in data.zones))
        insert_rows(cur, "iana.zone_alias", ("release_id", "alias_id", "target_timezone_id"),
                    ((release_id, *row) for row in data.aliases))
        insert_rows(cur, "iana.zone_location",
                    ("release_id", "timezone_id", "coordinates", "comment"),
                    ((release_id, *row) for row in data.locations))
        insert_rows(cur, "iana.country_zone",
                    ("release_id", "country_alpha2", "timezone_id"),
                    ((release_id, *row) for row in data.country_zones))

        for table, expected in counts.items():
            cur.execute(f"SELECT count(*) FROM iana.{table} WHERE release_id=%s", (release_id,))
            actual = cur.fetchone()[0]
            if actual != expected:
                raise ValueError(f"iana.{table}: loaded {actual}, expected {expected}")
        cur.execute("UPDATE iana.release SET status='retired' WHERE status='active'")
        cur.execute("UPDATE iana.release SET status='active' WHERE release_id=%s", (release_id,))
        conn.commit()
        return release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--archive", help="use a local release tarball instead of downloading")
    parser.add_argument("--dry-run", action="store_true",
                        help="download, parse, and validate without writing PostgreSQL")
    args = parser.parse_args()
    source_url = URL_TEMPLATE.format(version=args.version)
    print(f"reading IANA tzdb {args.version}...", flush=True)
    if args.archive:
        with open(args.archive, "rb") as source:
            archive = source.read()
    else:
        archive = download(source_url)
    data = parse_archive(archive)
    print(f"validated {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(archive)}", flush=True)
        return
    conn = connect()
    try:
        release_id = synchronize(conn, args.version, source_url, archive, data)
    finally:
        conn.close()
    print(f"active IANA release: {release_id}", flush=True)


if __name__ == "__main__":
    main()
