"""Build engine/data/named_coordinates.json, the name set: three tiers of named coordinates.

(a) operation and structure: the closed lists of engine/program/names.py and engine/sheet/names.py, read from them, never copied; each name gets its one line here, and a list name without a definition fails the build. Three answer types the brief asks for and engine/program/names.py does not hold yet (place, money, quantity) are added here and marked pending.
(b) meaning: schema.org 30.0's 1,521 properties and 926 classes by reference to their contract, and the vocabulary pack (vocabulary.py), each pack name with its FIBO IRI where its label is one.
(c) the sheet's own words: matched by rule at serving, no list.

usage: python -m training.factory.build_names [fibo_labels.json]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from training.factory.paths import NEXT, PUBLIC, pin
from training.factory.vocabulary import PACK

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
    "sign_word": {"net": "net, position, value: the signed sum", "loss": "a loss, owing, short: lowest where stored negative", "size": "biggest driver, moved most: by size", "magnitude": "a burn, a payment: the signed figure stated as its size"},
    "period": {"day": "one day", "month": "one month", "quarter": "one quarter", "half": "one half year", "season": "a trading season: winter, summer", "year": "one year", "range": "a span between two days", "relative": "YTD, MTD, BOM, prompt", "weekday": "a weekday", "week": "one week", "hour": "one hour"},
    "column_role": {"key": "text naming the record, mostly distinct", "label": "text with repeats, a category", "section_label": "a sparse label filled down over the lines under it", "measure": "numbers", "date": "typed or written days", "period": "months, quarters, years as words", "peer": "one of several columns of one axis", "row_total": "adds up its own line", "derived": "arithmetic on its own line", "tab_name": "the tab a family member came from", "spacer": "empty inside a table's range"},
    "line_role": {"record": "a data line", "subtotal": "a printed summary of part of a table", "total": "a printed summary of a whole table", "band": "a section label over the lines under it", "header": "names columns", "title": "context above a table", "note": "a label and value outside a table", "blank": "nothing on the line"},
    "value_kind": {"blank": "no value", "number": "a number", "date": "a day", "period": "a named period", "text": "text", "error": "a formula error", "bool": "true or false", "marker": "a filled cell saying no value: -, n/a"},
}


def structure_tier() -> list[dict]:
    from engine.program import names as program
    from engine.sheet import names as sheet
    lists = {"form": program.FORMS, "aggregate": program.AGGREGATES, "direction": program.DIRECTIONS, "size": program.SIZES, "entity": program.RANK_ENTITIES, "comparison": program.COMPARISONS, "row_op": program.ROW_OPS, "calc_op": program.CALC_OPS,
             "answer_type": program.ANSWER_TYPES, "word_role": program.WORD_ROLES, "condition": program.CONDITION_KINDS, "anchor": program.ANCHORS, "measure_kind": program.MEASURE_KINDS, "sign_word": program.SIGN_WORDS,
             "period": sheet.PERIOD_KINDS, "column_role": sheet.COLUMN_ROLES, "line_role": sheet.LINE_ROLES, "value_kind": sheet.VALUE_KINDS}
    out = []
    for group, members in lists.items():
        for member in members:
            out.append({"name": f"{group}:{member}", "group": group, "definition": DEFINITIONS[group][member]})
    return out


def meaning_tier(fibo: dict[str, str]) -> list[dict]:
    seen = set()
    out = []
    for group, name, definition in PACK:
        if name in seen:
            raise ValueError(f"a pack name twice: {name}")
        seen.add(name)
        row = {"name": f"biz:{name}", "group": group, "definition": definition}
        label = name.replace("_", " ")
        if label in fibo:
            row["fibo"] = fibo[label]
        out.append(row)
    return out


def main() -> None:
    pin()
    from engine.config import DATA_DIR
    fibo_path = sys.argv[1] if len(sys.argv) > 1 else PUBLIC + "fibo_labels.json"
    with open(fibo_path, encoding="utf-8") as source:
        fibo = json.load(source)
    with open(os.path.join(DATA_DIR, "schema_org_v30.json"), encoding="utf-8") as source:
        contract = json.load(source)
    structure = structure_tier()
    pack = meaning_tier(fibo)
    document = {
        "schema_version": 1,
        "builder": "training/factory/build_names.py",
        "tiers": {
            "structure": {"note": "closed lists, read from engine/program/names.py and engine/sheet/names.py", "count": len(structure), "names": structure},
            "meaning": {
                "schema_org": {"version": contract["version"], "contract_sha256": contract["contract_sha256"], "properties": len(contract["properties"]), "classes": len(contract["classes"]), "prefix": "schema:", "note": "representable by name through engine/schema_org.py; not copied here"},
                "vocabulary": {"prefix": "biz:", "count": len(pack), "names": pack},
            },
            "words": {"note": "the sheet's own words are matched by rule at serving and need no list"},
        },
        "licenses": [
            {"source": "schema.org 30.0", "url": "https://schema.org/docs/terms.html", "license": "CC BY-SA 3.0", "use": "names and contract, already in the runtime bundle"},
            {"source": "FIBO", "url": "https://github.com/edmcouncil/fibo", "license": "MIT", "notice": "Copyright (c) 2020 Enterprise Data Management Council", "use": "a pack name whose label is a FIBO label carries its IRI; definitions are our own"},
            {"source": "EIA glossary and tables", "url": "https://www.eia.gov/about/copyrights_reuse.php", "license": "US government, public domain", "use": "read for what the words mean; definitions are our own"},
            {"source": "US GAAP taxonomy (FASB)", "url": "https://xbrl.fasb.org/terms/TaxonomiesTermsConditions.html", "license": "royalty-free for financial reporting; modification barred without consent", "use": "NOT USED: a derived vocabulary reads as a modification"},
            {"source": "IFRS taxonomy", "url": "https://www.ifrs.org/issued-standards/ifrs-taxonomy/", "license": "copyright IFRS Foundation", "use": "NOT USED: terms not clearly open"},
            {"source": "ISDA and NAESB definitions", "url": "https://www.isda.org/", "license": "proprietary", "use": "NOT USED"},
        ],
    }
    text = json.dumps(document, indent=0, ensure_ascii=False) + "\n"
    with open(OUT, "w", encoding="utf-8", newline="\n") as sink:
        sink.write(text)
    print("structure", len(structure), "pack", len(pack), "with a fibo label", sum("fibo" in row for row in pack), "schema", len(contract["properties"]), len(contract["classes"]), "sha", hashlib.sha256(text.encode()).hexdigest()[:12])


if __name__ == "__main__":
    main()
