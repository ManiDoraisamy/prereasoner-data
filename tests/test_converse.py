"""test_converse.py — offline unit tests for engine.converse.generate_master (the master-data fill behind
/api/master/generate). Mocks the Anthropic SDK so it runs with NO key and NO network, pinning the invariants:
the user's already-filled cells are PRESERVED, empty cells are filled, an entity-only table gets new columns,
ragged rows are normalized, and the user's instruction + the current rows reach the prompt.

Run:  python -m tests.test_converse
"""
from __future__ import annotations

import json
import sys
import types


def _gen(resp_text, columns, rows, instruction=None):
    """Call generate_master with a FAKE Anthropic client that returns `resp_text`. Returns (out, captured)
    where captured has the user/system prompt + model the client was called with."""
    cap = {}
    fake = types.ModuleType("anthropic")

    class _Msgs:
        def create(self, **kw):
            cap["user"] = kw["messages"][0]["content"]; cap["system"] = kw.get("system"); cap["model"] = kw.get("model")
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=resp_text)])

    class _Client:
        def __init__(self, *a, **k): self.messages = _Msgs()

    fake.Anthropic = _Client
    sys.modules["anthropic"] = fake
    import engine.config as cfg
    cfg.anthropic_api_key = lambda: "test-key"               # generate_master imports this at call time; no real key needed
    from engine import converse
    return converse.generate_master("series", columns, rows, instruction=instruction), cap


def test_preserves_existing_and_fills_empty():
    # Doyle.category is already "Detective Fiction"; the model tries to OVERWRITE it -> it must be PRESERVED.
    resp = json.dumps({"columns": ["series", "category", "author"],
                       "rows": [["Doyle", "OVERWRITE", "Arthur Conan Doyle"], ["Christie", "Mystery", "Agatha Christie"]]})
    out, _ = _gen(resp, ["series", "category", "author"], [["Doyle", "Detective Fiction", ""], ["Christie", "", ""]])
    assert out["rows"][0] == ["Doyle", "Detective Fiction", "Arthur Conan Doyle"], out["rows"]
    assert out["rows"][1] == ["Christie", "Mystery", "Agatha Christie"], out["rows"]


def test_preserves_by_column_name_even_if_model_reorders():
    # The model returns columns in a DIFFERENT order; preservation is keyed by (entity, column NAME), not position.
    resp = json.dumps({"columns": ["series", "author", "category"],
                       "rows": [["Doyle", "someone else", "WRONG"]]})
    out, _ = _gen(resp, ["series", "category", "author"], [["Doyle", "Detective Fiction", ""]])
    row = dict(zip(out["columns"], out["rows"][0]))
    assert row["category"] == "Detective Fiction", out          # preserved despite reorder + model overwrite attempt
    assert row["author"] == "someone else", out                 # empty cell filled


def test_instruction_and_current_rows_reach_the_prompt():
    _, cap = _gen(json.dumps({"columns": ["series"], "rows": [["Doyle"]]}),
                  ["series"], [["Doyle"]], instruction="Fill only the empty cells.")
    assert "Fill only the empty cells." in cap["user"], cap["user"]
    assert "Current rows" in cap["user"], cap["user"]


def test_entity_only_adds_columns():
    resp = json.dumps({"columns": ["series", "genre", "origin"], "rows": [["Doyle", "Detective", "UK"]]})
    out, _ = _gen(resp, ["series"], [["Doyle"]])
    assert out["columns"] == ["series", "genre", "origin"], out["columns"]
    assert out["rows"][0] == ["Doyle", "Detective", "UK"], out["rows"]


def test_ragged_row_normalized_to_width():
    out, _ = _gen(json.dumps({"columns": ["series", "a", "b"], "rows": [["Doyle", "x"]]}), ["series"], [["Doyle"]])
    assert out["rows"][0] == ["Doyle", "x", ""], out["rows"]


def test_no_entities_returns_empty_without_calling_the_model():
    out, cap = _gen("{}", ["series"], [["", ""]])               # blank entity -> nothing to do, no model call
    assert out["rows"] == [], out
    assert "user" not in cap, "the model was called for an empty entity list"


TESTS = [
    test_preserves_existing_and_fills_empty,
    test_preserves_by_column_name_even_if_model_reorders,
    test_instruction_and_current_rows_reach_the_prompt,
    test_entity_only_adds_columns,
    test_ragged_row_normalized_to_width,
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
