# MCP Implementation — v1 (reasoning in the LLM)

> **Audience: Claude Code.** Build spec for wrapping PreReasoner as an MCP tool that an LLM
> orchestrator calls. This is the **launch** version. Verify all current-behavior claims against
> the code (`runtime20/*`, `/reason`, `/world`, the `world.words` / `wikipedia."<type>"` schema)
> before acting. Code wins where this doc disagrees.
>
> **Honest scope of this version (state it, don't hide it):** in v1 the LLM does the
> *decomposition* — it decides how a multi-hop question breaks into steps. That decomposition
> happens inside the LLM's forward pass and is **not auditable**. PreReasoner makes each
> **execution** auditable (every step it runs emits a trace), but the **plan** that chose those
> steps is a black-box LLM output. So v1 delivers *interpretable execution, opaque reasoning*.
> That is a real and shippable improvement over a pure LLM (the numbers are derived, not guessed),
> but it is **not** the full auditability claim. The full claim requires v2 (see `mcp-future.md`),
> where PreReasoner derives the plan too. Do not let v1 marketing overstate this: v1 is "the
> numbers are computed and checkable," not "the reasoning is auditable."

---

## What v1 is

An LLM (Claude/Sonnet or whatever the caller wires up) is the **orchestrator**. PreReasoner is an
**MCP tool** it calls. The orchestrator handles conversation, multimodal input (PDF/image/Excel →
CSV), clarification, tool selection, and security. When a question needs a **fact or computation
over the user's data**, the orchestrator calls PreReasoner, which returns a **traceable answer**
(the SQL, the resolved QIDs, the join, the value) that the orchestrator explains back in prose.

The division of labor, stated as the invariant to protect:

- **In the orchestrator (LLM):** conversation, multimodal ingest, clarification, tool selection,
  security, and — in v1 only — **decomposing multi-hop questions into single-hop calls**.
- **In PreReasoner (the tool):** typing, resolution, join construction, operator selection, and
  **execution of each hop** as auditable SQL with a streamed trace.

The reasoning that PreReasoner *does* expose in v1 is the **single-hop derivation** — for one
resolved step, the typing → resolution → SQL is fully auditable (that is the existing `/reason`
and `/world` behavior). What is *not* auditable in v1 is the **cross-hop decomposition** — the
LLM's choice of which hops, in what order.

---

## MCP tool surface

Expose PreReasoner as an MCP server with a small, typed tool set. Verify the exact endpoint
signatures against `runtime20` before finalizing; these are the intended shapes.

### Tool: `prereasoner_query`
The primary tool. Takes a **single-hop or directly-expressible** data question plus a reference to
the user's uploaded data, returns the auditable answer.

Input:
- `question` (string): a natural-language data question that resolves to one aggregate/filter/join
  over the user's tables joined to the world model. E.g. "total amount in France", "how many
  hospitals in Texas".
- `dataset_id` (string): handle to the user's uploaded tables (already parsed/related in the
  session — see session handling below).

Output (structured, so the orchestrator can both use the value and show the trace):
- `answer`: the scalar/table result.
- `sql`: the exact SQL executed (the auditable artifact).
- `resolution`: per-cell QID resolution with **candidates and confidence** (not just the winner —
  see invariant below).
- `trace_url`: the Firebase RTDB path where the live step-by-step trace streamed.
- `status`: `answered` | `clarify` | `unresolved`. **`clarify` and `unresolved` are first-class
  outcomes, not errors** (see routing discipline).

### Tool: `prereasoner_upload`
Registers the user's data (CSV, or CSV produced by the orchestrator from PDF/Excel/image) and
returns a `dataset_id`. Runs parse + relate (FK discovery). Deterministic.

### Tool: `prereasoner_describe`
Returns what PreReasoner *can* answer about a dataset: the typed columns, which resolved to world
entities (the 42 live taxonomy leaves), which are unresolved/company-specific. This lets the
orchestrator know the coverage boundary **before** it decomposes a question — critical for routing
(don't route a question to PreReasoner whose entities it can't type).

---

## Routing discipline — the actual crux of v1

The whole trustworthiness of v1 lives here, because **the LLM decides when to call the tool**, and
the LLM is the unreliable component. The failure mode is not the LLM failing to recognize an
obvious calculation — orchestrators route those fine. The failure modes are narrower and must be
handled explicitly in the MCP tool description and the orchestrator system prompt:

