"""The Sonnet orchestrator — the conversational layer that calls Prereasoner as an MCP tool.

See docs/MCP.md. The orchestrator does conversation / routing / security; Prereasoner (the
engine, reached through the MCP server) does the reasoning + execution and stays the auditable core.
"""
