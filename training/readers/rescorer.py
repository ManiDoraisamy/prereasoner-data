"""Re-scorer trainer: an encoder reads a question beside one candidate's read-as line and result shape, and scores
how likely the candidate answers it. The model and what it reads are engine.readers.models (Rescorer,
rescorer_inputs), the one definition serving loads: the text "<question> </s> <read-as line> | <rows> rows of
<columns> columns", and beside the first token's state the decoder's own view (the candidate's log probability,
its rank, and whether its result is empty).

All of a question's servable candidates (those that ran, at most 32) are scored together. Training is listwise (the
right candidate against the others, on questions that have one) plus a yes/no term, so the score also says how sure
the choice is, for refusal. Checkpoints are selected on --calibration candidates by the share of questions whose
top-scored candidate is right. Writes <out>/model.pt, best.json, log.jsonl and tokenizer/ (with the encoder's
config).

usage: python -m training.readers.rescorer train --candidates a.jsonl [b.jsonl ...] --backbone <id or dir>
           --out <dir> [--calibration cal.jsonl] [--epochs 3] [--questions 8] [--lr 2e-5] [--seed 0]
           [--checkpointing]
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

from engine.readers.models import Rescorer, rescorer_inputs

MAX_CANDIDATES = 32                                # the servable candidates scored per question, at most


def servable(candidate):
    """A candidate that ran: only those can be served."""
    return candidate["status"] == "ran"


def score(model, tokenizer, question, candidates, device="cpu"):
    """Scores (logits) for the servable candidates, in their order."""
    if not candidates:
        return []
    ids, mask, features = rescorer_inputs(tokenizer, question, candidates)
    with torch.no_grad():
        return model(ids.to(device), mask.to(device), features.to(device)).float().cpu().tolist()


def load_groups(paths):
    groups = []
    for path in paths:
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            candidates = [c for c in record["candidates"] if servable(c)][:MAX_CANDIDATES]
            if candidates:
                groups.append((record, candidates))
    return groups


def evaluate(model, tokenizer, groups, device):
    """(share of questions whose top-scored candidate is right, share whose first candidate is right)."""
    model.eval()
    chosen = first = 0
    for record, candidates in groups:
        scores = score(model, tokenizer, record["question"], candidates, device)
        best = max(range(len(scores)), key=lambda i: (scores[i], -i))
        chosen += bool(candidates[best].get("right"))
        first += bool(candidates[0].get("right"))
    model.train()
    return chosen / max(1, len(groups)), first / max(1, len(groups))


def train(args):
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    groups = load_groups(args.candidates)
    held = load_groups([args.calibration]) if args.calibration else []
    tokenizer = AutoTokenizer.from_pretrained(args.backbone)
    model = Rescorer(AutoModel.from_pretrained(args.backbone)).to(device)
    if args.checkpointing:
        model.encoder.gradient_checkpointing_enable()
    optimizer = torch.optim.AdamW([{"params": model.encoder.parameters(), "lr": args.lr},
                                   {"params": model.head.parameters(), "lr": 1e-3}], weight_decay=0.01)
    steps = args.epochs * math.ceil(len(groups) / args.questions)
    warmup = max(1, steps // 20)
    scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lambda s: min(1.0, (s + 1) / warmup) * max(0.0, (steps - s) / max(1, steps - warmup)))
    os.makedirs(args.out, exist_ok=True)
    log = open(os.path.join(args.out, "log.jsonl"), "a", encoding="utf-8")
    best, step, started = -1.0, 0, time.time()
    for epoch in range(args.epochs):
        order = list(range(len(groups)))
        random.Random(args.seed + epoch).shuffle(order)
        for begin in range(0, len(order), args.questions):
            loss = 0.0
            for i in order[begin:begin + args.questions]:
                record, candidates = groups[i]
                ids, mask, features = rescorer_inputs(tokenizer, record["question"], candidates)
                labels = torch.tensor([float(bool(c.get("right"))) for c in candidates], device=device)
                with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=device == "cuda"):
                    logits = model(ids.to(device), mask.to(device), features.to(device))
                logits = logits.float()
                term = nn.functional.binary_cross_entropy_with_logits(logits, labels)
                if labels.sum() > 0:
                    term = term + (torch.logsumexp(logits, 0)
                                   - torch.logsumexp(logits.masked_fill(labels == 0, -1e4), 0))
                loss = loss + term / args.questions
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step(); scheduler.step(); optimizer.zero_grad()
            step += 1
            if step % 100 == 0:
                log.write(json.dumps({"step": step, "loss": round(float(loss), 4),
                                      "seconds": round(time.time() - started)}) + "\n"); log.flush()
        metrics = {"epoch": epoch + 1, "step": step, "seconds": round(time.time() - started)}
        if held:
            chosen, first = evaluate(model, tokenizer, held, device)
            metrics.update(calibration_chosen_right=chosen, calibration_first_right=first,
                           calibration_questions=len(held))
        log.write(json.dumps(metrics) + "\n"); log.flush()
        print(json.dumps(metrics), flush=True)
        value = metrics.get("calibration_chosen_right", epoch)
        if value > best:
            best = value
            torch.save(model.state_dict(), os.path.join(args.out, "model.pt"))
            json.dump({**metrics, "backbone": args.backbone, "seed": args.seed, "lr": args.lr,
                       "epochs": args.epochs}, open(os.path.join(args.out, "best.json"), "w"), indent=1)
    tokenizer.save_pretrained(os.path.join(args.out, "tokenizer"))
    model.encoder.config.save_pretrained(os.path.join(args.out, "tokenizer"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train the re-scorer on candidate files.")
    ap.add_argument("command", choices=["train"])
    ap.add_argument("--candidates", nargs="+", required=True)
    ap.add_argument("--calibration")
    ap.add_argument("--backbone", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--questions", type=int, default=8, help="questions per step")
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--checkpointing", action="store_true", help="recompute activations: a large encoder fits 24 GB")
    train(ap.parse_args(argv))


if __name__ == "__main__":
    main()
