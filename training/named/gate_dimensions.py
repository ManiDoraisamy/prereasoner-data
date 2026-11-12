"""The gate of the named dimensions: does each basis property read correctly on held-out examples, role by role.

For every basis property and each role it is read in (a column, a question's word linked to a column or value, a corpus
table's profile): a threshold is the lowest reading whose precision on calibration units is MIN_PRECISION, and on units
it did not calibrate on the precision and recall at that threshold are measured. One truth sets and judges a threshold.
For sheets that is the second pass's multi-label truth on the factory's val side, whose families no run trained on: the
val families fall in FOLDS folds, and each fold's units are read at the threshold the other folds' units set, so every
val unit is judged once and never by a threshold its own reading set. (The first pass's single labels cannot calibrate:
its "none" turns off every property on a column the second pass often gives one, so a threshold set there fell far from
the precision the second pass then measured: the anchoring bundle's price column read 0.73 against 0.99 at one reading.)
For profiles, the corpus's validation split calibrates and its test split judges. A dimension passes a role with at
least MIN_HELD judged positives and precision of at least MIN_PRECISION; with fewer positives it is not passed: reported
as lacking evidence. A dimension qualifies for links when it passes both as a column and as a word, since a link needs
both sides. Unknown labels are never counted either way (training/named/train.py:meaning_targets, the same masks as
training).

Classes are not fitted: a corpus table's class is read back from its property profile through count-made signatures
(training/schema_org/signatures.py:_signature_candidates on the train split, the basis only), and the share of held-out
tables whose own class comes first is reported per class.

Writes <bundle>/../gate.json and prints the table: names, counts and rates only.
usage: python -m training.named.gate_dimensions --bundle DIR --examples <train examples, for co_held.json>
       --val-examples <val examples> [--second-pass audit_second_val] [--corpus semantic_instances.jsonl]"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import numpy as np
import torch

from engine.named.bundle import read_bundle
from engine.named.names import MEANING_PREFIX
from engine.named.units import T_COLUMN
from training.named.data import TabExample, family_share, read_examples  # noqa: F401 - the shards name TabExample under the module that wrote them, as train.py's own import serves
from training.named.train import THRESHOLD, build_batch, co_held_matrix, meaning_targets, profile_logits, read_corpus, run_batch

MIN_PRECISION = 0.95       # main's property gate (training/schema_org/train_property_head.py:MIN_PROPERTY_PRECISION)
MIN_HELD = 25              # held-out positives a dimension needs to be judged (main's support rule)
MIN_CALIBRATION = 10       # predicted positives a threshold must rest on
ROLES = ("column", "word", "profile")
FOLDS = 2                  # the val families' folds: each fold is judged at the threshold the others set
FOLD_SEED = 2              # family_share's seed for the folds


def second_pass_labels(journal: str, names) -> dict[tuple[str, str], dict]:
    """(tab key, '<table>.<position>') -> a label in the examples' format holding every listed basis property the second pass gave at the threshold: the held-out side's multi-label truth."""
    import re
    from training.factory.factory import read_journal
    line = re.compile(r"^([tfn]\d+)\.(\d+)\s+(.*)$")
    out = {}
    for key, entry in read_journal(journal).items():
        for raw in (entry.get("reply") or "").splitlines():
            match = line.match(raw.strip().strip("`*- "))
            if not match:
                continue
            parts = match[3].split()
            if parts and parts[0].lower() == "none":
                held = []
            else:
                held = [f"{MEANING_PREFIX}{parts[i].removeprefix('schema:')}" for i in range(0, len(parts) - 1, 2) if parts[i + 1].isdigit() and int(parts[i + 1]) >= THRESHOLD]
            held = [name for name in held if names.at(name) is not None]
            out[(key.split("|", 1)[1], f"{match[1]}.{match[2]}")] = {"coordinates": held, "raw": "none" if not held and parts and parts[0].lower() == "none" else "second pass", "conf": 9 if held or (parts and parts[0].lower() == "none") else 0}
    return out


def sheet_readings(bundle, examples, co_held, seed: int = 0) -> dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray]]]:
    """Per role, (readings, targets, masks) over the units whose meaning is known: every column of every tab, and every word a question links to a column or a value."""
    names, config = bundle.names, bundle.config
    out = {"column": [], "word": []}
    rng = random.Random(seed)
    with torch.no_grad():
        for example in sorted(examples, key=lambda item: item.key):
            batch = build_batch(example, config, names, rng, questions=64, shares=None)
            if batch is None or not batch.targets:
                continue
            read = run_batch(bundle.backbone, bundle.reader, config, batch, "cpu")
            targets = read.target_read(bundle.reader)[:, names.meaning_start:]
            columns = [at for at, target in enumerate(batch.targets) if target.kind == T_COLUMN]
            rows, wanted, masks = meaning_targets([batch.meaning[at] for at in columns], names, co_held, "cpu")
            if rows:
                out["column"].append((targets[[columns[row] for row in rows]].numpy(), wanted.numpy(), masks.numpy()))
            for question, labelled in enumerate(batch.word_positive):
                rows, wanted, masks = meaning_targets(labelled, names, co_held, "cpu")
                if rows:
                    out["word"].append((read.word_read(bundle.reader, question)[rows][:, names.meaning_start:].numpy(), wanted.numpy(), masks.numpy()))
    return out


def profile_readings(bundle, instances, batch: int = 64) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    names = bundle.names
    out = []
    with torch.no_grad():
        for start in range(0, len(instances), batch):
            part = instances[start:start + batch]
            read = torch.sigmoid(profile_logits(bundle.backbone, bundle.reader, bundle.config, part))
            _, wanted, masks = meaning_targets([instance.properties for instance in part], names, None, "cpu")
            out.append((read.numpy(), wanted.numpy(), masks.numpy()))
    return out


def stacked(parts) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not parts:
        empty = np.zeros((0, 0))
        return empty, empty, empty
    return tuple(np.concatenate([part[at] for part in parts]) for at in range(3))


def threshold_of(scores: np.ndarray, truth: np.ndarray) -> float | None:
    """The lowest reading at which the calibration units' precision is MIN_PRECISION on at least MIN_CALIBRATION predicted positives; None when no reading gives it."""
    order = np.argsort(-scores, kind="stable")
    hits = np.cumsum(truth[order])
    taken = np.arange(1, len(order) + 1)
    fine = (hits / taken >= MIN_PRECISION) & (taken >= MIN_CALIBRATION)
    # only at the last of a run of equal readings is the cut a threshold
    last = np.r_[scores[order][1:] != scores[order][:-1], True] if len(order) else np.zeros(0, dtype=bool)
    at = np.flatnonzero(fine & last)
    return float(scores[order][at[-1]]) if len(at) else None


