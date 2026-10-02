"""The labelled Gemini fallback of own-data selection (engine/tables.py:TableQuery.select_query).

The deterministic search builds every query Prereasoner serves on its own. Only when its pool holds
no query that executes and is grounded, and the operator enabled external models
(``EXTERNAL_LLM_ENABLED``, engine/llm.py), does selection ask Gemini for help, in two bounded steps:

1. ``rewrite``: Gemini rewords the question once in the tables' own words, and the deterministic
   search runs again on the rewording. The SQL is still built by the search; the answer says which
   question it answered.
2. ``propose``: when the rewording finds nothing either, Gemini proposes one SQL query. It is text
   until engine/sql_import.py maps it into the typed AST, the validator accepts it and the renderer
   reproduces it; selection then runs and grounds it like a search candidate. The answer says the
   query is Gemini's.

Gemini sees the question and the schema text of engine/sql_prompt.py, never the rows. Replies are
cached per prompt, so a repeated request gets the same reading. Nothing here writes a number: the
database computes every result.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict

from engine import llm, request_timing
from engine.sql_ast import render_query, validate_query
from engine.sql_candidate import ScoredQuery
from engine.sql_import import Unsupported, import_sql, normalize_decoded_sql
from engine.sql_prompt import (
    PROPOSE_SCHEMA, PROPOSE_SYSTEM, REWRITE_SCHEMA, REWRITE_SYSTEM, propose_prompt, rewrite_prompt,
)

MAX_QUESTION_CHARS = 500
MAX_SQL_CHARS = 4000
PROPOSAL_EVIDENCE = "gemini:proposal"
# The note when Gemini could not be reached. Selection then stops: a second call would wait out the
# same outage again.
UNAVAILABLE = "Gemini unavailable"


class SQLFallback:
    """Gemini's two bounded steps. ``available`` is false unless the operator enabled and configured
    Gemini, in which case selection never calls this class."""

    _CACHE_CAP = 256

    def __init__(self, client=llm):
        self._llm = client
        self._replies: OrderedDict[tuple[str, str], str | None] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._llm.available()

    @property
    def model(self) -> str:
        return self._llm.model_id()

    def rewrite(self, question: str, graph) -> tuple[str | None, str]:
        """(the rewording, "") or (None, why there is none). A rewording equal to the question,
        ignoring case and spacing, is none: the search has already read those words."""
        reply, note = self._ask("rewrite", REWRITE_SYSTEM, rewrite_prompt(graph, question),
                                REWRITE_SCHEMA, "question", MAX_QUESTION_CHARS)
        if reply is None:
            return None, note
        if " ".join(reply.lower().split()) == " ".join(question.lower().split()):
            return None, "rewording unchanged"
        return reply, ""

    def propose(self, question: str, graph) -> tuple[str | None, ScoredQuery | None, str]:
        """(Gemini's SQL text, the typed candidate it imports to, "") or a None candidate and why.

        The candidate's SQL is the engine's own rendering of the imported AST, never Gemini's text."""
        text, note = self._ask("propose", PROPOSE_SYSTEM, propose_prompt(graph, question),
                               PROPOSE_SCHEMA, "sql", MAX_SQL_CHARS)
        if text is None:
            return None, None, note
        try:
            query = import_sql(normalize_decoded_sql(text), graph)
            validate_query(query)
            rendered = render_query(query)
        except (Unsupported, TypeError, ValueError) as exc:
            request_timing.count("fallback_import_rejections")
            return text, None, f"proposal not importable ({type(exc).__name__})"
        return text, ScoredQuery(query, rendered, 0.0, (PROPOSAL_EVIDENCE,)), ""

    def _ask(self, step, system, prompt, schema, field, limit) -> tuple[str | None, str]:
        key = (step, prompt)
        with self._lock:
            if key in self._replies:
                self._replies.move_to_end(key)
                request_timing.count("fallback_cache_hits")
                return self._replies[key], "" if self._replies[key] is not None else "no usable reply"
        request_timing.count(f"fallback_{step}_calls")
        try:
            # The cap leaves room for Gemini's thinking tokens, which count toward it (engine/converse.py).
            raw = self._llm.generate_text(system=system, prompt=prompt, max_output_tokens=4096,
                                          json_schema=schema)
        except self._llm.LLMUnavailable:
            # Not cached: an outage is not this prompt's answer.
            return None, UNAVAILABLE
        try:
            value = json.loads(raw).get(field)
        except (ValueError, AttributeError):
            value = None
        value = value.strip() if isinstance(value, str) else None
        if not value or len(value) > limit:
            value = None
        with self._lock:
            self._replies[key] = value
            while len(self._replies) > self._CACHE_CAP:
                self._replies.popitem(last=False)
        return value, "" if value is not None else "no usable reply"
