from __future__ import annotations

from engine.sql_prompt import xiyan_mschema, xiyansql_prompt
from engine.sql_schema import SchemaGraph
from engine.xiyan_sql_proposer import XiYanSQLProposer, load_contract


class FakeTokenizer:
    eos_token = "<|im_end|>"

    def apply_chat_template(self, messages, **kwargs):
        assert kwargs == {"tokenize": False, "add_generation_prompt": True}
        return "<|im_start|>user\n" + messages[0]["content"] + (
            "<|im_end|>\n<|im_start|>assistant\n"
        )


class FakeModel:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create_completion(self, **kwargs):
        self.calls.append(kwargs)
        return {"choices": [{"text": self.text, "finish_reason": "stop"}]}


def _graph():
    tables = [{"name": "singer", "columns": ["name"], "rows": [["Mina"]]}]
    return SchemaGraph.from_tables(tables, [])


def test_xiyan_mschema_keeps_inferred_type_examples_and_foreign_keys():
    graph = SchemaGraph.from_tables(
        [
            {"name": "singer", "columns": ["id", "name"], "rows": [[1, "Mina"]]},
            {"name": "concert", "columns": ["singer_id"], "rows": [[1]]},
        ],
        [{"from_table": "concert", "from_col": "singer_id",
          "to_table": "singer", "to_col": "id"}],
    )
    rendered = xiyan_mschema(graph)
    assert "# Table: singer" in rendered
    assert "(name:TEXT, Examples: [\"Mina\"])" in rendered
    assert "concert.singer_id=singer.id" in rendered


def test_xiyan_prompt_uses_the_publisher_user_template_and_chat_generation_prefix():
    prompt = xiyansql_prompt(FakeTokenizer(), _graph(), "Which singer?")
    assert prompt.startswith("<|im_start|>user\nYou are an SQLite expert.")
    assert "Database schema:\n" in prompt
    assert "Which singer?\n\n```sql" in prompt
    assert prompt.endswith("<|im_end|>\n<|im_start|>assistant\n")


def test_proposer_uses_measured_greedy_contract_normalization_and_neutral_scores():
    contract = load_contract()
    model = FakeModel("SELECT name\nFROM singer;")
    proposer = XiYanSQLProposer(model, FakeTokenizer(), contract, "0" * 64)
    graph = _graph()
    proposed = proposer.propose([], "Which singer?", graph, floor=0.0)
    assert len(proposed) == 1
    assert proposed[0].sql == 'SELECT "singer"."name" FROM "singer"'
    assert proposed[0].evidence == ("proposer:variant0",)
    assert proposer.likelihoods([], "Which singer?", [proposed[0].sql]) == [(0.0, 1)]
    assert model.calls == [{
        "prompt": xiyansql_prompt(FakeTokenizer(), graph, "Which singer?"),
        "max_tokens": 1024,
        "temperature": 0.0,
        "top_p": 1.0,
        "seed": 0,
        "stop": ["<|im_end|>"],
    }]
    # The bounded prompt cache must avoid an extra model decode for the same request.
    assert proposer.propose([], "Which singer?", graph, floor=0.0) == proposed
    assert len(model.calls) == 1


def test_proposer_rejects_sql_that_cannot_import_into_the_typed_ast():
    proposer = XiYanSQLProposer(FakeModel("SELECT missing FROM singer"), FakeTokenizer(),
                                load_contract(), "0" * 64)
    assert proposer.propose([], "Which singer?", _graph(), floor=0.0) == []


def test_contract_pins_the_measured_gguf_and_one_cpu_sample():
    contract = load_contract()
    assert contract["gguf"]["sha256"] == (
        "50840d65a753074a670d7929ca0a4b5d633b0a4b435f1a68b4a6fba26c4d18bb"
    )
    assert contract["runtime"]["gpu_layers"] == 0
    assert contract["runtime"]["accuracy_evidence_threads"] == 16
    assert contract["runtime"]["threads"] == 8
    assert contract["generation"]["temperature"] == 0.0
    assert contract["likelihood_policy"] == "neutral-sentinel-v1"
    assert contract["selector"]["model_matched_arbiter"] is False
    assert contract["selector"]["fit_source_proposer_sha256"] == (
        "d8939fea27e474eea3e77384e9a56b2bfbeac7ec7a90cf5a990a46b6e8bfacdf"
    )


def test_runtime_contract_rejects_an_undisclosed_model_mismatch():
    import json
    import tempfile
    from pathlib import Path

    contract = load_contract()
    contract["selector"]["model_matched_arbiter"] = True
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        try:
            load_contract(path)
        except RuntimeError as exc:
            assert "disclose that it was fit with another proposer" in str(exc), exc
        else:
            raise AssertionError("an undisclosed proposer/arbiter mismatch was accepted")


# Registered in tests/run_all.py: the release gates run that runner, not pytest, so these tests
# never ran after the 7B proposer landed (2026-09-28).
TESTS = [
    test_xiyan_mschema_keeps_inferred_type_examples_and_foreign_keys,
    test_xiyan_prompt_uses_the_publisher_user_template_and_chat_generation_prefix,
    test_proposer_uses_measured_greedy_contract_normalization_and_neutral_scores,
    test_proposer_rejects_sql_that_cannot_import_into_the_typed_ast,
    test_contract_pins_the_measured_gguf_and_one_cpu_sample,
    test_runtime_contract_rejects_an_undisclosed_model_mismatch,
]


def main() -> None:
    failures = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nXiYanSQL proposer: {len(TESTS) - len(failures)} passed, {len(failures)} failed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
