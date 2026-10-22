"""The readers' choice: a logistic model of whether a candidate that ran answers its question, fitted on the held-out
calibration databases. Serving (engine.readers.search.ReadingSearch.rescore) ranks a question's candidates by it and
refuses a choice whose probability is under its threshold (``confidence``, ``confident``): one model chooses and
refuses.

It weighs the features serving computes (engine.readers.search.choice_features): the re-scorer's logit, the
candidate's log probability, its best rank among the readers, the share of the readers' readings that build it, and
whether its result is empty. ``fit`` reads a calibration union pool (training.readers.union), each reader's own
candidates (for the rank and the share) and a re-scorer run. It prints the re-scorer alone against the choice
cross-validated by database (GroupKFold 5, so a database's questions are never scored by a model fitted on them) and
writes the model fitted on all of calibration, with no threshold.

usage: python -m training.readers.choice fit <union.jsonl[.gz]> <reader candidates.jsonl ...> --rescorer <run dir>
           --out <choice.json> [--logits <cache.jsonl>]
       python -m training.readers.choice match <choice.json> <baseline per-example.json> <readers per-example.json>

``match`` sets the threshold from two serving-faithful calibration runs of spider.probe.full_eval on the same
questions: the baseline planner's, and the readers' with no threshold (each answer records its confidence in
``selection.confidence``). The threshold is the lowest confidence at which the readers give no more wrong answers
than the baseline, so they answer as many questions right as they can without answering more wrongly than the
planner they replace; it is written with the counts it was chosen on.
"""
import argparse
import gzip
import json
import os

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

from engine.readers.search import CHOICE_FEATURES, choice_features


def _key(record):
    return record["source"], record["index"]


def _identity(candidate):
    return candidate["sql"] or candidate["read_as"]


def _lines(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as source:
        return [json.loads(line) for line in source]


def _logits(union, rescorer_dir, cache):
    """{question: {candidate: re-scorer logit}} for the servable candidates, cached in ``cache`` when given."""
    logits = {}
    if cache and os.path.exists(cache):
        for row in _lines(cache):
            logits[(row["source"], row["index"])] = row["logits"]
    missing = [record for record in union if _key(record) not in logits]
    if missing:
        import torch

        from training.readers import rescorer
        from training.readers.choose import RescorerChoice

        chooser = RescorerChoice(rescorer_dir)
        sink = open(cache, "a", encoding="utf-8", newline="\n") if cache else None
        with torch.no_grad():
            for record in missing:
                ran = [c for c in record["candidates"] if rescorer.servable(c)][:rescorer.MAX_CANDIDATES]
                scores = rescorer.score(chooser.model, chooser.tokenizer, record["question"], ran) if ran else []
                logits[_key(record)] = {_identity(c): float(s) for c, s in zip(ran, scores)}
                if sink:
                    sink.write(json.dumps({"source": record["source"], "index": record["index"],
                                           "logits": logits[_key(record)]}) + "\n")
        if sink:
            sink.close()
    return logits


def features(union, reader_paths, logits):
    """(X, right, question index of each row) over the union's candidates the re-scorer scored."""
    built = {}
    for path in reader_paths:
        for record in _lines(path):
            for candidate in record["candidates"]:
                built.setdefault((_key(record), _identity(candidate)), []).append(candidate["rank"])
    X, y, question_of = [], [], []
    for q, record in enumerate(union):
        scored = logits[_key(record)]
        for candidate in record["candidates"]:
            if _identity(candidate) not in scored:
                continue
            ranks = built.get((_key(record), _identity(candidate)), [candidate["rank"]])
            X.append(choice_features(scored[_identity(candidate)], candidate["score"], min(ranks),
                                     len(ranks) / len(reader_paths), candidate["rows"]))
            y.append(int(bool(candidate.get("right"))))
            question_of.append(q)
    return np.array(X), np.array(y), np.array(question_of)


def _chosen_right(scores, y, question_of, questions):
    right = 0
    for q in questions:
        members = np.where(question_of == q)[0]
        if len(members):
            right += int(y[members[np.argmax(scores[members])]])     # argmax keeps the first of tied candidates
    return right


def fit(union_path, reader_paths, rescorer_dir, out, cache=None):
    union = _lines(union_path)
    X, y, question_of = features(union, reader_paths, _logits(union, rescorer_dir, cache))
    databases = np.array([record["db_id"] for record in union])
    alone = chosen = 0
    for train, held in GroupKFold(n_splits=5).split(np.arange(len(union)), groups=databases):
        rows = np.isin(question_of, train)
        model = LogisticRegression(C=1.0, max_iter=2000).fit(X[rows], y[rows])
        chosen += _chosen_right(model.decision_function(X), y, question_of, held)
        alone += _chosen_right(X[:, 0], y, question_of, held)
    pool = sum(any(c.get("right") for c in record["candidates"]) for record in union)
    final = LogisticRegression(C=1.0, max_iter=2000).fit(X, y)
    fitted = {"features": list(CHOICE_FEATURES), "coef": [float(w) for w in final.coef_[0]],
              "intercept": float(final.intercept_[0]), "threshold": None,
              "calibration": {"questions": len(union), "readers": len(reader_paths), "pool": int(pool),
                              "rescorer_alone": int(alone), "choice_cross_validated": int(chosen)}}
    with open(out, "w", encoding="utf-8", newline="\n") as sink:
        json.dump(fitted, sink, indent=1)
    print(f"{len(union)} questions, right answer in the pool for {pool}: the re-scorer alone chooses {alone} right, "
          f"the choice (cross-validated by database) {chosen} -> {out}")


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


def match(choice_path, baseline_path, readers_path):
    """Set ``choice_path``'s threshold to the lowest at which the readers' run answers no more wrongly than the
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
    fitted = json.load(open(choice_path, encoding="utf-8"))
    fitted.update(threshold=threshold, matched={
        "questions": len(shared), "baseline_wrong": allowed, "answered": given, "right": right,
        "wrong": given - right})
    with open(choice_path, "w", encoding="utf-8", newline="\n") as sink:
        json.dump(fitted, sink, indent=1)
    print(f"threshold {threshold:.4f}: answered {given}/{len(shared)}, right {right}, wrong {given - right} "
          f"(baseline wrong {allowed})")


def main():
    ap = argparse.ArgumentParser(description="Fit the readers' choice, or match its threshold.")
    commands = ap.add_subparsers(dest="command", required=True)
    fitting = commands.add_parser("fit")
    fitting.add_argument("union", help="the calibration union pool (training.readers.union)")
    fitting.add_argument("readers", nargs="+", help="each reader's calibration candidates")
    fitting.add_argument("--rescorer", required=True, help="the re-scorer run directory")
    fitting.add_argument("--out", required=True, help="where to write choice.json")
    fitting.add_argument("--logits", help="a cache of the re-scorer's logits for the union")
    matching = commands.add_parser("match")
    matching.add_argument("choice")
    matching.add_argument("baseline")
    matching.add_argument("readers_run")
    args = ap.parse_args()
    if args.command == "fit":
        fit(args.union, args.readers, args.rescorer, args.out, args.logits)
    else:
        match(args.choice, args.baseline, args.readers_run)


if __name__ == "__main__":
    main()
