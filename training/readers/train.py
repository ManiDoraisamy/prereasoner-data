"""The two reader trainers. The models, their inputs and their outputs are engine.readers.models, the one
definition serving loads; this module holds only what training adds (targets, losses, the metric, the loop).

schema_items (stage 1): an encoder reads (question, one serialized table) and scores whether the table is used,
and for each of its columns whether it is used and in which roles (select, filter, group, order, having). Loss:
BCE for the table (pos weight 2.5) + BCE for each column's use (pos weight 4) + BCE for the roles of used columns.
Evaluates every epoch on 'calibration' and keeps the checkpoint with the best calibration column average precision;
writes calibration_predictions.jsonl.

readings (stage 2): one encoder pass over (question, pruned schema) reads every fact the builder needs. Heads:

    per column (and COUNT's star), per block b in {0, 1}: the per-block outputs of engine.readers.layout.LAYOUT
        (select aggregates, distinct inside the aggregate, the select slot of each aggregate, where operators, the
        where value is block 1, compared twice by one operator, group, having aggregate and operator, the having
        value is block 1, order aggregate, the condition makes the part of a share)
    per table, per block: used
    per question (first token), engine.readers.layout.GLOBAL: structure; per block distinct, OR between conditions,
        direction, limit (none / 1 / a number in the question), how many tables, shown items, conditions, groups,
        sort terms and HAVING terms the block holds, and whether the shown aggregate is a share
    value pointer: for each (block, column), start and end over the input tokens; for the limit, a start and end
        from the first token

Evaluates on 'calibration' from epoch --eval-from: the share of questions whose every reading is right, keeping the
checkpoint with the best share on the gold-pruned calibration rows (kind "gold"), and writes their outputs
(calibration_outputs.jsonl) for local decoding.

Both write <out>/model.pt, best.json, log.jsonl and tokenizer/ (the tokenizer and the encoder's config, from which
serving builds the encoder). Runs go under training/readers/data/experiments/<id>/, never into engine/data.

usage: python -m training.readers.train schema_items --examples stage1.jsonl --backbone <hf id or dir> --out <dir>
           [--epochs 3] [--batch 32] [--lr 3e-5] [--limit N] [--seed 0]
       python -m training.readers.train readings --examples stage2.jsonl --backbone <hf id or dir> --out <dir>
           [--epochs 10] [--batch 16] [--lr 3e-5] [--eval-from 1] [--checkpointing] [--limit N] [--seed 0]
"""
import argparse
import json
import math
import os
import random
import time

import numpy as np
import torch
from torch import nn
from transformers import AutoModel, AutoTokenizer

from engine.readers import numerals
from engine.readers.layout import (AGGREGATES, COLUMN_POS_WEIGHT, COUNTS, GLOBAL, GLOBAL_WIDTH, HAVING_OPERATORS,
                                   LAYOUT, OPERATORS, PER_BLOCK, SLOTS, STRUCTURES, TABLE_POS_WEIGHT)
from engine.readers.models import (ROLES, ReadingModel, SchemaItemModel, reading_encode, row_output,
                                   schema_item_inputs, slot_view, token_positions)


