"""PreReasoner MCP server — a thin, typed wrapper that exposes the auditable engine as MCP tools.

See mcp-now.md §2 and §7. This package adds NO learned steps and NO state: it forwards a question +
inline tables to the engine's POST /api/reason (and /api/dimension for `describe`) and shapes the
response for a tool caller. Identity is passed through per mcp-now.md §5 (ENGINE_BEARER_TOKEN in env).
"""
