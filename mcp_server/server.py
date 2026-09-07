"""Prereasoner MCP server (stdio) — exposes the auditable engine as MCP tools.

Run: python -m mcp_server.server   (stdio transport; launched by EXTERNAL MCP clients — Claude
Desktop, IDEs, other agents. The chat orchestrator does NOT spawn this: it awaits the same
`engine_client` coroutines in-process, so both entry points share one engine contract.)

The tool DESCRIPTIONS below carry the routing-discipline rules (docs/MCP.md) so ANY MCP client
inherits them. Tools return a JSON string; the client json.loads the text content.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_server import engine_client
from mcp_server.descriptions import QUERY_DESC, DESCRIBE_DESC

mcp = FastMCP("prereasoner")


@mcp.tool(description=QUERY_DESC)
async def prereasoner_query(question: str, tables: list, job_id: str | None = None,
                            conversation_id: str | None = None,
                            dataset_ops: list | None = None) -> str:
    """See description. `tables` = [{name, data(raw CSV)}], inline (no dataset_id). `dataset_ops`
    carries conversation-stated measure metadata (set/clear_measure_metadata) per
    docs/DATASET_FORMATTER.md — the engine validates against its closed grammar."""
    return json.dumps(await engine_client.call_query(question, tables or [], job_id, conversation_id,
                                                     dataset_ops=dataset_ops))


@mcp.tool(description=DESCRIBE_DESC)
async def prereasoner_describe(tables: list) -> str:
    """See description. `tables` = [{name, data(raw CSV)}], inline; identity is transport context."""
    return json.dumps(await engine_client.call_describe(tables or []))


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
