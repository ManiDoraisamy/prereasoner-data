"""Prereasoner MCP server (stdio) — exposes the auditable engine as MCP tools.

Run: python -m mcp_server.server   (stdio transport; launched by the orchestrator per session)

The tool DESCRIPTIONS below carry the routing-discipline rules (docs/MCP.md) so ANY MCP client — not
just our orchestrator — inherits them. Tools return a JSON string; the client json.loads the text content.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_server import engine_client
from mcp_server.descriptions import QUERY_DESC, DESCRIBE_DESC

mcp = FastMCP("prereasoner")


@mcp.tool(description=QUERY_DESC)
def prereasoner_query(question: str, tables: list, job_id: str | None = None,
                      conversation_id: str | None = None) -> str:
    """See description. `tables` = [{name, data(raw CSV)}], inline (no dataset_id)."""
    return json.dumps(engine_client.call_query(question, tables or [], job_id, conversation_id))


@mcp.tool(description=DESCRIBE_DESC)
def prereasoner_describe(tables: list) -> str:
    """See description. `tables` = [{name, data(raw CSV)}], inline; identity is transport context."""
    return json.dumps(engine_client.call_describe(tables or []))


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
