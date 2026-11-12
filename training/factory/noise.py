"""Typos and shorthand, by script: the gold stays what it was, the words get messier, and the share is exact. Deterministic per question id. A word's position is kept, so a link still points at it.

Shorthand is the general form desks write in any script: a long word cut to its first three or four letters (training/named/augment.py cuts headers the same way). No list of any trade's or language's short forms is kept."""
from __future__ import annotations

import hashlib
import random
import re

SHARE = 0.15
LONG = 5
_WORD = re.compile(r"[^\W\d_]+")


def _roll(question_id: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.blake2b(question_id.encode("utf-8"), digest_size=8).digest(), "little"))


def _typo(word: str, rng: random.Random) -> str:
    at = rng.randrange(1, len(word) - 1)
    kind = rng.randrange(3)
    if kind == 0:
        return word[:at] + word[at + 1] + word[at] + word[at + 2:]
    if kind == 1:
        return word[:at] + word[at + 1:]
    return word[:at] + word[at] + word[at:]


def messy(question_id: str, question: str) -> tuple[str, dict | None]:
    """The question as typed, and what changed: {kind, word, became}, or None for most questions."""
    rng = _roll(question_id)
    if rng.random() >= SHARE:
        return question, None
    long_enough = [m for m in _WORD.finditer(question) if len(m.group(0)) >= LONG]
    if not long_enough:
        return question, None
    match = long_enough[rng.randrange(len(long_enough))]
    if rng.random() < 0.5:
        became, kind = match.group(0)[:rng.choice((3, 4))], "shorthand"
    else:
        became, kind = _typo(match.group(0), rng), "typo"
    return question[:match.start()] + became + question[match.end():], {"kind": kind, "word": match.group(0), "became": became}


__all__ = ["messy"]
