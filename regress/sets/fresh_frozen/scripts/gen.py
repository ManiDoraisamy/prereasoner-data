"""Gemini steps through the repo's own client (engine/llm.py).
Run with GOOGLE_CLOUD_PROJECT set:
  python regress/sets/fresh_frozen/scripts/gen.py generate [ids...]   -> work/gen/<id>.json
  python regress/sets/fresh_frozen/scripts/gen.py verify   [ids...]   -> work/ver/<id>.json   (needs work/exec1/<id>.json)
Token use of every call is appended to work/usage.jsonl. Nothing here prints prompts, questions or answers."""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(ROOT)))
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO, ".env"))
from engine import llm  # noqa: E402
from sources import records  # noqa: E402

# Assumed list prices (USD per 1M tokens) for the cost estimate: deliberately above Flash-class list prices.
PRICE_IN, PRICE_OUT = 1.00, 6.00
BUDGET_STOP = 12.0
N_QUESTIONS = 6
USAGE = os.path.join(ROOT, "work", "usage.jsonl")

_last_usage = {}


def _install_usage_hook():
    models = llm._client().models
    orig = models.generate_content

    def wrapped(*a, **k):
        resp = orig(*a, **k)
        u = getattr(resp, "usage_metadata", None)
        _last_usage.clear()
        if u is not None:
            _last_usage.update(prompt=u.prompt_token_count or 0, output=u.candidates_token_count or 0,
                               thoughts=getattr(u, "thoughts_token_count", 0) or 0,
                               total=u.total_token_count or 0)
        return resp

    models.generate_content = wrapped


def spent():
    if not os.path.exists(USAGE):
        return 0.0, 0, 0
    pin = pout = 0
    for line in open(USAGE, encoding="utf-8"):
        r = json.loads(line)
        pin += r.get("prompt", 0)
        pout += r.get("output", 0) + r.get("thoughts", 0)
    return pin / 1e6 * PRICE_IN + pout / 1e6 * PRICE_OUT, pin, pout


def call(step, sid, system, prompt, schema, max_out=32000, timeout=240):
    cost, _, _ = spent()
    if cost >= BUDGET_STOP:
        raise SystemExit(f"budget stop: estimated ${cost:.2f}")
    for attempt in range(3):
        t0 = time.time()
        try:
            text = llm.generate_text(system=system, prompt=prompt, max_output_tokens=max_out,
                                     json_schema=schema, timeout_seconds=timeout)
            ok = True
        except llm.LLMUnavailable as exc:
            text, ok = None, False
            err = str(exc)
        rec = {"step": step, "id": sid, "attempt": attempt, "ok": ok, "seconds": round(time.time() - t0, 1),
               **_last_usage}
        _last_usage.clear()
        with open(USAGE, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec) + "\n")
        if ok:
            try:
                return json.loads(text)
            except ValueError:
                err = "bad json"
        print(f"  retry {sid} {step}: {err[:80]}")
        time.sleep(5)
    return None


ANSWER_RULES = """answer_type is one of: number, name, list, yes_no, date.
   - number: a plain number with no commas, units or % sign (a percentage is written as shown, e.g. 12.5 for 12.5%). Give the full value unless the question asks for rounding.
   - name: the exact text of the cell that answers (a state, a cause, a hospital, a category...).
   - list: a JSON array of exact cell texts (or numbers), 2 to 5 items; order does not matter.
   - yes_no: "yes" or "no".
   - date: ISO format: "YYYY-MM-DD" for a day, "YYYY-MM" for a month, "YYYY" for a year."""

PROGRAM_RULES = """program: Python 3 using pandas (you may also import numpy, openpyxl, re, math, datetime). It must read the file by its bare name '{file}' in the current directory with pd.read_excel(...) (always pass sheet_name explicitly) or pd.read_csv(...), compute the answer from the data (never hard-code the answer), and assign it to a variable named result: an int or float for number, a str for name or date (ISO as above), a list for list, and the string "yes" or "no" for yes_no. No printing, no file writing, no network, no other imports. For Excel sheets with title rows and multi-row headers, read with header=None and locate rows and columns by matching their label text (strip spaces, dots and footnote markers) rather than trusting fixed positions; with header=None the pandas row index is the Excel row number minus 1 and the column index is the Excel column position minus 1. Convert numbers stored as text (commas, '*', footnote markers) before arithmetic. Beware: with header=None a year or code in a column that also holds decimals is read as a float (2024 becomes 2024.0, so str() gives '2024.0'); compare such labels numerically or normalise them first. CSV dates are read as text unless you parse them."""

