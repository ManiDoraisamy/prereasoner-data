"""Synchronize labels for QID-valued properties in the capped Wikidata corpus.

The capped entity snapshot stores references as QIDs. A named property is valid training
evidence only when the encoder can see a human-readable value, so this job materializes a
bounded, immutable label snapshot in the source-owned ``wikidata`` schema. It never mutates
``capped.*`` and never fetches data during serving or corpus generation.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from db.sync._conn import connect
from db.sync.sources.common import (
    USER_AGENT,
    SourceRelease,
    insert_rows,
    prepare_source,
    sha256_bytes,
    stage_release,
    verify_and_activate,
)

API_URL = "https://www.wikidata.org/w/api.php"
LICENSE_NAME = "Creative Commons CC0 1.0"
LICENSE_URL = "https://www.wikidata.org/wiki/Wikidata:Licensing"
DEFAULT_PROPERTY_IDS = ("P50", "P144", "P162", "P178", "P1830")
MAX_REQUEST_QIDS = 50_000
_QID = re.compile(r"^Q[1-9][0-9]*$")
_PID = re.compile(r"^P[1-9][0-9]*$")


@dataclass(frozen=True)
class WikidataLabels:
    property_ids: tuple[str, ...]
    requested_qids: tuple[str, ...]
    rows: tuple[tuple[str, str, str, str, int], ...]
    missing_qids: tuple[str, ...]

    def counts(self) -> dict[str, int]:
        return {"entity_label": len(self.rows)}


def _entity_records(payload: dict) -> list[dict]:
    entities = payload.get("entities")
    if isinstance(entities, dict):
        return list(entities.values())
    if isinstance(entities, list):
        return entities
    raise TypeError("Wikidata response has no entities collection")


def _requested_qid(entity: dict) -> str:
    canonical = str(entity.get("id", ""))
    redirect = entity.get("redirects")
    if not isinstance(redirect, dict):
        return canonical
    source = str(redirect.get("from", ""))
    target = str(redirect.get("to", ""))
    if not _QID.fullmatch(source) or target != canonical:
        raise ValueError(f"invalid Wikidata redirect {redirect!r} for {canonical!r}")
    return source


def collect_reference_qids(cur, property_ids=DEFAULT_PROPERTY_IDS,
                           *, limit: int = MAX_REQUEST_QIDS) -> tuple[str, ...]:
    """Collect the bounded QID universe used by the audited Schema.org mappings."""
    properties = tuple(sorted(set(property_ids)))
    if not properties or any(not _PID.fullmatch(pid) for pid in properties):
        raise ValueError("Wikidata property IDs must be non-empty canonical P-ids")
    cur.execute(
        "SELECT properties FROM capped.entity WHERE properties ?| %s::text[] ORDER BY qid",
        (list(properties),),
    )
    qids: set[str] = set()
    for (claims,) in cur:
        for pid in properties:
            for value in (claims or {}).get(pid, ()) or ():
                qid = str(value)
                if _QID.fullmatch(qid):
                    qids.add(qid)
                    if len(qids) > limit:
                        raise ValueError(
                            f"Wikidata reference-label scope exceeds the {limit:,}-QID safety bound"
                        )
    return tuple(sorted(qids, key=lambda value: int(value[1:])))


def _request_batch(session: requests.Session, qids: tuple[str, ...], *, retries: int = 5) -> list[dict]:
    params = {
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "info|labels",
        "languages": "en",
        "format": "json",
        "formatversion": "2",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(API_URL, params=params, timeout=90)
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(f"Wikidata API returned {response.status_code}", response=response)
            response.raise_for_status()
            payload = response.json()
            entities = _entity_records(payload)
            returned = {_requested_qid(entity) for entity in entities}
            if returned != set(qids):
                missing = sorted(set(qids) - returned)
                extra = sorted(returned - set(qids))
                raise ValueError(
                    f"Wikidata response QIDs do not match the requested batch; "
                    f"missing={missing[:5]} extra={extra[:5]}"
                )
            return entities
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 == retries:
                break
            retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
            delay = min(float(retry_after or 0), 30.0) or min(30.0, 2.0 ** attempt)
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def fetch_snapshot(qids: tuple[str, ...], *, property_ids=DEFAULT_PROPERTY_IDS,
                   batch_size: int = 50, delay_seconds: float = 0.05) -> bytes:
    if not qids or len(qids) > MAX_REQUEST_QIDS:
        raise ValueError(f"Wikidata label snapshot requires 1..{MAX_REQUEST_QIDS:,} QIDs")
    if not 1 <= batch_size <= 50:
        raise ValueError("Wikidata batch_size must be between 1 and 50")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    entities = []
    try:
        for offset in range(0, len(qids), batch_size):
            batch = qids[offset:offset + batch_size]
            entities.extend(_request_batch(session, batch))
            if delay_seconds and offset + batch_size < len(qids):
                time.sleep(delay_seconds)
    finally:
        session.close()
    payload = {
        "schema_version": 1,
        "property_ids": sorted(set(property_ids)),
        "requested_qids": list(qids),
        "entities": sorted(entities, key=lambda entity: int(_requested_qid(entity)[1:])),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def parse_snapshot(snapshot: bytes, *, minimum_resolution: float = 0.95) -> WikidataLabels:
    payload = json.loads(snapshot)
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported Wikidata label snapshot schema")
    property_ids = tuple(payload.get("property_ids") or ())
    requested = tuple(payload.get("requested_qids") or ())
    if (not requested or len(requested) != len(set(requested))
            or any(not _QID.fullmatch(qid) for qid in requested)):
        raise ValueError("Wikidata snapshot has invalid or duplicate requested QIDs")
    if not property_ids or any(not _PID.fullmatch(pid) for pid in property_ids):
        raise ValueError("Wikidata snapshot has invalid property scope")

    rows = []
    missing = []
    seen = set()
    for entity in _entity_records(payload):
        qid = _requested_qid(entity)
        canonical_qid = str(entity.get("id", ""))
        if qid not in requested or qid in seen:
            raise ValueError(f"Wikidata snapshot has foreign or duplicate entity {qid!r}")
        seen.add(qid)
        label = ((entity.get("labels") or {}).get("en") or {}).get("value")
        if "missing" in entity or not isinstance(label, str) or not label.strip():
            missing.append(qid)
            continue
        revision = entity.get("lastrevid")
        if not isinstance(revision, int) or revision <= 0:
            raise ValueError(f"Wikidata entity {qid} has no positive last revision")
        rows.append((qid, canonical_qid, label.strip(), "en", revision))
    if seen != set(requested):
        raise ValueError("Wikidata snapshot does not account for every requested QID")
    resolution = len(rows) / len(requested)
    if resolution < minimum_resolution:
        raise ValueError(
            f"Wikidata label resolution {resolution:.1%} is below {minimum_resolution:.1%}"
        )
    return WikidataLabels(
        tuple(sorted(set(property_ids))), requested, tuple(sorted(rows)), tuple(sorted(missing)),
    )


DDL = """
CREATE TABLE IF NOT EXISTS wikidata.entity_label (
  release_id text NOT NULL REFERENCES wikidata.release(release_id),
  qid text NOT NULL CHECK (qid ~ '^Q[1-9][0-9]*$'),
  canonical_qid text NOT NULL CHECK (canonical_qid ~ '^Q[1-9][0-9]*$'),
  label text NOT NULL,
  language text NOT NULL CHECK (language = 'en'),
  source_revision bigint NOT NULL CHECK (source_revision > 0),
  PRIMARY KEY (release_id, qid, language)
);
"""


def synchronize(conn, snapshot: bytes, data: WikidataLabels) -> str:
    requested_hash = sha256_bytes("\n".join(data.requested_qids).encode())
    release = SourceRelease(
        schema="wikidata",
        version="wbgetentities-live",
        source_url=API_URL,
        content_sha256=sha256_bytes(snapshot),
        completeness="bounded_snapshot",
        import_scope={
            "property_ids": list(data.property_ids),
            "requested_qids": len(data.requested_qids),
            "requested_qids_sha256": requested_hash,
            "resolved_qids": len(data.rows),
            "missing_qids": len(data.missing_qids),
            "language": "en",
        },
        license_name=LICENSE_NAME,
        license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "wikidata", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows(
            cur,
            "wikidata.entity_label",
            ("release_id", "qid", "canonical_qid", "label", "language", "source_revision"),
            ((release.release_id, *row) for row in data.rows),
        )
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", help="reuse a previously captured JSON snapshot")
    parser.add_argument("--write-snapshot", help="write the exact fetched JSON before import")
    parser.add_argument("--property", action="append", dest="properties")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    properties = tuple(sorted(set(args.properties or DEFAULT_PROPERTY_IDS)))

    if args.snapshot:
        snapshot = Path(args.snapshot).read_bytes()
    else:
        connection = connect()
        try:
            qids = collect_reference_qids(connection.cursor(), properties)
        finally:
            connection.close()
        print(f"Wikidata label scope: {len(qids):,} QIDs for {properties}", flush=True)
        snapshot = fetch_snapshot(qids, property_ids=properties)
    if args.write_snapshot:
        Path(args.write_snapshot).write_bytes(snapshot)
    data = parse_snapshot(snapshot)
    print(
        f"validated Wikidata labels: {len(data.rows):,}/{len(data.requested_qids):,} resolved",
        flush=True,
    )
    if args.dry_run:
        print(f"sha256:{sha256_bytes(snapshot)}")
        return
    connection = connect()
    try:
        release_id = synchronize(connection, snapshot, data)
    finally:
        connection.close()
    print(f"active Wikidata label release: {release_id}")


if __name__ == "__main__":
    main()
