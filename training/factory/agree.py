"""Do two answers agree? One a program's rows through engine/program, the other a Python program's answer over raw cells. Stdlib only.

A figure agrees to one part in a million, or as a share stated either way. A name agrees folded; a peer column's joined name holds the header the other route read. A rank's key must be among the other's, or its figure must be the other's whole answer, since "highest charge" may ask for the charge; a list must be the same set; a figure beside a name is not required.
"""
from __future__ import annotations

import re

_NUM = re.compile(r"^\(?-?\$?[\d,]*\.?\d+\)?%?$")


def fold(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def as_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not _NUM.match(text):
        return None
    negative = text.startswith("(") or text.startswith("-")
    percent = text.endswith("%")
    digits = text.strip("()%-$").replace(",", "").replace("$", "")
    try:
        number = float(digits)
    except ValueError:
        return None
    number = number / 100 if percent else number
    return -number if negative else number


_SLASH_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")


def as_day(value: object) -> str | None:
    """A written day as ISO: 2001-02-03 or 2/3/2001, US order as these sheets write it."""
    text = " ".join(str(value).split())
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    match = _SLASH_DATE.match(text)
    if match:
        year = match[3] if len(match[3]) == 4 else ("20" if int(match[3]) < 50 else "19") + match[3]
        return f"{year}-{int(match[1]):02d}-{int(match[2]):02d}"
    return None


def same_number(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(b) * 1e-6, 1e-9)


def same_scalar(a: object, b: object) -> bool:
    x, y = as_number(a), as_number(b)
    if x is not None and y is not None:
        return same_number(x, y) or same_number(x, y * 100) or same_number(x * 100, y)
    if x is not None or y is not None:
        return False
    day_a, day_b = as_day(a), as_day(b)
    if day_a is not None and day_b is not None:
        return day_a == day_b
    return fold(a) == fold(b)


def flatten(value: object) -> list:
    if isinstance(value, dict):
        out = []
        for key, item in value.items():
            out.append(key)
            out.extend(flatten(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(flatten(item))
        return out
    return [value]


def _set_key(value: object) -> object:
    number = as_number(value)
    if number is not None:
        return round(number, 9)
    return as_day(value) or fold(value)


def agrees(form: str, listing: bool, rows: list, answer: object, peer: bool = False) -> bool:
    """rows: the program's result rows. form: show, agg, rank or calc. For rank a row's last cell is its value, except a line rank showing columns, where every shown cell counts as a name."""
    if not rows:
        return False
    got = [item for item in flatten(answer) if item not in (None, "")]
    if not got:
        return False
    if listing:
        return {_set_key(row[0]) for row in rows} == {_set_key(item) for item in got}
    if form in ("agg", "calc"):
        value = rows[0][0]
        return any(same_scalar(value, item) for item in got) and len(got) <= 3
    if form == "show":
        wanted = [cell for cell in rows[0] if cell not in (None, "")]
        return bool(wanted) and all(any(same_scalar(cell, item) for item in got) for cell in wanted)
    # rank: the names, each tied row's, against what came back; or the top figure alone, when that is all that came back.
    if len(got) == 1 and as_number(got[0]) not in (None, 0.0) and as_number(rows[0][-1]) is not None and same_number(as_number(got[0]), as_number(rows[0][-1])):
        return True
    names = [[cell for cell in row[:-1] if cell not in (None, "")] for row in rows]
    if not any(names):
        return False
    if peer:
        return any(fold(item) and fold(item) in fold(row[0]) for row in names if row for item in got if isinstance(item, str) and len(fold(item)) >= 2)
    return any(all(any(same_scalar(cell, item) for item in got) for cell in row) for row in names if row)


__all__ = ["agrees", "as_number", "flatten", "fold", "same_scalar"]
