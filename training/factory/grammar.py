"""The program as Gemini writes it: a short line of text over a table's positions, bound here to engine/program's typed forms. Slots over the compiled model, nothing more. Text that does not parse, or names a position the table lacks, raises Unbound with a reason.

  agg sum 3 @t0 where 1="EES", 0 in 2001-02      agg sum|avg|min|max|count|countd; count takes no measure; a trailing "mag" states the result by its size
  agg sum 3-4 @t0        measure: 3 | 3+4 3-4 3*4 3/4 | abs(3) | abs(3-4) | date(0)
  show 5 @t0 where 1="X"     show 5,6 @t0 ...     list 2 @t0 where 3>=36     list band @t0
  rank line high 3 show 1 @t0 where ...       rank line low abs(3) show 1,2 @t0
  rank group high sum 3 by 1 @t0 size         rank group high count by 1 @t0      by band works too
  rank peer high a0 @t0 size                  rank peer high 3,4,5 @t0 mark "X"
  calc (agg sum 3 @t0 where 1="A") - (agg sum 3 @t0 where 1="B")     - + /, a trailing "mag" states it by size
Conditions, comma separated: 1="X" 1=5 | 1~"X" holds, 1^"X" starts, 1$"X" ends | 0 in 2001-02 (2002, 2001-02, 2001-11-29, m02 a month of any year, 2001-q4, a..b) | 3>=36 > <= < != | 3>c4 another column | 1 blank, 1 filled, 1 isnum | band="X" band~"X" | line="X" line="X" total line total line subtotal
"""
from __future__ import annotations

import re

from engine.program.catalog import Catalog
from engine.program.language import (Agg, Calc, Col, Compare, CompareColumns, Contains, DateOf, Equals, InPeriod, InSection, IsBlank, IsNumber, OnLine, Rank, RowArithmetic, SECTION, Show, Size, is_sound, period_from_name)
from engine.program.names import ANYWHERE, COUNT, COUNT_DISTINCT, ENDS, ENTITY_GROUP, ENTITY_LINE, ENTITY_PEER, HIGHEST, LOWEST, STARTS
from engine.sheet.names import SUBTOTAL, TOTAL


class Unbound(ValueError):
    """Why a program's text does not bind."""


_AGGS = {"sum", "avg", "min", "max", "count", "countd"}
_NUMBER = r"-?\d+(?:\.\d+)?"
_QUOTED = r'"((?:[^"]|"")*)"'


