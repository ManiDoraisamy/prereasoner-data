"""llm.py — the ONE Gemini client for the whole product.

Gemini is the only external model, and it is always on: there is no switch. Four surfaces share this module:

- ``decide``: one call to the Gemini Developer API's closed-choice Decisions endpoint. A record's STATE (the
  question and the sheet's description) is the input; each of its questions is a closed choice among options the
  engine built, answered as a probability for every option. It returns probabilities only, never text, SQL or a
  program. It authenticates with GOOGLE_API_KEY (the Developer API; the endpoint is not on Vertex AI).
- ``write_query``: the own-data retry (engine/retry.py). One generateContent call to QUERY_MODEL (Gemini 4 Neon, on
  the Developer API only) that writes one SQLite SELECT over the request's tables, or says the tables cannot answer
  the question, as a JSON document; the caller guards and runs it. Owner's decision, 2026-11-21: the Decisions path
  never asks back for its own uncertainty, and its answer most likely wrong, or one the person retries, is retried
  by this call. It authenticates as ``decide`` does.
- ``judge``: the own-data choice's one closed question put to a second model (JUDGE_MODEL, one generateContent call on
  the Developer API), its answer held by a schema to the key of one option the engine built: an answer stays unmarked
  only where both judges pick it (engine/program/closed.py choose_answer). Owner's decision, 2026-11-26. It writes no
  text, SQL or program, and authenticates as ``decide`` does.

- The synchronous calls: ``generate_text`` (starter questions, orchestrator/suggestions.py) and
  ``stream_text`` (reference generation, /api/master/generate). One ``genai.Client`` per process, created lazily under a lock.
  Every call runs at temperature 0 with seed 0, and every failure, "not enabled" included, reaches
  the caller as ``LLMUnavailable``.
- ``AsyncGeminiClient``, driven by the chat orchestrator's tool loop (orchestrator/orchestrator.py).
  The loop speaks a small content-block protocol (``messages.stream``, ``text_stream``,
  ``get_final_message``, and ``text`` / ``tool_use`` / ``tool_result`` blocks); this class
  translates it to Gemini's content API.

Vertex AI authenticates with Application Default Credentials: the Cloud Run service account in a
deployment, ``gcloud auth application-default login`` locally. Nothing here logs a prompt, a
question, a cell, or a reply (engine/request_timing.py rules).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine import config


class LLMUnavailable(RuntimeError):
    """Gemini is unconfigured, or the call failed or timed out."""


_CLIENT = None
_CLIENT_LOCK = threading.Lock()

# A chat round waits this long for Gemini, then sends the round again. Vertex's shared pool held single requests
# for 50-120 s while its median stayed under 2 s (2026-10-10: 6 of 362 turns, every call answered 200), and the
# round had no timeout, so the turn waited them out. A 429 or 5xx is sent again on the same budget. The SDK retries a
# timeout raised by httpx, the chat image's transport (orchestrator/requirements.lock.txt holds no aiohttp).
# The SDK sends the timeout to Vertex as X-Server-Timeout, so it is a server deadline too: a round that needs longer
# is answered 504 every time it is sent. 10 s (2a82e2c) failed a compound follow-up's round twice in a row and raised
# Vertex's 504s from about 1 an hour to a quarter of its calls (2026-10-27). Its latency queues, not only holds: the
# same 87-token first round of a compound question took 5.6-38.4 s (median 27 s, 12 of 12 answered) that evening, so
# a 20 s round failed it three attempts running. A round waits 45 s, twice at most (90 s; the chat may run 300 s).
CHAT_ROUND_TIMEOUT_SECONDS = 45
CHAT_ROUND_ATTEMPTS = 2
CHAT_RETRY_STATUS_CODES = (408, 429, 500, 502, 503, 504)


def model_id() -> str:
    """The configured Gemini model id (GEMINI_MODEL)."""
    return config.llm_model()


def available() -> bool:
    """Whether Gemini on Vertex AI is configured (a project; credentials are Application Default Credentials)."""
    return config.llm_configured()



DECISIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
DECISIONS_MAX_OPTIONS = 255              # the endpoint's limit on one question's options
# A call answered in 0.4 s at the median and 2.2 s at p90 over 2,655 calls (decisions-probe-20261118); a stalled call
# is sent again rather than waited out. 429 and 5xx are sent again on the same budget, with the server's Retry-After.
DECISIONS_TIMEOUT_SECONDS = 20.0
DECISIONS_ATTEMPTS = 4
DECISIONS_RETRY_STATUS_CODES = (408, 429, 500, 502, 503, 504)


@dataclass(frozen=True)
class Decision:
    """One Decisions answer: for each question, the probability of each of its options; the input tokens billed."""
    fields: dict[str, dict[str, float]]
    tokens: int


def decisions_payload(state: dict, questions: dict) -> bytes:
    """The request body, as bytes: one STATE input and one closed choice per question. Keys are sorted, so a record
    gives one body, and an evaluation can replay a recorded answer by the body's hash (``decision_hash``)."""
    body = {"model": config.DECISIONS_MODEL, "input": "STATE:\n" + json.dumps(state, ensure_ascii=False), "questions": {}}
    for name, question in questions.items():
        criteria = question["criteria"]
        if not 2 <= len(criteria) <= DECISIONS_MAX_OPTIONS:
            raise ValueError(f"question {name} has {len(criteria)} options")
        body["questions"][name] = {"type": "choice", "instructions": question["instructions"], "criteria": criteria}
    return json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")


