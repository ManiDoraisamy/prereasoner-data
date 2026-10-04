"""One deterministic renderer for engine-owned results on every product surface."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
import re
from typing import Any


UNAVAILABLE_REPLY = "Prereasoner couldn't answer that right now. Please ask again in a moment."
BUSY_REPLY = "Prereasoner is busy right now. Please send your question again shortly."
SIGN_IN_REPLY = "Your sign-in has expired. Reload Prereasoner, then ask again."
CONVERSATION_LIMIT_REPLY = "Your account has reached its limit of saved conversations. Delete an old one, then ask again."
FULL_CONVERSATION_REPLY = "This conversation is full. Start a new chat to keep asking."
# Engine text that describes code rather than the request: an exception name, a query, a payload,
# a status line or an address. Users see a sentence; the raw text stays in the trace.
_TECHNICAL = re.compile(r"^[A-Za-z_.]*(?:Error|Exception)\b|Traceback|\bSQL\b|psycopg|[{}]|\bHTTP \d{3}\b|https?://",
                        re.I)


def error_reply(shaped: dict[str, Any]) -> str:
    """A failed engine call as one sentence the user can act on. The engine's own text reaches the
    user only when it was written for them; a 500, a lost connection or an exception message showed
    as "internal server error" or the engine's internal address (2026-10-04)."""
    error = str(shaped.get("error") or "")
    status = shaped.get("http_status")
    if re.search(r"\bconversation (?:storage )?limit reached\b", error, re.I):
        return CONVERSATION_LIMIT_REPLY
    if re.search(r"\btoo many (?:analyses|revisions)\b|\b(?:workbook|state) is too large\b", error, re.I):
        return FULL_CONVERSATION_REPLY
    if status in (429, 503) or re.search(r"\bbusy\b", error, re.I):
        return BUSY_REPLY
    if status == 401 or re.search(r"\bsign in required\b", error, re.I):
        return SIGN_IN_REPLY
    if shaped.get("unreachable") or (status or 0) >= 500 or not error or _TECHNICAL.search(error):
        return UNAVAILABLE_REPLY
    return error


def clarify_reply(clarify: dict[str, Any]) -> str:
    """The engine's clarification, with the question it proposes quoted as one the user can send."""
    reason = str(clarify.get("reason") or "I need one more detail before I can answer that.").strip()
    proposed = str(clarify.get("proposed") or "").strip()
    if proposed:
        sentence = reason if reason[-1:] in ".?!" else reason + "."
        return f"{sentence} Try asking: “{proposed}”"
    if "?" not in reason and not re.search(r"\b(?:choose|select|try asking)\b", reason, re.I):
        reason += " Which interpretation should I use?"
    return reason


def terminal_reply(shaped: dict[str, Any]) -> str:
    """Render the engine's terminal facts, without an external presentation model."""
    if shaped.get("status") == "clarify":
        return clarify_reply(shaped.get("clarify") or {})
    if shaped.get("status") == "error":
        return error_reply(shaped)
    answer = shaped.get("answer") or {}
    rows = answer.get("rows") or []
    notes = []
    unmatched = shaped.get("unmatched") or {}
    if unmatched.get("rows"):
        notes.append(f"{unmatched['rows']} of {unmatched.get('of', '?')} source rows could not be matched and were excluded.")
    # A question Gemini reworded is the turn's reading, shown on its "read as" line
    # (orchestrator.reading); appended to the answer, it was a second answer to read (2026-10-04).
    suffix = ("\n\n" + " ".join(notes)) if notes else ""
    if len(rows) == 1 and len(rows[0]) == 1:
        if rows[0][0] is None:
            return 'No value was recorded for the matching rows.' + suffix
        currency = output_currency(shaped)
        value = readable_value(shaped, rows[0][0])
        return (f"{value} {currency}" if currency else value) + suffix
    if not rows:
        return 'No matching rows were found.' + suffix
    columns = answer.get('columns') or []
    if columns and len(rows) <= 10 and all(len(row) == len(columns) for row in rows):
        def cell(value):
            return 'Not recorded' if value is None else re.sub(r'\s+', ' ', str(value)).strip()
        preview = '\n'.join('- ' + '; '.join(f'{cell(name)}: {cell(value)}'
                            for name, value in zip(columns, row)) for row in rows)
        if len(preview) <= 2000:
            return preview + suffix
    return "I completed the calculation; the result and its reasoning are shown in the workbook." + suffix


def readable_value(shaped: dict[str, Any], value: Any) -> str:
    """The engine's one-number answer as a reply writes it: a share of a whole as a percentage (the
    engine's `unit`), any other number by `readable_scalar`, to the cent in a verified currency."""
    if shaped.get("unit") == "percent":
        return readable_percent(value)
    return readable_scalar(value, bool(output_currency(shaped)))


def readable_percent(value: Any) -> str:
    """A verified fraction as a percentage: 0.3 is "30%", 0.62318... is "62.32%"."""
    text = str(value).strip()
    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return text
    with localcontext() as context:
        context.prec = max(28, len(text) + 10)
        percent = (Decimal(text) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP).normalize()
    return f"{percent:,f}%"


def readable_scalar(value: Any, money: bool) -> str:
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
        with localcontext() as context:
            context.prec = max(28, len(text) + 10)
            return f"{number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"
    if "." not in text:
        return f"{number:,}"
    return f"{number:.3g}"


def output_currency(shaped: dict[str, Any]) -> str:
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


