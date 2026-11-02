"""Multi-task training of the reader, on family-held-out examples.

Losses, each on named outputs:
  target coordinates   a column's, value's, line's and band's named coordinates: the factory's labels and, always, what the compiler knows (column role, value kind, line role)
  word coordinates     the same head over the question's words: a word in a link reads what its target is labelled; other words read none of the meaning tier
  closed heads         form, aggregate, direction, size, entity, comparison, row and calc operators, answer type, blank, how many conditions; word role and calc side per word
  links                link(span, target) over the target and its in-sheet hard negatives: the sum over named coordinates of weight x question x target, plus named rule evidence. The one formula of engine/named/links.py, its weights learned here.
  open                 the separate unnamed channel, on the same candidates. It trains its own two projections and, unless --open-detached, the shared encoder.

Each step reads one tab, and --accumulate of them make one optimizer step: its tables through the augmentations (renames, masks, shuffles, distractor columns; training/named/augment.py), its questions with the question words alone. A checkpoint is chosen on held-out families by top-10 link recall. float32 unless --amp. A candidate goes only to training/named/data/experiments/<id>/out/: promotion is separate.

Training needs an experiment contract (design/phase4_contract.txt, CLAUDE.md). This module incurs no spend by itself; a lease does (training/named/lease.py).

usage: python -m training.named.train --examples <dir> --experiment <id> --backbone bge-small [--steps N] [--accumulate 4] [--held-share 0.1] [--seed 0] [--blocks 3] [--hidden 384] [--layers N] [--sample-rows 8] [--questions 8] [--lr 2e-5] [--head-lr 1e-3] [--out <dir>] [--eval-every 500] [--amp]
       --fake runs on the small fake with the hashing backbone: a pipeline check, never a result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import torch
from torch import nn

from engine.named.backbone import question_words
from engine.named.index import ClassSignatures
from engine.named.links import LinkWeights, RULES, SLOT_BIASES, rule_evidence
from engine.named.model import NamedReader, ReaderConfig
from engine.named.names import CALC_SIDES, CLOSED_HEADS, LEFT, RIGHT, WORD_ROLE_CLASSES, NameSet, closed_labels, fine_answer_type, load_name_set, meaning_groups
from engine.named.bundle import write_bundle
from engine.named.units import T_BAND, T_COLUMN, T_LINE, T_TABLE, T_VALUE, Graph, Target, collate, question_graph, table_graphs, targets_of, value_graph
from engine.program.catalog import Catalog, CatalogTable
from engine.program.decoder import Decoder
from engine.program.index import stem
from engine.program.names import A_NAME, ROLE_KEY, ROLE_MEASURE, SLOT_ANSWER_TYPE, SLOT_CALC_LEFT, slot_of
from engine.program.semantics import fold
from engine.program.words import _STOP, read_question
from training.named import EXPERIMENTS_DIR
from training.named.augment import TRAINING_SHARES, augmented
from training.named.data import TabExample, check_held_out, read_examples, split_families

THRESHOLD = 6              # the factory's: a label at this confidence or more counts
GOLD = "gold"
_ROW_SIDES = re.compile(r"(?:^|\s|\()(\d+)\s*[-/]\s*(\d+)(?:\s|\)|$)")
COLUMN_NEGATIVES = 24
VALUE_NEGATIVES = 16
TABLES_PER_STEP = 3
LOSS_WEIGHTS = {"target": 1.0, "word": 1.0, "closed": 1.0, "role": 0.5, "side": 0.25, "link": 2.0, "open": 0.5}


@dataclass
class TrainConfig:
    reader: ReaderConfig
    steps: int = 300
    accumulate: int = 1            # tabs read per optimizer step
    questions: int = 8
    lr: float = 2e-5
    head_lr: float = 1e-3
    weight_decay: float = 0.01
    seed: int = 0
    held_share: float = 0.1
    eval_every: int = 500
    eval_tabs: int = 200
    amp: bool = False
    open_detached: bool = False
    augment: dict = field(default_factory=lambda: dict(TRAINING_SHARES))


@dataclass
class LinkExample:
    """One link of a question: its question, its words, and its candidates, the first the right one."""
    question: int
    span: list[int]
    kind: str
    candidates: list[int]
    rules: np.ndarray


@dataclass
class Batch:
    """One step's read: the graphs, the targets with what is known of them, the questions with their labels, the links."""
    graphs: list[Graph]
    columns: list[tuple[int, int, int]]         # (graph, unit, target)
    tables_at: list[tuple[int, int]]            # (graph, target): a table's target, read from its units
    value_graphs: list[Graph]
    values: list[int]                           # target per value graph
    targets: list[Target]
    positive: list[list[str]]                   # per target: its named coordinates
    known_meaning: list[bool]                   # per target: the labels speak for its meaning tier
    questions: list[str]
    closed: list[dict[str, str]]
    word_positive: list[list[list[str] | None]]  # per question, per word: names, or None when unknown
    word_role: list[list[int]]
    word_side: list[list[int]]
    links: list[LinkExample]
    texts: int = 0