def decision_hash(payload: bytes) -> str:
    """The body's identity (sha256), the key a recorded answer is replayed by."""
    return hashlib.sha256(payload).hexdigest()


def _post_developer(url: str, payload: bytes, timeout_seconds: float, attempts: int, label: str) -> dict:
    """One body POSTed to a Gemini Developer API endpoint with the deployment's key, sent again on a stall, a 429 or a
    5xx (with the server's Retry-After) within the request's time; the parsed JSON answer. Raises LLMUnavailable."""
    from engine.request_deadline import remaining

    key = config.decisions_key()
    if not key:
        raise LLMUnavailable(f"the {label} call has no key for this deployment")
    wait = 1.0
    for attempt in range(attempts):
        budget = remaining()
        timeout = timeout_seconds if budget is None else max(0.1, min(timeout_seconds, budget))
        request = urllib.request.Request(url, data=payload, method="POST",
                                         headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in DECISIONS_RETRY_STATUS_CODES or attempt + 1 == attempts:
                raise LLMUnavailable(f"{label} call failed ({exc.code})") from None
            after = exc.headers.get("Retry-After") if exc.headers else None
            pause = float(after) if after and after.replace(".", "", 1).isdigit() else wait
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as exc:
            if attempt + 1 == attempts:
                raise LLMUnavailable(f"{label} call failed ({type(exc).__name__})") from None
            pause = wait
        budget = remaining()
        if budget is not None and budget <= pause:
            raise LLMUnavailable(f"{label} call ran out of the request's time")
        time.sleep(pause)
        wait = min(wait * 2, 8.0)
    raise LLMUnavailable(f"{label} call failed")


def _post_decisions(payload: bytes) -> dict:
    """The endpoint's answer to one body: {"answers": {...}, "tokens": n}. Raises LLMUnavailable."""
    result = _post_developer(DECISIONS_ENDPOINT, payload, DECISIONS_TIMEOUT_SECONDS, DECISIONS_ATTEMPTS, "Decisions")
    try:
        return {"answers": result["answers"], "tokens": int((result.get("usage") or {}).get("total_input_tokens") or 0)}
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMUnavailable(f"Decisions call failed ({type(exc).__name__})") from None


# The transport ``decide`` sends a body through. An evaluation may set it to replay recorded answers by
# ``decision_hash``; serving never changes it.
DECISIONS_TRANSPORT = _post_decisions


QUERY_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Gemini 4 Neon at its low thinking level wrote a probe's query in 5.3 s (2026-11-21); high, its default, thinks far
# longer. It takes no temperature or seed: the same body may be answered with another query.
QUERY_THINKING = "low"
QUERY_MAX_OUTPUT_TOKENS = 8192
QUERY_TIMEOUT_SECONDS = 60.0
QUERY_ATTEMPTS = 2
QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean", "description": "whether the tables hold what the question asks"},
        "sql": {"type": "string", "description": "one SQLite SELECT statement answering the question; empty when not answerable"},
        "reason": {"type": "string", "description": "one sentence: how the query reads the question, or why the tables cannot answer it"},
    },
    "required": ["answerable", "sql", "reason"],
}


