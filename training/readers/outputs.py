"""Training runs read the way serving reads: a run's model built as serving builds one, serving's stage 1 and
pruning over a run's stage-1 model, and the stage-2 outputs of a run's reading model for a file of rows.

- ``load_run``: the model a trainer here saved (model.pt, tokenizer/ with the encoder's config), on CPU in float32,
  as the recorded runs were read. Serving (engine.readers.models.load) computes in float32 too, over the promoted
  weights, which stay in bfloat16 and are widened as they are used.
- ``Stage1``: engine.readers.search.ReadingSearch's own schema_items, prune and row over a run's stage-1 model, so
  the pruned rows (training.readers.pruned) are built by the code serving runs.
- ``serving_row``: that row with the empty labels the trainer's batches read.
- ``main``: the stage-2 outputs for rows that training.readers.pruned or training.readers.examples wrote, on CPU,
  the same outputs a GPU run writes, sharded so several processes share a file. Decoding, candidates and the
  re-scorer then iterate on these without running the model again.

usage: python -m training.readers.outputs <stage2 run dir> <rows.jsonl> <outputs.jsonl> [--shard i/n] [--threads 4]
"""
import argparse
import json
from pathlib import Path

import torch

from engine.readers import models
from engine.readers.layout import COUNTS
from engine.readers.search import ReadingSearch

KINDS = {"schema_items": models.SchemaItemModel, "readings": models.ReadingModel, "rescorer": models.Rescorer}
BATCH = 8


def load_run(kind, directory):
    """The ``kind`` model of a run in ``directory``, in float32 and eval mode: its encoder built from the config the
    trainer saved beside the tokenizer (a run saved without one, as the re-scorer runs before this package were, from
    the backbone its best.json names), its weights from model.pt."""
    from transformers import AutoConfig, AutoModel
    directory = Path(directory)
    config = directory / "tokenizer"
    if not (config / "config.json").exists():
        config = json.loads((directory / "best.json").read_text(encoding="utf-8"))["backbone"]
    model = KINDS[kind](AutoModel.from_config(AutoConfig.from_pretrained(str(config))))
    model.load_state_dict(torch.load(directory / "model.pt", map_location="cpu", weights_only=True))
    return model.eval()


class Stage1(ReadingSearch):
    """Serving's stage 1 (schema_items), pruning (prune) and stage-2 row (row) over a run's stage-1 model."""

    def __init__(self, stage1_dir, threads=None):           # loads stage 1 only, not serving's whole bundle
        from transformers import AutoTokenizer
        if threads:
            torch.set_num_threads(threads)
        self.tokenizer = AutoTokenizer.from_pretrained(Path(stage1_dir) / "tokenizer")
        self.stage1 = load_run("schema_items", stage1_dir)


def serving_row(reader, question, schema, tables, columns_of, droppable, anchored=True):
    """The stage-2 row serving builds for a question over its kept tables, fitted to the token budget, with the
    empty labels the trainer's batches read."""
    row = reader.row(question, schema, tables, columns_of, droppable, anchored)
    return {"kind": "serve", "question": question, "text": row["text"], "star": row["star"],
            "tables": [dict(t, used=[0, 0]) for t in row["tables"]], "columns": row["columns"],
            "labels": {}, "values": [], "structure": "single",
            "globals": [{"distinct": 0, "conj": "AND", "dir": None, "limit": None,
                         "counts": {kind: 0 for kind in COUNTS}}]}


def main():
    ap = argparse.ArgumentParser(description="Stage-2 outputs of a reading model for a file of rows.")
    ap.add_argument("stage2_dir", help="a readings run (model.pt, tokenizer/)")
    ap.add_argument("rows")
    ap.add_argument("out")
    ap.add_argument("--shard", default="0/1", help="i/n: only rows whose line number is i modulo n")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    shard = tuple(map(int, args.shard.split("/")))
    from transformers import AutoTokenizer
    torch.set_num_threads(args.threads)
    tokenizer = AutoTokenizer.from_pretrained(Path(args.stage2_dir) / "tokenizer")
    model = load_run("readings", args.stage2_dir)
    rows = [json.loads(line) for n, line in enumerate(open(args.rows, encoding="utf-8")) if n % shard[1] == shard[0]]
    with open(args.out, "w", encoding="utf-8", newline="\n", buffering=1) as sink, torch.no_grad():
        for begin in range(0, len(rows), BATCH):
            chunk = rows[begin:begin + BATCH]
            encoded, t = models.reading_inputs(tokenizer, chunk)
            column_logits, table_logits, global_logits, start, end, limit_start, limit_end = model(
                t["ids"], t["mask"], t["column_index"], t["column_mask"], t["table_index"], t["table_mask"])
            for b, row in enumerate(chunk):
                output = models.row_output(row, encoded[b], torch.sigmoid(column_logits[b]),
                                           models.slot_view(column_logits[b]), torch.sigmoid(table_logits[b]),
                                           global_logits[b], start[b], end[b], limit_start[b], limit_end[b],
                                           t["column_valid"][b], t["table_valid"][b])
                sink.write(json.dumps({"kind": row["kind"], "source": row["source"], "index": row["index"],
                                       "db_id": row["db_id"], **output}) + "\n")
    print(f"{len(rows)} outputs -> {args.out}")


if __name__ == "__main__":
    main()
