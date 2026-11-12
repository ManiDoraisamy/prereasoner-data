"""The learned half of the header decision: a boosted sum of stumps over named line coordinates, where rules stall (header blocks on report-shaped tabs).

line_features(facts, grid) -> {row: {name: value}} is the one definition of the inputs, used by training/sheet/train.py and by serving. The artifact is JSON holding one step function per named feature, evaluated here in plain Python: no
scikit-learn at serving, the same answer on every machine. A score is the base plus one number per feature, and contributions() returns those numbers, so a decision can be shown and checked.
"""
from __future__ import annotations

import json
import os
from bisect import bisect_left
from collections import Counter
from functools import lru_cache

from engine.sheet.grid import Grid
from engine.sheet.line_facts import LineFacts
from engine.sheet.names import V_DATE, V_ERROR, V_MARKER, V_NUMBER, V_PERIOD, V_TEXT

ARTIFACT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "sheet_line_model.json")

FEATURES = ("filled", "text", "real_figures", "year_or_index", "date", "period", "error", "marker", "label_share", "width_share", "starts_left", "wordy", "longest",
            "bold", "fill", "under", "bold_next", "fill_next", "under_next", "repeated", "sequence", "gap_above", "gap_below", "wide_merge", "label_value", "value_score", "named", "typed_below",
            "overlap_below", "over_figures", "order", "is_first", "frozen_last", "in_frozen", "autofilter_top", "prev_filled", "prev_figures", "prev_bold", "prev_gap", "prev_single",
            "next_filled", "next_figures", "next_bold", "next_over_figures", "next_value_score", "next_same_columns", "next_same_kinds", "formula_cells",
            # The line against the columns under it.
            "covered_share", "kind_break_share", "text_over_typed", "text_over_typed_share", "text_repeats_below", "text_over_text", "length_against_below",
            # The line against the line above it, and what stands around it.
            "prev_same_columns", "prev_same_kinds", "gap_after_figures", "rule_above", "rule_below", "blank_rows_above",
            # How its text reads.
            "caps_share", "digits_share", "mean_words", "distinct_share", "next_text_share", "next_denser",
            # Where it sits on the tab, and what the columns under it spell.
            "columns_used", "filled_against_common", "digits_under_text")

_BELOW = 8
_DIGIT = str.maketrans("", "", "0123456789")


def _kind_group(kind: str) -> str:
    return V_NUMBER if kind in (V_NUMBER, V_ERROR, V_MARKER) else V_DATE if kind in (V_DATE, V_PERIOD) else V_TEXT


def _under_columns(grid: Grid, row: int, below: list[int]) -> dict[str, float]:
    """What the line says against the columns under it: a name stands over cells of another kind, and its words do not come back in them."""
    line = grid.rows[row]
    covered = breaks = over_typed = repeats = over_text = digits_under = 0
    own_length = under_length = 0.0
    for c, cell in line.items():
        cells = [grid.rows[x][c] for x in below if c in grid.rows[x]]
        if not cells:
            continue
        covered += 1
        kinds = Counter(_kind_group(under.kind) for under in cells)
        top = min(kinds, key=lambda k: (-kinds[k], k))
        if _kind_group(cell.kind) != top:
            breaks += 1
        if cell.kind == V_TEXT:
            if top != V_TEXT:
                over_typed += 1
            else:
                over_text += 1
                words = [len(str(under.value)) for under in cells if under.kind == V_TEXT]
                if words:
                    own_length += len(str(cell.value))
                    under_length += sum(words) / len(words)
                    digits_under += sum(1 for under in cells if under.kind == V_TEXT and str(under.value).translate(_DIGIT) != str(under.value)) * 2 >= len(words) and str(cell.value).translate(_DIGIT) == str(cell.value)
            if any(under.kind == V_TEXT and under.value == cell.value for under in cells):
                repeats += 1
    size = max(1, len(line))
    return {"covered_share": covered / size, "kind_break_share": breaks / size, "text_over_typed": over_typed, "text_over_typed_share": over_typed / size, "text_repeats_below": repeats / size, "text_over_text": over_text, "digits_under_text": digits_under,
            "length_against_below": own_length / under_length if under_length else 1.0}


