"""Train the line model: gradient-boosted stumps for "this line is a header", on train-family tabs only. Written experiment contract:

Hypothesis: many more header labels from train families (an offline Gemini markup of about 2,000 tabs) over a few more named coordinates raise header recall past the 34 hand tabs and free labels, at no cost in precision.
Frozen baseline: the artifact at 845898c, sha256 in design/phase1_finish.txt; dev recall .806 of lines, .755 of blocks on validation tabs V and the first blind batch.
Training data: training/sheet/line_labels.json, row numbers only, no cell text, each tab named by a hash of its file and tab name: hand labels, the markup (journal design/phase1_finish_markup_journal.jsonl, screened as scan_corpus.py does), free labels from what authors marked. Every tab is a train-family tab by the split of seed 20261102. Validation, frozen and studied workbooks are not in it.
Objective: logistic loss, weighted by source. No random draw at all. No GPU; minutes on CPU, in numpy with no scikit-learn. Stumps only, so the artifact stays one step function per named feature.
Offline gates: beats the baseline on validation tabs V and the first blind batch; the gold run over compiled lines does not fall. Serving gate: none, nothing in production imports this until the cut-over.
Output: training/sheet/data/experiments/<id>/line_model.json, disposable and gitignored. training/sheet/promote.py is the one writer of engine/data/sheet_line_model.json. Rollback: promote the earlier candidate, or git checkout the artifact and the manifest.

usage: python -X utf8 -B -m training.sheet.train --files <corpus folder> --cache <payload cache folder> --id <experiment id> [--trees 400] [--workers 4]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

WEIGHTS = {"hand": 1.0, "markup": 1.0, "free": 0.25}
THRESHOLD = -0.5    # chosen on dev (validation tabs V and the first blind batch, the train holdout, the gold run): 0 gold 183 lines .881; -0.5 gold 183 lines .933; -0.75 gold 181 lines .940
LEARNING_RATE = 0.08
BINS = 64
SMOOTHING = 5.0
LEAST_WEIGHT = 2.0
FOLDS = 3
LABELS = Path(__file__).with_name("line_labels.json")
EXPERIMENTS = Path(__file__).with_name("data") / "experiments"


def short_hash(name: str) -> str:
    """A name in the labels file: what ties a label to its tab, with no name of a person or a file in the repository."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def expand(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def ranges(rows) -> str:
    """Row numbers as "1-3,7"."""
    parts: list[str] = []
    ordered = sorted(set(rows))
    at = 0
    while at < len(ordered):
        end = at
        while end + 1 < len(ordered) and ordered[end + 1] == ordered[end] + 1:
            end += 1
        parts.append(str(ordered[at]) if end == at else f"{ordered[at]}-{ordered[end]}")
        at = end + 1
    return ",".join(parts)


def payload(files: str, cache: str, name: str) -> dict:
    """A corpus file's wire payload, cached."""
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, re.sub(r"[^\w.-]", "_", name) + ".json.gz")
    if os.path.exists(out):
        with gzip.open(out, "rt", encoding="utf-8") as source:
            return json.load(source)
    from engine.sheet.xlsx_reader import read_workbook
    raw = read_workbook(os.path.join(files, name))
    with gzip.open(out + ".tmp", "wt", encoding="utf-8", compresslevel=3) as sink:
        json.dump(raw, sink)
    os.replace(out + ".tmp", out)
    return raw


def fold_of(file_hash: str) -> int:
    return int(file_hash, 16) % FOLDS


def _tab_examples(job: tuple[str, str, str, list[dict], list[dict] | None]) -> list[tuple[str, list[float], int, int]]:
    """One workbook's labelled lines as (source, feature values, is header, fold), in row order: the first reading's features, or, given first readings fitted without each fold, the second's, each tab scored by the reading that never saw it."""
    from engine.sheet.grid import Grid
    from engine.sheet.line_facts import line_facts
    from engine.sheet.line_model import FEATURES, SECOND_FEATURES, header_score, line_features, second_features
    from engine.sheet.payload import tab_from_json

    files, cache, name, entries, first_models = job
    raw = payload(files, cache, name)
    out: list[tuple[str, list[float], int, int]] = []
    for entry in entries:
        tab = next((t for t in raw["tabs"] if short_hash(t["name"]) == entry["tab"]), None)
        if tab is None:
            continue
        grid = Grid(tab_from_json(tab))
        facts = line_facts(grid)
        features = line_features(facts, grid)
        names = FEATURES
        fold = fold_of(entry["file"])
        if first_models is not None:
            features = second_features(facts, grid, {r: header_score(values, first_models[fold]) for r, values in features.items()})
            names = SECOND_FEATURES
        heads, others = set(expand(entry["head"])), set(expand(entry["other"]))
        for r in sorted(heads | others):
            if r in features:
                out.append((entry["source"], [float(features[r][f]) for f in names], int(r in heads), fold))
    return out