1. **Numbers buried in conversational questions.** "Does our French revenue justify hiring in
   Europe?" contains a must-be-right number (French revenue) inside a strategic question. The LLM
   will answer the whole thing fluently and *estimate the number* rather than call the tool. Route
   on **truth-bearing**, not surface form: *any* factual number about the user's data must come
   from a `prereasoner_query` call, never from the model's own arithmetic or memory.

2. **Confidence bypass on recall.** If the data is in context from earlier, the LLM will read it
   and compute in-head instead of calling the tool. Its in-head arithmetic is the exact
   unreliable thing the tool replaces. System-prompt rule: **"When a number about the user's data
   must be correct, you do not compute or recall it — you call `prereasoner_query`."**

3. **Follow-on math.** LLM calls the tool, gets 270, then computes "270 × 1.15" in-head. That
   post-processing is unaudited. Rule: follow-on arithmetic on a tool result is **another tool
   call**, not in-head work.

4. **Confabulation on failure.** When the tool returns `clarify` or `unresolved`, the LLM's
   failure mode is to *fill the gap* with a plausible answer rather than surface the refusal.
   Rule: **`clarify`/`unresolved` must be passed through to the user, never smoothed over.** The
   refusal is the product — it is the "asks instead of guessing" promise. Do not let the
   orchestrator override it.

Encode 1–4 in **both** the MCP tool description (so any orchestrator sees them) and a recommended
system-prompt snippet shipped with the server. The honest limit: you cannot *guarantee* the LLM
calls the tool — that is v1's irreducible weakness, and it is the reason v2 exists. Make deferral
the default and make bypass expensive; do not claim it is impossible to bypass.

---

## Multi-hop in v1 (the part that is NOT auditable, handled honestly)

For "second name of the third person in gold tier" — a multi-hop question — v1 has the LLM
**decompose** it into a sequence of `prereasoner_query` calls (resolve gold-tier set → order →
take third → project name), sequencing them and passing intermediate results between calls.

Each call is auditable. The **decomposition is not** — it is the LLM's forward-pass reasoning.
This is acceptable for v1 *only if labeled as such*: the orchestrator should present the sequence
of executed steps (which are traceable) but must not claim the *choice* of steps was derived. If
the user needs the decomposition itself to be auditable, that is v2.

Do not build a "separate explainability agent" that narrates the LLM's decomposition. That is
post-hoc explanation of a black-box reasoning step — exactly the interpretability-by-explanation
this project rejects. A second LLM explaining the first LLM's plan can be unfaithful. If the
decomposition needs to be auditable, the answer is to **derive** it (v2), not to **narrate** it.

---

## Session & security

- **Data scope:** `dataset_id` is bound to the verified user (Google `sub` = per-user Postgres
  schema, as today). The MCP layer must pass the verified identity through; never trust a
  client-supplied user id (no IDOR).
- **Trace ownership:** `trace_url` points to `/runs/{uid}/{jobId}`, owner-read-only, keyed by
  verified Firebase uid (existing behavior).
- **Orchestrator does not see raw data it shouldn't:** PreReasoner returns the answer + trace, not
  a dump of the user's tables. Keep the tool outputs scoped to the query.

---

## Carry-over invariants (from ROADMAP.md — do not relax)

- Every layer emits **reason + alternatives + confidence**. `resolution` in the tool output must
  surface the candidate set and tiebreak, not just the winning QID.
- **Determinism where structure exists** (joins, FK, SQL assembly) stays deterministic. The MCP
  wrapper does not add learned steps.
- **No generation in the core.** The tool derives; the orchestrator may generate prose *around*
  the derived answer, but never the number.
- The clarify gate is a **feature**, surfaced through `status: clarify`. Protect it end to end.

---

## Done when

- An LLM orchestrator (via MCP) can: upload user data, ask a single-hop question, and get back a
  value + SQL + resolution-with-candidates + live trace.
- A multi-hop question executes as a sequence of auditable single-hop calls, with the sequence
  shown to the user and the **decomposition honestly labeled as LLM-chosen, not derived**.
- `clarify`/`unresolved` pass through to the user intact in an adversarial test (a deliberately
  ambiguous query does not get a confabulated answer).
- The routing-discipline rules (1–4) are present in both the tool description and the shipped
  system-prompt snippet.
- No "explainability agent" exists. Reasoning that is exposed is derived (single-hop); reasoning
  that is not derived (multi-hop decomposition) is labeled as opaque, not narrated.