def _text_shape(grid: Grid, row: int) -> dict[str, float]:
    """How the line's text reads: no word of any domain, only case, digits, length and repeats."""
    texts = [str(cell.value) for cell in grid.rows[row].values() if cell.kind == V_TEXT]
    if not texts:
        return {"caps_share": 0.0, "digits_share": 0.0, "mean_words": 0.0, "distinct_share": 1.0}
    caps = sum(1 for t in texts if t.isupper())
    digits = sum(1 for t in texts if t.translate(_DIGIT) != t)
    return {"caps_share": caps / len(texts), "digits_share": digits / len(texts), "mean_words": sum(len(t.split()) for t in texts) / len(texts), "distinct_share": len(set(texts)) / len(texts)}


def line_features(facts: dict[int, LineFacts], grid: Grid) -> dict[int, dict[str, float]]:
    """The model's inputs for every filled line, all named."""
    tab = grid.tab
    rows = sorted(facts)
    leftmost = min((f.first_col for f in facts.values()), default=1)
    width = max(1, grid.width - leftmost + 1)
    top_filter = tab.autofilter[0] if tab.autofilter else None
    common = Counter(f.filled for f in facts.values())
    usual = min(common, key=lambda n: (-common[n], n)) if common else 1
    out: dict[int, dict[str, float]] = {}
    for at, r in enumerate(rows):
        f = facts[r]
        before = facts[rows[at - 1]] if at else None
        after = facts[rows[at + 1]] if at + 1 < len(rows) else None
        labels = f.text + f.date + f.period + (f.number - f.real_figures)
        same_columns = same_kinds = prev_columns = prev_kinds = next_text = 0.0
        here = grid.rows[r]
        if after is not None:
            under = grid.rows[after.row]
            shared = [c for c in here if c in under]
            same_columns = len(shared) / max(len(here), len(under))
            same_kinds = sum(1 for c in shared if here[c].kind == under[c].kind) / max(1, len(here))
            next_text = after.text / after.filled
        if before is not None:
            over = grid.rows[before.row]
            shared = [c for c in here if c in over]
            prev_columns = len(shared) / max(len(here), len(over))
            prev_kinds = sum(1 for c in shared if here[c].kind == over[c].kind) / max(1, len(here))
        blank_above = r - before.row - 1 if before is not None else r - 1
        out[r] = {
            "filled": f.filled, "text": f.text, "real_figures": f.real_figures, "year_or_index": f.number - f.real_figures, "date": f.date, "period": f.period, "error": f.error, "marker": f.marker,
            "label_share": labels / f.filled, "width_share": f.filled / width, "starts_left": float(f.first_col == leftmost), "wordy": f.wordy, "longest": f.longest,
            "bold": f.bold, "fill": f.fill, "under": f.under, "bold_next": float(f.bold_next), "fill_next": float(f.fill_next), "under_next": float(f.under_next), "repeated": float(f.repeated),
            "sequence": float(f.sequence), "gap_above": float(f.gap_above), "gap_below": float(f.gap_below), "wide_merge": f.wide_merge, "label_value": float(f.label_value), "value_score": f.value_score,
            "named": f.named, "typed_below": f.typed_below, "overlap_below": f.overlap_below, "over_figures": f.over_figures, "order": min(at, 64), "is_first": float(at == 0),
            "frozen_last": float(tab.frozen_rows == r), "in_frozen": float(0 < r <= tab.frozen_rows), "autofilter_top": float(top_filter == r),
            "prev_filled": before.filled if before else 0, "prev_figures": before.real_figures if before else 0, "prev_bold": before.bold if before else 0.0, "prev_gap": float(before.gap_above) if before else 1.0,
            "prev_single": float(before.filled == 1) if before else 0.0,
            "next_filled": after.filled if after else 0, "next_figures": after.real_figures if after else 0, "next_bold": after.bold if after else 0.0, "next_over_figures": after.over_figures if after else 0.0,
            "next_value_score": after.value_score if after else 0.0, "next_same_columns": same_columns, "next_same_kinds": same_kinds,
            "formula_cells": sum(1 for c, cell in here.items() if cell.kind == V_NUMBER and (r, c) in grid.shapes),
            "prev_same_columns": prev_columns, "prev_same_kinds": prev_kinds, "gap_after_figures": float(f.gap_above and before is not None and before.real_figures > 0),
            "rule_above": float(r - 1 in grid.rule_lines), "rule_below": float(r + 1 in grid.rule_lines), "blank_rows_above": min(blank_above, 8), "next_text_share": next_text,
            "next_denser": after.filled / f.filled if after else 0.0, "columns_used": min(width, 64), "filled_against_common": f.filled / usual,
            **_under_columns(grid, r, rows[at + 1:at + 1 + _BELOW]), **_text_shape(grid, r),
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
    """The additive log-odds that a line is a header on what it shows itself: the base plus one number per named feature."""
    model = model or load_model()
    return model["base"] + sum(_step(step, values[name]) for name, step in model["steps"].items())


def contributions(values: dict[str, float], model: dict | None = None) -> dict[str, float]:
    """The score, feature by feature: base + the sum of these is header_score, so a decision can be shown and checked."""
    model = model or load_model()
    return {name: _step(step, values[name]) for name, step in model["steps"].items()}


SECOND_FEATURES = ("score", "score_prev", "score_next", "prev_is_head", "next_is_head", "words_of_head", "style_of_head", "shape_of_head", "lines_since_head", "no_head_above", "tab_has_head", "best_of_top", "best_with_no_head", "set_off_below", "single_over_head", "style_shape_of_head")
_TOP_LINES = 15
_HEADS_KEPT = 8


def second_features(facts: dict[int, LineFacts], grid: Grid, scores: dict[int, float]) -> dict[int, dict[str, float]]:
    """A second reading of every line, once the first has scored them all: what its neighbours scored, and how it stands to the lines above that scored as headers: their words in the same columns (a repeated header), their style set off from the line under it (the next table's header styled alike), their shape."""
    rows = sorted(facts)
    leftmost = min((f.first_col for f in facts.values()), default=1)
    style = {r: (facts[r].bold >= 0.5, facts[r].fill >= 0.5, facts[r].under >= 0.5) for r in rows}
    top = rows[:_TOP_LINES]
    best = max(top, key=lambda r: (scores[r], -r)) if top else None
    any_head = any(scores[r] >= 0 for r in rows)
    heads: list[int] = []
    head_at = 0
    out: dict[int, dict[str, float]] = {}
    for at, r in enumerate(rows):
        line = grid.rows[r]
        before = rows[at - 1] if at else None
        after = rows[at + 1] if at + 1 < len(rows) else None
        words = shape = 0.0
        like = False
        for h in heads[-_HEADS_KEPT:]:
            there = grid.rows[h]
            same = sum(1 for c, cell in line.items() if c in there and str(there[c].value).lower() == str(cell.value).lower())
            words = max(words, same / len(line))
            shape = max(shape, len(line.keys() & there.keys()) / len(line.keys() | there.keys()))
            like = like or (any(style[r]) and style[h] == style[r])
        set_off = any(style[r]) and (after is None or style[after] != style[r])
        out[r] = {"score": scores[r], "score_prev": scores[before] if before is not None else -9.0, "score_next": scores[after] if after is not None else -9.0,
                  "prev_is_head": float(before in heads), "next_is_head": float(after is not None and scores[after] >= 0), "words_of_head": words, "style_of_head": float(like and set_off), "shape_of_head": shape,
                  "lines_since_head": min(at - head_at, 64) if heads else 64, "no_head_above": float(not heads), "tab_has_head": float(any_head),
                  "best_of_top": float(r == best), "best_with_no_head": float(r == best and not any_head), "set_off_below": float(set_off),
                  "single_over_head": float(len(line) == 1 and facts[r].first_col > leftmost and after is not None and scores[after] >= 0 and after - r <= 2 and facts[after].first_col <= facts[r].first_col <= facts[after].last_col),
                  "style_shape_of_head": float(like and set_off and shape >= 0.8)}
        if scores[r] >= 0:
            heads.append(r)
            head_at = at
    return out


def final_scores(facts: dict[int, LineFacts], grid: Grid, features: dict[int, dict[str, float]], model: dict | None = None) -> dict[int, float]:
    """Every line's score: the first reading, then the second when the artifact holds one. Each is a base plus one number per named feature."""
    model = model or load_model()
    first = {r: header_score(values, model) for r, values in features.items()}
    second = model.get("second")
    if not second:
        return first
    return {r: header_score(values, second) for r, values in second_features(facts, grid, first).items()}
