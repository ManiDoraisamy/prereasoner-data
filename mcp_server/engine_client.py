"""engine_client.py — the HTTP call to the PreReasoner engine + response shaping.

Kept separate from the MCP transport (server.py) so it is unit-testable without the `mcp` package or a
subprocess: `shape_reason_response(...)` is a pure function over the engine's JSON, and `call_query(...)`
is the only thing that touches the network.

Contract reference (docs/MCP.md): the engine's /api/reason body is NOT one fixed shape. We map
it to a stable tool output whose `status` is one of "answered" | "clarify" | "error" — a mapping WE
compute (the engine has no top-level `status` field).
"""
from __future__ import annotations

import os
from typing import Any

import httpx

# Read at call time so tests / the orchestrator can set these before a call (mirrors engine/config.py style).
DEFAULT_TIMEOUT = float(os.environ.get("ENGINE_HTTP_TIMEOUT", "180"))  # cold Cloud Run can take minutes


def _engine_base_url() -> str:
    return os.environ.get("ENGINE_BASE_URL") or "http://127.0.0.1:8080"


def _bearer_token() -> str | None:
    """The Firebase token the orchestrator injected into this MCP server's env for the session.
    Absent locally, where the engine runs with AUTH_TEST_SUB and needs no token."""
    return os.environ.get("ENGINE_BEARER_TOKEN") or None


def shape_reason_response(engine_json: dict[str, Any], job_id: str | None) -> dict[str, Any]:
    """Map a raw /api/reason response to the stable tool output (docs/MCP.md).

    Pure function — no I/O — so the full status-mapping matrix is unit-testable.
    """
    j = engine_json if isinstance(engine_json, dict) else {}

    # Status mapping (engine has no `status` field; derive it).
    if j.get("clarify") is True:
        status = "clarify"
    elif j.get("error"):  # `error` as a field (guard/exec) OR a top-level {"error": ...} rejection body
        status = "error"
    elif j.get("result") is not None:
        status = "answered"
    else:
        # No result, no clarify, no error — treat as an (unusual) error so the orchestrator never
        # confabulates a value out of an empty answer.
        status = "error"

    out: dict[str, Any] = {"status": status, "model": j.get("model")}

    if status == "answered":
        out["answer"] = j.get("result")            # {columns, rows}
        if j.get("sql") is not None:
            out["sql"] = j.get("sql")
        if j.get("views") is not None:
            out["views"] = j.get("views")           # the reasoning stack the player renders
        for k in ("meaning_join", "provenance", "warnings", "as_of", "reference"):
            if j.get(k) is not None:
                out[k] = j.get(k)
        # trace coordinates: the browser knows its own uid; we return the jobId the engine streamed under.
        out["trace"] = {"jobId": job_id}
        if j.get("conversation_id"):
            out["conversation_id"] = j["conversation_id"]    # so the orchestrator reuses ONE conversation for the whole session (no per-call minting)
    elif status == "clarify":
        out["clarify"] = {k: j.get(k) for k in ("proposed", "dropped", "bindings", "original_sql")
                          if j.get(k) is not None}
        out["trace"] = {"jobId": job_id}
        if j.get("conversation_id"):
            out["conversation_id"] = j["conversation_id"]
    else:  # error
        out["error"] = str(j.get("error") or "the engine returned no answer")

    return out


def call_query(question: str, tables: list[dict], job_id: str | None = None,
               conversation_id: str | None = None,
               *, base_url: str | None = None, token: str | None = None,
               timeout: float | None = None) -> dict[str, Any]:
    """POST the question + inline tables to the engine's /api/reason and return the shaped tool output.

    `tables` is [{name, data}] where data is raw CSV text — exactly the engine's inline shape (no dataset_id).
    `conversation_id`, when given, keeps every call on ONE conversation schema (else the engine mints a fresh
    one per call — the orchestrated-mode conversation-spam bug)."""
    base = (base_url or _engine_base_url()).rstrip("/")
    tok = token if token is not None else _bearer_token()
    headers = {"content-type": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    body: dict[str, Any] = {"tables": tables, "question": question}
    if job_id:
        body["jobId"] = job_id
    if conversation_id:
        body["conversation_id"] = conversation_id
    try:
        r = httpx.post(f"{base}/api/reason", json=body, headers=headers,
                       timeout=timeout or DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        return {"status": "error", "error": f"could not reach the PreReasoner engine at {base}: {e}"}
    # The engine returns 200 for most in-band outcomes; 401/500 carry a top-level {"error": ...}.
    try:
        j = r.json()
    except ValueError:
        return {"status": "error",
                "error": f"engine returned non-JSON (HTTP {r.status_code}): {r.text[:200]}"}
    return shape_reason_response(j, job_id)


def call_describe(tables: list[dict], *, base_url: str | None = None,
                  timeout: float | None = None) -> dict[str, Any]:
    """Per-table coverage hint via the engine's stateless /api/dimension (no auth).

    Honest scope limit (docs/MCP.md): this reports what the model TYPES each column as, not which cells
    actually resolved to world entities. Returns one readout per table.
    """
    base = (base_url or _engine_base_url()).rstrip("/")
    out: list[dict[str, Any]] = []
    for t in tables or []:
        name = t.get("name") or "data"
        data = t.get("data") or ""
        if not data.strip():
            continue
        try:
            r = httpx.post(f"{base}/api/dimension",
                           json={"data": data, "table": name, "mode": "analyze"},
                           timeout=timeout or DEFAULT_TIMEOUT)
            j = r.json()
        except (httpx.HTTPError, ValueError) as e:
            out.append({"table": name, "error": str(e)})
            continue
        if j.get("error"):
            out.append({"table": name, "error": j["error"]})
        else:
            # columns[].name + a compact top-dimension-per-column summary from the readout.
            cols = []
            for c in (j.get("columns") or []):
                evo = c.get("evolution") or []
                top = {}
                if evo:
                    last = evo[-1] if isinstance(evo[-1], dict) else {}
                    # highest-scoring named dim at the final layer = the model's best read of the column.
                    if last:
                        top = max(last.items(), key=lambda kv: kv[1] if isinstance(kv[1], (int, float)) else -1)
                        top = {"dim": top[0], "score": top[1]}
                cols.append({"name": c.get("name"), "reads_as": top})
            out.append({"table": name, "columns": cols, "model": j.get("model")})
    return {"tables": out}