RENDER_NOTE = """How to read the rendering:
- Excel sheets are shown as rows "rN: A=value | B=value ..." with Excel row numbers and column letters; empty cells are omitted. A line break inside a cell is shown as \\n (normalise whitespace when you match labels).
- CSV files are shown the same way; r1 is the header line and r2 is the first data row (pandas index 0).
- Large sheets show the first rows, a sample of rows, the last rows and a profile of every column (counts, min, max, sum, value counts)."""

GEN_SYSTEM = ("You write a held-out test set for a system that answers plain-language questions about spreadsheets. "
              "You are given a text rendering of one real public government spreadsheet. You write questions that a real "
              "analyst, clinician or program manager would type, each with exactly one correct answer computed from the file, "
              "and a short pandas program that computes it. Reply with JSON only.")

GEN_PROMPT = """Workbook file name: {file}
Publisher: {publisher}

{render_note}

<<<RENDERING
{rendering}
RENDERING>>>

Write {n} questions about this workbook.

Rules for each question:
1. Wording: plain language, the way a busy analyst or clinician would ask a colleague. Do NOT copy column headers, row labels or cell text verbatim; paraphrase them the way people do (for example "folks 65 and older" rather than "65 years and over", "kids with no coverage" rather than "Uninsured, under 19 years"). Proper names (states, hospitals, drugs, causes, places) stay recognisable. One sentence. Never mention row numbers, column letters, sheet names or file names.
2. Exactly one correct answer, with no judgement calls. Name the year or period, the group, and the measure whenever the sheet has several (number vs percent, age-adjusted vs crude, estimate vs standard error, current vs constant dollars). If the same figure appears on several sheets, make the question specific enough that every sheet gives the same answer. Never ask about footnotes, notes, flags, standard errors, table titles, or cells marked unreliable, suppressed or not available.
3. You must be able to work out the answer exactly from the rendering above (shown rows, or the column profiles for large sheets). Only ask about sheets shown in detail. Your stated answer must be exactly right.
4. Mix the kinds across the {n} questions, using at least four different kinds from: lookup (one value), filtered_count, filtered_sum, average, top_n (a list of 2 to 5 names), comparison (which of two is larger, or the difference between two), yes_no (a true/false check), date_period (which year, month or date had the most or least, or a total over a period), group_by (which group has the largest total or average). Use at least two answer types other than number when the data allows.
5. {answer_rules}
6. {program_rules}

Return JSON: {{"questions": [{{"question": "...", "sheet": "sheet name, or empty for a CSV", "kind": "...", "answer_type": "...", "answer": "the answer as a string (a JSON array string for list)", "program": "..."}}]}}"""

GEN_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {"question": {"type": "string"}, "sheet": {"type": "string"}, "kind": {"type": "string"},
                       "answer_type": {"type": "string", "enum": ["number", "name", "list", "yes_no", "date"]},
                       "answer": {"type": "string"}, "program": {"type": "string"}},
        "required": ["question", "sheet", "kind", "answer_type", "answer", "program"]}}},
    "required": ["questions"]}

VER_SYSTEM = ("You check questions about a spreadsheet. You are given a text rendering of one real public government "
              "workbook and some questions. For each question, work out the answer yourself from the rendering and write a "
              "short pandas program that computes it from the file. Reply with JSON only.")

VER_PROMPT = """Workbook file name: {file}
Publisher: {publisher}

{render_note}

<<<RENDERING
{rendering}
RENDERING>>>

Questions:
{questions}

For each question give its answer and a program.
- {answer_rules}
- {program_rules}
- If a question is ambiguous (it has two or more reasonable answers in this workbook) or cannot be answered from it, set "answer" to "AMBIGUOUS" and still write your best program.

Return JSON: {{"answers": [{{"id": "question id", "answer": "the answer as a string (a JSON array string for list)", "program": "..."}}]}}"""

VER_SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "answer": {"type": "string"}, "program": {"type": "string"}},
        "required": ["id", "answer", "program"]}}},
    "required": ["answers"]}


def load(sid):
    meta = json.load(open(os.path.join(ROOT, "work", "render", sid + ".json"), encoding="utf-8"))
    text = open(os.path.join(ROOT, "work", "render", sid + ".txt"), encoding="utf-8").read()
    return meta, text


def generate(ids):
    os.makedirs(os.path.join(ROOT, "work", "gen"), exist_ok=True)
    for rec in records():
        sid = rec["id"]
        out = os.path.join(ROOT, "work", "gen", sid + ".json")
        if (ids and sid not in ids) or os.path.exists(out):
            continue
        meta, text = load(sid)
        prompt = GEN_PROMPT.format(file=meta["file"], publisher=rec["publisher"], render_note=RENDER_NOTE,
                                   rendering=text, n=N_QUESTIONS, answer_rules=ANSWER_RULES,
                                   program_rules=PROGRAM_RULES.format(file=meta["file"]))
        res = call("generate", sid, GEN_SYSTEM, prompt, GEN_SCHEMA)
        if res is None:
            print(f"gen FAIL {sid}")
            continue
        qs = res.get("questions", [])
        for i, q in enumerate(qs):
            q["qid"] = f"{sid}#{i + 1}"
        json.dump({"id": sid, "file": meta["file"], "questions": qs}, open(out, "w", encoding="utf-8"), indent=1)
        cost, pin, pout = spent()
        print(f"gen {sid}: {len(qs)} questions; est ${cost:.2f} ({pin} in / {pout} out)")


