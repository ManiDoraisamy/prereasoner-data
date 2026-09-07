"""Prereasoner MCP server — a thin, typed wrapper that exposes the auditable engine as MCP tools.

See docs/MCP.md. This package adds NO learned steps and NO state: it forwards a question +
inline tables to the engine's POST /api/reason (and /api/dimension for `describe`) and shapes the
response for a tool caller. `engine_client` is async and is awaited by BOTH entry points: the chat
orchestrator in-process (token passed explicitly per call) and the standalone stdio server for
external MCP clients (token from ENGINE_BEARER_TOKEN in its per-process env).
"""