@dataclass(frozen=True)
class WrittenQuery:
    """The retry model's answer: one SQLite SELECT, or that the tables cannot answer the question, with its one-sentence
    reason; the tokens billed (input, output with thinking)."""
    answerable: bool
    sql: str
    reason: str
    input_tokens: int = 0
    output_tokens: int = 0


def query_payload(system: str, prompt: str) -> bytes:
    """The generateContent body of one retry: the system instruction, the prompt, the JSON schema of its answer and the
    thinking level. Keys are sorted, so a prompt gives one body (``decision_hash`` keys a recorded answer)."""
    body = {"model": config.QUERY_MODEL,
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": QUERY_SCHEMA,
                                 "thinkingConfig": {"thinkingLevel": QUERY_THINKING},
                                 "maxOutputTokens": QUERY_MAX_OUTPUT_TOKENS}}
    return json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _post_query(payload: bytes) -> dict:
    """The model's answer to one retry body: {"text": its JSON answer, "usage": {...}}. Raises LLMUnavailable."""
    body = json.loads(payload.decode("utf-8"))
    model = body.pop("model")
    result = _post_developer(QUERY_ENDPOINT.format(model=model), json.dumps(body, ensure_ascii=False).encode("utf-8"),
                             QUERY_TIMEOUT_SECONDS, QUERY_ATTEMPTS, "Retry query")
    try:
        parts = result["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable(f"Retry query call failed ({type(exc).__name__})") from None
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    return {"text": text, "usage": result.get("usageMetadata") or {}}


# The transport ``write_query`` sends a body through; an evaluation may replay recorded answers by ``decision_hash``.
QUERY_TRANSPORT = _post_query


def write_query(system: str, prompt: str) -> WrittenQuery:
    """One retry call: the query the model writes for ``prompt`` (engine/retry.py builds it), or its word that the
    tables cannot answer the question. Raises LLMUnavailable and nothing else."""
    answer = QUERY_TRANSPORT(query_payload(system, prompt))
    try:
        parsed = json.loads(answer.get("text") or "")
        answerable, sql, reason = bool(parsed["answerable"]), str(parsed.get("sql") or ""), str(parsed.get("reason") or "")
    except (ValueError, KeyError, TypeError) as exc:
        raise LLMUnavailable(f"Retry query answer is not its JSON ({type(exc).__name__})") from None
    usage = answer.get("usage") or {}
    return WrittenQuery(answerable and bool(sql.strip()), sql.strip(), reason.strip(),
                        int(usage.get("promptTokenCount") or 0),
                        int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0))


