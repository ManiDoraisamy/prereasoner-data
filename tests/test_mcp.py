"""test_mcp.py — the Prereasoner MCP server layer.

Two parts, both dependency-light (no seeded Postgres, no model weights, no Gemini access):
  (A) UNIT — shape_reason_response's status-mapping matrix (pure function; docs/MCP.md).
  (B) INTEGRATION — engine_client.call_query / call_describe against an in-process STUB engine
      (tests/stub_engine.py) that returns the engine's exact documented shapes.

Run: python -m tests.test_mcp     (self-contained; always runnable)
Follows the repo's hand-rolled P/F convention (tests/README.md) — no pytest.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from http.server import ThreadingHTTPServer

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_server import engine_client
from tests.stub_engine import AUTH_SEEN, ENGINE_RATE_LIMITED, H, LOSE_FIRST_RESPONSE, NO_INSTANCE, REQUESTS

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


def _start_stub(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------- (A) unit: the status-mapping matrix ----------------
def test_shape():
    print("[A] shape_reason_response — status mapping")
    ans = engine_client.shape_reason_response(
        {"question": "q", "result": {"columns": ["total"], "rows": [[270]]},
         "sql": "SELECT ...", "views": [{"op": "group_agg"}], "model": "engine - x"}, "job1")
    ok(ans["status"] == "answered", "answer -> status 'answered'")
    ok(ans["answer"]["rows"] == [[270]], "answer carries result rows")
    ok(ans.get("sql") == "SELECT ...", "answer carries sql")
    ok(ans.get("views") == [{"op": "group_agg"}], "answer carries views")
    ok(ans["trace"]["jobId"] == "job1", "answer carries trace.jobId")
    calculations = [{"specification": "currency", "status": "satisfied", "realization": "converted",
                     "target": "USD"}]
    converted = engine_client.shape_reason_response(
        {"question": "q", "result": {"columns": ["total_usd"], "rows": [[70401]]},
         "calculations": calculations, "currency": calculations[0]}, "job2")
    ok(converted.get("calculations") == calculations,
       "answer carries the calculation evidence (the verified output currency)")
    ok("currency" not in converted, "the compatibility projection is not a second copy in the tool output")
    share = engine_client.shape_reason_response(
        {"question": "q", "result": {"columns": ["share"], "rows": [["0.3"]]}, "unit": "percent"}, "job3")
    ok(share.get("unit") == "percent", "answer carries the engine's unit (a share is stated as a percentage)")
    # A non-geo total that left out rows whose entity matched nothing says so (engine/knowledge_query.py
    # unmatched_rows); the disclosure was never forwarded to the chat, so the reply could not say it.
    unmatched = {"table": "transfers", "column": "hospital", "entity": "hospital", "rows": 1, "of": 5,
                 "names": ["Xqzv Kpltr"], "more": 0}
    partial = engine_client.shape_reason_response(
        {"question": "q", "result": {"columns": ["sum"], "rows": [[46]]}, "unmatched": unmatched,
         "warnings": ["1 of the 5 rows name a hospital that could not be matched"]}, "job4")
    ok(partial.get("unmatched") == unmatched, "answer carries the rows whose entity matched nothing")
    copies = {"read": ["NT"], "others": ["SI", "FF"]}
    copied = engine_client.shape_reason_response(
        {"question": "q", "result": {"columns": ["sum"], "rows": [[20]]}, "layout_copies": copies}, "job6")
    ok(copied.get("layout_copies") == copies, "answer carries the other tables that could answer it")
    declined = engine_client.shape_reason_response(
        {"question": "q", "clarify": True, "reason": "3 of the 5 hospital names could not be matched",
         "unmatched": {**unmatched, "rows": 3}}, "job5")
    ok(declined["status"] == "clarify" and declined["clarify"].get("unmatched", {}).get("rows") == 3,
       "a decline over unmatched names carries them in the clarification")

    clar = engine_client.shape_reason_response(
        {"question": "q", "clarify": True, "proposed": "by country", "dropped": ["region"],
         "original_sql": "GROUP BY region"}, "j")
    ok(clar["status"] == "clarify", "clarify:true -> status 'clarify'")
    ok(clar["clarify"]["proposed"] == "by country", "clarify carries proposed")
    ok(clar["clarify"]["dropped"] == ["region"], "clarify carries dropped")
    ok("answer" not in clar, "clarify has no answer")

    decompose = engine_client.shape_reason_response(
        {
            "question": "q",
            "clarify": True,
            "decomposition_required": {"reason": "compound analysis"},
            "conversation_id": "c_" + "1" * 32,
        },
        "j-decompose",
    )
    ok(decompose["status"] == "decompose", "decomposition request -> status 'decompose'")
    ok(
        decompose["decomposition_required"]["reason"] == "compound analysis",
        "decompose carries the bounded-retry reason",
    )
    ok("answer" not in decompose, "decompose has no premature answer")
    ok(
        decompose.get("conversation_id") == "c_" + "1" * 32,
        "decompose preserves the conversation for the one retry",
    )

    rejected = engine_client.shape_reason_response(
        {
            "question": "q",
            "clarify": True,
            "reason": "I couldn't run this as one combined analysis.",
            "detail": "subquestion 'top_categories' is a ranking but groups 2 columns",
            "decomposition_rejected": True,
        },
        "j-rejected",
    )
    ok(rejected["status"] == "clarify", "rejected proposal keeps status 'clarify'")
    ok(rejected.get("decomposition_rejected") is True,
       "rejected proposal carries the correctable marker")
    ok("groups 2 columns" in rejected.get("rejection_detail", ""),
       "rejected proposal forwards the engine's actionable detail")
    ok("detail" not in rejected.get("clarify", {}),
       "the user-facing clarify still hides validator internals")

    rejected_op = engine_client.shape_reason_response(
        {
            "question": "q",
            "clarify": True,
            "reason": "dataset operation names a table that is not uploaded: 'budget'",
            "dataset_ops_rejected": True,
        },
        "j-op",
    )
    ok(rejected_op["status"] == "clarify" and rejected_op.get("dataset_ops_rejected") is True,
       "a rejected dataset op keeps status 'clarify' and carries the correctable marker")
    ok("not uploaded: 'budget'" in rejected_op.get("rejection_detail", ""),
       "a rejected dataset op forwards the validator's sentence as the repair detail")

    err_field = engine_client.shape_reason_response({"question": "q", "error": "guard: no", "result": None}, "j")
    ok(err_field["status"] == "error", "error field -> status 'error'")
    ok(err_field["error"] == "guard: no", "error message surfaced")

    err_body = engine_client.shape_reason_response({"error": "sign in required"}, "j")
    ok(err_body["status"] == "error", "top-level {error} body -> status 'error'")

    empty = engine_client.shape_reason_response({}, "j")
    ok(empty["status"] == "error", "empty (no result/clarify/error) -> 'error', never a fake answer")

    # Regression for an OBSERVED drop (2026-09-07): the engine returned dataset_semantics but the
    # shaping whitelist omitted it, so the browser badge never received its data on the chat path.
    ds = engine_client.shape_reason_response(
        {"question": "q", "result": {"columns": ["sum"], "rows": [[71573.9]]},
         "dataset_semantics": [{"table": "responses", "column": "budget", "currency": "EUR"}],
         "analysis": {"analysis_id": "a_" + "1" * 32, "slug": "total_sales", "revision": 1}}, "j")
    ok(ds.get("dataset_semantics") == [{"table": "responses", "column": "budget", "currency": "EUR"}],
       "dataset_semantics survives the shaping (the UI badge rides the trace payload)")
    ok(ds.get("analysis", {}).get("slug") == "total_sales",
       "analysis identity survives shaping for workbook selection")
    headers = engine_client._headers("token", "request", "v1=signature")
    ok(headers.get("X-Prereasoner-Dataset-Attestation") == "v1=signature",
       "the shared HTTP client carries the orchestrator's dataset attestation")


# ---------------- (B) integration: against the stub engine ----------------
def run_integration(base):
    print("[B] engine_client against the stub engine")
    tables = [{"name": "customers", "data": "customer_id,city\n1,Paris\n2,Lyon\n3,Berlin\n"},
              {"name": "orders", "data": "order_id,customer_id,amount\n10,1,120\n11,2,150\n12,3,90\n"}]

    r = asyncio.run(engine_client.call_query("total amount in France", tables, "jobA", base_url=base))
    ok(r["status"] == "answered", "France query -> answered")
    ok(r["answer"]["rows"] == [[270]], "France total == 270")
    ok(len(r.get("views", [])) == 3, "France answer carries the 3-view stack")
    ok(r["trace"]["jobId"] == "jobA", "trace jobId round-trips")

    r2 = asyncio.run(engine_client.call_query("total revenue by region", tables, "jobB", base_url=base))
    ok(r2["status"] == "clarify", "ambiguous 'region' query -> clarify")
    ok("region" in (r2["clarify"].get("dropped") or []), "clarify drops 'region'")

    r3 = asyncio.run(engine_client.call_query("how much did we sell overall", tables, "jobC", base_url=base))
    ok(r3["status"] == "answered", "generic query -> answered")

    asyncio.run(engine_client.call_query("how much did we sell overall", tables, "jobMode",
                                         base_url=base, use="both"))
    ok(REQUESTS[-1].get("use") == "both", "URL-selected execution mode reaches the engine client")

    d = asyncio.run(engine_client.call_describe([{"name": "customers", "data": "city\nParis\nLyon\n"}],
                                                base_url=base))
    ok("tables" in d and d["tables"], "describe returns per-table readout")
    ok(d["tables"][0].get("columns") is not None, "describe reports columns")

    # unreachable engine -> a clean error, never a raised exception
    bad = asyncio.run(engine_client.call_query("x", tables, "jobD",
                                               base_url="http://127.0.0.1:9", timeout=2))
    ok(bad["status"] == "error", "unreachable engine -> status 'error' (no crash)")

    # Chrome gate, 2026-10-02 (complex-category-gaps): the engine answered in 65 s, its response was
    # lost on the way to the chat service, and the reply asked the user to send the question again.
    # The client asks once more with the same jobId; the engine answers a repeated jobId with the
    # first request's response (engine.request_replay.DurableResponseReplay), so nothing runs twice.
    LOSE_FIRST_RESPONSE.add("jobLost")
    before = len(REQUESTS)
    lost = asyncio.run(engine_client.call_query("total amount in France", tables, "jobLost", base_url=base))
    ok(lost["status"] == "answered" and lost["answer"]["rows"] == [[270]],
       f"a lost response is asked for again and answers (got {lost.get('status')}: {lost.get('error')})")
    ok([request.get("jobId") for request in REQUESTS[before:]] == ["jobLost", "jobLost"],
       "the repeat carries the first request's jobId, so the engine can answer it without a second run")
    # Without a jobId the engine cannot tell a repeat from a new question, so nothing is repeated.
    LOSE_FIRST_RESPONSE.add(None)
    before = len(REQUESTS)
    unnamed = asyncio.run(engine_client.call_query("total amount in France", tables, None, base_url=base))
    ok(unnamed["status"] == "error" and len(REQUESTS) - before == 1,
       "a request without a jobId is never sent twice")
    LOSE_FIRST_RESPONSE.discard(None)

    # Cloud Run answers 429 "no available instance" while every engine instance is busy or a new one is
    # still starting: two of 94 questions run three at a time on one instance got it (2026-10-06). That
    # request never reached the engine; the client asks again until an instance takes it. The engine's
    # own 429 (a principal's rate) is final.
    retry_delays = engine_client.NO_INSTANCE_RETRY_SECONDS
    engine_client.NO_INSTANCE_RETRY_SECONDS = (0.0, 0.0, 0.0)
    try:
        NO_INSTANCE["jobBusy"] = 2
        before = len(REQUESTS)
        busy = asyncio.run(engine_client.call_query("total amount in France", tables, "jobBusy", base_url=base))
        ok(busy["status"] == "answered" and busy["answer"]["rows"] == [[270]],
           f"a question Cloud Run had no instance for is asked again and answers (got {busy.get('status')})")
        ok([request.get("jobId") for request in REQUESTS[before:]] == ["jobBusy"] * 3,
           "it is asked again with the same jobId until an instance takes it")
        NO_INSTANCE["jobFull"] = 4
        full = asyncio.run(engine_client.call_query("total amount in France", tables, "jobFull", base_url=base))
        ok(full["status"] == "error" and full.get("http_status") == 429 and NO_INSTANCE["jobFull"] == 0,
           "past its retries the 429 reaches the caller, which presents it as busy")
        ENGINE_RATE_LIMITED.add("jobRate")
        before = len(REQUESTS)
        limited = asyncio.run(engine_client.call_query("total amount in France", tables, "jobRate", base_url=base))
        ok(limited["status"] == "error" and limited.get("http_status") == 429 and len(REQUESTS) - before == 1,
           "the engine's own 429 is not asked again")
    finally:
        engine_client.NO_INSTANCE_RETRY_SECONDS = retry_delays
        NO_INSTANCE.clear()
        ENGINE_RATE_LIMITED.clear()

    # AUTH IS PER CALL, NOT PER PROCESS. The orchestrator now calls this coroutine in-process while
    # serving concurrent users, so an explicit token must win over the process-wide env fallback —
    # otherwise one user's turn could authenticate as another.
    os.environ["ENGINE_BEARER_TOKEN"] = "process-wide-token"
    try:
        seen = asyncio.run(engine_client.call_query("total amount in France", tables, "jobE",
                                                    base_url=base, token="caller-token"))
        ok(seen["status"] == "answered", "explicit-token call still answers")
        ok(AUTH_SEEN[-1] == "Bearer caller-token",
           f"explicit token must override the env fallback (engine saw {AUTH_SEEN[-1]!r})")
        asyncio.run(engine_client.call_query("total amount in France", tables, "jobF", base_url=base))
        ok(AUTH_SEEN[-1] == "Bearer process-wide-token",
           "with no explicit token the standalone MCP server's env fallback still applies")
    finally:
        os.environ.pop("ENGINE_BEARER_TOKEN", None)

    # One client reused across calls (the per-turn connection reuse the orchestrator relies on).
    async def two_calls_one_connection():
        async with httpx.AsyncClient() as shared:
            a = await engine_client.call_query("total amount in France", tables, "jobG",
                                               base_url=base, client=shared)
            b = await engine_client.call_query("how much did we sell overall", tables, "jobH",
                                               base_url=base, client=shared)
            return a, b
    a, b = asyncio.run(two_calls_one_connection())
    ok(a["status"] == "answered" and b["status"] == "answered",
       "a shared AsyncClient serves multiple calls in one turn")


def test_mcp_server_module_imports():
    """The orchestrator spawns mcp_server/server.py as a stdio SUBPROCESS, so a broken import there
    is invisible to every test that only exercises the pure adapter — which is exactly how an
    unpinned `mcp` resolved 2.0.0 (FastMCP moved) and killed every live /chat turn while this suite
    stayed green. Import the real module so a dependency bump fails the BUILD, not production."""
    import importlib

    try:
        module = importlib.import_module("mcp_server.server")
    except Exception as exc:  # noqa: BLE001 - the failure mode under test is any import error
        ok(False, f"mcp_server.server imports: {type(exc).__name__}: {exc}")
        return
    ok(hasattr(module, "mcp") or hasattr(module, "main"),
       "mcp_server.server exposes its server object after import")


async def _stdio_handshake():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_server.server"],
                                   env=env, cwd=os.getcwd())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return {
                tool.name: tool.inputSchema
                for tool in (getattr(result, "tools", None) or [])
            }


def test_mcp_stdio_handshake():
    """Exercise the same child-process initialize/list_tools path used by /chat."""
    try:
        tools = asyncio.run(_stdio_handshake())
    except Exception as exc:  # noqa: BLE001
        ok(False, f"MCP stdio handshake: {type(exc).__name__}: {exc}")
        return
    ok({"prereasoner_query", "prereasoner_describe"}.issubset(tools),
       "MCP stdio handshake exposes both tools")
    ok(
        "decomposition" in tools["prereasoner_query"].get("properties", {}),
        "external MCP clients can submit the one engine-requested decomposition retry",
    )


def main():
    port = 8811
    srv = _start_stub(port)
    base = f"http://127.0.0.1:{port}"
    try:
        test_shape()
        test_mcp_server_module_imports()
        test_mcp_stdio_handshake()
        run_integration(base)
    finally:
        srv.shutdown()
    print(f"\ntest_mcp: {P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
