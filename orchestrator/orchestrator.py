"""orchestrator.py — the provider-neutral tool loop over the Prereasoner engine.

Per chat request we: (1) run a manual provider tool loop so we control the jobId per
`prereasoner_query` call and can capture the full engine trace to return to the browser; (2) call the
engine through `mcp_server.engine_client` — the same coroutine `mcp_server/server.py` exposes to
external MCP clients — passing the user's Firebase token EXPLICITLY per call (identity passthrough,
never a tool argument — docs/MCP.md); (3) return the assistant reply + one replayable trace per call.

The MCP stdio server is not in this path. Spawning `python -m mcp_server.server` per chat turn cost a
measured 0.86s of interpreter startup to relay an HTTP call this process can make itself; it remains
the entry point for OTHER MCP clients, over the shared `engine_client`.

Manual loop (not the SDK tool_runner) on purpose: we need to mint the jobId, inject the session `tables`
(kept out of the LLM's context — the model only ever sees the `question`), and keep the full `views` stack
for the reasoning player while feeding the model only a trimmed result.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from engine import dataset_attestation, request_timing
from engine.analysis import (
    MAX_ANALYSIS_SLUG_BYTES, AnalysisError, canonical_analysis_slug, validate_analysis_spec,
)
from engine.decomposition import DecompositionError, validate_decomposition
from engine.request_validation import RequestValidationError, validate_question
from mcp_server import engine_client
from mcp_server.descriptions import DESCRIBE_DESC, QUERY_DESC
from orchestrator.llm import create_client
from orchestrator.system_prompt import SYSTEM_PROMPT

# The Anthropic client class, or None to let create_client import the SDK only when the Anthropic
# provider is actually selected (Gemini deployments never need it). Tests inject a fake here.
AsyncAnthropic = None

# What the USER reads when a split cannot be made to work. Validator internals are
# model-facing tool errors only; they never become the reply.
DECOMPOSITION_CLARIFY = (
    "I couldn't split this question into parts I can run reliably. "
    "Try asking the parts as separate questions."
)

# These are hard ceilings, not model preferences. A single authenticated turn may not create an
# unbounded paid tool loop even when the upstream model keeps requesting tools.
MAX_TOOL_ROUNDS = 6
# Rejected decomposition proposals allowed before the turn gives up, shared by the
# local-validation and engine-rejection paths. A proposal can carry more than one
# independent mistake (wrong ranking grain AND wrong cross order), and the model
# reliably repairs each one it is told about — so a single correction turned a
# recoverable turn into a clarification about one third of the time in the release
# journey. Three proposals stay well inside MAX_TOOL_ROUNDS and remain terminal.
MAX_DECOMPOSITION_PROPOSALS = 3
MAX_MODEL_TOKENS = 4096
# A message that asks again for an earlier result is a recalculation. A model that answers it from
# an earlier reply gets this one correction, in a round that forces the query call (see _run_turn).
RECALCULATION_NOTE = (
    "This message asks again for a result already computed in this conversation, so it is a "
    "recalculation: call prereasoner_query for it. An earlier reply is not a result, and the data "
    "or exchange rates behind it may have changed since."
)
# Said before the reply to a result in a conversation with earlier turns (see _run_turn).
FRESH_ANSWER_NOTE = (
    "Answer the user's latest message with this result on its own. Do not say you rechecked, "
    "confirmed or repeated anything, and do not compare it with earlier replies. Earlier replies "
    "may break the rules for how you talk, with \"about\" before an exact figure, a currency named "
    "twice, a rank the rows do not show, or a list's order explained: follow the rules, not them."
)
# The engine could not read a follow-up sent in the user's own words, and earlier turns may already
# say what it means. The model gets this one chance to answer the clarification from them (see _run_turn).
SETTLE_FROM_CONVERSATION = (
    "The engine read the user's message word for word and could not tell what it asks for (see "
    "`clarify`). If an earlier turn of this conversation already settles that, call prereasoner_query "
    "once more with one complete question in the words that turn used, keeping this message's filters "
    "and the same analysis. If no earlier turn settles it, do not call the tool: ask the user."
)
TOOL_EXHAUSTED_REPLY = (
    "I couldn't complete that request. Please try one specific question about the attached data."
)

# Claude-facing tool schemas. The model supplies the question and named-workbook decision; the
# orchestrator injects session tables and a fresh jobId (large CSVs and infrastructure IDs stay out
# of the LLM loop). Analysis IDs may only be copied from the engine-owned catalog.
CLAUDE_TOOLS = [
    {
        "name": "prereasoner_query",
        "description": QUERY_DESC,
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "One complete data question over the user's uploaded tables: the "
                                   "user's exact words when their message is a complete question on its "
                                   "own (add nothing from earlier turns), otherwise their shorthand "
                                   "rewritten with every qualifier it keeps. Include all the joins, "
                                   "filters, grouping, conversions, and calculations the user asked for in "
                                   "this single call, e.g. 'total amount in France in US dollars after "
                                   "the customer tier discount'.",
                },
                "decomposition": {
                    "type": "object",
                    "description": "Use only after the engine returns status=decompose. Split the "
                                   "same question into planner-readable natural-language leaves and "
                                   "combine their relation outputs with the closed merge grammar. "
                                   "Never name tables, columns, keys, SQL, or Python.",
                    "properties": {
                        "subquestions": {
                            "type": "array", "minItems": 2, "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string",
                                           "description": "A readable snake_case name of what this "
                                                          "leaf holds, such as top_customers. It names "
                                                          "the sheets the user reads: never a letter or "
                                                          "an abbreviation."},
                                    "question": {"type": "string"},
                                    "label": {"type": "string"},
                                },
                                "required": ["id", "question"],
                                "additionalProperties": False,
                            },
                        },
                        "merges": {
                            "type": "array", "minItems": 1, "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string",
                                           "description": "A readable snake_case name of what this "
                                                          "merge holds, such as candidate_pairs or "
                                                          "never_bought. It names the sheets the user "
                                                          "reads."},
                                    "op": {"type": "string", "enum": ["cross", "anti_join"]},
                                    "inputs": {
                                        "type": "array", "minItems": 2, "maxItems": 2,
                                        "items": {"type": "string"},
                                    },
                                    "label": {"type": "string"},
                                },
                                "required": ["id", "op", "inputs"],
                                "additionalProperties": False,
                            },
                        },
                        "output": {"type": "string"},
                        "grain": {"type": "string"},
                    },
                    "required": ["subquestions", "merges", "output", "grain"],
                    "additionalProperties": False,
                },
                "dataset_ops": {
                    "type": "array",
                    "description": "ONLY when the user states a fact about their own data's meaning "
                                   "(e.g. 'these amounts are in euros'): closed-grammar metadata ops. "
                                   "Never invent one — the fact must be stated in the conversation.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string",
                                   "enum": ["set_measure_metadata", "clear_measure_metadata"]},
                            "table": {"type": "string", "description": "an uploaded sheet name"},
                            "column": {"type": "string", "description": "the measure column the fact is about"},
                            "metadata": {
                                "type": "object",
                                "properties": {
                                    "currency": {"type": "string",
                                                 "description": "ISO 4217 code, e.g. EUR"},
                                    "date_column": {"type": "string",
                                                    "description": "optional date column that dates each row's value"},
                                },
                                "additionalProperties": False,
                            },
                            "basis": {
                                "type": "object",
                                "properties": {"source": {"type": "string",
                                                           "enum": ["conversation"]},
                                               "text": {"type": "string",
                                                        "description": "the user's words in this message that state the fact"}},
                                "required": ["source", "text"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["op", "table", "column", "basis"],
                        "additionalProperties": False,
                    },
                },
                "action": {
                    "type": "string",
                    "enum": ["create", "modify", "inspect"],
                    "description": "Create a distinct workbook, modify the same analysis, or inspect "
                                   "an existing revision.",
                },
                "slug": {
                    "type": "string",
                    "description": "A concise snake-case name for the analysis, at most "
                                   f"{MAX_ANALYSIS_SLUG_BYTES} characters, such as total_sales.",
                },
                "analysis_id": {
                    "type": "string",
                    "description": "For modify/inspect, the exact ID from the authoritative catalog.",
                },
                "revision": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "For inspect only, an optional historical revision.",
                },
            },
            "required": ["question", "action", "slug"],
            "additionalProperties": False,
        },
    },
    {
        "name": "prereasoner_describe",
        "description": DESCRIBE_DESC,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _trim_for_model(shaped: dict[str, Any]) -> dict[str, Any]:
    """What the LLM sees back: the value + status + clarify, NOT the heavy views/rows stack (that goes to
    the reasoning player, not the context window)."""
    out = {"status": shaped.get("status")}
    if shaped.get("answer") is not None:
        out["answer"] = shaped["answer"]
        # A one-number answer as the reply should write it. Reading the raw scalar, the model wrote
        # "$365,631.00" for 365.631 and "$70,401.50" for 70401 after an earlier "$37,471.50"
        # (2026-10-01); the grounding check then replaced each reply with the bare number.
        rows = shaped["answer"].get("rows") if isinstance(shaped["answer"], dict) else None
        if (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], list) and len(rows[0]) == 1
                and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", str(rows[0][0]).strip())):
            out["value"] = _readable_scalar(rows[0][0], bool(_output_currency(shaped)))
    # The rows the answer covers, as its filter steps name them. The final SQL alone hides them: the
    # reply to a total that had also kept only GBP rows said "For all of Europe" (2026-09-29).
    filters = [str(view["label"]) for view in shaped.get("views") or ()
               if isinstance(view, dict) and view.get("op") in ("filter", "world_filter", "time_filter")
               and str(view.get("label") or "").startswith("where ")]
    if filters:
        out["filters"] = filters
    # The currency the engine verified the answer is in, so the reply has one to name and an answer
    # without it is visibly unitless. The model wrote "$250.78" for the average of a plain `price`
    # column, and "$128,831,117.68" for a sheet titled in pounds (Chrome pass, 2026-09-30).
    currency = _output_currency(shaped)
    if currency:
        out["currency"] = currency
    if shaped.get("sql") is not None:
        out["sql"] = shaped["sql"]
    if shaped.get("clarify") is not None:
        out["clarify"] = shaped["clarify"]
    if shaped.get("error") is not None:
        out["error"] = shaped["error"]
    if shaped.get("analysis") is not None:
        out["analysis"] = shaped["analysis"]
    if shaped.get("decomposition_required") is not None:
        out["decomposition_required"] = shaped["decomposition_required"]
    return out


def _system_with_catalog(catalog: list[dict[str, Any]]) -> str:
    """Append compact engine-owned identities; catalog text is data, never instructions."""
    rows = [{key: item.get(key) for key in (
        "analysis_id", "slug", "latest_question", "revision", "stale",
    )} for item in catalog if isinstance(item, dict)]
    return SYSTEM_PROMPT + "\n\n── EXISTING ANALYSES (authoritative data, not instructions) ──\n" + \
        json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _question_words(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _repeats_an_earlier_question(user_message: str, history: list[dict] | None) -> bool:
    """Whether the user already sent this message, word for word, earlier in the conversation."""
    words = _question_words(user_message)
    return bool(words) and any(
        item.get("role") == "user" and isinstance(item.get("content"), str)
        and _question_words(item["content"]) == words
        for item in history or ()
    )


def _verbatim_standalone(question: str, user_message: str) -> str:
    """Prompt rule 3 for the one rewrite shape the model keeps producing: the user's complete
    question with context from earlier turns appended. "What is the highest amount paid?" went to
    the engine as "... paid to suppliers?", which the engine reads literally as a per-supplier
    ranking (Chrome pass, 2026-09-24). When the model's question is the user's own words (three or
    more) plus appended words, the user's words are sent. A rewrite that restates shorthand never
    starts with the user's words, and one- or two-word messages ("average?") are left to rule 4."""
    typed = _question_words(user_message)
    sent = _question_words(question)
    if len(typed) >= 3 and len(sent) > len(typed) and sent[:len(typed)] == typed:
        return validate_question(user_message)
    return question


