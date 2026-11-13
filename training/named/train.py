"""Multi-task training of the reader, on family-held-out examples.

Losses, each on named outputs. Every coordinate counts its own log loss, unweighted, so what it outputs is a probability: the structure tier's names always; each basis property (engine/named/model.py) on units whose meaning is known, the anchor that keeps each output its property's. A link's sum means how much two readings share.
  target coordinates   a column's, value's, line's and band's structure names, always: what the compiler knows (column role, value kind, line role); and the basis properties where Gemini's label speaks (training/factory/schema_labels.py): a basis property at the threshold is on and the rest off, a property outside the basis or none all off, an unsure label nothing
  word coordinates     the same head over the question's words: a word in a link to a column or a value reads that column's properties as known; other words have no meaning label
  self-anchors         each basis property's own schema.org label and comment, as a lone header, reads that property at 1; a property the frozen base encoder reads at RELATED_AT or more on that text keeps that soft value, every other goes to 0. Only schema.org's text: no structure definition is read. --anchor-steps trains the property rows alone on them first (backbone and relational blocks frozen); afterwards they stay as an auxiliary loss
  picks                --picks: training-split questions with CLEF's recorded reading (each field's options as program coordinates and the one it picked at PICK_MIN or more). For a field over sheet targets (measure, key, shown, filter, value, line, section, table) the link to CLEF's pick must beat the links to the other options and "none of these": the same sum of named terms and exact-text evidence serving computes, the span the best-linking word, as serving chooses it. No target is invented for any dimension: the gradient moves the named readings, and the anchors above stay on, so a pick can sharpen a dimension and not change what it means. A closed field's pick trains that named head. The unnamed channel never enters this sum
  profiles             --corpus: main's schema.org corpus (training/schema_org, rendered tables of Wikidata and publisher instances with the set of properties each shows): the highest reading of each property over a table's columns must be its set, the table's profile, from which its class is the superposition. The first part of an instance's text names its source relation and so its class: never read
Gemini names a single property per column, so the properties that hold together with it on one column are unknown there, not absent: <examples>/co_held.json lists them, measured on a multi-label pass over one tab of each of 600 train families, and they are masked on a unit labelled with the property alone. Properties whose texts merely read alike (RELATED_AT) keep their negatives: a start date is not an end date. A label of a property outside the basis says nothing of the basis and is masked whole.
  closed heads         form, aggregate, direction, size, entity, comparison, row and calc operators, answer type, blank, how many conditions; word role and calc side per word
  links                link(span, target) over the target and its in-sheet hard negatives: the sum over named coordinates of weight x question x target, plus named rule evidence and the word's role. The one formula of engine/named/links.py, its weights learned here. Beside the candidates stands "links to nothing", at a learned bias per kind of target: a content word no link stands on is an example of it. The bias is what a coordinate reads below at serving, so each kind needs its own such examples: a word linked to a column links to no value, and one linked to a value to no column. (With examples on columns alone the value bias stayed at .58, under what a value read from its word's role alone, and spurious values won: 57 of 207 misses lost most on the value slot.)
  open                 the separate unnamed channel, on the same candidates. It trains its own two projections and, unless --open-detached, the shared encoder.

Each property's output starts at its own text as the backbone reads it at the start; from there its labels anchor it.

Each step reads one tab, and --accumulate of them make one optimizer step: its tables through the augmentations (renames, masks, shuffles, distractor columns; training/named/augment.py), its questions with the question words alone. A checkpoint is chosen on held-out families by top-1 link recall (top 10 among a link's in-sheet candidates is nearly full early on, and says little). float32 unless --amp. A candidate goes only to training/named/data/experiments/<id>/out/: promotion is separate.

Training needs an experiment contract (design/phase4_contract.txt, CLAUDE.md). This module incurs no spend by itself; a lease does (training/named/lease.py).

usage: python -m training.named.train --examples <dir> --experiment <id> --backbone bge-small [--steps N] [--accumulate 4] [--held-share 0.1] [--seed 0] [--blocks 3] [--hidden 384] [--layers N] [--sample-rows 8] [--questions 8] [--lr 2e-5] [--head-lr 1e-3] [--out <dir>] [--eval-every 500] [--amp]
       --fake runs on the small fake with the hashing backbone: a pipeline check, never a result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil
import torch
from torch import nn

from engine.named.backbone import question_words
from engine.named.index import ClassSignatures
from engine.named.links import LinkWeights, RULES, SLOTS_OF_LINK_KIND, rule_evidence, is_stated_word
from engine.named.model import LINK_KINDS, NamedReader, ReaderConfig
from engine.named.names import CALC_SIDES, CLASS_PREFIX, CLOSED_HEADS, LEFT, MEANING_PREFIX, RIGHT, WORD_ROLE_CLASSES, NameSet, closed_labels, fine_answer_type, load_name_set, meaning_names, property_texts
from engine.named.bundle import write_bundle
from engine.named.units import T_BAND, T_COLUMN, T_LINE, T_TABLE, T_VALUE, Graph, Target, collate, question_graph, rendered_graph, table_graphs, targets_of, value_graph
from engine.program.catalog import Catalog, CatalogTable
from engine.program.decoder import Decoder
from engine.program.names import A_NAME, ROLE_CONTEXT, ROLE_FILTER, ROLE_KEY, ROLE_MEASURE, SLOT_ANSWER_TYPE, SLOT_CALC_LEFT, slot_of
from engine.program.semantics import fold
from engine.program.words import read_question
from training.named import EXPERIMENTS_DIR
from training.named.augment import TRAINING_SHARES, augmented
from training.named.data import TabExample, check_held_out, read_examples, split_families

THRESHOLD = 6              # the factory's: a label at this confidence or more counts
UNKNOWN = -1               # a word whose role or side nothing says
GOLD = "gold"
_ROW_SIDES = re.compile(r"(?:^|\s|\()(\d+)\s*[-/]\s*(\d+)(?:\s|\)|$)")
COLUMN_NEGATIVES = 24
VALUE_NEGATIVES = 16
TABLES_PER_STEP = 3
UNLINKED_WORDS = 2         # a question's content words no link stands on, read as links to nothing
ROW_NAMES = 4.0            # about how many names a target or a word holds: scales the coordinates' summed log loss to one row's worth
LOSS_WEIGHTS = {"target": 1.0, "meaning": 1.0, "word": 1.0, "closed": 1.0, "role": 0.5, "side": 0.25, "link": 2.0, "open": 0.5, "anchor": 0.25, "profile": 1.0, "pick": 1.0, "pick_closed": 0.5}
DESCRIPTION_TOKENS = 64
RELATED_AT = 0.1           # the frozen base encoder reads a property at this or more on another's own text: related, a soft target there and masked on a labelled unit
ANCHOR_EVERY = 4           # the self-anchors and a corpus batch join every this many steps
CORPUS_PER_STEP = 16
CORPUS_VALUES = 6
PICK = "pick"              # a row holding the teacher's picks, no gold program
PICK_EVERY = 2             # a pick batch joins every this many steps
PICK_CANDIDATES = 32       # a field's options read per pick: the pick, then the others in their order
PICK_ROLES = {"measure": ROLE_MEASURE, "key": ROLE_KEY, "shown": ROLE_KEY, "filter": ROLE_FILTER, "value": ROLE_FILTER, "line": ROLE_FILTER, "section": ROLE_FILTER, "table": ROLE_CONTEXT}


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
    link_weight: float = LOSS_WEIGHTS["link"]     # 0: anchoring alone, the links' weights left where they start
    open_weight: float = LOSS_WEIGHTS["open"]
    anchor_steps: int = 0          # the property rows alone on the self-anchors first
    corpus: str | None = None      # the schema.org corpus's instances (semantic_instances.jsonl)
    co_held: str | None = None     # <examples>/co_held.json: the properties that hold together on one column
    picks: str | None = None       # a folder of pick examples (the teacher's recorded picks on training-split questions)
    qualified: str | None = None   # a gate.json: only the properties it qualified for links enter a link's sum
    init: str | None = None        # a bundle to start from: the step before this one
    resume: str | None = None      # a last_state.pt to go on from
    stop_at: int | None = None     # stop here with the state written, for a test


@dataclass
class LinkExample:
    """One link of a question: its question, its words, and its candidates, the first the right one; or, with nothing true, a word that links to none of them. role: the word role its span reads in."""
    question: int
    span: list[int]
    kind: str
    candidates: list[int]
    rules: np.ndarray
    role: str = ROLE_MEASURE
    nothing: bool = False
    any_word: bool = False      # a pick: no span is given, the best-linking of these words is the span


@dataclass
class Batch:
    """One step's read: the graphs, the targets with what is known of them, the questions with their labels, the links."""
    graphs: list[Graph]
    columns: list[tuple[int, int, int]]         # (graph, unit, target)
    tables_at: list[tuple[int, int]]            # (graph, target): a table's target, read from its units
    value_graphs: list[Graph]
    values: list[int]                           # target per value graph
    targets: list[Target]
    positive: list[list[str]]                   # per target: its structure names
    meaning: list[list[str] | None]             # per target: its basis properties, [] when known to hold none, None when unknown
    questions: list[str]
    closed: list[dict[str, str]]
    word_positive: list[list[list[str] | None]]  # per question, per word: basis properties ([] none), or None when unknown
    word_role: list[list[int]]
    word_side: list[list[int]]
    links: list[LinkExample]
    texts: int = 0


