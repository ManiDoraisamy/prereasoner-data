"""test_llm.py — hermetic tests for engine/llm.py, the one Gemini client.

No network and no credentials: the google-genai client is replaced by fakes, while every request is
built from the real SDK types, so a field the pinned SDK does not accept fails here first. Pins: the
configuration gates every call (there is no switch); the Decisions call sends one sorted body and returns each
question's probabilities over exactly its options; JSON mode, temperature 0, seed 0, and the
timeout reach the request; every failure reaches callers only as LLMUnavailable, without its message;
one client per process; the chat facade replays a function call with its thought signature and maps
the tool loop's tool_choice to Gemini's function-calling modes.

Run:  python -m tests.test_llm
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from google.auth.exceptions import DefaultCredentialsError
from google.genai import types

from engine import config, llm

SCHEMA = {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
TOOLS = [
    {"name": "prereasoner_query", "description": "query",
     "input_schema": {"type": "object", "properties": {"question": {"type": "string"}},
                      "required": ["question"]}},
    {"name": "prereasoner_describe", "description": "describe",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


@contextmanager
def _gemini(project="test-project"):
    """Gemini configured (or not), whatever the developer's environment says."""
    with patch.object(config, "GOOGLE_CLOUD_PROJECT", project), \
            patch.object(config, "GEMINI_MODEL", "gemini-test"):
        yield


class _Models:
    """The sync ``client.models`` surface: one scripted reply, chunk list, or error."""

    def __init__(self, reply=None, chunks=(), error=None):
        self.reply, self.chunks, self.error, self.calls = reply, list(chunks), error, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.reply)

    def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield SimpleNamespace(text=chunk)


def _serving(models):
    return patch.object(llm, "_client", lambda: SimpleNamespace(models=models))


def _unavailable(call):
    try:
        call()
    except llm.LLMUnavailable as exc:
        return exc
    raise AssertionError("expected LLMUnavailable")


def test_unconfigured_gemini_is_unavailable_without_a_call():
    """No switch exists: a configured project is all Gemini on Vertex AI needs."""
    def no_client():
        raise AssertionError("a client was created while Gemini is unavailable")

    assert not hasattr(config, "external_llm_enabled")
    with _gemini(None), patch.object(llm, "_client", no_client), patch.dict("os.environ", {"EXTERNAL_LLM_ENABLED": "true"}):
        assert not llm.available()
        _unavailable(lambda: llm.generate_text(system="s", prompt="p", max_output_tokens=8))
        _unavailable(lambda: llm.stream_text(system="s", prompt="p", max_output_tokens=8))
    with _gemini(), patch.dict("os.environ", {"EXTERNAL_LLM_ENABLED": "false"}):
        assert llm.available() and llm.model_id() == "gemini-test"


def test_a_decision_sends_one_sorted_body_and_returns_each_questions_options():
    """decide() sends the STATE and each closed question as one body whose keys are sorted (the same record gives the
    same body, so an evaluation replays a recorded answer by its hash), and returns each question's probabilities over
    exactly its options. An answer missing an option, a transport failure, and a missing key all reach the caller as
    LLMUnavailable."""
    import json

    state = {"question": "how many orders", "workbook": [{"table": "t0", "tab": "Orders", "lines": 3}]}
    questions = {"reading": {"instructions": "Choose the reading.", "criteria": {"r00": "the number of lines", "none": "none"}},
                 "form": {"instructions": "What kind of answer?", "criteria": {"o00": "agg", "o01": "show"}}}
    sent = []

    def transport(payload):
        sent.append(payload)
        return {"answers": {"reading": {"probabilities": {"r00": 0.9, "none": 0.1}},
                            "form": {"probabilities": {"o00": 0.7, "o01": 0.3}}}, "tokens": 120}

    with patch.object(llm, "DECISIONS_TRANSPORT", transport), patch.object(config, "DECISIONS_MODEL", "gemini-decision-1"):
        decision = llm.decide(state, questions)
        assert decision.fields == {"reading": {"r00": 0.9, "none": 0.1}, "form": {"o00": 0.7, "o01": 0.3}} and decision.tokens == 120
        body = json.loads(sent[0])
        assert body["model"] == "gemini-decision-1" and body["input"] == "STATE:\n" + json.dumps(state, ensure_ascii=False)
        assert body["questions"]["form"] == {"type": "choice", "instructions": "What kind of answer?", "criteria": questions["form"]["criteria"]}
        assert sent[0] == llm.decisions_payload(state, dict(reversed(list(questions.items()))))
        assert llm.decision_hash(sent[0]) == llm.decision_hash(llm.decisions_payload(state, questions))
    with patch.object(llm, "DECISIONS_TRANSPORT", lambda payload: {"answers": {"reading": {"probabilities": {"r00": 1.0}}}}):
        _unavailable(lambda: llm.decide(state, {"reading": questions["reading"]}))
    with patch.dict("os.environ", {"GOOGLE_API_KEY": ""}):
        _unavailable(lambda: llm.decide(state, questions))
    try:
        llm.decisions_payload(state, {"one": {"instructions": "x", "criteria": {"o00": "only"}}})
    except ValueError:
        pass
    else:
        raise AssertionError("a question with one option was sent")


