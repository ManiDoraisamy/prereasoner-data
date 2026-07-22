"""
training.lib.pg — the Postgres "world" DB connection helper shared by the world-provisioning and gate scripts.

Entirely env-driven (no hardcoded endpoints):
  KB_PG_HOST      host or Cloud SQL unix-socket path (default "localhost"; a path starting with "/" is a socket)
  KB_PG_PORT      port for TCP connections (default 5432)
  KB_PG_DB        database name (default "world")
  KB_PG_USER      user (default "postgres")
  KB_PG_PASSWORD  password (REQUIRED — never hardcoded)
  KB_PG_SSLMODE   sslmode for TCP connections (default "require"; use "disable" for a local dev Postgres)
"""
from __future__ import annotations
import os

import psycopg2
import psycopg2.extensions


def _numeric_to_py(value, cur):
    """NUMERIC -> int when integral else float (keeps aggregates JSON-serializable)."""
    if value is None:
        return None
    f = float(value)
    return int(f) if f.is_integer() else f


psycopg2.extensions.register_type(psycopg2.extensions.new_type((1700,), "NUMERIC2PY", _numeric_to_py))


def _pg():
    """Connect to the Postgres world DB (unix socket when KB_PG_HOST is a path, else TCP + SSL)."""
    host = os.environ.get("KB_PG_HOST", "localhost")
    kw = dict(host=host,
              dbname=os.environ.get("KB_PG_DB", "world"),
              user=os.environ.get("KB_PG_USER", "postgres"),
              password=os.environ["KB_PG_PASSWORD"],
              connect_timeout=30)
    if not host.startswith("/"):
        kw["port"] = int(os.environ.get("KB_PG_PORT", "5432"))
        kw["sslmode"] = os.environ.get("KB_PG_SSLMODE", "require")
    return psycopg2.connect(**kw)
