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
import sys

# Unsloth patches transformers at import time. The semantic CLI is its only
# consumer here; ordinary XLM readers and hermetic loss tests need no Unsloth.
if sys.argv[1:2] and sys.argv[1] in ("semantic", "semantic-evaluate", "semantic-cache"):
    import unsloth  # noqa: F401

import numpy as np
import torch
from torch import nn
from transformers import AutoModel, AutoTokenizer

from engine.readers import numerals
from engine.readers.layout import (AGGREGATES, COLUMN_POS_WEIGHT, COUNTS, GLOBAL, GLOBAL_WIDTH, HAVING_OPERATORS,
                                   LAYOUT, OPERATORS, PER_BLOCK, SLOTS, STRUCTURES, TABLE_POS_WEIGHT)
from engine.readers.models import (ROLES, ReadingModel, SchemaItemModel, reading_encode, row_output,
                                   schema_item_inputs, slot_view, token_positions)
from training.readers.provenance import assert_no_evaluation_sources


def semantic_stream(path, mode="rt"):
    """One reader for losslessly packed semantic records; contents are identical."""
    if str(path).endswith(".zst"):
        import io
        import zstandard
        if "b" in mode:
            return io.BufferedReader(zstandard.open(path, mode))
        return zstandard.open(path, mode, encoding="utf-8")
    if str(path).endswith(".gz"):
        import gzip
        return gzip.open(path, mode, encoding="utf-8" if "t" in mode else None)
    return open(path, mode, encoding="utf-8" if "t" in mode else None)


class SemanticInputs:
    """Exact frozen records on ephemeral disk, with bounded decoded-row memory.

    This is a byte cache, not a data transform or another corpus. Large source
    samples stay in the hashed input artifact without keeping every copy in RAM.
    """
    def __init__(self, path, cache_size=32):
        import tempfile
        from collections import OrderedDict
        self.stream = tempfile.TemporaryFile(mode="w+b")
        self.positions, self.metadata, self.cache = {}, [], OrderedDict()
        self.cache_size = cache_size
        with semantic_stream(path, "rb") as source:
            for line in source:
                row = json.loads(line)
                if row["split"] not in ("train", "calibration"):
                    continue
                identity = row["id"]
                if identity in self.positions:
                    self.close()
                    raise ValueError("duplicate semantic input identity")
                self.positions[identity] = (self.stream.tell(), len(line))
                self.stream.write(line)
                self.metadata.append({key: row[key] for key in ("id", "source", "db_id", "split", "contract_sha256")})
        self.stream.flush()

    def __getitem__(self, identity):
        if identity not in self.cache:
            offset, length = self.positions[identity]
            self.stream.seek(offset)
            self.cache[identity] = json.loads(self.stream.read(length))
            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        self.cache.move_to_end(identity)
        return self.cache[identity]

    def close(self):
        self.stream.close()


def semantic_required_items(row):
    """States used by the existing head, never a filter on encoder evidence."""
    if "pairs" not in row:
        return list(range(len(row["items"])))
    indices = sorted({index for pair in row["pairs"]
                      for index in (pair["query"], *pair["components"])})
    if any(type(index) is not int or not 0 <= index < len(row["items"]) for index in indices):
        raise ValueError("feature references must identify actual input items")
    return indices


