"""Run model-written programs over one workbook's raw cells, in this process alone, with no imports and no file access: each sees R, num, s and a short list of built-ins, and one that uses anything else is refused before it runs. The restrictions are evals/enron/sandbox_run.py's.

usage: python -I -X utf8 sandbox_run.py <grids.json.gz> <programs.json>     programs: {"tab": "name", "programs": {"id": "text"}}. Prints one line of JSON: {"id": {"answer": ...} | {"error": ...}}.
A program may end in an expression: that is its answer. T is the rows of the question's tab; every row is as wide as its tab.
"""
import ast
import builtins
import collections
import gzip
import json
import math
import re
import statistics
import sys

ALLOWED = ("abs all any bool dict divmod enumerate filter float format frozenset int isinstance len list map max min next print range repr "
           "reversed round set slice sorted str sum tuple zip ValueError TypeError IndexError KeyError ZeroDivisionError Exception True False None").split()
REFUSED_NAMES = {"exec", "eval", "open", "compile", "globals", "locals", "getattr", "setattr", "delattr", "vars", "dir", "type", "object",
                 "breakpoint", "input", "help", "memoryview", "__import__", "classmethod", "staticmethod", "super", "property"}


def refuse(program):
    tree = ast.parse(program)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom)):
            return f"{type(node).__name__} is not allowed"
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return f"attribute {node.attr} is not allowed"
        if isinstance(node, ast.Name) and (node.id in REFUSED_NAMES or node.id.startswith("__")):
            return f"name {node.id} is not allowed"
    return None


def plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if not (isinstance(value, float) and not math.isfinite(value)) else None
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v) for v in list(value)[:200]]
    return str(value)


def num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "") if value is not None else ""
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    percent = text.endswith("%")
    try:
        number = float(text.rstrip("%"))
    except ValueError:
        return None
    number = number / 100 if percent else number
    return -number if negative else number


def s(value):
    return "" if value is None else str(value).strip()


def run_one(program, grids, tab_name):
    def R(tab):
        if tab not in grids:
            raise KeyError(f"no tab named {tab!r}; tabs are {list(grids)}")
        return grids[tab]

    try:
        problem = refuse(program)
    except SyntaxError as exc:
        return {"error": f"SyntaxError: {str(exc)[:200]}"}
    if problem:
        return {"error": f"refused: {problem}"}
    tree = ast.parse(program)
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body[-1]
        tree.body[-1] = ast.Assign(targets=[ast.Name("answer", ast.Store())], value=last.value)
        ast.fix_missing_locations(tree)
    safe = {name: getattr(builtins, name) for name in ALLOWED if hasattr(builtins, name)}
    scope = {"__builtins__": safe, "R": R, "num": num, "s": s, "math_sqrt": math.sqrt, "stdev": statistics.pstdev, "sample_stdev": statistics.stdev,
             "mean": statistics.fmean, "median": statistics.median, "defaultdict": collections.defaultdict, "Counter": collections.Counter, "re_search": re.search,
             "re_match": re.match, "re_sub": re.sub, "T": grids.get(tab_name, [])}
    try:
        exec(compile(tree, "program", "exec"), scope)
    except Exception as exc:  # noqa: BLE001 - the error is the program's result
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if "answer" not in scope:
        return {"error": "the program did not set answer"}
    return {"answer": plain(scope["answer"])}


def main():
    with gzip.open(sys.argv[1], "rt", encoding="utf-8") as source:
        grids = {g["name"]: g["rows"] for g in json.load(source)}
    for name, rows in grids.items():
        width = max((len(row) for row in rows), default=0)
        grids[name] = [[" ".join(cell.split()) if isinstance(cell, str) else cell for cell in row] + [""] * (width - len(row)) for row in rows]
    with open(sys.argv[2], encoding="utf-8") as source:
        job = json.load(source)
    out = {key: run_one(text, grids, job["tab"]) for key, text in job["programs"].items()}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