def _labelled_names(label: dict | None, key: str = "coordinates") -> tuple[list[str], bool]:
    """(the names a label gives, whether it speaks for the meaning tier at all)."""
    if not label:
        return [], False
    names = list(label.get(key) or ())
    if names:
        return names, True
    return [], str(label.get("raw", "")).lower() == "none" and int(label.get("conf", 0)) >= THRESHOLD


def _column_structure(column) -> list[str]:
    out = [f"column_role:{column.role}"]
    if column.kind:
        out.append(f"value_kind:{column.kind}")
    return out


def closed_of(row: dict, labels: dict[str, dict]) -> dict[str, str]:
    """A row's closed heads. The answer type is read as finely as the labels of what it answers with allow: its linked key or measure, then the columns it used."""
    closed = closed_labels(row.get("coordinates", ()))
    coarse = closed[SLOT_ANSWER_TYPE]
    wanted = ROLE_KEY if coarse == A_NAME else ROLE_MEASURE
    names = [name for link in row.get("links", ()) if link.get("role") == wanted for name in link.get("coordinates") or ()]
    for used in row.get("used", ()):
        names.extend((labels.get(f"{used.get('table')}.{used.get('position')}") or {}).get("coordinates") or ())
    closed[SLOT_ANSWER_TYPE] = fine_answer_type(coarse, names)
    return closed