def _schedule(optimizer, epochs, rows, batch):
    """Linear warmup over the first 5% of steps, then linear decay to zero."""
    steps = epochs * math.ceil(rows / batch)
    warmup = max(1, steps // 20)
    return torch.optim.lr_scheduler.LambdaLR(
        optimizer, lambda s: min(1.0, (s + 1) / warmup) * max(0.0, (steps - s) / max(1, steps - warmup)))


def _save_tokenizer(model, tokenizer, out):
    tokenizer.save_pretrained(os.path.join(out, "tokenizer"))
    model.encoder.config.save_pretrained(os.path.join(out, "tokenizer"))


# ------------------------------------------------------------------------------------------- stage 1: schema items

def schema_item_batches(rows, tokenizer, max_len, size, shuffle, seed):
    """(rows, ids, mask, span index, span mask, column mask, column labels [B, C, 1 + roles], table labels)."""
    order = list(range(len(rows)))
    if shuffle:
        random.Random(seed).shuffle(order)
    for start in range(0, len(order), size):
        chunk = [rows[i] for i in order[start:start + size]]
        ids, mask, span_index, span_mask, column_mask = schema_item_inputs(tokenizer, chunk, max_len)
        column_labels = torch.zeros((len(chunk), column_mask.shape[1], 1 + len(ROLES)))
        table_labels = torch.tensor([float(r["table_used"]) for r in chunk])
        for b, row in enumerate(chunk):
            for c, column in enumerate(row["columns"]):
                if not column_mask[b, c]:
                    continue                                   # truncated away
                column_labels[b, c, 0] = column["used"]
                column_labels[b, c, 1:] = torch.tensor(column["roles"], dtype=torch.float)
        yield chunk, ids, mask, span_index, span_mask, column_mask, column_labels, table_labels


def average_precision(scores, labels):
    order = np.argsort(-np.asarray(scores), kind="stable")
    labels = np.asarray(labels)[order]
    if labels.sum() == 0:
        return float("nan")
    hits = np.cumsum(labels)
    return float((hits[labels == 1] / (np.nonzero(labels)[0] + 1)).mean())


def evaluate_schema_items(model, rows, tokenizer, args, device):
    model.eval()
    predictions = []
    with torch.no_grad():
        for chunk, ids, mask, si, sm, cm, _, _ in schema_item_batches(rows, tokenizer, args.max_len, args.eval_batch,
                                                                      False, 0):
            t_logit, c_logits = model(ids.to(device), mask.to(device), si.to(device), sm.to(device))
            t_prob = torch.sigmoid(t_logit).float().cpu().numpy()
            c_prob = torch.sigmoid(c_logits).float().cpu().numpy()
            for b, row in enumerate(chunk):
                predictions.append({"db_id": row["db_id"], "source": row["source"], "index": row["index"],
                                    "table": row["table"], "table_used": row["table_used"],
                                    "table_p": float(t_prob[b]),
                                    "columns": [{"name": col["name"], "used": col["used"], "roles": col["roles"],
                                                 "p": [float(x) for x in c_prob[b, c]] if cm[b, c] else None}
                                                for c, col in enumerate(row["columns"])]})
    model.train()
    t_scores = [p["table_p"] for p in predictions]
    t_labels = [p["table_used"] for p in predictions]
    c_scores = [c["p"][0] for p in predictions for c in p["columns"] if c["p"] is not None]
    c_labels = [c["used"] for p in predictions for c in p["columns"] if c["p"] is not None]
    # Question level: are all gold tables (columns) among those scored above 0.5?
    by_question = {}
    for p in predictions:
        by_question.setdefault((p["source"], p["index"]), []).append(p)
    table_all = column_all = 0
    for items in by_question.values():
        table_all += all(p["table_p"] >= 0.5 for p in items if p["table_used"])
        column_all += all(c["p"] is not None and c["p"][0] >= 0.5 for p in items for c in p["columns"] if c["used"])
    n = max(1, len(by_question))
    return predictions, {"table_ap": average_precision(t_scores, t_labels),
                         "column_ap": average_precision(c_scores, c_labels), "questions": len(by_question),
                         "all_tables_at_0.5": table_all / n, "all_columns_at_0.5": column_all / n}


def train_schema_items(args):
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rows = [json.loads(line) for line in open(args.examples, encoding="utf-8")]
    train = [r for r in rows if r["split"] == "train"]
    calibration = [r for r in rows if r["split"] == "calibration"]
    if args.limit:
        train, calibration = train[:args.limit], calibration[:max(50, args.limit // 4)]
    tokenizer = AutoTokenizer.from_pretrained(args.backbone)
    model = SchemaItemModel(AutoModel.from_pretrained(args.backbone)).to(device)
    head_params = list(model.table_head.parameters()) + list(model.column_head.parameters())
    optimizer = torch.optim.AdamW([{"params": model.encoder.parameters(), "lr": args.lr},
                                   {"params": head_params, "lr": args.head_lr}], weight_decay=0.01)
    scheduler = _schedule(optimizer, args.epochs, len(train), args.batch)
    table_loss = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(2.5, device=device))
    os.makedirs(args.out, exist_ok=True)
    log = open(os.path.join(args.out, "log.jsonl"), "a", encoding="utf-8")
    best, step, started = -1.0, 0, time.time()
    use_amp = device == "cuda"
    for epoch in range(args.epochs):
        for _, ids, mask, si, sm, cm, cl, tl in schema_item_batches(train, tokenizer, args.max_len, args.batch, True,
                                                                    args.seed + epoch):
            ids, mask, si, sm, cm, cl, tl = (x.to(device) for x in (ids, mask, si, sm, cm, cl, tl))
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=use_amp):
                t_logit, c_logits = model(ids, mask, si, sm)
            t_logit, c_logits = t_logit.float(), c_logits.float()
            loss = table_loss(t_logit, tl)
            if cm.any():
                used_logit, used_label = c_logits[..., 0][cm], cl[..., 0][cm]
                loss = loss + nn.functional.binary_cross_entropy_with_logits(
                    used_logit, used_label, pos_weight=torch.tensor(4.0, device=device))
                positive = cm & (cl[..., 0] > 0)
                if positive.any():
                    loss = loss + nn.functional.binary_cross_entropy_with_logits(c_logits[..., 1:][positive],
                                                                                 cl[..., 1:][positive])
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step(); scheduler.step(); optimizer.zero_grad()
            step += 1
            if step % 100 == 0:
                log.write(json.dumps({"step": step, "loss": round(loss.item(), 4),
                                      "seconds": round(time.time() - started)}) + "\n"); log.flush()
        predictions, metrics = evaluate_schema_items(model, calibration, tokenizer, args, device)
        metrics.update(epoch=epoch + 1, step=step, seconds=round(time.time() - started))
        log.write(json.dumps(metrics) + "\n"); log.flush()
        print(json.dumps(metrics), flush=True)
        if metrics["column_ap"] > best:
            best = metrics["column_ap"]
            torch.save(model.state_dict(), os.path.join(args.out, "model.pt"))
            with open(os.path.join(args.out, "calibration_predictions.jsonl"), "w", encoding="utf-8") as sink:
                for p in predictions:
                    sink.write(json.dumps(p) + "\n")
            json.dump({**metrics, "backbone": args.backbone, "seed": args.seed, "lr": args.lr, "epochs": args.epochs,
                       "max_len": args.max_len, "batch": args.batch},
                      open(os.path.join(args.out, "best.json"), "w"), indent=1)
    _save_tokenizer(model, tokenizer, args.out)


# ----------------------------------------------------------------------------------------------- stage 2: readings

def encode(tokenizer, row, max_len):
    """engine.readers.models.reading_encode's (ids, offsets, sequence ids, column spans, table spans), plus each
    labelled value's token positions and the limit number's."""
    ids, offsets, sequence, columns, tables = reading_encode(tokenizer, row, max_len)
    values = [token_positions(offsets, sequence, 0 if v["in"] == "question" else 1, v["start"], v["end"])
              for v in row["values"]]
    limit = None
    for g in row["globals"][:1]:
        if g["limit"] not in (None, 1):
            for number, start, end in numerals.mentions(row["question"]):
                if number == g["limit"]:
                    limit = token_positions(offsets, sequence, 0, start, end)
                    break
    return ids, offsets, sequence, columns, tables, values, limit


def column_targets(row):
    """[C, 2, PER_BLOCK] targets and [C, 2, 6] slot targets (-1: not selected) for star + columns."""
    names = ["*"] + [c["name"] for c in row["columns"]]
    target = np.zeros((len(names), 2, PER_BLOCK), dtype=np.float32)
    slot = -np.ones((len(names), 2, len(AGGREGATES)), dtype=np.int64)
    for c, name in enumerate(names):
        label = row["labels"].get(name)
        if not label:
            continue
        for b in range(2):
            item = label["block"][b]
            for key, vocabulary in (("select", AGGREGATES), ("where", OPERATORS), ("having", AGGREGATES),
                                    ("having_op", HAVING_OPERATORS), ("order", AGGREGATES)):
                offset, _ = LAYOUT[key]
                for value in item.get(key, []):
                    target[c, b, offset + vocabulary.index(value)] = 1
            for key in ("distinct_aggregate", "group", "where_sub", "where_twice", "having_sub", "share_part"):
                if item.get(key):
                    target[c, b, LAYOUT[key][0]] = 1
            for aggregate, position in item.get("slots", {}).items():
                slot[c, b, AGGREGATES.index(aggregate)] = min(position, SLOTS - 1)
    return target, slot


def reading_batches(rows, tokenizer, max_len, size, shuffle, seed):
    """(rows, their encodings, the model's input tensors and every target)."""
    order = list(range(len(rows)))
    if shuffle:
        random.Random(seed).shuffle(order)
    for begin in range(0, len(order), size):
        chunk = [rows[i] for i in order[begin:begin + size]]
        encoded = [encode(tokenizer, r, max_len) for r in chunk]
        B = len(chunk)
        L = max(len(e[0]) for e in encoded)
        C = max(len(e[3]) for e in encoded)
        Tb = max(1, max(len(e[4]) for e in encoded))
        T = max(1, max((len(p) for e in encoded for p in e[3] + e[4]), default=1))
        ids = torch.full((B, L), tokenizer.pad_token_id, dtype=torch.long)
        mask = torch.zeros((B, L), dtype=torch.long)
        column_index = torch.zeros((B, C, T), dtype=torch.long)
        column_mask = torch.zeros((B, C, T), dtype=torch.bool)
        column_valid = torch.zeros((B, C), dtype=torch.bool)
        table_index = torch.zeros((B, Tb, T), dtype=torch.long)
        table_mask = torch.zeros((B, Tb, T), dtype=torch.bool)
        table_valid = torch.zeros((B, Tb), dtype=torch.bool)
        column_target = torch.zeros((B, C, 2, PER_BLOCK))
        slot_target = -torch.ones((B, C, 2, len(AGGREGATES)), dtype=torch.long)
        table_target = torch.zeros((B, Tb, 2))
        global_target = torch.zeros((B, GLOBAL_WIDTH))
        structure_target = torch.zeros(B, dtype=torch.long)
        dir_target = torch.zeros((B, 2), dtype=torch.long)
        limit_target = torch.zeros((B, 2), dtype=torch.long)
        count_target = torch.zeros((B, 2, len(COUNTS)), dtype=torch.long)
        value_start = torch.zeros((B, C, 2, L))
        value_end = torch.zeros((B, C, 2, L))
        value_valid = torch.zeros((B, C, 2), dtype=torch.bool)
        limit_start = torch.zeros((B, L))
        limit_end = torch.zeros((B, L))
        limit_valid = torch.zeros(B, dtype=torch.bool)
        blocks_used = torch.zeros((B, 2), dtype=torch.bool)
        for b, ((tokens, _, _, columns, tables, values, limit), row) in enumerate(zip(encoded, chunk)):
            ids[b, :len(tokens)] = torch.tensor(tokens)
            mask[b, :len(tokens)] = 1
            names = ["*"] + [c["name"] for c in row["columns"]]
            index_of = {name: i for i, name in enumerate(names)}
            ct, st = column_targets(row)
            for c, positions in enumerate(columns):
                if not positions:
                    continue
                column_index[b, c, :len(positions)] = torch.tensor(positions)
                column_mask[b, c, :len(positions)] = True
                column_valid[b, c] = True
                column_target[b, c] = torch.from_numpy(ct[c])
                slot_target[b, c] = torch.from_numpy(st[c])
            for t, positions in enumerate(tables):
                if not positions:
                    continue
                table_index[b, t, :len(positions)] = torch.tensor(positions)
                table_mask[b, t, :len(positions)] = True
                table_valid[b, t] = True
                table_target[b, t] = torch.tensor(row["tables"][t]["used"], dtype=torch.float)
            structure_target[b] = STRUCTURES.index(row["structure"])
            blocks_used[b, 0] = True
            blocks_used[b, 1] = len(row["globals"]) > 1
            for k, g in enumerate(row["globals"]):
                global_target[b, GLOBAL[f"distinct{k}"][0]] = g["distinct"]
                global_target[b, GLOBAL[f"conj{k}"][0]] = float(g["conj"] == "OR")
                global_target[b, GLOBAL[f"share{k}"][0]] = float(g.get("share", 0))
                dir_target[b, k] = {None: 0, "ASC": 1, "DESC": 2}[g["dir"]]
                limit_target[b, k] = 0 if g["limit"] is None else (1 if g["limit"] == 1 else 2)
                for j, (kind, top) in enumerate(COUNTS.items()):
                    count_target[b, k, j] = min(g["counts"][kind], top)
            for v, positions in zip(row["values"], values):
                c = index_of.get(v["column"])
                if c is None or not positions or not column_valid[b, c]:
                    continue
                value_start[b, c, v["block"], positions[0]] = 1
                value_end[b, c, v["block"], positions[-1]] = 1
                value_valid[b, c, v["block"]] = True
            if limit:
                limit_start[b, limit[0]] = 1
                limit_end[b, limit[-1]] = 1
                limit_valid[b] = True
        yield chunk, encoded, dict(
            ids=ids, mask=mask, column_index=column_index, column_mask=column_mask, column_valid=column_valid,
            table_index=table_index, table_mask=table_mask, table_valid=table_valid, column_target=column_target,
            slot_target=slot_target, table_target=table_target, global_target=global_target,
            structure_target=structure_target, dir_target=dir_target, limit_target=limit_target,
            count_target=count_target,
            value_start=value_start, value_end=value_end, value_valid=value_valid, limit_start=limit_start,
            limit_end=limit_end, limit_valid=limit_valid, blocks_used=blocks_used)


def binary_mask(device):
    """The per-block column outputs that are independent yes/no readings (all but the slots)."""
    binary = torch.ones(PER_BLOCK, dtype=torch.bool, device=device)
    offset, width = LAYOUT["slot"]
    binary[offset:offset + width] = False
    return binary


def multi_target_ce(logits, target, valid):
    """-log of the probability mass on the target positions, for each valid row."""
    if not valid.any():
        return logits.new_zeros(())
    logits, target = logits[valid], target[valid]
    return (torch.logsumexp(logits, -1) - torch.logsumexp(logits.masked_fill(target == 0, -1e4), -1)).mean()


def losses(outputs, batch):
    column_logits, table_logits, global_logits, start, end, limit_start, limit_end = outputs
    cv = batch["column_valid"]
    used = batch["blocks_used"]                                                   # [B, 2]
    block_valid = cv[:, :, None] & used[:, None, :]                              # [B, C, 2]
    binary = binary_mask(cv.device)
    pos_weight = torch.full((PER_BLOCK,), COLUMN_POS_WEIGHT, device=cv.device)
    bce = nn.functional.binary_cross_entropy_with_logits(
        column_logits, batch["column_target"], pos_weight=pos_weight, reduction="none")
    loss = bce[..., binary][block_valid].mean()
    slot_logits = slot_view(column_logits)                                        # [B, C, 2, 6, SLOTS]
    slot_valid = block_valid[..., None] & (batch["slot_target"] >= 0)
    if slot_valid.any():
        loss = loss + nn.functional.cross_entropy(slot_logits[slot_valid], batch["slot_target"][slot_valid])
    tv = batch["table_valid"][:, :, None] & used[:, None, :]
    loss = loss + nn.functional.binary_cross_entropy_with_logits(
        table_logits[tv], batch["table_target"][tv], pos_weight=torch.tensor(TABLE_POS_WEIGHT, device=cv.device))
    loss = loss + nn.functional.cross_entropy(global_logits[:, :5], batch["structure_target"])
    for k in range(2):
        rows = used[:, k]
        if not rows.any():
            continue
        for name in ("distinct", "conj", "share"):
            offset = GLOBAL[f"{name}{k}"][0]
            loss = loss + nn.functional.binary_cross_entropy_with_logits(
                global_logits[rows, offset], batch["global_target"][rows, offset])
        offset = GLOBAL[f"dir{k}"][0]
        loss = loss + nn.functional.cross_entropy(global_logits[rows, offset:offset + 3], batch["dir_target"][rows, k])
        offset = GLOBAL[f"limit{k}"][0]
        loss = loss + nn.functional.cross_entropy(global_logits[rows, offset:offset + 3],
                                                  batch["limit_target"][rows, k])
        for j, (kind, top) in enumerate(COUNTS.items()):
            offset = GLOBAL[f"count_{kind}{k}"][0]
            loss = loss + nn.functional.cross_entropy(global_logits[rows, offset:offset + top + 1],
                                                      batch["count_target"][rows, k, j])
    vv = batch["value_valid"]
    loss = loss + multi_target_ce(start, batch["value_start"], vv) + multi_target_ce(end, batch["value_end"], vv)
    lv = batch["limit_valid"]
    loss = loss + multi_target_ce(limit_start, batch["limit_start"], lv) + multi_target_ce(limit_end,
                                                                                           batch["limit_end"], lv)
    return loss


def evaluate_readings(model, rows, tokenizer, args, device, keep_outputs):
    """(share of questions whose every reading is right, by kind; outputs for local decoding)."""
    model.eval()
    right, total = {}, {}
    outputs = []
    with torch.no_grad():
        for chunk, encoded, batch in reading_batches(rows, tokenizer, args.max_len, args.eval_batch, False, 0):
            tensors = {k: v.to(device) for k, v in batch.items()}
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=device == "cuda"):
                out = model(tensors["ids"], tensors["mask"], tensors["column_index"], tensors["column_mask"],
                            tensors["table_index"], tensors["table_mask"])
            column_logits, table_logits, global_logits, start, end, limit_start, limit_end = (o.float() for o in out)
            column_p = torch.sigmoid(column_logits)
            table_p = torch.sigmoid(table_logits)
            for b, row in enumerate(chunk):
                cv, used = tensors["column_valid"][b], tensors["blocks_used"][b]
                ok = int(global_logits[b, :5].argmax()) == int(tensors["structure_target"][b])
                binary = binary_mask(device)
                slots = slot_view(column_logits[b])                                 # [C, 2, 6, SLOTS]
                for k in range(2):
                    if not used[k]:
                        continue
                    predicted = (column_p[b, :, k] >= 0.5)[cv][:, binary]
                    ok &= bool((predicted == (tensors["column_target"][b, :, k] > 0.5)[cv][:, binary]).all())
                    st = tensors["slot_target"][b, :, k]
                    sv = cv[:, None] & (st >= 0)
                    if sv.any():
                        ok &= bool((slots[:, k].argmax(-1)[sv] == st[sv]).all())
                    tv = tensors["table_valid"][b]
                    ok &= bool(((table_p[b, :, k] >= 0.5) == (tensors["table_target"][b, :, k] > 0.5))[tv].all())
                    for name in ("distinct", "conj"):
                        g = GLOBAL[f"{name}{k}"][0]
                        ok &= bool((global_logits[b, g] >= 0) == (tensors["global_target"][b, g] > 0.5))
                    g = GLOBAL[f"dir{k}"][0]
                    ok &= int(global_logits[b, g:g + 3].argmax()) == int(tensors["dir_target"][b, k])
                    g = GLOBAL[f"limit{k}"][0]
                    ok &= int(global_logits[b, g:g + 3].argmax()) == int(tensors["limit_target"][b, k])
                    for j, (kind, top) in enumerate(COUNTS.items()):
                        g = GLOBAL[f"count_{kind}{k}"][0]
                        ok &= int(global_logits[b, g:g + top + 1].argmax()) == int(tensors["count_target"][b, k, j])
                    for c in torch.nonzero(tensors["value_valid"][b, :, k]).flatten().tolist():
                        ok &= bool(tensors["value_start"][b, c, k, start[b, c, k].argmax()] > 0)
                kind = row["kind"]
                right[kind] = right.get(kind, 0) + int(ok)
                total[kind] = total.get(kind, 0) + 1
                if keep_outputs:
                    output = row_output(row, encoded[b], column_p[b], slots, table_p[b], global_logits[b], start[b],
                                        end[b], limit_start[b], limit_end[b], cv, tensors["table_valid"][b])
                    outputs.append({"kind": kind, "source": row["source"], "index": row["index"],
                                    "db_id": row["db_id"], **output})
    model.train()
    return {kind: right[kind] / total[kind] for kind in total}, outputs