def _unquote(text: str) -> str:
    return text.replace('""', '"')


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Split on a separator outside quotes and parentheses."""
    parts, depth, quoted, current = [], 0, False, []
    for ch in text:
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == sep and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


class _Binder:
    def __init__(self, catalog: Catalog, allowed: set[str]):
        self.catalog = catalog
        self.allowed = allowed

    def table(self, name: str):
        if name not in self.allowed or name not in self.catalog.by_id:
            raise Unbound("unknown_table")
        return self.catalog.table(name)

    def col(self, table, text: str) -> Col:
        text = text.strip()
        if text == "band":
            return Col(table.id, SECTION)
        if not re.fullmatch(r"\d+", text) or int(text) >= len(table.columns):
            raise Unbound("unknown_column")
        return Col(table.id, int(text))

    def measure(self, table, text: str):
        text = text.strip()
        low = text.lower()
        if low.startswith("abs(") and text.endswith(")"):
            return Size(self.measure(table, text[4:-1]))
        if low.startswith("date(") and text.endswith(")"):
            return DateOf(self.col(table, text[5:-1]))
        match = re.fullmatch(r"(\d+)\s*([-+*/])\s*(\d+)", text)
        if match:
            return RowArithmetic(self.col(table, match[1]), match[2], self.col(table, match[3]))
        return self.col(table, text)

    def condition(self, table, text: str):
        text = text.strip()
        if match := re.fullmatch(rf"band\s*=\s*{_QUOTED}", text):
            return InSection(table.id, _unquote(match[1]))
        if match := re.fullmatch(rf"band\s*~\s*{_QUOTED}", text):
            return InSection(table.id, _unquote(match[1]), True)
        if match := re.fullmatch(rf"line\s*=\s*{_QUOTED}(?:\s+(total|subtotal))?", text):
            return OnLine(table.id, _unquote(match[1]), {"total": TOTAL, "subtotal": SUBTOTAL}.get(match[2]))
        if match := re.fullmatch(r"line\s+(total|subtotal)", text):
            return OnLine(table.id, "", {"total": TOTAL, "subtotal": SUBTOTAL}[match[1]])
        if match := re.fullmatch(rf"(\d+)\s*=\s*{_QUOTED}", text):
            return Equals(self.col(table, match[1]), _unquote(match[2]))
        if match := re.fullmatch(rf"(\d+)\s*=\s*({_NUMBER})", text):
            return Equals(self.col(table, match[1]), float(match[2]))
        if match := re.fullmatch(rf"(\d+)\s*([~^$])\s*{_QUOTED}", text):
            anchor = {"~": ANYWHERE, "^": STARTS, "$": ENDS}[match[2]]
            return Contains(self.col(table, match[1]), _unquote(match[3]), anchor)
        if match := re.fullmatch(r"(\d+)\s+in\s+(\S+)", text):
            try:
                return InPeriod(self.col(table, match[1]), period_from_name(match[2]))
            except (ValueError, IndexError) as exc:
                raise Unbound("bad_period") from exc
        if match := re.fullmatch(r"(\d+)\s*(>=|<=|!=|>|<)\s*c(\d+)", text):
            return CompareColumns(self.col(table, match[1]), match[2], self.col(table, match[3]))
        if match := re.fullmatch(rf"(\d+)\s*(>=|<=|!=|>|<)\s*({_NUMBER})", text):
            return Compare(self.col(table, match[1]), match[2], float(match[3]))
        if match := re.fullmatch(r"(\d+)\s+(blank|filled|isnum)", text):
            column = self.col(table, match[1])
            return IsNumber(column) if match[2] == "isnum" else IsBlank(column, filled=match[2] == "filled")
        raise Unbound("bad_condition")

    def body(self, text: str):
        """(head, table, conditions, flags) of one plain program's text."""
        text = text.strip()
        at = re.search(r"@([tfn]\d+)", text)
        if at is None:
            raise Unbound("no_table")
        table = self.table(at[1])
        head, rest = text[:at.start()].strip(), text[at.end():].strip()
        flags = set()
        mark = None
        if match := re.search(rf"\bmark\s+{_QUOTED}", rest):
            mark = _unquote(match[1])
            rest = (rest[:match.start()] + rest[match.end():]).strip()
        conditions = ()
        if rest.lower().startswith("where"):
            body = rest[5:].strip()
            tail = re.search(r"\s+(size|mag)\s*$", body)
            if tail:
                flags.add(tail[1])
                body = body[:tail.start()]
            conditions = tuple(self.condition(table, part) for part in _split_top(body))
        else:
            for word in rest.split():
                if word in ("size", "mag"):
                    flags.add(word)
                elif word:
                    raise Unbound("bad_tail")
        return head, table, conditions, flags, mark

    def plain(self, text: str):
        head, table, where, flags, mark = self.body(text)
        words = head.split()
        if not words:
            raise Unbound("no_form")
        form = words[0].lower()
        if form == "agg":
            function = words[1].lower() if len(words) > 1 else ""
            if function not in _AGGS:
                raise Unbound("bad_aggregate")
            if function == COUNT:
                return Agg(COUNT, None, table.id, where, magnitude="mag" in flags)
            if len(words) < 3:
                raise Unbound("no_measure")
            expression = "".join(words[2:])
            if function == COUNT_DISTINCT:
                return Agg(COUNT_DISTINCT, self.col(table, expression), table.id, where)
            return Agg(function, self.measure(table, expression), table.id, where, magnitude="mag" in flags)
        if form == "show":
            shown = tuple(self.col(table, part) for part in " ".join(words[1:]).split(","))
            return Show(table.id, shown, where)
        if form == "list":
            return Show(table.id, (self.col(table, " ".join(words[1:])),), where, listing=True)
        if form == "rank":
            return self.rank(words, table, where, flags, mark)
        raise Unbound("unknown_form")

    def rank(self, words, table, where, flags, mark):
        if len(words) < 3 or words[2].lower() not in ("high", "low"):
            raise Unbound("bad_rank")
        entity, direction, rest = words[1].lower(), HIGHEST if words[2].lower() == "high" else LOWEST, words[3:]
        if entity == "line":
            if "show" not in rest:
                raise Unbound("rank_line_needs_show")
            at = rest.index("show")
            shown = tuple(self.col(table, part) for part in "".join(rest[at + 1:]).split(","))
            return Rank(ENTITY_LINE, table.id, direction, where, self.measure(table, "".join(rest[:at])), shown)
        if entity == "group":
            if "by" not in rest or not rest:
                raise Unbound("rank_group_needs_by")
            at = rest.index("by")
            function = rest[0].lower()
            if function not in ("sum", "avg", "min", "max", "count"):
                raise Unbound("bad_aggregate")
            key = self.col(table, "".join(rest[at + 1:]))
            if function == COUNT:
                return Rank(ENTITY_GROUP, table.id, direction, where, None, key=key, function=COUNT, by_size="size" in flags)
            return Rank(ENTITY_GROUP, table.id, direction, where, self.measure(table, "".join(rest[1:at])), key=key, function=function, by_size="size" in flags)
        if entity == "peer":
            spec = "".join(rest)
            if match := re.fullmatch(r"a(\d+)", spec):
                axes = table.table.axes
                if int(match[1]) >= len(axes):
                    raise Unbound("unknown_axis")
                peers = tuple(axes[int(match[1])].positions)
            else:
                peers = tuple(self.col(table, part).position for part in spec.split(","))
            return Rank(ENTITY_PEER, table.id, direction, where, peers=peers, by_size="size" in flags, mark=mark)
        raise Unbound("bad_rank")

    def program(self, text: str):
        text = text.strip()
        if text.lower().startswith("calc"):
            return self.calc(text[4:].strip())
        return self.plain(text)

    def calc(self, text: str):
        magnitude = False
        if text.endswith(" mag"):
            magnitude = True
            text = text[:-4].strip()
        if not text.startswith("("):
            raise Unbound("bad_calc")
        depth, quoted, close = 0, False, None
        for at, ch in enumerate(text):
            if ch == '"':
                quoted = not quoted
            elif not quoted:
                depth += ch == "("
                depth -= ch == ")"
                if depth == 0:
                    close = at
                    break
        if close is None:
            raise Unbound("bad_calc")
        left, rest = text[1:close], text[close + 1:].strip()
        if not rest or rest[0] not in "-+/" or not rest[1:].strip().startswith("(") or not rest.endswith(")"):
            raise Unbound("bad_calc")
        op, right = rest[0], rest[1:].strip()[1:-1]
        try:
            return Calc(self.program(left), op, self.program(right), magnitude=magnitude)
        except ValueError as exc:
            if isinstance(exc, Unbound):
                raise
            raise Unbound("calc_sides") from exc


def bind(text: str, catalog: Catalog, allowed: set[str] | None = None):
    """The typed program a line of text means, or Unbound."""
    binder = _Binder(catalog, allowed if allowed is not None else set(catalog.by_id))
    try:
        program = binder.program(text)
    except Unbound:
        raise
    except ValueError as exc:
        raise Unbound("refused_by_language") from exc
    if not is_sound(program):
        raise Unbound("unsound")
    return program


__all__ = ["Unbound", "bind"]
