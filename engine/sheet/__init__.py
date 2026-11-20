"""The sheet compiler: a host's payload (payload.py) to a SheetModel (model.py) of named coordinates (names.py). Nothing in production imports this package until the cut-over."""
from __future__ import annotations

from collections.abc import Mapping

from engine.sheet.compiler import compile_tab
from engine.sheet.families import find_families
from engine.sheet.model import SheetModel
from engine.sheet.payload import WorkbookPayload, workbook_from_json


def compile_workbook(workbook: WorkbookPayload, typed: Mapping[str, str] | None = None) -> SheetModel:
    """Every tab's model, in workbook order, and the families across them. typed: text cells the workbook's loading read
    as days or months, cell text -> ISO day or month (engine/program/dates.py); a cell of that text in any tab is read as
    that period and keeps its own text. tab_hash does not hold it: a model compiled with it is not cached by tab."""
    tabs = [compile_tab(tab, typed) for tab in workbook.tabs]
    return SheetModel(workbook.name, tabs, find_families(tabs))


__all__ = ["compile_tab", "compile_workbook", "workbook_from_json"]
