"""The chooser's fit, on families the reader never trained on: run the engine with the prior chooser, keep every candidate's named evidence and whether its answer is right (enron_grade), fit a multinomial logistic model among each question's candidates on one part of those families, calibrate the chosen candidate per form on another, set each form's answer threshold and the margin there. The decoder's slot weights are fit the same way on what each candidate's coordinates sum to per slot.

numpy Newton steps, a fixed count of them, no random draw: the same records give the same bytes. Candidates only: the result goes into a bundle under the experiment's folder.

The chooser's weights come from cross-folds of the train families the reader never saw (fold_report says how they hold up out of fold). Calibration, thresholds and margin come from validation folds (cross_fit): a validation question is decided by the fit of the folds it is not in, so Gate A never reads a threshold set on its own answer.

usage: python -m training.named.choose --bundle <dir> --side train,pub_eia,pub_sec --families <out>/held_families.json --seed 0 --out <new bundle dir> [--limit N]
"""
from __future__ import annotations

import argparse
import gzip
import json
import pickle
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

import numpy as np

from engine.named.chooser import ANSWER, ASK, ASK_NOTHING, FEATURES, PRIOR, Chooser, reasons_to_ask, score_candidates
from engine.named.reader import unsupported_links
from engine.named.bundle import BUNDLE_META, BUNDLE_STATE
from engine.named.pipeline import NamedEngine, load_bundle
from engine.program.language import TOGETHER, coordinates, form_of
from engine.program.execute import run
from engine.program.names import FORMS, SLOTS, slot_of
from engine.program.scoring import Reading, Weights
from engine.program.verifier import verify
from training.named.data import family_share

L2 = 1.0
NEWTON_STEPS = 25
MIN_CALIBRATION = 30
TARGET_WRONG = 0.12         # of all gold questions, on the folds that set a policy: Gate A's bar is 15% and a fold's estimate is not the bar's
THRESHOLDS = tuple(round(0.05 * step, 2) for step in range(0, 20))
MARGINS = (0.0, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
BIAS_GRID = {"column": (0.0, 0.5, 1.0, 1.9), "value": (0.25, 0.75, 1.25, 2.2), "label": (0.25, 0.75, 1.5)}
WRONG_COSTS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0)
FOLDS = 5
FOLD_SEED = 20261102
ANSWER_ROWS = 50
SUPPORT_NO_LINKS = 0
SUPPORT_ALL = 1
SUPPORT_SOME = 2


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
    support: int = SUPPORT_NO_LINKS

    @property
    def ran(self) -> bool:
        return self.answer not in ("", "[]")


def answer_key(rows) -> str:
    """Two candidates with this key say the same thing."""
    return json.dumps([[round(cell, 6) if isinstance(cell, float) else cell for cell in row] for row in rows[:ANSWER_ROWS]] + ([len(rows)] if len(rows) > ANSWER_ROWS else []), default=str)


def support_of(program, named, catalog) -> int:
    """Whether a program's links stand on named evidence: it has none, all do, or some do not. What the report's count of answers carrying named support reads."""
    links = [name for name in coordinates(program, catalog.column_kind) if name.split(":", 1)[0] in ("measure", "key", "shown", "value", "contains", "line", "section") and TOGETHER not in name]
    if not links:
        return SUPPORT_NO_LINKS
    return SUPPORT_SOME if unsupported_links(program, named, catalog) else SUPPORT_ALL


def records_of(candidates, item, named, catalog) -> list["CandidateRecord"]:
    """One question's scored candidates as records: the evidence, whether the answer is right, what the answer is."""
    out = []
    for candidate in candidates:
        rows = candidate.rows
        slots: dict[str, float] = {}
        for term in candidate.verified.scored.contributions:
            slots[term.slot] = slots.get(term.slot, 0.0) + term.score
        right = bool(rows) and (item.grade(list(rows)) or (item.alternate is not None and item.alternate(list(rows))))
        out.append(CandidateRecord(item.id, item.family, form_of(candidate.verified.scored.program), {name: value for name, value in candidate.features.items() if value}, slots, right, candidate.rank, answer_key(rows), support_of(candidate.verified.scored.program, named, catalog)))
    return out


