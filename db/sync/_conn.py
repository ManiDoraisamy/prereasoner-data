"""Shared Postgres connection helper for the db/sync scripts.

Standalone on purpose — db/ must bootstrap a fresh database without importing
the engine package. Connection parameters come exclusively from env vars:

  SYNC_PG_*       privileged sync-job connection settings
  KB_PG_*         fallback settings for local development

Production sync jobs should set SYNC_PG_USER/SYNC_PG_PASSWORD so serving can use a
separate non-superuser KB_PG_USER. Host, port, database, and sslmode follow the same
override-then-fallback rule.
                     use "require" for a Cloud SQL public IP; "prefer" works for
                     both local docker (no SSL) and SSL-enabled servers.
"""
from __future__ import annotations
import os

import psycopg2


def _setting(name: str, default=None):
    return os.environ.get(f"SYNC_PG_{name}", os.environ.get(f"KB_PG_{name}", default))


def _connection_kwargs() -> dict:
    host = _setting("HOST", "localhost")
    kw = dict(host=host,
              dbname=_setting("DB", "world"),
              user=_setting("USER", "postgres"),
              password=_setting("PASSWORD"),
              connect_timeout=30)
    if not kw["password"]:
        raise ValueError("SYNC_PG_PASSWORD or KB_PG_PASSWORD is required")
    if not host.startswith("/"):          # TCP; a "/cloudsql/..." unix socket takes no port/sslmode
        kw["port"] = int(_setting("PORT", "5432"))
        kw["sslmode"] = _setting("SSLMODE", "prefer")
    return kw


def connect():
    return psycopg2.connect(**_connection_kwargs())
