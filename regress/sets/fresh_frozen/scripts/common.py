"""Answer comparison and the sandboxed program runner (stdlib only, so both interpreters can import it)."""
import json
import math
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
USER_SITE = site.getusersitepackages()
TMP = os.path.join(ROOT, "work", "tmp")
TIMEOUT = 90

ALLOWED_IMPORTS = {"pandas", "numpy", "openpyxl", "re", "math", "datetime", "collections", "statistics",
                   "itertools", "functools", "decimal", "string", "calendar", "json", "warnings"}
FORBIDDEN = re.compile(r"\b(os|sys|subprocess|socket|requests|urllib|http|shutil|pathlib|glob|ctypes|pickle|"
                       r"importlib|builtins|multiprocessing|threading)\b\s*\.|__import__|\beval\s*\(|\bexec\s*\(|"
                       r"\bcompile\s*\(|\bopen\s*\(|to_csv|to_excel|to_pickle|to_parquet|\.write\(|https?://|"
                       r"read_html|read_sql|read_pickle|read_parquet|globals\(|locals\(|setattr|delattr|getattr\(\s*__")

RUNNER = """import sys
sys.path.append({site!r})
import json, math, datetime, warnings
warnings.filterwarnings("ignore")
import pandas as pd
import numpy as np
import socket
def _blocked(*a, **k):
    raise OSError("network disabled")
socket.socket.connect = _blocked
socket.getaddrinfo = _blocked
socket.create_connection = _blocked

def _norm(x):
    if isinstance(x, (list, tuple, set, np.ndarray, pd.Series, pd.Index)):
        return [_norm(v) for v in list(x)]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if math.isnan(float(x)) else float(x)
    if isinstance(x, (pd.Timestamp, datetime.datetime, datetime.date)):
        s = x.isoformat()
        return s[:-9] if s.endswith("T00:00:00") else s
    if isinstance(x, pd.Period):
        return str(x)
    if isinstance(x, (int, str)) or x is None:
        return x
    return str(x)

result = None
{program}

print("__RESULT__" + json.dumps(_norm(result)))
"""


def static_check(program):
    for line in program.splitlines():
        s = line.strip()
        m = re.match(r"(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", s)
        if m:
            mods = [m.group(1)] if m.group(1) else [p.strip().split()[0] for p in m.group(2).split(",")]
            for mod in mods:
                if mod.split(".")[0] not in ALLOWED_IMPORTS:
                    return f"import {mod}"
    m = FORBIDDEN.search(program)
    return f"forbidden: {m.group(0)}" if m else None


def run_program(program, workbook_file):
    """Run a program in a fresh temp directory holding a copy of the workbook. Returns (ok, value, error)."""
    bad = static_check(program)
    if bad:
        return False, None, "static: " + bad
    os.makedirs(TMP, exist_ok=True)
    d = tempfile.mkdtemp(dir=TMP)
    try:
        shutil.copyfile(os.path.join(ROOT, "workbooks", workbook_file), os.path.join(d, workbook_file))
        script = os.path.join(d, "_run.py")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write(RUNNER.format(site=USER_SITE, program=program))
        env = {k: os.environ[k] for k in ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATH") if k in os.environ}
        env.update({"TEMP": d, "TMP": d, "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                    "NO_PROXY": "", "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"})
        try:
            p = subprocess.run([PY, "-I", script], cwd=d, env=env, capture_output=True, timeout=TIMEOUT,
                               text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return False, None, "timeout"
        for line in p.stdout.splitlines()[::-1]:
            if line.startswith("__RESULT__"):
                return True, json.loads(line[len("__RESULT__"):]), None
        err = (p.stderr or "").strip().splitlines()
        return False, None, (err[-1] if err else f"exit {p.returncode}")[:300]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- comparison
NUM_RE = re.compile(r"^[\s$]*([-+]?\(?[\d,]*\.?\d+(?:[eE][-+]?\d+)?\)?)\s*%?\s*$")


def to_number(x):
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return None if (isinstance(x, float) and math.isnan(x)) else float(x)
    if isinstance(x, list) and len(x) == 1:
        return to_number(x[0])
    m = NUM_RE.match(str(x))
    if not m:
        return None
    s = m.group(1).replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def norm_text(x):
    if isinstance(x, list) and len(x) == 1:
        x = x[0]
    s = str(x).strip().casefold()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .;:'\"")


def norm_date(x):
    if isinstance(x, list) and len(x) == 1:
        x = x[0]
    if isinstance(x, float) and x == int(x):
        x = int(x)
    s = str(x).strip()
    s = re.sub(r"T00:00:00(\.0+)?$", "", s)
    s = re.sub(r" 00:00:00$", "", s)
    return s


def norm_yes_no(x):
    if isinstance(x, list) and len(x) == 1:
        x = x[0]
    if isinstance(x, bool):
        return "yes" if x else "no"
    s = norm_text(x)
    if s in ("yes", "y", "true", "1"):
        return "yes"
    if s in ("no", "n", "false", "0"):
        return "no"
    return s


def numbers_agree(a, b):
    if a is None or b is None:
        return False
    if a == b:
        return True
    return abs(a - b) <= 0.005 * max(abs(a), abs(b))


def parse_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    return v
            except ValueError:
                pass
        return [p for p in re.split(r"\s*[|;]\s*|\s*,\s+", s) if p]
    return [x]


def element_key(v):
    n = to_number(v)
    return ("n", round(n, 6)) if n is not None else ("t", norm_text(v))


def lists_agree(a, b):
    la, lb = parse_list(a), parse_list(b)
    if len(la) != len(lb):
        return False
    ka, kb = [element_key(v) for v in la], [element_key(v) for v in lb]
    if sorted(ka) == sorted(kb):
        return True
    # numbers within tolerance, order-free
    if all(k[0] == "n" for k in ka + kb):
        return all(numbers_agree(x[1], y[1]) for x, y in zip(sorted(ka), sorted(kb)))
    return False


def agree(answer_type, a, b):
    if a is None or b is None:
        return False
    if answer_type == "number":
        return numbers_agree(to_number(a), to_number(b))
    if answer_type == "list":
        return lists_agree(a, b)
    if answer_type == "yes_no":
        return norm_yes_no(a) == norm_yes_no(b) and norm_yes_no(a) in ("yes", "no")
    if answer_type == "date":
        return norm_date(a) == norm_date(b) and norm_date(a) != ""
    # name
    if isinstance(a, list) or isinstance(b, list):
        return lists_agree(a, b) if (isinstance(a, list) and isinstance(b, list)) else norm_text(a) == norm_text(b)
    return norm_text(a) == norm_text(b) and norm_text(a) != ""


def stated_value(answer_type, s):
    """Gemini states answers as strings; lists as JSON array strings."""
    if answer_type == "list":
        return parse_list(s)
    return s