def _labelled_names(label: dict | None) -> list[str] | None:
    """What a column's label says of the basis: its properties at the threshold; [] when it said no schema.org property fits; None (unknown, masked) when there is no label, it is unsure, or it names a property outside the basis, which says nothing of the basis."""
    if not label:
        return None
    if str(label.get("raw", "")).lower() == "none":
        return []
    if int(label.get("conf", 0)) < THRESHOLD:
        return None
    return list(label.get("coordinates") or ()) or None


def _meaning_names(label: dict | None) -> list[str] | None:
    """What a column's label says of the basis for the meaning targets alone. An examples folder may give that apart from the
    label the closed heads read, as label["meaning"] (a list of basis properties, [] for none, None for unknown), when the
    meaning comes from another labelling pass than the closed heads' answer types; otherwise it is the label's own reading."""
    if label and "meaning" in label:
        return label["meaning"]
    return _labelled_names(label)


def _value_class(label: dict | None) -> list[str]:
    """A column's values' schema.org class at the threshold, as a class name."""
    if not label or not label.get("value_class") or int(label.get("value_conf", 0)) < THRESHOLD:
        return []
    return [f"{CLASS_PREFIX}{label['value_class']}"]


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
    names = []
    for link in row.get("links", ()):
        if link.get("role") == wanted and link.get("position") is not None:
            label = labels.get(f"{link.get('table')}.{link.get('position')}")
            names.extend(_value_class(label) + (_labelled_names(label) or []))
    for used in row.get("used", ()):
        label = labels.get(f"{used.get('table')}.{used.get('position')}")
        names.extend(_value_class(label) + (_labelled_names(label) or []))
    closed[SLOT_ANSWER_TYPE] = fine_answer_type(coarse, names)
    return closed


