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

# Comparatives and the operator each makes with the number after "than"; the measure some describe. The quantity
# comparatives compare a count or an amount ("more than 3 models"). sql_expansion.comparative_operator reads both, and
# the search builds a comparison for each of COMPARATIVES (sql_search).
QUANTITY_COMPARATIVES = {"more": ">", "less": "<", "fewer": "<"}
COMPARATIVES = {
    "greater": ">", "higher": ">", "bigger": ">", "larger": ">", "older": ">", "heavier": ">", "taller": ">",
    "longer": ">", "later": ">", "lower": "<", "smaller": "<", "younger": "<", "lighter": "<", "shorter": "<",
    "cheaper": "<", "earlier": "<",
}
COMPARATIVE_COLUMNS = {
    "older": frozenset({"age"}), "younger": frozenset({"age"}), "heavier": frozenset({"weight"}),
    "lighter": frozenset({"weight"}), "taller": frozenset({"height"}), "shorter": frozenset({"height", "length"}),
    "longer": frozenset({"length", "duration"}), "cheaper": frozenset({"price", "cost"}),
}

# Negation and exclusion cues. Compose's exclusion detector reads this pattern, and a semantic search never
# answers a question that uses one: similarity and word matching cannot express 'without a trench coat'. A negated
# comparative is a bound, not an exclusion: "weighing no less than 3000 and no more than 4000" is between them
# (Spider DEV 153, 2026-10-10: it asked for an exclusion, and a set difference was served), and so is any comparative
# the comparison parser reads ("not older than 35" was refused for a missing exclusion while "not higher than 35"
# passed: a second list missed half of them, review of 2026-10-10).
_BOUND = r'(?!\s+(?:' + '|'.join(sorted({**QUANTITY_COMPARATIVES, **COMPARATIVES})) + r')\s+than\b)'
EXCLUSION_CUES = re.compile(r'exclud\w*|without|\bno\b' + _BOUND + r'|\bnot\b' + _BOUND + r'|ignoring|not counting', re.I)

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
    for cue in re.finditer(r'\b(?:how many|number of|count|(?P<most>most|fewest))\s+', low):
        phrase = []
        for word in re.findall(r'[^\W_]+|[.,;:!?]', low[cue.end():], re.UNICODE):
            if word in closed or not word.isalpha() or len(phrase) == 4:
                break
            phrase.append(word)
        # "the most leads" counts leads; "the most expensive leads" grades them and counts nothing.
        if phrase and cue.group('most') and phrase[0] not in nouns:
            continue
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
def value_participles(text):
    """The participles ``text`` relates its rows to a value with, each with the words of its prepositional
    object's own noun phrase: ("departing", ("apg",)) in "flights departing from APG", ("used", ("document",
    "with", "the", "name", "data", "base")) in "used by a document with the name Data base". A clause attached
    to the object is not part of it: in "staff working for the museums that were opened before 2009" the object
    is "the museums". Exclusion cues ("excluding France") are never one."""
    found = []
    for token in spacy_model()(text or ""):
        if token.tag_ not in {"VBN", "VBG"} or EXCLUSION_CUES.fullmatch(token.text):
            continue
        for preposition in token.children:
            if preposition.dep_ not in {"prep", "agent"}:
                continue
            for target in preposition.children:
                if target.dep_ == "pobj":
                    found.append((token.text.lower(), tuple(part.text.lower() for part in _noun_phrase(target)
                                                            if not part.is_punct)))
    return tuple(found)


# The attachments that start a clause of their own, or another conjunct, outside the noun phrase they attach to.
_ATTACHED = frozenset({"relcl", "advcl", "conj", "cc", "punct"})


def _noun_phrase(token):
    """``token`` and its own modifiers, in order: its determiners, adjectives, compounds, appositions,
    participles and prepositional phrases, not a relative or adverbial clause attached to it."""
    phrase = []
    for child in token.lefts:
        if child.dep_ not in _ATTACHED:
            phrase.extend(_noun_phrase(child))
    phrase.append(token)
    for child in token.rights:
        if child.dep_ not in _ATTACHED:
            phrase.extend(_noun_phrase(child))
    return phrase


@lru_cache(maxsize=1024)
def number_words(text):
    """The lowercased words the tagger reads as a spelled number somewhere in ``text``: "two" in "the two
    oldest", never "single" in "single customers"."""
    return frozenset(token.text.lower() for token in spacy_model()(text or "")
                     if token.pos_ == "NUM" and token.text.isalpha())


@lru_cache(maxsize=1024)
def closed_class_words(text):
    """The lowercased words the tagger reads as closed-class somewhere in ``text``, except exclusion cues.

    Cached by text: one request asks this of the same question from the operator readout, the semantic
    predicate and the coverage check, and the tagger's reading of a sentence never changes."""
    return frozenset(
        token.text.lower() for token in spacy_model()(text or "")
        if token.pos_ in CLOSED_CLASS_POS and not EXCLUSION_CUES.fullmatch(token.text)
    )