def judge(pairs, names) -> dict[str, dict]:
    """Per property, over (calibration, judged) pairs of units: each pair's judged units read at the threshold its calibration
    units set, then the judged positives, precision and recall over every pair. A property passes only with a threshold on
    every pair."""
    out = {}
    for at, name in enumerate(names.names[names.meaning_start:]):
        row = {"calibration_positives": 0, "thresholds": [], "held_positives": 0, "held_predicted": 0, "precision": None, "recall": None, "passes": False, "why": "no calibration units"}
        said, truth = [], []
        for (c_scores, c_truth, c_mask), (h_scores, h_truth, h_mask) in pairs:
            threshold = None
            if c_scores.size:
                known = c_mask[:, at] > 0
                row["calibration_positives"] += int(c_truth[known, at].sum())
                threshold = threshold_of(c_scores[known, at], c_truth[known, at])
            row["thresholds"].append(threshold)
            if h_scores.size:
                known = h_mask[:, at] > 0
                truth.append(h_truth[known, at] > 0)
                said.append(h_scores[known, at] >= threshold if threshold is not None else np.zeros(int(known.sum()), dtype=bool))
        truth = np.concatenate(truth) if truth else np.zeros(0, dtype=bool)
        said = np.concatenate(said) if said else np.zeros(0, dtype=bool)
        row["held_positives"] = int(truth.sum())
        complete = bool(row["thresholds"]) and all(threshold is not None for threshold in row["thresholds"])
        if complete:
            row["held_predicted"] = int(said.sum())
            right = int((said & truth).sum())
            row["precision"] = round(right / int(said.sum()), 4) if said.any() else None
            row["recall"] = round(right / max(1, int(truth.sum())), 4)
        if not complete:
            row["why"] = "no threshold reaches the precision on the calibration units"
        elif row["held_positives"] < MIN_HELD:
            row["why"] = f"fewer than {MIN_HELD} held-out positives: not judged"
        elif row["precision"] is None or row["precision"] < MIN_PRECISION:
            row["why"] = "held-out precision under the gate"
        else:
            row["passes"], row["why"] = True, ""
        out[name] = row
    return out


def folded(parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]]) -> list[tuple]:
    """(calibration, judged) pairs from folds of units: each fold judged by the others."""
    return [(stacked([part for other, part in enumerate(parts) if other != at and part[0].size]), part) for at, part in enumerate(parts)]


