"""The new engine reads no word of any language from its own code: no regular expression holds a word, no month or weekday
is spelled, no stop list or word ending is kept. Meaning comes from the learned readers (owner's rule, 2026-11-12: "you
can't have a single word in English anywhere in the code or config file to lookup"). This scans the serving packages'
source, so a word list cannot come back unnoticed.

Allowed, and named here with the reason: the program language's own identifiers (names.py files: what a coordinate is
called, never compared with a question or a cell), and the spreadsheet file format's own codes (Excel's error values and
number-format tokens), which the file stores the same in every language.
usage: python -m tests.test_no_word_lookups"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("engine/program", "engine/named", "engine/sheet")
# the file format's own codes: Excel error values and number-format tokens, the same in every language
FORMAT_GRAMMAR = {
    ("engine/sheet/values.py", r"^#(?:NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!|N/A|ERROR!|GETTING_DATA)$"),
    ("engine/sheet/grid.py", None),
}
_REGEX_CALLS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
_WORD_IN_PATTERN = re.compile(r"[A-Za-z]{3,}")
_MONTHS_AND_DAYS = re.compile(r"\b(?:january|february|march|april|june|july|august|september|october|november|december|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_FORBIDDEN_NAMES = ("_STOP", "STOP_WORDS", "stem", "SHORTHAND", "_MONTHS", "TOTAL_WORD", "_LEXICON")


def _pattern_words(pattern: str) -> list[str]:
    """Letters in a pattern that are words, not regex syntax: escapes (\\d, \\w, \\b), named groups, inline flags and
    character classes are cut first."""
    text = re.sub(r"\\[A-Za-z]", "", pattern)
    text = re.sub(r"\(\?P<\w+>", "(", text)
    text = re.sub(r"\(\?P=\w+\)", "", text)
    text = re.sub(r"\(\?[aiLmsux-]+\)", "", text)
    text = re.sub(r"\[(?:\\.|[^\]])*\]", "", text)
    return _WORD_IN_PATTERN.findall(text)


def _sources():
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _allowed(path: str, pattern: str) -> bool:
    return any(path == allowed and (allowed_pattern is None or pattern == allowed_pattern) for allowed, allowed_pattern in FORMAT_GRAMMAR)


def test_no_regular_expression_holds_a_word() -> None:
    found = []
    for path, text in _sources():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "re" and node.func.attr in _REGEX_CALLS and node.args:
                first = node.args[0]
                pattern = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
                if pattern is None and isinstance(first, ast.JoinedStr):
                    pattern = "".join(v.value for v in first.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if pattern and _pattern_words(pattern) and not _allowed(path, pattern):
                    found.append(f"{path}:{node.lineno} {pattern[:80]!r} holds {_pattern_words(pattern)[:4]}")
    assert not found, "a regular expression holds a word:\n" + "\n".join(found)


def test_no_month_or_weekday_is_spelled() -> None:
    found = []
    for path, text in _sources():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _MONTHS_AND_DAYS.search(node.value):
                parent_doc = node.value.strip().startswith(('"', "'")) is False and len(node.value) > 120
                if not parent_doc:
                    found.append(f"{path}:{getattr(node, 'lineno', 0)} {node.value[:60]!r}")
    docstrings = set()
    for path, text in _sources():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and ast.get_docstring(node):
                docstrings.add((path, ast.get_docstring(node)[:60]))
    found = [f for f in found if not any(f.startswith(path) and doc[:40] in f for path, doc in docstrings)]
    assert not found, "a month or a weekday is spelled in code:\n" + "\n".join(found)


def test_no_stop_list_stemming_or_word_table_comes_back() -> None:
    found = []
    for path, text in _sources():
        for node in ast.walk(ast.parse(text)):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else None
            if name in _FORBIDDEN_NAMES:
                found.append(f"{path}:{getattr(node, 'lineno', 0)} {name}")
    assert not found, "a word list or a word ending rule is back:\n" + "\n".join(found)


def main() -> int:
    tests = [(name, item) for name, item in globals().items() if name.startswith("test_") and callable(item)]
    failed = 0
    for name, item in tests:
        try:
            item()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"{len(tests) - failed} of {len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
