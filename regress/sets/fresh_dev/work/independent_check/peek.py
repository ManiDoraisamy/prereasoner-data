"""Print chosen rows of a sheet (my own reading of the workbook, for the hand recomputation).
usage: python -I peek.py <file> <sheet or -> <a-b[,c-d...] row ranges> [regex on any cell]"""
import re
import sys
import openpyxl  # noqa: E402

path, sheet, ranges = sys.argv[1], sys.argv[2], sys.argv[3]
pat = re.compile(sys.argv[4], re.I) if len(sys.argv) > 4 else None
wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
ws = wb[sheet] if sheet != "-" else wb[wb.sheetnames[0]]
want = []
for part in ranges.split(","):
    a, b = part.split("-")
    want.append((int(a), int(b)))
for i, row in enumerate(ws.iter_rows(values_only=True), 1):
    cells = [(j, v) for j, v in enumerate(row) if v is not None and str(v).strip() != ""]
    inr = any(a <= i <= b for a, b in want)
    hit = pat is not None and any(pat.search(str(v)) for _, v in cells)
    if inr or hit:
        print(i, " | ".join(f"{j}:{str(v)[:38]!r}" if isinstance(v, str) else f"{j}:{v}" for j, v in cells)[:420])