def decision_of(rows: Sequence["CandidateRecord"], chooser: Chooser) -> tuple[str, "CandidateRecord | None", tuple[str, ...]]:
    """(answer or ask, the record chosen, why it asks): the same rule as answer_or_ask, read from records, so a fold's chooser decides without running anything again."""
    scored = sorted(((round(chooser.logit(row.features)[0], 4), row) for row in rows), key=lambda item: (-item[0], item[1].rank))
    ran = [(logit, row) for logit, row in scored if row.ran]
    if not ran:
        return ASK, None, (ASK_NOTHING,)
    logit, best = ran[0]
    other = next((held for held, row in ran[1:] if row.answer != best.answer), None)
    reasons = reasons_to_ask(chooser.probability(logit, best.form), best.form, logit - other if other is not None else None, bool(best.features.get("needs_asking")), bool(best.features.get("adds_twice")), chooser)
    return (ASK if reasons else ANSWER), best, reasons


def fold_of(family: str, folds: int = FOLDS) -> int:
    return int(family_share(family, FOLD_SEED) * folds)


def cross_fit(base: Chooser, rows: Sequence["CandidateRecord"], folds: int = FOLDS) -> dict[int, Chooser]:
    """base's weights, with calibration, thresholds and margin from every fold but one, per fold. A family stands in one fold."""
    return {fold: calibrated(base, [row for row in rows if fold_of(row.family, folds) != fold]) for fold in range(folds)}


def groups_of(rows: Sequence["CandidateRecord"], names: Sequence[str] = FEATURES) -> list[tuple[np.ndarray, np.ndarray]]:
    """Each question with a right candidate that ran: its candidates' evidence, and that evidence's target, shared among the right ones."""
    by_question: dict[str, list[CandidateRecord]] = {}
    for row in rows:
        if row.ran:
            by_question.setdefault(row.question, []).append(row)
    out = []
    for question in sorted(by_question):
        group = by_question[question]
        right = np.asarray([row.right for row in group], dtype=np.float64)
        if right.sum() and not right.all():
            out.append((matrix_of(group, names), right / right.sum()))
    return out


def fit_conditional(groups: Sequence[tuple[np.ndarray, np.ndarray]], columns: int, *, l2: float = L2, steps: int = NEWTON_STEPS) -> np.ndarray:
    """Weights of a multinomial logistic model among each question's candidates, by damped Newton on the L2-penalized log loss. Deterministic: linear solves, a fixed number of steps, halving a step that does not lower the loss."""
    theta = np.zeros(columns)
    penalty = np.eye(columns) * l2

    def loss_of(values: np.ndarray) -> float:
        total = 0.5 * l2 * float(values @ values)
        for matrix, target in groups:
            scores = matrix @ values
            scores = scores - scores.max()
            total -= float(target @ (scores - np.log(np.exp(scores).sum())))
        return total

    current = loss_of(theta)
    for _ in range(steps):
        gradient = l2 * theta
        hessian = penalty + np.eye(columns) * 1e-6
        for matrix, target in groups:
            scores = matrix @ theta
            scores = scores - scores.max()
            p = np.exp(scores)
            p /= p.sum()
            mean = p @ matrix
            gradient = gradient + mean - target @ matrix
            hessian = hessian + (matrix * p[:, None]).T @ matrix - np.outer(mean, mean)
        step = np.linalg.solve(hessian, gradient)
        size = 1.0
        while size > 1e-3:
            trial = theta - size * step
            value = loss_of(trial)
            if value < current:
                theta, current = trial, value
                break
            size /= 2.0
        else:
            break
    return np.round(theta, 6)


