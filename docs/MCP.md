# MCP And Conversational Orchestration

Prereasoner exposes the same engine through HTTP and MCP. MCP is an adapter, not a second reasoning engine.

## Components

```text
chat:         client -> orchestrator/server.py -> orchestrator/orchestrator.py
                                                -> mcp_server/engine_client.py (awaited in-process)
                                                -> POST /api/reason or /api/dimension
external MCP: MCP client -> mcp_server/server.py (stdio)
                          -> mcp_server/engine_client.py
                          -> POST /api/reason or /api/dimension
```

- `engine_client.py` is the ONE engine contract: async coroutines that map the engine's
  answer/clarify/error variants into a stable tool envelope. Both entry points await it.
- `mcp_server/server.py` publishes `prereasoner_query` and `prereasoner_describe` over stdio for
  EXTERNAL MCP clients. The chat orchestrator does not spawn it — a per-turn Python subprocess cost
  a measured 0.86s of interpreter startup to relay an HTTP call the orchestrator can make itself.
- `orchestrator` runs an optional Anthropic tool loop. It decides when to call a tool and how to present the result.
- Numbers and tables must come from the engine tool response. The orchestrator may not calculate or invent them.
- Before a follow-up, the orchestrator reads the ownership-scoped `/api/analyses` catalog. The catalog contains
  only ids, slugs, latest questions, revision numbers, and stale flags; it does not duplicate workbook rows.

## Identity

Identity is transport context, never a tool argument chosen by the model. The orchestrator verifies the incoming
Firebase bearer token and passes it EXPLICITLY per `engine_client` call (`token=`), never via process
environment — env is shared across concurrent turns of different users. The standalone stdio server,
which serves one client per process, receives the token as `ENGINE_BEARER_TOKEN` in its environment
from whichever MCP client launched it. The engine performs its own verification and conversation
ownership checks either way; an explicit `token=` always overrides the env fallback
(pinned by `tests/test_mcp.py`).

Local tests can use the engine's `AUTH_TEST_SUB` bypass only with `APP_ENV=development` or `APP_ENV=test`.
Production defaults to fail-closed. `/api/dimension` is authenticated too.

## Tool Outcomes

The HTTP `/chat` body accepts `use=sql|py|both` (and the internal aliases documented in
[DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md)). This is transport context: `_run_turn`
passes it to every `engine_client.call_query`, which sends it in the `/api/reason` JSON body.
It is not a mode chosen by Sonnet. The standalone MCP tool schema currently does not expose a
`use` argument; the shared Python HTTP adapter does.

The adapter preserves `execution` metadata on answers and errors, and `deterministic` source and
manifest records on answers. `execution.verified=true` means the shared-plan stage comparison
succeeded. A normal SQL answer, a saved snapshot, and an unsupported-mode error are not new parity
verification. See the execution contract for the transient direct-request slug and fallback rules.

`prereasoner_query` normalizes engine responses into one of:

- `answered`: result rows, SQL, views, and trace metadata;
- `decompose`: a non-terminal request for one bounded semantic split; no answer rows are returned;
- `clarify`: the engine rejected a query that would drop or ambiguously realize part of the question;
- `error`: transport, server, or malformed-response failure.

Every orchestrated query also carries `action` and `slug`. `modify` and `inspect` carry the exact `analysis_id` from the catalog;
`inspect` may name a revision. These are conversational intent fields, not authority. The engine canonicalizes the
slug, verifies conversation ownership, allocates IDs/revisions, and returns the authoritative `analysis` object.

An empty or unknown response shape is an error, never a fabricated answer. Clarification is passed through rather
than smoothed into a guess. Its `reason`, `unmet`, and typed evidence fields survive HTTP, MCP, RTDB streaming, and
the browser fallback; adapters must not reduce it to a generic rephrase message.

## Routing Discipline

The orchestrator calls the query tool when a response needs a fact derived from user data. It can answer greetings or
explain the interface without a tool call. One user data question is sent as one complete engine query; joins,
reference lookups, filters, grouping, conversion, and arithmetic are steps inside that query, not separate tool calls.
There is one exception after deterministic evidence: if the engine returns `decompose`, the orchestrator may call
the same question/action/slug once more with two to four natural-language leaves and a closed
`cross`/`anti_join` graph. Runtime guards reject proactive decomposition, changed analysis request identity, dead nodes,
unbounded Cartesian products, and a second retry. The engine plans every leaf and emits the final SQL/Python DAG;
the model never supplies identifiers, code, merge keys, or intermediate result rows.
Short follow-ups that name a data value, such as `how about Belgium?`, are still data questions even when they
repeat the current value: the orchestrator rewrites them into a complete query and calls the engine again. It must
not replace a numeric answer with a conversational confirmation.
After the engine returns `answered`, `clarify`, or `error`, the orchestrator performs one tool-disabled presentation
round. This keeps natural phrasing in the language model while making a terminal engine outcome structurally unable
to start a reformulation loop.

Workbook routing is separate from SQL routing. A qualifier change such as `in US dollars` modifies the existing
analysis; a new output grain such as `top selling products` creates another analysis. Sonnet proposes that choice
from conversation context and the catalog. The deterministic engine still owns SQL, calculations, authorization,
view naming, and revision persistence.

This conversational routing does not replace the engine's deterministic `engine.routing.route()` decision between
own-data AST and world-aware execution. Serving and Spider evaluation continue to share that one engine route.

## Running Locally

Start the engine, then provide `ANTHROPIC_API_KEY` and run the orchestrator service described in
[`../docker-compose.yml`](../docker-compose.yml). The MCP and orchestrator contract tests are:

```powershell
python -m tests.test_mcp
python -m tests.test_orchestrator_unit
python -m tests.test_orchestrator
```

The exact chat image also runs `python -m mcp_server.healthcheck` during its Docker build. That check
spawns the real stdio server, completes MCP `initialize`, and verifies both published tools; an import-only
test is insufficient because the server is a child process in production.

The MCP adapter adds no model artifacts, training pipeline, or persistent state of its own. Named workbook state is
owned by the engine's `chat.analysis` and `chat.analysis_revision` tables.
