"""Whether a question shares a word with the header of a column its program uses: the mechanical check of the word-leak rule. Words as the engine reads them: folded, letters and digits, a plural's s off. An abbreviation that is not the word does not share it."""
from __future__ import annotations


def overlap_of(question: str, header_words: set[str]) -> list[str]:
    from engine.program.index import words_of
    return sorted(set(words_of(question)) & header_words)


__all__ = ["overlap_of"]
