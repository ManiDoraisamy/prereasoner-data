"""The learned half of the header decision: a boosted sum of stumps over named line coordinates, where rules stall (header blocks on report-shaped tabs).

line_features(facts, grid) -> {row: {name: value}} is the one definition of the inputs, used by training/sheet/train.py and by serving. The artifact is JSON holding one step function per named feature, evaluated here in plain Python: no
scikit-learn at serving, the same answer on every machine. A score is the base plus one number per feature, and contributions() returns those numbers, so a decision can be shown and checked.
"""
from __future__ import annotations

import json
import os
from bisect import bisect_left
from functools import lru_cache

from engine.sheet.grid import Grid
from engine.sheet.line_facts import LineFacts
from engine.sheet.names import V_NUMBER

ARTIFACT = os.path.join(os.path.dirname(__file__), "line_model.json")

FEATURES = ("filled", "text", "real_figures", "year_or_index", "date", "period", "error", "marker", "label_share", "width_share", "starts_left", "wordy", "longest", "total_word",
            "bold", "fill", "under", "bold_next", "fill_next", "under_next", "repeated", "sequence", "gap_above", "gap_below", "wide_merge", "label_value", "value_score", "named", "typed_below",
            "overlap_below", "over_figures", "order", "is_first", "frozen_last", "in_frozen", "autofilter_top", "prev_filled", "prev_figures", "prev_bold", "prev_gap", "prev_single",
            "next_filled", "next_figures", "next_bold", "next_over_figures", "next_value_score", "next_same_columns", "next_same_kinds", "formula_cells")


def line_features(facts: dict[int, LineFacts], grid: Grid) -> dict[int, dict[str, float]]:
    """The model's inputs for every filled line, all named."""
    tab = grid.tab
    rows = sorted(facts)
    leftmost = min((f.first_col for f in facts.values()), default=1)
    width = max(1, grid.width - leftmost + 1)
    top_filter = tab.autofilter[0] if tab.autofilter else None
    out: dict[int, dict[str, float]] = {}
    for at, r in enumerate(rows):
        f = facts[r]
        before = facts[rows[at - 1]] if at else None
        after = facts[rows[at + 1]] if at + 1 < len(rows) else None
        labels = f.text + f.date + f.period + (f.number - f.real_figures)
        same_columns = same_kinds = 0.0
        if after is not None:
            here, under = grid.rows[r], grid.rows[after.row]
            shared = [c for c in here if c in under]
            same_columns = len(shared) / max(len(here), len(under))
            same_kinds = sum(1 for c in shared if here[c].kind == under[c].kind) / max(1, len(here))
        out[r] = {
            "filled": f.filled, "text": f.text, "real_figures": f.real_figures, "year_or_index": f.number - f.real_figures, "date": f.date, "period": f.period, "error": f.error, "marker": f.marker,
            "label_share": labels / f.filled, "width_share": f.filled / width, "starts_left": float(f.first_col == leftmost), "wordy": f.wordy, "longest": f.longest, "total_word": float(f.total_word),
            "bold": f.bold, "fill": f.fill, "under": f.under, "bold_next": float(f.bold_next), "fill_next": float(f.fill_next), "under_next": float(f.under_next), "repeated": float(f.repeated),
            "sequence": float(f.sequence), "gap_above": float(f.gap_above), "gap_below": float(f.gap_below), "wide_merge": f.wide_merge, "label_value": float(f.label_value), "value_score": f.value_score,
            "named": f.named, "typed_below": f.typed_below, "overlap_below": f.overlap_below, "over_figures": f.over_figures, "order": min(at, 64), "is_first": float(at == 0),
            "frozen_last": float(tab.frozen_rows == r), "in_frozen": float(0 < r <= tab.frozen_rows), "autofilter_top": float(top_filter == r),
            "prev_filled": before.filled if before else 0, "prev_figures": before.real_figures if before else 0, "prev_bold": before.bold if before else 0.0, "prev_gap": float(before.gap_above) if before else 1.0,
            "prev_single": float(before.filled == 1) if before else 0.0,
            "next_filled": after.filled if after else 0, "next_figures": after.real_figures if after else 0, "next_bold": after.bold if after else 0.0, "next_over_figures": after.over_figures if after else 0.0,
            "next_value_score": after.value_score if after else 0.0, "next_same_columns": same_columns, "next_same_kinds": same_kinds,
            "formula_cells": sum(1 for c, cell in grid.rows[r].items() if cell.kind == V_NUMBER and (r, c) in grid.shapes),
        }
    return out


@lru_cache(maxsize=1)
def load_model(path: str = ARTIFACT) -> dict:
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def _step(step: dict, value: float) -> float:
    """One feature's function: value at or below cuts[i] takes values[i]; above the last cut, the last value."""
    return step["values"][bisect_left(step["cuts"], value)]


def header_score(values: dict[str, float], model: dict | None = None) -> float:
    """The additive log-odds that a line is a header: the base plus one number per named feature."""
    model = model or load_model()
    return model["base"] + sum(_step(step, values[name]) for name, step in model["steps"].items())


def contributions(values: dict[str, float], model: dict | None = None) -> dict[str, float]:
    """The score, feature by feature: base + the sum of these is header_score, so a decision can be shown and checked."""
    model = model or load_model()
    return {name: _step(step, values[name]) for name, step in model["steps"].items()}