def test_the_retry_query_sends_one_sorted_body_and_reads_its_json_answer():
    """write_query() sends the retry (engine/retry.py) as one generateContent body to QUERY_MODEL on the Developer API:
    the system instruction, the prompt, the answer's JSON schema and the low thinking level, with no temperature (Gemini
    4 Neon refuses one), keys sorted so a prompt gives one body. Its answer's thought parts are not the answer; a query,
    or the model's word that the tables cannot answer, comes back with the tokens billed; an answer that is not its
    JSON, a failed call and a missing key reach the caller as LLMUnavailable."""
    import json
    from urllib.parse import urlparse

    sent = []

    def post(url, payload, timeout, attempts, label):
        sent.append((url, json.loads(payload.decode("utf-8"))))
        return {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True},
                                                      {"text": json.dumps({"answerable": True, "sql": " SELECT 1 ", "reason": "One."})}]}}],
                "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 7, "thoughtsTokenCount": 5}}

    with patch.object(llm, "_post_developer", post), patch.object(llm, "QUERY_TRANSPORT", llm._post_query):
        written = llm.write_query("rules", "TABLES: ...")
    assert written == llm.WrittenQuery(True, "SELECT 1", "One.", 30, 12), written
    url, body = sent[0]
    assert urlparse(url).path.endswith(f"/models/{config.QUERY_MODEL}:generateContent") and "model" not in body
    generation = body["generationConfig"]
    assert generation["thinkingConfig"] == {"thinkingLevel": "low"} and "temperature" not in generation
    assert generation["responseJsonSchema"]["required"] == ["answerable", "sql", "reason"]
    assert llm.query_payload("rules", "p") == llm.query_payload("rules", "p")
    with patch.object(llm, "QUERY_TRANSPORT", lambda payload: {"text": json.dumps({"answerable": False, "sql": "", "reason": "No costs."})}):
        assert llm.write_query("rules", "p") == llm.WrittenQuery(False, "", "No costs.")
    with patch.object(llm, "QUERY_TRANSPORT", lambda payload: {"text": "SELECT 1"}):
        _unavailable(lambda: llm.write_query("rules", "p"))
    with patch.dict("os.environ", {"GOOGLE_API_KEY": ""}), patch.object(llm, "QUERY_TRANSPORT", llm._post_query):
        _unavailable(lambda: llm.write_query("rules", "p"))


