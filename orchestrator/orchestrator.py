"""orchestrator.py — the Sonnet tool loop over the PreReasoner MCP server.

Per chat request we: (1) spawn the MCP server over stdio with the user's Firebase token injected into its
env (identity passthrough, never a tool argument — docs/MCP.md); (2) run a manual Anthropic tool loop so
we control the jobId per `prereasoner_query` call and can capture the full engine trace to return to the
browser; (3) return the assistant reply + one replayable trace per PreReasoner call.

Manual loop (not the SDK tool_runner) on purpose: we need to mint the jobId, inject the session `tables`
(kept out of the LLM's context — the model only ever sees the `question`), and keep the full `views` stack
for the reasoning player while feeding the model only a trimmed result.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from anthropic import AsyncAnthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_server.descriptions import QUERY_DESC, DESCRIBE_DESC
from orchestrator.system_prompt import SYSTEM_PROMPT

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_TOOL_ROUNDS = 8

# Claude-facing tool schemas. NOTE the model only supplies `question` — the orchestrator injects the
# session `tables` and a fresh `jobId` before calling the MCP server (large CSVs + infra IDs stay out of
# the LLM loop).
CLAUDE_TOOLS = [
    {
        "name": "prereasoner_query",
        "description": QUERY_DESC,
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "One single-hop data question (one aggregate/filter/join) over the "
                                   "user's uploaded tables, e.g. 'total amount in France'.",
                }
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prereasoner_describe",
        "description": DESCRIBE_DESC,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _mcp_params(engine_base_url: str, bearer_token: str | None) -> StdioServerParameters:
    env = dict(os.environ)
    env["ENGINE_BASE_URL"] = engine_base_url
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    if bearer_token:
        env["ENGINE_BEARER_TOKEN"] = bearer_token
    else:
        env.pop("ENGINE_BEARER_TOKEN", None)  # local: engine runs with AUTH_TEST_SUB, no token
    cmd = _mcp_cmd()
    return StdioServerParameters(command=cmd[0], args=cmd[1:], env=env, cwd=REPO_ROOT)


def _mcp_cmd() -> list[str]:
    raw = os.environ.get("MCP_SERVER_CMD")
    if raw:
        return json.loads(raw)
    import sys
    return [sys.executable, "-m", "mcp_server.server"]


def _tool_text(result: Any) -> str:
    """Extract the JSON text a FastMCP tool returned from a CallToolResult."""
    content = getattr(result, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "text" or hasattr(block, "text"):
            return block.text
    # Fallback: some SDK versions expose structured content
    sc = getattr(result, "structuredContent", None)
    return json.dumps(sc) if sc is not None else "{}"


def _trim_for_model(shaped: dict[str, Any]) -> dict[str, Any]:
    """What the LLM sees back: the value + status + clarify, NOT the heavy views/rows stack (that goes to
    the reasoning player, not the context window)."""
    out = {"status": shaped.get("status")}
    if shaped.get("answer") is not None:
        out["answer"] = shaped["answer"]
    if shaped.get("sql") is not None:
        out["sql"] = shaped["sql"]
    if shaped.get("clarify") is not None:
        out["clarify"] = shaped["clarify"]
    if shaped.get("error") is not None:
        out["error"] = shaped["error"]
    return out


async def run_chat(user_message: str, tables: list[dict], history: list[dict], *,
                   engine_base_url: str, bearer_token: str | None,
                   api_key: str, model: str, turn_id: str | None = None,
                   emit=None, conversation_id: str | None = None) -> dict[str, Any]:
    """Run one chat turn. `history` is a lean transcript [{role, content:str}, ...]; `tables` is the
    session's inline CSVs. Returns {reply, traces, history, conversation_id}.

    `conversation_id` keeps every engine call on ONE conversation schema; the FIRST call mints one if none
    was passed and we capture + reuse it for the rest of the session (and return it to the browser).

    LIVE STREAMING (optional): when `turn_id` + `emit` are supplied, each `prereasoner_query` call runs
    the engine under a DERIVABLE jobId `<turn_id>_<i>` and the call is ANNOUNCED on the turn's RTDB node
    (`emit("calls/<i>", {jobId, question})`) BEFORE it runs — so the browser, subscribed to the turn node,
    discovers each engine call and subscribes to its live `/runs/{uid}/{jobId}` trace. The engine streams
    that trace exactly as on the direct path. The final Sonnet text + terminal status are emitted too.
    `emit` is best-effort (a no-op when RTDB is unset) — streaming must never break the answer."""
    client = AsyncAnthropic(api_key=api_key)
    traces: list[dict[str, Any]] = []
    call_idx = 0                                             # per-turn engine-call counter (drives the jobIds)
    conv = conversation_id                                   # ONE conversation for the whole session (captured from the first call if new)

    def _emit(node, value):
        if emit:
            try:
                emit(node, value)
            except Exception:                                # noqa: BLE001 — never break the answer on a stream write
                pass

    async with stdio_client(_mcp_params(engine_base_url, bearer_token)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Work on a local copy of the full block-level message list for the tool loop.
            messages: list[dict[str, Any]] = [
                {"role": m["role"], "content": m["content"]} for m in (history or [])
            ]
            messages.append({"role": "user", "content": user_message})

            final_text = ""
            for _ in range(MAX_TOOL_ROUNDS):
                resp = await client.messages.create(
                    model=model,
                    max_tokens=8192,
                    system=SYSTEM_PROMPT,
                    thinking={"type": "adaptive"},
                    tools=CLAUDE_TOOLS,
                    messages=messages,
                )
                # Append the assistant turn verbatim (thinking blocks preserved for same-turn continuation).
                messages.append({"role": "assistant",
                                 "content": [b.model_dump() for b in resp.content]})

                if resp.stop_reason != "tool_use":
                    final_text = "".join(b.text for b in resp.content if b.type == "text").strip()
                    break

                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    if block.name == "prereasoner_query":
                        # Derivable per-call jobId so the browser can subscribe live; announce BEFORE the call.
                        job_id = f"{turn_id}_{call_idx}" if turn_id else uuid.uuid4().hex
                        question = (block.input or {}).get("question", "")
                        print(f"[chat] turn={turn_id} call={call_idx} rewrote -> {question!r}", flush=True)
                        _emit(f"calls/{call_idx}", {"jobId": job_id, "question": question})
                        call_idx += 1
                        args = {"question": question, "tables": tables, "job_id": job_id}
                        if conv:
                            args["conversation_id"] = conv
                        result = await session.call_tool("prereasoner_query", args)
                        shaped = json.loads(_tool_text(result))
                        if not conv and shaped.get("conversation_id"):
                            conv = shaped["conversation_id"]  # first call minted it -> reuse for the rest of the session
                            _emit("conversation_id", conv)    # stream it NOW, mid-turn — the browser unsubscribes from the
                                                              # turn node on 'status:done' (workbook settle()), so the
                                                              # post-'done' emit below would be MISSED: no URL, no snapshot save
                        traces.append({"jobId": job_id, "question": question, "engine": shaped})
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(_trim_for_model(shaped)),
                            "is_error": shaped.get("status") == "error",
                        })
                    elif block.name == "prereasoner_describe":
                        result = await session.call_tool("prereasoner_describe", {"tables": tables})
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": _tool_text(result),
                        })
                    else:
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": f"unknown tool {block.name}", "is_error": True,
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                final_text = final_text or "I wasn't able to complete that within the step budget."

    _emit("reply", final_text)                               # the Sonnet text for the rail
    _emit("status", "done")                                  # terminal — the browser stops waiting

    # Lean cross-turn transcript: user + assistant final text only (avoids block-replay pitfalls; the
    # reasoning traces are returned separately and stored per-message by the browser).
    new_history = list(history or [])
    new_history.append({"role": "user", "content": user_message})
    new_history.append({"role": "assistant", "content": final_text})
    _emit("conversation_id", conv or "")                     # stream it so the browser can persist + put it in the URL
    return {"reply": final_text, "traces": traces, "history": new_history, "conversation_id": conv}
