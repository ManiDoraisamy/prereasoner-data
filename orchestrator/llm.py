"""Provider-neutral async chat client adapters.

The orchestrator's tool loop was originally written against Anthropic's message
stream API.  Community Edition can use Vertex AI Gemini without changing that
loop or the production default: this module translates the small subset of the
Anthropic message/tool protocol that the loop needs to Gemini's content API.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GeminiBlock:
    type: str
    text: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    id: str = ""


@dataclass
class GeminiResponse:
    content: list[GeminiBlock]
    stop_reason: str


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
    """Small Anthropic-message-shaped facade over ``google-genai`` Vertex AI."""

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
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close:
                close()
        self._client = None

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
                    block_type = getattr(block, "type", None) or block.get("type")
                    if block_type == "text":
                        text = getattr(block, "text", None) or block.get("text", "")
                        if text:
                            parts.append(types.Part.from_text(text=text))
                    elif block_type == "tool_use":
                        name = getattr(block, "name", None) or block.get("name", "")
                        args = getattr(block, "input", None) or block.get("input", {})
                        tool_id = getattr(block, "id", None) or block.get("id")
                        if tool_id:
                            self._tool_names[tool_id] = name
                        parts.append(types.Part.from_function_call(name=name, args=args))
                    elif block_type == "tool_result":
                        tool_id = getattr(block, "tool_use_id", None) or block.get("tool_use_id", "")
                        name = self._tool_names.get(tool_id, tool_id)
                        content = getattr(block, "content", None) or block.get("content", "")
                        is_error = getattr(block, "is_error", None)
                        if is_error is None:
                            is_error = block.get("is_error", False)
                        response = {"error": content} if is_error else {"result": content}
                        parts.append(types.Part.from_function_response(name=name, response=response))
            if parts:
                contents.append(types.Content(role=role, parts=parts))
        return contents

    async def generate(self, *, model: str, max_tokens: int, system: str,
                       tools: list[dict[str, Any]], messages: list[dict[str, Any]], **_: Any) -> GeminiResponse:
        if self._client is None:
            raise RuntimeError("Gemini client was not entered")
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=model or self.model,
            contents=self._contents(messages),
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                tools=[self._tools(tools)],
            ),
        )
        content: list[GeminiBlock] = []
        text = getattr(response, "text", None) or ""
        if text:
            content.append(GeminiBlock(type="text", text=text))
        for call in (getattr(response, "function_calls", None) or []):
            name = getattr(call, "name", "")
            args = dict(getattr(call, "args", None) or {})
            block_id = f"gemini:{name}:{uuid.uuid4().hex}"
            self._tool_names[block_id] = name
            content.append(GeminiBlock(type="tool_use", name=name, input=args, id=block_id))
        return GeminiResponse(content=content, stop_reason="tool_use" if any(
            block.type == "tool_use" for block in content
        ) else "end_turn")


def create_client(provider: str, *, api_key: str | None, model: str,
                  project: str | None = None, location: str = "global",
                  anthropic_client_cls=None):
    provider = (provider or "anthropic").strip().lower()
    if provider == "gemini":
        return AsyncGeminiClient(model=model, project=project, location=location)
    if provider == "anthropic":
        if anthropic_client_cls is None:
            from anthropic import AsyncAnthropic
            anthropic_client_cls = AsyncAnthropic
        return anthropic_client_cls(api_key=api_key)
    raise ValueError(f"unsupported LLM provider: {provider}")
