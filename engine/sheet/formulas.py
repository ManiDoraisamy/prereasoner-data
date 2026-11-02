"""The shape of a formula, read from its text at its own address: one named shape from engine/sheet/names.py.

A total line is mostly aggregates of its own column; a total column, aggregates of its own row; a derived column, arithmetic on its own row. A form repeating down a column (a running
balance, =A5+1) is a series, not a total: mark_runs tells them apart once every formula of a tab is read. Only "total" is ever a word here; the rest is the formula's own structure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from engine.sheet.names import (FX_AGG_COLUMN, FX_AGG_OTHER, FX_AGG_ROW, FX_AGG_RUN, FX_COLUMN_ARITH, FX_COLUMN_RUN, FX_COND_AGG, FX_CONST, FX_DATE, FX_EXTERNAL, FX_IF, FX_LINK,
                                FX_LINK_COLUMN, FX_LOOKUP, FX_MIXED, FX_OTHER_TAB, FX_ROW_ARITH, FX_TEXT, FX_VOLATILE)

_STRINGS = re.compile(r'"[^"]*"')
_REF = re.compile(r"(?<![A-Za-z0-9_.$!\]'])(?:(?P<sheet>'[^']+'|(?:\[[^\]]*\])?[A-Za-z0-9_.]+)!)?(?P<c1>\$?[A-Za-z]{1,3})(?P<r1>\$?\d{1,7})(?::(?P<c2>\$?[A-Za-z]{1,3})(?P<r2>\$?\d{1,7}))?(?![A-Za-z0-9_(.])")
_WHOLE_COLUMN = re.compile(r"(?<![A-Za-z0-9_.$!])\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}(?![A-Za-z0-9_(])")
_FUNCTION = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\(")
_ONE_REF = re.compile(r"^(?:(?:'[^']+'|(?:\[[^\]]*\])?[A-Za-z0-9_.]+)!)?\$?[A-Za-z]{1,3}\$?\d{1,7}(?::\$?[A-Za-z]{1,3}\$?\d{1,7})?$")
_CALL = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\((.*)\)$")
_ADDERS = frozenset({"SUM", "SUBTOTAL", "AGGREGATE"})
_AGG = _ADDERS | {"AVERAGE", "AVERAGEA", "COUNT", "COUNTA", "MIN", "MAX", "MEDIAN", "PRODUCT", "STDEV", "STDEVP", "VAR"}
_COND_AGG = frozenset({"SUMIF", "SUMIFS", "COUNTIF", "COUNTIFS", "AVERAGEIF", "AVERAGEIFS", "SUMPRODUCT", "DSUM", "DCOUNT", "DCOUNTA", "DAVERAGE", "DMAX", "DMIN", "DGET", "FREQUENCY"})
_LOOKUP = frozenset({"VLOOKUP", "HLOOKUP", "LOOKUP", "INDEX", "MATCH", "CHOOSE", "OFFSET", "INDIRECT", "GETPIVOTDATA"})
_VOLATILE_FN = frozenset({"TODAY", "NOW", "RAND", "RANDBETWEEN"})
_DATE_FN = frozenset({"DATE", "EOMONTH", "EDATE", "YEAR", "MONTH", "DAY", "WEEKDAY", "DATEVALUE", "NETWORKDAYS", "WORKDAY", "HOUR", "MINUTE", "TIME", "WEEKNUM"})
_TEXT_FN = frozenset({"CONCATENATE", "LEFT", "RIGHT", "MID", "TEXT", "TRIM", "UPPER", "LOWER", "PROPER", "LEN", "FIND", "SEARCH", "SUBSTITUTE", "REPLACE", "VALUE", "T", "CLEAN", "REPT"})


@dataclass(frozen=True, slots=True)
class FormulaShape:
    """kind is a formula shape. span, for an aggregate of a column or row, is the (first, last) row or column it reads; stat: an aggregate that does not add (AVERAGE, MAX)."""
    kind: str
    span: tuple[int, int] | None = None
    stat: bool = False
    broken: bool = False


def column_number(letters: str) -> int:
    n = 0
    for ch in letters.replace("$", "").upper():
        n = n * 26 + ord(ch) - 64
    return n


def column_letter(column: int) -> str:
    """1 -> A, 27 -> AA."""
    text = ""
    while column > 0:
        column, rest = divmod(column - 1, 26)
        text = chr(65 + rest) + text
    return text


def _refs(body: str) -> list[tuple[str | None, int, int, int, int]]:
    out = []
    for m in _REF.finditer(body):
        c1 = column_number(m["c1"])
        r1 = int(m["r1"].replace("$", ""))
        c2 = column_number(m["c2"]) if m["c2"] else c1
        r2 = int(m["r2"].replace("$", "")) if m["r2"] else r1
        out.append((m["sheet"], min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)))
    return out


def _strip_outer(expr: str) -> str:
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, ch in enumerate(expr):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(expr) - 1:
                return expr
        expr = expr[1:-1]
    return expr


def _terms(expr: str) -> list[tuple[str, str]] | None:
    """Top-level terms joined by + and -; None if another operator sits at the top level."""
    terms: list[tuple[str, str]] = []
    depth, start, sign = 0, 0, "+"
    for i, ch in enumerate(expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and ch in "+-":
            if i == start:
                if ch == "-":
                    sign = "-" if sign == "+" else "+"
                start = i + 1
            else:
                terms.append((sign, expr[start:i]))
                sign, start = ch, i + 1
        elif depth == 0 and ch in "*/^&<>=":
            return None
    if depth != 0:
        return None
    if start < len(expr):
        terms.append((sign, expr[start:]))
    return terms


def _args(text: str) -> list[str]:
    args, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            args.append(text[start:i])
            start = i + 1
    args.append(text[start:])
    return [a.strip() for a in args]


def _additive(expr: str, depth: int = 0) -> dict | None:
    """When the expression is a sum of references and aggregate calls: its functions, its count of bare references and whether anything is subtracted."""
    if depth > 6:
        return None
    terms = _terms(_strip_outer(expr))
    if not terms:
        return None
    fns: list[str] = []
    plain, minus = 0, False
    for sign, term in terms:
        term = _strip_outer(term)
        minus = minus or sign == "-"
        if _ONE_REF.match(term):
            plain += 1
            continue
        m = _CALL.match(term)
        if m is not None and m[2].count("(") == m[2].count(")"):
            name = m[1].upper()
            args = _args(m[2])
            if name in _AGG:
                if name in ("SUBTOTAL", "AGGREGATE") and args and args[0].isdigit():
                    args = args[1:]
                if args and all(_ONE_REF.match(_strip_outer(a)) for a in args):
                    fns.append(name)
                    continue
                return None
            if name == "ROUND" and len(args) == 2 and (inner := _additive(args[0], depth + 1)):
                fns += inner["fns"]
                plain += inner["plain"]
                minus = minus or inner["minus"]
                continue
            return None
        inner = _additive(term, depth + 1) if term.startswith("(") else None
        if inner is None:
            return None
        fns += inner["fns"]
        plain += inner["plain"]
        minus = minus or inner["minus"]
    return {"fns": fns, "plain": plain, "minus": minus}


def classify(formula: str, row: int, column: int) -> FormulaShape:
    """The named shape of a formula at its address."""
    body = re.sub(r"\s+", "", _STRINGS.sub('""', formula)).lstrip("=").lstrip("+")
    broken = "#REF!" in body
    if "[" in body:
        return FormulaShape(FX_EXTERNAL, broken=broken)
    fns = {f.upper() for f in _FUNCTION.findall(body)}
    if fns & _VOLATILE_FN:
        return FormulaShape(FX_VOLATILE, broken=broken)
    refs = _refs(body)
    qualified = any(x[0] for x in refs)
    if _ONE_REF.match(body):
        if not qualified and refs[0][1] == column and refs[0][3] == column:
            return FormulaShape(FX_LINK_COLUMN, broken=broken)
        return FormulaShape(FX_OTHER_TAB if qualified else FX_LINK, broken=broken)
    if not refs and _WHOLE_COLUMN.search(body):
        return FormulaShape(FX_COND_AGG if fns & _COND_AGG else FX_AGG_OTHER if fns & _AGG else FX_MIXED, broken=broken)
    shape = _additive(body)
    if shape and refs and (shape["fns"] or shape["plain"] >= 2):
        if qualified:
            return FormulaShape(FX_OTHER_TAB, broken=broken)
        same_column = all(x[1] == column and x[3] == column for x in refs)
        same_row = all(x[2] == row and x[4] == row for x in refs)
        inside = any(x[1] <= column <= x[3] and x[2] <= row <= x[4] for x in refs)
        if shape["minus"] and not shape["fns"]:
            return FormulaShape(FX_ROW_ARITH if same_row else FX_COLUMN_ARITH if same_column else FX_MIXED, broken=broken)
        if same_column and not same_row and not inside:
            first, last = min(x[2] for x in refs), max(x[4] for x in refs)
            # =SUM(D16) far from D16 only reads one cell: a link. Right above or below it is the subtotal of one record.
            if first == last and abs(first - row) > 2:
                return FormulaShape(FX_LINK_COLUMN, broken=broken)
            return FormulaShape(FX_AGG_COLUMN, span=(first, last), stat=bool(shape["fns"]) and not set(shape["fns"]) & _ADDERS, broken=broken)
        if same_row and not same_column and not inside:
            return FormulaShape(FX_AGG_ROW, span=(min(x[1] for x in refs), max(x[3] for x in refs)), broken=broken)
        if shape["fns"]:
            return FormulaShape(FX_AGG_OTHER, broken=broken)
    if qualified:
        return FormulaShape(FX_OTHER_TAB, broken=broken)
    if fns & _COND_AGG:
        return FormulaShape(FX_COND_AGG, broken=broken)
    if fns & _LOOKUP:
        return FormulaShape(FX_LOOKUP, broken=broken)
    if fns & {"IF", "ISERROR", "IFERROR"}:
        return FormulaShape(FX_IF, broken=broken)
    if fns & _TEXT_FN or "&" in body:
        return FormulaShape(FX_TEXT, broken=broken)
    if fns & _DATE_FN:
        return FormulaShape(FX_DATE, broken=broken)
    if fns & _AGG:
        return FormulaShape(FX_AGG_OTHER, broken=broken)
    if not refs:
        return FormulaShape(FX_CONST, broken=broken)
    if all(x[2] == row and x[4] == row for x in refs):
        return FormulaShape(FX_ROW_ARITH, broken=broken)
    if all(x[1] == column and x[3] == column for x in refs):
        return FormulaShape(FX_COLUMN_ARITH, broken=broken)
    return FormulaShape(FX_MIXED, broken=broken)


def relative_form(formula: str, row: int, column: int) -> str:
    """A formula with its relative references as offsets, so a column of =C5*D5, =C6*D6 reads as one form."""
    def one(m: re.Match) -> str:
        def part(col: str, line: str) -> str:
            r = f"R{line[1:]}" if line.startswith("$") else f"R[{int(line) - row}]"
            return r + (f"C{column_number(col)}" if col.startswith("$") else f"C[{column_number(col) - column}]")

        out = f"{m['sheet']}!" if m["sheet"] else ""
        out += part(m["c1"], m["r1"])
        return out + (":" + part(m["c2"], m["r2"]) if m["c2"] else "")

    return _REF.sub(one, _STRINGS.sub('""', formula))


def mark_runs(shapes: dict[tuple[int, int], FormulaShape], formulas: dict[tuple[int, int], str]) -> dict[tuple[int, int], FormulaShape]:
    """An aggregate or a column step whose form repeats on the next or previous line of its column is a running series, not a total line."""
    candidates = {at: relative_form(formulas[at], *at) for at, s in shapes.items() if s.kind in (FX_AGG_COLUMN, FX_COLUMN_ARITH, FX_LINK_COLUMN)}
    out = dict(shapes)
    for (r, c), form in candidates.items():
        if candidates.get((r - 1, c)) == form or candidates.get((r + 1, c)) == form:
            old = shapes[(r, c)]
            out[(r, c)] = FormulaShape(FX_AGG_RUN if old.kind == FX_AGG_COLUMN else FX_COLUMN_RUN, span=old.span, stat=old.stat, broken=old.broken)
    return out
