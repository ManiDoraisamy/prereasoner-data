"""Positional spreadsheet field names; do not discard duplicate or blank cells."""
from collections import Counter
import re

# PostgreSQL keeps 63 bytes of an identifier. A longer header keeps its start and its end around an
# ellipsis: long headers differ at the end as often as at the start (a Forms grid's "...service?
# [Speed]" and "...service? [Support]"), and keeping only the start made them one repeated field
# (2026-10-04). web/public/lib/workbook-import.js names fields the same way.
IDENTIFIER_BYTES = 63
ELLIPSIS = '…'
TAIL_BYTES = 16


def column_letter(index):
    text = ''
    while index >= 0:
        index, remainder = divmod(index, 26)
        text = chr(65 + remainder) + text
        index -= 1
    return text


def _shortened(name, suffix):
    data = name.encode('utf-8')
    budget = IDENTIFIER_BYTES - len(suffix.encode('utf-8'))
    if len(data) <= budget:
        return name + suffix
    tail = data[-TAIL_BYTES:].decode('utf-8', errors='ignore')
    room = budget - len(ELLIPSIS.encode('utf-8')) - len(tail.encode('utf-8'))
    return data[:room].decode('utf-8', errors='ignore') + ELLIPSIS + tail + suffix


def canonical_columns(headers):
    names = [re.sub('[\ud800-\udfff]', '�', str(value or '')).strip()
             or 'Column ' + column_letter(i) for i, value in enumerate(headers)]
    frequencies = Counter(name.lower() for name in names)
    used, result = set(), []
    for index, name in enumerate(names):
        candidate = name
        suffix = ' [column ' + column_letter(index) + ']'
        if frequencies[name.lower()] > 1 or len(name.encode('utf-8')) > IDENTIFIER_BYTES:
            candidate = _shortened(name, suffix)
        counter = 1
        while candidate.lower() in used:
            counter += 1
            candidate = _shortened(name, suffix + ' ' + str(counter))
        used.add(candidate.lower())
        result.append(candidate)
    return result
