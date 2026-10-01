"""Closed-class words: determiners, pronouns, adpositions, auxiliaries, conjunctions, particles, interjections
and punctuation.

They carry grammar, never data. An aggregate is not expressed by 'a' or 'which' (the operator readout once
read COUNT off the article in 'who ordered a trench coat in France'), and 'everything' is not free text to
search the rows for. spaCy's tagger decides by context, so no word list has to be kept in step with the
grammar. It is the engine's one spaCy model: EntityQuery's candidate extraction loads it through here too.

Negation and exclusion cues are the exception. 'not', 'no' and 'without' are closed-class, but they carry a
constraint, so closed_class_words never returns them: a query that drops one must still be caught.
"""
from __future__ import annotations

import re
import threading
from functools import lru_cache

CLOSED_CLASS_POS = frozenset({"DET", "PRON", "ADP", "AUX", "CCONJ", "SCONJ", "PART", "INTJ", "PUNCT", "SYM"})

# A verb's finite or base forms (sold, placed, comes, weigh). Participles (returned, listed, pending) are left
# out: "orders were returned" names a state of the rows, and a state can be a row filter.
FINITE_VERB_TAGS = frozenset({"VB", "VBD", "VBP", "VBZ"})

# Negation and exclusion cues. Compose's exclusion detector reads this pattern, and a semantic search never
# answers a question that uses one: similarity and word matching cannot express 'without a trench coat'.
EXCLUSION_CUES = re.compile(r'exclud\w*|without|\bno\b|\bnot\b|ignoring|not counting', re.I)

_MODEL = None
_LOCK = threading.Lock()


def spacy_model():
    """The process's spaCy pipeline (en_core_web_md), loaded once."""
    global _MODEL
    if _MODEL is None:
        with _LOCK:
            if _MODEL is None:
                import spacy
                _MODEL = spacy.load("en_core_web_md", disable=["lemmatizer"])
    return _MODEL


@lru_cache(maxsize=1024)
def action_words(text):
    """The lowercased words the tagger reads somewhere in ``text`` as a finite or base-form verb or an adverb.

    They say what the rows did or how ("which item sold the most units", "documents still pending"), never
    which rows: a dropped one is not a dropped filter. Participles stay out (see FINITE_VERB_TAGS), and so do
    the exclusion cues."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if ((token.pos_ == "VERB" and token.tag_ in FINITE_VERB_TAGS) or token.pos_ == "ADV")
        and not EXCLUSION_CUES.fullmatch(token.text)
    )


@lru_cache(maxsize=1024)
def noun_words(text):
    """The lowercased words the tagger reads somewhere in ``text`` as a noun or a proper noun."""
    return frozenset(token.text.lower() for token in spacy_model()(text or "") if token.pos_ in {"NOUN", "PROPN"})


@lru_cache(maxsize=1024)
def closed_class_words(text):
    """The lowercased words the tagger reads as closed-class somewhere in ``text``, except exclusion cues.

    Cached by text: one request asks this of the same question from the operator readout, the semantic
    predicate and the coverage check, and the tagger's reading of a sentence never changes."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if token.pos_ in CLOSED_CLASS_POS and not EXCLUSION_CUES.fullmatch(token.text)
    )