def build_batch(example: TabExample, reader_config: ReaderConfig, names: NameSet, rng: random.Random, *, questions: int, shares: dict | None, rows: list[dict] | None = None) -> Batch | None:
    """One tab's step. Tables reach the reader through the augmentations; the same units, targets and rule reading serving uses."""
    units = reader_config.units
    gold = [row for row in example.rows if row.get("label") == GOLD] if rows is None else rows
    if not gold:
        return None
    chosen = rng.sample(gold, min(questions, len(gold))) if rows is None else gold
    tables: dict[str, CatalogTable] = {}
    moved: dict[str, dict[int, int]] = {}
    wanted = {link.get("table") for row in chosen for link in row.get("links", ()) if link.get("table")}
    order = sorted(example.tables, key=lambda item: (item[0] not in wanted, item[0]))
    for table_id, kind, table in order[:TABLES_PER_STEP]:
        if not table.lines and not table.printed:
            continue
        if shares:
            table, mapping = augmented(table, rng, shares)
        else:
            mapping = {column.position: column.position for column in table.columns}
        tables[table_id] = CatalogTable(table_id, table, kind)
        moved[table_id] = mapping
    if not tables:
        return None
    catalog = Catalog(list(tables.values()))
    decoder = Decoder(catalog)
    graphs: list[Graph] = []
    columns: list[tuple[int, int, int]] = []
    tables_at: list[tuple[int, int]] = []
    targets: list[Target] = []
    positive: list[list[str]] = []
    known: list[bool] = []
    at_target: dict[tuple, int] = {}

    def add_target(target: Target, names_for: list[str], speaks: bool) -> int:
        held = at_target.get(target.key)
        if held is None:
            held = at_target[target.key] = len(targets)
            targets.append(target)
            positive.append(names_for)
            known.append(speaks)
        return held

    all_targets = {}
    for table_id, table in tables.items():
        inverse = {new: old for old, new in moved[table_id].items()}
        for graph in table_graphs(table, units):
            at_graph = len(graphs)
            graphs.append(graph)
            union: set[str] = set()
            speaks_table = False
            for position, unit in sorted(graph.columns.items()):
                column = table.columns[position]
                old = inverse.get(position)
                label_names, speaks = _labelled_names(example.labels.get(f"{table_id}.{old}")) if old is not None else ([], True)
                union.update(label_names)
                speaks_table = speaks_table or speaks
                at = add_target(Target(table_id, T_COLUMN, position, column.name), [*label_names, *_column_structure(column)], speaks)
                columns.append((at_graph, unit, at))
            at = add_target(Target(table_id, T_TABLE, -1, ""), sorted(union), speaks_table)
            tables_at.append((at_graph, at))
        all_targets[table_id] = targets_of(table, units)
    value_graphs: list[Graph] = []
    values: list[int] = []
    orders = {table_id: table.column_order for table_id, table in tables.items()}

    def add_value(target: Target) -> int:
        fresh = target.key not in at_target
        table = tables[target.table]
        if target.kind == T_VALUE:
            inverse = {new: old for old, new in moved[target.table].items()}
            label_names, speaks = _labelled_names(example.labels.get(f"{target.table}.{inverse.get(target.position)}"), "value_coordinates")
            at = add_target(target, [*label_names, "value_kind:text"], speaks)
        else:
            at = add_target(target, [f"line_role:{'band' if target.kind == T_BAND else 'total'}"], False)
        if fresh:
            value_graphs.append(value_graph(table, target, units, orders[target.table]))
            values.append(at)
        return at

    links: list[LinkExample] = []
    closed = []
    word_positive = []
    word_role = []
    word_side = []
    texts = []
    for row in chosen:
        question = row["question"]
        words = question_words(question)
        at_question = len(texts)
        texts.append(question)
        closed.append(closed_of(row, example.labels))
        word_names: list[list[str] | None] = [([] if stem(word) in _STOP else None) for word in words]
        roles = [0] * len(words)
        sides = [0] * len(words)
        left_names = {name.split(":", 1)[1] for name in row.get("coordinates", ()) if slot_of(name) == SLOT_CALC_LEFT}
        calc = row.get("form") == "calc"
        row_sides = _ROW_SIDES.search(str(row.get("program", "")).split(" where ")[0]) if not calc else None
        floor = read_question(question, catalog, decoder).scores
        stems = {stem(word) for word in words}
        for link in row.get("links", ()):
            span = [at for at in link.get("span", ()) if at < len(words)]
            if not span:
                continue
            role = link.get("role")
            names_of_link = list(link.get("coordinates") or ())
            for at in span:
                if names_of_link:
                    word_names[at] = (word_names[at] or []) + names_of_link
                if role in WORD_ROLE_CLASSES:
                    roles[at] = WORD_ROLE_CLASSES.index(role)
            kind = link.get("kind")
            table_id = link.get("table")
            if table_id not in tables:
                continue
            table = tables[table_id]
            if kind in ("column", "value"):
                old = link.get("position")
                position = moved[table_id].get(old)
                if position is None:
                    continue
                if calc:
                    side = LEFT if any(name in left_names for name in Target(table_id, T_COLUMN, position, "").coordinates()) else RIGHT
                    for at in span:
                        sides[at] = CALC_SIDES.index(side)
                elif row_sides is not None and kind == "column" and str(old) in row_sides.groups():
                    for at in span:
                        sides[at] = CALC_SIDES.index(LEFT if str(old) == row_sides.group(1) else RIGHT)
            elif kind == "context":
                gold = Target(table_id, T_TABLE, -1, "")
                others = [Target(other, T_TABLE, -1, "") for other in sorted(tables) if other != table_id]
                if gold.key in at_target and others and all(item.key in at_target for item in others):
                    candidates = [gold, *others]
                    links.append(LinkExample(at_question, span, kind, [at_target[item.key] for item in candidates], np.stack([_rule_row(item.coordinates(), floor, stems, tables[item.table]) for item in candidates])))
                continue
            example_link = None
            if kind == "column":
                example_link = _column_candidates(table, table_id, position, floor, stems, rng, at_target)
            elif kind == "value":
                value = link.get("target")
                if value is None:
                    continue
                example_link = _value_candidates(all_targets[table_id], table_id, position, fold(value), floor, stems, rng, add_value)
            elif kind in ("line", "band"):
                value = link.get("target")
                if value is None:
                    continue
                example_link = _label_candidates(all_targets[table_id], table_id, T_LINE if kind == "line" else T_BAND, fold(value), floor, stems, rng, add_value)
            if example_link is not None:
                candidates, rules = example_link
                links.append(LinkExample(at_question, span, kind, candidates, rules))
        word_positive.append(word_names)
        word_role.append(roles)
        word_side.append(sides)
    return Batch(graphs, columns, tables_at, value_graphs, values, targets, positive, known, texts, closed, word_positive, word_role, word_side, links)