def fold_report(rows: Sequence["CandidateRecord"], folds: int = FOLDS) -> dict:
    """Weights fit on all folds but one, read out of fold on the one left: top-1 right per question, against the prior order. What the chooser's weights are worth on families neither saw."""
    right = base = questions = 0
    for fold in range(folds):
        kept = [row for row in rows if fold_of(row.family, folds) != fold]
        held = [row for row in rows if fold_of(row.family, folds) == fold]
        if not kept or not held:
            continue
        chooser = Chooser(dict(zip(FEATURES, (float(value) for value in fit_conditional(groups_of(kept), len(FEATURES))))))
        by_question: dict[str, list[CandidateRecord]] = {}
        for row in held:
            by_question.setdefault(row.question, []).append(row)
        for group in by_question.values():
            _, best, _ = decision_of(group, chooser)
            ran = [row for row in group if row.ran]
            right += bool(best is not None and best.right)
            base += bool(ran and min(ran, key=lambda row: row.rank).right)
            questions += 1
    return {"questions": questions, "top1_right_chooser": right, "top1_right_decoder_order": base}


def collect(items: list, engine: NamedEngine, *, k: int = 50, progress: bool = False) -> list[CandidateRecord]:
    """Every gold question's candidates through the verifier, each with its named evidence and whether its answer is right."""
    from training.named.evaluate import Runner
    runner = Runner(engine)
    out = []
    for at, item in enumerate(items):
        if item.label != "gold":
            continue
        prepared = runner.prepare(item)
        named = engine.read(item.question, prepared)
        decoded = prepared.decoder.decode(named.reading, k=k, weights=engine.slot_weights)
        verified = verify(decoded.programs, prepared.tables, named.expected, top=k)
        out.extend(records_of(score_candidates(verified, named, prepared.catalog, PRIOR), item, named, prepared.catalog))
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


def choose_thresholds(best: Sequence[tuple[str, float, bool]], cost: float) -> dict[str, float]:
    """For each form, the threshold where right answers less cost x wrong ones is highest, from each question's chosen candidate (form, probability, right). Ties go to the higher threshold."""
    out = {}
    for form in FORMS:
        rows = [(p, right) for held, p, right in best if held == form]
        chosen, most = THRESHOLDS[-1], None
        for threshold in THRESHOLDS:
            answered = [right for p, right in rows if p >= threshold]
            value = sum(answered) - cost * (len(answered) - sum(answered))
            if most is None or value >= most:
                chosen, most = threshold, value
        out[form] = chosen
    return out


def fit_chooser(fit: Sequence[CandidateRecord], held: Sequence[CandidateRecord]) -> Chooser:
    """Weights on fit, calibration and thresholds on held. Fit and held must not share a family."""
    shared = {record.family for record in fit} & {record.family for record in held}
    if shared:
        raise ValueError(f"{len(shared)} families both fit and calibrate")
    fitted = dict(zip(FEATURES, (float(value) for value in fit_conditional(groups_of(fit), len(FEATURES)))))
    return calibrated(Chooser(fitted), held)


def calibrated(base: Chooser, held: Sequence[CandidateRecord]) -> Chooser:
    """base's weights, with each form's calibration, its threshold and the margin set on held."""
    weights = np.asarray([float(base.weights.get(name, 0.0)) for name in FEATURES])
    questions: dict[str, list[tuple[float, int, CandidateRecord]]] = {}
    for record, logit in zip(held, (matrix_of(held) @ weights + base.bias).tolist()):
        if record.ran:
            questions.setdefault(record.question, []).append((round(logit, 4), record.rank, record))
    ranked = [sorted(questions[question], key=lambda row: (-row[0], row[1])) for question in sorted(questions)]
    calibration = {}
    for form in FORMS:
        chosen = [rows[0] for rows in ranked if rows[0][2].form == form]
        if chosen:
            calibration[form] = calibrate(np.asarray([row[0] for row in chosen]), np.asarray([row[2].right for row in chosen], dtype=np.float64))
    chooser = Chooser(base.weights, base.bias, calibration)
    thresholds, margin = choose_policy(ranked, chooser, len({record.question for record in held}))
    return Chooser(base.weights, base.bias, calibration, thresholds, margin)


