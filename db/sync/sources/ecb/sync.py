"""Synchronize the ECB's complete published euro reference-rate history."""
from __future__ import annotations

import argparse
import csv
import io
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from db.sync._conn import connect
from db.sync.sources.common import (
    SourceRelease, download_http, insert_rows, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

SOURCE_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
LICENSE_NAME = "ECB reuse policy"
LICENSE_URL = "https://www.ecb.europa.eu/services/copyright/html/index.en.html"


@dataclass(frozen=True)
class EcbData:
    rates: tuple[tuple[str, str, Decimal], ...]

    def counts(self) -> dict[str, int]:
        return {"exchange_rate": len(self.rates)}


def parse_archive(archive: bytes, *, enforce_minimums: bool = True) -> EcbData:
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        csv_names = [name for name in source.namelist() if name.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValueError(f"ECB archive must contain one CSV, found {csv_names!r}")
        rows = csv.reader(io.TextIOWrapper(source.open(csv_names[0]), encoding="utf-8-sig"))
        try:
            header = next(rows)
        except StopIteration as exc:
            raise ValueError("ECB CSV is empty") from exc
        currencies = [value.strip() for value in header[1:] if value.strip()]
        rates = []
        for row in rows:
            if not row or not row[0].strip():
                continue
            effective_date = row[0].strip()
            for currency, value in zip(currencies, row[1:]):
                value = value.strip()
                if not value or value == "N/A":
                    continue
                try:
                    rate = Decimal(value)
                except InvalidOperation as exc:
                    raise ValueError(f"invalid ECB rate {currency} {effective_date}: {value!r}") from exc
                rates.append((effective_date, currency, rate))
    result = EcbData(tuple(rates))
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: EcbData, *, enforce_minimums: bool = True) -> None:
    keys = [(date, currency) for date, currency, _ in data.rates]
    if len(keys) != len(set(keys)):
        raise ValueError("ECB exchange-rate keys are not unique")
    if any(rate <= 0 for _, _, rate in data.rates):
        raise ValueError("ECB exchange rates must be positive")
    if enforce_minimums and len(data.rates) < 100_000:
        raise ValueError(f"ECB history is unexpectedly small: {len(data.rates)}")


DDL = """
CREATE TABLE IF NOT EXISTS ecb.exchange_rate (
  release_id text NOT NULL REFERENCES ecb.release(release_id),
  effective_date date NOT NULL,
  quote_currency text NOT NULL CHECK (quote_currency ~ '^[A-Z]{3}$'),
  units_per_eur numeric NOT NULL CHECK (units_per_eur > 0),
  PRIMARY KEY (release_id, effective_date, quote_currency)
);
"""


def synchronize(conn, version: str, archive: bytes, data: EcbData) -> str:
    release = SourceRelease(
        schema="ecb", version=version, source_url=SOURCE_URL,
        content_sha256=sha256_bytes(archive), completeness="full_source_artifact",
        import_scope={"artifact": "eurofxref-hist.csv", "base_currency": "EUR"},
        license_name=LICENSE_NAME, license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "ecb", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows(cur, "ecb.exchange_rate",
                    ("release_id", "effective_date", "quote_currency", "units_per_eur"),
                    ((release.release_id, *row) for row in data.rates))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive")
    parser.add_argument("--version", help="snapshot label; defaults to newest date in the file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    archive = open(args.archive, "rb").read() if args.archive else download_http(SOURCE_URL)
    data = parse_archive(archive)
    version = args.version or max(row[0] for row in data.rates)
    print(f"validated ECB {version}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(archive)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, version, archive, data)
    finally:
        conn.close()
    print(f"active ECB release: {release_id}")


if __name__ == "__main__":
    main()
