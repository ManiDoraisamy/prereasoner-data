"""Single source of truth for the tool descriptions — imported by both the MCP server (mcp_server/server.py)
and the orchestrator's Claude-facing tool schemas (orchestrator/orchestrator.py), so the routing-discipline
rules (docs/MCP.md) live in exactly one place and any client inherits them."""

QUERY_DESC = """\
Answer ONE data question over the user's uploaded tables by writing and running real SQL (joined to a
Wikidata world model when the question names a place/type the sheet doesn't contain), and return the
value plus the exact SQL and the reasoning stack.

WHEN TO CALL THIS (routing discipline — you are the unreliable component, so defer by default):
1. ANY factual number about the user's data must come from this tool — never from your own arithmetic or
   memory. This holds even when the number is buried inside a conversational or strategic question.
2. If the data is already visible in the conversation, you STILL call this tool instead of computing
   in-head. In-head arithmetic is exactly the unreliable thing this tool replaces.
3. Follow-on math on a result (e.g. "the 270 you got, times 1.15") is ANOTHER call to this tool, not
   in-head work.
4. If the result has status "clarify", surface it to the user verbatim — do NOT fill the gap with a
   plausible answer. The clarification is the product.

INPUT: a single-hop, directly-expressible question (one aggregate/filter/join). For a multi-hop question,
call this tool once per hop and pass intermediate values forward.
OUTPUT: {status: "answered"|"clarify"|"error", answer:{columns,rows}, sql, clarify}."""

DESCRIBE_DESC = """\
Report what PreReasoner believes each column of the user's tables IS (city / hospital / free-text /
numeric …), so you know the coverage boundary before routing a question to `prereasoner_query`. Use it
when unsure whether a question's entities are ones PreReasoner can type. It reports the model's column
typing, not which cells resolved to specific world entities."""
