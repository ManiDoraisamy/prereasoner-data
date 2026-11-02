"""The sheet compiler: a host's payload (payload.py) to a SheetModel (model.py) of named coordinates (names.py). Nothing in production imports this package until the cut-over."""
from __future__ import annotations

from engine.sheet.compiler import compile_tab
from engine.sheet.families import find_families
from engine.sheet.model import SheetModel
from engine.sheet.payload import WorkbookPayload, workbook_from_json


def compile_workbook(workbook: WorkbookPayload) -> SheetModel:
    """Every tab's model, in workbook order, and the families across them."""
    tabs = [compile_tab(tab) for tab in workbook.tabs]
    return SheetModel(workbook.name, tabs, find_families(tabs))


__all__ = ["compile_tab", "compile_workbook", "workbook_from_json"]
