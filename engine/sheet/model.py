"""The SheetModel: what the sheet compiler produces and later layers read. Plain dataclasses; every fact traces to cell addresses, every role is a name from engine/sheet/names.py."""
from __future__ import annotations

from dataclasses import dataclass, field

from engine.sheet.formulas import column_letter
from engine.sheet.values import Period


@dataclass(slots=True)
class Cell:
    """One filled cell, traced to its address. value: a float for a number, an ISO day for a date (None when the text gives no year), the cell's cleaned text otherwise; raw is what the host sent."""
    row: int
    col: int
    raw: object
    kind: str
    value: object
    period: Period | None = None
    format_kind: str = "general"
    formula: str | None = None
    shape: str | None = None
    stray: str | None = None

    @property
    def address(self) -> str:
        return f"{column_letter(self.col)}{self.row}"


@dataclass(slots=True)
class Line:
    """One line of a tab or of a table. cells is keyed by sheet column on a tab's lines and by the column's position on a table's lines. A label filled down holds the source cell, so it still traces to where it was written."""
    tab: str
    row: int
    role: str
    cells: dict[int, Cell] = field(default_factory=dict)
    signals: tuple[str, ...] = ()
    hidden: bool = False
    section: tuple[str, ...] = ()
    label: str = ""
    scope: tuple[int, int] | None = None     # a printed line's rows, when its formulas say which it reads
    repeated: bool = False                   # a header line repeating the table's own header


@dataclass(slots=True)
class Column:
    """One column of a table. letter is the sheet's column; name joins the header path, or is "Column F" when nothing names it; header_cells are the addresses the name came from."""
    position: int
    col: int
    letter: str
    name: str
    path: tuple[str, ...] = ()
    header_cells: tuple[str, ...] = ()
    role: str = "label"
    kind: str = "text"
    format_kind: str = "general"
    unit: str = ""
    hidden: bool = False
    filled: int = 0
    distinct: int = 0


@dataclass(slots=True)
class Axis:
    """Peer columns: months across, hours across, columns under one group label. Printed in sheet order; positions index Table.columns."""
    kind: str
    name: str
    positions: tuple[int, ...]


@dataclass(slots=True)
class Stray:
    """A cell that does not fit its column's type, set apart by kind and never lost."""
    address: str
    tab: str
    row: int
    position: int
    kind: str
    raw: object


@dataclass(slots=True)
class Context:
    """What a table's surroundings say: tab name, title lines, as-of date, unit, each with the address it came from."""
    tab: str
    titles: tuple[tuple[str, str], ...] = ()      # (address, text)
    as_of: Period | None = None
    as_of_address: str = ""
    unit: str = ""
    unit_address: str = ""


@dataclass(slots=True)
class Note:
    """A label: value line outside a table: a one-line table."""
    tab: str
    row: int
    label: str
    value: Cell | None
    address: str


@dataclass(slots=True)
class Table:
    """One table of a tab. lines holds its records, printed its subtotal and total lines (searchable, never summed). bands lists the section labels in sheet order. first_row and last_row bound the body; header_rows may be empty."""
    tab: str
    index: int
    name: str
    columns: list[Column]
    lines: list[Line]
    printed: list[Line]
    header_rows: tuple[int, ...]
    first_row: int
    last_row: int
    context: Context
    bands: tuple[tuple[int, str], ...] = ()
    axes: tuple[Axis, ...] = ()
    strays: tuple[Stray, ...] = ()
    repeated_header_rows: tuple[int, ...] = ()
    block: int = 0                                # 0, or which side-by-side block of the rows


@dataclass(slots=True)
class Family:
    """Tables of one schema merged into one table, the member's name as a column: months on their own tabs, or repeated side-by-side blocks. basis says what matched them."""
    name: str
    basis: str
    members: tuple[tuple[str, int], ...]          # (tab, table index)
    table: Table


@dataclass(slots=True)
class TabModel:
    """One compiled tab. lines covers every row down to the last filled one, blanks included."""
    name: str
    sheet_type: str
    hidden: bool
    lines: list[Line]
    tables: list[Table]
    notes: list[Note]
    hidden_columns: tuple[int, ...] = ()


@dataclass(slots=True)
class SheetModel:
    """One compiled workbook."""
    name: str
    tabs: list[TabModel]
    families: list[Family]
