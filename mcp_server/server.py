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
from mcp_server.descriptions import DESCRIBE_DESC, QUERY_DESC

mcp = FastMCP("prereasoner")


@mcp.tool(description=QUERY_DESC)
async def prereasoner_query(question: str, tables: list, job_id: str | None = None,
                            conversation_id: str | None = None,
                            action: str | None = None, slug: str | None = None,
                            analysis_id: str | None = None, revision: int | None = None) -> str:
    """See description. `tables` = [{name, data(raw CSV)}], inline (no dataset_id).

    Dataset claims are emitted only by the authenticated chat orchestrator because the engine
    requires a principal-bound transport attestation; arbitrary MCP clients cannot mint them.
    """
    analysis = ({"action": action, "slug": slug, "analysis_id": analysis_id,
                 "revision": revision} if action and slug else None)
    if analysis:
        analysis = {key: value for key, value in analysis.items() if value is not None}
    return json.dumps(await engine_client.call_query(
        question, tables or [], job_id, conversation_id, analysis=analysis,
    ))


@mcp.tool(description=DESCRIBE_DESC)
async def prereasoner_describe(tables: list) -> str:
    """See description. `tables` = [{name, data(raw CSV)}], inline; identity is transport context."""
    return json.dumps(await engine_client.call_describe(tables or []))


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