# ------------------------------------------------------------------------------------------------ the second judge
# The own-data choice's one closed question put to a second model (engine/program/answer.py): generateContent on the
# Developer API, the answer held by a JSON schema to the key of exactly one option the engine built. It writes no
# text, no SQL and no program. Gemini 4 Neon at its low thinking level answered in 4.9 s at the median, 8.3 s at p90
# and 11 s at p95 (2026-11-26, 606 questions; 16 took over 15 s); it takes no temperature or seed. One attempt within
# 15 s: an answer waits no longer for its second judge, and one the judge does not reach in time is warned as unread.
# A call still out after 8 s is sent a second time beside the first, and the first of the two to answer is the
# judgement: the provider's slow spells hold a call for 15 s and more (release 568b1f0a's gate: 5 of 115 waits ended
# by the clock, each answer right and served warned), and of 4,401 recorded calls 5.1% were out at 8 s and 0.5% at
# 15 s, so the second asking adds a twentieth to the judge's cost and leaves a second call 7 s, which 91% of calls
# answer within.
JUDGE_THINKING = "low"
JUDGE_MAX_OUTPUT_TOKENS = 16384
JUDGE_TIMEOUT_SECONDS = 15.0
JUDGE_SECOND_SECONDS = 8.0
JUDGE_ATTEMPTS = 1
JUDGE_SYSTEM = ("You answer one closed question about a person's spreadsheet. STATE describes the spreadsheet and the "
                "question asked of it, QUESTION says what to choose, and OPTIONS lists the options by key. Reply with the "
                "key of exactly one option.")


