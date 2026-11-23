"""Run generated programs in the sandbox and compare.
  python -I scripts/exec_step.py first  [ids...]  gen/<id>.json  -> exec1/<id>.json (program vs Gemini's stated answer)
  python -I scripts/exec_step.py second [ids...]  ver/<id>.json  -> exec2/<id>.json (independent program vs gold)
Prints counts only."""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import ROOT, agree, run_program, stated_value, to_number  # noqa: E402
from sources import records  # noqa: E402

TYPES = {"number", "name", "list", "yes_no", "date"}


def wording_problem(q, meta):
    text = q.get("question", "")
    if not text or len(text) < 12:
        return "empty"
    if re.search(r"\brow\s+\d|\bcolumn\s+[A-Z]{1,2}\b|\bcell\s+[A-Z]{1,2}\d", text):
        return "mentions a row/column"
    if meta["file"].lower() in text.lower():
        return "mentions the file"
    for s in meta["sheets"]:
        name = s.get("name") or ""
        code_like = "_" in name or re.fullmatch(r"(?i)sheet\s*\d+|table\s*\d+[\w.-]*", name.strip() or "x")
        if code_like and name.lower() in text.lower():
            return "mentions a sheet"
    return None


def result_problem(answer_type, value):
    if value is None:
        return "no result"
    if answer_type == "number":
        n = to_number(value)
        if n is None or n != n or n in (float("inf"), float("-inf")):
            return "not a number"
    if answer_type == "list":
        if not isinstance(value, list) or not (2 <= len(value) <= 10):
            return "list size"
    if answer_type in ("name", "date") and (isinstance(value, list) or str(value).strip() == ""):
        return "not a single value"
    if answer_type == "yes_no" and str(value).strip().lower() not in ("yes", "no", "true", "false"):
        return "not yes/no"
    return None


def first(ids):
    os.makedirs(os.path.join(ROOT, "work", "exec1"), exist_ok=True)
    tot = kept = 0
    for rec in records():
        sid = rec["id"]
        src = os.path.join(ROOT, "work", "gen", sid + ".json")
        out = os.path.join(ROOT, "work", "exec1", sid + ".json")
        if (ids and sid not in ids) or not os.path.exists(src):
            continue
        g = json.load(open(src, encoding="utf-8"))
        done = {q["qid"]: q for q in json.load(open(out, encoding="utf-8"))["questions"]} \
            if os.path.exists(out) else {}
        if done and all(q["qid"] in done for q in g["questions"]):
            continue
        meta = json.load(open(os.path.join(ROOT, "work", "render", sid + ".json"), encoding="utf-8"))
        reasons = {}
        for idx, q in enumerate(g["questions"]):
            if q["qid"] in done:
                g["questions"][idx] = done[q["qid"]]
                continue
            q["pass1"] = False
            why = None
            if q.get("answer_type") not in TYPES:
                why = "bad answer_type"
            else:
                why = wording_problem(q, meta)
            if why is None:
                ok, value, err = run_program(q.get("program", ""), g["file"])
                q["result"], q["error"] = value, err
                if not ok:
                    why = "program failed"
                else:
                    why = result_problem(q["answer_type"], value)
                    if why is None and not agree(q["answer_type"], stated_value(q["answer_type"], q["answer"]), value):
                        why = "disagrees with stated answer"
            q["reject1"] = why
            q["pass1"] = why is None
            reasons[why or "kept"] = reasons.get(why or "kept", 0) + 1
        json.dump(g, open(out, "w", encoding="utf-8"), indent=1)
        n, k = len(g["questions"]), sum(q["pass1"] for q in g["questions"])
        tot, kept = tot + n, kept + k
        print(f"exec1 {sid}: {k}/{n} " + json.dumps(reasons))
    print(f"exec1 total this run: {kept}/{tot}")


def second(ids):
    os.makedirs(os.path.join(ROOT, "work", "exec2"), exist_ok=True)
    tot = kept = 0
    for rec in records():
        sid = rec["id"]
        src1 = os.path.join(ROOT, "work", "exec1", sid + ".json")
        srcv = os.path.join(ROOT, "work", "ver", sid + ".json")
        out = os.path.join(ROOT, "work", "exec2", sid + ".json")
        if (ids and sid not in ids) or not os.path.exists(srcv):
            continue
        g = json.load(open(src1, encoding="utf-8"))
        answers = {a.get("id"): a for a in json.load(open(srcv, encoding="utf-8"))["answers"]}
        done = {q["qid"]: q for q in json.load(open(out, encoding="utf-8"))["questions"]
                if "pass2" in q and q.get("reject2") != "no check answer"} if os.path.exists(out) else {}
        if done and all(q["qid"] in done for q in g["questions"] if q["pass1"]):
            continue
        reasons = {}
        for idx, q in enumerate(g["questions"]):
            if not q["pass1"]:
                continue
            if q["qid"] in done:
                g["questions"][idx] = done[q["qid"]]
                continue
            a = answers.get(q["qid"])
            q["pass2"] = False
            why = None
            if a is None:
                why = "no check answer"
            else:
                q["check_stated"] = a.get("answer")
                q["check_program"] = a.get("program", "")
                if str(a.get("answer", "")).strip().upper() == "AMBIGUOUS":
                    why = "checker says ambiguous"
                else:
                    ok, value, err = run_program(q["check_program"], g["file"])
                    q["check_result"], q["check_error"] = value, err
                    if not ok:
                        why = "check program failed"
                    elif not agree(q["answer_type"], q["result"], value):
                        why = "check disagrees with gold"
            q["reject2"] = why
            q["pass2"] = why is None
            reasons[why or "kept"] = reasons.get(why or "kept", 0) + 1
        json.dump(g, open(out, "w", encoding="utf-8"), indent=1)
        n = sum(q["pass1"] for q in g["questions"])
        k = sum(q.get("pass2", False) for q in g["questions"])
        tot, kept = tot + n, kept + k
        print(f"exec2 {sid}: {k}/{n} " + json.dumps(reasons))
    print(f"exec2 total this run: {kept}/{tot}")


if __name__ == "__main__":
    step, ids = sys.argv[1], set(sys.argv[2:])
    {"first": first, "second": second}[step](ids)
