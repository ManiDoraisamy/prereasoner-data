"""Synchronize a pinned Unicode CLDR release into the source-owned ``cldr`` schema.

The materialization preserves the structures CLDR publishes. Locale display names and
symbols are separate from code metadata; temporal territory-currency rows remain temporal;
and unit conversion inputs are not collapsed into an inferred domain catalog.

Run: ``python -m db.sync.sources.cldr.sync``
"""
from __future__ import annotations

import argparse
import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date

from db.sync._conn import connect
from db.sync.migrations import latest_schema_version
from db.sync.sources.common import (
    SourceRelease, download, insert_rows, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

DEFAULT_VERSION = "48.2"
URL_TEMPLATE = "https://github.com/unicode-org/cldr/archive/refs/tags/release-{tag}.zip"


@dataclass(frozen=True)
class CldrData:
    territory_codes: tuple[tuple[str, str, str, str], ...]
    territory_aliases: tuple[tuple[str, str, str], ...]
    territory_names: tuple[tuple[str, str, str, str, str], ...]
    currency_codes: tuple[tuple[str, str], ...]
    currency_names: tuple[tuple[str, str, str, str, str, str], ...]
    currency_symbols: tuple[tuple[str, str, str, str, str], ...]
    currency_fractions: tuple[tuple[str, int, int, int | None, int | None], ...]
    territory_currencies: tuple[tuple[str, int, str, date | None, date | None, bool], ...]
    unit_prefixes: tuple[tuple[str, str, str, str], ...]
    unit_constants: tuple[tuple[str, str, str, str], ...]
    unit_quantities: tuple[tuple[str, str, str], ...]
    unit_conversions: tuple[tuple[str, str, str, str, str, str, str], ...]
    unit_aliases: tuple[tuple[str, str, str], ...]
    unit_preferences: tuple[tuple[str, str, int, str, str, str, str], ...]

    def counts(self) -> dict[str, int]:
        return {
            "territory_code": len(self.territory_codes),
            "territory_alias": len(self.territory_aliases),
            "territory_name": len(self.territory_names),
            "currency_code": len(self.currency_codes),
            "currency_name": len(self.currency_names),
            "currency_symbol": len(self.currency_symbols),
            "currency_fraction": len(self.currency_fractions),
            "territory_currency": len(self.territory_currencies),
            "unit_prefix": len(self.unit_prefixes),
            "unit_constant": len(self.unit_constants),
            "unit_quantity": len(self.unit_quantities),
            "unit_conversion": len(self.unit_conversions),
            "unit_alias": len(self.unit_aliases),
            "unit_preference": len(self.unit_preferences),
        }


def _archive_prefix(zf: zipfile.ZipFile) -> str:
    candidates = {name.split("/", 1)[0] for name in zf.namelist() if "/" in name}
    if len(candidates) != 1:
        raise ValueError(f"CLDR archive must have one root directory, found {sorted(candidates)!r}")
    return candidates.pop()


def _xml(zf: zipfile.ZipFile, path: str) -> ET.Element:
    try:
        return ET.fromstring(zf.read(path))
    except KeyError as exc:
        raise ValueError(f"CLDR archive is missing {path}") from exc


def _text(element: ET.Element) -> str:
    return (element.text or "").strip()


def parse_archive(archive: bytes, *, enforce_minimums: bool = True) -> CldrData:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        prefix = _archive_prefix(zf)
        supplemental = _xml(zf, f"{prefix}/common/supplemental/supplementalData.xml")
        metadata = _xml(zf, f"{prefix}/common/supplemental/supplementalMetadata.xml")
        units = _xml(zf, f"{prefix}/common/supplemental/units.xml")

        territory_codes = tuple(sorted(
            (e.attrib["type"], e.attrib.get("numeric", ""), e.attrib.get("alpha3", ""),
             e.attrib.get("fips10", ""))
            for e in supplemental.iter("territoryCodes")
        ))
        territory_aliases = tuple(sorted(
            (e.attrib["type"], e.attrib.get("replacement", ""), e.attrib["reason"])
            for e in metadata.iter("territoryAlias")
        ))
        currency_codes = tuple(sorted(
            (e.attrib["type"], e.attrib.get("numeric", ""))
            for e in supplemental.iter("currencyCodes")
        ))

        currency_fractions = tuple(sorted(
            (e.attrib["iso4217"], int(e.attrib["digits"]), int(e.attrib["rounding"]),
             int(e.attrib["cashDigits"]) if "cashDigits" in e.attrib else None,
             int(e.attrib["cashRounding"]) if "cashRounding" in e.attrib else None)
            for e in supplemental.findall("./currencyData/fractions/info")
        ))
        territory_currencies = []
        for region in supplemental.findall("./currencyData/region"):
            territory = region.attrib["iso3166"]
            for source_order, currency in enumerate(region.findall("currency")):
                territory_currencies.append((
                    territory, source_order, currency.attrib["iso4217"],
                    date.fromisoformat(currency.attrib["from"]) if currency.attrib.get("from") else None,
                    date.fromisoformat(currency.attrib["to"]) if currency.attrib.get("to") else None,
                    currency.attrib.get("tender", "true") != "false",
                ))

        unit_prefixes = tuple(sorted(
            (e.attrib["type"], e.attrib["symbol"], e.attrib.get("power10", ""),
             e.attrib.get("power2", ""))
            for e in units.iter("unitPrefix")
        ))
        unit_constants = tuple(sorted(
            (e.attrib["constant"], e.attrib["value"], e.attrib.get("status", ""),
             e.attrib.get("description", ""))
            for e in units.iter("unitConstant")
        ))
        unit_quantities = tuple(sorted(
            (e.attrib["baseUnit"], e.attrib["quantity"], e.attrib.get("status", ""))
            for e in units.iter("unitQuantity")
        ))
        unit_conversions = tuple(sorted(
            (e.attrib["source"], e.attrib["baseUnit"], e.attrib.get("factor", ""),
             e.attrib.get("offset", ""), e.attrib.get("special", ""),
             e.attrib.get("systems", ""), e.attrib.get("description", ""))
            for e in units.iter("convertUnit")
        ))
        unit_aliases = tuple(sorted(
            (e.attrib["type"], e.attrib["replacement"], e.attrib["reason"])
            for e in units.iter("unitAlias")
        ))
        unit_preferences = []
        preference_root = units.find("unitPreferenceData")
        if preference_root is None:
            raise ValueError("CLDR units.xml is missing unitPreferenceData")
        for group in preference_root.findall("unitPreferences"):
            category = group.attrib["category"]
            usage = group.attrib["usage"]
            for source_order, preference in enumerate(group.findall("unitPreference")):
                unit_preferences.append((
                    category, usage, source_order, preference.attrib["regions"],
                    preference.attrib.get("geq", ""), preference.attrib.get("skeleton", ""),
                    _text(preference),
                ))

        territory_names = []
        currency_names = []
        currency_symbols = []
        main_prefix = f"{prefix}/common/main/"
        locale_files = sorted(
            name for name in zf.namelist()
            if name.startswith(main_prefix) and name.endswith(".xml") and "/" not in name[len(main_prefix):]
        )
        for path in locale_files:
            locale = path[len(main_prefix):-4]
            root = _xml(zf, path)
            for territory in root.findall("./localeDisplayNames/territories/territory"):
                territory_names.append((
                    locale, territory.attrib["type"], territory.attrib.get("alt", ""),
                    territory.attrib.get("draft", ""), _text(territory),
                ))
            for currency in root.findall("./numbers/currencies/currency"):
                code = currency.attrib["type"]
                for display_name in currency.findall("displayName"):
                    currency_names.append((
                        locale, code, display_name.attrib.get("count", ""),
                        display_name.attrib.get("alt", ""), display_name.attrib.get("draft", ""),
                        _text(display_name),
                    ))
                for symbol in currency.findall("symbol"):
                    currency_symbols.append((
                        locale, code, symbol.attrib.get("alt", ""),
                        symbol.attrib.get("draft", ""), _text(symbol),
                    ))

    result = CldrData(
        territory_codes=territory_codes,
        territory_aliases=territory_aliases,
        territory_names=tuple(sorted(territory_names)),
        currency_codes=currency_codes,
        currency_names=tuple(sorted(currency_names)),
        currency_symbols=tuple(sorted(currency_symbols)),
        currency_fractions=currency_fractions,
        territory_currencies=tuple(sorted(territory_currencies)),
        unit_prefixes=unit_prefixes,
        unit_constants=unit_constants,
        unit_quantities=unit_quantities,
        unit_conversions=unit_conversions,
        unit_aliases=unit_aliases,
        unit_preferences=tuple(sorted(unit_preferences)),
    )
    validate(result, enforce_minimums=enforce_minimums)
    return result


def _assert_unique(label: str, values: list[tuple]) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"CLDR {label} keys must be unique")