def test_the_judge_sends_the_choices_question_and_reads_one_key():
    """judge() puts one closed question to JUDGE_MODEL on the Developer API (engine/program/answer.py, the choice's
    second judge): the STATE the Decisions endpoint is given, the instruction and the options in the order given, the
    answer held by a JSON schema to one option's key, at the low thinking level, keys sorted so a question gives one
    body (a recorded answer replays by its hash). Its thought parts are not the answer; an answer that is not its JSON,
    a key that is no option, a failed call and a missing key reach the caller as LLMUnavailable."""
    import json
    from urllib.parse import urlparse

    sent = []
    state = {"question": "total amount", "workbook": []}
    options = [("r00", "the total of Amount -> answer: 7"), ("r01", "the number of lines -> answer: 3"), ("none", "None of these.")]

    def post(url, payload, timeout, attempts, label):
        sent.append((url, json.loads(payload.decode("utf-8")), timeout, attempts))
        return {"candidates": [{"content": {"parts": [{"text": "weighing...", "thought": True}, {"text": json.dumps({"choice": "r01"})}]}}],
                "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 3, "thoughtsTokenCount": 9}}

    with patch.object(llm, "_post_developer", post), patch.object(llm, "JUDGE_TRANSPORT", llm._post_judge):
        assert llm.judge(state, "Choose the reading.", options) == "r01"
    url, body, timeout, attempts = sent[0]
    assert urlparse(url).path.endswith(f"/models/{config.JUDGE_MODEL}:generateContent") and "model" not in body
    assert (timeout, attempts) == (llm.JUDGE_TIMEOUT_SECONDS, llm.JUDGE_ATTEMPTS)
    generation = body["generationConfig"]
    assert generation["thinkingConfig"] == {"thinkingLevel": "low"} and "temperature" not in generation
    assert generation["responseJsonSchema"]["properties"]["choice"]["enum"] == ["r00", "r01", "none"]
    prompt = body["contents"][0]["parts"][0]["text"]
    assert prompt == ("STATE:\n" + json.dumps(state) + "\n\nQUESTION: Choose the reading.\n\nOPTIONS:\n"
                      "r00: the total of Amount -> answer: 7\nr01: the number of lines -> answer: 3\nnone: None of these.")
    assert llm.judge_payload(state, "Choose.", options) == llm.judge_payload(state, "Choose.", options)
    assert json.loads(llm.judge_payload(state, "Choose.", options, "gemini-4-hydrogen", "medium"))["model"] == "gemini-4-hydrogen"
    for text in ("r01", json.dumps({"choice": "r07"}), json.dumps({"pick": "r01"})):
        with patch.object(llm, "JUDGE_TRANSPORT", lambda payload, text=text: {"text": text}):
            _unavailable(lambda: llm.judge(state, "Choose.", options))
    with patch.dict("os.environ", {"GOOGLE_API_KEY": ""}), patch.object(llm, "JUDGE_TRANSPORT", llm._post_judge):
        _unavailable(lambda: llm.judge(state, "Choose.", options))


def test_a_judge_still_out_is_asked_a_second_time_and_the_first_answer_is_read():
    """The provider holds a call now and then for longer than an answer waits for its second judge (release
    568b1f0a's gate: five right answers served warned). A call still out JUDGE_SECOND_SECONDS on is made a second
    time beside the first and the first to answer is the judgement. A call that answers in time is made once; one
    that fails at once is not made again; and when the first fails while the second is out, the second answers."""
    import json
    import threading

    state = {"question": "total amount", "workbook": []}
    options = [("r00", "the total of Amount -> answer: 7"), ("none", "None of these.")]
    answer = {"candidates": [{"content": {"parts": [{"text": json.dumps({"choice": "r00"})}]}}]}
    held = threading.Event()

    def posting(first):
        sent = []

        def post(url, payload, timeout, attempts, label):
            sent.append(len(sent))
            if sent[-1] == 0:
                return first()
            return answer
        return sent, post

    def stalled():
        held.wait(10)
        return {"candidates": [{"content": {"parts": [{"text": json.dumps({"choice": "none"})}]}}]}

    def stalled_then_failed():
        held.wait(10)
        raise llm.LLMUnavailable("Judge call failed (TimeoutError)")

    def failed():
        raise llm.LLMUnavailable("Judge call failed (503)")

    try:
        with patch.object(llm, "JUDGE_TRANSPORT", llm._post_judge), patch.object(llm, "JUDGE_SECOND_SECONDS", 0.05):
            sent, post = posting(stalled)
            with patch.object(llm, "_post_developer", post):
                assert llm.judge(state, "Choose.", options) == "r00" and sent == [0, 1], "the second call answered while the first was held"
            sent, post = posting(lambda: answer)
            with patch.object(llm, "_post_developer", post):
                assert llm.judge(state, "Choose.", options) == "r00" and sent == [0], "a call that answers in time is made once"
            sent, post = posting(failed)
            with patch.object(llm, "_post_developer", post):
                _unavailable(lambda: llm.judge(state, "Choose.", options))
                assert sent == [0], "a call that fails at once is not made again"
            sent, post = posting(stalled_then_failed)
            with patch.object(llm, "_post_developer", post):
                assert llm.judge(state, "Choose.", options) == "r00" and sent == [0, 1]
    finally:
        held.set()


def test_json_request_is_deterministic_and_carries_the_schema():
    models = _Models(reply='{"sql": "SELECT 1"}')
    with _gemini(), _serving(models):
        out = llm.generate_text(system="the rules", prompt="the question", max_output_tokens=1024,
                                json_schema=SCHEMA)
    assert out == '{"sql": "SELECT 1"}'
    call = models.calls[0]
    assert call["model"] == "gemini-test" and call["contents"] == "the question", call
    request = call["config"]
    assert isinstance(request, types.GenerateContentConfig)
    assert request.system_instruction == "the rules" and request.max_output_tokens == 1024
    assert request.temperature == 0.0 and request.seed == 0
    assert request.response_mime_type == "application/json" and request.response_json_schema == SCHEMA
    assert request.http_options.timeout == 30_000

    plain = _Models(reply="Hello there.")
    with _gemini(), _serving(plain):
        assert llm.generate_text(system="s", prompt="p", max_output_tokens=64, timeout_seconds=5) == "Hello there."
    request = plain.calls[0]["config"]
    assert request.response_mime_type is None and request.response_json_schema is None
    assert request.temperature == 0.0 and request.seed == 0 and request.http_options.timeout == 5_000


def test_failures_reach_callers_only_as_unavailable():
    """Every failure reaches the caller as LLMUnavailable, the one exception callers handle. Messages
    name the failure type only: an upstream error can quote the request."""
    for models in (_Models(error=RuntimeError("upstream 500 for SELECT secret")),
                   _Models(error=httpx.ReadTimeout("timed out")), _Models(error=TimeoutError()),
                   _Models(reply=None), _Models(reply="  \n")):
        with _gemini(), _serving(models):
            exc = _unavailable(lambda: llm.generate_text(system="s", prompt="p", max_output_tokens=8))
        assert "secret" not in str(exc), exc

    def no_credentials():
        raise DefaultCredentialsError("Your default credentials were not found")

    with _gemini(), patch.object(llm, "_client", no_credentials):
        _unavailable(lambda: llm.generate_text(system="s", prompt="p", max_output_tokens=8))
        _unavailable(lambda: list(llm.stream_text(system="s", prompt="p", max_output_tokens=8)))


def test_stream_yields_chunks_and_fails_as_unavailable():
    models = _Models(chunks=['{"columns": ["a"]}\n', None, '{"row": ["x"]}\n'])
    with _gemini(), _serving(models):
        assert list(llm.stream_text(system="s", prompt="p", max_output_tokens=8192)) == [
            '{"columns": ["a"]}\n', '{"row": ["x"]}\n']
    request = models.calls[0]["config"]
    assert request.max_output_tokens == 8192 and request.http_options.timeout == 60_000
    assert request.temperature == 0.0 and request.seed == 0 and request.response_mime_type is None

    # A stream that breaks keeps what the caller already took, then reports the outage.
    taken = []
    with _gemini(), _serving(_Models(chunks=['{"row": ["x"]}\n', RuntimeError("reset")])):
        _unavailable(lambda: taken.extend(llm.stream_text(system="s", prompt="p", max_output_tokens=8)))
    assert taken == ['{"row": ["x"]}\n']
    with _gemini(), _serving(_Models(chunks=[None, ""])):
        _unavailable(lambda: list(llm.stream_text(system="s", prompt="p", max_output_tokens=8)))


def test_one_client_per_process_is_created_under_a_lock():
    created = []

    def vertex_client(**kwargs):
        created.append(kwargs)
        time.sleep(0.05)                                   # widen the window a second creation would need
        return object()

    clients = []
    with _gemini(), patch.object(llm, "_CLIENT", None), patch("google.genai.Client", vertex_client):
        threads = [threading.Thread(target=lambda: clients.append(llm._client())) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert created == [{"vertexai": True, "project": "test-project", "location": config.GEMINI_LOCATION}]
    assert len(clients) == 8 and all(client is clients[0] for client in clients)


# ---------- the chat facade ----------

def _model_turn(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=list(parts)))])


