"""One deterministic renderer for engine-owned results on every product surface."""
from decimal import Decimal, ROUND_HALF_UP, localcontext
import re
from typing import Any


UNAVAILABLE_REPLY = "Prereasoner couldn't answer that right now. Please ask again in a moment."
BUSY_REPLY = "Prereasoner is busy right now. Please send your question again shortly."
SIGN_IN_REPLY = "Your sign-in has expired. Reload Prereasoner, then ask again."
CONVERSATION_LIMIT_REPLY = "Your account has reached its limit of saved conversations. Delete an old one, then ask again."
FULL_CONVERSATION_REPLY = "This conversation is full. Start a new chat to keep asking."
UNREAD_REPLY = ("I couldn't find a way to answer that from your sheets. Try asking again with the column "
                "names as they appear in the sheet.")
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
    if re.match(r"planner:", error):
        # The search read no query from the question ("planner: no valid AST candidate"): the words reached a
        # Sheets user as the whole reply (2026-10-05). Asking again unchanged would not help.
        return UNREAD_REPLY
    if status in (429, 503) or re.search(r"\bbusy\b", error, re.I):
        return BUSY_REPLY
    if status == 401 or re.search(r"\bsign in required\b", error, re.I):
        return SIGN_IN_REPLY
    if shaped.get("unreachable") or (status or 0) >= 500 or not error or _TECHNICAL.search(error):
        return UNAVAILABLE_REPLY
    return error


def unread_words_reply(words, options=()) -> str:
    """A reply naming question words no reading of the sheets reads, and the columns they could mean."""
    quoted = " and ".join(f"“{word}”" for word in words)
    verb = "means" if len(words) == 1 else "mean"
    if options:
        listed = options[0] if len(options) == 1 else ", ".join(options[:-1]) + " or " + options[-1]
        return f"I couldn't tell which column {quoted} {verb}. Did you mean {listed}?"
    return f"I couldn't tell what {quoted} {verb} in your sheets. Ask again with the column's name as the sheet spells it."


def clarify_reply(clarify: dict[str, Any]) -> str:
    """The engine's clarification, with the question it proposes quoted as one the user can send. One without a
    reason or a proposal names the words it could not read: "Which interpretation should I use?" with nothing
    to choose from was the whole reply to "keyword volume for home inspection checklist" (2026-10-05)."""
    proposed = str(clarify.get("proposed") or "").strip()
    dropped = [str(word).strip() for word in clarify.get("dropped") or () if str(word).strip()]
    if not clarify.get("reason") and not proposed and dropped:
        return unread_words_reply(dropped)
    reason = str(clarify.get("reason") or "I need one more detail before I can answer that.").strip()
    if proposed:
        sentence = reason if reason[-1:] in ".?!" else reason + "."
        return f"{sentence} Try asking: “{proposed}”"
    if "?" not in reason and not re.search(r"\b(?:choose|select|try asking)\b", reason, re.I):
        reason = (reason if reason[-1:] in ".!" else reason + ".") + " Which interpretation should I use?"
    return reason


_OFFERED = re.compile(r"Try asking: “([^”]+)”\s*$")