def validate(data: CldrData, *, enforce_minimums: bool = True) -> None:
    _assert_unique("territory-code", [(r[0],) for r in data.territory_codes])
    _assert_unique("territory-alias", [(r[0],) for r in data.territory_aliases])
    _assert_unique("territory-name", [r[:4] for r in data.territory_names])
    _assert_unique("currency-code", [(r[0],) for r in data.currency_codes])
    _assert_unique("currency-name", [r[:5] for r in data.currency_names])
    _assert_unique("currency-symbol", [r[:4] for r in data.currency_symbols])
    _assert_unique("currency-fraction", [(r[0],) for r in data.currency_fractions])
    _assert_unique("territory-currency", [r[:2] for r in data.territory_currencies])
    _assert_unique("unit-prefix", [(r[0],) for r in data.unit_prefixes])
    _assert_unique("unit-constant", [(r[0],) for r in data.unit_constants])
    _assert_unique("unit-quantity", [(r[0],) for r in data.unit_quantities])
    _assert_unique("unit-conversion", [(r[0],) for r in data.unit_conversions])
    _assert_unique("unit-alias", [(r[0],) for r in data.unit_aliases])
    _assert_unique("unit-preference", [r[:3] for r in data.unit_preferences])
    if any(not row[0] for rows in (
        data.territory_codes, data.territory_aliases, data.currency_codes,
        data.currency_fractions, data.unit_prefixes, data.unit_constants,
        data.unit_quantities, data.unit_conversions, data.unit_aliases,
    ) for row in rows):
        raise ValueError("CLDR source identifiers must not be empty")
    if any(not row[-1] for row in data.territory_names):
        raise ValueError("CLDR territory names must not be empty")
    if any(not row[-1] for row in data.currency_names + data.currency_symbols):
        raise ValueError("CLDR currency display values must not be empty")
    if enforce_minimums:
        counts = data.counts()
        expected_minimums = {
            "territory_code": 300,
            "territory_alias": 600,
            "territory_name": 10000,
            "currency_code": 180,
            "currency_name": 10000,
            "currency_symbol": 1000,
            "currency_fraction": 70,
            "territory_currency": 500,
            "unit_prefix": 30,
            "unit_constant": 15,
            "unit_quantity": 45,
            "unit_conversion": 150,
            "unit_alias": 10,
            "unit_preference": 140,
        }
        small = {name: (counts[name], minimum) for name, minimum in expected_minimums.items()
                 if counts[name] < minimum}
        if small:
            raise ValueError(f"CLDR source is unexpectedly small: {small}")


