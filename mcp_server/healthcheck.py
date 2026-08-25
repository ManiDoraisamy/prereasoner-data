"""Build-time handshake for the exact MCP server shipped in the chat image."""
from __future__ import annotations

import asyncio
import importlib
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"prereasoner_query", "prereasoner_describe"}


async def _handshake() -> set[str]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        env=dict(os.environ),
        cwd=os.getcwd(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return {tool.name for tool in (getattr(result, "tools", None) or [])}


def main() -> int:
    tools = asyncio.run(_handshake())
    missing = EXPECTED_TOOLS - tools
    if missing:
        raise SystemExit(f"MCP handshake missing tools: {sorted(missing)}; got {sorted(tools)}")
    importlib.import_module("orchestrator.server")
    print("orchestrator import OK")
    print(f"MCP handshake OK: {', '.join(sorted(tools))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