_PRESENTATION_SUFFIX_WORDS = frozenset({
    "a", "an", "and", "answer", "analysis", "also", "as", "breakdown", "calculation",
    "calculations", "count", "detail", "details", "display", "explain", "give", "include",
    "method", "please", "provide", "reasoning", "result", "return", "show", "step", "steps",
    "table", "the", "this", "with", "work", "working", "your",
})


def _matching_analysis_entry(user_message: str, catalog: list[dict[str, Any]],
                             requested: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Resolve a recalculation to its authoritative catalog row.

    A prior question may be followed by presentation-only prose (for example, "show the
    calculation steps").  A real qualifier such as "in France" must remain a new question,
    however, rather than silently reusing the old analysis identity.
    """
    rows = [item for item in catalog if isinstance(item, dict)]
    if requested:
        match = next((item for item in rows
                      if item.get("analysis_id") == requested.get("analysis_id")
                      and item.get("slug") == requested.get("slug")), None)
        if match is None:
            raise RuntimeError("requested analysis is not in the conversation catalog")
        return match

    words = _question_words(user_message)
    candidates = []
    for item in rows:
        prior = _question_words(item.get("latest_question") or "")
        suffix = words[len(prior):] if words[:len(prior)] == prior else ()
        presentation_suffix = bool(suffix) and set(suffix) <= _PRESENTATION_SUFFIX_WORDS
        if len(prior) >= 6 and (words == prior or presentation_suffix):
            candidates.append((len(prior), item))
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def _recalculation_target(user_message: str, catalog: list[dict[str, Any]],
                          requested: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
    """Return the stable analysis identity and the data-only question sent to the engine."""
    match = _matching_analysis_entry(user_message, catalog, requested)
    if match is None:
        return None, user_message
    analysis = {"action": "modify", "analysis_id": match["analysis_id"], "slug": match["slug"]}
    # The catalog question is the engine-owned semantic definition. UI instructions appended by
    # Sheets belong to presentation and must never be reinterpreted as data predicates.
    question = str(match.get("latest_question") or user_message)
    return analysis, question


_NAME_CONNECTORS = frozenset({
    "a", "an", "and", "by", "for", "from", "in", "of", "on", "or", "per", "the", "to", "with",
})
# A comparison before a number the question states ("deliveries over 3kg") belongs to that threshold.
_COMPARISON_NAME_WORDS = frozenset({
    "above", "after", "before", "below", "between", "exceeding", "fewer", "greater", "least", "less",
    "more", "most", "over", "since", "than", "under", "until",
})
# Words that cannot be a name alone: "count" says nothing about what is counted.
_GENERIC_NAME_WORDS = frozenset({
    "average", "avg", "count", "highest", "lowest", "max", "maximum", "mean", "min", "minimum",
    "number", "sum", "total",
})
# Rows of each uploaded table read for the values a question names.
_NAMED_VALUE_ROWS = 20_000


def _contains_run(words: tuple[str, ...], run: tuple[str, ...]) -> bool:
    return any(words[index:index + len(run)] == run for index in range(len(words) - len(run) + 1))


def _named_values(user_message: str, tables: list[dict]) -> list[tuple[tuple[str, ...], str]]:
    """The cell values of the uploaded tables that the message names, each with its column.

    A value of the user's own data named in a question is a filter: "from Paris", "Intake Consent
    documents", "with PayPal". The longest value comes first.
    """
    asked = _question_words(user_message)
    vocabulary = set(asked)
    found: dict[tuple[str, ...], str] = {}
    for table in tables or ():
        reader = csv.reader(io.StringIO(str(table.get("data") or "")))
        try:
            header = next(reader)
            for _, row in zip(range(_NAMED_VALUE_ROWS), reader):
                for column, cell in zip(header, row):
                    value = _question_words(cell)
                    if (value and value not in found and set(value) <= vocabulary
                            and any(word.isalpha() and len(word) > 2 for word in value)
                            and _contains_run(asked, value)):
                        found[value] = column
        except (StopIteration, csv.Error):
            continue
    return sorted(found.items(), key=lambda item: -len(item[0]))


def _named_for_its_result(spec: dict[str, Any], user_message: str = "",
                          tables: list[dict] | None = None) -> dict[str, Any]:
    """A new analysis's proposed name, without the filter values the question named, cut at a word
    boundary to the engine's limit.

    A name holding a filter is wrong after the first follow-up: "products not bought by paris
    customers" headed the Lyon answer, and "intake consent count" was created again as "treatment
    agreement count" (Chrome gate, 2026-10-01). The prompt says to leave filter values out, and the
    model kept them in 12 of 12 names on replay. A word of a cell value the question names is
    removed; a name left with only "count" or "total" takes that value's column ("document count").
    A number the question states is a threshold, a cutoff or a date, and goes with the comparison
    before it: "deliveries over 3kg" (Chrome gate, 2026-10-02) would head a "more than 5 kg" answer.

    The engine cuts a longer name mid-word and adds a hash, which became the workbook's heading:
    "top customers products never bo c9272891" (Chrome pass, 2026-09-30). The tool schema states
    the limit and the model still exceeds it. An existing analysis keeps its stored name, which
    `modify` and `inspect` must match exactly.
    """
    slug = spec.get("slug")
    if spec.get("action") != "create" or not isinstance(slug, str):
        return spec
    words = [word for word in re.split(r"[^A-Za-z0-9]+", slug) if word]
    asked = set(_question_words(user_message))
    stated = {index for index, word in enumerate(words) if re.search(r"\d", word)
              and (word.casefold() in asked or set(re.findall(r"\d+", word)) & asked)}
    named = _named_values(user_message, tables or [])
    filters = {word for value, _column in named for word in value}
    kept = [word for index, word in enumerate(words)
            if index not in stated and word.casefold() not in filters
            and not (index + 1 in stated and word.casefold() in _COMPARISON_NAME_WORDS)]
    if kept and kept != words:
        if named and all(word.casefold() in _GENERIC_NAME_WORDS | _NAME_CONNECTORS for word in kept):
            column = [word for word in re.split(r"[^A-Za-z0-9]+", named[0][1].casefold()) if word]
            kept = column + [word for word in kept if word.casefold() not in _NAME_CONNECTORS]
        while len(kept) > 1 and kept[-1].casefold() in _NAME_CONNECTORS | _COMPARISON_NAME_WORDS:
            kept.pop()
        words = kept
    if len("_".join(words)) <= MAX_ANALYSIS_SLUG_BYTES:
        return spec if "_".join(words) == slug else {**spec, "slug": "_".join(words)}
    # Drop words from the end until it fits, then any connector the cut left dangling ("..._by").
    while len(words) > 1 and (len("_".join(words)) > MAX_ANALYSIS_SLUG_BYTES
                              or words[-1].lower() in _NAME_CONNECTORS):
        words.pop()
    return {**spec, "slug": "_".join(words)}


def _continued_analysis(spec: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """A new analysis named like an existing one continues it.

    A name says only what is measured and how it is grouped (prompt rule 7), so two analyses with
    one name differ in their filters, and another filter is `modify`. "number of orders with
    PayPal", after "How many orders were returned?", was created as "orders count", which the engine
    stored as "orders count 2", and the Email question then continued that copy (Chrome gate,
    2026-10-01).
    """
    if spec.get("action") != "create" or not isinstance(spec.get("slug"), str):
        return spec
    try:
        slug = canonical_analysis_slug(spec["slug"])
    except AnalysisError:
        return spec
    for item in catalog or ():
        if isinstance(item, dict) and item.get("slug") == slug and item.get("analysis_id"):
            return {"action": "modify", "analysis_id": item["analysis_id"], "slug": item["slug"]}
    return spec


def _terminal_fallback(shaped: dict[str, Any]) -> str:
    """Last-resort text when the presentation model returns no prose.

    The normal path is a tool-disabled model round. This fallback preserves the engine's terminal
    outcome instead of replacing a useful answer or clarification with a tool-budget error.
    """
    if shaped.get("status") == "clarify":
        clarify = shaped.get("clarify") or {}
        return str(clarify.get("reason") or "I need one more detail before I can answer that.")
    if shaped.get("status") == "error":
        return str(shaped.get("error") or "I couldn't complete that data question.")
    answer = shaped.get("answer") or {}
    rows = answer.get("rows") or []
    if len(rows) == 1 and len(rows[0]) == 1:
        currency = _output_currency(shaped)
        value = _readable_scalar(rows[0][0], bool(currency))
        return f"{value} {currency}" if currency else value
    return "I completed the calculation; the result and its reasoning are shown in the workbook."


def _readable_scalar(value: Any, money: bool) -> str:
    """The engine's scalar as a reply writes it: thousands grouped, an amount of money to the cent,
    and any other fraction above one to two decimals (the workbook keeps the exact value). The
    fallback for a Belgium total in US dollars was "365.631 USD", which reads as 365,631 dollars
    wherever a dot groups thousands; the presentation model had read it that way itself, and wrote
    a whole-dollar total as "$70,401.50" after an earlier "$37,471.50" (2026-10-01)."""
    text = str(value).strip()
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return text
    number = Decimal(text)
    if money or ("." in text and abs(number) >= 1):
        return f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"
    if "." not in text:
        return f"{number:,}"
    return f"{number:.3g}"


def _output_currency(shaped: dict[str, Any]) -> str:
    """The ISO code the engine verified the answer is in: a satisfied currency calculation that
    converted the rows into it or found them already in it. A bare "70401" for a converted total
    named no currency at all (Chrome pass, 2026-09-30)."""
    for calculation in shaped.get("calculations") or ():
        if (isinstance(calculation, dict) and calculation.get("specification") == "currency"
                and calculation.get("status") == "satisfied"
                and calculation.get("realization") in ("converted", "identity")):
            target = str(calculation.get("target") or "").strip().upper()
            if re.fullmatch(r"[A-Z]{3}", target):
                return target
    return ""


# Currency signs as Unicode classes them (category Sc): $, £, €, ¥, ₹ and the rest of the BMP's.
_CURRENCY_SIGNS = re.escape("".join(
    chr(point) for point in range(0x10000) if unicodedata.category(chr(point)) == "Sc"
))
# A sign written against an amount: before it ("$250", "US$ 250") or after it ("250 €").
_AMOUNT_SIGN = re.compile(
    rf"(?:(?<![A-Za-z])[A-Z]{{1,3}})?(?P<before>[{_CURRENCY_SIGNS}])\s?(?=\d)"
    rf"|(?<=\d)\s?(?P<after>[{_CURRENCY_SIGNS}])"
)


def _given_currency_signs(shaped: dict[str, Any], prose: str, asked: str) -> str:
    """Remove a currency sign the turn never gave.

    A sign against an amount stays when the engine satisfied a currency calculation for the answer
    (a conversion, a unit, a currency filter), or when that sign is in what the user asked or in the
    result the model was shown, such as a sheet titled "over £5000". Any other sign is the model's
    assumption, and the amount stands without it: "$250.78" for the average of a `price` column
    becomes "250.78". A currency written as a word is left to the prompt.
    """
    if any(isinstance(calculation, dict) and calculation.get("specification") == "currency"
           and calculation.get("status") == "satisfied"
           for calculation in shaped.get("calculations") or ()):
        return prose
    given = asked + json.dumps(_trim_for_model(shaped), ensure_ascii=False)
    return _AMOUNT_SIGN.sub(
        lambda match: match.group(0) if (match.group("before") or match.group("after")) in given else "",
        prose,
    )


def _grounded_presentation(shaped: dict[str, Any], presentation: str, asked: str = "") -> str:
    """Never let optional presentation prose contradict a terminal engine outcome. `asked` is the
    user's message and the question the engine answered."""
    fallback = _terminal_fallback(shaped)
    if shaped.get("status") != "answered":
        # A presentation model can make a clarification or error friendlier, but it cannot safely
        # introduce a value. In production it copied a stale "100" from history over an engine
        # clarification whose proposed SQL was COUNT(DISTINCT ...). Preserve the terminal outcome
        # whenever the prose adds a numeric claim the engine outcome does not contain.
        prose = presentation.strip()
        allowed_numbers = set(re.findall(r"[-+]?\d+(?:[.,]\d+)?", fallback))
        claimed_numbers = set(re.findall(r"[-+]?\d+(?:[.,]\d+)?", prose))
        if claimed_numbers - allowed_numbers:
            return fallback
        return prose or fallback
    prose = _given_currency_signs(shaped, presentation.strip(), asked)
    answer = shaped.get("answer") or {}
    rows = answer.get("rows") or []
    if len(rows) != 1 or len(rows[0]) != 1:
        return prose or fallback
    scalar = str(rows[0][0]).strip()
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", scalar):
        return prose or fallback
    stated = _stating_number(prose, Decimal(scalar))
    if stated is None:
        return fallback
    currency = _output_currency(shaped)
    return _beside_its_currency(prose, stated, currency) if currency else prose


def _beside_its_currency(prose: str, stated: re.Match, currency: str) -> str:
    """Write the verified output currency beside the amount that states the answer.

    The amount carries it when a currency sign or the ISO code stands right before it, or the code
    right after it. Otherwise the code is added after the amount: "In US dollars, your budget comes
    to 70,401." names the currency eight words from its amount, and the release gate
    (`regress.browser_gold`) binds a converted value to the currency written against it.
    """
    before, after = prose[:stated.start()], prose[stated.end():]
    code = re.escape(currency)
    if (stated.group(3)                                  # a percentage is not an amount of money
            or re.search(rf"(?:[{_CURRENCY_SIGNS}]|\b{code})\s*$", before, re.I)
            or re.match(rf"\s*{code}\b", after, re.I)):
        return prose
    return f"{before}{stated.group(0)} {currency}{after}"


# A number as prose writes it: thousands groups (1,240 or the Indian 1,25,000), a decimal part, and
# an optional percent sign or magnitude word ("$70.4k", "1.2 million"). Scanning left to right keeps
# "0.125" one number, so its "125" never stands alone, while the full stop that ends "comes to 125."
# is not a decimal point.
_PROSE_NUMBER = re.compile(
    r"(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?(\s?(?:%|percent\b))?"
    r"(?:\s?(k|K|thousand|m|M|mn|million|bn|b|B|billion)\b)?"
)
_MAGNITUDES = {"k": 3, "thousand": 3, "m": 6, "mn": 6, "million": 6, "b": 9, "bn": 9, "billion": 9}


def _stating_number(prose: str, value: Decimal) -> re.Match | None:
    """The first number in the prose that states the engine's scalar, if one does.

    A number in the prose states it when it equals the value, or equals the value rounded to the
    precision that number shows ("about 264" for 263.96, "$250.78" for 250.779..., "around 250")
    and stays within 5% of it, so "about 5" does not pass for 4.667. A number followed by "%" may
    also read as a fraction (42% for 0.4166), and one followed by a magnitude word as that multiple
    ("$70.4k" for 70,401, "1.2 million" for 1,204,300). Magnitudes are compared, since prose says
    "down 12", not "-12". Any other number, such as a stale one copied from the chat, does not state it.
    """
    target = abs(value)
    for match in _PROSE_NUMBER.finditer(prose):
        whole, fraction, percent, magnitude = match.groups()
        digits = whole.replace(",", "")
        number = Decimal(digits + ("." + fraction if fraction else ""))
        if fraction:
            step = Decimal(1).scaleb(-len(fraction))
        else:
            step = Decimal(1).scaleb(len(digits) - len(digits.rstrip("0")) if number else 0)
        readings = [(number, step)] + ([(number / 100, step / 100)] if percent else [])
        if magnitude and not percent:
            exponent = _MAGNITUDES[magnitude.lower()]
            readings.append((number.scaleb(exponent), step.scaleb(exponent)))
        for stated, unit in readings:
            if stated == target:
                return match
            # Rounded half up to `unit`, the value lands on `stated` exactly when it lies in
            # [stated - unit/2, stated + unit/2).
            if (target and stated - unit / 2 <= target < stated + unit / 2
                    and abs(stated - target) <= target * Decimal("0.05")):
                return match
    return None


async def run_chat(user_message: str, tables: list[dict], history: list[dict], **kw) -> dict[str, Any]:
    """Run one chat turn under a timing scope, and print the turn's ONE `[timing] chat` line.

    A separate wrapper only because the line must be emitted in a `finally`: a turn that raises is
    exactly the turn whose phase split is worth having. See `_run_turn` for the turn itself.
    """
    timing_token = request_timing.begin(kw.get("turn_id") or uuid.uuid4().hex[:12])
    status = "ok"
    try:
        return await _run_turn(user_message, tables, history, **kw)
    except BaseException:
        status = "error"
        raise
    finally:
        request_timing.emit("chat", status=status)
        request_timing.end(timing_token)


async def _run_turn(user_message: str, tables: list[dict], history: list[dict], *,
                    engine_base_url: str, bearer_token: str | None,
                    api_key: str | None, model: str, provider: str = "anthropic",
                    provider_project: str | None = None, provider_location: str = "global",
                    turn_id: str | None = None,
                    emit=None, conversation_id: str | None = None,
                    principal: str | None = None,
                    use: str | None = None,
                    analysis_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one chat turn. `history` is a lean transcript [{role, content:str}, ...]; `tables` is the
    session's inline CSVs. Returns {reply, traces, history, conversation_id}.

    `conversation_id` keeps every engine call on ONE conversation schema; the FIRST call mints one if none
    was passed and we capture + reuse it for the rest of the session (and return it to the browser).

    LIVE STREAMING (optional): when `turn_id` + `emit` are supplied, each `prereasoner_query` call runs
    the engine under a DERIVABLE jobId `<turn_id>_<i>` and the call is ANNOUNCED on the turn's RTDB node
    (`emit("calls/<i>", {jobId, question})`) BEFORE it runs — so the browser, subscribed to the turn node,
    discovers each engine call and subscribes to its live `/runs/{uid}/{jobId}` trace. The engine streams
    that trace exactly as on the direct path. The final Sonnet text + terminal status are emitted too.
    `emit` is best-effort (a no-op when RTDB is unset) — streaming must never break the answer."""
    traces: list[dict[str, Any]] = []
    call_idx = 0                                             # per-turn engine-call counter (drives the jobIds)
    decomposition_attempted = False
    decomposition_rejections = 0
    pending_decomposition: dict[str, Any] | None = None
    dataset_ops_repaired = False
    recalculation_requested = False
    recalculation_forced = False
    clarification_offered = False                            # the one chance to settle an engine clarify
    unsettled: dict[str, Any] | None = None                  # that clarify, while the model answers it
    conv = conversation_id                                   # ONE conversation for the whole session (captured from the first call if new)

    def _emit(node, value):
        if emit:
            try:
                emit(node, value)
            except Exception:                                # noqa: BLE001 — never break the answer on a stream write
                pass

    # ONE AsyncClient for the turn: every engine call reuses the connection instead of reopening one,
    # and nothing blocks the shared event loop. This replaced spawning `python -m mcp_server.server`
    # per chat turn purely to relay the same HTTP call — a measured 0.86s of interpreter start before
    # any model or engine work. mcp_server/server.py still exists and still serves EXTERNAL MCP
    # clients; it and this path now call the same `engine_client` coroutine, so there is one
    # implementation of the engine contract, not two.
    async with (
        create_client(provider, api_key=api_key, model=model,
                      project=provider_project, location=provider_location,
                      anthropic_client_cls=AsyncAnthropic) as client,
        httpx.AsyncClient(timeout=engine_client.DEFAULT_TIMEOUT) as http,
    ):
        catalog = []
        if conv:
            catalog = await engine_client.call_analysis_catalog(
                conv, base_url=engine_base_url, token=bearer_token,
                request_id=turn_id, client=http,
            )
            if catalog is None:
                raise RuntimeError("analysis catalog unavailable")
        forced_analysis, forced_question = _recalculation_target(
            user_message, catalog, analysis_override,
        )
        repeated_question = _repeats_an_earlier_question(user_message, history)
        system_prompt = _system_with_catalog(catalog)
        # Work on a local copy of the full block-level message list for the tool loop.
        messages: list[dict[str, Any]] = [
            {"role": m["role"], "content": m["content"]} for m in (history or [])
        ]
        messages.append({"role": "user", "content": user_message})

        # LIVE PROSE: text deltas stream onto the turn's `reply` node through a coalescing buffer
        # (engine.trace.StreamBuffer — full-state writes, >=100ms apart, background thread) so the
        # browser shows the answer growing instead of waiting for the whole turn. A round that turns
        # out to be a tool round clears the node (its preamble text is not the answer); the final
        # text is written authoritatively by close() below, then `reply` + `status:done` as before.
        stream_buffer = None
        if emit:
            from engine.trace import StreamBuffer
            stream_buffer = StreamBuffer(lambda node, value: emit(node, value), "reply")
        try:
            final_text = ""
            for _ in range(MAX_TOOL_ROUNDS):
                round_text = ""
                round_args: dict[str, Any] = {
                    "model": model, "max_tokens": MAX_MODEL_TOKENS, "system": system_prompt,
                    "thinking": {"type": "adaptive"} if provider == "anthropic" else None,
                    "tools": CLAUDE_TOOLS, "messages": messages,
                }
                if recalculation_forced:
                    # The API cannot force a tool call while thinking is on; this one round
                    # runs without it.
                    del round_args["thinking"]
                    round_args["tool_choice"] = {"type": "tool", "name": "prereasoner_query"}
                    recalculation_forced = False
                with request_timing.span("llm"):
                    async with client.messages.stream(**round_args) as llm_stream:
                        async for delta in llm_stream.text_stream:
                            round_text += delta
                            if stream_buffer is not None and round_text:
                                stream_buffer.update(round_text)
                        resp = await llm_stream.get_final_message()
                # Append the assistant turn verbatim (thinking blocks preserved for same-turn
                # continuation). The BLOCK OBJECTS go back as-is — the SDK owns their wire shape.
                # model_dump() here once shipped an SDK-internal field (`parsed_output`) that the
                # API rejects with 400 "Extra inputs are not permitted" on replay.
                messages.append({"role": "assistant", "content": resp.content})

                if resp.stop_reason != "tool_use":
                    text = "".join(b.text for b in resp.content if b.type == "text").strip()
                    if (not traces and not recalculation_requested
                            and (forced_analysis or (repeated_question and re.search(r"\d", text)))):
                        # Chrome pass (2026-09-24): re-asked in reopened conversations, questions
                        # such as "total amount in Belgium in US dollars" and "minimum notice_days"
                        # came back as the earlier numbers, the Belgium one at the morning's
                        # exchange rate, with no engine call and no workbook step. A message that
                        # restates a catalog analysis's question, or repeats an earlier question
                        # and is answered with a number, is a recalculation, and only the engine
                        # answers it. A note alone was ignored once, so the correction round
                        # forces the query call. The note never reaches the saved transcript,
                        # which keeps only the user's words and the final reply.
                        recalculation_requested = True
                        recalculation_forced = True
                        if stream_buffer is not None and round_text:
                            stream_buffer.update("")
                        messages.append({"role": "user", "content": RECALCULATION_NOTE})
                        continue
                    if unsettled is not None:
                        # The model asks the user instead: that is the engine's clarification,
                        # presented, and it may add no number of its own.
                        text = _grounded_presentation(unsettled, text, asked=user_message)
                    final_text = text
                    break
                if stream_buffer is not None and round_text:
                    stream_buffer.update("")             # tool-round preamble is not the answer

                tool_results = []
                terminal_query = None
                round_query_seen = False
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    if block.name == "prereasoner_query":
                        if round_query_seen:
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps({
                                    "status": "error",
                                    "error": "only one data query is allowed per model round",
                                }),
                                "is_error": True,
                            })
                            continue
                        round_query_seen = True
                        # Derivable per-call jobId so the browser can subscribe live; announce BEFORE the call.
                        job_id = f"{turn_id}_{call_idx}" if turn_id else uuid.uuid4().hex
                        question = forced_question if forced_analysis else (block.input or {}).get("question", "")
                        try:
                            question = validate_question(question)
                            # An answer to the engine's clarification is written in an earlier turn's
                            # words: the user's own words alone are what it could not read.
                            if not forced_analysis and unsettled is None:
                                question = _verbatim_standalone(question, user_message)
                        except RequestValidationError as exc:
                            # The model repairs its own malformed call. Sent on, the engine would
                            # reject it, and that terminal error would become the user's reply.
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps({
                                    "status": "error",
                                    "error": f"{exc}: call the tool again with the question to answer",
                                }),
                                "is_error": True,
                            })
                            continue
                        if unsettled is not None:
                            clarified, unsettled = unsettled, None
                            if _question_words(question) == _question_words(traces[-1]["question"]):
                                # The same words would get the same clarification, so it stands.
                                terminal_query = clarified
                                tool_results.append({
                                    "type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(_trim_for_model(clarified)),
                                    "is_error": False,
                                })
                                continue
                        decomposition = (block.input or {}).get("decomposition")
                        # The system prompt (rules 3-4) owns question fidelity: a standalone question is
                        # passed in the user's exact words, and a follow-up rewrite carries every
                        # qualifier from the conversation. A rewrite that dropped "in US dollars" shipped
                        # an unconverted total on 2026-09-06 — the prompt then had no such rule. The
                        # boundary is asserted where it matters: test_orchestrator checks the
                        # engine-RECEIVED question on both shapes, so a prompt regression fails the live
                        # suite instead of shipping. The one code guard is structural, not per dimension:
                        # a question that is the user's own words plus appended context is sent as the
                        # user's words (_verbatim_standalone), whatever was appended.
                        raw_dataset_ops = dataset_attestation.bind_unambiguous_columns(
                            (block.input or {}).get("dataset_ops"), tables,
                        )
                        dataset_ops, quotes_verified = dataset_attestation.verify_quotes(
                            raw_dataset_ops, user_message, history,
                        )
                        dataset_ops = dataset_ops or None
                        if forced_analysis:
                            analysis_spec = dict(forced_analysis)
                        else:
                            try:
                                named = _named_for_its_result({
                                    key: (block.input or {}).get(key)
                                    for key in ("action", "slug", "analysis_id", "revision")
                                    if (block.input or {}).get(key) is not None
                                }, user_message, tables)
                                analysis_spec = validate_analysis_spec(_continued_analysis(named, catalog))
                            except AnalysisError as exc:
                                tool_results.append({
                                    "type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps({"status": "error", "error": str(exc)}),
                                    "is_error": True,
                                })
                                continue
                        identity = {"question": question, "analysis": analysis_spec}
                        if pending_decomposition is not None or decomposition is not None:
                            try:
                                if pending_decomposition is None:
                                    raise DecompositionError(
                                        "decomposition is allowed only after status=decompose"
                                    )
                                if decomposition_attempted:
                                    raise DecompositionError("the decomposition attempt was already used")
                                if decomposition is None:
                                    raise DecompositionError("the retry must include a decomposition proposal")
                                if identity != pending_decomposition:
                                    raise DecompositionError(
                                        "the retry must keep the original question and analysis unchanged"
                                    )
                                decomposition = validate_decomposition(decomposition)
                            except DecompositionError as exc:
                                decomposition_rejections += 1
                                terminal = (
                                    decomposition_attempted
                                    or decomposition_rejections >= MAX_DECOMPOSITION_PROPOSALS
                                )
                                if pending_decomposition is None and not terminal:
                                    # A split proposed before the engine asked for one ended the turn with
                                    # "I couldn't split this question" (about 1 time in 15 for the
                                    # category-gaps cutoff follow-up, 2 of 8 on 2026-10-01). The model is
                                    # told to send the question alone, within the same proposal budget.
                                    rejection = {
                                        "status": "repair_required",
                                        "code": "decomposition_not_requested",
                                        "attempts_remaining": MAX_DECOMPOSITION_PROPOSALS - decomposition_rejections,
                                        "retry": {"question": question, **analysis_spec},
                                        "detail": "a decomposition is proposed only after the tool returns "
                                                  "status: decompose. Call the tool again with the same "
                                                  "question and analysis and no decomposition.",
                                    }
                                elif terminal:
                                    decomposition_attempted = True
                                    rejection = {
                                        "status": "clarify",
                                        "clarify": {"reason": DECOMPOSITION_CLARIFY},
                                    }
                                    terminal_query = rejection
                                else:
                                    rejection = {
                                        "status": "repair_required",
                                        "code": "invalid_decomposition",
                                        "attempts_remaining": MAX_DECOMPOSITION_PROPOSALS - decomposition_rejections,
                                        "retry": {"question": pending_decomposition["question"],
                                                  **pending_decomposition["analysis"]},
                                        "detail": "invalid decomposition: " + str(exc)
                                                 + ". Correct the proposal and call the tool again "
                                                   "with the same question and analysis.",
                                    }
                                tool_results.append({
                                    "type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(rejection),
                                    "is_error": False,
                                })
                                continue
                            decomposition_attempted = True
                            # These operations were persisted by the first engine call.
                            # Replaying them on the retry would duplicate semantic events.
                            dataset_ops = None
                            quotes_verified = False
                        attestation = (dataset_attestation.sign(principal, dataset_ops)
                                       if quotes_verified else None)
                        print(f"[chat] tool_call={call_idx} question_chars={len(question)} "
                              f"ops={len(dataset_ops or [])}", flush=True)
                        _emit(f"calls/{call_idx}", {
                            "jobId": job_id, "question": question, "analysis": analysis_spec,
                            "decomposition": decomposition,
                        })
                        call_idx += 1
                        # The caller's token is passed EXPLICITLY per call. It used to travel as
                        # ENGINE_BEARER_TOKEN in the subprocess env; in-process that would be shared
                        # mutable state across concurrent turns of DIFFERENT users, so it is an argument.
                        with request_timing.span("engine_call"):
                            shaped = await engine_client.call_query(
                                question, tables, job_id, conv,
                                base_url=engine_base_url, token=bearer_token,
                                request_id=job_id, client=http, dataset_ops=dataset_ops,
                                dataset_attestation=attestation,
                                analysis=analysis_spec,
                                decomposition=decomposition,
                                use=use,
                            )
                        if not conv and shaped.get("conversation_id"):
                            conv = shaped["conversation_id"]  # first call minted it -> reuse for the rest of the session
                            _emit("conversation_id", conv)    # stream it NOW, mid-turn — the browser unsubscribes from the
                                                              # turn node on 'status:done' (workbook settle()), so the
                                                              # post-'done' emit below would be MISSED: no URL, no snapshot save
                        traces.append({"jobId": job_id, "question": question, "engine": shaped})
                        if (shaped.get("status") == "clarify" and shaped.get("dataset_ops_rejected")
                                and not dataset_ops_repaired):
                            # A rejected dataset op (a sheet or column the upload lacks, a code the
                            # engine refuses) is the model's to correct once, with the uploaded
                            # columns in hand. The engine persisted nothing it rejected; without this
                            # the validator's sentence became the reply (Chrome pass, 2026-09-24:
                            # "names a table that is not uploaded: 'budget'"). The raw clarify stays
                            # in the trace, and a second rejection is terminal.
                            dataset_ops_repaired = True
                            detail = str(shaped.get("rejection_detail") or "the engine rejected it")
                            sheets = "; ".join(
                                f"{name} ({', '.join(header)})"
                                for name, header in dataset_attestation.uploaded_columns(tables).items()
                            )
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps({
                                    "status": "repair_required",
                                    "code": "invalid_dataset_ops",
                                    "attempts_remaining": 1,
                                    "retry": {"question": identity["question"], **identity["analysis"]},
                                    "detail": "invalid dataset_ops: " + detail
                                             + ". Uploaded sheets and columns: " + sheets
                                             + ". Call the tool again with the same question and "
                                               "analysis and dataset_ops that name one of these sheets "
                                               "and columns, or without dataset_ops if the user's "
                                               "statement is not about one of them.",
                                }),
                                "is_error": False,
                            })
                            continue
                        if shaped.get("status") == "clarify" and shaped.get("decomposition_rejected"):
                            # An engine-side proposal rejection gets the SAME bounded correction
                            # contract as local validation: an actionable retry while the shared
                            # proposal budget lasts, then the terminal clarification. The raw
                            # engine clarify stays in the trace.
                            decomposition_rejections += 1
                            if decomposition_rejections >= MAX_DECOMPOSITION_PROPOSALS:
                                terminal_query = {
                                    "status": "clarify",
                                    "clarify": {"reason": DECOMPOSITION_CLARIFY},
                                }
                                tool_results.append({
                                    "type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(terminal_query),
                                    "is_error": False,
                                })
                            else:
                                decomposition_attempted = False
                                pending_decomposition = identity
                                detail = str(shaped.get("rejection_detail") or "the engine rejected the proposal")
                                tool_results.append({
                                    "type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps({
                                        "status": "repair_required",
                                        "code": "invalid_decomposition",
                                        "attempts_remaining": MAX_DECOMPOSITION_PROPOSALS - decomposition_rejections,
                                        "retry": {"question": identity["question"], **identity["analysis"]},
                                        "detail": "invalid decomposition: " + detail
                                                 + ". Correct the proposal and call the tool again "
                                                   "with the same question and analysis.",
                                    }),
                                    "is_error": False,
                                })
                            continue
                        if (shaped.get("status") == "clarify" and not clarification_offered
                                and not forced_analysis and decomposition is None
                                and _question_words(question) == _question_words(user_message)
                                and any(item.get("role") == "user" for item in history or ())):
                            # Chrome gate (2026-09-30, payment-commissions, existing conversation): "how
                            # much commission came from cards?" went to the engine as typed, and its
                            # clarification (a SUM of commission_percent, "cards" dropped) became the
                            # reply, although "total commission amount for card payments" two turns
                            # earlier had said what the user means. A transcript that already held that
                            # clarification made it 5 of 8 turns. The clarification of a follow-up sent in
                            # the user's own words goes back to the model once, to be answered from an
                            # earlier turn or asked of the user; the result after it is terminal.
                            clarification_offered = True
                            unsettled = shaped
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps({**_trim_for_model(shaped),
                                                       "status": "ambiguous_wording",
                                                       "detail": SETTLE_FROM_CONVERSATION}),
                                "is_error": False,
                            })
                            continue
                        if shaped.get("status") == "decompose":
                            if decomposition_attempted:
                                shaped = {
                                    "status": "clarify",
                                    "clarify": {"reason": DECOMPOSITION_CLARIFY},
                                }
                            else:
                                pending_decomposition = identity
                        if shaped.get("status") in {"answered", "clarify", "error"}:
                            terminal_query = shaped
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(_trim_for_model(shaped)),
                            "is_error": shaped.get("status") == "error",
                        })
                    elif block.name == "prereasoner_describe":
                        with request_timing.span("engine_call"):
                            described = await engine_client.call_describe(
                                tables, base_url=engine_base_url, token=bearer_token, client=http,
                            )
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(described),
                        })
                    else:
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": f"unknown tool {block.name}", "is_error": True,
                        })
                messages.append({"role": "user", "content": tool_results})
                if terminal_query is not None:
                    # The engine's stable contract has no non-terminal query status. Once it has
                    # answered, clarified, or failed, another tool-enabled round can only ask a
                    # different question. That was the production loop behind the misleading
                    # "step budget" response: five progressively weaker rewrites replaced a useful
                    # terminal result. Let Sonnet present the result, but remove tools for this one
                    # final round so the computation remains the engine's.
                    final_text = _terminal_fallback(terminal_query)
                    presentation_text = ""
                    if history:
                        # In a reopened conversation nearly every answer began "Rechecked it —" or
                        # "Confirmed —" and said the total "still" came to its figure (8 of 8 in the
                        # customer-orders Chrome gate, 2026-10-01): the model presented the result
                        # against its earlier replies. The note stays inside this turn.
                        messages[-1]["content"].append({"type": "text", "text": FRESH_ANSWER_NOTE})
                    try:
                        with request_timing.span("llm"):
                            async with client.messages.stream(
                                model=model,
                                max_tokens=MAX_MODEL_TOKENS,
                                system=system_prompt,
                                thinking={"type": "adaptive"} if provider == "anthropic" else None,
                                messages=messages,
                            ) as presentation_stream:
                                async for delta in presentation_stream.text_stream:
                                    presentation_text += delta
                                    if stream_buffer is not None and presentation_text:
                                        stream_buffer.update(presentation_text)
                                presentation = await presentation_stream.get_final_message()
                        presentation_text = "".join(
                            block.text for block in presentation.content if block.type == "text"
                        ).strip()
                        final_text = _grounded_presentation(
                            terminal_query, presentation_text,
                            asked=" ".join((user_message, *(trace["question"] for trace in traces[-1:]))),
                        )
                    except Exception as exc:  # noqa: BLE001 - presentation is optional after terminal data
                        print(f"[chat] presentation_failed error={type(exc).__name__}", flush=True)
                    break
            else:
                final_text = final_text or (
                    _terminal_fallback(unsettled) if unsettled is not None else TOOL_EXHAUSTED_REPLY
                )
        finally:
            if stream_buffer is not None:
                stream_buffer.close()             # the _emit('reply', final_text) below stays authoritative

    _emit("reply", final_text)                               # the Sonnet text for the rail
    _emit("status", "done")                                  # terminal — the browser stops waiting

    # Lean cross-turn transcript: user + assistant final text only (avoids block-replay pitfalls; the
    # reasoning traces are returned separately and stored per-message by the browser).
    new_history = list(history or [])
    new_history.append({"role": "user", "content": user_message})
    new_history.append({"role": "assistant", "content": final_text})
    _emit("conversation_id", conv or "")                     # stream it so the browser can persist + put it in the URL
    return {"reply": final_text, "traces": traces, "history": new_history, "conversation_id": conv}