def build_batch(example: TabExample, reader_config: ReaderConfig, names: NameSet, rng: random.Random, *, questions: int, shares: dict | None, rows: list[dict] | None = None) -> Batch | None:
    """One tab's step. Tables reach the reader through the augmentations; the same units, targets and rule reading serving uses."""
    units = reader_config.units
    gold = [row for row in example.rows if row.get("label") in (GOLD, PICK)] if rows is None else rows
    if not gold:
        return None
    chosen = rng.sample(gold, min(questions, len(gold))) if rows is None else gold
    tables: dict[str, CatalogTable] = {}
    moved: dict[str, dict[int, int]] = {}
    wanted = {link.get("table") for row in chosen for link in row.get("links", ()) if link.get("table")} | {table for row in chosen for table in row.get("tables", ())}
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
    meaning: list[list[str] | None] = []
    at_target: dict[tuple, int] = {}

    def add_target(target: Target, structure: list[str], properties: list[str] | None) -> int:
        held = at_target.get(target.key)
        if held is None:
            held = at_target[target.key] = len(targets)
            targets.append(target)
            positive.append(structure)
            meaning.append(None if properties is None else [name for name in properties if names.at(name) is not None])
        return held

    all_targets = {}
    for table_id, table in tables.items():
        inverse = {new: old for old, new in moved[table_id].items()}
        for graph in table_graphs(table, units):
            at_graph = len(graphs)
            graphs.append(graph)
            union: set[str] = set()
            for position, unit in sorted(graph.columns.items()):
                column = table.columns[position]
                old = inverse.get(position)
                label_names = _meaning_names(example.labels.get(f"{table_id}.{old}")) if old is not None else None
                union.update(label_names or [])
                at = add_target(Target(table_id, T_COLUMN, position, column.name), _column_structure(column), label_names)
                columns.append((at_graph, unit, at))
            at = add_target(Target(table_id, T_TABLE, -1, ""), [], sorted(union) if union else None)
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
            at = add_target(target, ["value_kind:text"], _meaning_names(example.labels.get(f"{target.table}.{inverse.get(target.position)}")))
        else:
            at = add_target(target, [f"line_role:{'band' if target.kind == T_BAND else 'total'}"], None)
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
        if row.get("label") == PICK:
            closed.append({head: (row.get("closed") or {}).get(head) for head in CLOSED_HEADS})     # None: the teacher did not say
            word_positive.append([None for _ in words])
            word_role.append([UNKNOWN] * len(words))
            word_side.append([UNKNOWN] * len(words))
            links.extend(pick_links(row, tables, all_targets, read_question(question, catalog, decoder).scores, set(words), at_question, words, at_target, add_value))
            continue
        closed.append(closed_of(row, example.labels))
        read_as: list[list[str] | None] = [None for _ in words]     # no word is listed as meaning nothing: what a word reads as comes from the links alone
        roles = [0] * len(words)
        sides = [0] * len(words)
        left_names = {name.split(":", 1)[1] for name in row.get("coordinates", ()) if slot_of(name) == SLOT_CALC_LEFT}
        calc = row.get("form") == "calc"
        row_sides = _ROW_SIDES.search(str(row.get("program", "")).split(" where ")[0]) if not calc else None
        floor = read_question(question, catalog, decoder).scores
        stems = set(words)
        for link in row.get("links", ()):
            span = [at for at in link.get("span", ()) if at < len(words)]
            if not span:
                continue
            role = link.get("role")
            known = _meaning_names(example.labels.get(f"{link.get('table')}.{link.get('position')}")) if link.get("kind") in ("column", "value") else None
            names_of_link = None if known is None else [name for name in known if names.at(name) is not None]
            for at in span:
                if names_of_link is not None:
                    read_as[at] = sorted(set(read_as[at] or []) | set(names_of_link))
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
                    links.append(LinkExample(at_question, span, "table", [at_target[item.key] for item in candidates], np.stack([_rule_row(item.coordinates(), floor, stems, tables[item.table]) for item in candidates]), ROLE_CONTEXT))
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
                links.append(LinkExample(at_question, span, kind, candidates, rules, role if role in WORD_ROLE_CLASSES else ROLE_FILTER))
                # the same words read as the other kind: nothing there
                if kind == "column":
                    nothing = _nothing_among_values(all_targets[table_id], span, at_question, floor, stems, rng, add_value)
                elif kind == "value":
                    nothing = _nothing_among_columns(table, table_id, span, at_question, floor, stems, rng, at_target)
                else:
                    nothing = None
                if nothing is not None:
                    links.append(nothing)
        unlinked = [at for at, word in enumerate(words) if read_as[at] is None and not is_stated_word(word) and not any(at in link.get("span", ()) for link in row.get("links", ()))]
        rng.shuffle(unlinked)
        for at in unlinked[:UNLINKED_WORDS]:
            table_id = sorted(tables)[rng.randrange(len(tables))]
            for nothing in (_nothing_among_columns(tables[table_id], table_id, [at], at_question, floor, stems, rng, at_target), _nothing_among_values(all_targets[table_id], [at], at_question, floor, stems, rng, add_value)):
                if nothing is not None:
                    links.append(nothing)
        word_positive.append(read_as)
        word_role.append(roles)
        word_side.append(sides)
    return Batch(graphs, columns, tables_at, value_graphs, values, targets, positive, meaning, texts, closed, word_positive, word_role, word_side, links)


def _nothing_among_columns(table, table_id, span, question, floor, stems, rng, at_target) -> LinkExample | None:
    """These words link to no column of this table."""
    pool = [column for column in table.columns if Target(table_id, T_COLUMN, column.position, column.name).key in at_target]
    rng.shuffle(pool)
    if not pool:
        return None
    candidates = [Target(table_id, T_COLUMN, column.position, column.name) for column in pool[:COLUMN_NEGATIVES]]
    return LinkExample(question, span, "column", [at_target[item.key] for item in candidates], np.stack([_rule_row(item.coordinates(), floor, stems, table) for item in candidates]), ROLE_MEASURE, True)


def _nothing_among_values(targets, span, question, floor, stems, rng, add_value) -> LinkExample | None:
    """These words link to no stored value of this table: those their words reach, then any."""
    pool = [target for target in targets if target.kind == T_VALUE]
    if not pool:
        return None
    reached = [target for target in pool if target.coordinates()[0] in floor]
    rest = [target for target in pool if target.coordinates()[0] not in floor]
    rng.shuffle(reached)
    rng.shuffle(rest)
    candidates = (reached + rest)[:VALUE_NEGATIVES]
    return LinkExample(question, span, "value", [add_value(item) for item in candidates], np.stack([_rule_row(item.coordinates(), floor, stems, None) for item in candidates]), ROLE_FILTER, True)


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
    """What a batch read: each target's and word's hidden state and its own text as the backbone read it (the meaning tier's base), and the closed heads' logits."""
    target_hidden: torch.Tensor
    target_base: torch.Tensor
    words: list[list[str]]
    word_hidden: list[torch.Tensor]
    word_base: list[torch.Tensor]
    closed_logits: dict[str, torch.Tensor]

    def target_read(self, reader: NamedReader) -> torch.Tensor:
        return reader.coordinate_probabilities(self.target_hidden, self.target_base)

    def word_read(self, reader: NamedReader, question: int) -> torch.Tensor:
        return reader.coordinate_probabilities(self.word_hidden[question], self.word_base[question])


def run_batch(backbone: nn.Module, reader: NamedReader, config: ReaderConfig, batch: Batch, device: str) -> Forward:
    """The reader over a batch: one read of each distinct text, each table graph alone, value graphs together."""
    texts = [text for graph in (*batch.graphs, *batch.value_graphs) for text in graph.texts]
    distinct, vectors = backbone.encode_texts(texts, max_tokens=config.text_tokens)
    batch.texts = len(distinct)
    lookup = {text: at for at, text in enumerate(distinct)}

    def stack(graph: Graph) -> torch.Tensor:
        return vectors[torch.as_tensor([lookup[text] for text in graph.texts], device=vectors.device)] if graph.texts else torch.zeros((1, backbone.dim), device=vectors.device)

    by_graph: dict[int, torch.Tensor] = {}
    base_of: dict[int, torch.Tensor] = {}
    for at, graph in enumerate(batch.graphs):
        base_of[at] = stack(graph)
        inputs, edges, pad = collate([graph], [base_of[at]])
        by_graph[at] = reader.hidden(inputs, edges, pad)[0]
    rows, sources, bases = [], [], []
    for graph_at, unit, target in batch.columns:
        rows.append(target)
        sources.append(by_graph[graph_at][unit])
        bases.append(base_of[graph_at][unit])
    per_table: dict[int, list[tuple[torch.Tensor, torch.Tensor]]] = {}
    for graph_at, target in batch.tables_at:
        table_unit = batch.graphs[graph_at].table
        per_table.setdefault(target, []).append((by_graph[graph_at][table_unit], base_of[graph_at][table_unit]))
    for target, parts in per_table.items():
        rows.append(target)
        sources.append(torch.stack([hidden for hidden, _ in parts]).mean(0))
        bases.append(torch.stack([base for _, base in parts]).mean(0))
    if batch.value_graphs:
        value_bases = [stack(graph) for graph in batch.value_graphs]
        inputs, edges, pad = collate(batch.value_graphs, value_bases)
        read = reader.hidden(inputs, edges, pad)
        rows.extend(batch.values)
        sources.extend(read[:, 1].unbind(0))
        bases.extend(base[1] for base in value_bases)
    hidden = torch.zeros((len(batch.targets), config.hidden), device=device)
    target_base = torch.zeros((len(batch.targets), backbone.dim), device=device)
    if rows:
        read_out = torch.stack(sources).float()          # what the encoder read under autocast comes out in float32: the head and the losses count in it
        index = torch.as_tensor(rows, device=hidden.device)
        hidden = hidden.index_copy(0, index, read_out)
        target_base = target_base.index_copy(0, index, torch.stack(bases).float())
    questions = backbone.encode_questions(batch.questions, max_tokens=config.question_tokens)
    graphs = [question_graph(words) for words, _ in questions]
    inputs, edges, pad = collate(graphs, [vectors_q if len(words) else torch.zeros((1, backbone.dim), device=vectors.device) for words, vectors_q in questions])
    word_hidden_all = reader.hidden(inputs, edges, pad).float()
    closed = reader.closed_logits(word_hidden_all, pad)
    word_hidden = [word_hidden_all[at, :len(words)] for at, (words, _) in enumerate(questions)]
    word_base = [vectors_q[:len(words)].float() for words, vectors_q in questions]
    return Forward(hidden, target_base, [words for words, _ in questions], word_hidden, word_base, closed)


