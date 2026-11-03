"""The alignment's training: collect the decoder's own candidates with every (word, part) evidence, learn the alignment's named weights listwise over each question's candidates, then fit the chooser over the alignment's sums and the verifier's named evidence.

  collect  run a bundle's reader, decoder and verifier on factory questions (gold only) and keep, per question, its words, each part's evidence (engine/named/align.py:EvidenceBuilder, the serving code), each candidate's parts, its other named evidence, whether its answer is right, its read-as line, and the words the factory linked to which target.
  train    torch on CPU: score(candidate) = sum over words of max(nothing, best part) + parts nobody asked for + candidate evidence, the same formula as align_parts, softmax over a question's candidates that ran against the right ones; and each linked word's gold part against the candidate's other parts and nothing (factor GOLD_WEIGHT). Weights start at prior_weights and are held near it (L2). The epoch kept is the one with the most right first on the held families' collection, read by align_parts itself.
  chooser  the alignment's three sums beside the verifier's named evidence, a multinomial fit by training/named/choose.py on the held families, calibration and policy there; a new bundle folder with the reader copied, "align" and "chooser" written.

Candidates only: everything goes under training/named/data/experiments/<id>/. Deterministic: fixed seed, fixed order, a fixed number of epochs, float32 for the gradient and the serving code's float64 for every number kept.

usage: python -m training.named.align collect --bundle <dir> --side train,pub_eia,pub_sec --exclude <held_families.json> --questions 6000 --out <file.pkl.gz>
       python -m training.named.align collect --bundle <dir> --side train,pub_eia,pub_sec --families <held_families.json> --out <file.pkl.gz>
       python -m training.named.align train --train <file> --held <file> --bundle <dir> --out <weights.json>
       python -m training.named.align chooser --held <file> --bundle <dir> --align <weights.json> --out <new bundle dir>
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import pickle
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from engine.named.align import DENSE, GROUPS, LINKED, NOTHING, ALIGN_FEATURES, AlignWeights, EvidenceBuilder, PartEvidence, QuestionEvidence, align_parts, compile_weights, parts_of, prior_weights
from engine.named.bundle import BUNDLE_META, BUNDLE_STATE
from engine.named.chooser import PRIOR, Chooser
from engine.named.names import NameSet
from engine.named.pipeline import NamedEngine, load_bundle
from engine.program.language import Col, form_of
from engine.program.names import coordinate, SLOT_FILTER, SLOT_KEY, SLOT_LINE, SLOT_MEASURE, SLOT_SECTION, SLOT_SHOWN, SLOT_VALUE
from engine.program.output import read_as
from engine.program.semantics import fold
from training.named.choose import CandidateRecord, answer_key, fit_conditional, fold_report, groups_of, calibrated, support_of
from training.named.data import family_share

# What the chooser reads beside the alignment: the verifier's evidence and the program's own named facts. The summary link and word features (words_explained, parts_asked, link_*, rule_*) are what the alignment replaces.
CANDIDATE_FEATURES = ("fine_type", "shown_columns", "words_reused", "reach_gap", "type_agrees", "shape_agrees", "printed_tie", "bare_zero", "empty", "failed", "row_dump", "many_rows", "wrong_kind", "row_count_for_amount", "fraction_for_count", "too_large_for_share", "filter_did_nothing", "repeated_lines", "adds_twice", "form_prior", "decoder_score", "rank_inverse", "conditions", "class_agreement", "needs_asking", "reader_score")
CHOOSER_FEATURES = (*ALIGN_FEATURES, *CANDIDATE_FEATURES)
GOLD_WEIGHT = 0.5
L2_DENSE = 1e-3
L2_NAMES = 1e-3
EPOCHS = 12
LEARNING_RATE = 0.02
SEED = 20261103
DECODE_K = 50


@dataclass
class Collected:
    """One gold question's candidates and the evidence their alignment reads."""
    id: str
    family: str
    question: QuestionEvidence
    parts: list[PartEvidence]
    candidates: list[dict]
    gold: dict[int, tuple[str, ...]] = field(default_factory=dict)


