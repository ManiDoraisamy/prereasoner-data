"""The chooser's fit, on families the reader never trained on: run the engine with the prior chooser, keep every candidate's named evidence and whether its answer is right (enron_grade), fit a logistic model on one part of those families, calibrate it per form on another, set each form's answer threshold there. The decoder's slot weights are fit the same way on what each candidate's coordinates sum to per slot.

numpy Newton steps, a fixed count of them, no random draw: the same records give the same bytes. Candidates only: the result goes into a bundle under the experiment's folder.

usage: NAMED_PIN=1 python -m training.named.choose --bundle <dir> --side train --families <out>/held_families.json --seed 0 --out <new bundle dir> [--limit N]
"""
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from engine.named.chooser import FEATURES, PRIOR, Chooser, score_candidates
from engine.named.bundle import BUNDLE_META, BUNDLE_STATE
from engine.named.pipeline import NamedEngine, load_bundle
from engine.program.language import form_of
from engine.program.names import FORMS, SLOTS
from engine.program.scoring import Weights
from engine.program.verifier import verify
from training.named.data import family_share
from training.named.evaluate import Item, Runner, load_factory_set

L2 = 1.0
NEWTON_STEPS = 25
MIN_CALIBRATION = 30
TARGET_WRONG = 0.10
THRESHOLDS = tuple(round(0.05 * step, 2) for step in range(1, 20))
MARGINS = (0.0, 0.01, 0.02, 0.05, 0.1, 0.2)


@dataclass
class CandidateRecord:
    """One candidate's evidence and verdict. question ties candidates of one question; family keeps fit and calibration apart."""
    question: str
    family: str
    form: str
    features: dict[str, float]
    slots: dict[str, float]
    right: bool
    rank: int
    answer: str = ""


def answer_key(rows) -> str:
    """Two candidates with this key say the same thing."""
    return json.dumps([[round(cell, 6) if isinstance(cell, float) else cell for cell in row] for row in rows], default=str)


def collect(items: list[Item], engine: NamedEngine, *, k: int = 50, progress: bool = False) -> list[CandidateRecord]:
    """Every gold question's candidates through the verifier, each with its named evidence and whether its answer is right."""
    runner = Runner(engine)
    out = []
    for at, item in enumerate(items):
        if item.label != "gold":
            continue
        prepared = runner.prepare(item)
        named = engine.read(item.question, prepared)
        decoded = prepared.decoder.decode(named.reading, k=k, weights=engine.slot_weights)
        verified = verify(decoded.programs, prepared.tables, named.expected, top=k)
        for candidate in score_candidates(verified, named, prepared.catalog, PRIOR):
            rows = candidate.rows
            slots: dict[str, float] = {}
            for term in candidate.verified.scored.contributions:
                slots[term.slot] = slots.get(term.slot, 0.0) + term.score
            out.append(CandidateRecord(item.id, item.family, form_of(candidate.verified.scored.program), dict(candidate.features), slots, bool(rows) and item.grade(list(rows)), candidate.rank, answer_key(rows)))
        if progress and (at + 1) % 50 == 0:
            print(f"collected {at + 1} of {len(items)}", flush=True)
    runner.close()
    return out


def fit_logistic(matrix: np.ndarray, labels: np.ndarray, *, l2: float = L2, steps: int = NEWTON_STEPS) -> tuple[np.ndarray, float]:
    """(weights, bias) by Newton's method on the L2-penalized log loss. The bias is not penalized. Deterministic: linear solves, a fixed number of steps."""
    rows, columns = matrix.shape
    design = np.hstack([matrix, np.ones((rows, 1))])
    theta = np.zeros(columns + 1)
    penalty = np.eye(columns + 1) * l2
    penalty[-1, -1] = 0.0
    for _ in range(steps):
        logits = np.clip(design @ theta, -30.0, 30.0)
        p = 1.0 / (1.0 + np.exp(-logits))
        weights = p * (1.0 - p) + 1e-9
        gradient = design.T @ (p - labels) + penalty @ theta
        hessian = (design * weights[:, None]).T @ design + penalty + np.eye(columns + 1) * 1e-6
        theta = theta - np.linalg.solve(hessian, gradient)
    return np.round(theta[:-1], 6), round(float(theta[-1]), 6)


def matrix_of(records: Sequence[CandidateRecord], names: Sequence[str] = FEATURES) -> np.ndarray:
    return np.asarray([[record.features.get(name, 0.0) for name in names] for record in records], dtype=np.float64).reshape(len(records), len(names))


