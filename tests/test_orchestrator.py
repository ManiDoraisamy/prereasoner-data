"""test_orchestrator.py — the Sonnet orchestrator's routing discipline (docs/MCP.md), against the
in-process STUB engine so the whole browser->orchestrator->MCP->engine loop is exercised without a seeded
world Postgres.

GATED on ANTHROPIC_API_KEY (mirrors how the engine tests gate on KB_PG_PASSWORD): absent => the suite
self-skips with exit 0 rather than failing, so CI without a key stays green. It loads the repo .env if the
key isn't already in the environment.

Run: python -m tests.test_orchestrator
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

P = 0
F = 0


def ok(cond, msg):
    global P, F
    if cond:
        P += 1
        print(f"  PASS  {msg}")
    else:
        F += 1
        print(f"  FAIL  {msg}")


def _load_env():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


TABLES = [
    {"name": "customers", "data": "customer_id,city\n1,Paris\n2,Lyon\n3,Berlin\n"},
    {"name": "orders", "data": "order_id,customer_id,amount\n10,1,120\n11,2,150\n12,3,90\n"},
]


def _start_stub(port):
    from tests.stub_engine import H
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    _load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("test_orchestrator: SKIP (ANTHROPIC_API_KEY not set)")
        sys.exit(0)

    from orchestrator.orchestrator import run_chat

    port = 8812
    base = f"http://127.0.0.1:{port}"
    srv = _start_stub(port)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    key = os.environ["ANTHROPIC_API_KEY"]

    async def chat(msg, history=None):
        return await run_chat(msg, TABLES, history or [], engine_base_url=base,
                              bearer_token=None, api_key=key, model=model)

    try:
        # Rule 1 — a truth-bearing number buried in a strategic question must come from the tool.
        print("[1] truth-bearing number routes to the tool")
        r1 = asyncio.run(chat("Does our French revenue justify hiring in Europe? Give me the French total."))
        answered = [t for t in r1["traces"] if (t["engine"] or {}).get("status") == "answered"]
        ok(len(answered) >= 1, "at least one prereasoner_query call was made")
        ok("270" in r1["reply"], "the French total (270) from the tool appears in the reply")

        # Rule 4 — a clarify must pass through, never be smoothed into a fabricated answer.
        print("[4] clarify passes through, not smoothed over")
        r2 = asyncio.run(chat("What is our total revenue by region?"))
        clarified = [t for t in r2["traces"] if (t["engine"] or {}).get("status") == "clarify"]
        ok(len(clarified) >= 1, "the ambiguous query produced a clarify outcome")
        ok("region" in r2["reply"].lower(), "the reply relays the clarification (mentions 'region')")

        # server wiring — POST /chat returns a well-formed envelope (cheap, no tool needed).
        print("[S] POST /chat server envelope")
        _test_server(key, model, base)
    finally:
        srv.shutdown()

    print(f"\ntest_orchestrator: {P} passed, {F} failed")
    sys.exit(1 if F else 0)


def _test_server(key, model, engine_base):
    """Start the orchestrator HTTP server and post a trivial message (no tool call needed)."""
    os.environ["ANTHROPIC_API_KEY"] = key
    os.environ["ANTHROPIC_MODEL"] = model
    os.environ["ENGINE_BASE_URL"] = engine_base
    os.environ["ORCH_PORT"] = "8813"
    os.environ.setdefault("AUTH_TEST_SUB", "localdev")   # /chat now requires a verified identity; local/test mode uses the bypass (as in docker-compose + real local dev)
    # Relaying the message to Anthropic is egress, so /chat requires BOTH the deployment opt-in and
    # per-request consent. Set explicitly rather than setdefault: the test must not pass or fail on
    # whatever the developer happens to have exported.
    previous_egress = os.environ.get("EXTERNAL_LLM_ENABLED")
    os.environ["EXTERNAL_LLM_ENABLED"] = "1"
    # reimport config so ENGINE_BASE_URL/ORCH_PORT take effect
    import importlib
    from engine import config as _cfg
    importlib.reload(_cfg)
    from orchestrator import server as _srv
    importlib.reload(_srv)
    httpd = ThreadingHTTPServer(("127.0.0.1", 8813), _srv.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(payload, timeout=120):
        request = urllib.request.Request(
            "http://127.0.0.1:8813/chat", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    try:
        message = {"message": "Say hello in one short sentence. Do not call any tool.",
                   "tables": TABLES, "history": []}
        _, granted = post({**message, "external_llm_consent": True})
        ok("reply" in granted and isinstance(granted.get("history"), list),
           "POST /chat returns {reply, history}")

        # The gate is a privacy control, so it needs a NEGATIVE test: withholding consent must refuse
        # rather than quietly forward the user's message and tables to Anthropic.
        status, refused = post(message, timeout=30)
        ok(status == 503 and "reply" not in refused,
           "POST /chat without external_llm_consent is refused and returns no model reply")

        os.environ["EXTERNAL_LLM_ENABLED"] = "0"
        status, disabled = post({**message, "external_llm_consent": True}, timeout=30)
        ok(status == 503 and "reply" not in disabled,
           "per-request consent cannot override the deployment-level egress switch")
    finally:
        if previous_egress is None:
            os.environ.pop("EXTERNAL_LLM_ENABLED", None)
        else:
            os.environ["EXTERNAL_LLM_ENABLED"] = previous_egress
        httpd.shutdown()


if __name__ == "__main__":
    main()