def semantic_input_sha256(row):
    import hashlib
    return hashlib.sha256(json.dumps({"input": row["input"], "items": row["items"]},
                                    sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class SemanticFeatureCache:
    """Lossless FP32 pooled-state cache for one immutable frozen encoder.

    Targets select which already-computed states need storing. They never alter
    the complete encoder input, states or named projection. Every head output
    uses its original physical pair indices, with no feature quantization.
    """
    def __init__(self, root, contract):
        import hashlib
        from pathlib import Path
        self.root, self.contract = Path(root), contract
        self.identity = hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / "feature-contract.json"
        record = {"contract": contract, "contract_sha256": self.identity}
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")) != record:
                raise ValueError("feature cache belongs to another frozen encoder/format")
        else:
            # Workers use disjoint row shards; the immutable identical contract
            # is prepared before concurrent extraction starts.
            manifest.write_text(json.dumps(record, indent=2), encoding="utf-8")

    def paths(self, row):
        import hashlib
        key = hashlib.sha256(row["id"].encode()).hexdigest()
        return self.root / key[:2] / (key + ".pt.zst"), self.root / key[:2] / (key + ".json")

    def load(self, row):
        import hashlib
        import io
        import zstandard
        data_path, metadata_path = self.paths(row)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (metadata["id"] != row["id"] or metadata["input_sha256"] != semantic_input_sha256(row)
                or metadata["contract_sha256"] != self.identity):
            raise ValueError("cached features differ from their exact encoder input/contract")
        packed = data_path.read_bytes()
        if hashlib.sha256(packed).hexdigest() != metadata["data_sha256"]:
            raise ValueError("cached feature bytes are incomplete or changed")
        content = zstandard.ZstdDecompressor().decompress(packed, max_output_size=metadata["raw_bytes"])
        if len(content) != metadata["raw_bytes"]:
            raise ValueError("cached feature length differs")
        payload = torch.load(io.BytesIO(content), map_location="cpu", weights_only=True)
        indices, states = payload["indices"], payload["states"]
        required = semantic_required_items(row)
        if (indices.dtype != torch.int64 or indices.ndim != 1 or indices.tolist() != required
                or states.dtype != torch.float32 or tuple(states.shape) != (len(required), self.contract["width"])
                or not torch.isfinite(states).all()):
            raise ValueError("cached states do not cover the existing head's exact input indices")
        return payload, metadata

    def put(self, row, pooled, tokens):
        import hashlib
        import io
        import zstandard
        if type(tokens) is not int or tokens < 1:
            raise ValueError("cached features require the complete input token count")
        if pooled.ndim != 2 or tuple(pooled.shape) != (len(row["items"]), self.contract["width"]):
            raise ValueError("native pooled-state shape differs from its source items")
        indices = torch.tensor(semantic_required_items(row), dtype=torch.int64)
        states = pooled.detach().to(device="cpu", dtype=torch.float32)[indices].contiguous()
        if not torch.isfinite(states).all():
            raise ValueError("cannot cache non-finite encoder states")
        raw = io.BytesIO(); torch.save({"indices": indices, "states": states}, raw)
        content = raw.getvalue()
        packed = zstandard.ZstdCompressor(level=3, write_checksum=True).compress(content)
        data_path, metadata_path = self.paths(row)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {"id": row["id"], "input_sha256": semantic_input_sha256(row),
                    "contract_sha256": self.identity, "tokens": tokens,
                    "data_sha256": hashlib.sha256(packed).hexdigest(), "raw_bytes": len(content),
                    "packed_bytes": len(packed), "states": len(indices), "width": self.contract["width"]}
        for path, data in ((data_path, packed), (metadata_path, json.dumps(metadata, indent=2).encode())):
            temporary = path.with_name(path.name + f".{os.getpid()}.part")
            temporary.write_bytes(data); os.replace(temporary, path)
        return metadata


def semantic_cached_batch(rows, cache, tokenizer, coordinates):
    """Reuse the sole label/loss collator; cached states replace only its prefill."""
    batch = semantic_batch(rows, [([0], [[0] for _ in row["items"]]) for row in rows], tokenizer, coordinates)
    count = max(len(row["items"]) for row in rows)
    pooled = torch.zeros((len(rows), count, cache.contract["width"]), dtype=torch.float32)
    for i, row in enumerate(rows):
        payload, _ = cache.load(row)
        pooled[i, payload["indices"]] = payload["states"]
    batch["pooled"] = pooled
    return batch


def masked_semantic_loss(logits, targets, observed):
    """Direct coordinate supervision; unknown targets contribute no loss/gradient.

    A missing property is not a negative label. An observation mask must come from
    the source annotation contract; neither a zero target nor an SQL label supplies
    that mask. This loss trains the shared owner NamedSemanticHead, not a second
    selection path. Calibrate every role and quantized binding separately afterward.
    """
    if logits.shape != targets.shape or observed.shape != targets.shape or observed.dtype != torch.bool:
        raise ValueError("semantic logits, targets and boolean observation masks must have identical shapes")
    if not observed.any():
        raise ValueError("semantic batch has no observed supervision")
    if not torch.isfinite(logits).all():
        raise ValueError("semantic logits contain non-finite values")
    known = targets[observed]
    if not torch.isfinite(known).all() or not ((known >= 0) & (known <= 1)).all():
        raise ValueError("observed semantic targets must be finite probabilities in [0,1]")
    safe = torch.where(observed, targets, torch.zeros_like(targets))
    losses = nn.functional.binary_cross_entropy_with_logits(logits.float(), safe.float(), reduction="none")
    return losses[observed].mean()


def semantic_batch(rows, encoded, tokenizer, coordinates):
    """Pad the one prefill and supervise explicit named output pairs separately."""
    from engine.readers.models import _ids_tensor, semantic_bounds
    lookup = {name: i for i, name in enumerate(coordinates)}
    ids, mask = _ids_tensor(tokenizer, [r[0] for r in encoded])
    outputs = max(len(row["targets"]) for row in rows)
    index, span_mask = semantic_bounds([r[1] for r in encoded])
    targets = torch.zeros((len(rows), outputs, len(coordinates)), dtype=torch.float32)
    observed = torch.zeros_like(targets, dtype=torch.bool)
    queries = torch.zeros((len(rows), outputs), dtype=torch.long)
    components = torch.zeros((len(rows), outputs, 3), dtype=torch.long)
    component_mask = torch.zeros_like(components, dtype=torch.bool)
    interactions = torch.zeros_like(queries, dtype=torch.bool)
    choice_axes = torch.zeros_like(queries)
    choice_polarities = torch.zeros_like(queries, dtype=torch.float32)
    groups = []
    for b, row in enumerate(rows):
        pairs = row.get("pairs")
        if pairs is None:
            if len(row["targets"]) != len(row["items"]):
                raise ValueError("unpaired supervision requires one target per input item")
            pairs = [{"query": i, "components": [i], "interaction": False} for i in range(len(row["items"]))]
        if len(pairs) != len(row["targets"]) or len(row.get("outputs", pairs)) != len(pairs):
            raise ValueError("one named target/output is required per explicit pair")
        for i, (pair, labels) in enumerate(zip(pairs, row["targets"])):
            parts, query = pair["components"], pair["query"]
            if (type(query) is not int or not 0 <= query < len(row["items"]) or not 1 <= len(parts) <= 3
                    or any(type(j) is not int or not 0 <= j < len(row["items"]) for j in parts)
                    or type(pair["interaction"]) is not bool):
                raise ValueError("named pair references must identify actual prefill items")
            queries[b, i] = query
            components[b, i, :len(parts)] = torch.tensor(parts)
            component_mask[b, i, :len(parts)] = True
            interactions[b, i] = pair["interaction"]
            if not isinstance(labels, dict) or set(labels) - set(lookup):
                raise ValueError("semantic target must be within the pinned named contract")
            for name, value in labels.items():
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError("semantic annotation must be a finite probability")
                targets[b, i, lookup[name]], observed[b, i, lookup[name]] = value, True
        occupied = set()
        for group in row.get("choice_groups", []):
            start, stop, chosen = group["start"], group["stop"], group["chosen"]
            if not 0 <= start < stop <= len(pairs) or not start <= chosen < stop or len(group["polarities"]) != stop - start:
                raise ValueError("choice group disagrees with its explicit named output range")
            for i, polarity in zip(range(start, stop), group["polarities"]):
                labels = row["targets"][i]
                if i in occupied or len(labels) != 1 or polarity not in (-1, 1):
                    raise ValueError("choice loss requires one observed named axis and unique candidate per output")
                occupied.add(i)
                axis = next(iter(labels))
                choice_axes[b, i], choice_polarities[b, i] = lookup[axis], polarity
            groups.append((b, start, stop, chosen))
    if not observed.any():
        raise ValueError("semantic batch has no known target")
    group_width = max((stop - start for _, start, stop, _ in groups), default=1)
    group_rows = torch.zeros(len(groups), dtype=torch.long)
    group_indices = torch.zeros((len(groups), group_width), dtype=torch.long)
    group_valid = torch.zeros_like(group_indices, dtype=torch.bool)
    group_targets = torch.zeros(len(groups), dtype=torch.long)
    for i, (b, start, stop, chosen) in enumerate(groups):
        group_rows[i], group_targets[i] = b, chosen - start
        group_indices[i, :stop-start], group_valid[i, :stop-start] = torch.arange(start, stop), True
    return {"ids": ids, "mask": mask, "item_index": index, "item_mask": span_mask,
            "has_explicit_pairs": torch.tensor(["pairs" in row for row in rows], dtype=torch.bool),
            "targets": targets, "observed": observed, "pair_queries": queries, "pair_components": components,
            "pair_component_mask": component_mask, "pair_interactions": interactions,
            "choice_axes": choice_axes, "choice_polarities": choice_polarities,
            "group_rows": group_rows, "group_indices": group_indices, "group_valid": group_valid, "group_targets": group_targets}


def semantic_training_loss(logits, tensors):
    """Direct named-coordinate supervision and grouped CE on those same axes."""
    direct = masked_semantic_loss(logits, tensors["targets"], tensors["observed"])
    if not len(tensors["group_rows"]):
        return direct
    scores = logits.gather(-1, tensors["choice_axes"].unsqueeze(-1)).squeeze(-1) * tensors["choice_polarities"]
    selected = scores[tensors["group_rows"].unsqueeze(-1), tensors["group_indices"]]
    selected = selected.masked_fill(~tensors["group_valid"], -torch.inf)
    return direct + nn.functional.cross_entropy(selected.float(), tensors["group_targets"])


def semantic_forward(model, tensors):
    """One definition of paired/static forward used by fit and evaluation."""
    if "pooled" in tensors:
        if tensors["has_explicit_pairs"].any():
            return model.pair_logits(tensors["pooled"], tensors["pair_queries"], tensors["pair_components"],
                                     tensors["pair_component_mask"], tensors["pair_interactions"])
        pooled = tensors["pooled"]
        return model.head(pooled.reshape(-1, pooled.shape[-1])).reshape(*pooled.shape[:2], -1)
    if not tensors["has_explicit_pairs"].any():
        return model(tensors["ids"], tensors["mask"], tensors["item_index"], tensors["item_mask"])
    return model(tensors["ids"], tensors["mask"], tensors["item_index"], tensors["item_mask"],
                 **{key: tensors[key] for key in ("pair_queries", "pair_components", "pair_component_mask", "pair_interactions")})


def semantic_schedule(rows, batch, seed, steps, *, token_lengths=None, token_budget=None, epochs=None):
    """Seeded complete rounds: the short tail is trained before reshuffling."""
    if batch < 1 or steps < 1 or not rows or (epochs is not None and epochs < 1):
        raise ValueError("semantic schedule requires positive bounds and nonempty records")
    if (token_lengths is None) != (token_budget is None):
        raise ValueError("a token budget requires complete pinned input lengths")
    if token_budget is not None and token_budget < 1:
        raise ValueError("training token budget must be positive")
    rng = random.Random(seed)
    order, cursor, rounds = [], 0, 0
    for _ in range(steps):
        if cursor >= len(order):
            if epochs is not None and rounds >= epochs:
                return
            order = list(rows)
            rng.shuffle(order)
            cursor = 0; rounds += 1
        selected, longest = [], 0
        for row in order[cursor:cursor + batch]:
            length = token_lengths[row["id"]] if token_lengths is not None else 0
            longest = max(longest, length)
            if selected and token_budget is not None and longest * (len(selected) + 1) > token_budget:
                break
            selected.append(row)
            if token_budget is not None and longest > token_budget:
                break  # An oversized input runs alone, within max_len and native preflight.
        cursor += len(selected)
        yield selected


def semantic_diagnostics(rows, predictions, coordinates, *, levels=16):
    """Named-target diagnostics, not an SQL evaluator or binding qualification.

    Report the all-zero baseline and binary recall so sparse targets cannot make
    an uninformative head look successful through average error alone. No targets
    select a prediction; saved IDs join immutable annotations after inference.
    """
    by_id = {r["id"]: r for r in rows}
    lookup = {name: i for i, name in enumerate(coordinates)}
    if len(lookup) != len(coordinates) or len(by_id) != len(rows):
        raise ValueError("diagnostics require unique coordinate and source identities")
    buckets, seen, property_roles = {}, set(), set()
    choices = {"groups": 0, "forced_groups": 0, "raw_correct": 0, "quantized_correct": 0,
               "raw_ties": 0, "quantized_ties": 0, "records": 0, "raw_all_correct": 0,
               "quantized_all_correct": 0}
    for prediction in predictions:
        identity = prediction["id"]
        if identity in seen or identity not in by_id:
            raise ValueError("prediction is duplicate or has no source annotation")
        seen.add(identity)
        row = by_id[identity]
        probs = prediction["probabilities"]
        if len(probs) != len(row["targets"]) or len(probs) != len(row.get("outputs", row["items"])):
            raise ValueError("prediction item count differs from its input")
        row_correct = {"raw": True, "quantized": True}
        nontrivial = 0
        for group in row.get("choice_groups", []):
            start, stop, chosen = (group[key] for key in ("start", "stop", "chosen"))
            polarities = group["polarities"]
            if (not 0 <= start <= chosen < stop <= len(probs) or len(polarities) != stop - start
                    or any(p not in (-1, 1) for p in polarities)):
                raise ValueError("invalid conditional choice diagnostic group")
            if stop - start == 1:
                choices["forced_groups"] += 1
                continue
            nontrivial += 1; choices["groups"] += 1
            scores = {"raw": [], "quantized": []}
            for index, polarity in zip(range(start, stop), polarities):
                if len(row["targets"][index]) != 1:
                    raise ValueError("a conditional choice requires one declared named coordinate")
                coordinate, = row["targets"][index]
                probability = probs[index][coordinate] if isinstance(probs[index], dict) else probs[index][lookup[coordinate]]
                value = math.floor(probability * levels + 0.5)
                scores["raw"].append(probability if polarity == 1 else 1 - probability)
                scores["quantized"].append(value if polarity == 1 else levels - value)
            for kind, values in scores.items():
                winners = [i for i, value in enumerate(values) if value == max(values)]
                correct = winners == [chosen - start]
                choices[kind + "_correct"] += int(correct)
                choices[kind + "_ties"] += int(len(winners) > 1)
                row_correct[kind] &= correct
        if nontrivial:
            choices["records"] += 1
            for kind, correct in row_correct.items(): choices[kind + "_all_correct"] += int(correct)
        for item, targets, values in zip(row.get("outputs", row["items"]), row["targets"], probs):
            sparse = isinstance(values, dict)
            if ((sparse and (set(values) - set(lookup) or set(targets) - set(values))) or
                    (not sparse and len(values) != len(coordinates)) or any(type(v) not in (int, float)
                    or not math.isfinite(v) or not 0 <= v <= 1 for v in (values.values() if sparse else values))):
                raise ValueError("invalid named-coordinate probabilities")
            for name, truth in targets.items():
                if name not in lookup or not math.isfinite(truth) or not 0 <= truth <= 1:
                    raise ValueError("invalid source target in semantic diagnostics")
                probability = values[name] if sparse else values[lookup[name]]
                block = ("property" if name.startswith("https://schema.org/") else
                         "dialogue" if name.startswith("dialogue.") else "sql" if name.startswith(("sql.", "intent_"))
                         else "other")
                if block == "property":
                    property_roles.add(item["kind"])
                for key in ("all", "block/" + block, "role/" + item["kind"], "coordinate/" + name):
                    bucket = buckets.setdefault(key, {"known": 0, "absolute_error": 0.0, "zero_absolute_error": 0.0,
                                                       "binary_known": 0, "tp": 0, "fp": 0, "fn": 0, "tn": 0})
                    bucket["known"] += 1
                    bucket["absolute_error"] += abs(probability - truth)
                    bucket["zero_absolute_error"] += truth
                    if truth in (0, 1):
                        bucket["binary_known"] += 1
                        predicted = probability >= 0.5
                        bucket["tp" if predicted and truth else "fp" if predicted else "fn" if truth else "tn"] += 1
    for bucket in buckets.values():
        bucket["mae"] = bucket["absolute_error"] / max(1, bucket["known"])
        bucket["zero_predictor_mae"] = bucket["zero_absolute_error"] / max(1, bucket["known"])
        bucket["precision_at_0_5"] = bucket["tp"] / max(1, bucket["tp"] + bucket["fp"])
        bucket["recall_at_0_5"] = bucket["tp"] / max(1, bucket["tp"] + bucket["fn"])
        bucket["f1_at_0_5"] = 2 * bucket["tp"] / max(1, 2 * bucket["tp"] + bucket["fp"] + bucket["fn"])
    return {"predicted_examples": len(seen), "metrics": buckets,
            "conditional_choices": {**choices, "levels": levels,
                "scope": "Teacher traversal and legal-option contexts; forced choices excluded, ties fail. Not served SQL/answer accuracy."},
            "property_roles_observed": sorted(property_roles),
            "cross_role_evidence_present": {"noun", "column", "value"} <= property_roles,
            "release_qualification": False,
            "scope": "Fixed 0.5 threshold diagnostic on annotated coordinates, not calibrated binding or answer accuracy."}


@torch.inference_mode()
def evaluate_semantic(model, rows, encoded, tokenizer, basis, output, *, batch_size=1, token_budget=16384,
                      feature_cache=None):
    """The shared named-target evaluator for fit diagnostics and full held-out runs.

    Length buckets bound padding, never discard input. This is coordinate target
    evaluation; the one served SQL evaluator remains a separate required gate.
    """
    import gzip

    if type(batch_size) is not int or type(token_budget) is not int or batch_size < 1 or token_budget < 1:
        raise ValueError("semantic evaluation requires positive batch and token budgets")
    model.eval()
    device = next(model.head.parameters()).device
    ordered = sorted(rows, key=lambda r: (len(encoded[r["id"]][0]), r["id"]))
    if any(len(encoded[r["id"]][0]) > token_budget for r in ordered):
        raise ValueError("a complete semantic item exceeds the evaluation token budget")
    buckets, pending = {}, []

    def batches():
        nonlocal pending
        for row in ordered:
            if pending and (len(pending) >= batch_size
                            or (len(pending) + 1) * len(encoded[row["id"]][0]) > token_budget):
                yield pending
                pending = []
            pending.append(row)
        if pending:
            yield pending

    with gzip.open(output, "wt", encoding="utf-8") as sink:
        for selected in batches():
            started = time.perf_counter()
            batch = (semantic_cached_batch(selected, feature_cache, tokenizer, basis.coordinates) if feature_cache
                     else semantic_batch(selected, [encoded[r["id"]] for r in selected], tokenizer, basis.coordinates))
            tensors = {k: v.to(device) for k, v in batch.items()}
            logits = semantic_forward(model, tensors)
            probabilities = torch.sigmoid(logits).cpu()
            if not torch.isfinite(probabilities).all():
                raise ValueError("non-finite semantic evaluation")
            elapsed = time.perf_counter() - started
            for i, row in enumerate(selected):
                count = len(row["targets"])
                truth, observed = batch["targets"][i, :count], batch["observed"][i, :count]
                error = ((probabilities[i, :count] - truth).abs() * observed).sum().item()
                for group in ("all", row["source"]):
                    bucket = buckets.setdefault(group, {"examples": 0, "known": 0, "absolute_error": 0.0})
                    bucket["examples"] += 1; bucket["known"] += int(observed.sum()); bucket["absolute_error"] += error
                for j, item in enumerate(row.get("outputs", row["items"])):
                    bucket = buckets.setdefault(item["kind"], {"examples": 0, "known": 0, "absolute_error": 0.0})
                    bucket["examples"] += 1; bucket["known"] += int(observed[j].sum())
                    bucket["absolute_error"] += ((probabilities[i, j] - truth[j]).abs() * observed[j]).sum().item()
                if "pairs" in row:
                    known = [{name: float(probabilities[i, j, basis.coordinates.index(name)]) for name in labels}
                             for j, labels in enumerate(row["targets"])]
                    quantized = [{name: int(math.floor(value * basis.levels + 0.5)) for name, value in item.items()} for item in known]
                else:
                    known = probabilities[i, :count].tolist()
                    quantized = torch.floor(probabilities[i, :count] * basis.levels + 0.5).int().tolist()
                sink.write(json.dumps({"id": row["id"], "probabilities": known,
                                       "quantized": quantized,
                                       "output_domain": "named_pairs" if "pairs" in row else "input_items",
                                       "batch_seconds": elapsed, "batch_records": len(selected)}) + "\n")
    return {"metrics": {name: {**v, "mae": v["absolute_error"] / max(1, v["known"])}
                        for name, v in buckets.items()}, "examples": len(rows), "batch_size": batch_size,
            "release_qualification": False, "qualified_coordinates": sum(basis.qualified)}


def export_semantic_encoder_source(checkpoint, tokenizer_directory, destination):
    """Seal exact runtime bytes from a partial fit; never call it a completed fit."""
    import hashlib
    import shutil
    from pathlib import Path

    checkpoint, tokenizer_directory, destination = map(Path, (checkpoint, tokenizer_directory, destination))
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("never overwrite a sealed frozen encoder source")
    source_metadata = checkpoint / "semantic-model.json"
    metadata = json.loads(source_metadata.read_text(encoding="utf-8"))
    adapter = metadata.get("adapter")
    selected = {name: checksum for name, checksum in metadata["files"].items()
                if name == "named-head.pt" or adapter and name.startswith(adapter.rstrip("/") + "/")}
    if "named-head.pt" not in selected:
        raise ValueError("source checkpoint has no manifested named head")
    destination.mkdir(parents=True, exist_ok=True)
    for name, checksum in selected.items():
        source = (checkpoint / name).resolve()
        if not source.is_relative_to(checkpoint.resolve()) or hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
            raise ValueError("source checkpoint runtime bytes changed")
        target = (destination / name).resolve()
        if not target.is_relative_to(destination.resolve()):
            raise ValueError("source checkpoint runtime path escapes destination")
        target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)
    for source in tokenizer_directory.rglob("*"):
        if source.is_file():
            target = destination / "tokenizer" / source.relative_to(tokenizer_directory)
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, target)
    metadata.update(state="unqualified_frozen_encoder_source_from_partial_fit",
                    parent_checkpoint_metadata_sha256=hashlib.sha256(source_metadata.read_bytes()).hexdigest())
    metadata["files"] = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in destination.rglob("*") if p.is_file()}
    (destination / "semantic-model.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def semantic_feature_contract(candidate, tokenizer_directory):
    """Pin the featurizer, excluding the trainable named head and label data."""
    import hashlib
    import inspect
    from pathlib import Path
    from engine.readers.models import NamedSemanticModel, SEMANTIC_INPUT_FORMAT, _pool_bounds, semantic_encode

    candidate, tokenizer_directory = Path(candidate), Path(tokenizer_directory)
    metadata = json.loads((candidate / "semantic-model.json").read_text(encoding="utf-8"))
    adapter = metadata.get("adapter")
    encoder_files = {name: expected for name, expected in metadata["files"].items()
                     if adapter and name.startswith(adapter.rstrip("/") + "/")}
    if adapter and not encoder_files:
        raise ValueError("frozen encoder adapter is not manifested")
    for name, expected in encoder_files.items():
        path = (candidate / name).resolve()
        if not path.is_relative_to(candidate.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("frozen encoder adapter changed")
    tokenizer_files = {p.relative_to(tokenizer_directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in tokenizer_directory.rglob("*") if p.is_file()}
    if not tokenizer_files:
        raise ValueError("frozen feature extraction requires a pinned tokenizer")
    head = torch.load(candidate / "named-head.pt", map_location="cpu", weights_only=True)
    return {"schema_version": 1, "backbone": metadata["backbone"], "revision": metadata["revision"],
            "adapter_files": encoder_files, "tokenizer_files": tokenizer_files,
            "width": int(head["projection.weight"].shape[1]), "input_format": SEMANTIC_INPUT_FORMAT,
            "prefill": "whole_complete_input_batch_one", "state_dtype": "float32_lossless",
            "cuda_fp32_matmul": "ieee", "cudnn_deterministic": True,
            "serialization_sha256": hashlib.sha256(inspect.getsource(semantic_encode).encode()).hexdigest(),
            "pooling_sha256": hashlib.sha256(inspect.getsource(_pool_bounds).encode()).hexdigest(),
            "native_prefill_sha256": hashlib.sha256(inspect.getsource(NamedSemanticModel.pooled_states).encode()).hexdigest()}


@torch.inference_mode()
def cache_semantic_features(args):
    """Prepare immutable encoder features once; never optimize on held-out rows."""
    import hashlib
    from pathlib import Path
    from engine.readers.models import load, semantic_encode, semantic_bounds

    pieces = args.shard.split("/")
    if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
        raise ValueError("feature shard must be i/n")
    shard, shards = map(int, pieces)
    if not 0 <= shard < shards or args.max_seconds <= 0:
        raise ValueError("feature extraction requires bounded valid shards")
    cache = SemanticFeatureCache(args.out, semantic_feature_contract(args.candidate, args.tokenizer))
    inputs = SemanticInputs(args.examples)
    rows = [row for row in inputs.metadata if (args.split == "all" or row["split"] == args.split)
            and int(hashlib.sha256(row["id"].encode()).hexdigest(), 16) % shards == shard]
    if not rows:
        raise ValueError("feature shard is empty")
    if args.limit:
        rows.sort(key=lambda row: hashlib.sha256(("3407/feature-pilot/" + row["id"]).encode()).digest())
        rows = rows[:args.limit]
    rows.sort(key=lambda row: (row["db_id"], row["id"]))
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, fix_mistral_regex=True)
    model = None; started = time.monotonic(); completed = reused = tokens = packed_bytes = 0
    forward_seconds = 0.0
    for metadata in rows:
        if time.monotonic() - started > args.max_seconds:
            break
        row = inputs[metadata["id"]]
        _, record_path = cache.paths(row)
        if record_path.exists():
            _, record = cache.load(row)
            reused += 1
        else:
            if model is None:
                model = load("semantic", args.candidate)
                model.requires_grad_(False); model.eval()
            encoded = semantic_encode(tokenizer, row["input"], row["items"], args.max_len)
            ids = torch.tensor([encoded[0]], dtype=torch.long, device=next(model.parameters()).device)
            bounds, valid = semantic_bounds([encoded[1]])
            before = time.perf_counter()
            pooled = model.pooled_states(ids, torch.ones_like(ids), bounds.to(ids.device), valid.to(ids.device))[0]
            torch.cuda.synchronize() if ids.is_cuda else None
            forward_seconds += time.perf_counter() - before
            record = cache.put(row, pooled, len(encoded[0]))
            del pooled, ids, bounds, valid
        completed += 1; tokens += record["tokens"]; packed_bytes += record["packed_bytes"]
        if completed % 25 == 0 or completed == len(rows):
            progress = {"stage": "frozen_semantic_features", "shard": args.shard, "completed": completed,
                        "total": len(rows), "reused": reused, "tokens": tokens, "packed_bytes": packed_bytes,
                        "forward_seconds": forward_seconds, "seconds": time.monotonic() - started}
            print(json.dumps(progress), flush=True)
            (Path(args.out) / ("progress-" + args.shard.replace("/", "-") + ".json")).write_text(json.dumps(progress))
    inputs.close()
    print(json.dumps({"stage": "frozen_feature_shard_complete" if completed == len(rows) else "frozen_feature_shard_checkpoint",
                      "shard": args.shard, "completed": completed, "total": len(rows),
                      "contract_sha256": cache.identity, "gradient_updates": 0}), flush=True)


def evaluate_semantic_candidate(args):
    import gzip
    import hashlib
    from pathlib import Path

    from engine.readers.models import load, semantic_encode

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("never overwrite a completed candidate evaluation")
    candidate = Path(args.candidate)
    metadata = json.loads((candidate / "semantic-model.json").read_text(encoding="utf-8"))
    h = hashlib.sha256()
    with open(args.examples, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != metadata["examples_sha256"]:
        raise ValueError("candidate evaluation source differs from its frozen dataset")
    with semantic_stream(args.examples) as stream:
        rows = [r for r in map(json.loads, stream) if r["split"] == args.split
                and (not args.role or any(i["kind"] == args.role for i in r["items"]))]
    if not rows:
        raise ValueError("candidate evaluation split is empty")
    tokenizer = AutoTokenizer.from_pretrained(candidate / "tokenizer", fix_mistral_regex=True)
    feature_cache = (SemanticFeatureCache(args.feature_cache, semantic_feature_contract(candidate, candidate / "tokenizer"))
                     if args.feature_cache else None)
    if feature_cache:
        encoded = {row["id"]: (range(feature_cache.load(row)[1]["tokens"]), None) for row in rows}
    else:
        encoded = {r["id"]: semantic_encode(tokenizer, r["input"], r["items"], args.max_len) for r in rows}
    model = load("semantic", str(candidate), encoder=feature_cache is None)
    if feature_cache:
        model.to("cuda" if torch.cuda.is_available() else "cpu")
    out.mkdir(parents=True, exist_ok=True)
    predictions_path = out / "predictions.jsonl.gz"
    result = evaluate_semantic(model, rows, encoded, tokenizer, model.head.basis, predictions_path,
                               batch_size=args.batch, token_budget=args.token_budget, feature_cache=feature_cache)
    with gzip.open(predictions_path, "rt", encoding="utf-8") as stream:
        diagnostics = semantic_diagnostics(rows, list(map(json.loads, stream)), model.head.basis.coordinates)
    result.update(diagnostics=diagnostics, split=args.split, role_filter=args.role,
                  examples_sha256=metadata["examples_sha256"],
                  candidate_metadata_sha256=hashlib.sha256((candidate / "semantic-model.json").read_bytes()).hexdigest(),
                  predictions_sha256=hashlib.sha256(predictions_path.read_bytes()).hexdigest())
    (out / "assessment.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


def qualify_semantic_candidate(args):
    """Seal role/coordinate gates from complete, held-out named predictions.

    This qualifies a decision interface only. Spider/Enron answer accuracy,
    latency, generalization and promotion remain separate acceptance gates.
    """
    import gzip
    import hashlib
    from pathlib import Path
    from collections import Counter
    from engine.schema_decode import BindingBasis
    candidate = Path(args.candidate)
    metadata_path = candidate / "semantic-model.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assessment_path = Path(args.evaluation) / "assessment.json"
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    predictions_path = Path(args.evaluation) / "predictions.jsonl.gz"
    sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if (assessment["split"] != "calibration" or assessment.get("role_filter") is not None
            or assessment["candidate_metadata_sha256"] != sha(metadata_path)
            or assessment["predictions_sha256"] != sha(predictions_path)
            or metadata["examples_sha256"] != sha(args.examples)):
        raise ValueError("qualification requires the complete pinned calibration evaluation")
    with semantic_stream(args.examples) as stream:
        rows = {row["id"]: row for row in map(json.loads, stream) if row["split"] == "calibration"}
    assert_no_evaluation_sources(rows.values(), "named interface calibration")
    counts, seen = {}, set()
    basis = BindingBasis.from_record(metadata["basis"])
    lookup = {name: i for i, name in enumerate(basis.coordinates)}
    with gzip.open(predictions_path, "rt", encoding="utf-8") as stream:
        for prediction in map(json.loads, stream):
            identity = prediction["id"]
            if identity not in rows or identity in seen:
                raise ValueError("calibration predictions have unknown or duplicate source IDs")
            seen.add(identity); row = rows[identity]
            outputs = row.get("outputs", row["items"])
            if len(prediction["probabilities"]) != len(outputs):
                raise ValueError("calibration output counts disagree")
            for output, targets, probabilities in zip(outputs, row["targets"], prediction["probabilities"]):
                for name, truth in targets.items():
                    value = probabilities[name] if isinstance(probabilities, dict) else probabilities[lookup[name]]
                    if not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError("calibration probability is invalid")
                    value = math.floor(value * basis.levels + 0.5) / basis.levels
                    key = (output["kind"], name)
                    count = counts.setdefault(key, Counter())
                    count["known"] += 1; count["absolute_error"] += abs(value - truth)
                    if truth in (0, 1):
                        predicted = value >= 0.5
                        count["tp" if predicted and truth else "fp" if predicted else "fn" if truth else "tn"] += 1
    if seen != set(rows) or len(rows) != assessment["examples"]:
        raise ValueError("qualification is missing calibration records")
    metrics, role_masks = [], {}
    for (role, name), count in sorted(counts.items()):
        precision = count["tp"] / max(1, count["tp"] + count["fp"])
        recall = count["tp"] / max(1, count["tp"] + count["fn"])
        mae = count["absolute_error"] / max(1, count["known"])
        accepted = (count["tp"] + count["fn"] >= args.min_support
                    and count["tn"] + count["fp"] >= args.min_support
                    and min(precision, recall) >= args.min_accuracy and mae <= args.max_mae)
        role_masks.setdefault(role, [False] * len(basis.coordinates))[lookup[name]] = accepted
        metrics.append({"role": role, "coordinate": name, **count, "precision": precision,
                        "recall": recall, "mae": mae, "qualified": accepted})
    masks = tuple((role, tuple(mask)) for role, mask in sorted(role_masks.items()))
    union = tuple(any(mask[i] for _, mask in masks) for i in range(len(basis.coordinates)))
    evidence = {"candidate_metadata_sha256": sha(metadata_path), "evaluation_sha256": sha(assessment_path),
                "examples_sha256": metadata["examples_sha256"], "predictions_sha256": sha(predictions_path),
                "calibration_examples": len(rows), "metrics": metrics,
                "gates": {"min_support_per_binary_class": args.min_support,
                          "min_precision_and_recall": args.min_accuracy, "max_mae": args.max_mae},
                "decision_policy": {"min_score": [1, 2], "min_margin": [1, 16]},
                "quantization": {"levels": basis.levels, "rule": "round half up before qualification/gates"},
                "state": "candidate_interface_only_not_accuracy_or_promotion_qualified"}
    calibration_id = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    evidence["basis"] = BindingBasis(basis.coordinates, basis.weights, union,
                                     calibration_id, basis.levels, masks).record()
    output = candidate / "qualification.json"
    if output.exists(): raise ValueError("never overwrite a sealed qualification")
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({"qualified_coordinates": sum(union), "calibration_examples": len(rows)}), flush=True)


def train_semantic(args):
    """Controlled CLEF fit of the shared named bottleneck; no runtime promotion."""
    import hashlib
    from pathlib import Path

    from engine.readers.models import clef_semantic_model, load, semantic_encode, SEMANTIC_INPUT_FORMAT
    from engine.schema_decode import BindingBasis

    def sha(path):
        h = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    out, examples_path = Path(args.out), Path(args.examples)
    experiment = json.loads(Path(args.experiment_contract).read_text(encoding="utf-8"))
    required = {"hypothesis", "frozen_baseline", "training_data", "objective", "seeds", "offline_gates",
                "serving_gates", "candidate_output", "promotion_and_rollback", "compute_bound"}
    if not required <= set(experiment) or args.seed not in experiment["seeds"]:
        raise ValueError("fit requires the complete frozen experiment contract and seed")
    if out.resolve() != Path(experiment["candidate_output"]).resolve():
        raise ValueError("candidate output differs from the frozen experiment contract")
    parts = tuple(p.casefold() for p in out.resolve().parts)
    if any(parts[i:i + 2] == ("engine", "data") for i in range(len(parts) - 1)):
        raise ValueError("training cannot write into the promoted runtime bundle")
    if sha(examples_path) != experiment["training_data"]["sha256"]:
        raise ValueError("fit data differs from the frozen experiment contract")
    if out.exists() and any(out.iterdir()) and not args.resume_checkpoint:
        raise ValueError("candidate directory is not empty; never overwrite a fit")
    if (out / "semantic-model.json").exists():
        raise ValueError("never overwrite a completed candidate")
    if args.steps < 1 or args.steps > experiment["compute_bound"]["max_steps"]:
        raise ValueError("steps exceed the frozen experiment bound")
    if args.checkpoint_every < 1:
        raise ValueError("checkpoint interval must be positive")
    if args.max_seconds > experiment["compute_bound"]["max_training_seconds"]:
        raise ValueError("runtime exceeds the frozen experiment bound")
    for key, actual in {"backbone": args.backbone, "revision": args.revision, "lora_rank": args.lora_rank,
                        "lr": args.lr, "head_lr": args.head_lr, "batch": args.batch,
                        "max_length": args.max_len, "epochs": args.epochs, "token_budget": args.token_budget}.items():
        if key in experiment["objective"] and experiment["objective"][key] != actual:
            raise ValueError("fit hyperparameter differs from the frozen experiment: " + key)
    coordinate_contract = json.loads(Path(args.coordinates).read_text(encoding="utf-8"))
    coordinates = tuple(r["name"] for r in coordinate_contract["coordinates"])
    if coordinate_contract["contract_sha256"] != experiment["training_data"]["coordinate_contract_sha256"]:
        raise ValueError("fit coordinate meanings differ from the frozen contract")
    # Vocabulary definition does not qualify a learned coordinate. Calibration is
    # a separate held-out gate; no trained candidate is silently made servable.
    basis = BindingBasis(coordinates, (1,) * len(coordinates), (False,) * len(coordinates),
                         "uncalibrated-candidate/" + sha(args.experiment_contract))
    inputs = SemanticInputs(examples_path)
    rows = inputs.metadata
    assert_no_evaluation_sources(rows, "named semantic fit")
    if any(r["contract_sha256"] != coordinate_contract["contract_sha256"] for r in rows):
        raise ValueError("an example uses another named basis")
    train = [r for r in rows if r["split"] == "train"]
    calibration = [r for r in rows if r["split"] == "calibration"]
    if not train or not calibration or {r["db_id"] for r in train} & {r["db_id"] for r in calibration}:
        raise ValueError("semantic fit requires nonempty, source-group-disjoint partitions")
    # Fixed sample selected before any prediction. It is explicitly a pilot
    # diagnostic, not the full calibration or a release qualification result.
    calibration.sort(key=lambda r: hashlib.sha256((str(args.seed) + r["id"]).encode()).digest())
    calibration = [inputs[r["id"]] for r in calibration[:args.eval_records]]
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    resume_dir = Path(args.resume_checkpoint) if args.resume_checkpoint else None
    resumed = None
    if resume_dir is not None:
        manifest = json.loads((resume_dir / "semantic-model.json").read_text(encoding="utf-8"))
        if (manifest["examples_sha256"] != sha(examples_path)
                or manifest["experiment_sha256"] != sha(args.experiment_contract)
                or manifest["seed"] != args.seed or manifest["basis"] != basis.record()):
            raise ValueError("checkpoint differs from the frozen experiment")
        for name, expected in manifest["files"].items():
            path = (resume_dir / name).resolve()
            if not path.is_relative_to(resume_dir.resolve()) or sha(path) != expected:
                raise ValueError("checkpoint artifact hash mismatch")
        resumed = torch.load(resume_dir / "training-state.pt", map_location="cpu", weights_only=True)
    feature_cache = None
    initial_candidate = Path(args.initial_candidate) if args.initial_candidate else None
    initial_metadata = None
    if initial_candidate is not None:
        if args.lora_rank != 0 or initial_candidate is None:
            raise ValueError("frozen encoder fitting requires rank zero and the exact frozen source artifact")
        initial_metadata_path = initial_candidate / "semantic-model.json"
        initial_metadata = json.loads(initial_metadata_path.read_text(encoding="utf-8"))
        if (initial_metadata["backbone"] != args.backbone or initial_metadata["revision"] != args.revision
                or experiment["frozen_encoder"]["metadata_sha256"] != sha(initial_metadata_path)):
            raise ValueError("frozen source differs from the controlled experiment")
        feature_contract = semantic_feature_contract(initial_candidate, initial_candidate / "tokenizer")
        import hashlib
        feature_identity = hashlib.sha256(json.dumps(feature_contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if feature_identity != experiment["frozen_encoder"]["feature_contract_sha256"]:
            raise ValueError("frozen feature contract differs from the controlled experiment")
        if args.feature_cache:
            feature_cache = SemanticFeatureCache(args.feature_cache, feature_contract)
        model = load("semantic", str(initial_candidate), encoder=feature_cache is None)
        if model.encoder is not None:
            model.encoder.requires_grad_(False)
        model.head.basis = basis
        if feature_cache:
            model.to("cuda" if torch.cuda.is_available() else "cpu")
        tokenizer = AutoTokenizer.from_pretrained(initial_candidate / "tokenizer", fix_mistral_regex=True)
    else:
        if args.feature_cache:
            raise ValueError("frozen feature fitting requires the exact frozen source artifact")
        model, tokenizer = clef_semantic_model(basis, backbone=args.backbone, revision=args.revision,
                                               lora_rank=args.lora_rank, seed=args.seed,
                                               adapter=str(resume_dir / "adapter") if resume_dir is not None and args.lora_rank else None,
                                               trainable_adapter=resume_dir is not None)
    if resume_dir is not None:
        model.head.load_state_dict(torch.load(resume_dir / "named-head.pt", map_location="cpu", weights_only=True))
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    device = next(model.head.parameters()).device
    from collections import OrderedDict
    by_id = {row["id"]: row for row in [*train, *calibration]}
    if len(by_id) != len(train) + len(calibration):
        raise ValueError("duplicate semantic input identity")

    class Encodings:
        def __init__(self): self.cache = OrderedDict()
        def __getitem__(self, identity):
            if identity not in self.cache:
                row = inputs[identity]
                if feature_cache:
                    _, metadata = feature_cache.load(row)
                    if metadata["tokens"] > args.max_len:
                        raise ValueError("cached complete input exceeds its explicit token bound")
                    self.cache[identity] = (range(metadata["tokens"]), None)
                else:
                    self.cache[identity] = semantic_encode(tokenizer, row["input"], row["items"], args.max_len)
                while len(self.cache) > 1024: self.cache.popitem(last=False)
            self.cache.move_to_end(identity)
            return self.cache[identity]
    encoded = Encodings()
    out.mkdir(parents=True, exist_ok=True)
    if initial_candidate is not None:
        import shutil
        for path in (initial_candidate / "tokenizer").rglob("*"):
            if path.is_file():
                target = out / "tokenizer" / path.relative_to(initial_candidate / "tokenizer")
                target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, target)
    else:
        tokenizer.save_pretrained(out / "tokenizer")
    adapter_name = "adapter" if args.lora_rank or initial_metadata and initial_metadata.get("adapter") else None

    def save_adapter(destination):
        if initial_metadata and adapter_name:
            import shutil
            source = initial_candidate / initial_metadata["adapter"]
            for path in source.rglob("*"):
                if path.is_file():
                    target = destination / adapter_name / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, target)
        elif args.lora_rank:
            model.encoder.save_pretrained(destination / adapter_name)
    log = open(out / "log.jsonl", "a", encoding="utf-8")

    def batch(selected):
        selected = [inputs[r["id"]] for r in selected]
        tensors = (semantic_cached_batch(selected, feature_cache, tokenizer, coordinates) if feature_cache
                   else semantic_batch(selected, [encoded[r["id"]] for r in selected], tokenizer, coordinates))
        return {k: v.to(device) for k, v in tensors.items()}

    def forward(tensors):
        return semantic_forward(model, tensors)

    def evaluate(label):
        result = evaluate_semantic(model, calibration, encoded, tokenizer, basis,
                                   out / (label + "-predictions.jsonl.gz"), token_budget=args.max_len,
                                   feature_cache=feature_cache)
        import gzip
        with gzip.open(out / (label + "-predictions.jsonl.gz"), "rt", encoding="utf-8") as stream:
            diagnostics = semantic_diagnostics(calibration, list(map(json.loads, stream)), coordinates, levels=basis.levels)
        record = {**result, "stage": label, "calibration_sample": len(calibration), "diagnostics": diagnostics}
        log.write(json.dumps(record) + "\n"); log.flush(); print(json.dumps(record), flush=True)
        return record

    initial = resumed["initial"] if resumed is not None else evaluate("initial")
    lengths_path = out / "token-lengths.json"
    from engine.readers import models as semantic_models
    lengths_contract = {"examples_sha256": sha(examples_path),
                        "encoder_source_sha256": sha(semantic_models.__file__),
                        "input_format": SEMANTIC_INPUT_FORMAT}
    if feature_cache:
        lengths_contract["feature_contract_sha256"] = feature_cache.identity
    if lengths_path.exists():
        saved_lengths = json.loads(lengths_path.read_text(encoding="utf-8"))
        if saved_lengths["contract"] != lengths_contract:
            raise ValueError("cached input lengths belong to another source/encoder contract")
        token_lengths = saved_lengths["lengths"]
        if set(token_lengths) != {row["id"] for row in train}:
            raise ValueError("cached lengths do not cover every training record")
    else:
        token_lengths = {}
        for i, row in enumerate(train):
            token_lengths[row["id"]] = len(encoded[row["id"]][0])
            if (i + 1) % 1000 == 0:
                print(json.dumps({"stage": "complete_input_audit", "records": i + 1, "total": len(train),
                                  "maximum_tokens": max(token_lengths.values())}), flush=True)
        lengths_path.write_text(json.dumps({"contract": lengths_contract, "lengths": token_lengths}), encoding="utf-8")
    lengths = sorted(train, key=lambda r: (token_lengths[r["id"]], r["id"]))
    schedule_args = {"token_lengths": token_lengths, "token_budget": args.token_budget,
                     "epochs": args.epochs} if args.token_budget is not None else {"epochs": args.epochs}
    planned_steps = sum(1 for _ in semantic_schedule(train, args.batch, args.seed,
                        args.epochs * len(train) if args.epochs else args.steps, **schedule_args))
    if planned_steps > args.steps:
        raise ValueError("complete training epochs exceed the frozen update bound")
    (out / "fit-plan.json").write_text(json.dumps({"training_records": len(train), "planned_steps": planned_steps,
        "epochs": args.epochs, "batch": args.batch, "token_budget": args.token_budget,
        "min_tokens": min(token_lengths.values()), "max_tokens": max(token_lengths.values())}, indent=2), encoding="utf-8")
    preflight = []
    model.eval()
    with torch.inference_mode():
        for row in (lengths[0], lengths[len(lengths) // 2], lengths[-1]):
            tensors = batch([row]); start = time.perf_counter()
            first, second = forward(tensors), forward(tensors)
            first = torch.floor(torch.sigmoid(first) * basis.levels + 0.5)
            second = torch.floor(torch.sigmoid(second) * basis.levels + 0.5)
            if not torch.equal(first, second):
                raise ValueError("semantic inference changed its quantized named coordinates")
            preflight.append({"id": row["id"], "tokens": len(encoded[row["id"]][0]),
                              "repeat_equal": True, "two_forwards_seconds": time.perf_counter() - start})
    (out / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    model.train()
    started = time.perf_counter()
    tensors = batch([lengths[-1]])
    loss = semantic_training_loss(forward(tensors), tensors)
    loss.backward()
    gradients = [parameter.grad for parameter in model.parameters() if parameter.grad is not None]
    if not gradients or not all(torch.isfinite(gradient).all() for gradient in gradients):
        raise ValueError("native joint readout has absent or non-finite gradients")
    report = {"id": lengths[-1]["id"], "loss": float(loss.detach()),
              "forward_backward_seconds": time.perf_counter() - started,
              "peak_cuda_bytes": torch.cuda.max_memory_allocated(),
              "trainable_parameters": sum(p.numel() for p in model.parameters() if p.requires_grad),
              "optimizer_updates": 0, "finite_gradients": True}
    (out / "backward-preflight.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    model.zero_grad(set_to_none=True)
    del gradients, loss, tensors
    if args.preflight_only:
        log.close()
        inputs.close()
        return
    params = [{"params": [p for p in model.encoder.parameters() if p.requires_grad] if model.encoder is not None else [], "lr": args.lr},
              {"params": model.head.parameters(), "lr": args.head_lr}]
    optimizer = torch.optim.AdamW(params, weight_decay=0.01)
    scheduler = _schedule(optimizer, 1, planned_steps, 1)
    start_step = 0
    if resumed is not None:
        optimizer.load_state_dict(resumed["optimizer"])
        scheduler.load_state_dict(resumed["scheduler"])
        torch.set_rng_state(resumed["torch_rng"])
        torch.cuda.set_rng_state_all(resumed["cuda_rng"])
        start_step = resumed["step"]

    def checkpoint(step, optimizer):
        checkpoint_dir = out / "checkpoint"
        checkpoint_dir.mkdir(exist_ok=True)
        save_adapter(checkpoint_dir)
        torch.save(model.head.state_dict(), checkpoint_dir / "named-head.pt")
        torch.save({"step": step, "optimizer": optimizer.state_dict(), "torch_rng": torch.get_rng_state(),
                    "cuda_rng": torch.cuda.get_rng_state_all(), "examples_sha256": sha(examples_path),
                    "experiment_sha256": sha(args.experiment_contract), "initial": initial,
                    "seen": sorted(seen), "scheduler": scheduler.state_dict(),
                    "planned_steps": planned_steps}, checkpoint_dir / "training-state.pt")
        record = {"backbone": args.backbone, "revision": args.revision, "basis": basis.record(),
                  "input_format": SEMANTIC_INPUT_FORMAT,
                  "adapter": adapter_name, "seed": args.seed, "step": step,
                  "max_len": args.max_len,
                  "state": "incomplete_unqualified_training_checkpoint", "examples_sha256": sha(examples_path),
                  "experiment_sha256": sha(args.experiment_contract),
                  "frozen_feature_contract_sha256": feature_cache.identity if feature_cache else None}
        record["files"] = {str(p.relative_to(checkpoint_dir)).replace("\\", "/"): sha(p)
                           for p in checkpoint_dir.rglob("*") if p.is_file() and p.name != "semantic-model.json"}
        (checkpoint_dir / "semantic-model.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    started = time.monotonic()
    seen = set(resumed["seen"]) if resumed is not None else set()
    completed_steps = start_step
    logits = loss = None
    model.train()
    for step, selected in enumerate(semantic_schedule(train, args.batch, args.seed, planned_steps, **schedule_args)):
        if step < start_step:
            continue
        if time.monotonic() - started > args.max_seconds:
            checkpoint(step, optimizer)
            log.close()
            inputs.close()
            print("SEMANTIC_CHECKPOINT_SAVED", step, "of", planned_steps, flush=True)
            return
        seen.update(row["id"] for row in selected)
        tensors = batch(selected)
        logits = forward(tensors)
        loss = semantic_training_loss(logits, tensors)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step(); scheduler.step(); optimizer.zero_grad(set_to_none=True)
        completed_steps = step + 1
        if (step + 1) % args.checkpoint_every == 0:
            checkpoint(step + 1, optimizer)
        if step % 8 == 0 or step + 1 == planned_steps:
            record = {"step": step + 1, "loss": loss.item(), "seconds": time.monotonic() - started,
                      "unique_training_records_seen": len(seen), "training_records": len(train)}
            log.write(json.dumps(record) + "\n"); log.flush(); print(json.dumps(record), flush=True)
    final = evaluate("fitted")
    save_adapter(out)
    torch.save(model.head.state_dict(), out / "named-head.pt")
    manifest = {"backbone": args.backbone, "revision": args.revision, "basis": basis.record(),
                "input_format": SEMANTIC_INPUT_FORMAT,
                "max_len": args.max_len,
                "adapter": adapter_name, "contract_sha256": coordinate_contract["contract_sha256"],
                "examples_sha256": sha(examples_path), "experiment_sha256": sha(args.experiment_contract),
                "steps": completed_steps, "planned_steps": planned_steps,
                "unique_training_records_seen": len(seen), "training_records": len(train),
                "seed": args.seed, "initial": initial, "fitted": final,
                "state": "unqualified_candidate_not_promoted",
                "frozen_feature_contract_sha256": feature_cache.identity if feature_cache else None}
    manifest["files"] = {str(p.relative_to(out)).replace("\\", "/"): sha(p) for p in out.rglob("*")
                         if p.is_file() and (p.parent in (out / "adapter", out / "tokenizer") or p == out / "named-head.pt")}
    (out / "semantic-model.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    model.eval()
    reload_rows = [calibration[0], *[inputs[lengths[index]["id"]] for index in (0, len(lengths) // 2, len(lengths) - 1)]]
    references = []
    with torch.inference_mode():
        for row in reload_rows:
            tensors = batch([row])
            references.append(torch.floor(torch.sigmoid(forward(tensors)) * basis.levels + 0.5).cpu())
    del model, optimizer, params, logits, loss
    import gc
    gc.collect(); torch.cuda.empty_cache()
    model = load("semantic", str(out))
    reload_checks = []
    with torch.inference_mode():
        for source, reference in zip(reload_rows, references):
            actual_encoded = semantic_encode(tokenizer, source["input"], source["items"], args.max_len)
            tensors = {key: value.to(next(model.parameters()).device) for key, value in
                       semantic_batch([source], [actual_encoded], tokenizer, coordinates).items()}
            reloaded = torch.floor(torch.sigmoid(forward(tensors)) * basis.levels + 0.5).cpu()
            if not torch.equal(reference, reloaded):
                raise ValueError("saved semantic candidate changed quantized predictions on reload: " + source["id"])
            reload_checks.append({"id": source["id"], "tokens": len(actual_encoded[0]), "quantized_equal": True})
    (out / "reload-check.json").write_text(json.dumps({"passed": True, "quantized_equal": True,
                                                       "records": reload_checks}), encoding="utf-8")
    log.close()
    inputs.close()
    print("SEMANTIC_FIT_SAVED", flush=True)


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
    assert_no_evaluation_sources(rows, "schema-item examples")
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
    assert_no_evaluation_sources(rows, "reading examples")
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
    feature_cache = stages.add_parser("semantic-cache")
    for name in ("examples", "candidate", "tokenizer", "out"):
        feature_cache.add_argument("--" + name, required=True)
    feature_cache.add_argument("--split", choices=("train", "calibration", "all"), default="all")
    feature_cache.add_argument("--shard", default="0/1")
    feature_cache.add_argument("--max-len", type=int, default=131072)
    feature_cache.add_argument("--max-seconds", type=int, default=14400)
    feature_cache.add_argument("--limit", type=int, default=0, help="explicit diagnostic sample only; zero means the complete shard")
    semantic_eval = stages.add_parser("semantic-evaluate")
    for name in ("examples", "candidate", "out"):
        semantic_eval.add_argument("--" + name, required=True)
    semantic_eval.add_argument("--split", choices=("calibration", "test"), default="calibration")
    semantic_eval.add_argument("--role", choices=("noun", "column", "value", "question"))
    semantic_eval.add_argument("--batch", type=int, default=8)
    semantic_eval.add_argument("--max-len", type=int, default=12288)
    semantic_eval.add_argument("--token-budget", type=int, default=16384)
    semantic_eval.add_argument("--feature-cache", help="exact frozen states for the same encoder; no gradient updates")
    qualification = stages.add_parser("semantic-qualify")
    for name in ("examples", "candidate", "evaluation"):
        qualification.add_argument("--" + name, required=True)
    qualification.add_argument("--min-support", type=int, default=20)
    qualification.add_argument("--min-accuracy", type=float, default=0.95)
    qualification.add_argument("--max-mae", type=float, default=0.1)
    semantic = stages.add_parser("semantic")
    for name in ("examples", "coordinates", "experiment-contract", "backbone", "revision", "out"):
        semantic.add_argument("--" + name, required=True)
    semantic.add_argument("--steps", type=int, default=128)
    semantic.add_argument("--batch", type=int, default=1)
    semantic.add_argument("--eval-records", type=int, default=64)
    semantic.add_argument("--lr", type=float, default=2e-5)
    semantic.add_argument("--head-lr", type=float, default=1e-3)
    semantic.add_argument("--lora-rank", type=int, default=16)
    semantic.add_argument("--max-len", type=int, default=8192)
    semantic.add_argument("--max-seconds", type=int, default=5400)
    semantic.add_argument("--seed", type=int, default=3407)
    semantic.add_argument("--preflight-only", action="store_true")
    semantic.add_argument("--checkpoint-every", type=int, default=512)
    semantic.add_argument("--resume-checkpoint", help="verified incomplete checkpoint of this exact frozen experiment")
    semantic.add_argument("--initial-candidate", help="exact immutable frozen encoder/head source for cached fitting")
    semantic.add_argument("--feature-cache", help="lossless frozen pooled states; all data/loss/selection owners are shared")
    semantic.add_argument("--epochs", type=int, help="complete seeded passes over every admitted training record")
    semantic.add_argument("--token-budget", type=int, help="padded batch target; longer complete inputs run alone within --max-len")
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
    {"schema_items": train_schema_items, "readings": train_readings, "semantic": train_semantic,
     "semantic-evaluate": evaluate_semantic_candidate, "semantic-qualify": qualify_semantic_candidate,
     "semantic-cache": cache_semantic_features}[args.stage](args)


if __name__ == "__main__":
    main()