def _row_loss(logits: torch.Tensor, labels: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """Each name's own log loss, summed over a row's names and averaged over its rows: minimized by the true probability, so a score reads as one."""
    losses = nn.functional.binary_cross_entropy_with_logits(logits, labels, reduction="none") * mask
    return losses.sum() / max(1, logits.shape[0]) / ROW_NAMES


def meaning_targets(labelled: list[list[str] | None], names: NameSet, related: torch.Tensor | None, device) -> tuple[list[int], torch.Tensor, torch.Tensor]:
    """(the known rows, their targets, their masks): a labelled property on, the properties related to it masked, the rest off."""
    rows, wanted, masks = [], [], []
    for at, held in enumerate(labelled):
        if held is None:
            continue
        target = torch.zeros(names.meaning, device=device)
        mask = torch.ones(names.meaning, device=device)
        on = [names.at(name) - names.meaning_start for name in held if names.at(name) is not None]
        for position in on:
            if related is not None:
                mask[related[position]] = 0.0
        for position in on:
            target[position] = 1.0
            mask[position] = 1.0
        rows.append(at)
        wanted.append(target)
        masks.append(mask)
    empty = torch.zeros((0, names.meaning), device=device)
    return rows, (torch.stack(wanted) if rows else empty), (torch.stack(masks) if rows else empty)


def _meaning_loss(logits: torch.Tensor, labelled: list[list[str] | None], names: NameSet, related: torch.Tensor | None = None) -> torch.Tensor | None:
    """The anchor: each basis property's own log loss on the rows whose meaning is known. None when no row is known."""
    rows, wanted, masks = meaning_targets(labelled, names, related, logits.device)
    if not rows:
        return None
    return _row_loss(logits[torch.as_tensor(rows, device=logits.device)], wanted, masks)


@dataclass
class SelfAnchors:
    """Each basis property's own schema.org text as a lone header (its label and comment, then its label alone), what each should read, and which properties' texts read alike."""
    graphs: list[Graph]
    targets: torch.Tensor       # [texts, properties]
    related: torch.Tensor       # [properties, properties] bool, symmetric, never a property with itself


def co_held_matrix(path: str | Path, names: NameSet, device: str) -> torch.Tensor:
    """[properties, properties] bool: row P marks the properties that hold together with P on one column, masked on a unit labelled P alone."""
    out = torch.zeros((names.meaning, names.meaning), dtype=torch.bool, device=device)
    for name, others in json.loads(Path(path).read_text(encoding="utf-8"))["co_held"].items():
        for other in others:
            if names.at(name) is not None and names.at(other) is not None:
                out[names.at(name) - names.meaning_start, names.at(other) - names.meaning_start] = True
    return out


def self_anchors(backbone, reader: NamedReader, names: NameSet, device: str) -> SelfAnchors:
    """Built once, at the start, from the base encoder and the outputs' starting rows (read_descriptions), before any state is loaded: a text's own property 1; a property read at RELATED_AT or more keeps that value; every other 0."""
    text_of = dict(zip(meaning_names(), property_texts()))
    texts = [text_of[name] for name in names.names[names.meaning_start:]]
    was_training = backbone.training
    backbone.eval()
    with torch.no_grad():
        distinct, vectors = backbone.encode_texts(texts, max_tokens=DESCRIPTION_TOKENS, batch=64)
        at = {text: row for row, text in enumerate(distinct)}
        base = torch.stack([vectors[at[text]] for text in texts]).float()
        read = torch.sigmoid(reader.meaning_text(nn.functional.normalize(base, dim=-1)))
    backbone.train(was_training)
    targets = torch.where(read >= RELATED_AT, read, torch.zeros_like(read))
    targets.fill_diagonal_(1.0)
    related = (targets >= RELATED_AT) | (targets.T >= RELATED_AT)
    related.fill_diagonal_(False)
    labels = [text.split(":", 1)[0] for text in texts]
    return SelfAnchors([rendered_graph([(text, [])]) for text in (*texts, *labels)], torch.cat([targets, targets]).to(device), related.to(device))


def _read_rendered(backbone, reader: NamedReader, graphs: list[Graph], tokens: int) -> tuple[torch.Tensor, list[torch.Tensor]]:
    """(hidden [graphs, units, hidden], each graph's units' own text vectors)."""
    distinct, vectors = backbone.encode_texts([text for graph in graphs for text in graph.texts], max_tokens=tokens, batch=64)
    lookup = {text: row for row, text in enumerate(distinct)}
    bases = [vectors[torch.as_tensor([lookup[text] for text in graph.texts], device=vectors.device)].float() for graph in graphs]
    inputs, edges, pad = collate(graphs, bases)
    return reader.hidden(inputs, edges, pad).float(), bases


def anchor_loss(backbone, reader: NamedReader, anchors: SelfAnchors) -> torch.Tensor:
    hidden, bases = _read_rendered(backbone, reader, anchors.graphs, DESCRIPTION_TOKENS)
    logits = reader.meaning_logits(hidden[:, 1], torch.stack([base[1] for base in bases]))     # unit 1: the header after the table unit
    return _row_loss(logits, anchors.targets, torch.ones_like(anchors.targets))


def train_self_anchors(backbone, reader: NamedReader, anchors: SelfAnchors, steps: int, lr: float) -> dict:
    """Step 1: the property rows alone on the self-anchors, backbone and relational blocks frozen (read once). Returns what the check needs: every text's reading of its own property, the highest reading of an unrelated one, and the pairs still read at 0.3 or more."""
    was_backbone, was_reader = backbone.training, reader.training
    backbone.eval()
    reader.eval()
    with torch.no_grad():
        hidden, bases = _read_rendered(backbone, reader, anchors.graphs, DESCRIPTION_TOKENS)
        hidden, base = hidden[:, 1], torch.stack([item[1] for item in bases])
    rows = [reader.meaning_text.weight, reader.meaning_text.bias, reader.meaning_read.weight, reader.meaning_read.bias]
    optimizer = torch.optim.Adam(rows, lr=lr)
    with torch.enable_grad():
        for _ in range(steps):
            loss = _row_loss(reader.meaning_logits(hidden, base), anchors.targets, torch.ones_like(anchors.targets))
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
    with torch.no_grad():
        read = torch.sigmoid(reader.meaning_logits(hidden, base))[: anchors.related.shape[0]]
    backbone.train(was_backbone)
    reader.train(was_reader)
    own = read.diagonal()
    unrelated = read.masked_fill(anchors.related | torch.eye(len(own), dtype=torch.bool, device=read.device), 0.0)
    close = [(int(a), int(b), round(float(read[a, b]), 3)) for a, b in (read * anchors.related).ge(0.3).nonzero().tolist()]
    return {"steps": steps, "own_lowest": round(float(own.min()), 4), "unrelated_highest": round(float(unrelated.max()), 4), "related_pairs": int(anchors.related.sum()) // 2, "read_at_0.3_or_more": close}


@dataclass
class CorpusInstance:
    """One rendered table of the schema.org corpus: its columns as (header, sample values), the basis properties it shows, its classes, its split."""
    columns: list[tuple[str, list[str]]]
    properties: list[str]
    classes: list[str]
    split: str


def read_corpus(path: str | Path, names: NameSet) -> list[CorpusInstance]:
    """training/schema_org's semantic_instances.jsonl. An instance's text is 'table <source relation> | header: values; ... | ...': the first part is dropped, as it names the class."""
    out = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            columns = []
            for part in row["text"].split(" | ")[1:]:
                header, _, values = part.partition(": ")
                columns.append((header, [value for value in values.split("; ") if value][:CORPUS_VALUES]))
            if columns:
                shown = [f"{MEANING_PREFIX}{uri.rsplit('/', 1)[-1]}" for uri in row["properties"]]
                out.append(CorpusInstance(columns, [name for name in shown if names.at(name) is not None], [uri.rsplit("/", 1)[-1] for uri in row["classes"]], row["split"]))
    return out


def profile_logits(backbone, reader: NamedReader, config: ReaderConfig, instances: list[CorpusInstance]) -> torch.Tensor:
    """[instances, properties]: each table's profile, the highest logit of each property over its columns' name units."""
    graphs = [rendered_graph(instance.columns) for instance in instances]
    hidden, bases = _read_rendered(backbone, reader, graphs, config.text_tokens)
    profiles = []
    for at, graph in enumerate(graphs):
        units = torch.as_tensor(sorted(graph.columns.values()), device=hidden.device)
        profiles.append(reader.meaning_logits(hidden[at, units], bases[at][units]).max(0).values)
    return torch.stack(profiles)


def profile_loss(backbone, reader: NamedReader, config: ReaderConfig, names: NameSet, instances: list[CorpusInstance], related: torch.Tensor | None) -> torch.Tensor:
    logits = profile_logits(backbone, reader, config, instances)
    _, wanted, masks = meaning_targets([instance.properties for instance in instances], names, related, logits.device)
    return _row_loss(logits, wanted, masks)


def pick_links(row: dict, tables: dict, all_targets: dict, floor: dict, stems: set[str], question: int, words: list[str], at_target: dict, add_value) -> list[LinkExample]:
    """The teacher's picks of one question as links without a span: per field over sheet targets, the options that are single targets of the batch, the pick first; or, where it picked none of them, a link to nothing. An option that is no single target (two columns under an operator) is left out, and a field whose pick is such an option is skipped."""
    span = [at for at, word in enumerate(words) if not is_stated_word(word)]
    if not span:
        return []
    by_name = {name: target for targets in all_targets.values() for target in targets for name in target.coordinates()}
    out = []
    for asked, role in PICK_ROLES.items():
        pick = (row.get("picks") or {}).get(asked)
        if not pick:
            continue
        options = [by_name.get(name) for name in pick["coordinates"]]
        chosen = options[pick["pick"]] if pick["pick"] is not None else None
        if pick["pick"] is not None and chosen is None:
            continue
        candidates = ([chosen] if chosen is not None else []) + [target for target in options if target is not None and target is not chosen]
        candidates = candidates[:PICK_CANDIDATES]
        if not candidates or len({target.kind for target in candidates}) != 1:
            continue
        placed = []
        for target in candidates:
            at = at_target.get(target.key) if target.kind in (T_COLUMN, T_TABLE) else add_value(target)
            if at is None:              # a column of a table this step did not read
                placed = []
                break
            placed.append(at)
        if len(placed) < (2 if chosen is not None else 1):
            continue
        rules = np.stack([_rule_row(target.coordinates(), floor, stems, tables[target.table] if target.kind in (T_COLUMN, T_TABLE) else None) for target in candidates])
        out.append(LinkExample(question, span, candidates[0].kind, placed, rules, role, nothing=chosen is None, any_word=True))
    return out


def losses_of(reader: NamedReader, names: NameSet, batch: Batch, out: Forward, open_detached: bool, device: str, related: torch.Tensor | None = None, qualified: torch.Tensor | None = None) -> dict[str, torch.Tensor]:
    structure_labels = torch.zeros((len(batch.targets), names.meaning_start), device=device)
    for at, held in enumerate(batch.positive):
        for name in held:
            position = names.at(name)
            if position is not None and position < names.meaning_start:
                structure_labels[at, position] = 1.0
    structure_logits = reader.structure(out.target_hidden)
    zero = structure_logits.sum() * 0.0
    result = {"target": _row_loss(structure_logits, structure_labels, torch.ones_like(structure_labels))}
    meaning = _meaning_loss(reader.meaning_logits(out.target_hidden, out.target_base), batch.meaning, names, related)
    result["meaning"] = meaning if meaning is not None else zero
    word_losses = []
    role_losses = []
    side_losses = []
    for at, word_names in enumerate(batch.word_positive):
        if not len(out.word_hidden[at]):
            continue
        word_meaning = _meaning_loss(reader.meaning_logits(out.word_hidden[at], out.word_base[at]), word_names, names, related)
        if word_meaning is not None:
            word_losses.append(word_meaning)
        if UNKNOWN not in batch.word_role[at]:
            role_losses.append(nn.functional.cross_entropy(reader.word_role(out.word_hidden[at]), torch.as_tensor(batch.word_role[at], device=device)))
            side_losses.append(nn.functional.cross_entropy(reader.calc_side(out.word_hidden[at]), torch.as_tensor(batch.word_side[at], device=device)))
    result["word"] = torch.stack(word_losses).mean() if word_losses else zero
    result["role"] = torch.stack(role_losses).mean() if role_losses else zero
    result["side"] = torch.stack(side_losses).mean() if side_losses else zero
    closed_terms = []
    for head, logits in out.closed_logits.items():
        known = [at for at, row in enumerate(batch.closed) if row[head] is not None]
        if known:
            wanted = torch.as_tensor([CLOSED_HEADS[head].index(batch.closed[at][head]) for at in known], device=device)
            closed_terms.append(nn.functional.cross_entropy(logits[torch.as_tensor(known, device=device)], wanted))
    result["closed"] = torch.stack(closed_terms).mean() if closed_terms else zero
    link_terms, open_terms, pick_terms = [], [], []
    if batch.links:
        weights = reader.coordinate_weights() if qualified is None else reader.coordinate_weights() * qualified
        rule_weights = reader.rule_weights()
        biases = reader.link_biases()
        targets_read = out.target_read(reader)
        words_by_question = {question: out.word_read(reader, question) for question in sorted({link.question for link in batch.links})}
        open_targets = reader.open_target(out.target_hidden.detach() if open_detached else out.target_hidden)
        for link in batch.links:
            candidates = torch.as_tensor(link.candidates, device=device)
            rules = torch.as_tensor(link.rules, device=device)
            roles = torch.softmax(reader.word_role(out.word_hidden[link.question][link.span]), -1)[:, WORD_ROLE_CLASSES.index(link.role)]
            wanted = torch.as_tensor([0 if link.nothing else 1], device=device)
            if link.any_word:
                # each candidate's link from the word that links it best: the span serving would choose
                best = (weights * words_by_question[link.question][link.span][:, None, :] * targets_read[candidates][None]).sum(-1).max(0)
                scores = best.values + (rules * rule_weights).sum(-1) + reader.role_weight() * roles[best.indices]
                pick_terms.append(nn.functional.cross_entropy(torch.cat([biases[LINK_KINDS.index(link.kind)][None], scores])[None], wanted))
                continue
            words_read = words_by_question[link.question][link.span].mean(0)
            scores = (weights * words_read * targets_read[candidates]).sum(-1) + (rules * rule_weights).sum(-1) + reader.role_weight() * roles.mean()
            link_terms.append(nn.functional.cross_entropy(torch.cat([biases[LINK_KINDS.index(link.kind)][None], scores])[None], wanted))
            if link.nothing:
                continue
            wanted = torch.zeros(1, dtype=torch.long, device=device)
            hidden = out.word_hidden[link.question][link.span].mean(0)
            word_open = nn.functional.normalize(reader.open_word(hidden.detach() if open_detached else hidden), dim=-1)
            target_open = nn.functional.normalize(open_targets[candidates], dim=-1)
            open_terms.append(nn.functional.cross_entropy(((word_open * target_open).sum(-1) / 0.1)[None], wanted))
    result["link"] = torch.stack(link_terms).mean() if link_terms else zero
    result["open"] = torch.stack(open_terms).mean() if open_terms else zero
    result["pick"] = torch.stack(pick_terms).mean() if pick_terms else zero
    return result


def qualified_mask(path: str | Path, names: NameSet, device: str) -> torch.Tensor:
    """[coordinates] 1 where a coordinate may enter a link's sum: every structure name, and the properties a gate (training/named/gate_dimensions.py's gate.json) qualified for links."""
    passed = set(json.loads(Path(path).read_text(encoding="utf-8"))["qualified_for_links"])
    return torch.as_tensor([1.0 if at < names.meaning_start or name in passed else 0.0 for at, name in enumerate(names.names)], device=device)


def link_weights_of(reader: NamedReader, qualified: torch.Tensor | None = None) -> LinkWeights:
    """The learned weights as the plain numbers a bundle carries: a property the gate did not qualify weighs nothing in a link."""
    with torch.no_grad():
        biases = {slot: round(float(value), 6) for kind, value in zip(LINK_KINDS, reader.link_biases().cpu().tolist()) for slot in SLOTS_OF_LINK_KIND[kind]}
        return LinkWeights((reader.coordinate_weights() if qualified is None else reader.coordinate_weights() * qualified).cpu().double().numpy().round(6), {rule: round(float(value), 6) for rule, value in zip(RULES, reader.rule_weights().cpu().tolist())}, round(float(reader.role_weight()), 6), round(float(reader.closed_weight()), 6), biases)


def held_metrics(backbone, reader, config, names, examples, device, limit: int, seed: int, qualified: torch.Tensor | None = None) -> dict:
    """Held-out families, no augmentation: where each link's target ranks among its candidates under the sum a bundle serves (a property the gate did not qualify weighs nothing), and the closed heads."""
    reader.eval()
    backbone.eval()
    rng = random.Random(seed)
    top1 = top10 = links = nothing_links = nothing_right = labelled = labelled_right = 0
    right = {head: 0 for head in CLOSED_HEADS}
    questions = 0
    with torch.no_grad():
        weights = reader.coordinate_weights() if qualified is None else reader.coordinate_weights() * qualified
        rule_weights = reader.rule_weights()
        for example in sorted(examples, key=lambda item: item.key)[:limit]:
            batch = build_batch(example, config, names, rng, questions=8, shares=None)
            if batch is None:
                continue
            out = run_batch(backbone, reader, config, batch, device)
            read = out.target_read(reader)
            words = {question: out.word_read(reader, question) for question in sorted({link.question for link in batch.links})}
            meaning_at = torch.as_tensor([at for at, held in enumerate(batch.meaning) if held], device=device)     # units with a basis property: is the top output one of theirs
            if len(meaning_at):
                top = read[meaning_at, names.meaning_start:].argmax(-1).tolist()
                for at, best in zip(meaning_at.tolist(), top):
                    labelled += 1
                    labelled_right += names.names[names.meaning_start + best] in batch.meaning[at]
            for link in batch.links:
                if link.any_word:
                    continue
                words_read = words[link.question][link.span].mean(0)
                candidates = torch.as_tensor(link.candidates, device=device)
                rules = torch.as_tensor(link.rules, device=device)
                scores = (weights * words_read * read[candidates]).sum(-1) + (rules * rule_weights).sum(-1)
                if link.nothing:
                    nothing_links += 1
                    nothing_right += float(scores.max()) < float(reader.link_biases()[LINK_KINDS.index(link.kind)])
                    continue
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
    return {"links": links, "link_top1": round(top1 / max(1, links), 4), "link_top10": round(top10 / max(1, links), 4), "reads_nothing": round(nothing_right / max(1, nothing_links), 4), "labelled": labelled, "property_top1": round(labelled_right / max(1, labelled), 4), "questions": questions, "closed": {head: round(value / max(1, questions), 4) for head, value in right.items()}}


def pick_metrics(backbone, reader, config, names, examples, device, limit: int, seed: int, qualified: torch.Tensor | None) -> dict:
    """Held-out pick examples (the verifier's held workbooks, never trained on): how often the named link sum puts the teacher's pick first, or, where it picked none of the options, keeps them all under the bias."""
    reader.eval()
    backbone.eval()
    rng = random.Random(seed)
    right = total = 0
    with torch.no_grad():
        weights = reader.coordinate_weights() if qualified is None else reader.coordinate_weights() * qualified
        rule_weights = reader.rule_weights()
        for example in examples[:limit]:
            batch = build_batch(example, config, names, rng, questions=8, shares=None)
            if batch is None or not batch.targets or not batch.links:
                continue
            out = run_batch(backbone, reader, config, batch, device)
            read = out.target_read(reader)
            words = {question: out.word_read(reader, question) for question in sorted({link.question for link in batch.links})}
            for link in batch.links:
                if not link.any_word:
                    continue
                candidates = torch.as_tensor(link.candidates, device=device)
                roles = torch.softmax(reader.word_role(out.word_hidden[link.question][link.span]), -1)[:, WORD_ROLE_CLASSES.index(link.role)]
                best = (weights * words[link.question][link.span][:, None, :] * read[candidates][None]).sum(-1).max(0)
                scores = best.values + (torch.as_tensor(link.rules, device=device) * rule_weights).sum(-1) + reader.role_weight() * roles[best.indices]
                bias = float(reader.link_biases()[LINK_KINDS.index(link.kind)])
                total += 1
                right += float(scores.max()) < bias if link.nothing else (int(scores.argmax()) == 0 and float(scores[0]) > bias)
    reader.train()
    backbone.train()
    return {"picks": total, "pick_top1": round(right / max(1, total), 4)}


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
    reader = NamedReader(backbone.dim, names.meaning_start, names.meaning, reader_config).to(device).float().train()
    read_descriptions(backbone, reader, names)
    anchors = self_anchors(backbone, reader, names, device) if names.meaning else None
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if anchors is not None and config.anchor_steps and not config.resume and not config.init:
        (out / "self_anchors.json").write_text(json.dumps({**train_self_anchors(backbone, reader, anchors, config.anchor_steps, config.head_lr), "names": list(names.names[names.meaning_start:])}, indent=1) + "\n", encoding="utf-8", newline="\n")
    if config.init and not config.resume:
        start_from(config.init, backbone, reader, names, device)
    corpus = [instance for instance in read_corpus(config.corpus, names) if instance.split == "train"] if config.corpus else []
    weights_of_losses = {**LOSS_WEIGHTS, "link": config.link_weight, "open": config.open_weight}
    co_held = co_held_matrix(config.co_held, names, device) if config.co_held else None
    qualified = qualified_mask(config.qualified, names, device) if config.qualified else None
    picks = sorted((example for example in read_examples(config.picks) if example.side == "clef"), key=lambda item: item.key) if config.picks else []
    held_picks = sorted((example for example in read_examples(config.picks) if example.side == "clef_held"), key=lambda item: item.key) if config.picks else []
    rng_picks = random.Random(config.seed + 1)
    rng_picks.shuffle(picks)
    backbone_parameters = [parameter for parameter in backbone.parameters() if parameter.requires_grad]
    optimizer = torch.optim.AdamW([{"params": backbone_parameters, "lr": config.lr}, {"params": list(reader.parameters()), "lr": config.head_lr}], weight_decay=config.weight_decay)
    warmup = max(1, config.steps // 20)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda step: min(1.0, (step + 1) / warmup) * max(0.0, (config.steps - step) / max(1, config.steps - warmup)))
    (out / "held_families.json").write_text(json.dumps(sorted({example.family for example in held}), indent=0), encoding="utf-8", newline="\n")
    rng = random.Random(config.seed)
    order = sorted(kept, key=lambda item: item.key)
    rng.shuffle(order)
    best = -1.0
    started = time.time()
    begin = cursor_at = 0
    if config.resume:
        held_state = torch.load(config.resume, map_location=device, weights_only=False)
        backbone.load_state_dict(held_state["backbone"])
        reader.load_state_dict(held_state["reader"])
        optimizer.load_state_dict(held_state["optimizer"])
        scheduler.load_state_dict(held_state["scheduler"])
        begin, cursor_at, best = int(held_state["step"]), int(held_state["cursor"]), float(held_state["best"])
        rng.setstate(held_state["rng"])
        del held_state
    first, last = [], []
    history = []
    use_amp = config.amp
    with open(out / "log.jsonl", "a", encoding="utf-8", newline="\n") as log:
        step, cursor = begin, cursor_at
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
                parts = losses_of(reader, names, batch, outputs, config.open_detached, device, co_held, qualified)
                if not picks:
                    del parts["pick"]
            if picks and step % PICK_EVERY == 0:
                pick_batch = build_batch(picks[(step // PICK_EVERY) % len(picks)], reader_config, names, rng_picks, questions=config.questions, shares=None)
                if pick_batch is not None and pick_batch.targets:
                    with torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
                        pick_outputs = run_batch(backbone, reader, reader_config, pick_batch, device)
                    with torch.autocast(device_type=device, enabled=False):
                        pick_parts = losses_of(reader, names, pick_batch, pick_outputs, config.open_detached, device, co_held, qualified)
                    parts["pick"], parts["pick_closed"] = pick_parts["pick"], pick_parts["closed"]
            if anchors is not None and step % ANCHOR_EVERY == 0:
                with torch.autocast(device_type=device, dtype=torch.bfloat16, enabled=use_amp):
                    parts["anchor"] = anchor_loss(backbone, reader, anchors)
                    if corpus:
                        parts["profile"] = profile_loss(backbone, reader, reader_config, names, rng.sample(corpus, min(CORPUS_PER_STEP, len(corpus))), None)     # a corpus table's property set is whole
            with torch.autocast(device_type=device, enabled=False):
                loss = sum(weights_of_losses[name] * value.float() for name, value in parts.items())
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
                row = {"step": step, "loss": round(value, 4), **{name: round(float(part.detach()), 4) for name, part in parts.items()}, "rss_gb": round(psutil.Process().memory_info().rss / 1024 ** 3, 2), "seconds": round(time.time() - started, 1), "seconds_per_step": round((now - tick) / 10, 3), "build_seconds_per_step": round(spent["build"] / max(1, step), 3), "forward_seconds_per_step": round(spent["forward"] / max(1, step), 3), "texts_per_step": round(spent["texts"] / max(1, step), 1), "links_per_step": round(spent["links"] / max(1, step), 1)}
                tick = now
                log.write(json.dumps(row) + "\n")
                log.flush()
                history.append(row)
                if log_print and step % 50 == 0:
                    print(json.dumps(row), flush=True)
            if (step % config.eval_every == 0 or step == config.steps) and held:
                metrics = held_metrics(backbone, reader, reader_config, names, held, device, config.eval_tabs, config.seed, qualified)
                if held_picks:
                    metrics.update(pick_metrics(backbone, reader, reader_config, names, held_picks, device, config.eval_tabs, config.seed, qualified))
                metrics.update(step=step, seconds=round(time.time() - started, 1))
                log.write(json.dumps(metrics) + "\n")
                log.flush()
                if log_print:
                    print(json.dumps(metrics), flush=True)
                # with the links untrained (anchoring alone) the last measure is the one kept
                reached = metrics["link_top1"] if config.link_weight > 0 else float(step)
                if reached > best:
                    best = reached
                    write_bundle(out / "best", backbone, reader, names, reader_config, link_weights_of(reader, qualified), signatures=signatures, record={**(record or {}), "metrics": metrics, "step": step, "seed": config.seed})
                    (out / "best.json").write_text(json.dumps({**metrics, **(record or {})}, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
            if step % config.eval_every == 0 and step < config.steps:
                # what a dead pod leaves: the lease brings out/ back, and the next one goes on from here
                torch.save({"backbone": backbone.state_dict(), "reader": reader.state_dict(), "optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(), "step": step, "cursor": cursor, "best": best, "rng": rng.getstate()}, out / "last_state.tmp")
                os.replace(out / "last_state.tmp", out / "last_state.pt")
            if config.stop_at is not None and step >= config.stop_at:
                return {"steps": step}
    if (out / "last_state.pt").exists():
        (out / "last_state.pt").unlink()
    if not held:
        write_bundle(out / "best", backbone, reader, names, reader_config, link_weights_of(reader, qualified), signatures=signatures, record={**(record or {}), "step": step, "seed": config.seed})
    return {"steps": step, "resumed_at": begin, "loss_first": round(float(np.mean(first or last or [0.0])), 4), "loss_last": round(float(np.mean(last or first or [0.0])), 4), "seconds": round(time.time() - started, 1), "train_tabs": len(kept), "held_tabs": len(held), "best_top1": best, "seconds_per_step": round((time.time() - started) / max(1, step - begin), 3), "build_seconds_per_step": round(spent["build"] / max(1, step), 3), "texts_per_step": round(spent["texts"] / max(1, step), 1), "links_per_step": round(spent["links"] / max(1, step), 1)}


def start_from(folder: str | Path, backbone, reader: NamedReader, names: NameSet, device: str) -> None:
    """Start from a bundle, the step before this one: its tensors, and its link weights back into the parameters they came from."""
    from engine.named.bundle import BUNDLE_META, BUNDLE_STATE
    document = json.loads((Path(folder) / BUNDLE_META).read_text(encoding="utf-8"))
    if document["names_sha"] != names.sha:
        raise ValueError("a start bundle of another name set")
    state = torch.load(Path(folder) / BUNDLE_STATE, map_location=device, weights_only=True)
    backbone.load_state_dict({name[len("backbone."):]: value for name, value in state.items() if name.startswith("backbone.")}, strict=False)
    reader.load_state_dict({name[len("reader."):]: value for name, value in state.items() if name.startswith("reader.")}, strict=False)
    weights = LinkWeights.from_record(document["weights"])
    raw = lambda values: torch.log(torch.expm1(torch.as_tensor(values, dtype=torch.float32, device=device).clamp(min=1e-4)))     # softplus undone
    with torch.no_grad():
        reader.raw_coordinate_weights.copy_(raw(weights.coordinates))
        reader.raw_rule_weights.copy_(raw([weights.rules[rule] for rule in RULES]))
        reader.raw_role_weight.copy_(raw(weights.role))
        reader.raw_closed_weight.copy_(raw(weights.closed))
        reader.raw_link_bias.copy_(raw([weights.bias(SLOTS_OF_LINK_KIND[kind][0]) for kind in LINK_KINDS]))


def read_descriptions(backbone, reader: NamedReader, names: NameSet) -> None:
    """Each basis property's output started at its own text as the backbone reads it, no gradient, dropout off (NamedReader.set_descriptions)."""
    text_of = dict(zip(meaning_names(), property_texts()))
    texts = [text_of[name] for name in names.names[names.meaning_start:]]
    was_training = backbone.training
    backbone.eval()
    with torch.no_grad():
        distinct, vectors = backbone.encode_texts(list(texts), max_tokens=DESCRIPTION_TOKENS, batch=64)
        at = {text: row for row, text in enumerate(distinct)}
        reader.set_descriptions(torch.stack([vectors[at[text]] for text in texts]))
    backbone.train(was_training)


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
    parser.add_argument("--keep", type=int, default=16, help="named coordinates an index keeps per target")
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
    parser.add_argument("--resume-from", help="a last_state.pt to go on from")
    parser.add_argument("--open-detached", action="store_true")
    parser.add_argument("--no-augment", action="store_true")
    parser.add_argument("--link-weight", type=float, default=LOSS_WEIGHTS["link"], help="0: anchoring alone")
    parser.add_argument("--open-weight", type=float, default=LOSS_WEIGHTS["open"])
    parser.add_argument("--anchor-steps", type=int, default=0, help="the property rows alone on the self-anchors first")
    parser.add_argument("--corpus", help="the schema.org corpus's semantic_instances.jsonl")
    parser.add_argument("--init", help="a bundle to start from")
    parser.add_argument("--picks", help="a folder of pick examples: the teacher's recorded picks on training-split questions")
    parser.add_argument("--qualified", help="a gate.json: only the properties it qualified enter a link's sum")
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
        names = load_name_set()
        backbone = load_backbone(args.backbone, layers=args.layers)
        backbone.freeze_table()
        signatures = ClassSignatures.from_ontology()
        reader_config = ReaderConfig(backbone=args.backbone, backbone_layers=args.layers, hidden=args.hidden, blocks=args.blocks, sample_rows=args.sample_rows, window_columns=args.window_columns, keep=args.keep)
    record["names_sha"] = names.sha
    config = TrainConfig(reader=reader_config, steps=args.steps, accumulate=args.accumulate, questions=args.questions, lr=args.lr, head_lr=args.head_lr, seed=args.seed, held_share=args.held_share, eval_every=args.eval_every, eval_tabs=args.eval_tabs, amp=args.amp, open_detached=args.open_detached, augment={} if args.no_augment else dict(TRAINING_SHARES), resume=args.resume_from, link_weight=args.link_weight, open_weight=args.open_weight, anchor_steps=args.anchor_steps, corpus=args.corpus, init=args.init, picks=args.picks, qualified=args.qualified,
                         co_held=str(Path(args.examples) / "co_held.json") if args.examples and (Path(args.examples) / "co_held.json").exists() else None)
    summary = train(examples, names, backbone, config, out, record=record, signatures=signatures)
    (out / "summary.json").write_text(json.dumps({**summary, **record}, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, sort_keys=True), flush=True)


__all__ = ["Batch", "TrainConfig", "build_batch", "held_metrics", "link_weights_of", "losses_of", "main", "run_batch", "train"]

if __name__ == "__main__":
    main()
