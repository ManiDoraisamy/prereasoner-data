"""Dependency-free normalization shared by offline sync builders."""
from __future__ import annotations

import re


def normalize_surface(value: str | None) -> str:
    """Return the stable exact-match key stored in knowledgebase.words.norm."""
    value = (value or "").strip().lower()
    if value.startswith("the "):
        value = value[4:]
    return re.sub(r"[^a-z0-9]+", "", value)
