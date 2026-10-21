"""Learned refusal: a confidence for the chosen answer, fitted on the held-out calibration databases, and the
threshold that keeps wrong answers at or under a target share of the answers given.

The confidence is a logistic regression over what serving knows of its choice (engine.readers.search.ReadingSearch
.confident reads the same five features): the re-scorer's probability (when a re-scorer chose), the decoder's log
probability of the chosen candidate, the margin from the best candidate that ran to the next, the candidate's rank,
and whether its result is empty. It is cross-validated by database (5 folds), so each fold scores databases it did
not fit on. The threshold is the lowest that meets the target on those out-of-fold scores, and the final model is
fitted on all of calibration.

usage: python -m training.readers.refusal <calibration candidates.jsonl> [--rescorer <run dir>] [--target 0.12]
           [--out refusal.json]
       python -m training.readers.refusal match <refusal.json> <baseline per-example.json> <readers per-example.json>

``match`` sets a fitted refusal's threshold from two serving-faithful calibration runs of spider.probe.full_eval on
the same questions: the baseline planner's, and the readers' with the threshold off (each answer records its
confidence in ``selection.confidence``). The threshold is the lowest confidence at which the readers give no more
wrong answers than the baseline, so the readers answer as many questions right as they can without answering more
wrongly than the planner they replace; it is written with the counts it was chosen on.
"""
import argparse
import json

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

from training.readers.choose import RescorerChoice, pick_ran

FEATURES = ["rescorer_probability", "score", "margin", "rank", "empty"]


def _records(path):
    data = json.load(open(path, encoding="utf-8"))
    return {int(r["idx"]): r for r in (data["records"] if isinstance(data, dict) else data)}


def _true(value):
    return value is True or value == "True"


def _confidence(record):
    import ast
    selection = record.get("selection")
    selection = ast.literal_eval(selection) if isinstance(selection, str) else (selection or {})
    return selection.get("confidence")


def match(refusal_path, baseline_path, readers_path):
    """Set ``refusal_path``'s threshold to the lowest at which the readers' run answers no more wrongly than the
    baseline run (see the module docstring), and record what it was chosen on."""
    baseline, readers = _records(baseline_path), _records(readers_path)
    shared = sorted(set(baseline) & set(readers))
    allowed = sum(_true(baseline[i].get("ok")) and not _true(baseline[i].get("strict")) for i in shared)
    answered = [(_confidence(readers[i]), _true(readers[i].get("strict"))) for i in shared
                if _true(readers[i].get("ok")) and _confidence(readers[i]) is not None]
    best = None
    for threshold in sorted({round(c, 4) for c, _ in answered}):
        kept = [right for c, right in answered if c >= threshold]
        if len(kept) - sum(kept) <= allowed:
            best = (threshold, len(kept), sum(kept))
            break
    if best is None:
        raise SystemExit("no threshold keeps the readers' wrong answers at the baseline's")
    threshold, given, right = best
    fitted = json.load(open(refusal_path, encoding="utf-8"))
    fitted.update(threshold=threshold, target=None, matched={
        "questions": len(shared), "baseline_wrong": allowed, "answered": given, "right": right,
        "wrong": given - right})
    with open(refusal_path, "w", encoding="utf-8", newline="\n") as sink:
        json.dump(fitted, sink, indent=1)
    print(f"threshold {threshold:.4f}: answered {given}/{len(shared)}, right {right}, wrong {given - right} "
          f"(baseline wrong {allowed})")


def main():
    import sys
    if sys.argv[1:2] == ["match"]:
        if len(sys.argv) != 5:
            raise SystemExit("usage: python -m training.readers.refusal match <refusal.json> <baseline> <readers>")
        match(*sys.argv[2:5])
        return
    ap = argparse.ArgumentParser(description="Fit the refusal confidence and threshold on calibration candidates.")
    ap.add_argument("candidates", help="calibration candidates (training.readers.candidates, or their union)")
    ap.add_argument("--rescorer", help="a re-scorer run directory")
    ap.add_argument("--target", type=float, default=0.12, help="the largest share of wrong answers among those given")
    ap.add_argument("--out", help="where to write refusal.json")
    args = ap.parse_args()
    records = [json.loads(line) for line in open(args.candidates, encoding="utf-8")]
    chooser = RescorerChoice(args.rescorer) if args.rescorer else None
    X, y, groups = [], [], []
    for record in records:
        candidates = record["candidates"]
        if chooser:
            chosen, probability = chooser.pick(record["question"], candidates)
        else:
            chosen, probability = pick_ran(candidates), 0.0
        if chosen is None:
            continue
        ran = [c["score"] for c in candidates if c["status"] == "ran"]
        following = sorted(ran, reverse=True)[1] if len(ran) > 1 else chosen["score"] - 10.0
        X.append([probability, chosen["score"], min(max(ran) - following, 10.0), chosen["rank"],
                  float(chosen["rows"] == 0)])
        y.append(int(bool(chosen.get("right"))))
        groups.append(record["db_id"])
    total = len(records)
    X, y, groups = np.array(X), np.array(y), np.array(groups)
    scores = np.zeros(len(y))
    for train, held in GroupKFold(n_splits=5).split(X, y, groups):
        scores[held] = LogisticRegression(max_iter=1000).fit(X[train], y[train]).predict_proba(X[held])[:, 1]
    best = None
    for threshold in sorted(set(np.round(scores, 4))):
        keep = scores >= threshold
        given, right = int(keep.sum()), int(y[keep].sum())
        if given and (given - right) / given <= args.target:
            best = (float(threshold), given, right, given - right)
            break
    right_all = int(y.sum())
    print(f"calibration questions {total}; answering every chosen one: right {right_all} ({right_all / total:.1%}), "
          f"wrong {len(y) - right_all} ({(len(y) - right_all) / max(1, len(y)):.1%} of answered)")
    if best:
        threshold, given, right, wrong = best
        print(f"refusing below confidence {threshold:.4f} (out-of-fold): answered {given}/{total} "
              f"({given / total:.1%}), right {right} ({right / total:.1%} of all), wrong {wrong} "
              f"({wrong / given:.1%} of answered)")
    else:
        print(f"no threshold keeps wrong answers at or under {args.target:.0%} of the answers given")
    final = LogisticRegression(max_iter=1000).fit(X, y)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as sink:
            json.dump({"features": FEATURES, "coef": final.coef_[0].tolist(), "intercept": float(final.intercept_[0]),
                       "threshold": best and best[0], "target": args.target}, sink, indent=1)


if __name__ == "__main__":
    main()
