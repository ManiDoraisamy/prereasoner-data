"""Shared Postgres connection helper for the db/sync scripts.

Standalone on purpose — db/ must bootstrap a fresh database without importing
the engine package. Connection parameters come exclusively from env vars:

  WORLD_PG_HOST      host, or a "/cloudsql/..." unix-socket path  (default: localhost)
  WORLD_PG_PORT      port                                          (default: 5432)
  WORLD_PG_DB        database name                                 (default: world)
  WORLD_PG_USER      role                                          (default: postgres)
  WORLD_PG_PASSWORD  password                                      (required)
  WORLD_PG_SSLMODE   libpq sslmode                                 (default: prefer)
                     use "require" for a Cloud SQL public IP; "prefer" works for
                     both local docker (no SSL) and SSL-enabled servers.
"""
from __future__ import annotations
import os

import psycopg2


def connect():
    host = os.environ.get("WORLD_PG_HOST", "localhost")
    kw = dict(host=host,
              dbname=os.environ.get("WORLD_PG_DB", "world"),
              user=os.environ.get("WORLD_PG_USER", "postgres"),
              password=os.environ["WORLD_PG_PASSWORD"],
              connect_timeout=30)
    if not host.startswith("/"):          # TCP; a "/cloudsql/..." unix socket takes no port/sslmode
        kw["port"] = int(os.environ.get("WORLD_PG_PORT", "5432"))
        kw["sslmode"] = os.environ.get("WORLD_PG_SSLMODE", "prefer")
    return psycopg2.connect(**kw)
