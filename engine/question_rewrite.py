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
    # but cannot turn a known comparison, aggregate or ranking into another. A count is
    # the engine's reading of one (sql_expansion.count_requested): "the number of
    # products" of Number_products and "the phone number" ask for a field. Groups are
    # held by the answer check after the reworded search (constraint_violations of the
    # original question: "requested output grain is missing"), not by words: "miles per
    # gallon", "song names by singers" and "produced by the maker" group nothing, and 30
    # DEV rewordings were refused for losing such a word (2026-10-13).
    from engine.sql_expansion import count_requested
    fields = [column.ref for column in graph.columns]

    def roles(text):
        counted = count_requested(text, fields)
        text = " ".join(lexical_words(text))
        for column in sorted(graph.columns, key=lambda c: -len(c.ref.name)):
            text = text.replace(" ".join(lexical_words(column.ref.name)), " ")
        patterns = {
            "sum": r"\b(?:sum|total)\b", "avg": r"\b(?:avg|average|mean)\b",
            "max": r"\b(?:max|maximum)\b", "min": r"\b(?:min|minimum)\b",
            "greater": r"\b(?:greater than|more than|over|above|at least)\b",
            "less": r"\b(?:less than|fewer than|under|below|at most)\b",
            "before": r"\bbefore\b", "after": r"\bafter\b", "between": r"\bbetween\b",
            "top": r"\btop\b", "bottom": r"\bbottom\b",
            "share": r"\b(?:share|percentage|percent)\b",
        }
        return {role for role, pattern in patterns.items() if re.search(pattern, text)} | (
            {"count"} if counted else set())
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
    for _start, _end, phrase, options in matches:
        # A rewording keeps a value it says itself where the question said a word for it: "Asian countries" is
        # "countries in Asia" (Spider DEV 762, 763, 2026-10-15: refused once "Asian" read as 'Asia').
        if " ".join(phrase.casefold().split()) not in candidate and not any(
                re.search(r"(?<!\w)" + re.escape(" ".join(str(value).casefold().split())) + r"(?!\w)", candidate)
                for _column, value in options if len(str(value).strip()) > 1):
            return False
    # The English candidate search tokenizer cannot be the literal-preservation
    # authority for names such as 東京. Bind those locally with Unicode boundaries.
    # A value in ASCII is the search's to read, and is checked above: a country code
    # 'IN' or 'ARE' spelled as an English word refused 34 rewordings that dropped
    # "in" or "are" (Spider DEV, 2026-10-12).
    for column in graph.columns:
        for value in set(str(v) for v in column.values if v is not None):
            value = " ".join(value.casefold().split())
            if value.isascii():
                continue
            if value and re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", original):
                if not re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", candidate):
                    return False
    return True
