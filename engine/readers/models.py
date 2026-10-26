"""The three reader models and how they read their inputs: one definition that serving and training share.

- SchemaItemModel (stage 1) reads (question, one table) and scores the table's use and each column's use.
- ReadingModel (stage 2) reads (question, the kept tables) and scores every reading the builder uses
  (engine.readers.layout), plus pointers to value and limit spans.
- Rescorer reads (question, one candidate's read-as line and result shape) beside the decoder's own view of the
  candidate, and scores how likely the candidate answers the question.

Each is an XLM-R encoder with linear heads. Serving builds the encoder from its saved config and loads the
fine-tuned weights (``load``), so no base weights are fetched at serving time. Serving runs each encoder in bfloat16
and its heads in float32: half the memory of float32 and a few times faster on CPU, and on calibration rows no
reading's probability moves by more than 0.06 (p99 0.013). ``load`` places a model on the serving device
(engine.config.serving_device: the CPU, or the hosted edition's GPU); the reading functions run a model on the
device it is on and return its outputs on the CPU.
"""
from __future__ import annotations

import math
import os

import torch
from torch import nn

from engine.readers.layout import AGGREGATES, GLOBAL_WIDTH, LAYOUT, PER_BLOCK, SLOTS

ROLES = ["select", "filter", "group", "order", "having"]
POINTER = 128
MAX_LEN = 512
RESCORER_LEN = 160
RESCORER_FEATURES = 3
RANK_SCALE = 16.0                                  # the re-scorer's rank feature unit (decode.candidates' k)


def _states(encoder, ids, mask):
    """The encoder's last hidden states in float32, whatever precision the encoder runs in."""
    return encoder(input_ids=ids, attention_mask=mask).last_hidden_state.float()


def _pool(states, index, mask):
    """The mean of each span's token states: index [B, S, T] positions, mask [B, S, T]."""
    B, S, T = index.shape
    gathered = torch.gather(states, 1, index.reshape(B, S * T, 1).expand(-1, -1, states.size(-1)))
    gathered = gathered.reshape(B, S, T, -1)
    weights = mask.unsqueeze(-1).to(gathered.dtype)
    return (gathered * weights).sum(2) / weights.sum(2).clamp(min=1.0)


class SchemaItemModel(nn.Module):
    """Stage 1: the table's use from the first token; each column's use and roles from its own words' tokens."""

    def __init__(self, encoder):
        super().__init__()
        self.encoder = encoder
        size = encoder.config.hidden_size
        self.table_head = nn.Sequential(nn.Dropout(0.1), nn.Linear(size, 1))
        self.column_head = nn.Sequential(nn.Dropout(0.1), nn.Linear(size, 1 + len(ROLES)))

    def forward(self, input_ids, attention_mask, span_index, span_mask):
        states = _states(self.encoder, input_ids, attention_mask)
        table_logit = self.table_head(states[:, 0]).squeeze(-1)
        return table_logit, self.column_head(_pool(states, span_index, span_mask))


class ReadingModel(nn.Module):
    """Stage 2: per column (and COUNT's star) and block, the readings of layout.LAYOUT; per table and block, its
    use; from the first token, the per-question readings of layout.GLOBAL; and value and limit pointers."""

    def __init__(self, encoder):
        super().__init__()
        self.encoder = encoder
        size = encoder.config.hidden_size
        self.dropout = nn.Dropout(0.1)
        self.column_head = nn.Linear(size, 2 * PER_BLOCK)
        self.table_head = nn.Linear(size, 2)
        self.global_head = nn.Linear(size, GLOBAL_WIDTH)
        self.token_key = nn.Linear(size, 2 * POINTER)                 # start key, end key
        self.column_query = nn.Linear(size, 2 * 2 * POINTER)          # (block, start/end) queries
        self.limit_query = nn.Linear(size, 2 * POINTER)

    def forward(self, ids, mask, column_index, column_mask, table_index, table_mask):
        states = self.dropout(_states(self.encoder, ids, mask))
        columns = _pool(states, column_index, column_mask)
        tables = _pool(states, table_index, table_mask)
        first = states[:, 0]
        keys = self.token_key(states)
        start_key, end_key = keys[..., :POINTER], keys[..., POINTER:]
        queries = self.column_query(columns).reshape(*columns.shape[:2], 2, 2, POINTER)
        scale = 1.0 / math.sqrt(POINTER)
        start = torch.einsum("bcnp,blp->bcnl", queries[..., 0, :], start_key) * scale
        end = torch.einsum("bcnp,blp->bcnl", queries[..., 1, :], end_key) * scale
        limit = self.limit_query(first)
        limit_start = torch.einsum("bp,blp->bl", limit[:, :POINTER], start_key) * scale
        limit_end = torch.einsum("bp,blp->bl", limit[:, POINTER:], end_key) * scale
        pad = mask == 0
        start = start.masked_fill(pad[:, None, None, :], -1e4)
        end = end.masked_fill(pad[:, None, None, :], -1e4)
        limit_start = limit_start.masked_fill(pad, -1e4)
        limit_end = limit_end.masked_fill(pad, -1e4)
        return (self.column_head(columns).reshape(*columns.shape[:2], 2, PER_BLOCK), self.table_head(tables),
                self.global_head(first), start, end, limit_start, limit_end)


