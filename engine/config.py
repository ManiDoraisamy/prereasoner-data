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

  --- MCP layer (v1: the Sonnet orchestrator + PreReasoner MCP server; see mcp-now.md) ---
  ANTHROPIC_API_KEY    Anthropic key for the Sonnet orchestrator — REQUIRED to run the chat backend (no default)
  ANTHROPIC_MODEL      Sonnet model id for the orchestrator          (default claude-sonnet-5)
  ENGINE_BASE_URL      where the MCP server reaches this engine over HTTP (default http://127.0.0.1:$PORT)
  ORCH_HOST            bind address for the orchestrator chat server (default 0.0.0.0)
  ORCH_PORT            port for the orchestrator chat server          (default 8090)
  MCP_SERVER_CMD       argv (JSON list) to launch the PreReasoner MCP server (default: python -m mcp_server.server)
  ENGINE_BEARER_TOKEN  read by the MCP SERVER only: the Firebase token to forward to the engine. The
                       orchestrator sets this per session in the MCP server's env; unset locally (the engine
                       runs with AUTH_TEST_SUB). Never a request field the LLM sees.
"""
from __future__ import annotations
import os
from pathlib import Path


def _autoload_dotenv():
    """Local-dev convenience: load the repo .env so EVERY process (engine, orchestrator, MCP server,
    tests) reads one config file — run anything with `python -m ...` and it just works. Uses setdefault,
    so real environment variables (Cloud Run, or the orchestrator's per-subprocess overrides) always win.
    Prod has no .env file, so this is a no-op there."""
    p = Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_autoload_dotenv()

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


# ---------- MCP layer: Sonnet orchestrator + PreReasoner MCP server (mcp-now.md) ----------
# Static knobs resolve at import (mirrors BASE_MODEL_ID); the security-sensitive key is call-time
# (mirrors world_pg_password) so a clear error beats an anthropic auth stack trace.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
# The engine's own port (8080 by default) — NOT the orchestrator's PORT. Kept literal so a launcher that
# injects PORT for the orchestrator process (e.g. a dev-server manager) can't accidentally point the MCP
# server at the orchestrator itself. Override ENGINE_BASE_URL explicitly if the engine binds elsewhere.
ENGINE_BASE_URL = os.environ.get("ENGINE_BASE_URL") or "http://127.0.0.1:8080"
ORCH_HOST = os.environ.get("ORCH_HOST", "0.0.0.0")
ORCH_PORT = int(os.environ.get("ORCH_PORT", "8090"))
# How the chat UI should authenticate. "test" => the local AUTH_TEST_SUB bypass (engine is local/stub, no
# Google sign-in). "firebase" => real Google sign-in (engine is the deployed one). Default: inferred from
# whether the engine is on loopback. The orchestrator exposes this to the UI at GET /config.
ORCH_AUTH_MODE = os.environ.get("ORCH_AUTH_MODE") or (
    "test" if ("127.0.0.1" in ENGINE_BASE_URL or "localhost" in ENGINE_BASE_URL) else "firebase")


def anthropic_api_key():
    """The Anthropic API key, read at connect time (security-sensitive; mirrors world_pg_password)."""
    k = os.environ.get("ANTHROPIC_API_KEY")
    if not k:
        raise RuntimeError("ANTHROPIC_API_KEY is not set — the Sonnet orchestrator needs it")
    return k


def mcp_server_cmd():
    """argv used to spawn the PreReasoner MCP server over stdio. Overridable as a JSON list so the
    transport/entrypoint can change without code edits (matches the 'every knob is an env var' contract)."""
    raw = os.environ.get("MCP_SERVER_CMD")
    if raw:
        import json
        return json.loads(raw)
    import sys
    return [sys.executable, "-m", "mcp_server.server"]
