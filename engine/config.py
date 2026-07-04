"""Central configuration — the ONE place the engine reads environment variables.

Every knob is an env var so the same image runs locally (docker-compose Postgres, no RTDB) and on Cloud Run
(Cloud SQL + Firebase). Values that must be able to change between test runs (AUTH_TEST_SUB) or that are
security-sensitive (WORLD_PG_PASSWORD) are read at call time via the helper functions; everything else is
resolved once at import.

Env contract:
  HOST                 bind address for the HTTP server              (default 0.0.0.0)
  PORT                 port for the HTTP server                      (default 8080)
  WORLD_PG_HOST        Postgres host (or a unix-socket dir path)     (default localhost)
  WORLD_PG_PORT        Postgres port                                 (default 5432)
  WORLD_PG_DB          Postgres database name                        (default world)
  WORLD_PG_USER        Postgres user                                 (default postgres)
  WORLD_PG_PASSWORD    Postgres password — REQUIRED when connecting  (no default)
  WORLD_PG_SSLMODE     libpq sslmode for TCP connections             (default prefer)
  RTDB_URL             Firebase RTDB url for live trace streaming — OPTIONAL. Unset => streaming is a
                       clean no-op; the HTTP response still carries the full JSON answer.
  AUTH_TEST_SUB        TEST-ONLY auth bypass: a fixed principal, skips Firebase token verification.
  PREREASONER_DATA_DIR model/data directory                          (default: engine/data in the package)
  DEVICE               torch device for the encoder                  (default cpu)
  BASE_MODEL_ID        Hugging Face id of the base encoder LM        (default Qwen/Qwen2.5-0.5B)
  WORLD_MODEL_ROUTE    "0" disables model-driven column routing (falls back to value membership; default on)
"""
from __future__ import annotations
import os
from pathlib import Path

# ---------- serving ----------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

# ---------- world Postgres ----------
WORLD_PG_HOST = os.environ.get("WORLD_PG_HOST", "localhost")
WORLD_PG_PORT = int(os.environ.get("WORLD_PG_PORT", "5432"))
WORLD_PG_DB = os.environ.get("WORLD_PG_DB", "world")
WORLD_PG_USER = os.environ.get("WORLD_PG_USER", "postgres")
WORLD_PG_SSLMODE = os.environ.get("WORLD_PG_SSLMODE", "prefer")


def world_pg_password():
    """The Postgres password, read at connect time. REQUIRED for any world-DB path; a clear error beats a
    psycopg2 auth stack trace."""
    pw = os.environ.get("WORLD_PG_PASSWORD")
    if not pw:
        raise RuntimeError("WORLD_PG_PASSWORD is not set — the world Postgres connection needs it")
    return pw


# ---------- Firebase RTDB (trace streaming) — OPTIONAL ----------
RTDB_URL = os.environ.get("RTDB_URL") or None


def auth_test_sub():
    """TEST-ONLY bypass: when AUTH_TEST_SUB is set, auth returns this fixed principal without verifying a
    token. Read at call time so a test can set it before spawning the server."""
    return os.environ.get("AUTH_TEST_SUB") or None


# ---------- model / data ----------
DATA_DIR = Path(os.environ.get("PREREASONER_DATA_DIR") or Path(__file__).resolve().parent / "data")
DEVICE = os.environ.get("DEVICE", "cpu")
BASE_MODEL_ID = os.environ.get("BASE_MODEL_ID", "Qwen/Qwen2.5-0.5B")


def world_model_route_enabled():
    """Model-driven column routing (the trained router types columns). "0" falls back to pure value
    membership so the live demo can never hard-break on a model regression."""
    return os.environ.get("WORLD_MODEL_ROUTE", "1") != "0"
