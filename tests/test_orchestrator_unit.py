"""Hermetic control-flow tests for the chat orchestrator.

The external ``tests.test_orchestrator`` suite checks prompt fidelity against Anthropic. These tests
replace Anthropic and the engine with contract-shaped fakes so the release gate always proves that a
terminal engine result cannot start another paid tool round.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from orchestrator import orchestrator


class _TextStream:
    def __init__(self, text: str):
        self._text = text

    def __aiter__(self):
        self._sent = False
        return self

    async def __anext__(self):
        if self._sent or not self._text:
            raise StopAsyncIteration
        self._sent = True
        return self._text


class _MessageStream:
    def __init__(self, response):
        self.response = response
        self.text_stream = _TextStream("".join(
            block.text for block in response.content if block.type == "text"
        ))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get_final_message(self):
        return self.response


class _Messages:
    def __init__(self, calls, fail_presentation=False):
        self.calls = calls
        self.fail_presentation = fail_presentation

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            response = SimpleNamespace(
                stop_reason="tool_use",
                content=[SimpleNamespace(
                    type="tool_use", name="prereasoner_query", id="query-1",
                    input={"question": "total amount after the customer tier discount"},
                )],
            )
        else:
            if self.fail_presentation:
                raise RuntimeError("presentation unavailable")
            response = SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text="The verified result is ready.")],
            )
        return _MessageStream(response)


class _Client:
    def __init__(self, calls, fail_presentation=False):
        self.messages = _Messages(calls, fail_presentation)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _HTTP:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


async def _run(status: str, *, fail_presentation=False):
    model_calls, engine_calls = [], []
    shaped = {"status": status}
    if status == "answered":
        shaped["answer"] = {"columns": ["net_amount"], "rows": [["876.50"]]}
    elif status == "clarify":
        shaped["clarify"] = {"reason": "choose a discount schedule"}
    else:
        shaped["error"] = "engine unavailable"

    async def query(*args, **kwargs):
        engine_calls.append((args, kwargs))
        return shaped

    original_client = orchestrator.AsyncAnthropic
    original_http = orchestrator.httpx.AsyncClient
    original_query = orchestrator.engine_client.call_query
    orchestrator.AsyncAnthropic = lambda **_kwargs: _Client(model_calls, fail_presentation)
    orchestrator.httpx.AsyncClient = lambda **_kwargs: _HTTP()
    orchestrator.engine_client.call_query = query
    try:
        result = await orchestrator._run_turn(
            "reduce the discount from total amount based on customer's tier",
            [{"name": "orders", "data": "tier,amount\nGold,100\n"}],
            [],
            engine_base_url="http://engine.invalid",
            bearer_token=None,
            api_key="test",
            model="test-model",
        )
    finally:
        orchestrator.AsyncAnthropic = original_client
        orchestrator.httpx.AsyncClient = original_http
        orchestrator.engine_client.call_query = original_query
    return result, model_calls, engine_calls


def test_terminal_engine_status_uses_one_query_and_a_tool_disabled_presentation():
    for status in ("answered", "clarify", "error"):
        result, model_calls, engine_calls = asyncio.run(_run(status))
        assert len(engine_calls) == 1, (status, engine_calls)
        assert len(model_calls) == 2, (status, model_calls)
        assert "tools" in model_calls[0]
        assert "tools" not in model_calls[1]
        assert result["reply"] == "The verified result is ready."
        assert "step budget" not in result["reply"]
        assert len(result["traces"]) == 1


def test_terminal_fallback_preserves_the_engine_outcome():
    assert orchestrator._terminal_fallback({
        "status": "answered", "answer": {"rows": [["876.50"]]},
    }) == "876.50"
    assert orchestrator._terminal_fallback({
        "status": "clarify", "clarify": {"reason": "choose a discount schedule"},
    }) == "choose a discount schedule"
    assert orchestrator._terminal_fallback({
        "status": "error", "error": "engine unavailable",
    }) == "engine unavailable"

    result, model_calls, engine_calls = asyncio.run(_run(
        "answered", fail_presentation=True,
    ))
    assert len(model_calls) == 2
    assert len(engine_calls) == 1
    assert result["reply"] == "876.50"
    assert "step budget" not in result["reply"]


TESTS = [
    test_terminal_engine_status_uses_one_query_and_a_tool_disabled_presentation,
    test_terminal_fallback_preserves_the_engine_outcome,
]


def main():
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\norchestrator unit: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