class _VertexClient:
    """The async surface of google.genai.Client the facade uses, answering scripted responses."""

    def __init__(self, responses):
        self.requests, self.closed, pending = [], [], list(responses)
        outer = self

        class Models:
            async def generate_content(self, **kwargs):
                outer.requests.append(kwargs)
                return pending.pop(0)

        class Aio:
            models = Models()

            async def aclose(self):
                outer.closed.append("async")

        self.aio = Aio()

    def close(self):
        self.closed.append("sync")


def test_chat_facade_replays_a_function_call_with_its_thought_signature():
    """Gemini 3 rejects a replayed function call without the signature it was issued with, and the
    describe tool takes no arguments: its empty argument object once crashed the replay."""
    vertex = _VertexClient([
        _model_turn(types.Part(text="Let me look.", thought_signature=b"text-signature"),
                    types.Part(function_call=types.FunctionCall(name="prereasoner_describe", args={}),
                               thought_signature=b"call-signature")),
        _model_turn(types.Part(text="You uploaded two tables.")),
    ])

    async def turn():
        async with llm.AsyncGeminiClient(model="gemini-test", project="p") as client:
            messages = [{"role": "user", "content": "what did I upload?"}]
            async with client.messages.stream(model="gemini-test", max_tokens=64, system="rules",
                                              tools=TOOLS, messages=messages) as stream:
                first = await stream.get_final_message()
            messages.append({"role": "assistant", "content": first.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": first.content[1].id, "content": '{"tables": 2}'},
                {"type": "text", "text": "Answer on its own."},
            ]})
            async with client.messages.stream(model="gemini-test", max_tokens=64, system="rules",
                                              tools=TOOLS, messages=messages) as stream:
                chunks = [chunk async for chunk in stream.text_stream]
                second = await stream.get_final_message()
        return first, chunks, second

    with patch("google.genai.Client", lambda **_kwargs: vertex):
        first, chunks, second = asyncio.run(turn())
    assert first.stop_reason == "tool_use" and [block.type for block in first.content] == ["text", "tool_use"]
    assert first.content[1].name == "prereasoner_describe" and first.content[1].input == {}
    assert second.stop_reason == "end_turn" and chunks == ["You uploaded two tables."]
    model, answer, note = vertex.requests[1]["contents"][1:]
    assert model.role == "model"
    assert (model.parts[0].text, model.parts[0].thought_signature) == ("Let me look.", b"text-signature")
    assert model.parts[1].function_call.name == "prereasoner_describe"
    assert model.parts[1].function_call.args == {} and model.parts[1].thought_signature == b"call-signature"
    # A turn of function responses holds nothing else: with the chat's note beside them, Vertex
    # answered 400 "Requests ending with a model turn are not supported" (2026-10-02).
    assert answer.role == "user" and len(answer.parts) == 1
    assert answer.parts[0].function_response.name == "prereasoner_describe"
    assert answer.parts[0].function_response.response == {"result": '{"tables": 2}'}
    assert note.role == "user" and [part.text for part in note.parts] == ["Answer on its own."]
    assert vertex.closed == ["async", "sync"]
    # The chat's turns run at temperature 0 with seed 0, as the module documents for every call (the planted-text
    # test, 2026-10-08, found the async client setting neither).
    for request in vertex.requests:
        assert request["config"].temperature == 0.0 and request["config"].seed == 0


