"""Whether a question shares a word with the header of a column its program uses: the mechanical check of the word-leak rule. Words as the engine reads them: folded runs of letters and digits, in any script. An abbreviation that is not the word does not share it.

A side's common words are counted, never written: a word in at least COMMON_SHARE of the side's own written questions is the language's (a function word, in any language), not a header's, and does not count as shared."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

COMMON_SHARE = 0.02


def common_words(questions: Iterable[str], share: float = COMMON_SHARE) -> frozenset[str]:
    """The words at least `share` of these questions hold, each question counted once."""
    from engine.program.index import words_of
    seen: Counter = Counter()
    n = 0
    for question in questions:
        n += 1
        seen.update(set(words_of(question)))
    return frozenset(word for word, count in seen.items() if count >= share * n) if n else frozenset()


def overlap_of(question: str, header_words: set[str]) -> list[str]:
    from engine.program.index import words_of
    return sorted(set(words_of(question)) & header_words)


__all__ = ["COMMON_SHARE", "common_words", "overlap_of"]
