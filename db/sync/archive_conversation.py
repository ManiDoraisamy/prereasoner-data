#!/usr/bin/env python3
"""archive_conversation.py — serialize an inactive conversation's Postgres schema to GCS, and
restore it back. A conversation's data lives entirely in its own schema (named by its
conversation_id, `c_<32 hex>`; see engine/conversations.py), so the schema IS the archival unit.

    archive:  pg_dump --schema=<conversation_id>  →  gzip  →  gs://$GCS_BUCKET/conversations/<id>.sql.gz
              then (optionally) DROP SCHEMA to free the live database.
    restore:  gs://…/<id>.sql.gz  →  gunzip  →  psql  →  the schema exists again in Postgres.

The `chat` metadata (chat.conversation / chat.user_conversation) is NOT dropped on archive — the
conversation stays listable and re-openable; restore re-materializes its data schema on demand.

Env: WORLD_PG_HOST/PORT/DB/USER/PASSWORD (same as the engine; see .env.example) and
GCS_BUCKET. Requires pg_dump + psql on PATH, and either the `google-cloud-storage` package or
`gsutil`. Standalone (no engine import), like the rest of db/sync.

    python db/sync/archive_conversation.py archive c_<32hex> [--drop]
    python db/sync/archive_conversation.py restore c_<32hex>
    python db/sync/archive_conversation.py list

NOTE: this is the "later" archival capability from the design — written and self-consistent, but
not yet exercised end-to-end against a live GCS bucket. Smoke-test in a scratch project first.
"""
from __future__ import annotations
import gzip
import os
import re
import subprocess
import sys
import tempfile

_ID_RE = re.compile(r"^c_[0-9a-f]{32}$")


def _env(k, default=None, required=False):
    v = os.environ.get(k, default)
    if required and not v:
        sys.exit(f"missing required env var {k}")
    return v


def _pg_env():
    """A libpq environment for pg_dump/psql from the WORLD_PG_* vars (no password on the CLI)."""
    host = _env("WORLD_PG_HOST", "localhost")
    e = dict(os.environ)
    e["PGHOST"] = host
    e["PGDATABASE"] = _env("WORLD_PG_DB", "world")
    e["PGUSER"] = _env("WORLD_PG_USER", "postgres")
    e["PGPASSWORD"] = _env("WORLD_PG_PASSWORD", "", required=True)
    if not host.startswith("/"):                              # unix socket (Cloud SQL) needs no port/ssl
        e["PGPORT"] = str(_env("WORLD_PG_PORT", "5432"))
        e["PGSSLMODE"] = _env("WORLD_PG_SSLMODE", "prefer")
    return e


def _gcs_path(cid):
    return f"gs://{_env('GCS_BUCKET', required=True)}/conversations/{cid}.sql.gz"


def _gcs_upload(local, gcs_uri):
    try:
        from google.cloud import storage                     # preferred: the client library
        bucket, _, blob = gcs_uri[len("gs://"):].partition("/")
        storage.Client().bucket(bucket).blob(blob).upload_from_filename(local)
    except ImportError:
        subprocess.run(["gsutil", "cp", local, gcs_uri], check=True)   # fallback: gsutil


def _gcs_download(gcs_uri, local):
    try:
        from google.cloud import storage
        bucket, _, blob = gcs_uri[len("gs://"):].partition("/")
        storage.Client().bucket(bucket).blob(blob).download_to_filename(local)
    except ImportError:
        subprocess.run(["gsutil", "cp", gcs_uri, local], check=True)


def archive(cid, drop=False):
    if not _ID_RE.match(cid):
        sys.exit(f"not a conversation schema id: {cid}")
    env = _pg_env()
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "dump.sql")
        gz = os.path.join(td, "dump.sql.gz")
        # plain-SQL dump of just this conversation's schema (portable; restore with psql).
        subprocess.run(["pg_dump", "--schema=" + cid, "--no-owner", "--no-privileges", "-f", raw],
                       env=env, check=True)
        with open(raw, "rb") as fi, gzip.open(gz, "wb") as fo:
            fo.writelines(fi)
        uri = _gcs_path(cid)
        _gcs_upload(gz, uri)
        print(f"archived {cid} -> {uri}")
        if drop:
            subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-c", f'DROP SCHEMA IF EXISTS "{cid}" CASCADE'],
                           env=env, check=True)
            print(f"dropped live schema {cid} (freed; restore re-materializes it)")


def restore(cid):
    if not _ID_RE.match(cid):
        sys.exit(f"not a conversation schema id: {cid}")
    env = _pg_env()
    with tempfile.TemporaryDirectory() as td:
        gz = os.path.join(td, "dump.sql.gz")
        raw = os.path.join(td, "dump.sql")
        _gcs_download(_gcs_path(cid), gz)
        with gzip.open(gz, "rb") as fi, open(raw, "wb") as fo:
            fo.writelines(fi)
        subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-f", raw], env=env, check=True)
        print(f"restored {cid} from {_gcs_path(cid)}")


def list_archived():
    prefix = f"gs://{_env('GCS_BUCKET', required=True)}/conversations/"
    try:
        subprocess.run(["gsutil", "ls", prefix], check=False)
    except FileNotFoundError:
        from google.cloud import storage
        bucket = _env("GCS_BUCKET", required=True)
        for b in storage.Client().list_blobs(bucket, prefix="conversations/"):
            print(f"gs://{bucket}/{b.name}")


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "archive":
        archive(argv[2], drop="--drop" in argv[3:])
    elif cmd == "restore":
        restore(argv[2])
    elif cmd == "list":
        list_archived()
    else:
        sys.exit(f"unknown command {cmd!r} (archive | restore | list)")


if __name__ == "__main__":
    main(sys.argv)