def test_chat_facade_maps_tool_choice_to_function_calling_modes():
    """The tool loop forces the query call in its recalculation round and turns calls off in its
    presentation round; both reach Gemini. An argument the facade does not implement is refused,
    not silently dropped as tool_choice once was."""
    vertex = _VertexClient([_model_turn(types.Part(text="ok")) for _ in range(4)])

    async def rounds():
        async with llm.AsyncGeminiClient(model="gemini-test") as client:
            for choice in ({"type": "tool", "name": "prereasoner_query"}, {"type": "none"}, None):
                options = {} if choice is None else {"tool_choice": choice}
                async with client.messages.stream(model="", max_tokens=64, system="rules", tools=TOOLS,
                                                  messages=[{"role": "user", "content": "total"}],
                                                  **options) as stream:
                    await stream.get_final_message()
            async with client.messages.stream(model="", max_tokens=64, system="rules",
                                              messages=[{"role": "user", "content": "hi"}]) as stream:
                await stream.get_final_message()
            try:
                await client.generate(model="", max_tokens=64, system="rules", messages=[],
                                      temperature=0.5)
            except TypeError:
                return
            raise AssertionError("an unimplemented argument was silently dropped")

    with patch("google.genai.Client", lambda **_kwargs: vertex):
        asyncio.run(rounds())
    forced, off, auto, bare = (request["config"] for request in vertex.requests)
    assert forced.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.ANY
    assert forced.tool_config.function_calling_config.allowed_function_names == ["prereasoner_query"]
    assert off.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.NONE
    assert auto.tool_config is None
    for request in (forced, off, auto):
        assert [tool.name for tool in request.tools[0].function_declarations] == [
            "prereasoner_query", "prereasoner_describe"]
    assert bare.tools is None and bare.tool_config is None
    assert {request["model"] for request in vertex.requests} == {"gemini-test"}


