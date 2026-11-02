"""Line roles, in the signal order the evidence gives: printed lines by rule from words, formulas and style (rules reach them: total recall .81 to .96); header lines by the small line model where rules stall; bands, notes, titles and records by rule from where each line sits."""
from __future__ import annotations

from collections import Counter

from engine.sheet.grid import Grid
from engine.sheet.line_facts import LineFacts, line_facts
from engine.sheet.line_model import header_score, line_features, load_model
from engine.sheet.names import (BAND, HEADER, NOTE, RECORD, S_ABOVE_TABLE, S_ALONE_AFTER_GAP, S_ALONE_BEFORE_HEADER, S_ALONE_STYLED, S_ARITHMETIC_TIE, S_AUTOFILTER_TOP, S_BELOW_TABLE, S_FIGURES, S_FORMULA_CLOSES_BLOCK,
                                S_FORMULA_COMPUTED, S_FORMULA_TOTAL, S_FROZEN_LAST, S_JOINED_ABOVE, S_JOINED_BELOW, S_LABEL_VALUE, S_MERGED_OVER_COLUMNS, S_MODEL, S_NAMES_BODY, S_TEXT_LINE, S_TOTAL_WORD, S_TOTAL_WORD_IN_KEY, TITLE, TOTAL)

_WORD_ON_MOST_LINES = 8


def words_in_keys(facts: dict[int, LineFacts], worded: list[int]) -> set[int]:
    """When the total word sits on most figure lines it may be no mark at all: a pasted pivot's records all end in it. Such a line is a record only when the word is inside the record's own key and its figures add up nothing above them. A grand total, a word beside a blank key (an invoice's "Ticket Total") and a line that ties keep their printed role."""
    if len(worded) < _WORD_ON_MOST_LINES or len(worded) * 2 < sum(1 for f in facts.values() if f.number > 0):
        return set()
    held = set(worded)
    others = Counter(f.first_col for r, f in facts.items() if f.number > 0 and r not in held)
    starts = others or Counter(facts[r].first_col for r in worded)
    key_col = min(starts, key=lambda c: (-starts[c], c))
    return {r for r in worded if facts[r].word_col == key_col and not facts[r].grand and not facts[r].word_ties}


def _printed_signals(facts: dict[int, LineFacts]) -> dict[int, list[str]]:
    """Printed total and computed lines, in rule order: a total word beside a figure, a sum of its own column on most of its figures, a computed line closing a block, a figure line that adds up and is set apart by style."""
    printed: dict[int, list[str]] = {}
    for r, f in facts.items():
        if (f.number and not f.real_figures) or f.sequence:
            continue
        signals = []
        if f.total_word and f.number > 0:
            signals.append(S_TOTAL_WORD)
        if f.formula_major:
            signals.append(S_FORMULA_TOTAL)
        elif f.formula_line and (f.gap_above or f.gap_below or f.bold >= 0.5):
            signals.append(S_FORMULA_COMPUTED)
        elif f.agg_column and f.gap_below:
            signals.append(S_FORMULA_CLOSES_BLOCK)
        if f.arith_ties >= 2 and f.arith_ties * 2 >= f.number and (f.bold_prev or f.fill_prev):
            signals.append(S_ARITHMETIC_TIE)
        if signals:
            printed[r] = signals
    return printed


def _words_of(facts: dict[int, LineFacts], signals: dict[int, list[str]]) -> set[int]:
    return words_in_keys(facts, [r for r, s in signals.items() if s == [S_TOTAL_WORD]])


def printed_lines(facts: dict[int, LineFacts]) -> dict[int, list[str]]:
    """The printed lines and their signals, the words that sit inside record keys left out."""
    signals = _printed_signals(facts)
    words = _words_of(facts, signals)
    return {r: s for r, s in signals.items() if r not in words}


def _name_line(f: LineFacts) -> bool:
    """A line that could sit in a header block: labels, no sentence, no figure beyond a few among many labels."""
    if f.wordy > 1 or f.longest > 60 or (f.filled <= 2 and (f.label_value or f.date)):
        return False
    labels = f.text + f.date + f.period + (f.number - f.real_figures)
    if labels < 1 or f.real_figures * 4 > labels:
        return False
    return f.filled >= 2 or f.wide_merge >= 2


_MOST_LINES_ABOVE = 4
_TITLE_ZONE = 15