class Rescorer(nn.Module):
    """The first token's state beside the decoder's view of the candidate (log probability, rank, empty result)."""

    def __init__(self, encoder):
        super().__init__()
        self.encoder = encoder
        size = encoder.config.hidden_size
        self.head = nn.Sequential(nn.Dropout(0.1), nn.Linear(size + RESCORER_FEATURES, 1))

    def forward(self, ids, mask, features):
        first = _states(self.encoder, ids, mask)[:, 0]
        return self.head(torch.cat([first, features], dim=-1)).squeeze(-1)


def load(kind, directory):
    """The model of ``kind`` saved in ``directory``, for serving: its encoder built from the saved config in bfloat16,
    its heads in float32, and the saved weights (either precision) copied in."""
    from transformers import AutoConfig, AutoModel
    # Built in bfloat16 rather than cast: a float32 build of XLM-R large holds 2 GB more until it is freed.
    encoder = AutoModel.from_config(AutoConfig.from_pretrained(directory), dtype=torch.bfloat16)
    model = {"schema_items": SchemaItemModel, "readings": ReadingModel, "rescorer": Rescorer}[kind](encoder)
    model.load_state_dict(torch.load(os.path.join(directory, "model.pt"), map_location="cpu", weights_only=True))
    from engine.config import serving_device
    return model.eval().to(serving_device())


def on_device(model, *tensors):
    """``tensors`` moved to the device ``model`` runs on."""
    device = next(model.parameters()).device
    return [t.to(device) for t in tensors]


# ---------------------------------------------------------------------------------------------------- inputs

def token_positions(offsets, sequence, part, start, end):
    """Token positions overlapping characters [start, end) of input part 0 (question) or 1 (schema text)."""
    return [i for i, (s, e) in enumerate(offsets) if sequence[i] == part and s < end and e > start and e > s]


def _spans_tensor(groups, B, S, T):
    index = torch.zeros((B, S, T), dtype=torch.long)
    mask = torch.zeros((B, S, T), dtype=torch.bool)
    valid = torch.zeros((B, S), dtype=torch.bool)
    for b, spans in enumerate(groups):
        for s, positions in enumerate(spans):
            if positions:
                index[b, s, :len(positions)] = torch.tensor(positions)
                mask[b, s, :len(positions)] = True
                valid[b, s] = True
    return index, mask, valid


def _ids_tensor(tokenizer, sequences):
    L = max(len(ids) for ids in sequences)
    ids = torch.full((len(sequences), L), tokenizer.pad_token_id, dtype=torch.long)
    mask = torch.zeros((len(sequences), L), dtype=torch.long)
    for b, tokens in enumerate(sequences):
        ids[b, :len(tokens)] = torch.tensor(tokens)
        mask[b, :len(tokens)] = 1
    return ids, mask


def schema_item_inputs(tokenizer, rows, max_len=MAX_LEN):
    """Stage-1 tensors for rows of {"question", "text", "columns": [{"start", "end"}]}: ids, mask, span index,
    span mask, column valid."""
    encoded = []
    for row in rows:
        enc = tokenizer(row["question"], row["text"], truncation="only_second", max_length=max_len,
                        return_offsets_mapping=True)
        sequence = enc.sequence_ids()
        encoded.append((enc["input_ids"], [token_positions(enc["offset_mapping"], sequence, 1, c["start"], c["end"])
                                           for c in row["columns"]]))
    ids, mask = _ids_tensor(tokenizer, [e[0] for e in encoded])
    C = max(1, max(len(spans) for _, spans in encoded))
    T = max(1, max((len(p) for _, spans in encoded for p in spans), default=1))
    index, span_mask, valid = _spans_tensor([spans for _, spans in encoded], len(rows), C, T)
    return ids, mask, index, span_mask, valid


def reading_encode(tokenizer, row, max_len=MAX_LEN):
    """(token ids, offsets, sequence ids, star + column spans, table spans) for one stage-2 row."""
    enc = tokenizer(row["question"], row["text"], truncation="only_second", max_length=max_len,
                    return_offsets_mapping=True)
    offsets, sequence = enc["offset_mapping"], enc.sequence_ids()
    columns = [token_positions(offsets, sequence, 1, *row["star"])]
    columns += [token_positions(offsets, sequence, 1, *c["span"]) for c in row["columns"]]
    tables = [token_positions(offsets, sequence, 1, *t["span"]) for t in row["tables"]]
    return enc["input_ids"], offsets, sequence, columns, tables


