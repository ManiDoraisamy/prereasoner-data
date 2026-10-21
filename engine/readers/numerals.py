"""Numbers a question writes as digits or as English number words (a closed class), shared by the example builder
and the decoder."""
import re

WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
         "fifty": 50, "hundred": 100, "once": 1, "twice": 2, "single": 1, "both": 2}
TOKEN = re.compile(r"-?\d[\d,]*(?:\.\d+)?|[A-Za-z]+")


def number(text):
    """The number a span of text names, or None."""
    text = text.strip().lower()
    plain = text.replace(",", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", plain):
        return float(plain) if "." in plain else int(plain)
    return WORDS.get(text)


def mentions(question):
    """(number, start, end) for every number the question writes."""
    for match in TOKEN.finditer(question):
        value = number(match.group())
        if value is not None:
            yield value, match.start(), match.end()