def grow_blocks(heads: dict[int, list[str]], facts: dict[int, LineFacts], printed: dict[int, list[str]]) -> dict[int, list[str]]:
    """A header block grows from the lines named: down over adjacent name lines standing over typed cells, up over short name lines and merged group labels."""
    heads = dict(heads)
    usable = {r: f for r, f in facts.items() if r not in printed and _name_line(f)}
    for r in sorted(heads):
        y = facts[r].next
        while y is not None and y - r <= 2 and y in usable and y not in heads and facts[y].real_figures == 0 and facts[y].over_figures >= 0.3 and facts[y].typed_below >= 0.5:
            heads[y] = [S_JOINED_BELOW]
            r, y = y, facts[y].next
    for r in sorted(heads):
        x = facts[r].prev
        lines = 0
        while x is not None and r - x <= 2 and x not in printed and x not in heads and lines < _MOST_LINES_ABOVE:
            f = facts[x]
            over_some = f.wide_merge >= 2 and f.wide_merge < 0.8 * max(1, facts[r].last_col - facts[r].first_col + 1)
            if not (x in usable and f.filled >= 2 and f.longest <= 40) and not (f.filled == 1 and over_some):
                break
            # A styled header is joined by lines styled like it: the plain lines above a repeated styled header are the last table's records.
            if (facts[r].bold >= 0.5 and f.bold < 0.5) or (facts[r].fill >= 0.5 and f.fill < 0.5):
                break
            heads[x] = [S_MERGED_OVER_COLUMNS if f.filled == 1 else S_JOINED_ABOVE]
            lines += 1
            r, x = x, f.prev
    return heads


def _fills_table(grid: Grid, r: int, f: LineFacts, heads: dict[int, list[str]]) -> bool:
    """A line standing under the columns of the header above it, and filling enough of them, is a record short of figures, not a note."""
    above = [h for h in heads if h < r]
    if not above or f.filled < 2 or f.label_value:
        return False
    named = grid.rows[max(above)]
    if not all(c in named for c in grid.rows[r]):
        return False
    return f.filled >= 3 or (f.filled * 2 >= len(named) and not f.wordy and f.longest <= 60)


def assign_roles(grid: Grid, facts: dict[int, LineFacts], heads: dict[int, list[str]], printed: dict[int, list[str]], words_in_key: set[int] = frozenset()) -> dict[int, tuple[str, tuple[str, ...]]]:
    """Every line's role given its header and printed lines: bands, notes, titles and records follow from where each line sits."""
    rows = sorted(facts)
    first_head = min(heads) if heads else None
    # Figures are counted from the first header down: a dated title is no figure of the table under it.
    last_figures = None
    for r in rows:
        if (first_head is None or r > first_head) and r not in heads and r not in printed and facts[r].number + facts[r].date > 0:
            last_figures = r
    roles: dict[int, tuple[str, tuple[str, ...]]] = {}
    for r in rows:
        f = facts[r]
        if r in words_in_key:
            roles[r] = (RECORD, (S_TOTAL_WORD_IN_KEY,))
            continue
        if r in heads:
            roles[r] = (HEADER, tuple(dict.fromkeys(heads[r])))
            continue
        if r in printed:
            roles[r] = (TOTAL, tuple(printed[r]))
            continue
        styled = f.bold >= 0.5 or f.fill >= 0.5 or f.under >= 0.5 or f.wide_merge > 1
        alone = f.filled == 1 and f.text == 1 and f.longest <= 60
        figures = f.number + f.date
        if first_head is None:
            if figures == 0 and f.label_value:
                roles[r] = (NOTE, (S_LABEL_VALUE,))
            elif figures == 0 and f.filled <= 2 and r < rows[min(3, len(rows) - 1)] and (last_figures is not None or r < rows[min(1, len(rows) - 1)]):
                roles[r] = (TITLE, (S_ABOVE_TABLE,))
            elif alone and styled:
                roles[r] = (BAND, (S_ALONE_STYLED,))
            else:
                roles[r] = (RECORD, (S_FIGURES if figures else S_TEXT_LINE,))
            continue
        if r < first_head:
            # A line above the first header holding figures, or filling the columns under it, is a record of a table it has no name for, not a title.
            if not f.label_value and ((f.number >= 1 and f.filled >= 2) or (f.filled >= 3 and f.overlap_below >= 0.5)):
                roles[r] = (RECORD, (S_FIGURES if f.number else S_TEXT_LINE,))
            elif alone and styled and r > rows[0] and facts[f.prev].filled >= 3:
                roles[r] = (BAND, (S_ALONE_STYLED,))
            elif f.label_value:
                roles[r] = (NOTE, (S_LABEL_VALUE,))
            elif rows.index(r) < _TITLE_ZONE:
                roles[r] = (TITLE, (S_ABOVE_TABLE,))
            else:
                # Far from the top, lines above a late header are no title: a list with no header of its own.
                roles[r] = (RECORD, (S_FIGURES if f.number else S_TEXT_LINE,))
            continue
        before_head = f.next in heads
        short_band = f.filled <= 3 and figures == 0 and f.longest <= 60 and styled
        if (alone or short_band) and last_figures is not None and r < last_figures:
            following = facts[f.next] if f.next is not None else None
            if not alone or styled:
                roles[r] = (BAND, (S_ALONE_STYLED,))
            elif before_head:
                roles[r] = (BAND, (S_ALONE_BEFORE_HEADER,))
            elif f.gap_above and following is not None and following.filled >= 2:
                roles[r] = (BAND, (S_ALONE_AFTER_GAP,))
            else:
                roles[r] = (RECORD, (S_TEXT_LINE,))
            continue
        if last_figures is not None and r > last_figures and figures == 0 and (f.filled <= 2 or f.wordy) and not _fills_table(grid, r, f, heads):
            roles[r] = (NOTE, (S_LABEL_VALUE,) if f.label_value else (S_BELOW_TABLE,))
            continue
        roles[r] = (RECORD, (S_FIGURES if figures else S_TEXT_LINE,))
    return roles