def gold_parts(link: dict) -> tuple[str, ...]:
    """The part names a factory link's target can fill."""
    table, kind = link.get("table"), link.get("kind")
    if kind == "column" and link.get("position") is not None:
        name = Col(table, int(link["position"])).name
        return tuple(coordinate(slot, name) for slot in (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN, SLOT_FILTER))
    if link.get("target") is None:
        return ()
    if kind == "value" and link.get("position") is not None:
        return (coordinate(SLOT_VALUE, Col(table, int(link["position"])).name, fold(link["target"])),)
    if kind == "line":
        return (coordinate(SLOT_LINE, table, fold(link["target"])),)
    if kind == "band":
        return (coordinate(SLOT_SECTION, table, fold(link["target"])),)
    return ()


def collect(items: list, engine: NamedEngine, *, k: int = DECODE_K, progress: bool = False) -> list[Collected]:
    """Every gold question's candidates with the evidence of each of their parts. The reading and the candidates are the serving path's; the prior chooser only writes the other named evidence."""
    from training.named.evaluate import Runner
    runner = Runner(engine)
    out: list[Collected] = []
    started = time.time()
    gold_items = [item for item in items if item.label == "gold"]
    for at, item in enumerate(gold_items):
        prepared = runner.prepare(item)
        named = engine.read(item.question, prepared)
        pool = engine.pool(item.question, prepared, named, k=k)
        candidates = engine.score(pool, named, prepared, PRIOR)
        builder = EvidenceBuilder(named, prepared.catalog, engine.weights.coordinates, engine.names)
        names: dict[str, int] = {}
        rows = []
        for candidate in candidates:
            program = candidate.verified.scored.program
            parts = parts_of(program, prepared.catalog)
            for name in parts:
                if name not in names:
                    names[name] = -1
            result = candidate.rows
            right = bool(result) and bool(item.grade(list(result)) or (item.alternate is not None and item.alternate(list(result))))
            rows.append({"parts": parts, "features": {name: candidate.features.get(name, 0.0) for name in CANDIDATE_FEATURES if candidate.features.get(name, 0.0)}, "right": right, "rank": candidate.rank, "form": form_of(program), "answer": answer_key(result), "support": support_of(program, named, prepared.catalog), "read_as": read_as(program, prepared.catalog), "proposed": candidate.proposed})
        ordered = sorted(names)
        position = {name: at for at, name in enumerate(ordered)}
        parts = [builder.part(name) for name in ordered]
        for row in rows:
            row["parts"] = tuple(position[name] for name in row["parts"])
        gold: dict[int, tuple[str, ...]] = {}
        for link in item.links:
            wanted = gold_parts(link)
            for word in link.get("span", ()):
                if wanted and 0 <= word < len(builder.words):
                    gold[word] = tuple(sorted(set(gold.get(word, ())) | set(wanted)))
        out.append(Collected(item.id, item.family, builder.question_evidence(), parts, rows, gold))
        if progress and (at + 1) % 100 == 0:
            print(f"collected {at + 1} of {len(gold_items)} in {time.time() - started:.0f} s", flush=True)
    runner.close()
    return out


def candidate_records(collected: Sequence[Collected], weights: AlignWeights, names: NameSet) -> list[CandidateRecord]:
    """The chooser's records: each candidate's other named evidence and its alignment's three sums, by align_parts itself."""
    compiled = compile_weights(weights, names)
    out = []
    for question in collected:
        for row in question.candidates:
            alignment = align_parts(question.question, [question.parts[at] for at in row["parts"]], compiled)
            features = {**row["features"], **{name: value for name, value in alignment.features().items() if value}}
            out.append(CandidateRecord(question.id, question.family, row["form"], features, {}, row["right"], row["rank"], row["answer"], row["support"], row.get("proposed", "decoder")))
    return out


def first_right(collected: Sequence[Collected], weights: AlignWeights, names: NameSet, *, folds: int = 5) -> dict:
    """Out of fold, by families: the chooser over CHOOSER_FEATURES fit on the other folds, read on each one; how many questions it ranks a right candidate first, against the decoder's order."""
    report = fold_report(candidate_records(collected, weights, names), folds, names=CHOOSER_FEATURES)
    report["right_listed"] = sum(any(row["right"] and row["answer"] not in ("", "[]") for row in question.candidates) for question in collected)
    return report