def offered_question(reply: Any) -> str:
    """The question a clarification offered (`clarify_reply`), read back from its reply so a "yes" can
    accept it: the chat model sees no earlier replies, and "yes" had nothing to accept (2026-10-04)."""
    match = _OFFERED.search(str(reply or ""))
    return match[1].strip() if match else ""


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
    copies = shaped.get("layout_copies") or {}
    if isinstance(copies, dict) and copies.get("read") and copies.get("others"):
        # One of several tables that could answer did: say which, so the answer is not taken for all of
        # them together (2026-10-04). The engine names tables canonically ("nt_report"); the chat names them
        # as the user did (`table_names`, "NT Report").
        names = shaped.get("table_names") or {}

        def named(table):
            return str(names.get(table) or str(table).replace("_", " "))

        read = _listed_names([named(name) for name in copies["read"]])
        others = [named(name) for name in copies["others"]]
        notes.append(f"From {read}. {_listed_names(others)} could answer this too; "
                     f"name {'it' if len(others) == 1 else 'one'} in your question to read "
                     f"{'it' if len(others) == 1 else 'that one'} instead.")
    # A question Gemini reworded is the turn's reading, shown on its "read as" line
    # (orchestrator.reading); appended to the answer, it was a second answer to read (2026-10-04).
    suffix = ("\n\n" + " ".join(notes)) if notes else ""
    if len(rows) == 1 and len(rows[0]) == 1:
        if _blank(rows[0][0]):
            return 'No value was recorded for the matching rows.' + suffix
        currency = output_currency(shaped)
        value = _data_text(readable_value(shaped, rows[0][0]))
        return (f"{value} {currency}" if currency else value) + suffix
    if not rows:
        return 'No matching rows were found.' + suffix
    columns = answer.get('columns') or []
    if columns and all(len(row) == len(columns) for row in rows):
        # A long answer lists its first rows and says how many there are. "The result and its reasoning are
        # shown in the workbook" pointed at nothing in the Sheets sidebar, which has no workbook: a total by
        # Plan and Currency over 160 rows showed no number at all (2026-10-05).
        shown = rows[:10]
        preview = _listed(columns, shown, answer.get('column_provenance'), output_currency(shaped))
        while len(preview) > 2000 and len(shown) > 1:
            shown = shown[:-1]
            preview = _listed(columns, shown, answer.get('column_provenance'), output_currency(shaped))
        if len(preview) <= 2000:
            if len(shown) < len(rows):
                preview += f"\n\nThe first {len(shown)} of {len(rows):,} rows."
            return preview + suffix
    return f"The answer has {len(rows):,} rows, each too long to show here." + suffix


def _listed(columns: list, rows: list, provenance: Any, currency: str) -> str:
    """Rows as reply lines: one row is one line, several are a list, and a one-column answer lists
    its values without repeating the column's name. A quantity the engine computed (provenance
    `measure`) is written as a one-number answer writes it, to the cent in a column named for the
    verified currency; a value taken from the data (a year, an ID) is written as it is. "which
    country has the most deposits?" was answered "- country: Switzerland; sum: 1550" one line under
    Switzerland's total as "1,550", and "total_usd: 3495" named neither a reader's words nor cents
    (Chrome gate, 2026-10-04)."""
    measures = _measures(columns, provenance)
    labels = [_label(name, currency) for name in columns]

    def cell(index, value):
        if _blank(value):
            return 'Not recorded'
        if index in measures:
            value = readable_scalar(value, bool(currency) and currency in labels[index].split())
        return _data_text(value)

    if len(columns) == 1:
        lines = [cell(0, row[0]) for row in rows]
    else:
        lines = ['; '.join(f'{label}: {cell(index, value)}'
                           for index, (label, value) in enumerate(zip(labels, row))) for row in rows]
    return lines[0] if len(lines) == 1 else '\n'.join('- ' + line for line in lines)


def _measures(columns: list, provenance: Any) -> frozenset[int]:
    """The answer columns the engine computed, as its column provenance marks them. Without a record
    for every column nothing is formatted: a year is never written as "2,026"."""
    if not isinstance(provenance, list) or len(provenance) != len(columns):
        return frozenset()
    return frozenset(index for index, record in enumerate(provenance)
                     if isinstance(record, dict) and record.get('measure') is True)


def _label(name: Any, currency: str) -> str:
    """A column name as a reader writes it: "customer_name" is "customer name", and the verified
    output currency is its code ("total_usd" is "total USD")."""
    words = str(name).replace('_', ' ').split()
    if currency:
        words = [currency if word.casefold() == currency.casefold() else word for word in words]
    return _data_text(' '.join(words)) or 'value'


def _listed_names(names) -> str:
    names = [_data_text(name) for name in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _blank(value: Any) -> bool:
    """An empty cell: serving writes a missing value as "", which answered a blank cell with an
    empty reply (2026-10-04)."""
    return value is None or not str(value).strip()


def _data_text(value: Any) -> str:
    """A cell as reply text, on one line. Replies render Markdown links, so a link written in a cell
    ("[x](https://…)") is broken apart and shown as the text it is: a shared sheet cannot put a live
    link in an answer."""
    return re.sub(r"\]\s*\(", "] (", re.sub(r"\s+", " ", str(value)).strip())


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


