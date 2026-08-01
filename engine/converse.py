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


GEN_SYSTEM = """You fill in a REFERENCE ("master") data table for a spreadsheet product. You are given a table
name, its column headers, the entities (the values in the FIRST column), and the CURRENT rows (some cells may
already be filled). Produce accurate, concise, factual values from general knowledge (a few words each; use ""
only when genuinely unknown).

Rules:
- Keep the FIRST column's values EXACTLY as given, one output row per entity, same order.
- PRESERVE every already-filled (non-empty) cell EXACTLY as given — only fill the empty "" cells.
- If only the entity column was provided, ADD 2–3 useful, clearly-named attribute columns and fill them.
- Follow any additional instruction from the user (e.g. which columns to add, or to fill only missing cells).

Output STREAMING JSONL — one JSON object per line, nothing else, no markdown, no code fence:
- The FIRST line is the header: {"columns": [<all headers, entity column first>]}
- THEN one line per entity IN THE GIVEN ORDER: {"row": [<cell>, ...]} — exactly as many cells as columns,
  the first cell the entity verbatim.
Emit each row on its own line as soon as it is ready (the product renders rows live as they arrive)."""


def generate_master(name, columns, rows, instruction=None, emit=None, model=None, api_key=None, max_tokens=2000):
    """Generate/fill a master (reference) table with Sonnet, STREAMING. `columns` = headers (first is the
    entity key); `rows` = existing rows (only the first column's entity values are required; other cells may
    already be filled). `instruction` = optional user guidance (which columns to add, or fill only missing
    cells). `emit` = optional RTDB emit(node, value) — when given, the header is streamed to `mcols` and each
    completed row to `mrows/<i>` AS IT ARRIVES, so the browser fills the sheet live. Returns the assembled
    {'columns', 'rows'} (entity column verbatim, already-filled cells preserved, empties filled) regardless.
    Raises if the Anthropic key/SDK is unavailable."""
    from anthropic import Anthropic
    from engine.config import anthropic_api_key, ANTHROPIC_MODEL

    columns = [str(c) for c in (columns or [])] or ["name"]
    # entities (col 0) + the already-filled cells to PRESERVE, keyed by (entity, column name) so a preserved
    # value survives even if the model adds/reorders columns.
    entities, existing, table, seen = [], {}, [], set()
    for r in (rows or []):
        cells = [("" if v is None else str(v)) for v in (r or [])]
        e = (cells[0] if cells else "").strip()
        if not e or e.lower() in seen:
            continue
        entities.append(e); seen.add(e.lower())
        table.append((cells + [""] * len(columns))[:len(columns)])
        existing[e.lower()] = {str(columns[i]).lower(): cells[i]
                               for i in range(1, min(len(cells), len(columns))) if cells[i].strip()}
    if not entities:
        return {"columns": columns, "rows": []}
    client = Anthropic(api_key=api_key or anthropic_api_key())
    guidance = (f"\n\nAdditional instruction from the user (follow it):\n{instruction.strip()}"
                if instruction and str(instruction).strip() else "")
    user = (f"Table name: {name!r}\nColumns: {json.dumps(columns)}\n"
            f"Entity column: {columns[0]!r}\nEntities ({len(entities)}): {json.dumps(entities)}\n"
            f"Current rows (preserve every non-empty cell EXACTLY; fill only the \"\" cells): {json.dumps(table)}"
            f"{guidance}\n\nReturn the JSONL now.")

    state = {"cols": list(columns)}                              # out_cols, mutated when the header line arrives
    out_rows = []

    def _preserve(row):                                         # normalize to width + keep the user's already-filled cells
        cols = state["cols"]; w = len(cols)
        rr = ([("" if v is None else str(v)) for v in row] + [""] * w)[:w]
        ex = existing.get(rr[0].strip().lower())
        if ex:
            for i in range(1, w):
                v = ex.get(cols[i].lower())
                if v and v.strip():
                    rr[i] = v
        return rr

    def _consume(line):                                        # one JSONL line -> stream a header or a row
        s = line.strip().strip("`").strip()
        if not s or s.lower() == "json":
            return
        try:
            obj = json.loads(s)
        except Exception:                                      # noqa: BLE001 — a partial/garbled line; skip it
            return
        if isinstance(obj, dict) and isinstance(obj.get("columns"), list) and obj["columns"]:
            state["cols"] = [str(c) for c in obj["columns"]]
            if emit:
                emit("mcols", state["cols"])
        elif isinstance(obj, dict) and isinstance(obj.get("row"), list):
            rr = _preserve(obj["row"]); out_rows.append(rr)
            if emit:
                emit(f"mrows/{len(out_rows) - 1:04d}", rr)     # zero-padded key so RTDB child order == row order

    full, buf = "", ""
    with client.messages.stream(model=model or ANTHROPIC_MODEL, max_tokens=max_tokens, system=GEN_SYSTEM,
                                messages=[{"role": "user", "content": user}]) as stream:
        for text in stream.text_stream:
            full += text; buf += text
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                _consume(line)
    if buf.strip():
        _consume(buf)                                          # a trailing line with no closing newline

    if not out_rows:                                           # model ignored JSONL and returned one {columns, rows} blob
        t = full.strip()
        if t.startswith("```"):
            t = t.split("```", 2)[1].lstrip("json").strip() if t.count("```") >= 2 else t.strip("`")
        try:
            data = json.loads(t)
        except Exception:                                      # noqa: BLE001
            data = {}
        if isinstance(data, dict):
            if isinstance(data.get("columns"), list) and data["columns"]:
                state["cols"] = [str(c) for c in data["columns"]]
                if emit:
                    emit("mcols", state["cols"])
            for row in (data.get("rows") or []):
                if row:
                    rr = _preserve(row); out_rows.append(rr)
                    if emit:
                        emit(f"mrows/{len(out_rows) - 1:04d}", rr)
    return {"columns": state["cols"], "rows": out_rows}
