"""Clear checks whose independent program crashed, so the verify step re-asks them with the error text.
Usage: python -I scripts/reset_failed_checks.py   (prints counts only)"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import ROOT  # noqa: E402

errs_path = os.path.join(ROOT, "work", "retry_errors.json")
errors = json.load(open(errs_path, encoding="utf-8")) if os.path.exists(errs_path) else {}
n = 0
for p in sorted(glob.glob(os.path.join(ROOT, "work", "exec2", "*.json"))):
    g = json.load(open(p, encoding="utf-8"))
    failed = {q["qid"]: q.get("check_error") for q in g["questions"] if q.get("reject2") == "check program failed"}
    if not failed:
        continue
    for q in g["questions"]:
        if q["qid"] in failed:
            for k in ("pass2", "reject2", "check_stated", "check_program", "check_result", "check_error"):
                q.pop(k, None)
    json.dump(g, open(p, "w", encoding="utf-8"), indent=1)
    vp = os.path.join(ROOT, "work", "ver", os.path.basename(p))
    v = json.load(open(vp, encoding="utf-8"))
    v["answers"] = [a for a in v["answers"] if a.get("id") not in failed]
    json.dump(v, open(vp, "w", encoding="utf-8"), indent=1)
    errors.update({k: (e or "")[:300] for k, e in failed.items()})
    n += len(failed)
    print(f"reset {os.path.basename(p)[:-5]}: {len(failed)}")
json.dump(errors, open(errs_path, "w", encoding="utf-8"), indent=1)
print(f"reset {n} checks")
