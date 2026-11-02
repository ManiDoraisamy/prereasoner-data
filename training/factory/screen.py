"""The screen every workbook passes before any of its cells may reach Gemini: scan_corpus.py's words and its email count, plus bank details, personal identity numbers, passwords and personal matters. It reads every cell of every tab, hidden ones too. A hit keeps the whole workbook out. The screen names what fired by rule name, never the cell.

A keyword screen is no guarantee (corpora.txt): this lowers the risk, it does not end it.
"""
from __future__ import annotations

import re

# scan_corpus.py's list, unchanged.
CORPUS_WORDS = re.compile(
    r"pass\s?word|passwd|\bpwd\b|user\s?name|user\s?id\b|\blogin\b|\bssn\b|social sec|salary|salaries|payroll|bonus|"
    r"compensation|date of birth|\bdob\b|home phone|home address|passport|employee|medical|\bhire date\b|"
    r"personal|confidential|privileged|checkbook|contribution|credit card", re.I)
EMAIL = re.compile(r"[\w.]+@[\w.]+\.\w+")
MAX_EMAILS = 2

RULES = {
    "bank_details": re.compile(r"\biban\b|\bswift\b|\baba\b|routing (?:no|num)|account (?:no\b|num)|acct\.? ?(?:no\b|num|#)|bank acc|sort code|wire (?:instruction|transfer)|\bach\b.{0,12}(?:no\b|num)|beneficiary", re.I),
    "identity_number": re.compile(r"\b\d{3}-\d{2}-\d{4}\b|driver'?s? licen[cs]e|national id|\btax ?id\b|\bsin number\b|\bnino\b|green card|\bvisa number\b", re.I),
    "password": re.compile(r"pass\s?code|\bpin (?:no|code|number)\b|\bsecret\b|credentials?\b|\bapi key\b|\blog-?on\b", re.I),
    "personal_matter": re.compile(r"\bresume\b|\bcurriculum vitae\b|\bdivorce|\bdisciplin|performance (?:review|rating|ranking|appraisal|evaluation)|\bpre-?rank|year-?end rank|\bprc\b|\bapprais(?:al|ee)\b|\bterminat(?:ed|ion) (?:of employment|for cause)|\bsick (?:leave|day)|\bvacation\b|\battendance\b|\bheadcount\b|\bmaternity\b|\bbenefici|\bdiagnos|\bprescription|\binsurance claim|\bspouse\b|\bmy (?:wife|husband|son|daughter|kids?|mother|father)\b|\bgrievance|\bseverance\b|\b401\(?k\)?\b|\bgarnish|\bchild support\b|\bhealth (?:plan|care|insurance)\b|\b(?:first|last|sur) ?name\b|\bpager\b|\bcell(?:ular)? (?:phone|no\b|#)|\bbirthday\b|\bage\b.{0,3}$", re.I),
}
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_PHONE = re.compile(r"(?<!\d)(?:\(?\d{3}\)?[ .-])\d{3}[ .-]\d{4}(?!\d)|(?<!\d)\d{3}-\d{4}(?!\d)|\bx\d{4,5}\b")
MAX_PHONES = 9     # a list of people's numbers, not a counterparty's one line
SCREEN_VERSION = 2


def _luhn(digits: str) -> bool:
    total = 0
    for at, ch in enumerate(reversed(digits)):
        n = int(ch)
        if at % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def screen_text(text: str) -> set[str]:
    """Which rules one cell's text fires, by name."""
    fired = set()
    if CORPUS_WORDS.search(text):
        fired.add("corpus_word")
    for name, rule in RULES.items():
        if rule.search(text):
            fired.add(name)
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 16 and _luhn(digits):
            fired.add("card_number")
    return fired


def screen_workbook(raw: dict) -> list[str]:
    """Every rule a workbook payload fires: empty means it may be sent. raw is the host's workbook JSON."""
    fired: set[str] = set()
    emails = phones = 0
    for tab in raw.get("tabs") or ():
        fired |= screen_text(str(tab.get("name") or ""))
        for row in tab.get("rows") or ():
            for value in row:
                if isinstance(value, str) and value:
                    fired |= screen_text(value)
                    if EMAIL.search(value):
                        emails += 1
                    if _PHONE.search(value):
                        phones += 1
        for _, _, text in tab.get("formulas") or ():
            if '"' in text:
                fired |= screen_text(text)
    if emails > MAX_EMAILS:
        fired.add("emails")
    if phones > MAX_PHONES:
        fired.add("contact_list")
    return sorted(fired)


__all__ = ["SCREEN_VERSION", "screen_text", "screen_workbook"]
