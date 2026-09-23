"""SFT the proposer: (schema + question) -> execution-verified typed-AST SQL.

Deterministic manual loop (seed 7, seeded shuffle, no sampling anywhere): LoRA on the
pinned Qwen2.5-0.5B base, prompt tokens masked out of the loss, validation split by db_id
(`training.proposer.is_validation_db`) so validation databases are never trained on. The
prompt is the serving prompt (engine/sql_prompt.py). Ends with a greedy exact-match decode on
a validation sample. Artifacts go ONLY to the experiment directory; installing an adapter in
the runtime bundle is training/rank/promote.py's job.

    python -m training.proposer.train_sft \
        --targets training/proposer/data/experiments/<id>/targets.jsonl \
        --out-dir training/proposer/data/experiments/<id>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.model_revisions import QWEN_MODEL_ID, QWEN_REVISION
from engine.sql_prompt import schema_prompt
from training.proposer import is_validation_db

SEED = 7


def db_tables(meta: dict) -> list[dict]:
    tables = [{"name": name, "columns": []} for name in meta["table_names_original"]]
    for table_index, column in meta["column_names_original"]:
        if table_index >= 0:
            tables[table_index]["columns"].append(column)
    return tables


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--base", default=QWEN_MODEL_ID)
    ap.add_argument("--base-revision", default=QWEN_REVISION)
    ap.add_argument("--tables", default=os.path.join(ROOT, "spider", "data", "tables.json"))
    ap.add_argument("--max-steps", type=int, default=1200)
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--seq-len", type=int, default=384)
    ap.add_argument("--val-decode", type=int, default=50)
    args = ap.parse_args()

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.manual_seed(SEED)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    print(f"device: {device} ({dtype})")
    meta = {table["db_id"]: table for table in json.load(open(args.tables, encoding="utf-8"))}
    rows = [json.loads(line) for line in open(args.targets, encoding="utf-8")]
    rows = [row for row in rows if "idx" in row]
    train_rows = [row for row in rows if not is_validation_db(row["db_id"])]
    val_rows = [row for row in rows if is_validation_db(row["db_id"])]
    print(f"targets: {len(train_rows)} train / {len(val_rows)} val "
          f"({len({r['db_id'] for r in val_rows})} val dbs)")

    tokenizer = AutoTokenizer.from_pretrained(args.base, revision=args.base_revision)
    model = AutoModelForCausalLM.from_pretrained(
        args.base, revision=args.base_revision, dtype=dtype).to(device)
    model = get_peft_model(model, LoraConfig(
        r=16, lora_alpha=32, lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    ))
    model.train()

    def encode(row):
        prompt = schema_prompt(db_tables(meta[row["db_id"]]), row["question"])
        prompt_ids = tokenizer(prompt, add_special_tokens=False).input_ids
        target_ids = tokenizer(row["sql"] + tokenizer.eos_token,
                               add_special_tokens=False).input_ids
        ids = (prompt_ids + target_ids)[:args.seq_len]
        labels = ([-100] * len(prompt_ids) + list(target_ids))[:args.seq_len]
        return ids, labels

    def batch_tensors(batch_rows):
        encoded = [encode(row) for row in batch_rows]
        width = max(len(ids) for ids, _ in encoded)
        pad = tokenizer.pad_token_id or tokenizer.eos_token_id
        input_ids = torch.tensor([ids + [pad] * (width - len(ids)) for ids, _ in encoded])
        labels = torch.tensor([lab + [-100] * (width - len(lab)) for _, lab in encoded])
        attention = (input_ids != pad).long()
        return input_ids, attention, labels

    optimizer = torch.optim.AdamW(
        [parameter for parameter in model.parameters() if parameter.requires_grad], lr=args.lr)
    generator = torch.Generator().manual_seed(SEED)
    step = accumulated = 0
    started = time.perf_counter()
    while step < args.max_steps:
        # Shuffle example indexes, then batch successive chunks of the permutation: every
        # example appears exactly once per epoch. (The previous contiguous-slice-at-shuffled-
        # start sampling over-weighted interior examples and coupled weighting to batch size;
        # adapters d1/d2/d4 were trained under it and stay frozen as recorded baselines.)
        permutation = torch.randperm(len(train_rows), generator=generator).tolist()
        for start in range(0, len(permutation), args.batch):
            chunk = [train_rows[index] for index in permutation[start:start + args.batch]]
            input_ids, attention, labels = batch_tensors(chunk)
            loss = model(input_ids=input_ids.to(device), attention_mask=attention.to(device),
                         labels=labels.to(device)).loss
            (loss / args.accum).backward()
            accumulated += 1
            if accumulated % args.accum == 0:
                optimizer.step()
                optimizer.zero_grad()
                step += 1
                if step % 25 == 0:
                    rate = (time.perf_counter() - started) / step
                    print(f"step {step}/{args.max_steps}  loss {loss.item():.3f}  "
                          f"{rate:.1f}s/step", flush=True)
                if step >= args.max_steps:
                    break

    model.eval()
    exact = 0
    sample = val_rows[:args.val_decode]
    with torch.no_grad():
        for row in sample:
            prompt = schema_prompt(db_tables(meta[row["db_id"]]), row["question"])
            inputs = tokenizer(prompt, return_tensors="pt").to(device)
            output = model.generate(**inputs, max_new_tokens=80, do_sample=False,
                                    pad_token_id=tokenizer.eos_token_id)
            text = tokenizer.decode(output[0][inputs.input_ids.shape[1]:],
                                    skip_special_tokens=True).strip()
            exact += text.splitlines()[0].strip() == row["sql"] if text else False
    metrics = {
        "seed": SEED, "base": args.base, "base_revision": args.base_revision,
        "max_steps": args.max_steps,
        "lr": args.lr, "batch": args.batch, "accum": args.accum,
        "train_targets": len(train_rows), "val_targets": len(val_rows),
        "val_decode_sample": len(sample), "val_exact_match": exact,
    }
    os.makedirs(args.out_dir, exist_ok=True)
    model.save_pretrained(args.out_dir)
    with open(os.path.join(args.out_dir, "metrics.json"), "w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2)
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
