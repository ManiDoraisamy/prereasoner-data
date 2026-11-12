"""Build engine/data/named_coordinates.json: the name set's structure tier and its meaning tier.

(a) operation and structure: the closed lists of engine/program/names.py and engine/sheet/names.py, read from them, never copied; each name gets its one line here, and a list name without a definition fails the build.
(b) meaning: the owner's selected property basis, never the 1,521-property catalogue (docs/ARCHITECTURE.md, Shared Property Binding Migration): the "taxonomy" dimensions of engine/data/alloc.json in that file's order, provenance training/props/, with the documented migration of its two non-property names (the class-named GeoCoordinates becomes the geo property; taxonName, absent from schema.org 30.0, becomes alternateName). Each property is written with its own label and comment and the fine answer type its ranges hold; each schema.org class that is itself a domain of a basis property is written as the superposition of those properties; the classes under the fine answer types' roots are written with their type. training/schema_org/basis.py reads the catalogue for these, through engine/schema_org.py; the runtime reads this file. A property enters the basis only through alloc.json, with coverage and held-out evidence and the owner's OK. No vocabulary of any trade or language is written here.

usage: python -m training.factory.build_names
"""
from __future__ import annotations

import hashlib
import json

from training.factory.paths import NEXT

OUT = f"{NEXT}/engine/data/named_coordinates.json"

DEFINITIONS = {
    "form": {"show": "reads a labelled cell, or lists the values on the kept lines", "agg": "one figure over the kept lines", "rank": "the top line, group or peer column", "calc": "the difference, sum or ratio of two one-number programs"},
    "aggregate": {"sum": "adds the figures", "avg": "the mean of the figures", "min": "the lowest figure", "max": "the highest figure", "count": "how many lines", "countd": "how many different values"},
    "direction": {"highest": "ranks from the top", "lowest": "ranks from the bottom"},
    "size": {"signed": "a figure read with its sign", "by_size": "a group or peer ranks by the size of its total", "each": "a measure read line by line with its sign dropped", "stated": "the signed result stated by its size"},
    "entity": {"line": "the line holding the top value", "group": "the key whose lines add up highest", "peer": "the peer column holding the top value"},
    "comparison": {">=": "at least", ">": "above", "<=": "at most", "<": "below", "!=": "other than"},
    "row_op": {"+": "two measures of one line added", "-": "one measure of a line less another", "*": "two measures of one line multiplied", "/": "one measure of a line over another"},
    "calc_op": {"-": "one program's number less another's", "+": "two programs' numbers added", "/": "one program's number over another's"},
    "answer_type": {"number": "a figure", "count": "how many", "percent": "a share or percent", "date": "a day or period", "person": "a person's name", "company": "a company's name", "code": "a code or other name", "list": "several values", "place": "a place", "money": "a sum of money", "quantity": "a physical quantity"},
    "word_role": {"measure": "the word names what is measured", "key": "the word names what is grouped by or shown", "filter": "the word narrows the lines", "context": "the word names the sheet, the tab or its subject"},
    "condition": {"equals": "a cell is this value", "contains": "a cell holds this text", "period": "a date falls in this period", "comparison": "a number compares with a number stated", "not_equals": "a text cell is some other value", "blank": "a cell is blank or filled", "section": "a band above the line says this", "line": "a line's own label holds this"},
    "anchor": {"anywhere": "the text sits anywhere in the cell", "starts": "the cell starts with it", "ends": "the cell ends with it"},
    "measure_kind": {"column": "one column's figures", "row_arithmetic": "two columns of one line under an operator", "size": "a measure with its sign dropped", "date": "a date column read as its day", "across": "peer columns of one line added"},
    "period": {"day": "one day", "month": "one month", "quarter": "one quarter", "half": "one half year", "season": "a trading season: winter, summer", "year": "one year", "range": "a span between two days", "relative": "YTD, MTD, BOM, prompt", "weekday": "a weekday", "week": "one week", "hour": "one hour"},
    "column_role": {"key": "text naming the record, mostly distinct", "label": "text with repeats, a category", "section_label": "a sparse label filled down over the lines under it", "measure": "numbers", "date": "typed or written days", "period": "months, quarters, years as words", "peer": "one of several columns of one axis", "row_total": "adds up its own line", "derived": "arithmetic on its own line", "tab_name": "the tab a family member came from", "spacer": "empty inside a table's range"},
    "line_role": {"record": "a data line", "subtotal": "a printed summary of part of a table", "total": "a printed summary of a whole table", "band": "a section label over the lines under it", "header": "names columns", "title": "context above a table", "note": "a label and value outside a table", "blank": "nothing on the line"},
    "value_kind": {"blank": "no value", "number": "a number", "date": "a day", "period": "a named period", "text": "text", "error": "a formula error", "bool": "true or false", "marker": "a filled cell saying no value: -, n/a"},
}


def structure_tier() -> list[dict]:
    from engine.program import names as program
    from engine.sheet import names as sheet
    lists = {"form": program.FORMS, "aggregate": program.AGGREGATES, "direction": program.DIRECTIONS, "size": program.SIZES, "entity": program.RANK_ENTITIES, "comparison": program.COMPARISONS, "row_op": program.ROW_OPS, "calc_op": program.CALC_OPS,
             "answer_type": program.ANSWER_TYPES, "word_role": program.WORD_ROLES, "condition": program.CONDITION_KINDS, "anchor": program.ANCHORS, "measure_kind": program.MEASURE_KINDS,
             "period": sheet.PERIOD_KINDS, "column_role": sheet.COLUMN_ROLES, "line_role": sheet.LINE_ROLES, "value_kind": sheet.VALUE_KINDS}
    out = []
    for group, members in lists.items():
        for member in members:
            out.append({"name": f"{group}:{member}", "group": group, "definition": DEFINITIONS[group][member]})
    return out


def main() -> None:
    """The worktree's own closed lists, not the factory's pinned commit: the name set is the runtime's."""
    from training.schema_org.basis import meaning_tier
    meaning = meaning_tier()
    structure = structure_tier()
    document = {
        "schema_version": 3,
        "builder": "training/factory/build_names.py",
        "tiers": {
            "structure": {"note": "closed lists, read from engine/program/names.py and engine/sheet/names.py", "count": len(structure), "names": structure},
            "meaning": meaning,
        },
        "licenses": [
            {"source": "schema.org 30.0", "url": "https://schema.org/docs/terms.html", "license": "CC BY-SA 3.0", "use": "names and contract, already in the runtime bundle"},
        ],
    }
    text = json.dumps(document, indent=0, ensure_ascii=False) + "\n"
    with open(OUT, "w", encoding="utf-8", newline="\n") as sink:
        sink.write(text)
    print("structure", len(structure), "basis", meaning["basis"]["count"], "classes", len(meaning["classes"]), "fine classes", len(meaning["fine_classes"]), "sha", hashlib.sha256(text.encode()).hexdigest()[:12])


if __name__ == "__main__":
    main()