DDL = """
CREATE SCHEMA IF NOT EXISTS cldr;

CREATE TABLE IF NOT EXISTS cldr.release (
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
ALTER TABLE cldr.release ADD COLUMN IF NOT EXISTS completeness text NOT NULL
  DEFAULT 'full_declared_scope';
ALTER TABLE cldr.release ADD COLUMN IF NOT EXISTS import_scope jsonb NOT NULL
  DEFAULT '{"structures":["territory","currency","unit","preference","localized display"],"excludes":["calendars","collation","annotations","numbering"]}'::jsonb;
ALTER TABLE cldr.release ADD COLUMN IF NOT EXISTS license_name text NOT NULL
  DEFAULT 'Unicode License v3';
ALTER TABLE cldr.release ADD COLUMN IF NOT EXISTS license_url text NOT NULL
  DEFAULT 'https://www.unicode.org/license.txt';
CREATE UNIQUE INDEX IF NOT EXISTS ux_cldr_release_active
  ON cldr.release ((status)) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS cldr.territory_code (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  territory_code text NOT NULL,
  numeric_code text NOT NULL,
  alpha3 text NOT NULL,
  fips10 text NOT NULL,
  PRIMARY KEY (release_id, territory_code)
);
CREATE TABLE IF NOT EXISTS cldr.territory_alias (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  territory_code text NOT NULL,
  replacement text NOT NULL,
  reason text NOT NULL,
  PRIMARY KEY (release_id, territory_code)
);
CREATE TABLE IF NOT EXISTS cldr.territory_name (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  locale text NOT NULL,
  territory_code text NOT NULL,
  alt text NOT NULL,
  draft text NOT NULL,
  name text NOT NULL,
  PRIMARY KEY (release_id, locale, territory_code, alt)
);
CREATE TABLE IF NOT EXISTS cldr.currency_code (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  currency_code text NOT NULL,
  numeric_code text NOT NULL,
  PRIMARY KEY (release_id, currency_code)
);
CREATE TABLE IF NOT EXISTS cldr.currency_name (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  locale text NOT NULL,
  currency_code text NOT NULL,
  plural_count text NOT NULL,
  alt text NOT NULL,
  draft text NOT NULL,
  name text NOT NULL,
  PRIMARY KEY (release_id, locale, currency_code, plural_count, alt)
);
CREATE TABLE IF NOT EXISTS cldr.currency_symbol (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  locale text NOT NULL,
  currency_code text NOT NULL,
  alt text NOT NULL,
  draft text NOT NULL,
  symbol text NOT NULL,
  PRIMARY KEY (release_id, locale, currency_code, alt)
);
CREATE TABLE IF NOT EXISTS cldr.currency_fraction (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  currency_code text NOT NULL,
  digits smallint NOT NULL,
  rounding integer NOT NULL,
  cash_digits smallint,
  cash_rounding integer,
  PRIMARY KEY (release_id, currency_code)
);
CREATE TABLE IF NOT EXISTS cldr.territory_currency (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  territory_code text NOT NULL,
  source_order integer NOT NULL,
  currency_code text NOT NULL,
  valid_from date,
  valid_to date,
  tender boolean NOT NULL,
  PRIMARY KEY (release_id, territory_code, source_order)
);
CREATE TABLE IF NOT EXISTS cldr.unit_prefix (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  prefix text NOT NULL,
  symbol text NOT NULL,
  power10 text NOT NULL,
  power2 text NOT NULL,
  PRIMARY KEY (release_id, prefix)
);
CREATE TABLE IF NOT EXISTS cldr.unit_constant (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  constant text NOT NULL,
  value text NOT NULL,
  status text NOT NULL,
  description text NOT NULL,
  PRIMARY KEY (release_id, constant)
);
CREATE TABLE IF NOT EXISTS cldr.unit_quantity (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  base_unit text NOT NULL,
  quantity text NOT NULL,
  status text NOT NULL,
  PRIMARY KEY (release_id, base_unit)
);
CREATE TABLE IF NOT EXISTS cldr.unit_conversion (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  source_unit text NOT NULL,
  base_unit text NOT NULL,
  factor_expression text NOT NULL,
  offset_expression text NOT NULL,
  special_function text NOT NULL,
  systems text NOT NULL,
  description text NOT NULL,
  PRIMARY KEY (release_id, source_unit)
);
CREATE TABLE IF NOT EXISTS cldr.unit_alias (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  unit_code text NOT NULL,
  replacement text NOT NULL,
  reason text NOT NULL,
  PRIMARY KEY (release_id, unit_code)
);
CREATE TABLE IF NOT EXISTS cldr.unit_preference (
  release_id text NOT NULL REFERENCES cldr.release(release_id),
  category text NOT NULL,
  usage text NOT NULL,
  source_order integer NOT NULL,
  regions text NOT NULL,
  greater_or_equal text NOT NULL,
  skeleton text NOT NULL,
  unit_code text NOT NULL,
  PRIMARY KEY (release_id, category, usage, source_order)
);
"""


