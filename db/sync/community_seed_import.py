"""Restore a verified, versioned Community Edition world-database seed.

The guided installer uses this command in a short-lived Cloud Run Job.  It replaces the
slow, network-sensitive Wikidata bootstrap with a deterministic download, checksum
verification, pg_restore, and the same application migration/grant checks used by the
live bootstrap.  The dump must contain only public Community data (never ``chat`` or
customer/private schemas).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Sequence

from db.sync._conn import connect
from db.sync.community_bootstrap import (
    BOOTSTRAP_VERSION,
    DEFAULT_DATASETS,
    _LOCK_NAME,
    _ROLE,
    _grant_serving_access,
    _initialize_database,
    _mark,
    _ready,
)


def _download(uri: str, expected_sha256: str) -> str:
    if not uri.startswith("https://"):
        raise ValueError("the Community seed URI must use HTTPS")
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError("the Community seed checksum must be a 64-character SHA-256")

    handle = tempfile.NamedTemporaryFile(prefix="community-seed-", suffix=".dump", delete=False)
    path = handle.name
    digest = hashlib.sha256()
    try:
        with handle, urllib.request.urlopen(uri, timeout=120) as response:  # noqa: S310 - URI is operator-supplied
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                handle.write(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise RuntimeError(
                f"Community seed checksum mismatch: expected {expected}, got {actual}"
            )
        return path
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _restore(path: str) -> None:
    password = os.environ.get("SYNC_PG_PASSWORD") or os.environ.get("KB_PG_PASSWORD")
    if not password:
        raise RuntimeError("SYNC_PG_PASSWORD or KB_PG_PASSWORD is required for seed restore")
    env = os.environ.copy()
    env["PGHOST"] = os.environ.get("SYNC_PG_HOST") or os.environ.get("KB_PG_HOST", "localhost")
    env["PGPORT"] = os.environ.get("SYNC_PG_PORT") or os.environ.get("KB_PG_PORT", "5432")
    env["PGDATABASE"] = os.environ.get("SYNC_PG_DB") or os.environ.get("KB_PG_DB", "world")
    env["PGUSER"] = os.environ.get("SYNC_PG_USER") or os.environ.get("KB_PG_USER", "postgres")
    env["PGPASSWORD"] = password
    if not env["PGHOST"].startswith("/"):
        env["PGSSLMODE"] = os.environ.get("SYNC_PG_SSLMODE") or os.environ.get("KB_PG_SSLMODE", "prefer")
    # A dump made by a newer PostgreSQL client can contain a harmless
    # ``SET transaction_timeout`` preamble that older Cloud SQL PostgreSQL
    # versions do not recognize.  Render the custom dump to SQL, remove only
    # that version-specific statement, and let psql stop on every other error.
    # A failed Cloud Run bootstrap can leave a partial restore in the reusable SQL instance.
    # Clean the objects represented by this immutable seed before replaying it so retries are
    # deterministic instead of failing on an already-created schema or table.
    restore_command: Sequence[str] = (
        "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "--file=-", path,
    )
    psql_command: Sequence[str] = ("psql", "--set=ON_ERROR_STOP=1", "--dbname", env["PGDATABASE"])
    print("bootstrap: pg_restore community seed", flush=True)
    restore = subprocess.Popen(restore_command, stdout=subprocess.PIPE, env=env)
    assert restore.stdout is not None
    psql = subprocess.Popen(psql_command, stdin=subprocess.PIPE, env=env)
    assert psql.stdin is not None
    try:
        for line in restore.stdout:
            stripped = line.strip()
            if (
                stripped == b"SET transaction_timeout = 0;"
                or stripped.startswith(b"\\restrict")
                or stripped.startswith(b"\\unrestrict")
                # Cloud SQL installs serving extensions in the shared public schema.  A
                # retryable pg_restore must leave that schema in place; dropping it would
                # also try to remove extensions such as vector and pg_trgm.
                or stripped in {b"DROP SCHEMA public;", b"CREATE SCHEMA public;"}
            ):
                continue
            psql.stdin.write(line)
        psql.stdin.close()
        psql.stdin = None
        restore_error = restore.wait()
        psql_error = psql.wait()
    finally:
        restore.stdout.close()
        if psql.stdin is not None:
            psql.stdin.close()
        if restore.poll() is None:
            restore.kill()
            restore.wait()
        if psql.poll() is None:
            psql.kill()
            psql.wait()
    if restore_error:
        raise subprocess.CalledProcessError(restore_error, restore_command)
    if psql_error:
        raise subprocess.CalledProcessError(psql_error, psql_command)


def import_seed(connection, role: str, datasets: frozenset[str], uri: str,
                sha256: str, *, force: bool = False) -> bool:
    if not _ROLE.fullmatch(role or "") or role == "postgres":
        raise ValueError("role must be a non-postgres lowercase PostgreSQL identifier")
    _initialize_database(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_NAME,))
    path = None
    try:
        if _ready(connection) and not force:
            print(f"bootstrap: version {BOOTSTRAP_VERSION} is already ready", flush=True)
            return False
        _mark(connection, "running")
        try:
            path = _download(uri, sha256)
            _restore(path)
            # The public dump deliberately excludes customer chat schemas.  Apply the current
            # chat/knowledgebase migrations after restore so the imported public data and the
            # application schema always use the code shipped in this image.
            subprocess.run((sys.executable, "-m", "db.sync.app_migrations"), check=True)
            _grant_serving_access(connection, role, datasets)
        except Exception as exc:
            connection.rollback()
            _mark(connection, "failed", str(exc)[:1000])
            raise
        _mark(connection, "ready")
        print(f"bootstrap: version {BOOTSTRAP_VERSION} ready from {uri}", flush=True)
        return True
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))
        connection.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="serving", help="existing non-superuser serving role")
    parser.add_argument(
        "--datasets", default=",".join(sorted(DEFAULT_DATASETS)),
        help="comma-separated code-approved reference datasets to grant after import",
    )
    parser.add_argument("--seed-uri", default=os.environ.get("COMMUNITY_SEED_URI", ""))
    parser.add_argument("--seed-sha256", default=os.environ.get("COMMUNITY_SEED_SHA256", ""))
    parser.add_argument("--force", action="store_true", help="restore even when the marker is ready")
    args = parser.parse_args()
    if not args.seed_uri or not args.seed_sha256:
        parser.error("--seed-uri and --seed-sha256 (or their environment variables) are required")
    datasets = frozenset(item.strip() for item in args.datasets.split(",") if item.strip())
    connection = connect()
    try:
        import_seed(connection, args.role, datasets, args.seed_uri, args.seed_sha256, force=args.force)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
