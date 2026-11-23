"""Render one workbook as text for the question writer.
Usage: python -I scripts/render.py <workbook path> <out .txt path>
Writes the rendering and a sidecar .json with sheet names and dimensions."""
import datetime as dt
import gc
import json
import os
import random
import sys

sys.path.append("C:/Users/manid/AppData/Roaming/Python/Python314/site-packages")

SHEET_FULL_MAX = 90000      # characters: a sheet this small is shown whole
TOTAL_MAX = 140000           # characters for the whole rendering
CELL_MAX = 90
HEAD_ROWS, SAMPLE_ROWS, TAIL_ROWS = 35, 30, 8


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:
            return ""
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.10g}"
    if isinstance(v, (dt.datetime, dt.date)):
        s = v.isoformat()
        return s[:-9] if s.endswith("T00:00:00") else s
    s = str(v).replace("\r\n", "\n").replace("\r", "\n").strip().replace("\n", "\\n")
    return s if len(s) <= CELL_MAX else s[:CELL_MAX] + "..."


def col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def grid_line(rnum, row):
    cells = [f"{col_letter(j)}={fmt(v)}" for j, v in enumerate(row) if fmt(v) != ""]
    return f"r{rnum}: " + " | ".join(cells) if cells else f"r{rnum}: (blank)"


def profile(values):
    vals = [v for v in values if fmt(v) != ""]
    out = {"non_empty": len(vals)}
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if vals and len(nums) >= 0.8 * len(vals):
        out.update(numeric=len(nums), min=fmt(min(nums)), max=fmt(max(nums)), sum=fmt(float(sum(nums))))
    dates = [v for v in vals if isinstance(v, (dt.datetime, dt.date))]
    if dates:
        out.update(date_min=fmt(min(dates)), date_max=fmt(max(dates)))
    counts = {}
    for v in vals:
        k = fmt(v)
        counts[k] = counts.get(k, 0) + 1
    out["distinct"] = len(counts)
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(counts) <= 30:
        out["value_counts"] = dict(top)
    else:
        out["top_values"] = dict(top[:6])
    return out


def render_rows(name, rows, header_row_idx, header_note):
    """rows: list of lists (already trimmed). Returns text."""
    n = len(rows)
    ncols = max((len(r) for r in rows), default=0)
    lines = []
    full = "\n".join(grid_line(i + 1, r) for i, r in enumerate(rows))
    if len(full) <= SHEET_FULL_MAX:
        lines.append(f"(whole sheet shown; {header_note})")
        lines.append(full)
        return "\n".join(lines), True
    lines.append(f"(sheet too large to show whole; {header_note}; showing the first {HEAD_ROWS} rows, "
                 f"{SAMPLE_ROWS} sampled rows, the last {TAIL_ROWS} rows, and a profile of every column)")
    for i in range(min(HEAD_ROWS, n)):
        lines.append(grid_line(i + 1, rows[i]))
    rest = list(range(HEAD_ROWS, max(HEAD_ROWS, n - TAIL_ROWS)))
    rng = random.Random(20261110)
    sample = sorted(rng.sample(rest, min(SAMPLE_ROWS, len(rest))))
    if sample:
        lines.append("... sampled rows:")
        for i in sample:
            lines.append(grid_line(i + 1, rows[i]))
    lines.append("... last rows:")
    for i in range(max(HEAD_ROWS, n - TAIL_ROWS), n):
        lines.append(grid_line(i + 1, rows[i]))
    h = header_row_idx
    hdr = rows[h] if h is not None and h < n else []
    lines.append(f"... column profiles (data rows below Excel row {h + 1 if h is not None else 0}):")
    for j in range(ncols):
        label = fmt(hdr[j]) if j < len(hdr) else ""
        p = profile([r[j] if j < len(r) else None for r in rows[(h + 1 if h is not None else 0):]])
        lines.append(f"  {col_letter(j)} [{label}]: " + json.dumps(p, ensure_ascii=False))
    text = "\n".join(lines)
    return text, False


def trim(rows):
    rows = [list(r) for r in rows]
    while rows and all(fmt(v) == "" for v in rows[-1]):
        rows.pop()
    width = 0
    for r in rows:
        for j in range(len(r) - 1, -1, -1):
            if fmt(r[j]) != "":
                width = max(width, j + 1)
                break
    return [r[:width] for r in rows]


def guess_header(rows):
    if not rows:
        return None
    width = max(len(r) for r in rows)
    for i, r in enumerate(rows[:40]):
        filled = sum(1 for v in r if fmt(v) != "")
        if filled >= max(2, 0.6 * width) and all(isinstance(v, str) or fmt(v) == "" for v in r):
            return i
    return 0


def main(path, out):
    meta = {"file": os.path.basename(path), "sheets": []}
    parts = []
    used = 0
    if path.lower().endswith(".csv"):
        import pandas as pd
        df = pd.read_csv(path, low_memory=False)
        meta["sheets"].append({"name": None, "rows": int(len(df)), "cols": int(df.shape[1]), "shown_whole": None})
        parts.append(f"FILE {meta['file']} (CSV). pandas.read_csv gives {len(df)} data rows x {df.shape[1]} columns.")
        parts.append("Columns and pandas dtypes: " + "; ".join(f"{c} ({df[c].dtype})" for c in df.columns))
        rows = [list(df.columns)] + df.astype(object).where(df.notna(), None).values.tolist()
        del df
        gc.collect()
        text, whole = render_rows("csv", rows, 0, "r1 is the CSV header line; r2 is pandas index 0")
        meta["sheets"][0]["shown_whole"] = whole
        parts.append(text)
    else:
        import openpyxl
        big = os.path.getsize(path) > 1_000_000
        wb = openpyxl.load_workbook(path, read_only=big, data_only=True)
        names = wb.sheetnames
        parts.append(f"FILE {meta['file']} (Excel workbook) with {len(names)} sheet(s): " + json.dumps(names))
        for k, name in enumerate(names):
            ws = wb[name]
            if not hasattr(ws, "iter_rows"):
                continue
            rows = trim(ws.iter_rows(values_only=True))
            merged = [] if big else [str(m) for m in list(ws.merged_cells.ranges)[:40]]
            n = len(rows)
            ncols = max((len(r) for r in rows), default=0)
            info = {"name": name, "rows": n, "cols": ncols}
            head = f"\n### Sheet {json.dumps(name)}: {n} rows x {ncols} columns (Excel rows 1..{n}, columns A..{col_letter(max(ncols - 1, 0))})"
            if merged:
                head += "\nmerged cells: " + ", ".join(merged)
            if used > TOTAL_MAX or (k >= 1 and used + 2000 > TOTAL_MAX):
                preview = "\n".join(grid_line(i + 1, r) for i, r in enumerate(rows[:4]))
                parts.append(head + "\n(not shown in detail: do NOT ask about this sheet)\n" + preview)
                info["shown_whole"] = None
            else:
                text, whole = render_rows(name, rows, guess_header(rows), "Excel row numbers")
                info["shown_whole"] = whole
                parts.append(head + "\n" + text)
            used = sum(len(p) for p in parts)
            meta["sheets"].append(info)
            del rows
        wb.close()
    text = "\n".join(parts)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    meta["chars"] = len(text)
    with open(out[:-4] + ".json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, indent=1)
    print(f"{meta['file']}\t{len(text)} chars\t" + ", ".join(
        f"{s['name']}:{s['rows']}x{s['cols']}{'' if s['shown_whole'] is None else ('W' if s['shown_whole'] else 'P')}"
        for s in meta["sheets"])[:300])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
