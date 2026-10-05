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
# Rewording one question needs little reasoning. At the model's default thinking (about 1,200 thought tokens)
# "keyword volume for home inspection checklist" took 12-14 s and, once, the whole 30 s client timeout, so a
# Sheets user waited a minute for no answer (2026-10-05). LOW thinking (about 560 tokens; the model rejects
# MINIMAL) answered in 6-14 s, and the call stops at 20 s.
REWRITE_THINKING = "LOW"
REWRITE_TIMEOUT_SECONDS = 20.0


class QuestionRewriter:
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
                                          json_schema=REWRITE_SCHEMA, thinking=REWRITE_THINKING,
                                          timeout_seconds=REWRITE_TIMEOUT_SECONDS)
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
    from engine.query_contract import lexical_words
    from engine.closed_class import EXCLUSION_CUES

    if bool(EXCLUSION_CUES.search(question)) != bool(EXCLUSION_CUES.search(rewritten)):
        return False
    from engine.currency_intent import currency_conversion_target
    target = currency_conversion_target(question)
    if target is not None and currency_conversion_target(rewritten) != target:
        return False
    # Preserve recognized computation roles. A rewrite may replace a schema label,
    # but cannot turn a known comparison, aggregate, ranking or grouping into another.
    def roles(text):
        text = " ".join(lexical_words(text))
        for column in sorted(graph.columns, key=lambda c: -len(c.ref.name)):
            text = text.replace(" ".join(lexical_words(column.ref.name)), " ")
        patterns = {
            "sum": r"\b(?:sum|total)\b", "avg": r"\b(?:avg|average|mean)\b",
            "max": r"\b(?:max|maximum)\b", "min": r"\b(?:min|minimum)\b",
            "count": r"\b(?:count|how many|number of)\b",
            "greater": r"\b(?:greater than|more than|over|above)\b",
            "less": r"\b(?:less than|fewer than|under|below)\b",
            "before": r"\bbefore\b", "after": r"\bafter\b", "between": r"\bbetween\b",
            "top": r"\btop\b", "bottom": r"\bbottom\b",
            "share": r"\b(?:share|percentage|percent)\b",
            "group": r"\b(?:by|per|each)\b",
        }
        return {role for role, pattern in patterns.items() if re.search(pattern, text)}
    if not roles(question).issubset(roles(rewritten)):
        return False
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
    # The English candidate search tokenizer cannot be the literal-preservation
    # authority for names such as 東京. Bind those locally with Unicode boundaries.
    for column in graph.columns:
        for value in set(str(v) for v in column.values if v is not None):
            value = " ".join(value.casefold().split())
            if value and re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", original):
                if not re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", candidate):
                    return False
    return True
