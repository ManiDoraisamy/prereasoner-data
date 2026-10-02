"""Shared checks for the test suites."""
from __future__ import annotations

import importlib.util


def spacy_model_installed() -> bool:
    """Whether spaCy and its en_core_web_md pipeline can be imported.

    The coverage gate reads the tagger's verbs and adverbs (engine/closed_class.py). The public
    checkout's hermetic CI installs neither package (requirements-ci.txt), so a test that reaches the
    tagger reports SKIP there; the engine image installs both and runs it in full.
    """
    return all(importlib.util.find_spec(name) is not None for name in ("spacy", "en_core_web_md"))
