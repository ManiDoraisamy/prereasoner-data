"""What Gemini is shown of a tab, built once: the rows as an analyst sees them, and for the author the compiled tables by id and position. Rows are capped, so a large tab costs what a small one costs."""
from __future__ import annotations

from collections import Counter

MAX_ROW_CHARS = 24000
MAX_SLOT_CHARS = 9000
HEAD_ROWS = 60
TAIL_ROWS = 5
LOW_CARDINALITY = 30
CELL_WIDTH = 40


def short(value, width: int = CELL_WIDTH) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        value = float(f"{value:.8g}")
        if value.is_integer():
            value = int(value)
    return " ".join(str(value).split()).replace("|", "/")[:width]


def grid_rows(tab) -> list[list]:
    """A compiled tab's cells as a grid of plain values: sheet row 1 is index 0, a blank is "", a date or a dated period that was a serial number is its ISO day."""
    rows: list[list] = []
    for line in tab.lines:
        if not line.cells:
            continue
        width = max(line.cells)
        row: list = [""] * width
        for col, cell in line.cells.items():
            value = cell.raw
            if not isinstance(value, str) and cell.kind in ("date", "period") and cell.period is not None and cell.period.start:
                value = cell.period.start
            row[col - 1] = value
        while len(rows) < line.row - 1:
            rows.append([])
        rows.append(row)
    return rows


def rows_view(name: str, rows: list[list]) -> str:
    """The tab's rows with their numbers: the first 60 and last 5 filled ones, plus the values of its low-cardinality text columns."""
    filled = [(i, row) for i, row in enumerate(rows) if any(v not in ("", None) for v in row)]
    width = max((len(row) for _, row in filled), default=0)
    lines = [f"=== tab {name!r}: {len(rows)} rows, {width} columns"]
    shown = filled[:HEAD_ROWS] + ([] if len(filled) <= HEAD_ROWS + TAIL_ROWS else [(None, None)]) + (filled[HEAD_ROWS:] if len(filled) <= HEAD_ROWS + TAIL_ROWS else filled[-TAIL_ROWS:])
    for i, row in shown:
        if i is None:
            lines.append("...")
            continue
        cells = [short(v) for v in row[:80]]
        while cells and cells[-1] == "":
            cells.pop()
        lines.append(f"r{i + 1}: " + " | ".join(cells))
    text = "\n".join(lines)[:MAX_ROW_CHARS]
    values = []
    for c in range(min(width, 60)):
        distinct = Counter(s for _, row in filled if c < len(row) and isinstance(row[c], str) for s in [row[c].strip()] if s)
        if 1 < len(distinct) <= LOW_CARDINALITY and len(filled) > HEAD_ROWS + TAIL_ROWS:
            values.append(f"col {c}: " + "; ".join(short(k, 30) for k, _ in distinct.most_common()))
    return text + ("\nvalues by column (0-based):\n" + "\n".join(values) if values else "")


def _table_view(table) -> str:
    """One catalog table: id, name, columns by position with kind, role and values, axes, bands, printed labels."""
    t = table.table
    lines = [f"{table.id} {short(t.name, 60)!r} rows {t.first_row}-{t.last_row}, {sum(table.is_data)} data lines" + (f", as of {t.context.as_of.start}" if t.context.as_of and t.context.as_of.start else "") + (f", unit {short(t.context.unit, 20)}" if t.context.unit else "")]
    peer_positions = {p for axis in t.axes for p in axis.positions}
    for column in t.columns:
        if column.position in peer_positions and len(peer_positions) > 4:
            continue
        words = table.spelling.get(column.position, {})
        values = ""
        if column.kind not in ("number", "date") and 0 < len(words) <= 15:
            values = " = " + "; ".join(short(v, 26) for v in list(words.values())[:15])
        elif column.kind not in ("number", "date") and words:
            values = " e.g. " + "; ".join(short(v, 22) for v in list(words.values())[:4])
        lines.append(f" {column.position} {short(column.name, 50)!r} {column.kind}/{column.role}{values}")
    for at, axis in enumerate(t.axes):
        names = [short(t.columns[p].name, 14) for p in axis.positions]
        lines.append(f" a{at} {axis.kind} {short(axis.name, 24)!r}: positions {axis.positions[0]}..{axis.positions[-1]} ({'; '.join(names[:6])}{' ...' if len(names) > 6 else ''})")
    bands = list(dict.fromkeys(text for text in table.section_texts if text))
    if bands:
        lines.append(" bands: " + "; ".join(short(b, 26) for b in bands[:12]) + (" ..." if len(bands) > 12 else ""))
    printed = list(dict.fromkeys(short(line.label, 30) or "(unlabelled)" for line in table.lines if line.role in ("subtotal", "total")))
    if printed:
        lines.append(" printed lines: " + "; ".join(printed[:12]) + (" ..." if len(printed) > 12 else ""))
    return "\n".join(lines)


def slots_view(tables: list) -> str:
    """The compiled tables a program may read."""
    return "\n".join(_table_view(table) for table in tables)[:MAX_SLOT_CHARS]


__all__ = ["grid_rows", "rows_view", "short", "slots_view"]
