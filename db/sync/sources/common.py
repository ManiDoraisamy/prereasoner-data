"""Shared helpers for immutable source-release synchronization."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from urllib.parse import urlsplit
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import islice

import requests
from psycopg2 import sql
from psycopg2.extras import execute_values

USER_AGENT = "prereasoner-source-sync/1.0 (https://github.com/ManiDoraisamy/prereasoner-data)"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class SourceRelease:
    """Identity and policy recorded beside every immutable source snapshot."""

    schema: str
    version: str
    source_url: str
    content_sha256: str
    completeness: str
    import_scope: dict[str, object]
    license_name: str
    license_url: str
    schema_version: int = 1

    @property
    def release_id(self) -> str:
        return f"{self.version}+sha256:{self.content_sha256}"


def _require_https(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"source download must use an absolute HTTPS URL: {url!r}")
    return url


def download(url: str, timeout: int = 120) -> bytes:
    url = _require_https(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_http(url: str, timeout: int = 120) -> bytes:
    """Download through requests for hosts whose TLS chain urllib cannot validate locally."""
    url = _require_https(url)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response.content


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def insert_rows(cur, qualified_table: str, columns: Sequence[str], rows: Iterable[Sequence],
                page_size: int = 5000) -> int:
    materialized = list(rows)
    if not materialized:
        return 0
    column_sql = ", ".join(f'"{column}"' for column in columns)
    execute_values(cur, f"INSERT INTO {qualified_table} ({column_sql}) VALUES %s",
                   materialized, page_size=page_size)
    return len(materialized)


def insert_rows_batched(cur, qualified_table: str, columns: Sequence[str],
                        rows: Iterable[Sequence], page_size: int = 5000) -> int:
    """Insert a large iterable without duplicating the complete source in memory."""
    iterator = iter(rows)
    inserted = 0
    while batch := list(islice(iterator, page_size)):
        inserted += insert_rows(cur, qualified_table, columns, batch, page_size=page_size)
    return inserted


def ensure_release_table(cur, schema_name: str) -> None:
    """Create the common release ledger in a validated source-owned schema."""
    if not _IDENTIFIER.fullmatch(schema_name):
        raise ValueError(f"unsafe source schema name: {schema_name!r}")
    schema = sql.Identifier(schema_name)
    cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
    cur.execute(sql.SQL("""
        CREATE TABLE IF NOT EXISTS {}.release (
          release_id text PRIMARY KEY,
          source_version text NOT NULL,
          source_url text NOT NULL,
          content_sha256 text NOT NULL,
          schema_version integer NOT NULL,
          completeness text NOT NULL CHECK (completeness IN (
            'full_source_artifact', 'full_declared_scope', 'bounded_snapshot'
          )),
          import_scope jsonb NOT NULL,
          license_name text NOT NULL,
          license_url text NOT NULL,
          table_counts jsonb NOT NULL,
          materialized_at timestamptz NOT NULL DEFAULT now(),
          status text NOT NULL CHECK (status IN ('staged', 'active', 'retired', 'rejected')),
          UNIQUE (source_version, content_sha256)
        )
    """).format(schema))
    cur.execute(sql.SQL("""
        CREATE UNIQUE INDEX IF NOT EXISTS {} ON {}.release ((status))
        WHERE status = 'active'
    """).format(sql.Identifier(f"ux_{schema_name}_release_active"), schema))


def prepare_source(cur, schema_name: str, ddl: str) -> None:
    """Create/migrate source tables before checking whether source content is unchanged."""
    ensure_release_table(cur, schema_name)
    cur.execute(ddl)


def stage_release(cur, release: SourceRelease, table_counts: dict[str, int]) -> bool:
    """Stage a release, returning False when the exact active release already exists."""
    ensure_release_table(cur, release.schema)
    schema = sql.Identifier(release.schema)
    cur.execute(
        sql.SQL(
            "SELECT status, table_counts, schema_version FROM {}.release WHERE release_id=%s"
        ).format(schema),
        (release.release_id,),
    )
    existing = cur.fetchone()
    if existing:
        if int(existing[2]) != release.schema_version:
            raise ValueError(
                f"{release.schema} release schema version {existing[2]} does not match "
                f"required version {release.schema_version}; run db.sync.migrations"
            )
        if existing[0] == "active" and existing[1] == table_counts:
            return False
        if existing[0] == "active":
            raise ValueError(f"active {release.schema} release counts do not match parsed source")
        raise ValueError(
            f"{release.schema} release already exists with status {existing[0]!r}"
        )
    cur.execute(sql.SQL("""
        INSERT INTO {}.release
          (release_id, source_version, source_url, content_sha256, schema_version,
           completeness, import_scope, license_name, license_url, table_counts, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,'staged')
    """).format(schema), (
        release.release_id, release.version, release.source_url, release.content_sha256,
        release.schema_version, release.completeness,
        json.dumps(release.import_scope, sort_keys=True), release.license_name,
        release.license_url, json.dumps(table_counts, sort_keys=True),
    ))
    return True


def verify_and_activate(cur, release: SourceRelease, table_counts: dict[str, int]) -> None:
    """Verify physical row counts, then atomically make the staged release active."""
    schema = sql.Identifier(release.schema)
    for table_name, expected in table_counts.items():
        if not _IDENTIFIER.fullmatch(table_name):
            raise ValueError(f"unsafe source table name: {table_name!r}")
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.{} WHERE release_id=%s").format(
                schema, sql.Identifier(table_name)
            ),
            (release.release_id,),
        )
        actual = cur.fetchone()[0]
        if actual != expected:
            raise ValueError(
                f"{release.schema}.{table_name}: loaded {actual}, expected {expected}"
            )
    cur.execute(sql.SQL("UPDATE {}.release SET status='retired' WHERE status='active'").format(schema))
    cur.execute(
        sql.SQL("UPDATE {}.release SET status='active' WHERE release_id=%s").format(schema),
        (release.release_id,),
    )