def _rule_row(names: tuple[str, ...], floor: dict, stems: set[str], table) -> np.ndarray:
    row = np.zeros(len(RULES), dtype=np.float32)
    for name in names[:1]:
        score = floor.get(name)
        if score is not None:
            for rule, evidence in rule_evidence(name, score, stems, table):
                row[RULES.index(rule)] += evidence
    return row


def _column_candidates(table, table_id, position, floor, stems, rng, at_target):
    """The column, and the sheet's columns a word could mistake for it: same kind first, then what its words reach, each draw from the batch's rng."""
    gold = Target(table_id, T_COLUMN, position, table.columns[position].name)
    if gold.key not in at_target:
        return None
    others = [column for column in table.columns if column.position != position and Target(table_id, T_COLUMN, column.position, column.name).key in at_target]
    same = [column for column in others if column.kind == table.columns[position].kind]
    rest = [column for column in others if column.kind != table.columns[position].kind]
    rng.shuffle(same)
    rng.shuffle(rest)
    picked = (same + rest)[:COLUMN_NEGATIVES]
    if not picked:
        return None
    candidates = [gold] + [Target(table_id, T_COLUMN, column.position, column.name) for column in picked]
    return [at_target[item.key] for item in candidates], np.stack([_rule_row(item.coordinates(), floor, stems, table) for item in candidates])