def _name_unnamed_bodies(heads: dict[int, list[str]], facts: dict[int, LineFacts], printed: dict[int, list[str]]) -> None:
    """A body of figures with no header over it takes the name line directly above it, when that line starts its block: a small clean table needs no further evidence."""
    rows = sorted(facts)
    above_is_body = False
    for at, r in enumerate(rows):
        f = facts[r]
        typed = f.real_figures + f.date >= 1 and r not in printed and f.filled >= 2
        if typed and not above_is_body and at and at + 1 < len(rows):
            x = rows[at - 1]
            # A band may sit between a header and its first line of figures.
            if facts[x].filled == 1 and x not in heads and at >= 2:
                x = rows[at - 2]
            more = any(facts[y].real_figures + facts[y].date >= 1 for y in rows[at + 1:at + 6])
            if x not in heads and x not in printed and r - x <= 3 and _name_line(facts[x]) and facts[x].real_figures == 0 and facts[x].over_figures >= 0.3 and more:
                above = facts[x].prev
                if above is None or x - above > 1 or above in printed or above in heads or facts[above].filled == 1:
                    heads[x] = [S_NAMES_BODY]
        above_is_body = typed or (above_is_body and r in printed)


def repeats_line(here: dict, there: dict) -> bool:
    """Most of a line's cells say what another line says in the same columns."""
    same = sum(1 for c, cell in here.items() if c in there and str(there[c].value).lower() == str(cell.value).lower())
    return same >= max(2, len(here) * 0.6)


def _clear_filter_range(heads: dict[int, list[str]], grid: Grid) -> None:
    """The autofilter names one table: nothing inside its range is a header, unless it repeats the filter's own top line (a printed page's header kept inside the range)."""
    if not grid.tab.autofilter:
        return
    top, _, bottom, _ = grid.tab.autofilter
    named = grid.rows.get(top, {})
    for r in sorted(heads):
        if top < r <= bottom and not repeats_line(grid.rows[r], named):
            del heads[r]


def header_lines(grid: Grid, facts: dict[int, LineFacts], printed: dict[int, list[str]]) -> dict[int, list[str]]:
    """Header lines with their signals. The autofilter's top line is a header outright (right on 16 of 16 tabs); the line model decides the rest (line_model.py, a sum over named features), and each block then grows over its name lines."""
    tab = grid.tab
    features = line_features(facts, grid)
    heads: dict[int, list[str]] = {}
    top = tab.autofilter[0] if tab.autofilter else None
    if top in facts:
        heads[top] = [S_AUTOFILTER_TOP]
    model = load_model()
    for r in sorted(facts):
        if r not in printed and header_score(features[r], model) >= model["threshold"]:
            heads.setdefault(r, []).append(S_MODEL)
    if tab.frozen_rows in heads:
        heads[tab.frozen_rows].append(S_FROZEN_LAST)
    _name_unnamed_bodies(heads, facts, printed)
    heads = grow_blocks(heads, facts, printed)
    _clear_filter_range(heads, grid)
    return heads


def read_roles(grid: Grid) -> dict[int, tuple[str, tuple[str, ...]]]:
    """Each filled line's role and the signals behind it, in the evidence's order: printed lines by rule, header lines by the line model, the rest by where each line sits. Printed lines come back as TOTAL; the compiler tells subtotals from totals once it knows the table."""
    facts = line_facts(grid)
    if not facts:
        return {}
    signals = _printed_signals(facts)
    words = _words_of(facts, signals)
    printed = {r: s for r, s in signals.items() if r not in words}
    heads = {r: s for r, s in header_lines(grid, facts, printed).items() if r not in words}
    return assign_roles(grid, facts, heads, printed, words)
