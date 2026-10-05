"""engine_client.py — the HTTP call to the Prereasoner engine + response shaping.

Kept separate from the MCP transport (server.py) so it is unit-testable without the `mcp` package or a
subprocess: `shape_reason_response(...)` is a pure function over the engine's JSON, and `call_query(...)`
is the only thing that touches the network.

Contract reference (docs/MCP.md): the engine's /api/reason body is NOT one fixed shape. We map
it to a stable tool output whose `status` is one of "answered" | "decompose" | "clarify" | "error" — a mapping WE
compute (the engine has no top-level `status` field).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

import httpx

from engine import request_timing

# Read at call time so tests / the orchestrator can set these before a call (mirrors engine/config.py style).
DEFAULT_TIMEOUT = float(os.environ.get("ENGINE_HTTP_TIMEOUT", "180"))  # cold Cloud Run can take minutes
# Stored sheets this process fetched, by (principal, conversation, source hash). A hash names one content,
# and the principal keys the entry so a cached copy never answers a user the engine did not authorize.
_SOURCES: OrderedDict = OrderedDict()
_SOURCES_LOCK = threading.Lock()
_SOURCES_MAX_CHARS = 64_000_000


class StoredSourceError(Exception):
    """A question named stored sheets the engine does not hold for this user: 409 with the stored hash when
    they were replaced (the client uploads its sheets again), 404 when there is no such conversation."""

    def __init__(self, status: int, message: str, source_hash: str = ""):
        super().__init__(message)
        self.status = status
        self.source_hash = source_hash


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
    if j.get("decomposition_required"):
        status = "decompose"
    elif j.get("clarify") is True:
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
    if j.get("execution") is not None:
        out["execution"] = j["execution"]

    if status == "answered":
        out["answer"] = j.get("result")            # {columns, rows}
        if j.get("sql") is not None:
            out["sql"] = j.get("sql")
        if j.get("views") is not None:
            out["views"] = j.get("views")           # the reasoning stack the player renders
        for k in ("meaning_join", "provenance", "warnings", "as_of", "reference",
                  "dataset_semantics", "analysis", "deterministic", "decomposition", "calculations",
                  "fallback", "unit", "unmatched", "layout_copies"):
            if j.get(k) is not None:
                out[k] = j.get(k)
        # trace coordinates: the browser knows its own uid; we return the jobId the engine streamed under.
        out["trace"] = {"jobId": job_id}
        if j.get("conversation_id"):
            out["conversation_id"] = j["conversation_id"]    # so the orchestrator reuses ONE conversation for the whole session (no per-call minting)
        if j.get("source_hash"):
            out["source_hash"] = j["source_hash"]            # a later question names these sheets instead of sending them
    elif status == "decompose":
        out["decomposition_required"] = j["decomposition_required"]
        out["trace"] = {"jobId": job_id}
        if j.get("conversation_id"):
            out["conversation_id"] = j["conversation_id"]
    elif status == "clarify":
        out["clarify"] = {k: j.get(k) for k in (
            "proposed", "dropped", "bindings", "original_sql", "reason", "unmet",
            "calculations", "currency", "unmatched",
        )
                          if j.get(k) is not None}
        if j.get("decomposition_rejected"):
            # A rejected proposal is correctable: surface the marker and the
            # engine's actionable detail so the orchestrator can grant its one
            # bounded retry instead of ending the turn on the humanized reason.
            out["decomposition_rejected"] = True
            out["rejection_detail"] = str(j.get("detail") or "")
        if j.get("dataset_ops_rejected"):
            # A rejected dataset op is correctable too: the orchestrator grants one repair.
            out["dataset_ops_rejected"] = True
            out["rejection_detail"] = str(j.get("reason") or "")
        out["trace"] = {"jobId": job_id}
        if j.get("conversation_id"):
            out["conversation_id"] = j["conversation_id"]
    else:  # error
        out["error"] = str(j.get("error") or "the engine returned no answer")

    return out


# Cloud Run answers 429 "no available instance" when every engine instance it runs is busy and none
# frees up while the request waits: a burst of questions, or a new instance still starting (about 70 s).
# That request never reached the engine. Asked again a little later it lands on a free or a new instance,
# and a repeated jobId never runs twice (engine/request_replay.py). The engine's own 429 (a principal's
# rate or budget) is JSON and goes back to the caller at once.
NO_INSTANCE_RETRY_SECONDS = (2.0, 4.0, 8.0, 16.0, 30.0)


def _no_instance(response: httpx.Response) -> bool:
    return (response.status_code == 429
            and not response.headers.get("content-type", "").startswith("application/json"))


async def _send(http: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """``http.request(method, url, **kwargs)``, asked again while Cloud Run has no instance for it."""
    response = await http.request(method, url, **kwargs)
    for delay in NO_INSTANCE_RETRY_SECONDS:
        if not _no_instance(response):
            break
        request_timing.count("engine_no_instance_retry")
        await asyncio.sleep(delay)
        response = await http.request(method, url, **kwargs)
    return response


@asynccontextmanager
async def _http(client: httpx.AsyncClient | None, timeout: float | None):
    """Yield the caller's client (connection reuse across a chat turn's calls) or a temporary one."""
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT) as temporary:
        yield temporary


def _headers(token: str | None, request_id: str | None,
             dataset_attestation: str | None = None) -> dict[str, str]:
    """Per-call headers. `token` is passed EXPLICITLY by callers that have one: the env fallback is
    process-global, so an in-process caller serving concurrent users must never rely on it."""
    headers = {"content-type": "application/json"}
    tok = token if token is not None else _bearer_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if request_id:
        headers["X-Request-Id"] = request_id       # correlate this call with the chat turn that issued it
    if dataset_attestation:
        from engine.dataset_attestation import HEADER
        headers[HEADER] = dataset_attestation
    return headers


async def call_conversation_source(conversation_id: str, source_hash: str, *, principal: str,
                                   base_url: str | None = None, token: str | None = None,
                                   timeout: float | None = None, request_id: str | None = None,
                                   client: httpx.AsyncClient | None = None) -> list[dict]:
    """The sheets a conversation stores, when they are the snapshot ``source_hash`` names: a question names
    its sheets instead of carrying them (upload once, 2026-10-02), and the chat service still reads their
    headers and values for its own checks. Raises StoredSourceError."""
    key = (principal, conversation_id, source_hash)
    with _SOURCES_LOCK:
        cached = _SOURCES.get(key)
        if cached is not None:
            _SOURCES.move_to_end(key)
            return cached[0]
    base = (base_url or _engine_base_url()).rstrip("/")
    try:
        async with _http(client, timeout) as http:
            r = await _send(http, "GET", f"{base}/api/conversation", params={"id": conversation_id},
                            headers=_headers(token, request_id), timeout=timeout or DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        raise StoredSourceError(502, f"could not reach the Prereasoner engine at {base}: {e}") from e
    if r.status_code == 404:
        raise StoredSourceError(404, "conversation not found")
    if r.status_code != 200:
        raise StoredSourceError(502, f"the engine returned HTTP {r.status_code} for the conversation")
    body = r.json()
    if body.get("source_hash") != source_hash:
        raise StoredSourceError(409, "the conversation's sheets changed; upload them again",
                                str(body.get("source_hash") or ""))
    tables = list(body.get("tables") or [])
    chars = sum(len(str(table.get("data") or "")) for table in tables)
    if chars <= _SOURCES_MAX_CHARS:
        with _SOURCES_LOCK:
            _SOURCES[key] = (tables, chars)
            while sum(entry[1] for entry in _SOURCES.values()) > _SOURCES_MAX_CHARS:
                _SOURCES.popitem(last=False)
    return tables


async def call_query(question: str, tables: list[dict], job_id: str | None = None,
                     conversation_id: str | None = None,
                     *, base_url: str | None = None, token: str | None = None,
                     timeout: float | None = None, request_id: str | None = None,
                     client: httpx.AsyncClient | None = None,
                     dataset_ops: list[dict] | None = None,
                     dataset_attestation: str | None = None,
                     analysis: dict[str, Any] | None = None,
                     decomposition: dict[str, Any] | None = None,
                     use: str | None = None,
                     source_hash: str | None = None) -> dict[str, Any]:
    """POST the question + inline tables to the engine's /api/reason and return the shaped tool output.

    `tables` is [{name, data}] where data is raw CSV text — exactly the engine's inline shape (no dataset_id).
    With `source_hash`, the question names the sheets `conversation_id` stores instead (the tables stay
    out of the request; the engine answers 409 when they were replaced).
    `conversation_id`, when given, keeps every call on ONE conversation schema (else the engine mints a fresh
    one per call — the orchestrated-mode conversation-spam bug).

    ASYNC because both callers are async: the MCP tool (FastMCP awaits it) and the orchestrator's chat
    loop, which runs on one shared event loop and would stall every concurrent turn on a blocking POST.
    One implementation serves both; pass `client` to reuse a connection across a turn's calls."""
    base = (base_url or _engine_base_url()).rstrip("/")
    body: dict[str, Any] = ({"source_hash": source_hash} if source_hash else {"tables": tables})
    body["question"] = question
    if job_id:
        body["jobId"] = job_id
    if conversation_id:
        body["conversation_id"] = conversation_id
    if dataset_ops:
        body["dataset_ops"] = dataset_ops        # conversation-stated measure metadata (docs/DATASET_FORMATTER.md)
    if analysis:
        body["analysis"] = analysis
    if decomposition:
        body["decomposition"] = decomposition
    if use:
        body["use"] = use
    try:
        async with _http(client, timeout) as http:
            for attempt in (1, 2):
                started = time.perf_counter()
                try:
                    r = await _send(http, "POST", f"{base}/api/reason", json=body,
                                    headers=_headers(token, request_id, dataset_attestation),
                                    timeout=timeout or DEFAULT_TIMEOUT)
                    break
                except (httpx.ReadTimeout, httpx.PoolTimeout):
                    raise                            # the engine is slow, not unreachable: asking again waits again
                except httpx.TransportError as e:
                    # A response lost between the services ended a chat turn with "send the question
                    # again" although the engine had answered (Chrome gate, 2026-10-02). The engine
                    # answers a repeated jobId with the first request's response
                    # (engine.request_replay.DurableResponseReplay), so asking once more never runs it twice.
                    request_timing.mark(f"engine_transport_{type(e).__name__}", time.perf_counter() - started)
                    if attempt == 2 or not job_id:
                        raise
                    await asyncio.sleep(1.0)
    except httpx.HTTPError as e:
        return {"status": "error", "error": f"could not reach the Prereasoner engine at {base}: {e}",
                "unreachable": True}
    # The engine returns 200 for most in-band outcomes; 401/500 carry a top-level {"error": ...}. The
    # status travels with the shaped error so the reply can say what the user can do about it.
    try:
        j = r.json()
    except ValueError:
        return {"status": "error", "http_status": r.status_code,
                "error": f"engine returned non-JSON (HTTP {r.status_code}): {r.text[:200]}"}
    shaped = shape_reason_response(j, job_id)
    if r.status_code >= 400:
        shaped["http_status"] = r.status_code
    return shaped