def test_chat_facade_passes_the_thinking_level():
    """The chat rounds run at LOW thinking (orchestrator.MODEL_THINKING): at the model's default a
    round once took 34 s. A round without a level leaves the model's default."""
    vertex = _VertexClient([_model_turn(types.Part(text="ok")) for _ in range(2)])

    async def rounds():
        async with llm.AsyncGeminiClient(model="gemini-test") as client:
            for options in ({"thinking": "LOW"}, {}):
                async with client.messages.stream(model="", max_tokens=64, system="rules", tools=TOOLS,
                                                  messages=[{"role": "user", "content": "total"}],
                                                  **options) as stream:
                    await stream.get_final_message()

    with patch("google.genai.Client", lambda **_kwargs: vertex):
        asyncio.run(rounds())
    low, default = (request["config"] for request in vertex.requests)
    assert low.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert default.thinking_config is None



def test_chat_rounds_are_bounded_and_sent_once_more():
    """Vertex's shared pool held single chat rounds for 50-120 s while its median stayed under 2 s (2026-10-10), and
    the rounds had no timeout. Each round now waits CHAT_ROUND_TIMEOUT_SECONDS and is sent again after a timeout, a
    429 or a 5xx, up to CHAT_ROUND_ATTEMPTS times: two held attempts of 20 s failed 5 turns (2026-10-27). The timeout is
    also Vertex's deadline (X-Server-Timeout): 10 s answered every longer round 504, so a compound follow-up failed
    each time it was asked, and 20 s cut a queued first round that answered in 5.6-38.4 s (2026-10-27)."""
    vertex = _VertexClient([_model_turn(types.Part(text="ok"))])

    async def one_round():
        async with llm.AsyncGeminiClient(model="gemini-test") as client:
            async with client.messages.stream(model="", max_tokens=64, system="rules",
                                              messages=[{"role": "user", "content": "total"}]) as stream:
                await stream.get_final_message()

    with patch("google.genai.Client", lambda **_kwargs: vertex):
        asyncio.run(one_round())
    options = vertex.requests[0]["config"].http_options
    assert options.timeout == llm.CHAT_ROUND_TIMEOUT_SECONDS * 1000 == 45_000
    assert options.retry_options.attempts == 2
    assert {429, 503, 504} <= set(options.retry_options.http_status_codes)

def test_external_calls_share_the_request_deadline():
    from engine import request_deadline
    token = request_deadline.begin(2)
    try:
        configuration = llm._config('rules', 64, 30)
        assert 0 < configuration.http_options.timeout <= 2000
    finally:
        request_deadline.end(token)
    assert llm._config('rules', 64, 30).http_options.timeout == 30000


TESTS = [
    test_external_calls_share_the_request_deadline,
    test_unconfigured_gemini_is_unavailable_without_a_call,
    test_a_decision_sends_one_sorted_body_and_returns_each_questions_options,
    test_json_request_is_deterministic_and_carries_the_schema,
    test_the_retry_query_sends_one_sorted_body_and_reads_its_json_answer,
    test_the_judge_sends_the_choices_question_and_reads_one_key,
    test_a_judge_still_out_is_asked_a_second_time_and_the_first_answer_is_read,
    test_failures_reach_callers_only_as_unavailable,
    test_stream_yields_chunks_and_fails_as_unavailable,
    test_one_client_per_process_is_created_under_a_lock,
    test_chat_facade_replays_a_function_call_with_its_thought_signature,
    test_chat_facade_maps_tool_choice_to_function_calling_modes,
    test_chat_facade_passes_the_thinking_level,
    test_chat_rounds_are_bounded_and_sent_once_more,
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
    print(f"\nllm: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
