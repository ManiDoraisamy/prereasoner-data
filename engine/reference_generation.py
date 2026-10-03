"""Explicit, labelled Gemini reference-table generation; separate from question answering."""
import json
from engine import llm

GENERATE_MAX_TOKENS = 8192

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


def generate_master(name, columns, rows, instruction=None, emit=None, max_tokens=GENERATE_MAX_TOKENS):
    """Generate/fill a master (reference) table with Gemini, STREAMING. `columns` = headers (first is the
    entity key); `rows` = existing rows (only the first column's entity values are required; other cells may
    already be filled). `instruction` = optional user guidance (which columns to add, or fill only missing
    cells). `emit` = optional RTDB emit(node, value) — when given, the header is streamed to `mcols` and each
    completed row to `mrows/<i>` AS IT ARRIVES, so the browser fills the sheet live. Returns the assembled
    {'columns', 'rows'} (entity column verbatim, already-filled cells preserved, empties filled) regardless.
    Raises llm.LLMUnavailable when Gemini is unavailable."""
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
    for text in llm.stream_text(system=GEN_SYSTEM, prompt=user, max_output_tokens=max_tokens):
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