def reading_inputs(tokenizer, rows, max_len=MAX_LEN):
    """(encoded rows, tensors) for stage-2 rows: ids, mask, column index/mask/valid, table index/mask/valid."""
    encoded = [reading_encode(tokenizer, row, max_len) for row in rows]
    ids, mask = _ids_tensor(tokenizer, [e[0] for e in encoded])
    C = max(len(e[3]) for e in encoded)
    Tb = max(1, max(len(e[4]) for e in encoded))
    T = max(1, max((len(p) for e in encoded for p in e[3] + e[4]), default=1))
    column_index, column_mask, column_valid = _spans_tensor([e[3] for e in encoded], len(rows), C, T)
    table_index, table_mask, table_valid = _spans_tensor([e[4] for e in encoded], len(rows), Tb, T)
    return encoded, dict(ids=ids, mask=mask, column_index=column_index, column_mask=column_mask,
                         column_valid=column_valid, table_index=table_index, table_mask=table_mask,
                         table_valid=table_valid)


def slot_view(column_logits):
    offset, width = LAYOUT["slot"]
    return column_logits[..., offset:offset + width].reshape(*column_logits.shape[:-1], len(AGGREGATES), SLOTS)


def top_spans(start, end, k=5, width=12):
    """The k best (start, end, score) spans with start <= end < start + width."""
    start, end = start.tolist(), end.tolist()
    spans = []
    for s in sorted(range(len(start)), key=lambda i: -start[i])[:k * 2]:
        e = max(range(s, min(len(end), s + width)), key=lambda j: end[j])
        spans.append((s, e, start[s] + end[e]))
    spans.sort(key=lambda x: -x[2])
    return spans[:k]


def row_output(row, encoded, column_p, slots, table_p, global_logits, start, end, limit_start, limit_end,
               column_valid, table_valid):
    """One question's outputs as decoding reads them: each column's probabilities and slot distributions per block,
    each table's use per block, the first-token logits, and the top value and limit spans as (part, start char,
    end char, score)."""
    tokens, offsets, sequence = encoded[0], encoded[1], encoded[2]
    n = len(tokens)
    names = ["*"] + [c["name"] for c in row["columns"]]
    spans = {}
    for c, name in enumerate(names):
        if not column_valid[c]:
            continue
        for k in range(2):
            spans[f"{k}|{name}"] = [[sequence[s], offsets[s][0], offsets[e][1], round(score, 3)]
                                    for s, e, score in top_spans(start[c, k, :n], end[c, k, :n])
                                    if sequence[s] is not None and sequence[s] == sequence[e]]
    return {
        "columns": {name: [[round(x, 4) for x in column_p[c, k].tolist()] for k in range(2)]
                    for c, name in enumerate(names) if column_valid[c]},
        "slots": {name: [slots[c, k].softmax(-1).round(decimals=4).tolist() for k in range(2)]
                  for c, name in enumerate(names) if column_valid[c]},
        "tables": {t["name"]: [round(x, 4) for x in table_p[i].tolist()]
                   for i, t in enumerate(row["tables"]) if table_valid[i]},
        "globals": [round(x, 4) for x in global_logits.tolist()],
        "values": spans,
        "limit": [[sequence[s], offsets[s][0], offsets[e][1], round(score, 3)]
                  for s, e, score in top_spans(limit_start[:n], limit_end[:n], k=3, width=3)
                  if sequence[s] == 0 and sequence[e] == 0]}


def read_outputs(model, tokenizer, row):
    """The stage-2 outputs for one row."""
    with torch.no_grad():
        encoded, t = reading_inputs(tokenizer, [row])
        outputs = model(*on_device(model, t["ids"], t["mask"], t["column_index"], t["column_mask"],
                                   t["table_index"], t["table_mask"]))
        column_logits, table_logits, global_logits, start, end, limit_start, limit_end = [o.cpu() for o in outputs]
        return row_output(row, encoded[0], torch.sigmoid(column_logits[0]), slot_view(column_logits[0]),
                          torch.sigmoid(table_logits[0]), global_logits[0], start[0], end[0], limit_start[0],
                          limit_end[0], t["column_valid"][0], t["table_valid"][0])


def candidate_text(question, candidate):
    """The re-scorer's reading of a candidate: (question, "<read-as> | <rows> rows of <columns> columns")."""
    return question, f"{candidate['read_as']} | {candidate['rows']} rows of {candidate['columns']} columns"


def candidate_features(candidate):
    return [candidate["score"] / 10.0, candidate["rank"] / RANK_SCALE, float(candidate["rows"] == 0)]


def rescorer_inputs(tokenizer, question, candidates):
    pairs = [candidate_text(question, c) for c in candidates]
    enc = tokenizer([p[0] for p in pairs], [p[1] for p in pairs], truncation="longest_first",
                    max_length=RESCORER_LEN, padding=True, return_tensors="pt")
    return enc["input_ids"], enc["attention_mask"], torch.tensor([candidate_features(c) for c in candidates],
                                                                 dtype=torch.float)


def rescore(model, tokenizer, question, candidates):
    """The re-scorer's logit for each candidate, in their order."""
    if not candidates:
        return []
    with torch.no_grad():
        return model(*on_device(model, *rescorer_inputs(tokenizer, question, candidates))).float().cpu().tolist()