TABLE_ROWS = (
    ("territory_code", ("territory_code", "numeric_code", "alpha3", "fips10"), "territory_codes"),
    ("territory_alias", ("territory_code", "replacement", "reason"), "territory_aliases"),
    ("territory_name", ("locale", "territory_code", "alt", "draft", "name"), "territory_names"),
    ("currency_code", ("currency_code", "numeric_code"), "currency_codes"),
    ("currency_name", ("locale", "currency_code", "plural_count", "alt", "draft", "name"), "currency_names"),
    ("currency_symbol", ("locale", "currency_code", "alt", "draft", "symbol"), "currency_symbols"),
    ("currency_fraction", ("currency_code", "digits", "rounding", "cash_digits", "cash_rounding"), "currency_fractions"),
    ("territory_currency", ("territory_code", "source_order", "currency_code", "valid_from", "valid_to", "tender"), "territory_currencies"),
    ("unit_prefix", ("prefix", "symbol", "power10", "power2"), "unit_prefixes"),
    ("unit_constant", ("constant", "value", "status", "description"), "unit_constants"),
    ("unit_quantity", ("base_unit", "quantity", "status"), "unit_quantities"),
    ("unit_conversion", ("source_unit", "base_unit", "factor_expression", "offset_expression",
                         "special_function", "systems", "description"), "unit_conversions"),
    ("unit_alias", ("unit_code", "replacement", "reason"), "unit_aliases"),
    ("unit_preference", ("category", "usage", "source_order", "regions", "greater_or_equal", "skeleton", "unit_code"), "unit_preferences"),
)