def judge_payload(state: dict, instructions: str, options: Sequence[tuple[str, str]], model: str | None = None,
                  thinking: str = JUDGE_THINKING) -> bytes:
    """The generateContent body of one judgement: the same STATE the Decisions endpoint is given, the instruction, and
    the options (key, text) in the order given; the answer held to one option's key by its schema. Keys are sorted, so
    a question and a judge give one body (``decision_hash`` keys a recorded answer)."""
    prompt = ("STATE:\n" + json.dumps(state, ensure_ascii=False) + "\n\nQUESTION: " + instructions
              + "\n\nOPTIONS:\n" + "\n".join(f"{key}: {text}" for key, text in options))
    schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": [key for key, _text in options]}}, "required": ["choice"]}
    body = {"model": model or config.JUDGE_MODEL, "systemInstruction": {"parts": [{"text": JUDGE_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                 "thinkingConfig": {"thinkingLevel": thinking}, "maxOutputTokens": JUDGE_MAX_OUTPUT_TOKENS}}
    return json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _first_to_answer(call, after: float):
    """``call()``'s answer, the call made a second time beside the first when the first is still out ``after`` seconds
    on: the answer of whichever returns first. A call that fails leaves the other to answer, and when none is left
    the first failure is raised; a first call that fails before ``after`` is not made again. The request's timing
    line counts the second asking (``judge_second``) and its answer when that is the one read
    (``judge_second_answered``)."""
    import contextvars
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    from engine import request_timing

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        first = pool.submit(contextvars.copy_context().run, call)
        out = {first}
        if not wait(out, timeout=after).done:
            request_timing.count("judge_second")
            out.add(pool.submit(contextvars.copy_context().run, call))
        failure = None
        while out:
            done, out = wait(out, return_when=FIRST_COMPLETED)
            for one in done:
                if one.exception() is None:
                    if one is not first:
                        request_timing.count("judge_second_answered")
                    return one.result()
                failure = failure or one.exception()
        raise failure
    finally:
        pool.shutdown(wait=False)     # a call still out ends by its own limit


def _post_judge(payload: bytes) -> dict:
    """The judge's answer to one body: {"text": its JSON answer, "usage": {...}, "seconds": the time to it}. The call
    is made a second time when it is still out JUDGE_SECOND_SECONDS on (_first_to_answer). Raises LLMUnavailable."""
    body = json.loads(payload.decode("utf-8"))
    model = body.pop("model")
    url, data = QUERY_ENDPOINT.format(model=model), json.dumps(body, ensure_ascii=False).encode("utf-8")
    clock = time.perf_counter()
    result = _first_to_answer(lambda: _post_developer(url, data, JUDGE_TIMEOUT_SECONDS, JUDGE_ATTEMPTS, "Judge"), JUDGE_SECOND_SECONDS)
    try:
        parts = result["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise LLMUnavailable(f"Judge call failed: no choice ({(result.get('candidates') or [{}])[0].get('finishReason')})") from None
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    return {"text": text, "usage": result.get("usageMetadata") or {}, "seconds": round(time.perf_counter() - clock, 2)}


# The transport ``judge`` sends a body through; an evaluation may replay recorded answers by ``decision_hash``.
JUDGE_TRANSPORT = _post_judge


def judged_key(payload: bytes, keys: Sequence[str]) -> str:
    """The key the second judge answers one body with (``judge_payload``), one of ``keys``. The body is built when the
    answer is chosen and sent when its mark is settled (engine/program/answer.py ask). Raises LLMUnavailable and
    nothing else."""
    answer = JUDGE_TRANSPORT(payload)
    try:
        choice = json.loads(answer.get("text") or "")["choice"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LLMUnavailable(f"Judge answer is not its JSON ({type(exc).__name__})") from None
    if choice not in set(keys):
        raise LLMUnavailable("Judge answer is not one of its options")
    return choice


def judge(state: dict, instructions: str, options: Sequence[tuple[str, str]]) -> str:
    """One judgement: the key of the option the second judge picks for a closed question. Raises LLMUnavailable and
    nothing else."""
    return judged_key(judge_payload(state, instructions, options), [key for key, _text in options])


def decide(state: dict, questions: dict) -> Decision:
    """One closed-choice call: ``questions`` maps a name to {"instructions", "criteria": {option: description}}.
    Returns each question's probabilities over exactly its options. Raises LLMUnavailable and nothing else."""
    payload = decisions_payload(state, questions)
    answer = DECISIONS_TRANSPORT(payload)
    fields = {}
    for name, question in questions.items():
        probabilities = ((answer.get("answers") or {}).get(name) or {}).get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != set(question["criteria"]):
            raise LLMUnavailable(f"Decisions answer for {name} does not hold its options")
        fields[name] = {option: float(value) for option, value in probabilities.items()}
    return Decision(fields, int(answer.get("tokens") or 0))

def _client():
    """The process's one Vertex AI client. Its credentials are read on its first request."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from google import genai

            _CLIENT = genai.Client(vertexai=True, project=config.GOOGLE_CLOUD_PROJECT,
                                   location=config.GEMINI_LOCATION)
        return _CLIENT


def _config(system: str, max_output_tokens: int, timeout_seconds: float,
            json_schema: dict | None = None, thinking: str | None = None):
    from google.genai import types
    from engine.request_deadline import remaining

    budget = remaining()
    if budget is not None:
        timeout_seconds = min(timeout_seconds, budget)

    json_mode = ({"response_mime_type": "application/json", "response_json_schema": json_schema}
                 if json_schema is not None else {})
    thinking_mode = ({"thinking_config": types.ThinkingConfig(thinking_level=thinking)}
                     if thinking is not None else {})
    return types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_output_tokens,
        temperature=0.0,
        seed=0,
        http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        **json_mode,
        **thinking_mode,
    )


def generate_text(*, system: str, prompt: str, max_output_tokens: int,
                  json_schema: dict | None = None, timeout_seconds: float = 30.0,
                  thinking: str | None = None) -> str:
    """Gemini's reply to ``prompt``: a JSON document of ``json_schema``'s shape when one is given.

    ``timeout_seconds`` bounds the call. ``thinking`` is the model's thinking level ("LOW", "HIGH");
    None leaves the model's default. Raises ``LLMUnavailable`` and nothing else."""
    if not available():
        raise LLMUnavailable("Gemini is not configured for this deployment")
    try:
        response = _client().models.generate_content(
            model=model_id(), contents=prompt,
            config=_config(system, max_output_tokens, timeout_seconds, json_schema, thinking),
        )
        text = response.text
    except Exception as exc:  # noqa: BLE001 — client, credential, API, and timeout failures are one outage to callers
        raise LLMUnavailable(f"Gemini call failed ({type(exc).__name__})") from exc
    if not text or not text.strip():
        raise LLMUnavailable("Gemini returned no text")
    return text


def stream_text(*, system: str, prompt: str, max_output_tokens: int,
                timeout_seconds: float = 60.0) -> Iterator[str]:
    """Yield Gemini's reply to ``prompt`` in chunks, as they arrive.

    ``timeout_seconds`` bounds each wait for the next chunk. Raises ``LLMUnavailable`` here when
    Gemini is not available, and from the iteration when the call fails or produces no text."""
    if not available():
        raise LLMUnavailable("Gemini is not configured for this deployment")
    return _stream(system, prompt, max_output_tokens, timeout_seconds)


def _stream(system: str, prompt: str, max_output_tokens: int, timeout_seconds: float) -> Iterator[str]:
    produced = False
    try:
        chunks = _client().models.generate_content_stream(
            model=model_id(), contents=prompt,
            config=_config(system, max_output_tokens, timeout_seconds),
        )
        for chunk in chunks:
            from engine.request_deadline import remaining
            remaining()
            text = chunk.text
            if text:
                produced = True
                yield text
    except Exception as exc:  # noqa: BLE001 — as in generate_text
        raise LLMUnavailable(f"Gemini stream failed ({type(exc).__name__})") from exc
    if not produced:
        raise LLMUnavailable("Gemini returned no text")


# ---------- the chat orchestrator's async client ----------

@dataclass
class GeminiBlock:
    type: str
    text: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    # Gemini signs its reasoning on the part that carries it. A function call sent back without its
    # signature is rejected by models that validate them, so every block keeps the one it came with.
    thought_signature: bytes | None = None


@dataclass
class GeminiResponse:
    content: list[GeminiBlock]
    stop_reason: str


def _field(block: Any, name: str, default: Any = None) -> Any:
    """A block's field, whether the block is a GeminiBlock or a dict the orchestrator built."""
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


class _GeminiMessageStream:
    def __init__(self, client: "AsyncGeminiClient", kwargs: dict[str, Any]):
        self._client = client
        self._kwargs = kwargs
        self._response: GeminiResponse | None = None

    async def __aenter__(self) -> "_GeminiMessageStream":
        self._response = await self._client.generate(**self._kwargs)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    @property
    def text_stream(self):
        return self._text_stream()

    async def _text_stream(self):
        for block in (self._response.content if self._response else []):
            if block.type == "text" and block.text:
                yield block.text

    async def get_final_message(self) -> GeminiResponse:
        if self._response is None:
            raise RuntimeError("Gemini message stream was not entered")
        return self._response


class _GeminiMessages:
    def __init__(self, client: "AsyncGeminiClient"):
        self._client = client

    def stream(self, **kwargs: Any) -> _GeminiMessageStream:
        return _GeminiMessageStream(self._client, kwargs)


class AsyncGeminiClient:
    """The chat tool loop's content-block protocol over ``google-genai`` Vertex AI, one per turn."""

    def __init__(self, *, model: str, project: str | None = None, location: str = "global"):
        self.model = model
        self.project = project
        self.location = location
        self.messages = _GeminiMessages(self)
        self._client = None
        self._tool_names: dict[str, str] = {}

    async def __aenter__(self) -> "AsyncGeminiClient":
        from google import genai

        self._client = genai.Client(
            vertexai=True,
            project=self.project,
            location=self.location,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aio.aclose()
            client.close()

    @staticmethod
    def _tools(tools: list[dict[str, Any]]):
        from google.genai import types

        return types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters_json_schema=tool.get("input_schema", {}),
            )
            for tool in tools
        ])

    @staticmethod
    def _tool_config(tool_choice: dict[str, Any] | None):
        """``{"type": "tool", "name": n}`` forces a call of ``n``; ``{"type": "none"}`` allows no call."""
        if tool_choice is None:
            return None
        from google.genai import types

        if tool_choice.get("type") == "tool":
            calling = types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY,
                allowed_function_names=[tool_choice["name"]],
            )
        elif tool_choice.get("type") == "none":
            calling = types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.NONE)
        else:
            raise ValueError(f"unsupported tool_choice: {tool_choice.get('type')!r}")
        return types.ToolConfig(function_calling_config=calling)

    def _contents(self, messages: list[dict[str, Any]]):
        from google.genai import types

        contents = []
        for message in messages:
            role = "model" if message.get("role") == "assistant" else "user"
            raw = message.get("content", "")
            parts = []
            if isinstance(raw, str):
                parts.append(types.Part.from_text(text=raw))
            elif isinstance(raw, list):
                for block in raw:
                    block_type = _field(block, "type")
                    signature = _field(block, "thought_signature")
                    if block_type == "text":
                        text = _field(block, "text", "")
                        if text:
                            parts.append(types.Part(text=text, thought_signature=signature))
                    elif block_type == "tool_use":
                        name = _field(block, "name", "")
                        tool_id = _field(block, "id", "")
                        if tool_id:
                            self._tool_names[tool_id] = name
                        parts.append(types.Part(
                            function_call=types.FunctionCall(name=name, args=_field(block, "input") or {}),
                            thought_signature=signature,
                        ))
                    elif block_type == "tool_result":
                        tool_id = _field(block, "tool_use_id", "")
                        name = self._tool_names.get(tool_id, tool_id)
                        content = _field(block, "content", "")
                        response = {"error": content} if _field(block, "is_error", False) else {"result": content}
                        parts.append(types.Part.from_function_response(name=name, response=response))
            # A turn of function responses holds nothing else. With a text part beside them (the chat's
            # note after the tool results of a reopened conversation) Vertex answers 400 "Requests ending
            # with a model turn are not supported", and every follow-up's presentation fell back to plain
            # text (2026-10-02). The text follows as a user turn of its own.
            responses = [part for part in parts if part.function_response is not None]
            if responses and len(responses) < len(parts):
                contents.append(types.Content(role=role, parts=responses))
                parts = [part for part in parts if part.function_response is None]
            if parts:
                contents.append(types.Content(role=role, parts=parts))
        return contents

    def _response(self, response) -> GeminiResponse:
        candidates = getattr(response, "candidates", None) or []
        content = getattr(candidates[0], "content", None) if candidates else None
        text, text_signature, calls = "", None, []
        for part in (getattr(content, "parts", None) or []):
            call = part.function_call
            if call is not None:
                name = call.name or ""
                block_id = f"gemini:{name}:{uuid.uuid4().hex}"
                self._tool_names[block_id] = name
                calls.append(GeminiBlock(type="tool_use", name=name, input=dict(call.args or {}),
                                         id=block_id, thought_signature=part.thought_signature))
            elif not part.thought:
                text += part.text or ""
                text_signature = text_signature or part.thought_signature
        blocks = ([GeminiBlock(type="text", text=text, thought_signature=text_signature)] if text else []) + calls
        return GeminiResponse(content=blocks, stop_reason="tool_use" if calls else "end_turn")

    async def generate(self, *, model: str, max_tokens: int, system: str,
                       messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                       tool_choice: dict[str, Any] | None = None,
                       thinking: str | None = None) -> GeminiResponse:
        """``thinking`` is the model's thinking level ("LOW", "HIGH"); None leaves the model's default."""
        if self._client is None:
            raise RuntimeError("Gemini client was not entered")
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=model or self.model,
            contents=self._contents(messages),
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                # As every call (the module doc): the chat's turns repeat, so a gate rerun compares replies.
                temperature=0.0,
                seed=0,
                tools=[self._tools(tools)] if tools else None,
                tool_config=self._tool_config(tool_choice),
                thinking_config=(types.ThinkingConfig(thinking_level=thinking)
                                 if thinking is not None else None),
                http_options=types.HttpOptions(
                    timeout=CHAT_ROUND_TIMEOUT_SECONDS * 1000,
                    retry_options=types.HttpRetryOptions(attempts=CHAT_ROUND_ATTEMPTS,
                                                         http_status_codes=list(CHAT_RETRY_STATUS_CODES)),
                ),
            ),
        )
        return self._response(response)
