"""What Gemini is asked, and the per-tab plan of question shapes and styles. The plan comes from what the compiled tab holds, seeded per tab, so rare forms appear wherever a tab can carry them. A style or a word rule here is a request; scripts check every one afterwards.

Three routes, none sees another's work: the author (tab and compiled tables), the rewriter (a question and the words to avoid), the second route (raw cells and the questions only).
"""
from __future__ import annotations

import hashlib
import random


PROMPT_VERSION = "f7"

COMMON = {
    "sum_filter": "agg sum, one filter", "sum_all": "agg sum over the whole table", "avg": "agg avg, one filter", "minmax": "agg min or max", "count_filter": "agg count, one filter", "count_all": "agg count of the lines",
    "rank_group": "rank group: the key adding up highest or lowest", "rank_line": "rank line: the line with the top or bottom figure", "show": "show: read one labelled cell", "list": "list: the values meeting a condition",
    "calc_diff": "calc: the difference of two filtered figures", "two_filters": "agg sum or count, two filters",
}
RARE = {
    "peer": ("peers", "rank peer: which of the axis's columns is top"), "band": ("bands", "any form with a band condition"), "printed": ("printed", "show reading a printed total or subtotal line, with line="),
    "period": ("dates", "agg with a period condition"), "latest": ("dates", "rank line by date(col): the latest or earliest"), "row_arith": ("two_numbers", "agg of two columns of one line, as 3-4 or 3*4"),
    "size": ("numbers", "rank or agg by size, abs() or size"), "compare": ("numbers", "any form with a number comparison"), "compare_cols": ("two_numbers", "any form comparing two columns of a line, as 3>c4"),
    "calc_ratio": ("two_numbers", "calc: one filtered figure over another, a share or ratio, with /"), "lowest": ("numbers", "rank group or line, the lowest"), "minimum": ("numbers", "agg min"), "blank": ("texts", "agg count or list with a blank or filled condition"), "contains": ("texts", "any form with a part-of-cell condition ~"), "countd": ("texts", "agg countd of a text column"),
    "notes": ("notes", "show reading a label: value note table"), "family": ("families", "agg over the family table across its tabs"),
}
NO_NUMBER_COMMON = ("count_filter", "count_all", "show", "list", "rank_group_count")
NO_FILTER_SHAPES = ("sum_all", "count_all", "minmax", "rank_group", "rank_line", "peer")

AUTHOR_SYSTEM = """You write training data for a question box over real working spreadsheets. You see one tab of a company's workbook as an analyst sees it, then its compiled tables by id and position.

Decide who uses this tab day to day. For each slot write what that person would really type, then the program that answers it.

Reply with lines only, nothing else, in this order:
ROLE <2 to 4 words>
COL <table>.<position> <name> <1-9> [val <name> <1-9>]    one per column a question could read; peers once, as <table>.a0. name says what the column means: the schema.org property whose values it holds, as schema:property, or none. 1-9 is how sure. val names what its text values are when that adds something.
Q<slot>|<question>|<program>|<links>
R<n>|<question>|<why>    a question this tab cannot answer, which that person might well type

Question rules. Typed fast: fragments are normal, no please, rarely a question mark, plain trade words, sometimes a slip. Say things as the person would, not as the header does: never copy a header's words if another word will do, and shorten or half-name values as people do. The question must still ask exactly what the program does: it states every filter the program uses and no others, unless the tab itself makes one obvious.
Never name a column by its letter or position, nor a row by number. Style F: at most 6 words. Style S: at most 6 words, no filter, naming only what the sheet is about, not a column's header or a value, as "biggest short" or "total for the month"; its words link to C. Style L: a normal sentence.

Programs:
agg sum 3 @t0 where 1="EES", 0 in 2001-02    agg sum|avg|min|max|count|countd; "agg count @t0"; "agg countd 2 @t0"; measure 3, 3-4, 3+4, 3*4, 3/4, abs(3), abs(3-4); add mag to state a signed result by its size
show 5 @t0 where 1="X"    the cell on the first line matching; show 5,6 for two cells; list 2 @t0 where 3>=36  the distinct values; list band @t0
rank line high 3 show 1 @t0 where ...    rank group high sum 3 by 1 @t0    rank group low count by 1 @t0    rank line high date(0) show 1 @t0    rank peer high a0 @t0    add size to rank a group or peer by size. Use a0 only in rank peer. A ranked column cannot also be the one shown.
calc (agg sum 3 @t0 where 1="A") - (agg sum 3 @t0 where 1="B")    operators - + /; each side gives one number
Conditions, comma separated: 1="X" 1=5 | 1~"X" holds, 1^"X" starts, 1$"X" ends | 0 in 2001-02 (2002, 2001-02, 2001-11-29, m02 any year's month, 2001-q4, 2001-01-01..2001-03-31) | 3>=36 > <= < != on numbers only | 3>c4 another column | 1 blank, 1 filled | band="X" band~"X" | line="X" line="X" total line total line subtotal
Fixed meanings: agg, rank and list read data lines only, never printed totals, unless a line= condition is used. Case and spacing do not matter. Blanks are skipped. Copy cell text exactly in conditions. Use only table ids and positions you are shown. Prefer a tab's own table to a family.

Links: which of the question's words point at what, separated by ; as words>ref. ref: 3 a column | V3 a value of column 3 | L a printed line's label | B a band | C the tab, title or subject | P0 a period on column 0 | N3 a number compared on column 3 | A0 an axis. Words like total, how many, top need no link. In a calc or other-table read, prefix the table: t1:3."""

