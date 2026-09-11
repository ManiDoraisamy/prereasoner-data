"""Single source of truth for the tool descriptions — imported by both the MCP server (mcp_server/server.py)
and the orchestrator's Claude-facing tool schemas (orchestrator/orchestrator.py), so the routing-discipline
rules (docs/MCP.md) live in exactly one place and any client inherits them."""

QUERY_DESC = """\
Answer ONE data question over the user's uploaded tables by writing and running real SQL (joined to
registered reference sources when the question needs facts the sheet does not contain), and return the
value plus the exact SQL and the reasoning stack.

WHEN TO CALL THIS (routing discipline — you are the unreliable component, so defer by default):
1. ANY factual number about the user's data must come from this tool — never from your own arithmetic or
   memory. This holds even when the number is buried inside a conversational or strategic question.
2. If the data is already visible in the conversation, you STILL call this tool instead of computing
   in-head. In-head arithmetic is exactly the unreliable thing this tool replaces.
3. Follow-on math on a result (e.g. "the 270 you got, times 1.15") is ONE new call to this tool, not
   in-head work. Express the complete calculation in that call.
4. If the result has status "clarify", surface it to the user verbatim — do NOT fill the gap with a
   plausible answer. The clarification is the product.
5. Call once without decomposition. Only if the engine returns status "decompose", retry the exact
   same question and named-workbook decision once with two to four natural-language subquestions and
   a closed cross/anti_join topology. Never supply SQL, Python, schema-identifier fields, or merge keys;
   each leaf remains an ordinary natural-language question.

INPUT: one complete data question plus a named-workbook decision. Use `create` with a concise snake-case
slug for a distinct analysis; use `modify` with the exact existing analysis_id and slug when the user is
changing or extending that analysis; use `inspect` only to reopen an existing revision. The engine owns
IDs, validates ownership, and performs the necessary joins, reference lookups, filters, grouping, unit
conversion, and typed arithmetic as one inspectable computation. Do not turn branches into independent
tool calls; the bounded retry still creates one engine-owned DAG. Answered, clarify, and error are terminal.
OUTPUT: {status: "answered"|"decompose"|"clarify"|"error", answer:{columns,rows}, sql, clarify,
analysis:{analysis_id,slug,revision,action}}."""

DESCRIBE_DESC = """\
Report what Prereasoner believes each column of the user's tables IS (city / hospital / free-text /
numeric …), so you know the coverage boundary before routing a question to `prereasoner_query`. Use it
when unsure whether a question's entities are ones Prereasoner can type. It reports the model's column
typing, not which cells resolved to specific world entities."""
