"""converse.py — the Sonnet conversational fallback for the /reason rail.

When the deterministic engine cannot run a message as a data query — it returned a CLARIFY (ambiguous), or
the message is META/conversational ("how did you convert germany to Germany?") — the browser calls
POST /api/converse and we return ONE short conversational reply from Sonnet, answered IN the same
conversation (never a page redirect, never a fabricated number). This is the cost-efficient fallback: the
fast deterministic path still answers real data questions with zero LLM cost; Sonnet is spent only here.
"""
from __future__ import annotations

import json

SYSTEM = """You are the conversational layer for PreReasoner, a product that answers questions about a \
user's own spreadsheet by writing and running REAL SQL — never by guessing. The deterministic engine \
already tried the user's latest message and could NOT run it as a data query: either it was ambiguous \
(the engine proposes a rephrasing), or it is a META/conversational question (about the data, the \
reasoning, or how a value was derived).

Reply in ONE short, warm message (1–3 sentences). Rules:
- If the engine proposed a rephrasing, tell the user what you think they meant and offer it plainly \
  ("Did you mean: <proposed>?"). There is a Run button beside your reply; mention they can run it or rephrase.
- If it's a META question about how a value was derived, explain conversationally and accurately. \
  PreReasoner types each column to a Wikidata taxonomy, resolves each cell's surface form to its canonical \
  world entity (e.g. the surface "germany" resolves to the country whose canonical label is "Germany"), then \
  joins the world tables. Point them to the "Resolving" slide in the trace panel, which shows the exact mapping.
- If answering would require a data value, tell them to ask it as a specific question over their columns \
  (a total, count, average, or filter). NEVER invent, estimate, or recall a number — that is exactly the \
  unreliable thing this product replaces.
- Be specific to their actual columns. Do not describe SQL you were not given.
"""


def _schema_text(tables):
    """A compact 'table(col, col, …); …' schema from the request's tables (name+CSV or name+columns)."""
    parts = []
    for t in (tables or []):
        cols = t.get("columns")
        if not cols and t.get("data"):
            first = (str(t["data"]).splitlines() or [""])[0]
            cols = [c.strip() for c in first.split(",") if c.strip()]
        parts.append(f"{t.get('name', 'table')}({', '.join(cols or [])})")
    return "; ".join(parts) or "(no tables uploaded)"


def reply(question, clarify=None, error=None, tables=None, model=None, api_key=None, max_tokens=400):
    """Return a short conversational reply (str). Raises if the Anthropic key/SDK is unavailable."""
    from anthropic import Anthropic
    from engine.config import anthropic_api_key, ANTHROPIC_MODEL

    client = Anthropic(api_key=api_key or anthropic_api_key())
    schema = _schema_text(tables)
    user = f"The user's tables: {schema}\n\nThe user's message: {question!r}\n"
    if clarify:
        user += f"\nThe engine found this ambiguous and proposed a rephrasing: {json.dumps(clarify)}\n"
    if error:
        user += f"\nThe engine returned this error: {error}\n"
    user += "\nReply to the user now, per your instructions."

    resp = client.messages.create(
        model=model or ANTHROPIC_MODEL, max_tokens=max_tokens, system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