REWRITE_SYSTEM = """You rewrite questions typed into a question box over a spreadsheet, so they stop using the words of the sheet's headers. Keep the exact meaning: same figure, same filters. Use the word a person in that trade would use, a shorthand, or name the thing another way; a value may stay as it is. At most 6 words unless a slot says L. Reply with lines only: <id>|<new question>|<links>. Links: words>ref separated by ; refs as given beside each question: a number is a column, V3 a value of column 3, L a printed line, B a band, C the sheet's subject, P0 a period, N3 a number, A0 an axis. If you cannot avoid a word, still give your best."""

ROUTE_SYSTEM = """You answer questions typed about a working spreadsheet by writing one short Python program per question over its raw cells. You see the tab's first and last rows with row numbers and the values of its low-cardinality columns. The tabs are real files: title rows, several tables, section bands, subtotals, totals and months across columns.

T is this tab's rows as a list of lists: sheet row 1 is T[0], columns 0-based, every row as wide as the tab, a blank cell is ''. Date cells are 'YYYY-MM-DD' text. R('other tab') reads another tab. Helpers: num(v) a float for a number or numeric text, else None; s(v) stripped text; mean, median, stdev, defaultdict, Counter, re_search, re_sub. Built-in functions only, no imports. The program's last expression is the answer itself: a number; a name; [name, figure] when asked which line or key is top; a list of names for a list. Do not add up printed totals with their lines. A question may be typed in fragments or shorthand; read it as a person in the trade would.

Reply with lines only, one per question: <id>|<one line of Python; ; between statements, comprehensions rather than loops>
If the cells cannot answer it: <id>|ASK <a few words on what is missing or unclear>"""


def _seed(book: str, tab: str) -> int:
    return int.from_bytes(hashlib.blake2b(f"{book}|{tab}".encode("utf-8", "ignore"), digest_size=8).digest(), "little")


def plan_slots(features: dict, book: str, tab: str) -> list[tuple[str, str]]:
    """The tab's slots: (style, shape text), rare shapes first where the tab can carry them, up to about half, then the common ones."""
    rng = random.Random(_seed(book, tab))
    numbers, texts = features["numbers"], features["texts"]
    usable = min(features["columns"], 24)
    count = 4 if features["lines"] < 5 or usable < 2 else max(5, min(14, 5 + usable // 2))
    have = {"peers": features["peers"], "bands": features["bands"], "printed": features["printed"], "dates": features["dates"], "numbers": numbers, "two_numbers": numbers >= 2, "texts": texts, "notes": features["notes"], "families": features["families"]}
    rare = [name for name, (need, _) in RARE.items() if have.get(need)]
    rng.shuffle(rare)
    rare = rare[:max(2, (count * 5) // 10)]
    if numbers:
        common = ["sum_all", "count_filter", "sum_filter", "rank_group", "show", "rank_line", "calc_diff", "two_filters", "avg", "minmax", "list", "count_all", "sum_filter", "rank_group"]
    else:
        common = ["count_filter", "show", "list", "count_all", "show", "list", "count_filter", "count_all"]
    chosen = rare + [common[at % len(common)] for at in range(max(0, count - len(rare)))]
    chosen = chosen[:count]
    rng.shuffle(chosen)
    slots = []
    for shape in chosen:
        text = COMMON.get(shape) or RARE[shape][1]
        roll = rng.random()
        if shape in NO_FILTER_SHAPES and roll < 0.6:
            style = "S"
        elif roll < 0.66:
            style = "F"
        else:
            style = "L"
        slots.append((style, text))
    return slots


def refusals_for(slots: list) -> int:
    return 2 if len(slots) >= 9 else 1


def author_prompt(record: dict) -> tuple[str, list[tuple[str, str]]]:
    slots = plan_slots(record["features"], record["book"], record["tab"])
    lines = "\n".join(f"{at} {style}: {text}" for at, (style, text) in enumerate(slots, 1))
    prompt = f"{record['rows_view']}\n\nCompiled tables:\n{record['slots_view']}\n\nSlots:\n{lines}\nThen {refusals_for(slots)} R line(s)."
    return prompt, slots


def author_max_output(slots: list) -> int:
    return 2500 + 170 * len(slots)


__all__ = ["AUTHOR_SYSTEM", "PROMPT_VERSION", "REWRITE_SYSTEM", "ROUTE_SYSTEM", "author_max_output", "author_prompt", "plan_slots", "refusals_for"]
