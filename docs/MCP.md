# MCP And Conversational Orchestration

PreReasoner exposes the same engine through HTTP and MCP. MCP is an adapter, not a second reasoning engine.

## Components

```text
client -> orchestrator/server.py -> orchestrator/orchestrator.py
                                  -> mcp_server/server.py
                                  -> mcp_server/engine_client.py
                                  -> POST /api/reason or /api/dimension
```

- `mcp_server` publishes `prereasoner_query` and `prereasoner_describe` over stdio.
- `engine_client.py` maps the engine's answer/clarify/error variants into a stable tool envelope.
- `orchestrator` runs an optional Anthropic tool loop. It decides when to call a tool and how to present the result.
- Numbers and tables must come from the engine tool response. The orchestrator may not calculate or invent them.

## Identity

Identity is transport context, never a tool argument chosen by the model. The orchestrator verifies the incoming
Firebase bearer token and passes it to the engine through the MCP subprocess environment. The engine performs its own
verification and conversation ownership checks.

Local tests can use the engine's `AUTH_TEST_SUB` bypass. Production refuses that bypass on Cloud Run.

## Tool Outcomes

`prereasoner_query` normalizes engine responses into one of:

- `answered`: result rows, SQL, views, and trace metadata;
- `clarify`: the engine rejected a query that would drop part of the question;
- `error`: transport, server, or malformed-response failure.

An empty or unknown response shape is an error, never a fabricated answer. Clarification is passed through rather
than smoothed into a guess.

## Routing Discipline

The orchestrator calls the query tool when a response needs a fact derived from user data. It can answer greetings or
explain the interface without a tool call. A question that needs multiple engine calls is executed sequentially
because one engine instance protects its shared model context with `WORLD_LOCK`.

This conversational routing does not replace the engine's deterministic `engine.routing.route()` decision between
own-data AST and world-aware execution. Serving and Spider evaluation continue to share that one engine route.

## Running Locally

Start the engine, then provide `ANTHROPIC_API_KEY` and run the orchestrator service described in
[`../docker-compose.yml`](../docker-compose.yml). The MCP and orchestrator contract tests are:

```powershell
python -m tests.test_mcp
python -m tests.test_orchestrator
```

The MCP adapter adds no model artifacts, training pipeline, database schema, or persistent state.
