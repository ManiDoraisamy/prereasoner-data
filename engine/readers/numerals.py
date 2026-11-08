"""Numbers a question writes as digits or as English number words (a closed class), shared by the example builder
and the decoder."""
import re

WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
         "fifty": 50, "hundred": 100, "once": 1, "twice": 2, "single": 1, "both": 2}
WORDS.update({"sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
              "thousand": 1000, "million": 1000000, "billion": 1000000000,
              "trillion": 1000000000000, "half": 0.5, "quarter": 0.25})
SCALES = {name: WORDS[name] for name in ("hundred", "thousand", "million", "billion", "trillion")}
CARDINAL = {name: value for name, value in WORDS.items() if type(value) is int and value < 100
            and name not in {"once", "twice", "single", "both"}}
TOKEN = re.compile(r"-?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d(?:[\d,]*\d)?)(?:\.\d+)?|[A-Za-z]+")


def number(text):
    """The number a span of text names, or None."""
    text = text.strip().lower()
    if "," in text:
        from engine.numeric import GROUPED_DIGITS
        if not re.fullmatch(rf"-?(?:{GROUPED_DIGITS})(?:\.\d+)?", text):
            return None
    plain = text.replace(",", "")
    if re.fullmatch(r"-?\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:\.\d+)?", plain):
        plain = re.sub(r"[ \u00a0\u202f]", "", plain)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", plain):
        return float(plain) if "." in plain else int(plain)
    if text in WORDS:
        return WORDS[text]
    parts = [p for p in re.split(r"[\s-]+", text) if p != "and"]
    if not parts or any(p not in CARDINAL and p.rstrip("s") not in SCALES for p in parts):
        return None
    total, current, previous, largest = 0, 0, None, float("inf")
    for part in parts:
        if part in CARDINAL:
            value = CARDINAL[part]
            if previous is not None and previous < 100 and (previous < 20 or value >= 10):
                return None
            current += value; previous = value
        else:
            scale = SCALES[part.rstrip("s")]
            if scale == 100:
                if not 0 < current < 100: return None
                current *= 100
            else:
                if scale >= largest: return None
                total += (current or 1) * scale; current = 0; largest = scale
            previous = scale
    return total + current


def mentions(question):
    """(number, start, end) for every number the question writes."""
    matches, i = list(TOKEN.finditer(question)), 0
    numeric_words = set(CARDINAL) | set(SCALES) | {word + "s" for word in SCALES} | {"and"}
    while i < len(matches):
        match = matches[i]
        if match.group().lower() in numeric_words - {"and"}:
            stop = i + 1
            while stop < len(matches) and matches[stop].group().lower() in numeric_words and re.fullmatch(r"[\s-]+", question[matches[stop - 1].end():matches[stop].start()]):
                stop += 1
            for end in range(stop, i, -1):
                if matches[end - 1].group().lower() == "and": continue
                value = number(question[match.start():matches[end - 1].end()])
                if value is not None:
                    yield value, match.start(), matches[end - 1].end()
                    i = end
                    break
            else:
                i += 1
            continue
        start, span = match.start(), match.group()
        # A hyphen after a numeric operand separates a range, score or date;
        # it is not the next operand's unary sign. Keep a second hyphen as
        # the sign in "5--3", and keep genuinely negative coordinates.
        if span.startswith("-") and question[start - 1:start].isdigit():
            start += 1
            span = span[1:]
        value = number(span)
        if value is None and re.fullmatch(r"-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?", match.group()):
            # An explicit coordinate/tuple comma separates operands. It is
            # neither a thousands separator nor a decimal-locale assumption.
            nearby = question[max(0, match.start() - 40):match.start()]
            if re.search(r"\bcoordinates?\b", nearby, re.IGNORECASE) or (match.start() > 0 and
                    question[match.start() - 1] == "(" and question[match.end():match.end() + 1] == ")"):
                for part in re.finditer(r"-?\d+(?:\.\d+)?", match.group()):
                    yield number(part.group()), match.start() + part.start(), match.start() + part.end()
                i += 1
                continue
        if (value is not None and i + 1 < len(matches) and matches[i + 1].group().lower().rstrip("s") in SCALES
                and re.fullmatch(r"\s*", question[match.end():matches[i + 1].start()])):
            yield value * SCALES[matches[i + 1].group().lower().rstrip("s")], start, matches[i + 1].end()
            i += 2
            continue
        if value is not None:
            yield value, start, match.end()
        i += 1


def intervals(question):
    """Explicit numeric interval endpoints and their original character span.

    Values inside an interval are implied by its bounds, not literal mentions.
    Keep them separate so mention offsets and unit attachment stay exact.
    """
    found = list(mentions(question))
    for first, last in zip(found, found[1:]):
        low, start, end = first
        high, next_start, stop = last
        cue = re.search(r"\b(from|between)\s*$", question[:start], re.IGNORECASE)
        if cue is None:
            continue
        separator = question[end:next_start].strip().lower()
        allowed = {"and"} if cue[1].lower() == "between" else {"to", "through", "-", "–", "—"}
        if separator in allowed:
            yield low, high, start, stop
