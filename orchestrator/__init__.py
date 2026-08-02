"""The Sonnet orchestrator — the conversational layer that calls PreReasoner as an MCP tool.

See docs/MCP.md. The orchestrator does conversation / routing / security; PreReasoner (the
engine, reached through the MCP server) does the reasoning + execution and stays the auditable core.
"""
