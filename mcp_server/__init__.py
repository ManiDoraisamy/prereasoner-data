"""PreReasoner MCP server — a thin, typed wrapper that exposes the auditable engine as MCP tools.

See docs/MCP.md. This package adds NO learned steps and NO state: it forwards a question +
inline tables to the engine's POST /api/reason (and /api/dimension for `describe`) and shapes the
response for a tool caller. Identity is passed through as transport context (ENGINE_BEARER_TOKEN in env).
"""