# ---------------------------------------------------------------------------------------------------- training
class _Tensors:
    """One question's evidence as tensors."""

    def __init__(self, question: Collected, torch, names_count: int):
        P = len(question.parts)
        W = len(question.question.words)
        K = max([len(part.ids) for part in question.parts] + [1])
        self.W, self.P = W, P
        dense = np.zeros((P, W, len(DENSE)), dtype=np.float32)
        ids = np.zeros((P, K), dtype=np.int64)
        products = np.zeros((P, W, K), dtype=np.float32)
        evident = np.zeros((P, W), dtype=bool)
        group = np.zeros(P, dtype=np.int64)
        for at, part in enumerate(question.parts):
            dense[at] = part.dense
            group[at] = GROUPS.index(part.group)
            evident[at] = part.evident
            if len(part.ids):
                ids[at, :len(part.ids)] = part.ids
                products[at, :, :len(part.ids)] = part.products
        ran = [row for row in question.candidates if row["answer"] not in ("", "[]")]
        self.ok = bool(ran) and any(row["right"] for row in ran) and not all(row["right"] for row in ran) and P > 0 and W > 0
        if not self.ok:
            return
        member = np.zeros((len(ran), P), dtype=bool)
        for at, row in enumerate(ran):
            member[at, list(row["parts"])] = True
        self.dense, self.ids, self.products = torch.from_numpy(dense), torch.from_numpy(ids), torch.from_numpy(products)
        self.evident, self.group = torch.from_numpy(evident), torch.from_numpy(group)
        self.member = torch.from_numpy(member)
        self.nothing = torch.from_numpy(question.question.nothing.astype(np.float32))
        self.content = torch.from_numpy(question.question.content.astype(bool))
        self.candidate = torch.from_numpy(np.asarray([[row["features"].get(name, 0.0) for name in CANDIDATE_FEATURES] for row in ran], dtype=np.float32))
        self.right = torch.from_numpy(np.asarray([row["right"] for row in ran], dtype=bool))
        part_at = {part.part: at for at, part in enumerate(question.parts)}
        gold = []
        for word, wanted in sorted(question.gold.items()):
            for candidate, row in enumerate(ran):
                if not row["right"]:
                    continue
                held = [part_at[name] for name in wanted if name in part_at and part_at[name] in row["parts"]]
                if held and evident[held[0], word]:
                    gold.append((candidate, word, held[0]))
        self.gold = gold


def _scores(t: _Tensors, theta, torch):
    """score per candidate, the formula of align_parts in torch: words to their best part or nothing, parts nobody took, the candidate's evidence."""
    dense_w, names_w, nothing_w, unasked_w, candidate_w = theta
    pair = (t.dense * dense_w[t.group][:, None, :]).sum(-1) + (t.products * names_w[t.group[:, None], t.ids][:, None, :]).sum(-1)       # [P, W]
    pair = torch.where(t.evident, pair, torch.full_like(pair, -1e9))
    nothing = t.nothing @ nothing_w                                                                                                       # [W]
    table = torch.where(t.member[:, :, None], pair[None, :, :], torch.full((t.member.shape[0], t.P, t.W), -1e9))                         # [C, P, W]
    best, where = table.max(dim=1)                                                                                                        # [C, W]
    taking = t.content[None, :] | (best > -1e8)
    takes = best > nothing[None, :]
    words = torch.where(taking, torch.where(takes, best, nothing[None, :].expand_as(best)), torch.zeros_like(best)).sum(-1)
    taken = taking & takes
    used = ((where[:, :, None] == torch.arange(t.P)[None, None, :]) & taken[:, :, None]).any(dim=1)                                    # [C, P]
    unasked = ((t.member & ~used).float() * unasked_w[t.group][None, :]).sum(-1)
    return words + unasked + t.candidate @ candidate_w, pair, nothing


def _theta_of(weights: AlignWeights, names: NameSet, torch):
    compiled = compile_weights(weights, names)
    return [torch.tensor(compiled.dense, dtype=torch.float32), torch.tensor(compiled.names, dtype=torch.float32), torch.tensor(compiled.nothing, dtype=torch.float32), torch.tensor(compiled.unasked, dtype=torch.float32), torch.zeros(len(CANDIDATE_FEATURES), dtype=torch.float32)]


