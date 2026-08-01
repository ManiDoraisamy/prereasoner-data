"""test_converse.py — offline unit tests for engine.converse.generate_master (the STREAMING master-data fill
behind /api/master/generate). Mocks the Anthropic streaming SDK so it runs with NO key and NO network, pinning:
the user's already-filled cells are PRESERVED, empty cells filled, an entity-only table gains columns, ragged
rows normalized, the instruction + current rows reach the prompt, each header/row is EMITTED to RTDB live in
order, incremental parsing survives chunk splits mid-line, and a non-JSONL {columns, rows} blob still parses.

Run:  python -m tests.test_converse
"""
from __future__ import annotations

import json
import sys
import types


def _gen(chunks, columns, rows, instruction=None, emit=None):
    """Call generate_master with a FAKE Anthropic STREAMING client whose text_stream yields `chunks` (a str is
    sent as one chunk; a list is streamed piece by piece, letting a test split JSONL mid-line). Returns
    (out, captured) where captured has the user/system prompt the client saw."""
    chunks = [chunks] if isinstance(chunks, str) else list(chunks)
    cap = {}
    fake = types.ModuleType("anthropic")

    class _Stream:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        @property
        def text_stream(self):
            for c in chunks:
                yield c

    class _Msgs:
        def stream(self, **kw):
            cap["user"] = kw["messages"][0]["content"]; cap["system"] = kw.get("system"); cap["model"] = kw.get("model")
            return _Stream()

    class _Client:
        def __init__(self, *a, **k): self.messages = _Msgs()

    fake.Anthropic = _Client
    sys.modules["anthropic"] = fake
    import engine.config as cfg
    cfg.anthropic_api_key = lambda: "test-key"               # generate_master imports this at call time; no real key needed
    from engine import converse
    return converse.generate_master("series", columns, rows, instruction=instruction, emit=emit), cap


def _jsonl(cols, rows):
    return json.dumps({"columns": cols}) + "\n" + "".join(json.dumps({"row": r}) + "\n" for r in rows)


def test_preserves_existing_and_fills_empty():
    # Doyle.category is already "Detective Fiction"; the model tries to OVERWRITE it -> it must be PRESERVED.
    jl = _jsonl(["series", "category", "author"],
                [["Doyle", "OVERWRITE", "Arthur Conan Doyle"], ["Christie", "Mystery", "Agatha Christie"]])
    out, _ = _gen(jl, ["series", "category", "author"], [["Doyle", "Detective Fiction", ""], ["Christie", "", ""]])
    assert out["rows"][0] == ["Doyle", "Detective Fiction", "Arthur Conan Doyle"], out["rows"]
    assert out["rows"][1] == ["Christie", "Mystery", "Agatha Christie"], out["rows"]


def test_preserves_by_column_name_even_if_model_reorders():
    jl = _jsonl(["series", "author", "category"], [["Doyle", "someone else", "WRONG"]])   # reordered header
    out, _ = _gen(jl, ["series", "category", "author"], [["Doyle", "Detective Fiction", ""]])
    row = dict(zip(out["columns"], out["rows"][0]))
    assert row["category"] == "Detective Fiction", out          # preserved despite reorder + overwrite attempt
    assert row["author"] == "someone else", out                 # empty cell filled


def test_instruction_and_current_rows_reach_the_prompt():
    _, cap = _gen(_jsonl(["series"], [["Doyle"]]), ["series"], [["Doyle"]],
                  instruction="Fill only the empty cells.")
    assert "Fill only the empty cells." in cap["user"], cap["user"]
    assert "Current rows" in cap["user"], cap["user"]


def test_entity_only_adds_columns():
    out, _ = _gen(_jsonl(["series", "genre", "origin"], [["Doyle", "Detective", "UK"]]), ["series"], [["Doyle"]])
    assert out["columns"] == ["series", "genre", "origin"], out["columns"]
    assert out["rows"][0] == ["Doyle", "Detective", "UK"], out["rows"]


def test_ragged_row_normalized_to_width():
    jl = json.dumps({"columns": ["series", "a", "b"]}) + "\n" + json.dumps({"row": ["Doyle", "x"]}) + "\n"
    out, _ = _gen(jl, ["series"], [["Doyle"]])
    assert out["rows"][0] == ["Doyle", "x", ""], out["rows"]


def test_streaming_emits_header_then_rows_in_order():
    jl = _jsonl(["series", "genre"], [["Doyle", "Detective"], ["Christie", "Mystery"]])
    ev = []
    _gen(jl, ["series"], [["Doyle"], ["Christie"]], emit=lambda *a: ev.append(a))
    nodes = [n for n, *_ in ev]
    assert nodes[0] == "mcols", ev
    assert nodes[1] == "mrows/0000" and nodes[2] == "mrows/0001", ev      # zero-padded keys keep RTDB child order
    assert dict((n, v) for n, v in ev)["mrows/0000"] == ["Doyle", "Detective"], ev


def test_incremental_parse_survives_chunk_splits_midline():
    jl = _jsonl(["series", "genre"], [["Doyle", "Detective"], ["Christie", "Mystery"]])
    chunks = [jl[i:i + 5] for i in range(0, len(jl), 5)]        # arbitrary 5-char chunks, split across + inside lines
    out, _ = _gen(chunks, ["series"], [["Doyle"], ["Christie"]])
    assert out["columns"] == ["series", "genre"], out
    assert out["rows"] == [["Doyle", "Detective"], ["Christie", "Mystery"]], out


def test_fallback_when_model_returns_one_nested_json_blob():
    blob = json.dumps({"columns": ["series", "genre"], "rows": [["Doyle", "Detective"]]})   # ignored JSONL -> one blob
    out, _ = _gen(blob, ["series"], [["Doyle"]])
    assert out["rows"] == [["Doyle", "Detective"]], out


def test_fallback_handles_a_code_fence():
    blob = "```json\n" + json.dumps({"columns": ["series", "g"], "rows": [["Doyle", "x"]]}) + "\n```"
    out, _ = _gen(blob, ["series"], [["Doyle"]])
    assert out["rows"] == [["Doyle", "x"]], out


def test_no_entities_returns_empty_without_calling_the_model():
    out, cap = _gen("{}", ["series"], [["", ""]])              # blank entity -> nothing to do, no model call
    assert out["rows"] == [], out
    assert "user" not in cap, "the model was called for an empty entity list"


TESTS = [
    test_preserves_existing_and_fills_empty,
    test_preserves_by_column_name_even_if_model_reorders,
    test_instruction_and_current_rows_reach_the_prompt,
    test_entity_only_adds_columns,
    test_ragged_row_normalized_to_width,
    test_streaming_emits_header_then_rows_in_order,
    test_incremental_parse_survives_chunk_splits_midline,
    test_fallback_when_model_returns_one_nested_json_blob,
    test_fallback_handles_a_code_fence,
    test_no_entities_returns_empty_without_calling_the_model,
]


def main():
    failed = []
    for t in TESTS:
        try:
            t(); print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed.append(t.__name__); print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\nConverse: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
