"""Synchronize one licensed WHO ICD-11 MMS release through the official ICD API."""
from __future__ import annotations

import argparse
import json
import os
from collections import deque
from dataclasses import dataclass

import requests

from db.sync._conn import connect
from db.sync.sources.common import (
    SourceRelease, USER_AGENT, insert_rows_batched, prepare_source, sha256_bytes, stage_release,
    verify_and_activate,
)

TOKEN_URL = "https://icdaccessmanagement.who.int/connect/token"
API_ROOT = "https://id.who.int"
LICENSE_NAME = "CC BY-ND 3.0 IGO"
LICENSE_URL = "https://icd.who.int/docs/icd-api/license/"


@dataclass(frozen=True)
class WhoData:
    entities: tuple[tuple, ...]

    def counts(self) -> dict[str, int]:
        return {"icd11_mms_entity": len(self.entities)}


def _display(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("@value", value.get("value", "")))
    return ""


def fetch_snapshot(release_version: str, client_id: str, client_secret: str,
                   language: str = "en") -> bytes:
    token_response = requests.post(
        TOKEN_URL, auth=(client_id, client_secret), timeout=60,
        data={"grant_type": "client_credentials", "scope": "icdapi_access"},
        headers={"User-Agent": USER_AGENT},
    )
    token_response.raise_for_status()
    token = token_response.json()["access_token"]
    headers = {
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "Accept-Language": language, "API-Version": "v2", "User-Agent": USER_AGENT,
    }
    root_uri = f"{API_ROOT}/icd/release/11/{release_version}/mms"
    queue = deque([root_uri])
    documents: dict[str, dict] = {}
    while queue:
        uri = queue.popleft()
        canonical = uri.replace("http://", "https://", 1)
        if canonical in documents:
            continue
        response = requests.get(canonical, headers=headers, timeout=90)
        response.raise_for_status()
        document = response.json()
        documents[canonical] = document
        queue.extend(
            str(child).replace("http://", "https://", 1)
            for child in document.get("child", [])
        )
    payload = {
        "release_version": release_version, "language": language, "root_uri": root_uri,
        "entities": [documents[key] for key in sorted(documents)],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def parse_snapshot(snapshot: bytes, *, enforce_minimums: bool = True) -> WhoData:
    payload = json.loads(snapshot)
    documents = payload["entities"]
    known = {
        str(row.get("@id", "")).replace("http://", "https://", 1): row
        for row in documents
    }
    depths: dict[str, int] = {}

    def depth(uri: str, seen: frozenset[str] = frozenset()) -> int:
        if uri in depths:
            return depths[uri]
        if uri in seen:
            raise ValueError("WHO ICD-11 hierarchy contains a parent cycle")
        row = known[uri]
        parents = [str(parent).replace("http://", "https://", 1)
                   for parent in row.get("parent", []) if str(parent).replace("http://", "https://", 1) in known]
        result = 0 if not parents else min(depth(parent, seen | {uri}) for parent in parents) + 1
        depths[uri] = result
        return result

    entities = []
    for uri in sorted(known):
        row = known[uri]
        parents = [str(parent).replace("http://", "https://", 1) for parent in row.get("parent", [])]
        children = row.get("child", [])
        entities.append((
            uri, str(row.get("code", "")), _display(row.get("title")),
            _display(row.get("definition")), str(row.get("classKind", "")),
            parents[0] if parents else "", depth(uri), not children,
            json.dumps(row, sort_keys=True, ensure_ascii=True),
        ))
    result = WhoData(tuple(entities))
    validate(result, enforce_minimums=enforce_minimums)
    return result


def validate(data: WhoData, *, enforce_minimums: bool = True) -> None:
    ids = [row[0] for row in data.entities]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("WHO ICD-11 entity URIs must be present and unique")
    if enforce_minimums and len(data.entities) < 20_000:
        raise ValueError(f"WHO ICD-11 MMS release is unexpectedly small: {len(data.entities)}")


DDL = """
CREATE TABLE IF NOT EXISTS who.icd11_mms_entity (
  release_id text NOT NULL REFERENCES who.release(release_id),
  entity_uri text NOT NULL, code text NOT NULL, title text NOT NULL, definition text NOT NULL,
  class_kind text NOT NULL, parent_uri text NOT NULL, depth integer NOT NULL,
  is_leaf boolean NOT NULL, source_document jsonb NOT NULL,
  PRIMARY KEY (release_id, entity_uri)
);
CREATE INDEX IF NOT EXISTS ix_who_icd11_mms_code
  ON who.icd11_mms_entity (code) WHERE code <> '';
"""


def synchronize(conn, release_version: str, language: str, snapshot: bytes,
                data: WhoData) -> str:
    release = SourceRelease(
        schema="who", version=f"icd11-mms-{release_version}-{language}",
        source_url=f"{API_ROOT}/icd/release/11/{release_version}/mms",
        content_sha256=sha256_bytes(snapshot), completeness="full_declared_scope",
        import_scope={
            "classification": "ICD-11 MMS", "release": release_version,
            "language": language, "rights": "no adaptation of WHO content",
        }, license_name=LICENSE_NAME, license_url=LICENSE_URL,
    )
    cur = conn.cursor()
    try:
        prepare_source(cur, "who", DDL)
        counts = data.counts()
        if not stage_release(cur, release, counts):
            conn.commit()
            return release.release_id
        insert_rows_batched(cur, "who.icd11_mms_entity", (
            "release_id", "entity_uri", "code", "title", "definition", "class_kind",
            "parent_uri", "depth", "is_leaf", "source_document",
        ), ((release.release_id, *row) for row in data.entities))
        verify_and_activate(cur, release, counts)
        conn.commit()
        return release.release_id
    except Exception:
        conn.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, help="WHO ICD-11 release id, e.g. 2025-01")
    parser.add_argument("--language", default="en")
    parser.add_argument("--snapshot")
    parser.add_argument("--write-snapshot")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.snapshot:
        snapshot = open(args.snapshot, "rb").read()
    else:
        client_id = os.environ.get("WHO_ICD_CLIENT_ID")
        client_secret = os.environ.get("WHO_ICD_CLIENT_SECRET")
        if not client_id or not client_secret:
            parser.error(
                "WHO_ICD_CLIENT_ID and WHO_ICD_CLIENT_SECRET are required; register at "
                "https://icd.who.int/icdapi"
            )
        snapshot = fetch_snapshot(args.release, client_id, client_secret, args.language)
    if args.write_snapshot:
        with open(args.write_snapshot, "wb") as target:
            target.write(snapshot)
    data = parse_snapshot(snapshot)
    print(f"validated WHO ICD-11 MMS {args.release}/{args.language}: {data.counts()}", flush=True)
    if args.dry_run:
        print(f"sha256:{sha256_bytes(snapshot)}")
        return
    conn = connect()
    try:
        release_id = synchronize(conn, args.release, args.language, snapshot, data)
    finally:
        conn.close()
    print(f"active WHO release: {release_id}")


if __name__ == "__main__":
    main()