def calibrate(logits: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    """Platt scaling: (a, b). Too few rows, or a single class, keeps the identity."""
    if len(logits) < MIN_CALIBRATION or labels.min() == labels.max():
        return (1.0, 0.0)
    weights, bias = fit_logistic(logits[:, None], labels, l2=1e-3)
    if weights[0] <= 0:                       # too little held data to say more than the logit does: a calibration never turns it upside down
        return (1.0, 0.0)
    return (round(float(weights[0]), 6), round(float(bias), 6))


def choose_thresholds(best: Sequence[tuple[str, float, bool]], target_wrong: float = TARGET_WRONG) -> dict[str, float]:
    """For each form, the lowest threshold whose answered questions are at most target_wrong wrong, from each question's chosen candidate (form, probability, right). None reaching it leaves the top one."""
    out = {}
    for form in FORMS:
        rows = [(p, right) for held, p, right in best if held == form]
        chosen = THRESHOLDS[-1]
        for threshold in THRESHOLDS:
            answered = [right for p, right in rows if p >= threshold]
            if answered and sum(not right for right in answered) / len(answered) <= target_wrong:
                chosen = threshold
                break
        out[form] = chosen
    return out


def fit_chooser(fit: Sequence[CandidateRecord], held: Sequence[CandidateRecord]) -> Chooser:
    """Weights on fit, calibration and thresholds on held. Fit and held must not share a family."""
    shared = {record.family for record in fit} & {record.family for record in held}
    if shared:
        raise ValueError(f"{len(shared)} families both fit and calibrate")
    matrix = matrix_of(fit)
    labels = np.asarray([record.right for record in fit], dtype=np.float64)
    weights, bias = fit_logistic(matrix, labels)
    fitted = dict(zip(FEATURES, (float(value) for value in weights)))
    logits_of = lambda rows: matrix_of(rows) @ weights + bias
    calibration = {}
    for form in FORMS:
        rows = [record for record in held if record.form == form]
        if rows:
            calibration[form] = calibrate(logits_of(rows), np.asarray([record.right for record in rows], dtype=np.float64))
    chooser = Chooser(fitted, bias, calibration)
    questions: dict[str, list[tuple[float, int, CandidateRecord]]] = {}
    for record, logit in zip(held, logits_of(list(held)).tolist()):
        questions.setdefault(record.question, []).append((round(logit, 4), record.rank, record))
    ranked = [sorted(rows, key=lambda row: (-row[0], row[1])) for rows in questions.values()]
    thresholds = choose_thresholds([(rows[0][2].form, chooser.probability(rows[0][0], rows[0][2].form), rows[0][2].right) for rows in ranked])
    return Chooser(fitted, bias, calibration, thresholds, choose_margin(ranked, chooser, thresholds))


def choose_margin(ranked: list[list[tuple[float, int, CandidateRecord]]], chooser: Chooser, thresholds: dict[str, float], target_wrong: float = TARGET_WRONG) -> float:
    """How close another answer may sit before the reading is asked about: the margin with the most right among those keeping answered questions at most target_wrong wrong, else the widest. Ties go to the wider."""
    rows = []
    for candidates in ranked:
        logit, _, best = candidates[0]
        p = chooser.probability(logit, best.form)
        other = next((chooser.probability(row[0], row[2].form) for row in candidates[1:] if row[2].answer != best.answer), None)
        rows.append((p, p - other if other is not None else 1.0, best.right, thresholds.get(best.form, 0.5)))
    chosen, most = MARGINS[-1], -1
    for margin in MARGINS:
        answered = [right for p, gap, right, threshold in rows if p >= threshold and gap >= margin]
        wrong = sum(not right for right in answered)
        if answered and wrong / len(answered) <= target_wrong and sum(answered) >= most:
            chosen, most = margin, sum(answered)
    return chosen


def fit_slot_weights(records: Sequence[CandidateRecord]) -> Weights:
    """The decoder's slot weights, from each candidate's per-slot sum of readings. Clipped above zero: a slot that argued against the right program is read as silent, never inverted."""
    matrix = np.asarray([[record.slots.get(slot, 0.0) for slot in SLOTS] for record in records], dtype=np.float64).reshape(len(records), len(SLOTS))
    labels = np.asarray([record.right for record in records], dtype=np.float64)
    weights, _ = fit_logistic(matrix, labels)
    return Weights({slot: round(float(max(value, 0.0)), 6) for slot, value in zip(SLOTS, weights)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit the chooser on held-out families.")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--side", default="train")
    parser.add_argument("--families", required=True, help="a JSON list of the families the reader never trained on")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True, help="a new bundle folder under the experiment: the reader's weights copied, this fit beside them")
    args = parser.parse_args()
    families = json.loads(Path(args.families).read_text(encoding="utf-8"))
    items = load_factory_set(args.side, limit=args.limit, families=families)
    engine = load_bundle(args.bundle)
    records = collect(items, engine, progress=True)
    fit = [record for record in records if family_share(record.family, args.seed + 1) >= 0.5]
    held = [record for record in records if family_share(record.family, args.seed + 1) < 0.5]
    chooser = fit_chooser(fit, held)
    slot_weights = fit_slot_weights(fit)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(args.bundle) / BUNDLE_STATE, out / BUNDLE_STATE)
    document = json.loads((Path(args.bundle) / BUNDLE_META).read_text(encoding="utf-8"))
    document.update(chooser=chooser.record(), slot_weights=dict(sorted(slot_weights.by_slot.items())))
    (out / BUNDLE_META).write_text(json.dumps(document, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"questions": len({record.question for record in records}), "candidates": len(records), "right": sum(record.right for record in records), "fit_families": len({record.family for record in fit}), "held_families": len({record.family for record in held}), "calibration": dict(chooser.calibration), "thresholds": dict(chooser.thresholds)}, sort_keys=True), flush=True)


__all__ = ["CandidateRecord", "answer_key", "calibrate", "choose_margin", "choose_thresholds", "collect", "fit_chooser", "fit_logistic", "fit_slot_weights"]

if __name__ == "__main__":
    main()