def train_readings(args):
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    rows = [json.loads(line) for line in open(args.examples, encoding="utf-8")]
    train = [r for r in rows if r["split"] == "train"]
    calibration = [r for r in rows if r["split"] == "calibration"]
    if args.limit:
        train, calibration = train[:args.limit], calibration[:max(40, args.limit // 4)]
    tokenizer = AutoTokenizer.from_pretrained(args.backbone)
    model = ReadingModel(AutoModel.from_pretrained(args.backbone)).to(device)
    if args.checkpointing:
        model.encoder.gradient_checkpointing_enable()
    head_params = [p for n, p in model.named_parameters() if not n.startswith("encoder.")]
    optimizer = torch.optim.AdamW([{"params": model.encoder.parameters(), "lr": args.lr},
                                   {"params": head_params, "lr": args.head_lr}], weight_decay=0.01)
    scheduler = _schedule(optimizer, args.epochs, len(train), args.batch)
    os.makedirs(args.out, exist_ok=True)
    log = open(os.path.join(args.out, "log.jsonl"), "a", encoding="utf-8")
    best, step, started = -1.0, 0, time.time()
    for epoch in range(args.epochs):
        for _, _, batch in reading_batches(train, tokenizer, args.max_len, args.batch, True, args.seed + epoch):
            tensors = {k: v.to(device) for k, v in batch.items()}
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=device == "cuda"):
                out = model(tensors["ids"], tensors["mask"], tensors["column_index"], tensors["column_mask"],
                            tensors["table_index"], tensors["table_mask"])
            loss = losses([o.float() for o in out], tensors)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step(); scheduler.step(); optimizer.zero_grad()
            step += 1
            if step % 100 == 0:
                log.write(json.dumps({"step": step, "loss": round(loss.item(), 4),
                                      "seconds": round(time.time() - started)}) + "\n"); log.flush()
        if epoch + 1 < args.eval_from and epoch + 1 < args.epochs:
            continue
        metrics, outputs = evaluate_readings(model, calibration, tokenizer, args, device, keep_outputs=True)
        metrics = {"all_readings_right": metrics, "epoch": epoch + 1, "step": step,
                   "seconds": round(time.time() - started)}
        log.write(json.dumps(metrics) + "\n"); log.flush()
        print(json.dumps(metrics), flush=True)
        score = metrics["all_readings_right"].get("gold", 0.0)
        if score > best:
            best = score
            torch.save(model.state_dict(), os.path.join(args.out, "model.pt"))
            with open(os.path.join(args.out, "calibration_outputs.jsonl"), "w", encoding="utf-8") as sink:
                for o in outputs:
                    sink.write(json.dumps(o) + "\n")
            json.dump({**metrics, "backbone": args.backbone, "seed": args.seed, "lr": args.lr,
                       "epochs": args.epochs, "batch": args.batch}, open(os.path.join(args.out, "best.json"), "w"),
                      indent=1)
    _save_tokenizer(model, tokenizer, args.out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train a reader model (stage 1 schema items or stage 2 readings).")
    stages = ap.add_subparsers(dest="stage", required=True)
    for name, epochs, batch, eval_batch in (("schema_items", 3, 32, 64), ("readings", 10, 16, 32)):
        stage = stages.add_parser(name)
        stage.add_argument("--examples", required=True)
        stage.add_argument("--backbone", required=True)
        stage.add_argument("--out", required=True)
        stage.add_argument("--epochs", type=int, default=epochs)
        stage.add_argument("--batch", type=int, default=batch)
        stage.add_argument("--eval-batch", type=int, default=eval_batch)
        stage.add_argument("--lr", type=float, default=3e-5)
        stage.add_argument("--head-lr", type=float, default=1e-3)
        stage.add_argument("--max-len", type=int, default=512)
        stage.add_argument("--seed", type=int, default=0)
        stage.add_argument("--limit", type=int, default=0)
        if name == "readings":
            stage.add_argument("--eval-from", type=int, default=1,
                               help="first epoch evaluated (and so eligible as the checkpoint)")
            stage.add_argument("--checkpointing", action="store_true",
                               help="recompute activations: a large encoder fits 16 GB")
    args = ap.parse_args(argv)
    {"schema_items": train_schema_items, "readings": train_readings}[args.stage](args)


if __name__ == "__main__":
    main()