def choose_policy(ranked: list[list[tuple[float, int, CandidateRecord]]], chooser: Chooser, questions: int, target_wrong: float = TARGET_WRONG) -> tuple[dict[str, float], float]:
    """Each form's threshold and the margin with the most right among those keeping wrong answers at most target_wrong of all questions: a higher cost of a wrong answer raises the thresholds, a wider margin asks about close readings. A candidate that must be asked about stands aside. Ties go to the wider margin and the higher cost."""
    rows = []
    for candidates in ranked:
        logit, _, best = candidates[0]
        if best.features.get("needs_asking") or best.features.get("adds_twice"):
            continue
        other = next((row[0] for row in candidates[1:] if row[2].answer != best.answer), None)
        rows.append((best.form, chooser.probability(logit, best.form), logit - other if other is not None else float("inf"), best.right))
    found: tuple | None = None
    for margin in MARGINS:
        kept = [(form, p, right) for form, p, gap, right in rows if gap >= margin]
        for cost in WRONG_COSTS:
            thresholds = choose_thresholds(kept, cost)
            answered = [right for form, p, right in kept if p >= thresholds[form]]
            right, wrong = sum(answered), len(answered) - sum(answered)
            if wrong <= target_wrong * max(1, questions) and (found is None or right >= found[0]):
                found = (right, thresholds, margin)
    if found is None:
        return {form: THRESHOLDS[-1] for form in FORMS}, MARGINS[-1]
    return found[1], found[2]


BIAS_SLOTS = {"column": ("measure", "key", "shown"), "value": ("value", "contains"), "label": ("line", "section")}


def biased(reading: Reading, held: dict[str, float], wanted: dict[str, float]) -> Reading:
    """A reading with each kind's bias moved from what it was read under to what is wanted."""
    moves = {slot: held[slot] - wanted[kind] for kind, slots in BIAS_SLOTS.items() for slot in slots}
    return Reading({name: round(score + moves.get(slot_of(name), 0.0), 4) for name, score in reading.scores.items()})


def fit_biases(items: list, engine: NamedEngine, *, k: int = 50, progress: bool = False) -> tuple[dict[str, float], list[dict]]:
    """What each kind of link must reach to read above nothing, on families the reader never trained on: the reader learned these against its own share of "links to nothing", which is the sampling's and not the questions', so they are set on what the decoder lists. Every grid point reads every question; a program runs once a question. By right answers in the top 5, then listed, then first."""
    from training.named.evaluate import Runner
    runner = Runner(engine)
    grid = [{"column": column, "value": value, "label": label} for column in BIAS_GRID["column"] for value in BIAS_GRID["value"] for label in BIAS_GRID["label"]]
    counts = [{"first": 0, "top5": 0, "listed": 0, "questions": 0} for _ in grid]
    start = {slot: engine.weights.bias(slot) for slots in BIAS_SLOTS.values() for slot in slots}
    gold = [item for item in items if item.label == "gold"]
    for at, item in enumerate(gold):
        prepared = runner.prepare(item)
        named = engine.read(item.question, prepared)
        memo: dict = {}

        def right(program) -> bool:
            held = memo.get(program)
            if held is None:
                try:
                    rows = run(program, prepared.tables).rows
                    held = bool(rows) and bool(item.grade(list(rows)) or (item.alternate is not None and item.alternate(list(rows))))
                except Exception:      # a candidate that fails answers nothing
                    held = False
                memo[program] = held
            return held

        for row, point in zip(counts, grid):
            programs = prepared.decoder.decode(biased(named.reading, start, point), k=k, weights=engine.slot_weights).programs
            rights = [place for place, scored in enumerate(programs[:k]) if right(scored.program)]
            row["questions"] += 1
            row["first"] += bool(rights) and rights[0] == 0
            row["top5"] += bool(rights) and rights[0] < 5
            row["listed"] += bool(rights)
        if progress and (at + 1) % 50 == 0:
            print(f"biases {at + 1} of {len(gold)}", flush=True)
    runner.close()
    best = max(range(len(grid)), key=lambda at: (counts[at]["top5"], counts[at]["listed"], counts[at]["first"], -at))
    return {slot: grid[best][kind] for kind, slots in BIAS_SLOTS.items() for slot in slots}, [{**point, **row} for point, row in zip(grid, counts)]