def examples(files: str, cache: str, workers: int, labels_path: Path = LABELS, first_models: list[dict] | None = None) -> tuple[list[str], list[list[float]], list[int], list[int]]:
    """Every labelled line, in the labels file's order, whatever the worker count."""
    labels = json.load(open(labels_path, encoding="utf-8"))
    names = {short_hash(n): n for n in sorted(os.listdir(files))}
    by_file: dict[str, list[dict]] = {}
    for entry in labels["tabs"]:
        by_file.setdefault(entry["file"], []).append(entry)
    jobs = [(files, cache, names[key], entries, first_models) for key, entries in by_file.items() if key in names]
    sources: list[str] = []
    rows: list[list[float]] = []
    targets: list[int] = []
    folds: list[int] = []
    # A pool that loses a worker raises here; multiprocessing.Pool would wait for it forever.
    with ProcessPoolExecutor(workers) as pool:
        for lines in pool.map(_tab_examples, jobs, chunksize=8):
            for source, values, target, fold in lines:
                sources.append(source)
                rows.append(values)
                targets.append(target)
                folds.append(fold)
    return sources, rows, targets, folds


def first_readings(rows, targets, weights, folds, trees: int, feature_names) -> list[dict]:
    """One first reading per fold, fitted without that fold: the second reading must learn how far to trust a score on tabs the first never saw."""
    out = []
    for k in range(FOLDS):
        keep = [at for at, fold in enumerate(folds) if fold != k]
        out.append(fit([rows[at] for at in keep], [targets[at] for at in keep], [weights[at] for at in keep], trees, feature_names))
    return out


def _bins(column, limit: int = BINS):
    """A feature's candidate cuts and each line's bin: a value at or below cuts[i] and above cuts[i - 1] is bin i, the serving rule."""
    import numpy as np

    values = np.unique(column)
    if len(values) > limit:
        values = np.unique(np.quantile(column, np.linspace(0, 1, limit + 1)[1:-1], method="lower"))
    cuts = values[:-1] if len(values) > 1 else values[:0]
    return cuts, np.searchsorted(cuts, column, side="left")


def fit(rows, targets, weights, trees: int, feature_names, learning_rate: float = LEARNING_RATE, smoothing: float = SMOOTHING, least_weight: float = LEAST_WEIGHT) -> dict:
    """Boosted stumps by Newton steps on the logistic loss, kept as one step function per named feature. Plain numpy with no random draw: the same rows give the same artifact byte for byte. smoothing and least_weight keep a stump off a handful of lines: nothing here can learn a step from one odd cell."""
    import numpy as np

    x = np.asarray(rows, dtype=np.float64)
    y = np.asarray(targets, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    prior = float((w * y).sum() / w.sum())
    base = math.log(prior / (1 - prior))
    binned = [_bins(x[:, j]) for j in range(x.shape[1])]
    tables = [np.zeros(len(cuts) + 1) for cuts, _ in binned]
    score = np.full(len(y), base)
    for _ in range(trees):
        p = 1 / (1 + np.exp(-score))
        g = w * (p - y)
        h = w * p * (1 - p)
        best = (0.0, -1, -1, 0.0, 0.0)
        for j, (cuts, at) in enumerate(binned):
            if not len(cuts):
                continue
            gs = np.cumsum(np.bincount(at, g, len(cuts) + 1))
            hs = np.cumsum(np.bincount(at, h, len(cuts) + 1))
            gl, hl = gs[:-1], hs[:-1]
            gr, hr = gs[-1] - gl, hs[-1] - hl
            gain = gl * gl / (hl + smoothing) + gr * gr / (hr + smoothing) - gs[-1] * gs[-1] / (hs[-1] + smoothing)
            gain[(hl < least_weight) | (hr < least_weight)] = -1.0
            b = int(np.argmax(gain))
            if gain[b] > best[0] + 1e-12:
                best = (float(gain[b]), j, b, float(-gl[b] / (hl[b] + smoothing)), float(-gr[b] / (hr[b] + smoothing)))
        _, j, b, left, right = best
        if j < 0:
            break
        at = binned[j][1]
        tables[j][:b + 1] += learning_rate * left
        tables[j][b + 1:] += learning_rate * right
        score += learning_rate * np.where(at <= b, left, right)
    steps = {}
    for j, table in enumerate(tables):
        if not np.any(table):
            continue
        cuts, values = [], [round(float(table[0]), 6)]
        for b in range(1, len(table)):
            value = round(float(table[b]), 6)
            if value != values[-1]:
                cuts.append(round(float(binned[j][0][b - 1]), 6))
                values.append(value)
        if cuts:
            steps[feature_names[j]] = {"cuts": cuts, "values": values}
    return {"base": round(base, 6), "threshold": THRESHOLD, "steps": steps}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--trees", type=int, default=800)
    parser.add_argument("--second-trees", type=int, default=400)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    from engine.sheet.line_model import FEATURES, SECOND_FEATURES

    sources, rows, targets, folds = examples(args.files, args.cache, args.workers)
    print(f"examples {len(rows)} positives {sum(targets)} by source " + str({s: sources.count(s) for s in sorted(set(sources))}))
    weights = [WEIGHTS[s] for s in sources]
    artifact = fit(rows, targets, weights, args.trees, FEATURES)
    held_out = first_readings(rows, targets, weights, folds, args.trees, FEATURES)
    sources, rows, targets, _ = examples(args.files, args.cache, args.workers, first_models=held_out)
    second = fit(rows, targets, [WEIGHTS[s] for s in sources], args.second_trees, SECOND_FEATURES)
    artifact["second"] = {"base": second["base"], "steps": second["steps"]}
    out = EXPERIMENTS / args.id
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "line_model.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(artifact, sink, separators=(",", ":"))
    print("wrote", out / "line_model.json")


if __name__ == "__main__":
    main()