MORE_SUFFIX = """

These questions were already written for this workbook. Write {n} NEW questions that differ from them in substance (other rows, measures, groups or kinds), following every rule above:
{previous}"""


def more(ids):
    """Top-up round: more candidates for the named workbooks, appended to gen/<id>.json."""
    for rec in records():
        sid = rec["id"]
        path = os.path.join(ROOT, "work", "gen", sid + ".json")
        if not ids or sid not in ids or not os.path.exists(path):
            continue
        g = json.load(open(path, encoding="utf-8"))
        meta, text = load(sid)
        prev = "\n".join("- " + q["question"] for q in g["questions"])
        prompt = GEN_PROMPT.format(file=meta["file"], publisher=rec["publisher"], render_note=RENDER_NOTE,
                                   rendering=text, n=N_QUESTIONS, answer_rules=ANSWER_RULES,
                                   program_rules=PROGRAM_RULES.format(file=meta["file"])) + \
            MORE_SUFFIX.format(n=N_QUESTIONS, previous=prev)
        res = call("generate_more", sid, GEN_SYSTEM, prompt, GEN_SCHEMA)
        if res is None:
            print(f"more FAIL {sid}")
            continue
        start = len(g["questions"])
        new = res.get("questions", [])
        for i, q in enumerate(new):
            q["qid"] = f"{sid}#{start + i + 1}"
        g["questions"].extend(new)
        g["rounds"] = g.get("rounds", 1) + 1
        json.dump(g, open(path, "w", encoding="utf-8"), indent=1)
        cost, pin, pout = spent()
        print(f"more {sid}: +{len(new)} questions; est ${cost:.2f}")


def verify(ids):
    os.makedirs(os.path.join(ROOT, "work", "ver"), exist_ok=True)
    for rec in records():
        sid = rec["id"]
        src = os.path.join(ROOT, "work", "exec1", sid + ".json")
        out = os.path.join(ROOT, "work", "ver", sid + ".json")
        if (ids and sid not in ids) or not os.path.exists(src):
            continue
        previous = json.load(open(out, encoding="utf-8"))["answers"] if os.path.exists(out) else []
        answered = {a.get("id") for a in previous}
        kept = [q for q in json.load(open(src, encoding="utf-8"))["questions"]
                if q["pass1"] and q["qid"] not in answered]
        if not kept:
            continue
        meta, text = load(sid)
        epath = os.path.join(ROOT, "work", "retry_errors.json")
        errors = json.load(open(epath, encoding="utf-8")) if os.path.exists(epath) else {}

        def line(q):
            d = {"id": q["qid"], "question": q["question"], "answer_type": q["answer_type"]}
            if q["qid"] in errors:
                d["previous_attempt_error"] = errors[q["qid"]]
            return json.dumps(d, ensure_ascii=False)

        qtext = "\n".join(line(q) for q in kept)
        if any(q["qid"] in errors for q in kept):
            qtext += ("\n\nA question with previous_attempt_error: an earlier program of yours for it crashed with that "
                      "error. Title rows often repeat words from the header row, so match header rows and labels "
                      "exactly (after normalising whitespace) or by their position, and write a corrected program.")
        prompt = VER_PROMPT.format(file=meta["file"], publisher=rec["publisher"], render_note=RENDER_NOTE,
                                   rendering=text, questions=qtext, answer_rules=ANSWER_RULES,
                                   program_rules=PROGRAM_RULES.format(file=meta["file"]))
        res = call("verify", sid, VER_SYSTEM, prompt, VER_SCHEMA)
        if res is None:
            print(f"ver FAIL {sid}")
            continue
        asked = {q["qid"] for q in kept}
        new = [a for a in res.get("answers", []) if a.get("id") in asked]
        json.dump({"id": sid, "answers": previous + new}, open(out, "w", encoding="utf-8"), indent=1)
        cost, pin, pout = spent()
        print(f"ver {sid}: {len(res.get('answers', []))} answers for {len(kept)}; est ${cost:.2f}")


if __name__ == "__main__":
    _install_usage_hook()
    step, ids = sys.argv[1], set(sys.argv[2:])
    {"generate": generate, "more": more, "verify": verify}[step](ids)
    cost, pin, pout = spent()
    print(f"total est ${cost:.2f}: {pin} prompt tokens, {pout} output+thinking tokens")