def _weights_of(theta, names: NameSet) -> AlignWeights:
    dense, by_name, nothing, unasked = (value.detach().double().numpy() for value in theta[:4])
    values: dict[str, float] = {}
    for g, group in enumerate(GROUPS):
        for f, feature in enumerate(DENSE):
            values[f"{group}.{feature}"] = round(float(dense[g, f]), 6)
        if group in LINKED:
            for at in np.flatnonzero(np.abs(by_name[g]) >= 1e-6).tolist():
                values[f"{group}.name={names.names[at]}"] = round(float(by_name[g, at]), 6)
        values[f"unasked.{group}"] = round(float(unasked[g]), 6)
    for f, feature in enumerate(NOTHING):
        values[f"nothing.{feature}"] = round(float(nothing[f]), 6)
    return AlignWeights({name: value for name, value in values.items() if value != 0.0})


def train(collected: Sequence[Collected], held: Sequence[Collected], names: NameSet, link_rules: dict, *, epochs: int = EPOCHS, seed: int = SEED, progress: bool = True) -> tuple[AlignWeights, list[dict]]:
    """The alignment's weights, listwise. Returns the epoch with the most out-of-fold right first on held, and every epoch's numbers."""
    import torch
    torch.manual_seed(seed)
    start = prior_weights(link_rules)
    theta = [value.clone().requires_grad_(True) for value in _theta_of(start, names, torch)]
    anchor = [value.detach().clone() for value in theta]
    linked = torch.tensor([group in LINKED for group in GROUPS], dtype=torch.float32)[:, None]
    tensors = [t for t in (_Tensors(question, torch, len(names)) for question in collected) if t.ok]
    optimizer = torch.optim.Adam(theta, lr=LEARNING_RATE)
    order = np.random.default_rng(seed)
    log = []
    best_weights, best_key = start, None
    for epoch in range(epochs + 1):
        if epoch:
            total = 0.0
            for batch in np.array_split(order.permutation(len(tensors)), max(1, len(tensors) // 64)):
                optimizer.zero_grad()
                loss = 0.0
                for at in batch.tolist():
                    t = tensors[at]
                    score, pair, nothing = _scores(t, theta, torch)
                    loss = loss + torch.logsumexp(score, 0) - torch.logsumexp(score[t.right], 0)
                    for candidate, word, part in t.gold:
                        held_parts = torch.nonzero(t.member[candidate]).flatten()
                        options = torch.cat([nothing[word:word + 1], pair[held_parts, word]])
                        target = int((held_parts == part).nonzero()[0, 0]) + 1
                        loss = loss + GOLD_WEIGHT * (torch.logsumexp(options, 0) - options[target]) / max(1, len(t.gold))
                loss = loss / len(batch)
                penalty = L2_DENSE * ((theta[0] - anchor[0]) ** 2).sum() + L2_NAMES * (((theta[1] - anchor[1]) * linked) ** 2).sum() + L2_DENSE * (((theta[2] - anchor[2]) ** 2).sum() + ((theta[3] - anchor[3]) ** 2).sum() + (theta[4] ** 2).sum())
                (loss + penalty).backward()
                with torch.no_grad():
                    theta[1].grad.mul_(linked)            # an operator part has no target coordinates
                optimizer.step()
                total += float(loss) * len(batch)
            mean = total / len(tensors)
        else:
            mean = None
        weights = _weights_of(theta, names)
        report = first_right(held, weights, names)
        row = {"epoch": epoch, "train_loss": round(mean, 5) if mean is not None else None, **report}
        log.append(row)
        if progress:
            print(json.dumps(row, sort_keys=True), flush=True)
        key = (report["top1_right_chooser"], -epoch)
        if best_key is None or key > best_key:
            best_key, best_weights = key, weights
    return best_weights, log


# ----------------------------------------------------------------------------------------------------- chooser
def fit_align_chooser(collected: Sequence[Collected], weights: AlignWeights, names: NameSet, seed: int = 0) -> tuple[Chooser, dict]:
    """The chooser over CHOOSER_FEATURES on the held families: weights fit on half by family, calibration and policy on the other half, as choose.py:main does."""
    records = candidate_records(collected, weights, names)
    report = fold_report(records, names=CHOOSER_FEATURES)
    fit = [record for record in records if family_share(record.family, seed + 1) >= 0.5]
    held = [record for record in records if family_share(record.family, seed + 1) < 0.5]
    if {record.family for record in fit} & {record.family for record in held}:
        raise ValueError("families both fit and calibrate")
    fitted = dict(zip(CHOOSER_FEATURES, (float(value) for value in fit_conditional(groups_of(fit, CHOOSER_FEATURES), len(CHOOSER_FEATURES)))))
    return calibrated(Chooser(fitted), held), report


def _load(path: str) -> list[Collected]:
    with gzip.open(path, "rb") as source:
        return pickle.load(source)


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect, train and fit the named alignment.")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("collect")
    one.add_argument("--bundle", required=True)
    one.add_argument("--side", default="train,pub_eia,pub_sec")
    one.add_argument("--families", help="only these families (a JSON list)")
    one.add_argument("--exclude", help="never these families (a JSON list)")
    one.add_argument("--questions", type=int, help="draw whole workbooks by hash until this many gold questions")
    one.add_argument("--seed", type=int, default=SEED)
    one.add_argument("--out", required=True)
    two = sub.add_parser("train")
    two.add_argument("--train", required=True)
    two.add_argument("--held", required=True)
    two.add_argument("--bundle", required=True)
    two.add_argument("--epochs", type=int, default=EPOCHS)
    two.add_argument("--out", required=True)
    three = sub.add_parser("chooser")
    three.add_argument("--held", required=True)
    three.add_argument("--bundle", required=True)
    three.add_argument("--align", required=True)
    three.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "collect":
        from training.named.evaluate import load_factory_set
        families = json.loads(Path(args.families).read_text(encoding="utf-8")) if args.families else None
        excluded = set(json.loads(Path(args.exclude).read_text(encoding="utf-8"))) if args.exclude else set()
        items = [item for item in load_factory_set(args.side, families=families, over_quota=True) if item.family not in excluded and item.label == "gold"]
        if args.questions:
            books = sorted({item.book for item in items}, key=lambda book: hashlib.blake2b(f"{args.seed}|{book}".encode(), digest_size=8).digest())
            wanted, count = set(), 0
            per_book: dict[str, int] = {}
            for item in items:
                per_book[item.book] = per_book.get(item.book, 0) + 1
            for book in books:
                if count >= args.questions:
                    break
                wanted.add(book)
                count += per_book[book]
            items = [item for item in items if item.book in wanted]
        print(json.dumps({"questions": len(items), "books": len({item.book for item in items}), "families": len({item.family for item in items})}), flush=True)
        engine = load_bundle(args.bundle)
        collected = collect(items, engine, progress=True)
        with gzip.open(args.out, "wb") as sink:
            pickle.dump(collected, sink, protocol=4)
        print(json.dumps({"collected": len(collected), "candidates": sum(len(row.candidates) for row in collected), "right_listed": sum(any(c["right"] for c in row.candidates) for row in collected)}), flush=True)
        return
    document = json.loads((Path(args.bundle) / BUNDLE_META).read_text(encoding="utf-8"))
    names = NameSet(tuple(document["names"]))
    if args.command == "train":
        collected, held = _load(args.train), _load(args.held)
        shared = {row.family for row in collected} & {row.family for row in held}
        if shared:
            raise ValueError(f"{len(shared)} families both train and choose")
        weights, log = train(collected, held, names, document["weights"]["rules"], epochs=args.epochs)
        weights.write(args.out)
        Path(f"{args.out}.log.json").write_text(json.dumps({"log": log, "train_sha256": _sha(args.train), "held_sha256": _sha(args.held), "epochs": args.epochs, "seed": SEED, "lr": LEARNING_RATE, "l2": [L2_DENSE, L2_NAMES], "gold_weight": GOLD_WEIGHT}, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        return
    held = _load(args.held)
    weights = AlignWeights.from_record(json.loads(Path(args.align).read_text(encoding="utf-8")))
    chooser, report = fit_align_chooser(held, weights, names)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(args.bundle) / BUNDLE_STATE, out / BUNDLE_STATE)
    document.update(chooser=chooser.record(), align=weights.record())
    (out / BUNDLE_META).write_text(json.dumps(document, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"out_of_fold": report, "calibration": dict(chooser.calibration), "thresholds": dict(chooser.thresholds), "margin": chooser.margin}, sort_keys=True), flush=True)


__all__ = ["CANDIDATE_FEATURES", "CHOOSER_FEATURES", "Collected", "candidate_records", "collect", "first_right", "fit_align_chooser", "gold_parts", "train"]

if __name__ == "__main__":
    from training.named import align as _module     # records pickle under the module's own name, not __main__
    _module.main()
