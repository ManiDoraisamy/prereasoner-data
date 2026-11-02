"""Train the line model: gradient-boosted trees for "this line is a header", on train-family workbooks only (design/corpora_family_map.json, seed fixed). Written experiment contract:

Hypothesis: a small boosted model over named line coordinates finds header blocks on report-shaped tabs better than the rules, which stall at 40 of 57 tabs.
Frozen baseline: the rule version of the header decision, recorded in design/phase1_build.txt (blind batch B: precision .90, recall .72, first header block 18 of 22).
Training data: hand labels on train-family tabs (labels.py; batch A's train-side tabs), plus free labels from train-family workbooks (autofilter top and last frozen row are headers; a formula total and the lines under an autofilter top are not), weighted lower. No frozen, validation or studied workbook; batch B's tabs left out.
Objective: logistic loss. Seed 20261103. No GPU, no paid call, under a minute on CPU.
Acceptance: beats the rules on batch A's non-train tabs and the silver markup, then on batch B once, blind. Result: kept at 300 stumps, one step function per named feature; trees of depth 2 and 3 scored no better on dev (design/phase1_build.txt).
Output: engine/sheet/line_model.json only when asked with --keep (the branch's one artifact; it reaches production only at the cut-over); else the working file named by --out.

usage: python -X utf8 -B -m training.sheet.train --design <evals/enron/design> [--workbooks 500] [--out model.json] [--keep]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys

SEED = 20261103
HAND_WEIGHT = 1.0
FREE_WEIGHT = 0.25
THRESHOLD = 0.0      # chosen on dev (gold rows, batch A non-train tabs, silver); -0.5 and -1 lose precision and gain little


def _examples(design: str, workbooks: int):
    sys.path.insert(0, design)
    import phase1_common as common  # the evaluation's split and payload cache, outside this repository
    from sheet_signals_hand_labels import LABELS, roles as hand_roles

    from engine.sheet.grid import Grid
    from engine.sheet.line_model import FEATURES, line_features
    from engine.sheet.line_facts import line_facts
    from engine.sheet.line_roles import printed_lines
    from engine.sheet.payload import tab_from_json
    from training.sheet.labels import LABELS_T, rows_of

    side, family = common.side_table()
    rows, targets, weights, groups = [], [], [], []
    tabs = json.load(open(f"{common.DESIGN}phase1_label_tabs_T.json", encoding="utf-8"))

    def add(tab, labelled: dict[int, bool], weight: float, group: str) -> None:
        grid = Grid(tab_from_json(tab))
        facts = line_facts(grid)
        features = line_features(facts, grid)
        for r in sorted(labelled):
            if r in features:
                rows.append([float(features[r][name]) for name in FEATURES])
                targets.append(int(labelled[r]))
                weights.append(weight)
                groups.append(group)

    for index, labels in sorted(LABELS_T.items()):
        name, title = tabs[index]
        assert side(name) == "train"
        tab = common.find_tab(common.payload(common.FILES + name), title)
        add(tab, {r: code == "H" for r, code in rows_of(labels).items()}, HAND_WEIGHT, family[name])
    items, _ = common.hand_items(side)
    used = set()
    for batch, index, name, title, s in items:
        used.add(name)
        if batch == "A" and index in LABELS and s == "train":
            tab = common.find_tab(common.payload(common.FILES + name), title)
            truth = {r: role == "header" for r, role in hand_roles(index).items() if role is not None}
            add(tab, truth, HAND_WEIGHT, family[name])
    # Free labels: what the author marked.
    by_family: dict[str, list[str]] = {}
    for name in sorted(family):
        if side(name) == "train" and name not in used:
            by_family.setdefault(family[name], []).append(name)
    rng = random.Random(SEED)
    names = [rng.choice(by_family[f]) for f in sorted(by_family)]
    rng.shuffle(names)
    import os
    taken = 0
    for name in names:
        if taken >= workbooks:
            break
        if os.path.getsize(common.FILES + name) > 1_500_000:
            continue
        taken += 1
        raw = common.payload(common.FILES + name)
        for tab in raw["tabs"]:
            if not tab.get("rows"):
                continue
            payload = tab_from_json(tab)
            top = payload.autofilter[0] if payload.autofilter else None
            frozen = payload.frozen_rows or None
            if top is None and frozen is None:
                continue
            grid = Grid(payload)
            facts = line_facts(grid)
            head = top or frozen
            if head not in facts or facts[head].filled < 2 or facts[head].real_figures * 3 > facts[head].filled:
                continue
            labelled = {head: True}
            below = [r for r in sorted(facts) if r > head][1:30]
            labelled.update({r: False for r in below})
            labelled.update({r: False for r in printed_lines(facts)})
            labelled[head] = True
            add(tab, labelled, FREE_WEIGHT, family[name])
    return rows, targets, weights, groups, taken


def _export(model, feature_names) -> dict:
    """Depth-one trees sum to one step function per feature: the cuts a feature's stumps split at, and the summed value of each stretch between them."""
    prior = model.init_.class_prior_[1]
    stumps: dict[int, list[tuple[float, float, float]]] = {}
    for estimator in model.estimators_[:, 0]:
        tree = estimator.tree_
        if tree.children_left[0] < 0:
            continue
        stumps.setdefault(int(tree.feature[0]), []).append((float(tree.threshold[0]), float(tree.value[tree.children_left[0], 0, 0]), float(tree.value[tree.children_right[0], 0, 0])))
    steps = {}
    for index in sorted(stumps):
        cuts = sorted({round(t, 6) for t, _, _ in stumps[index]})
        values = []
        for at in range(len(cuts) + 1):
            # A value in this stretch sits at or below cuts[at] and above cuts[at - 1].
            probe = cuts[at] if at < len(cuts) else cuts[-1] + 1.0
            values.append(round(model.learning_rate * sum(left if probe <= round(t, 6) else right for t, left, right in stumps[index]), 6))
        steps[feature_names[index]] = {"cuts": cuts, "values": values}
    return {"base": round(math.log(prior / (1 - prior)), 6), "threshold": THRESHOLD, "steps": steps}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", default="C:/work/prereasoner-readers-artifacts/evals/enron/design/")
    parser.add_argument("--workbooks", type=int, default=500)
    parser.add_argument("--out", default="")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--trees", type=int, default=300)
    args = parser.parse_args()
    from sklearn.ensemble import GradientBoostingClassifier

    from engine.sheet.line_model import ARTIFACT, FEATURES

    rows, targets, weights, groups, taken = _examples(args.design, args.workbooks)
    print(f"examples {len(rows)} positives {sum(targets)} families {len(set(groups))} free-label workbooks read {taken}")
    model = GradientBoostingClassifier(n_estimators=args.trees, max_depth=1, learning_rate=0.08, subsample=1.0, random_state=SEED)
    model.fit(rows, targets, sample_weight=weights)
    artifact = _export(model, FEATURES)
    path = ARTIFACT if args.keep else args.out
    with open(path, "w", encoding="utf-8", newline="\n") as sink:
        json.dump(artifact, sink, separators=(",", ":"))
    print("wrote", path)


if __name__ == "__main__":
    main()