def class_reconstruction(bundle, corpus) -> dict:
    """Each held-out table's class read back from its property profile through count-made signatures, nothing fitted: a class's signature is the basis properties its train tables show often and distinctively (training/schema_org/signatures.py:_signature_candidates on the corpus's train split), a class's score the weighted share of its signature the profile holds, and the class read back the highest score. Single-class tables; classes with the signatures module's own support."""
    from collections import Counter
    from training.schema_org.signatures import MIN_TRAIN, MIN_VALIDATION, _signature_candidates
    names = bundle.names
    position = {name: at for at, name in enumerate(names.names[names.meaning_start:])}
    train = [instance for instance in corpus if instance.split == "train" and len(instance.classes) == 1]
    seen = Counter(name for instance in train for name in instance.properties)
    members = defaultdict(list)
    for instance in train:
        members[instance.classes[0]].append(instance)
    validation = Counter(instance.classes[0] for instance in corpus if instance.split == "validation" and len(instance.classes) == 1)
    signatures = {}
    for name in sorted(members):
        if len(members[name]) < MIN_TRAIN or validation[name] < MIN_VALIDATION:
            continue
        candidates = _signature_candidates(Counter(prop for instance in members[name] for prop in instance.properties), len(members[name]), seen, len(train), ())
        if len(candidates) >= 2:
            signatures[name] = candidates
    classes = sorted(signatures)
    test = [instance for instance in corpus if instance.split == "test" and len(instance.classes) == 1 and instance.classes[0] in signatures]
    if not classes or not test:
        return {"classes": len(classes), "held_out_tables": len(test)}
    weights = np.zeros((len(classes), len(position)))
    for at, name in enumerate(classes):
        for row in signatures[name]:
            weights[at, position[row["property"]]] = row["weight"]
        weights[at] /= weights[at].sum()
    read = np.concatenate([part[0] for part in profile_readings(bundle, test)])
    said = (read @ weights.T).argmax(axis=1)
    counts = defaultdict(lambda: [0, 0])
    for instance, at in zip(test, said.tolist()):
        counts[instance.classes[0]][0] += classes[at] == instance.classes[0]
        counts[instance.classes[0]][1] += 1
    return {"classes": len(classes), "held_out_tables": len(test), "own_class_first": sum(right for right, _ in counts.values()),
            "per_class": {name: {"own_class_first": right, "tables": n, "signature": [row["property"][len(MEANING_PREFIX):] for row in signatures[name]]} for name, (right, n) in sorted(counts.items())}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--examples", required=True, help="the training examples: their co_held.json")
    parser.add_argument("--val-examples", required=True, help="the factory's val side: the units every threshold is set and judged on, by fold")
    parser.add_argument("--second-pass", default="audit_second_val")
    parser.add_argument("--corpus")
    parser.add_argument("--limit-tabs", type=int)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    torch.set_num_threads(args.threads)
    bundle = read_bundle(args.bundle)
    names = bundle.names
    co = Path(args.examples) / "co_held.json"
    co_held = co_held_matrix(co, names, "cpu") if co.exists() else None
    truth = second_pass_labels(args.second_pass, names)
    held = []
    for example in read_examples(args.val_examples)[: args.limit_tabs]:
        labels = {key: truth[(example.key, key)] for key in example.labels if (example.key, key) in truth}
        held.append(replace(example, labels=labels))
    folds = [[example for example in held if int(family_share(example.family, FOLD_SEED) * FOLDS) == at] for at in range(FOLDS)]
    report = {"bundle": args.bundle, "min_precision": MIN_PRECISION, "min_held_positives": MIN_HELD, "truth": args.second_pass, "fold_tabs": [len(fold) for fold in folds], "fold_seed": FOLD_SEED, "roles": {}}
    read = [sheet_readings(bundle, fold, co_held) for fold in folds]
    for role in ("column", "word"):
        report["roles"][role] = judge(folded([stacked(part[role]) for part in read]), names)
    if args.corpus:
        corpus = read_corpus(args.corpus, names)
        report["roles"]["profile"] = judge([(stacked(profile_readings(bundle, [i for i in corpus if i.split == "validation"])), stacked(profile_readings(bundle, [i for i in corpus if i.split == "test"])))], names)
        report["classes"] = class_reconstruction(bundle, corpus)
    link = [name for name in names.names[names.meaning_start:] if report["roles"]["column"][name]["passes"] and report["roles"]["word"][name]["passes"]]
    report["qualified_for_links"] = link
    for role, rows in report["roles"].items():
        passed = [name for name, row in rows.items() if row["passes"]]
        judged = [name for name, row in rows.items() if row["held_positives"] >= MIN_HELD]
        print(f"{role}: {len(passed)} pass of {len(judged)} with {MIN_HELD}+ held-out positives ({len(rows)} properties)")
        for name, row in sorted(rows.items(), key=lambda item: -item[1]["held_positives"]):
            if row["held_positives"] or row["calibration_positives"]:
                print(f"  {name[len(MEANING_PREFIX):]}: held-out positives {row['held_positives']} thresholds {[None if threshold is None else round(threshold, 3) for threshold in row['thresholds']]} precision {row['precision']} recall {row['recall']} {'PASS' if row['passes'] else row['why']}")
    print("qualified for links (pass as a column and as a word):", [name[len(MEANING_PREFIX):] for name in link])
    if "classes" in report:
        print("classes read back from the property profile:", {key: value for key, value in report["classes"].items() if key != "per_class"})
    Path(args.bundle).parent.joinpath("gate.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