def fit_slot_weights(records: Sequence[CandidateRecord]) -> Weights:
    """The decoder's slot weights, from each candidate's per-slot sum of readings. Clipped above zero: a slot that argued against the right program is read as silent, never inverted."""
    by_question: dict[str, list[CandidateRecord]] = {}
    for record in records:
        if record.ran:
            by_question.setdefault(record.question, []).append(record)
    groups = []
    for question in sorted(by_question):
        group = by_question[question]
        right = np.asarray([record.right for record in group], dtype=np.float64)
        if right.sum() and not right.all():
            groups.append((np.asarray([[record.slots.get(slot, 0.0) for slot in SLOTS] for record in group], dtype=np.float64), right / right.sum()))
    weights = fit_conditional(groups, len(SLOTS), l2=L2) if groups else np.ones(len(SLOTS))
    return Weights({slot: round(float(max(value, 0.0)), 6) for slot, value in zip(SLOTS, weights)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit the chooser on held-out families.")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--side", default="train,pub_eia,pub_sec")
    parser.add_argument("--families", required=True, help="a JSON list of the families the reader never trained on")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", required=True, help="a new bundle folder under the experiment: the reader's weights copied, this fit beside them")
    parser.add_argument("--records", help="where to keep the candidates' records, to fit again without reading")
    parser.add_argument("--biases", type=int, default=0, help="how many questions set each kind's bias first")
    parser.add_argument("--no-slot-weights", action="store_true", help="the decoder's slots stay at weight 1")
    args = parser.parse_args()
    from training.named.evaluate import load_factory_set
    families = json.loads(Path(args.families).read_text(encoding="utf-8"))
    items = load_factory_set(args.side, limit=args.limit, families=families, over_quota=True)
    engine = load_bundle(args.bundle)
    if args.biases:
        chosen, table = fit_biases(items[:: max(1, len(items) // args.biases)], engine, progress=True)
        print(json.dumps({"biases": chosen}, sort_keys=True), flush=True)
        for row in sorted(table, key=lambda item: -item["top5"])[:12]:
            print(json.dumps(row, sort_keys=True), flush=True)
        engine.weights = replace(engine.weights, slot_biases={**engine.weights.slot_biases, **chosen})
    records = collect(items, engine, progress=True)
    if args.records:
        with gzip.open(args.records, "wb") as sink:
            pickle.dump(records, sink, protocol=4)
    print(json.dumps({"out_of_fold": fold_report(records)}, sort_keys=True), flush=True)
    fit = [record for record in records if family_share(record.family, args.seed + 1) >= 0.5]
    held = [record for record in records if family_share(record.family, args.seed + 1) < 0.5]
    chooser = fit_chooser(fit, held)
    slot_weights = engine.slot_weights if args.no_slot_weights else fit_slot_weights(fit)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(args.bundle) / BUNDLE_STATE, out / BUNDLE_STATE)
    document = json.loads((Path(args.bundle) / BUNDLE_META).read_text(encoding="utf-8"))
    document.update(chooser=chooser.record(), slot_weights=dict(sorted(slot_weights.by_slot.items())))
    document["weights"] = {**document["weights"], "slot_biases": dict(sorted(engine.weights.slot_biases.items()))}
    (out / BUNDLE_META).write_text(json.dumps(document, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"questions": len({record.question for record in records}), "candidates": len(records), "right": sum(record.right for record in records), "fit_families": len({record.family for record in fit}), "held_families": len({record.family for record in held}), "calibration": dict(chooser.calibration), "thresholds": dict(chooser.thresholds)}, sort_keys=True), flush=True)


__all__ = ["CandidateRecord", "answer_key", "calibrate", "calibrated", "choose_policy", "choose_thresholds", "collect", "cross_fit", "decision_of", "fit_chooser", "fit_biases", "fit_conditional", "fit_logistic", "groups_of", "fit_slot_weights", "fold_of", "fold_report", "records_of"]

if __name__ == "__main__":
    main()
