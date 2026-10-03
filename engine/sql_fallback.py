"""One isolated Gemini wording rewrite for deterministic own-data SQL search.

Gemini receives one question and the request schema; it receives no conversation history, and its
reply is never cached. Only ``TableQuery``'s typed search can construct executable SQL.
"""
from __future__ import annotations

import json
import re

from engine import llm, request_timing
from engine.sql_prompt import REWRITE_SCHEMA, REWRITE_SYSTEM, rewrite_prompt

MAX_QUESTION_CHARS = 500
UNAVAILABLE = "Gemini unavailable"


class SQLFallback:
    """Optional one-shot question rewriter, enabled only by the operator's external-model switch."""

    def __init__(self, client=llm):
        self._llm = client

    @property
    def available(self) -> bool:
        return self._llm.available()

    @property
    def model(self) -> str:
        return self._llm.model_id()

    def rewrite(self, question: str, graph) -> tuple[str | None, str]:
        """Return a single rewording or a reason no safe rewording was available."""
        prompt = rewrite_prompt(graph, question)
        request_timing.count("fallback_rewrite_calls")
        try:
            raw = self._llm.generate_text(system=REWRITE_SYSTEM, prompt=prompt, max_output_tokens=4096,
                                          json_schema=REWRITE_SCHEMA)
        except self._llm.LLMUnavailable:
            return None, UNAVAILABLE
        try:
            value = json.loads(raw).get("question")
        except (ValueError, AttributeError):
            value = None
        value = value.strip() if isinstance(value, str) else None
        if not value or len(value) > MAX_QUESTION_CHARS:
            return None, "no usable reply"
        if " ".join(value.casefold().split()) == " ".join(question.casefold().split()):
            return None, "rewording unchanged"
        if not _preserves_explicit_constraints(question, value, graph):
            return None, "rewording changed a stated value or number"
        return value, ""


def _preserves_explicit_constraints(question: str, rewritten: str, graph) -> bool:
    """Keep request literals intact without sending workbook cell values to Gemini."""
    from engine.sql_search import SQLSearcher, _tokens

    original = " ".join(question.casefold().split())
    candidate = " ".join(rewritten.casefold().split())
    numbers = re.findall(r"(?<![a-z0-9])[+-]?\d[\d,]*(?:\.\d+)?(?![a-z0-9])", original)
    if any(number.casefold() not in candidate for number in numbers):
        return False
    quoted = re.findall(r"[\"']([^\"']{1,160})[\"']", question)
    if any(" ".join(value.casefold().split()) not in candidate for value in quoted):
        return False
    searcher = SQLSearcher(graph)
    matches, _occupied = searcher._value_matches(_tokens(question), frozenset(), question)
    for _start, _end, phrase, _options in matches:
        if " ".join(phrase.casefold().split()) not in candidate:
            return False
    return True
