"""llm.py — the ONE Gemini client (Vertex AI through ``google-genai``) for the whole product.

Gemini is the only external model. Two surfaces share this module:

- The synchronous calls of the engine's threaded HTTP server: ``generate_text`` (the labelled
  wording rewrite, engine/question_rewrite.py) and ``stream_text`` (reference
  generation, /api/master/generate). One ``genai.Client`` per process, created lazily under a lock.
  Every call runs at temperature 0 with seed 0, and every failure, "not enabled" included, reaches
  the caller as ``LLMUnavailable``.
- ``AsyncGeminiClient``, driven by the chat orchestrator's tool loop (orchestrator/orchestrator.py).
  The loop speaks a small content-block protocol (``messages.stream``, ``text_stream``,
  ``get_final_message``, and ``text`` / ``tool_use`` / ``tool_result`` blocks); this class
  translates it to Gemini's content API.

``EXTERNAL_LLM_ENABLED`` (engine/config.py) is the operator's authoritative switch for every call.
Vertex AI authenticates with Application Default Credentials: the Cloud Run service account in a
deployment, ``gcloud auth application-default login`` locally. Nothing here logs a prompt, a
question, a cell, or a reply (engine/request_timing.py rules).
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from engine import config


class LLMUnavailable(RuntimeError):
    """Gemini is disabled, unconfigured, or the call failed or timed out."""


_CLIENT = None
_CLIENT_LOCK = threading.Lock()

# A chat round waits this long for Gemini, then sends the round once more. Vertex's shared pool held single requests
# for 50-120 s while its median stayed under 2 s (2026-10-10: 6 of 362 turns, every call answered 200), and the
# round had no timeout, so the turn waited them out. A 429 or 5xx is sent again on the same budget. The SDK retries a
# timeout raised by httpx, the chat image's transport (orchestrator/requirements.lock.txt holds no aiohttp).
CHAT_ROUND_TIMEOUT_SECONDS = 20
CHAT_ROUND_ATTEMPTS = 2
CHAT_RETRY_STATUS_CODES = (408, 429, 500, 502, 503, 504)


def model_id() -> str:
    """The configured Gemini model id (GEMINI_MODEL)."""
    return config.llm_model()


def available() -> bool:
    """Whether the operator enabled external model processing and configured Gemini."""
    return config.external_llm_enabled() and config.llm_configured()


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
        raise LLMUnavailable("Gemini is not enabled for this deployment")
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
        raise LLMUnavailable("Gemini is not enabled for this deployment")
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
