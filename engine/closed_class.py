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

# The participles of light verbs (make, do, have, take, give, get, go, come, put) carry no state of their own:
# "how many payments were made by card" asks which payments, not whether they were made.
LIGHT_VERB_PARTICIPLES = frozenset({"made", "done", "had", "taken", "given", "got", "gotten", "gone", "come", "put"})

# Degree words: "the most expensive event", "the least popular workshop", "more expensive than 4000".
DEGREE_MODIFIERS = frozenset({"most", "least", "more", "less"})

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
    """The lowercased words the tagger reads somewhere in ``text`` as a finite or base-form verb, a light verb's
    participle, or an adverb.

    They say what the rows did or how ("which item sold the most units", "documents still pending"), never
    which rows: a dropped one is not a dropped filter. Other participles stay out (see FINITE_VERB_TAGS), and so
    do the exclusion cues."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if ((token.pos_ == "VERB" and (token.tag_ in FINITE_VERB_TAGS or token.lower_ in LIGHT_VERB_PARTICIPLES))
            or token.pos_ == "ADV")
        and not EXCLUSION_CUES.fullmatch(token.text)
    )


@lru_cache(maxsize=1024)
def measure_participles(text, measures):
    """The past participles ``text`` attaches to one of ``measures``: "purchased" in "the total quantity
    purchased", "paid" in "the total amount paid". They say how the measured values came about."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if token.tag_ == "VBN" and token.dep_ == "acl" and token.head.lower_ in measures
    )


@lru_cache(maxsize=1024)
def counted_rows(text):
    """The head of the row noun phrase governed by an explicit count cue.

    This recognizes grammar, not a mapping of business nouns to column labels.
    Modifiers such as German remain constraints and are never consumed here.
    """
    low = str(text).casefold()
    closed = closed_class_words(text)
    nouns = noun_words(text)
    heads = set()
    for cue in re.finditer(r'\b(?:how many|number of|count)\s+', low):
        phrase = []
        for word in re.findall(r'[^\W_]+|[.,;:!?]', low[cue.end():], re.UNICODE):
            if word in closed or not word.isalpha() or len(phrase) == 4:
                break
            phrase.append(word)
        if phrase:
            head = [word for word in phrase if word in nouns]
            heads.add(head[-1] if head else phrase[-1])
    return frozenset(heads)


@lru_cache(maxsize=1024)
def degree_words(text):
    """The lowercased adjectives ``text`` grades: a comparative or superlative ("cheapest", "higher"), or one a
    degree word modifies ("most expensive", "least popular"). An ordering or a comparison realizes them. A named
    entity is a place, not a grade: "German" in "the most German leads" is a filter."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if token.pos_ == "ADJ" and not token.ent_type_
        and (token.tag_ in {"JJR", "JJS"} or any(child.lower_ in DEGREE_MODIFIERS for child in token.children))
    )


@lru_cache(maxsize=1024)
def noun_words(text):
    """The lowercased words the tagger reads somewhere in ``text`` as a noun or a proper noun."""
    return frozenset(token.text.lower() for token in spacy_model()(text or "") if token.pos_ in {"NOUN", "PROPN"})


@lru_cache(maxsize=1024)
def measured_rows(text, measures):
    """The common nouns ``text`` takes one of ``measures`` over: the object of "of" after the measure, as
    "leads" in "the average score of the leads". A named entity ("the amount of France") is not one."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if token.dep_ == "pobj" and token.pos_ == "NOUN" and not token.ent_type_
        and token.head.lower_ == "of" and token.head.head.lower_ in measures
    )


@lru_cache(maxsize=1024)
def recipient_classes(text):
    """Unqualified plural recipient nouns of a verb, as in 'paid to suppliers'.

    This is a grammatical role, not a business-field synonym. A singular/named
    recipient or one carrying a modifier remains a potential row constraint.
    """
    return frozenset(token.text.casefold() for token in spacy_model()(text or '')
                     if token.pos_ == 'NOUN' and token.tag_ == 'NNS' and not token.ent_type_
                     and token.dep_ == 'pobj' and token.head.lower_ == 'to'
                     and token.head.head.pos_ == 'VERB'
                     and not any(child.dep_ in {'amod', 'compound', 'nmod', 'acl', 'appos'}
                                 for child in token.children))


@lru_cache(maxsize=1024)
def closed_class_words(text):
    """The lowercased words the tagger reads as closed-class somewhere in ``text``, except exclusion cues.

    Cached by text: one request asks this of the same question from the operator readout, the semantic
    predicate and the coverage check, and the tagger's reading of a sentence never changes."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if token.pos_ in CLOSED_CLASS_POS and not EXCLUSION_CUES.fullmatch(token.text)
    )