class EngineStatusError(RuntimeError):
    """The engine refused a call the chat turn depends on: 404 (the conversation is gone), 401, or
    429/503 (busy). The chat service answers with the same status, so a client can recover."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"engine returned HTTP {status}")
        self.status = status


async def call_analysis_catalog(conversation_id: str, *, base_url: str | None = None,
                                token: str | None = None, timeout: float | None = None,
                                request_id: str | None = None,
                                client: httpx.AsyncClient | None = None) -> list[dict[str, Any]] | None:
    """Load the engine-owned analysis catalog used to constrain create/modify selection. A refusal the
    client can act on raises EngineStatusError; any other failure returns None."""
    base = (base_url or _engine_base_url()).rstrip("/")
    try:
        async with _http(client, timeout) as http:
            response = await _send(
                http, "GET",
                f"{base}/api/analyses",
                params={"conversation_id": conversation_id},
                headers=_headers(token, request_id),
                timeout=timeout or DEFAULT_TIMEOUT,
            )
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if response.status_code in (401, 404, 429, 503):
        raise EngineStatusError(response.status_code,
                                str(payload.get("error") or "") if isinstance(payload, dict) else "")
    if response.status_code != 200 or not isinstance(payload, dict):
        return None
    analyses = payload.get("analyses")
    return analyses if isinstance(analyses, list) else []


async def call_describe(tables: list[dict], *, base_url: str | None = None,
                        token: str | None = None, timeout: float | None = None,
                        request_id: str | None = None,
                        client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Per-table coverage hint via the engine's stateless /api/dimension.

    Honest scope limit (docs/MCP.md): this reports what the model TYPES each column as, not which cells
    actually resolved to world entities. Returns one readout per table.
    """
    base = (base_url or _engine_base_url()).rstrip("/")
    headers = _headers(token, request_id)
    out: list[dict[str, Any]] = []
    async with _http(client, timeout) as http:
        for t in tables or []:
            name = t.get("name") or "data"
            data = t.get("data") or ""
            if not data.strip():
                continue
            try:
                r = await _send(http, "POST", f"{base}/api/dimension",
                                json={"data": data, "table": name, "mode": "analyze"},
                                headers=headers,
                                timeout=timeout or DEFAULT_TIMEOUT)
                j = r.json()
            except (httpx.HTTPError, ValueError) as e:
                out.append({"table": name, "error": str(e)})
                continue
            if j.get("error"):
                out.append({"table": name, "error": j["error"]})
                continue
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
            schema = j.get("schema_org") or {}
            out.append({
                "table": name,
                "columns": cols,
                "schema_org": {
                    "classes": schema.get("classes") or [],
                    "properties": [p for p in (schema.get("properties") or []) if p.get("fired")],
                    "abstained": bool(schema.get("abstained", True)),
                    "ontology_version": schema.get("ontology_version"),
                    "model_artifact_sha256": schema.get("model_artifact_sha256"),
                },
                "model": j.get("model"),
            })
    return {"tables": out}
