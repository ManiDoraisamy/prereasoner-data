"""The Sonnet orchestrator — the conversational layer that calls PreReasoner as an MCP tool.

See mcp-now.md §1, §5, §7. The orchestrator does conversation / routing / security; PreReasoner (the
engine, reached through the MCP server) does the reasoning + execution and stays the auditable core.
"""