def synchronize(conn, version: str, source_url: str, archive: bytes, data: CldrData) -> str:
    digest = sha256_bytes(archive)
    release = SourceRelease(
        schema="cldr", version=version, source_url=source_url, content_sha256=digest,
        completeness="full_declared_scope",
        import_scope={
            "structures": ["territory", "currency", "unit", "preference", "localized display"],
            "excludes": ["calendars", "collation", "annotations", "numbering"],
        },
        license_name="Unicode License v3",
        license_url="https://www.unicode.org/license.txt",
        schema_version=latest_schema_version("cldr"),
    )
    counts = data.counts()
    cur = conn.cursor()
    try:
        prepare_source(cur, "cldr", DDL)
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        for table, columns, attribute in TABLE_ROWS:
            rows = getattr(data, attribute)
            insert_rows(cur, f"cldr.{table}", ("release_id", *columns),
                        ((release.release_id, *row) for row in rows))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--archive", help="use a local release ZIP instead of downloading")
    parser.add_argument("--dry-run", action="store_true",
                        help="download, parse, and validate without writing PostgreSQL")
    args = parser.parse_args()
    tag = args.version.replace(".", "-")
    source_url = URL_TEMPLATE.format(tag=tag)
    print(f"reading Unicode CLDR {args.version}...", flush=True)
    if args.archive:
        with open(args.archive, "rb") as source:
            archive = source.read()
    else:
        archive = download(source_url, timeout=300)
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
    print(f"active CLDR release: {release_id}", flush=True)


if __name__ == "__main__":
    main()
