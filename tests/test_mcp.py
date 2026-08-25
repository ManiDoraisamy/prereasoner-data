"""test_mcp.py — the PreReasoner MCP server layer.

Two parts, both dependency-light (no seeded Postgres, no model weights, no Anthropic key):
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

from tests.stub_engine import H
from mcp_server import engine_client
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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

    clar = engine_client.shape_reason_response(
        {"question": "q", "clarify": True, "proposed": "by country", "dropped": ["region"],
         "original_sql": "GROUP BY region"}, "j")
    ok(clar["status"] == "clarify", "clarify:true -> status 'clarify'")
    ok(clar["clarify"]["proposed"] == "by country", "clarify carries proposed")
    ok(clar["clarify"]["dropped"] == ["region"], "clarify carries dropped")
    ok("answer" not in clar, "clarify has no answer")

    err_field = engine_client.shape_reason_response({"question": "q", "error": "guard: no", "result": None}, "j")
    ok(err_field["status"] == "error", "error field -> status 'error'")
    ok(err_field["error"] == "guard: no", "error message surfaced")

    err_body = engine_client.shape_reason_response({"error": "sign in required"}, "j")
    ok(err_body["status"] == "error", "top-level {error} body -> status 'error'")

    empty = engine_client.shape_reason_response({}, "j")
    ok(empty["status"] == "error", "empty (no result/clarify/error) -> 'error', never a fake answer")


# ---------------- (B) integration: against the stub engine ----------------
def test_integration(base):
    print("[B] engine_client against the stub engine")
    tables = [{"name": "customers", "data": "customer_id,city\n1,Paris\n2,Lyon\n3,Berlin\n"},
              {"name": "orders", "data": "order_id,customer_id,amount\n10,1,120\n11,2,150\n12,3,90\n"}]

    r = engine_client.call_query("total amount in France", tables, "jobA", base_url=base)
    ok(r["status"] == "answered", "France query -> answered")
    ok(r["answer"]["rows"] == [[270]], "France total == 270")
    ok(len(r.get("views", [])) == 3, "France answer carries the 3-view stack")
    ok(r["trace"]["jobId"] == "jobA", "trace jobId round-trips")

    r2 = engine_client.call_query("total revenue by region", tables, "jobB", base_url=base)
    ok(r2["status"] == "clarify", "ambiguous 'region' query -> clarify")
    ok("region" in (r2["clarify"].get("dropped") or []), "clarify drops 'region'")

    r3 = engine_client.call_query("how much did we sell overall", tables, "jobC", base_url=base)
    ok(r3["status"] == "answered", "generic query -> answered")

    d = engine_client.call_describe([{"name": "customers", "data": "city\nParis\nLyon\n"}], base_url=base)
    ok("tables" in d and d["tables"], "describe returns per-table readout")
    ok(d["tables"][0].get("columns") is not None, "describe reports columns")

    # unreachable engine -> a clean error, never a raised exception
    bad = engine_client.call_query("x", tables, "jobD", base_url="http://127.0.0.1:9", timeout=2)
    ok(bad["status"] == "error", "unreachable engine -> status 'error' (no crash)")


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
            return {tool.name for tool in (getattr(result, "tools", None) or [])}


def test_mcp_stdio_handshake():
    """Exercise the same child-process initialize/list_tools path used by /chat."""
    try:
        names = asyncio.run(_stdio_handshake())
    except Exception as exc:  # noqa: BLE001
        ok(False, f"MCP stdio handshake: {type(exc).__name__}: {exc}")
        return
    ok({"prereasoner_query", "prereasoner_describe"}.issubset(names),
       "MCP stdio handshake exposes both tools")


def main():
    port = 8811
    srv = _start_stub(port)
    base = f"http://127.0.0.1:{port}"
    try:
        test_shape()
        test_mcp_server_module_imports()
        test_mcp_stdio_handshake()
        test_integration(base)
    finally:
        srv.shutdown()
    print(f"\ntest_mcp: {P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
