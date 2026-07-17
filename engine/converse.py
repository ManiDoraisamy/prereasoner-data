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
user's own spreadsheet by writing and running REAL SQL — never by guessing. You run in one of two modes,
told to you below.

PRESENT mode — the engine already COMPUTED a correct answer (given to you), and the user's phrasing is
conversational/emotional, so a bare number would feel cold. Reply in warm, human text (1–3 sentences) that
USES the computed number verbatim — attribute it ("that comes to …", "you're at …"). You may add light,
factual framing, but do NOT invent trends, comparisons, or numbers you were not given, and do NOT change the
computed value. The full derivation (SQL + steps) is shown in the panel beside the chat, so you don't need to
restate the SQL.

FALLBACK mode — the engine could NOT run the message as a data query: it was ambiguous (a rephrasing is
proposed), or it's a META/conversational question. Reply in ONE short, warm message:
- If a rephrasing was proposed, offer it plainly ("Did you mean: <proposed>?"); a Run button sits beside your
  reply, so mention they can run it or rephrase.
- If it's a META question (how a value was derived), explain accurately: PreReasoner types each column to a
  Wikidata taxonomy, resolves each cell's surface form to its canonical world entity (e.g. "germany" -> the
  country "Germany"), then joins the world tables — point them to the "Resolving" slide in the trace panel.
- If answering would need a data value you were NOT given, tell them to ask it as a specific data question.
  NEVER invent, estimate, or recall a number.

Always: be specific to their actual columns; never describe SQL you were not given; never fabricate a number.
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


def _answer_text(answer, limit=40):
    """A compact 'col, col | v, v; v, v' rendering of a computed result, capped so we never blow the prompt.
    An EMPTY result (zero rows) renders as an explicit sentinel — never a bare 'col | ' with no value, which
    would leave Sonnet told to 'use the number' with no number in hand."""
    if not isinstance(answer, dict):
        return "(no result)"
    cols = answer.get("columns") or []
    rows = answer.get("rows") or []
    head = ", ".join(str(c) for c in cols)
    if not rows:
        return f"columns: [{head}] — the query ran successfully and returned NO ROWS (an empty result set)."
    body = "; ".join(", ".join(str(v) for v in r) for r in rows[:limit])
    more = f" … (+{len(rows) - limit} more rows)" if len(rows) > limit else ""
    return f"{head} | {body}{more}"


def reply(question, clarify=None, error=None, tables=None, answer=None, sql=None,
          model=None, api_key=None, max_tokens=400):
    """Return a short conversational reply (str). Raises if the Anthropic key/SDK is unavailable.

    PRESENT mode: pass `answer` (a {columns, rows} result the engine already computed) and optionally `sql` —
    Sonnet wraps that exact value in warm human text. FALLBACK mode: pass `clarify`/`error` (no `answer`)."""
    from anthropic import Anthropic
    from engine.config import anthropic_api_key, ANTHROPIC_MODEL

    client = Anthropic(api_key=api_key or anthropic_api_key())
    schema = _schema_text(tables)
    user = f"The user's tables: {schema}\n\nThe user's message: {question!r}\n"
    if answer is not None:
        user += ("\nMODE: PRESENT. The engine COMPUTED this answer — use the value(s) verbatim, do NOT recompute "
                 f"or change any number:\n  {_answer_text(answer)}\n")
        if sql:
            user += f"  (derived by: {sql})\n"
        user += ("Present it as a warm, human reply that uses this computed value. The full derivation is already "
                 "shown in the panel beside the chat.\n")
    else:
        user += "\nMODE: FALLBACK.\n"
        if clarify:
            user += f"The engine found this ambiguous and proposed a rephrasing: {json.dumps(clarify)}\n"
        if error:
            user += f"The engine returned this error: {error}\n"
    user += "\nReply to the user now, per your instructions."

    resp = client.messages.create(
        model=model or ANTHROPIC_MODEL, max_tokens=max_tokens, system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
