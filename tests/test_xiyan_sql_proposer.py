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

    def tokenize(self, text, **kwargs):
        return list(text)


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
            assert "fit proposer differs" in str(exc), exc
        else:
            raise AssertionError("an undisclosed proposer/arbiter mismatch was accepted")


def test_truncated_completion_is_rejected_even_when_prefix_is_valid_sql():
    class TruncatedModel(FakeModel):
        def create_completion(self, **kwargs):
            result = super().create_completion(**kwargs)
            result["choices"][0]["finish_reason"] = "length"
            return result

    model = TruncatedModel("SELECT name FROM singer")
    proposer = XiYanSQLProposer(model, FakeTokenizer(), load_contract(), "0" * 64)
    assert proposer.propose([], "Which singer?", _graph(), 0.0) == []
    assert not proposer._decoded, "incomplete generations must not poison the cache"


def test_context_overflow_is_rejected_before_decoding():
    model = FakeModel("SELECT name FROM singer")
    proposer = XiYanSQLProposer(model, FakeTokenizer(), load_contract(), "0" * 64)
    assert proposer.propose([], "Which singer?" * 1000, _graph(), 0.0) == []
    assert model.calls == []


def test_simultaneous_identical_requests_share_one_completed_decode():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    waiting = threading.Event()

    class ObservedLock:
        def __init__(self):
            self.lock = threading.Lock()
            self.guard = threading.Lock()
            self.entries = 0

        def acquire(self, **kwargs):
            with self.guard:
                self.entries += 1
                if self.entries == 2:
                    waiting.set()
            return self.lock.acquire(**kwargs)

        def release(self):
            self.lock.release()

    class WaitingModel(FakeModel):
        def create_completion(self, **kwargs):
            assert waiting.wait(5), "second request did not reach the model lock"
            return super().create_completion(**kwargs)

    model = WaitingModel("SELECT name FROM singer")
    proposer = XiYanSQLProposer(model, FakeTokenizer(), load_contract(), "0" * 64)
    proposer._model_lock = ObservedLock()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _: proposer.propose([], "Which singer?", _graph(), 0.0), range(2)))
    assert results[0] == results[1]
    assert len(model.calls) == 1, "a waiter decoded a prompt already completed by the first request"


def test_admission_timeout_never_starts_another_decode():
    from unittest.mock import Mock
    from engine.xiyan_sql_proposer import SQLProposerUnavailable

    model = FakeModel("SELECT name FROM singer")
    proposer = XiYanSQLProposer(model, FakeTokenizer(), load_contract(), "0" * 64)
    proposer._model_lock = Mock()
    proposer._model_lock.acquire.return_value = False
    try:
        proposer.propose([], "Which singer?", _graph(), 0)
    except SQLProposerUnavailable:
        pass
    else:
        raise AssertionError("busy inference was admitted")
    assert not model.calls
    proposer._model_lock.release.assert_not_called()


def test_decode_deadline_rejects_partial_sql_and_recovers_context():
    from unittest.mock import patch
    from engine.xiyan_sql_proposer import SQLProposerUnavailable

    class ExpiredModel(FakeModel):
        resets = 0

        def reset(self):
            self.resets += 1

    model = ExpiredModel("SELECT name FROM singer")
    proposer = XiYanSQLProposer(model, FakeTokenizer(), load_contract(), "0" * 64)
    with patch("engine.xiyan_sql_proposer.time.monotonic", side_effect=[0, 61, 61]):
        try:
            proposer.propose([], "Which singer?", _graph(), 0)
        except SQLProposerUnavailable:
            pass
        else:
            raise AssertionError("expired SQL was accepted")
    assert model.resets == 1 and not proposer._decoded and proposer._deadline is None
    assert proposer.propose([], "Which singer?", _graph(), 0)


def test_close_is_idempotent_and_rejects_further_inference():
    from unittest.mock import Mock
    from engine.xiyan_sql_proposer import SQLProposerUnavailable

    model = FakeModel("SELECT name FROM singer")
    model.close = Mock()
    proposer = XiYanSQLProposer(model, FakeTokenizer(), load_contract(), "0" * 64)
    proposer.close()
    proposer.close()
    model.close.assert_called_once()
    try:
        proposer.propose([], "Which singer?", _graph(), 0)
    except SQLProposerUnavailable:
        pass
    else:
        raise AssertionError("closed native context was reused")


def test_load_finalizer_releases_temporary_proposers_without_retaining_them():
    import gc
    import hashlib
    import json
    import sys
    import types
    import weakref
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from unittest.mock import Mock, patch

    class NativeModel:
        def __init__(self, **kwargs):
            self._ctx = types.SimpleNamespace(ctx=object())
            self.close = Mock()

    llama_cpp_api = types.SimpleNamespace(
        ggml_abort_callback=lambda callback: callback,
        llama_set_abort_callback=Mock(),
    )
    llama_module = types.ModuleType("llama_cpp")
    llama_module.Llama = NativeModel
    llama_module.llama_cpp = llama_cpp_api
    transformers_module = types.ModuleType("transformers")
    transformers_module.AutoTokenizer = types.SimpleNamespace(
        from_pretrained=lambda *args, **kwargs: FakeTokenizer())

    with TemporaryDirectory() as directory:
        root = Path(directory)
        model_path = root / "fixture.gguf"
        model_path.write_bytes(b"fixture model")
        contract = load_contract()
        contract["gguf"]["sha256"] = hashlib.sha256(model_path.read_bytes()).hexdigest()
        contract["gguf"]["size_bytes"] = model_path.stat().st_size
        contract_path = root / "contract.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        with patch.dict(sys.modules, {"llama_cpp": llama_module,
                                     "transformers": transformers_module}):
            proposer = XiYanSQLProposer.load(model_path, contract_path=contract_path)
            native = proposer.model
            reference = weakref.ref(proposer)
            del proposer
            gc.collect()
            assert reference() is None, "the finalizer callback retained the proposer instance"
            native.close.assert_called_once()


# Registered in tests/run_all.py: the release gates run that runner, not pytest, so these tests
# never ran after the 7B proposer landed (2026-09-28).
TESTS = [
    test_load_finalizer_releases_temporary_proposers_without_retaining_them,
    test_admission_timeout_never_starts_another_decode,
    test_decode_deadline_rejects_partial_sql_and_recovers_context,
    test_close_is_idempotent_and_rejects_further_inference,
    test_truncated_completion_is_rejected_even_when_prefix_is_valid_sql,
    test_context_overflow_is_rejected_before_decoding,
    test_simultaneous_identical_requests_share_one_completed_decode,
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