def _value_candidates(targets, table_id, position, value, floor, stems, rng, add_value):
    """The value, others of its column, and values of the sheet's other columns the question's words reach."""
    pool = [target for target in targets if target.kind == T_VALUE and target.table == table_id]
    gold = next((target for target in pool if target.position == position and target.text == value), None)
    if gold is None:
        return None
    same = [target for target in pool if target.position == position and target is not gold]
    reached = [target for target in pool if target.position != position and target.coordinates()[0] in floor]
    rng.shuffle(same)
    rng.shuffle(reached)
    picked = (reached[:VALUE_NEGATIVES // 2] + same)[:VALUE_NEGATIVES]
    if not picked:
        return None
    candidates = [gold, *picked]
    table = None
    return [add_value(item) for item in candidates], np.stack([_rule_row(item.coordinates(), floor, stems, table) for item in candidates])


def _label_candidates(targets, table_id, kind, value, floor, stems, rng, add_value):
    pool = [target for target in targets if target.kind == kind and target.table == table_id]
    gold = next((target for target in pool if target.text == value), None)
    if gold is None:
        return None
    others = [target for target in pool if target is not gold]
    rng.shuffle(others)
    picked = others[:VALUE_NEGATIVES]
    if not picked:
        return None
    candidates = [gold, *picked]
    return [add_value(item) for item in candidates], np.stack([_rule_row(item.coordinates(), floor, stems, None) for item in candidates])


@dataclass
class Forward:
    """What a batch read, as logits."""
    target_hidden: torch.Tensor
    target_logits: torch.Tensor
    words: list[list[str]]
    word_hidden: list[torch.Tensor]
    word_logits: list[torch.Tensor]
    closed_logits: dict[str, torch.Tensor]


def run_batch(backbone: nn.Module, reader: NamedReader, config: ReaderConfig, batch: Batch, device: str) -> Forward:
    """The reader over a batch: one read of each distinct text, each table graph alone, value graphs together."""
    texts = [text for graph in (*batch.graphs, *batch.value_graphs) for text in graph.texts]
    distinct, vectors = backbone.encode_texts(texts, max_tokens=config.text_tokens)
    batch.texts = len(distinct)
    lookup = {text: at for at, text in enumerate(distinct)}

    def stack(graph: Graph) -> torch.Tensor:
        return vectors[torch.as_tensor([lookup[text] for text in graph.texts], device=vectors.device)] if graph.texts else torch.zeros((1, backbone.dim), device=vectors.device)

    by_graph: dict[int, torch.Tensor] = {}
    for at, graph in enumerate(batch.graphs):
        inputs, edges, pad = collate([graph], [stack(graph)])
        by_graph[at] = reader.hidden(inputs, edges, pad)[0]
    rows, sources = [], []
    for graph_at, unit, target in batch.columns:
        rows.append(target)
        sources.append(by_graph[graph_at][unit])
    per_table: dict[int, list[torch.Tensor]] = {}
    for graph_at, target in batch.tables_at:
        per_table.setdefault(target, []).append(by_graph[graph_at][batch.graphs[graph_at].table])
    for target, parts in per_table.items():
        rows.append(target)
        sources.append(torch.stack(parts).mean(0))
    if batch.value_graphs:
        inputs, edges, pad = collate(batch.value_graphs, [stack(graph) for graph in batch.value_graphs])
        read = reader.hidden(inputs, edges, pad)
        rows.extend(batch.values)
        sources.extend(read[:, 1].unbind(0))
    hidden = torch.zeros((len(batch.targets), config.hidden), device=device)
    if rows:
        read_out = torch.stack(sources).float()          # what the encoder read under autocast comes out in float32: the head and the losses count in it
        hidden = hidden.index_copy(0, torch.as_tensor(rows, device=hidden.device), read_out)
    questions = backbone.encode_questions(batch.questions, max_tokens=config.question_tokens)
    graphs = [question_graph(words) for words, _ in questions]
    inputs, edges, pad = collate(graphs, [vectors_q if len(words) else torch.zeros((1, backbone.dim), device=vectors.device) for words, vectors_q in questions])
    word_hidden_all = reader.hidden(inputs, edges, pad).float()
    closed = reader.closed_logits(word_hidden_all, pad)
    word_hidden = [word_hidden_all[at, :len(words)] for at, (words, _) in enumerate(questions)]
    return Forward(hidden, reader.coordinate_logits(hidden), [words for words, _ in questions], word_hidden, [reader.coordinate_logits(item) for item in word_hidden], closed)


def _balanced_bce(logits: torch.Tensor, labels: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """The mean of the loss on positives and on negatives, so 385 names that are mostly no do not drown the one that is yes."""
    losses = nn.functional.binary_cross_entropy_with_logits(logits, labels, reduction="none") * mask
    pos = labels * mask
    neg = (1.0 - labels) * mask
    return 0.5 * ((losses * pos).sum() / pos.sum().clamp(min=1.0) + (losses * neg).sum() / neg.sum().clamp(min=1.0))


def losses_of(reader: NamedReader, names: NameSet, batch: Batch, out: Forward, open_detached: bool, structure: torch.Tensor, device: str) -> dict[str, torch.Tensor]:
    n = len(names)
    labels = torch.zeros((len(batch.targets), n), device=device)
    mask = torch.zeros((len(batch.targets), n), device=device)
    for at, (held, speaks) in enumerate(zip(batch.positive, batch.known_meaning)):
        mask[at] = 1.0 if speaks else structure
        for name in held:
            position = names.at(name)
            if position is not None:
                labels[at, position] = 1.0
                mask[at, position] = 1.0
    result = {"target": _balanced_bce(out.target_logits, labels, mask)}
    word_losses = []
    role_losses = []
    side_losses = []
    for at, (word_names, logits) in enumerate(zip(batch.word_positive, out.word_logits)):
        if not len(logits):
            continue
        w_labels = torch.zeros_like(logits)
        w_mask = torch.zeros_like(logits)
        for word_at, held in enumerate(word_names):
            if held is None:
                w_mask[word_at] = structure
                continue
            w_mask[word_at] = 1.0
            for name in held:
                position = names.at(name)
                if position is not None:
                    w_labels[word_at, position] = 1.0
        word_losses.append(_balanced_bce(logits, w_labels, w_mask))
        role_losses.append(nn.functional.cross_entropy(reader.word_role(out.word_hidden[at]), torch.as_tensor(batch.word_role[at], device=device)))
        side_losses.append(nn.functional.cross_entropy(reader.calc_side(out.word_hidden[at]), torch.as_tensor(batch.word_side[at], device=device)))
    zero = out.target_logits.sum() * 0.0
    result["word"] = torch.stack(word_losses).mean() if word_losses else zero
    result["role"] = torch.stack(role_losses).mean() if role_losses else zero
    result["side"] = torch.stack(side_losses).mean() if side_losses else zero
    closed_terms = []
    for head, logits in out.closed_logits.items():
        wanted = torch.as_tensor([CLOSED_HEADS[head].index(row[head]) for row in batch.closed], device=device)
        closed_terms.append(nn.functional.cross_entropy(logits, wanted))
    result["closed"] = torch.stack(closed_terms).mean()
    link_terms, open_terms = [], []
    if batch.links:
        weights = reader.coordinate_weights()
        rule_weights = reader.rule_weights()
        targets_read = torch.sigmoid(out.target_logits)
        open_targets = reader.open_target(out.target_hidden.detach() if open_detached else out.target_hidden)
        for link in batch.links:
            words_read = torch.sigmoid(out.word_logits[link.question][link.span]).mean(0)
            candidates = torch.as_tensor(link.candidates, device=device)
            rules = torch.as_tensor(link.rules, device=device)
            scores = (weights * words_read * targets_read[candidates]).sum(-1) + (rules * rule_weights).sum(-1)
            wanted = torch.zeros(1, dtype=torch.long, device=device)
            link_terms.append(nn.functional.cross_entropy(scores[None], wanted))
            hidden = out.word_hidden[link.question][link.span].mean(0)
            word_open = nn.functional.normalize(reader.open_word(hidden.detach() if open_detached else hidden), dim=-1)
            target_open = nn.functional.normalize(open_targets[candidates], dim=-1)
            open_terms.append(nn.functional.cross_entropy(((word_open * target_open).sum(-1) / 0.1)[None], wanted))
    result["link"] = torch.stack(link_terms).mean() if link_terms else zero
    result["open"] = torch.stack(open_terms).mean() if open_terms else zero
    return result


def link_weights_of(reader: NamedReader) -> LinkWeights:
    """The learned weights as the plain numbers a bundle carries."""
    with torch.no_grad():
        return LinkWeights(reader.coordinate_weights().cpu().double().numpy().round(6), {rule: round(float(value), 6) for rule, value in zip(RULES, reader.rule_weights().cpu().tolist())}, round(float(reader.role_weight()), 6), round(float(reader.closed_weight()), 6), dict(SLOT_BIASES))


def held_metrics(backbone, reader, config, names, examples, device, limit: int, seed: int) -> dict:
    """Held-out families, no augmentation: where each link's target ranks among its candidates, and the closed heads."""
    reader.eval()
    backbone.eval()
    rng = random.Random(seed)
    top1 = top10 = links = 0
    right = {head: 0 for head in CLOSED_HEADS}
    questions = 0
    with torch.no_grad():
        weights = reader.coordinate_weights()
        rule_weights = reader.rule_weights()
        for example in sorted(examples, key=lambda item: item.key)[:limit]:
            batch = build_batch(example, config, names, rng, questions=8, shares=None)
            if batch is None:
                continue
            out = run_batch(backbone, reader, config, batch, device)
            read = torch.sigmoid(out.target_logits)
            for link in batch.links:
                words_read = torch.sigmoid(out.word_logits[link.question][link.span]).mean(0)
                candidates = torch.as_tensor(link.candidates, device=device)
                rules = torch.as_tensor(link.rules, device=device)
                scores = (weights * words_read * read[candidates]).sum(-1) + (rules * rule_weights).sum(-1)
                rank = int((scores > scores[0]).sum())
                links += 1
                top1 += rank == 0
                top10 += rank < 10
            for head, logits in out.closed_logits.items():
                for row_at, row in enumerate(batch.closed):
                    right[head] += CLOSED_HEADS[head][int(logits[row_at].argmax())] == row[head]
            questions += len(batch.closed)
    reader.train()
    backbone.train()
    return {"links": links, "link_top1": round(top1 / max(1, links), 4), "link_top10": round(top10 / max(1, links), 4), "questions": questions, "closed": {head: round(value / max(1, questions), 4) for head, value in right.items()}}


def _commit() -> dict:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--short", "--", "engine/named", "training/named"], text=True, stderr=subprocess.DEVNULL).strip())
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = "", None
    return {"commit": commit, "dirty": dirty}


def examples_sha(folder: str | Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(folder).glob("shard_*.pkl.gz")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def train(examples: list[TabExample], names: NameSet, backbone: nn.Module, config: TrainConfig, out: str | Path, *, record: dict | None = None, log_print: bool = True, signatures=None) -> dict:
    """The loop. Same seed, same machine: same steps. Writes out/log.jsonl, out/best/ (a bundle) and out/best.json; returns the summary."""
    random.seed(config.seed)
    np.random.seed(config.seed)
    torch.manual_seed(config.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    kept, held = split_families(examples, config.held_share, config.seed)
    check_held_out(kept, held)
    if not kept:
        raise ValueError("no examples to train on")
    backbone = backbone.to(device).float().train()
    reader_config = config.reader
    reader = NamedReader(backbone.dim, len(names), reader_config).to(device).float().train()
    structure = torch.as_tensor([0.0 if name.startswith(("biz:", "schema:")) else 1.0 for name in names.names], device=device)
    backbone_parameters = [parameter for parameter in backbone.parameters() if parameter.requires_grad]
    optimizer = torch.optim.AdamW([{"params": backbone_parameters, "lr": config.lr}, {"params": list(reader.parameters()), "lr": config.head_lr}], weight_decay=config.weight_decay)
    warmup = max(1, config.steps // 20)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda step: min(1.0, (step + 1) / warmup) * max(0.0, (config.steps - step) / max(1, config.steps - warmup)))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "held_families.json").write_text(json.dumps(sorted({example.family for example in held}), indent=0), encoding="utf-8", newline="\n")
    rng = random.Random(config.seed)
    order = sorted(kept, key=lambda item: item.key)
    rng.shuffle(order)
    best = -1.0
    started = time.time()
    first, last = [], []
    history = []
    use_amp = config.amp
    with open(out / "log.jsonl", "a", encoding="utf-8", newline="\n") as log:
        step = cursor = 0
        tick = time.perf_counter()
        spent = {"build": 0.0, "forward": 0.0, "texts": 0, "links": 0}
        while step < config.steps:
            example = order[cursor % len(order)]
            cursor += 1
            built = time.perf_counter()
            batch = build_batch(example, reader_config, names, rng, questions=config.questions, shares=config.augment)
            if batch is None or not batch.targets:
                continue
            spent["build"] += time.perf_counter() - built
            forward = time.perf_counter()
            with torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
                outputs = run_batch(backbone, reader, reader_config, batch, device)
            with torch.autocast(device_type=device, enabled=False):
                parts = losses_of(reader, names, batch, outputs, config.open_detached, structure, device)
                loss = sum(LOSS_WEIGHTS[name] * value for name, value in parts.items())
            (loss / config.accumulate).backward()
            if (step + 1) % config.accumulate == 0:
                nn.utils.clip_grad_norm_([*backbone_parameters, *reader.parameters()], 1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
            scheduler.step()
            spent["forward"] += time.perf_counter() - forward
            spent["texts"] += batch.texts
            spent["links"] += len(batch.links)
            step += 1
            value = float(loss.detach())
            (first if step <= max(10, config.steps // 10) else last).append(value)
            last = last[-max(10, config.steps // 10):]
            if step % 10 == 0 or step == config.steps:
                now = time.perf_counter()
                row = {"step": step, "loss": round(value, 4), **{name: round(float(part.detach()), 4) for name, part in parts.items()}, "seconds": round(time.time() - started, 1), "seconds_per_step": round((now - tick) / 10, 3), "build_seconds_per_step": round(spent["build"] / max(1, step), 3), "forward_seconds_per_step": round(spent["forward"] / max(1, step), 3), "texts_per_step": round(spent["texts"] / max(1, step), 1), "links_per_step": round(spent["links"] / max(1, step), 1)}
                tick = now
                log.write(json.dumps(row) + "\n")
                log.flush()
                history.append(row)
                if log_print and step % 50 == 0:
                    print(json.dumps(row), flush=True)
            if (step % config.eval_every == 0 or step == config.steps) and held:
                metrics = held_metrics(backbone, reader, reader_config, names, held, device, config.eval_tabs, config.seed)
                metrics.update(step=step, seconds=round(time.time() - started, 1))
                log.write(json.dumps(metrics) + "\n")
                log.flush()
                if log_print:
                    print(json.dumps(metrics), flush=True)
                if metrics["link_top10"] > best:
                    best = metrics["link_top10"]
                    write_bundle(out / "best", backbone, reader, names, reader_config, link_weights_of(reader), signatures=signatures, record={**(record or {}), "metrics": metrics, "step": step, "seed": config.seed})
                    (out / "best.json").write_text(json.dumps({**metrics, **(record or {})}, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    if not held:
        write_bundle(out / "best", backbone, reader, names, reader_config, link_weights_of(reader), signatures=signatures, record={**(record or {}), "step": step, "seed": config.seed})
    return {"steps": step, "loss_first": round(float(np.mean(first)), 4), "loss_last": round(float(np.mean(last or first)), 4), "seconds": round(time.time() - started, 1), "train_tabs": len(kept), "held_tabs": len(held), "best_top10": best, "seconds_per_step": round((time.time() - started) / max(1, step), 3), "build_seconds_per_step": round(spent["build"] / max(1, step), 3), "texts_per_step": round(spent["texts"] / max(1, step), 1), "links_per_step": round(spent["links"] / max(1, step), 1)}


def names_for(examples: list[TabExample]) -> NameSet:
    """The name set's structure and vocabulary, and the schema.org names these labels use."""
    extra = sorted({name for example in examples for label in example.labels.values() for key in ("coordinates", "value_coordinates") for name in label.get(key) or () if name.startswith("schema:")})
    return load_name_set(extra=extra)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the named reader on family-held-out examples. Candidates only; promotion is separate.")
    parser.add_argument("--examples")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--out")
    parser.add_argument("--fake", action="store_true", help="the small fake and the hashing backbone: a pipeline check")
    parser.add_argument("--backbone", default="bge-small", choices=("bge-small", "xlmr-base"))
    parser.add_argument("--layers", type=int, help="keep the backbone's first N layers")
    parser.add_argument("--hidden", type=int, default=384)
    parser.add_argument("--blocks", type=int, default=3)
    parser.add_argument("--sample-rows", type=int, default=8)
    parser.add_argument("--window-columns", type=int, default=48)
    parser.add_argument("--steps", type=int, default=300)
    parser.add_argument("--accumulate", type=int, default=1, help="tabs read per optimizer step")
    parser.add_argument("--questions", type=int, default=8)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--head-lr", type=float, default=1e-3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--held-share", type=float, default=0.1)
    parser.add_argument("--eval-every", type=int, default=500)
    parser.add_argument("--eval-tabs", type=int, default=200)
    parser.add_argument("--amp", action="store_true")
    parser.add_argument("--open-detached", action="store_true")
    parser.add_argument("--no-augment", action="store_true")
    args = parser.parse_args()
    out = Path(args.out) if args.out else EXPERIMENTS_DIR / args.experiment / "out"
    record = {"experiment": args.experiment, **_commit(), "examples": args.examples, "fake": args.fake, "flags": {k: v for k, v in vars(args).items() if k not in ("examples", "experiment", "out", "fake")}}
    if args.fake:
        from training.named.fake import HashBackbone, fake_examples, fake_name_set
        examples, names, backbone = fake_examples(), fake_name_set(), HashBackbone()
        hidden = min(args.hidden, 48)
        blocks = min(args.blocks, 2)
        reader_config = ReaderConfig(backbone="hash", hidden=hidden, blocks=blocks, sample_rows=min(args.sample_rows, 4))
        signatures = None
    else:
        from engine.named.backbone import load_backbone
        examples = read_examples(args.examples)
        record["examples_sha256"] = examples_sha(args.examples)
        names = names_for(examples)
        backbone = load_backbone(args.backbone, layers=args.layers)
        backbone.freeze_table()
        signatures = ClassSignatures.from_groups(meaning_groups())
        reader_config = ReaderConfig(backbone=args.backbone, backbone_layers=args.layers, hidden=args.hidden, blocks=args.blocks, sample_rows=args.sample_rows, window_columns=args.window_columns)
    record["names_sha"] = names.sha
    config = TrainConfig(reader=reader_config, steps=args.steps, accumulate=args.accumulate, questions=args.questions, lr=args.lr, head_lr=args.head_lr, seed=args.seed, held_share=args.held_share, eval_every=args.eval_every, eval_tabs=args.eval_tabs, amp=args.amp, open_detached=args.open_detached, augment={} if args.no_augment else dict(TRAINING_SHARES))
    summary = train(examples, names, backbone, config, out, record=record, signatures=signatures)
    (out / "summary.json").write_text(json.dumps({**summary, **record}, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, sort_keys=True), flush=True)


__all__ = ["Batch", "TrainConfig", "build_batch", "held_metrics", "link_weights_of", "losses_of", "main", "run_batch", "train"]

if __name__ == "__main__":
    main()
