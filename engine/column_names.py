"""Positional spreadsheet field names; do not discard duplicate or blank cells."""
from collections import Counter
import re


def column_letter(index):
    text = ''
    while index >= 0:
        index, remainder = divmod(index, 26)
        text = chr(65 + remainder) + text
        index -= 1
    return text


def canonical_columns(headers):
    names = [re.sub('[\ud800-\udfff]', '\ufffd', str(value or '')).strip()
             or 'Column ' + column_letter(i) for i, value in enumerate(headers)]
    frequencies = Counter(name.lower() for name in names)
    used, result = set(), []
    for index, name in enumerate(names):
        candidate = name
        suffix = ' [column ' + column_letter(index) + ']'
        if frequencies[name.lower()] > 1 or len(name.encode('utf-8')) > 63:
            candidate = name.encode('utf-8')[:63-len(suffix)].decode('utf-8', errors='ignore') + suffix
        counter = 1
        while candidate.lower() in used:
            counter += 1
            numbered = suffix + ' ' + str(counter)
            candidate = name.encode('utf-8')[:63-len(numbered)].decode('utf-8', errors='ignore') + numbered
        used.add(candidate.lower())
        result.append(candidate)
    return result
