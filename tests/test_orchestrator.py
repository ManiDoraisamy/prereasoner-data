"""test_orchestrator.py — the Sonnet orchestrator's routing discipline (docs/MCP.md), against the
in-process STUB engine so the orchestrator->MCP->engine loop is exercised without a seeded world
Postgres.

GATED on ANTHROPIC_API_KEY (mirrors how the engine tests gate on KB_PG_PASSWORD): absent => the suite
self-skips with exit 0 rather than failing, so CI without a key stays green. It loads the repo .env if the
key isn't already in the environment.

For a hosted-release gate, set REQUIRE_ORCHESTRATOR_TESTS=1 so a missing key fails instead of skipping.
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
        if os.environ.get("REQUIRE_ORCHESTRATOR_TESTS") == "1":
            print("test_orchestrator: FAIL (ANTHROPIC_API_KEY is required for this release gate)")
            sys.exit(1)
        print("test_orchestrator: SKIP (ANTHROPIC_API_KEY not set)")
        sys.exit(0)

    from orchestrator.orchestrator import run_chat

    port = 8812
    base = f"http://127.0.0.1:{port}"
    srv = _start_stub(port)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
    key = os.environ["ANTHROPIC_API_KEY"]

    async def chat(msg, history=None, tables=None):
        return await run_chat(msg, tables or TABLES, history or [], engine_base_url=base,
                              bearer_token=None, api_key=key, model=model)

    try:
        # Rule 1 — a truth-bearing number buried in a strategic question must come from the tool.
        print("[1] truth-bearing number routes to the tool")
        r1 = asyncio.run(chat("Does our French revenue justify hiring in Europe? Give me the French total."))
        answered = [t for t in r1["traces"] if (t["engine"] or {}).get("status") == "answered"]
        ok(len(answered) >= 1, "at least one prereasoner_query call was made")
        ok("270" in r1["reply"],
           f"the French total (270) from the tool appears in the reply (got {r1['reply']!r})")

        # Rule 1b — a standalone question reaches the engine in the user's words (prompt rule 3).
        # A rewrite that dropped "in US dollars" shipped an unconverted total on 2026-09-06. The
        # stub echoes the received question into the trace, so this asserts at the exact boundary.
        print("[1b] standalone question passes through with its conversion intact")
        standalone = "total amount in France in US dollars"
        r1b = asyncio.run(chat(standalone))
        sent = [t.get("question", "") for t in r1b["traces"]]
        ok(any(q == standalone for q in sent),
           f"the engine receives the standalone question verbatim (got {sent})")

        # Rule 1b also covers qualifier families that a currency-specific code guard could not.
        print("[1b] standalone grouping, limit, and time qualifiers pass through verbatim")
        qualified = "top 3 customers by total amount per month since January 2025"
        rq = asyncio.run(chat(qualified))
        sent_q = [t.get("question", "") for t in rq["traces"]]
        ok(any(q == qualified for q in sent_q),
           f"the engine receives all standalone qualifiers verbatim (got {sent_q})")

        # Rule 1c — a follow-up rewrite carries the conversation's qualifiers (prompt rule 4):
        # after a France-in-USD turn, "how about Germany?" must become a Germany question that
        # still converts to USD. This is the case no per-dimension code guard could cover.
        print("[1c] follow-up rewrite carries the USD qualifier")
        r1c = asyncio.run(chat("how about Germany?", history=[
            {"role": "user", "content": "total amount in France in US dollars"},
            {"role": "assistant", "content": "Your total for France comes to about 1,127 in US dollars."}]))
        sent_c = [t.get("question", "") for t in r1c["traces"]]
        ok(any("german" in q.lower() and "us dollar" in q.lower() and "france" not in q.lower()
               for q in sent_c),
           f"the rewritten follow-up changes the country and keeps the USD qualifier (got {sent_c})")

        print("[1c] follow-up rewrite carries grouping and limit qualifiers")
        r1d = asyncio.run(chat("what about 2024?", history=[
            {"role": "user", "content": "top 3 customers by total amount per month in 2025"},
            {"role": "assistant", "content": "I found the top three customers for each month in 2025."}]))
        sent_d = [t.get("question", "") for t in r1d["traces"]]
        ok(any(all(term in q.lower() for term in ("top 3", "customer", "total", "month", "2024"))
               and "2025" not in q for q in sent_d),
           f"the rewritten follow-up changes the year and keeps grouping/limit qualifiers (got {sent_d})")

        # Production regression (2026-09-08): this follow-up was decomposed into five progressively
        # weaker queries, ended on a grouped COUNT, and discarded all useful terminal results with
        # "step budget". The shipped workbook now includes the exact tier schedule as a fixture.
        print("[1d] joined discount follow-up is one complete terminal query")
        dataset = Path(__file__).resolve().parents[1] / "web" / "public" / "dataset" / "customer-orders"
        discount_tables = [
            {"name": name, "data": (dataset / f"{name}.csv").read_text(encoding="utf-8")}
            for name in ("orders", "tier")
        ]
        discount = asyncio.run(chat(
            "reduce the discount from total amount based on customer's tier",
            history=[
                {"role": "user", "content": "total amount in France in US dollars"},
                {"role": "assistant", "content":
                 "Your total sales in France comes to about $1,126.56 US dollars."},
            ],
            tables=discount_tables,
        ))
        sent_discount = [trace.get("question", "") for trace in discount["traces"]]
        ok(len(sent_discount) == 1,
           f"the completed engine result terminates the tool loop (got {sent_discount})")
        ok(bool(sent_discount) and all(term in sent_discount[0].lower() for term in (
            "discount", "tier", "france", "us dollar",
        )), f"the one query carries calculation and prior qualifiers (got {sent_discount})")
        ok("step budget" not in discount["reply"].lower(),
           "the workflow cannot replace a terminal result with a step-budget error")

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
    previous_app_env = os.environ.get("APP_ENV")
    previous_test_sub = os.environ.get("AUTH_TEST_SUB")
    # The production guard intentionally ignores AUTH_TEST_SUB. Declare this harness as a test
    # environment before reloading config so the envelope test exercises chat, not real Firebase.
    os.environ["APP_ENV"] = "test"
    os.environ["AUTH_TEST_SUB"] = "localdev"
    # Relaying the message to Anthropic is egress, so /chat requires the operator's deployment
    # switch. Set it explicitly so the test does not depend on the developer's environment.
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
        _, granted = post(message)
        ok("reply" in granted and isinstance(granted.get("history"), list),
           "POST /chat returns {reply, history}")

        os.environ["EXTERNAL_LLM_ENABLED"] = "0"
        status, disabled = post(message, timeout=30)
        ok(status == 503 and "reply" not in disabled,
           "the deployment-level egress switch disables chat")
    finally:
        if previous_egress is None:
            os.environ.pop("EXTERNAL_LLM_ENABLED", None)
        else:
            os.environ["EXTERNAL_LLM_ENABLED"] = previous_egress
        httpd.shutdown()
        if previous_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = previous_app_env
        if previous_test_sub is None:
            os.environ.pop("AUTH_TEST_SUB", None)
        else:
            os.environ["AUTH_TEST_SUB"] = previous_test_sub
        importlib.reload(_cfg)


if __name__ == "__main__":
    main()
