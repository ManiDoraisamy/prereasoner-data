"""The three reader models and how they read their inputs: one definition that serving and training share.

- SchemaItemModel (stage 1) reads (question, one table) and scores the table's use and each column's use.
- ReadingModel (stage 2) reads (question, the kept tables) and scores every reading the builder uses
  (engine.readers.layout), plus pointers to value and limit spans.
- Rescorer reads (question, one candidate's read-as line and result shape) beside the decoder's own view of the
  candidate, and scores how likely the candidate answers the question.

Each is an XLM-R encoder with linear heads. Serving builds the encoder from its saved config and loads the
fine-tuned weights (``load``), so no base weights are fetched at serving time. The weights stay in bfloat16, half
the memory of float32, and serving computes in float32 on every device, as training reads its runs
(training.readers.outputs): each linear layer's weight is widened as it is used, and the word-embedding table, a
lookup, is widened per looked-up row. A CPU without native bfloat16 emulates it: on Cloud Run's AMD EPYC 7B13 (AVX2)
one large reader's pass over 4 x 256 tokens took 15.3 s in bfloat16 and 2.7 s in float32 (2026-10-27); widening as
used costs 2 to 6% over float32 weights, and float32 weights would need 4 GiB more for the bundle. bfloat16 was
faster only on CPUs that have it (0.9 s against 2.3 s on an AVX512-BF16 desktop). ``load`` places a model on the
serving device (engine.config.serving_device: the CPU, or the hosted edition's GPU); the reading functions run a
model on the device it is on and return its outputs on the CPU.
"""
from __future__ import annotations

import math
import os
import json

import torch
from torch import nn

from engine.readers.layout import AGGREGATES, GLOBAL_WIDTH, LAYOUT, PER_BLOCK, SLOTS

ROLES = ["select", "filter", "group", "order", "having"]
POINTER = 128
MAX_LEN = 512
RESCORER_LEN = 160
RESCORER_FEATURES = 3
SEMANTIC_INPUT_FORMAT = "joint_source_spans_preview96_ordinals/1"
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


def semantic_bounds(groups):
    """Contiguous input-token intervals; no quadratic token-index expansion."""
    count = max(map(len, groups))
    bounds = torch.zeros((len(groups), count, 2), dtype=torch.long)
    valid = torch.zeros((len(groups), count), dtype=torch.bool)
    for b, spans in enumerate(groups):
        for i, span in enumerate(spans):
            if not len(span) or span[-1] - span[0] + 1 != len(span):
                raise ValueError("named spans must be nonempty contiguous token intervals")
            bounds[b, i] = torch.tensor((span[0], span[-1] + 1))
            valid[b, i] = True
    return bounds, valid


def _pool_bounds(states, bounds, valid):
    """Interval means in float64 before the one FP32 named projection."""
    prefix = torch.cat((states.new_zeros((states.shape[0], 1, states.shape[-1]), dtype=torch.float64),
                        states.to(torch.float64).cumsum(1)), dim=1)
    width = states.shape[-1]
    start = prefix.gather(1, bounds[:, :, 0].unsqueeze(-1).expand(-1, -1, width))
    stop = prefix.gather(1, bounds[:, :, 1].unsqueeze(-1).expand(-1, -1, width))
    result = (stop - start) / (bounds[:, :, 1] - bounds[:, :, 0]).clamp_min(1).unsqueeze(-1)
    return result.to(states.dtype) * valid.unsqueeze(-1)


class NamedSemanticHead(nn.Module):
    """One shared projection into an explicit semantic basis for all input roles.

    Accepts pooled backbone states (including CLEF's prefill states); it cannot
    choose a SQL field or emit a residual choice score. The only inference export
    is BindingProfile. The basis pins coordinate meaning/qualification/calibration;
    adding this module does not qualify untrained coordinates or promote a model.
    """

    def __init__(self, input_dim, basis):
        super().__init__()
        from engine.schema_decode import BindingBasis

        if not isinstance(basis, BindingBasis) or type(input_dim) is not int or input_dim < 1:
            raise ValueError("named head needs an explicit input width and binding basis")
        self.basis = basis
        self.projection = nn.Linear(input_dim, len(basis.coordinates))

    def forward(self, states):
        if states.ndim != 2 or states.shape[-1] != self.projection.in_features:
            raise ValueError("named head expects one pooled backbone state per contextual item")
        # Keep the semantic readout's computation in FP32 even when a backbone or
        # saved weights use BF16. No lexical/residual score is exported beside it.
        return nn.functional.linear(states.float(), self.projection.weight.float(), self.projection.bias.float())

    @torch.inference_mode()
    def profiles(self, states, *, identities, kinds, evidence_kinds, relationships=None,
                 datatypes=None, evidence_mask=None):
        return self.profiles_from_logits(self(states), identities=identities, kinds=kinds,
                                        evidence_kinds=evidence_kinds, relationships=relationships,
                                        datatypes=datatypes, evidence_mask=evidence_mask)

    @torch.inference_mode()
    def profiles_from_logits(self, logits, *, identities, kinds, evidence_kinds, relationships=None,
                             datatypes=None, evidence_mask=None):
        """Export already computed named logits; never project them a second time."""
        from engine.schema_decode import BindingProfile

        if logits.ndim != 2 or logits.shape[-1] != len(self.basis.coordinates):
            raise ValueError("named logit export must cover exactly the pinned coordinates")
        count = len(logits)
        relationships = [""] * count if relationships is None else relationships
        datatypes = [""] * count if datatypes is None else datatypes
        if any(len(values) != count for values in (identities, kinds, evidence_kinds, relationships, datatypes)):
            raise ValueError("every named profile needs identity, input role and evidence provenance")
        if any(kind != "expected_concept" for kind in evidence_kinds):
            raise ValueError("the shared meaning head cannot certify observed instance facts; use verified source evidence")
        if not torch.isfinite(logits).all():
            raise ValueError("non-finite named semantic prediction")
        if evidence_mask is None:
            evidence_mask = torch.ones_like(logits, dtype=torch.bool)
        if evidence_mask.shape != logits.shape or evidence_mask.dtype != torch.bool:
            raise ValueError("semantic evidence mask must be an explicit per-item/per-coordinate boolean matrix")
        values = torch.floor(torch.sigmoid(logits) * self.basis.levels + 0.5).to(torch.int64).cpu().tolist()
        visible = evidence_mask.cpu().tolist()
        basis_id = self.basis.identity
        return tuple(BindingProfile(
            basis_id, identities[i], kinds[i],
            tuple(value if qualified and observed else None for value, qualified, observed
                  in zip(values[i], self.basis.qualified_for(kinds[i]), visible[i])),
            evidence_kinds[i], relationships[i], datatypes[i],
        ) for i in range(count))


class NamedSemanticModel(nn.Module):
    """One backbone and one shared named head, with no independent choice head.

    The encoder must return prefill hidden states; autoregressive generation and
    the CLEF lexical/residual choice head are not called. Qualified integer
    profiles, typed identities and scope pointers are its downstream interface.
    This candidate is not selected by the promoted readers loader.
    """

    def __init__(self, encoder, basis, input_dim=None, *, clef_prefill=False):
        super().__init__()
        self.encoder = encoder
        self.is_clef = clef_prefill
        self.head = NamedSemanticHead(input_dim or encoder.config.hidden_size, basis)

    def train(self, mode=True):
        super().train(mode)
        # A frozen featurizer must retain inference kernels/dropout while the
        # sole named projection learns. Trainable adapters retain train mode.
        if self.encoder is not None and not any(p.requires_grad for p in self.encoder.parameters()):
            self.encoder.eval()
        return self

    def pooled_states(self, ids, mask, item_index, item_mask):
        if self.is_clef:
            backbone = self.encoder.get_base_model() if hasattr(self.encoder, "get_base_model") else self.encoder
            text_model = getattr(backbone, "model", None) or backbone.base_model
            text_model = getattr(text_model, "language_model", text_model)
            states = text_model(input_ids=ids, attention_mask=mask, use_cache=False,
                                return_dict=True).last_hidden_state.float()
        else:
            states = _states(self.encoder, ids, mask)
        return _pool_bounds(states, item_index, item_mask) if item_mask.ndim == 2 else _pool(states, item_index, item_mask)

    def pair_logits(self, pooled, queries, components, component_mask, interactions):
        """The same named projection over explicit position/candidate pairs.

        Products are computed before the one 215-coordinate semantic head.
        There is no lexical score, residual pointer score or choice projection.
        Static property examples use their ordinary unpaired pooled states.
        """
        width = pooled.shape[-1]
        left = pooled.gather(1, queries.unsqueeze(-1).expand(-1, -1, width))
        flat = components.reshape(components.shape[0], -1)
        right = pooled.gather(1, flat.unsqueeze(-1).expand(-1, -1, width)).reshape(*components.shape, width)
        weights = component_mask.unsqueeze(-1).to(right.dtype)
        right = (right * weights).sum(-2) / weights.sum(-2).clamp_min(1)
        features = torch.where(interactions.unsqueeze(-1), left * right, left)
        return self.head(features.reshape(-1, width)).reshape(*features.shape[:2], -1)

    def forward(self, ids, mask, item_index, item_mask, pair_queries=None, pair_components=None,
                pair_component_mask=None, pair_interactions=None):
        if self.encoder is not None and not any(p.requires_grad for p in self.encoder.parameters()):
            # PEFT can retain an input-requires-grad hook after freezing weights.
            # Match serving's inference kernels and prevent a full encoder tape.
            with torch.inference_mode():
                pooled = self.pooled_states(ids, mask, item_index, item_mask)
            # A normal tensor is needed for the trainable head to save inputs.
            pooled = pooled.clone()
        else:
            pooled = self.pooled_states(ids, mask, item_index, item_mask)
        if pair_queries is not None:
            return self.pair_logits(pooled, pair_queries, pair_components, pair_component_mask, pair_interactions)
        return self.head(pooled.reshape(-1, pooled.shape[-1])).reshape(*pooled.shape[:2], -1)

    @torch.inference_mode()
    def read(self, tokenizer, state, items, *, max_len=32768, relationships=None, datatypes=None):
        """The inference consumer of the same input-only bank and shared head.

        No gold, field-choice logit or independent pointer score enters this
        interface. Qualification remains enforced at export and decoding.
        """
        if self.training:
            raise ValueError("named semantic inference requires evaluation mode")
        encoded, spans = semantic_encode(tokenizer, state, items, max_len)
        ids, mask = _ids_tensor(tokenizer, [encoded])
        index, item_mask = semantic_bounds([spans])
        device = next(self.parameters()).device
        logits = self(ids.to(device), mask.to(device), index.to(device), item_mask.to(device))[0]
        return self.head.profiles_from_logits(logits, identities=[i["identity"] for i in items],
            kinds=["value" if i["kind"] == "span" else i["kind"] for i in items], evidence_kinds=["expected_concept"] * len(items),
            relationships=relationships, datatypes=datatypes)

    @torch.inference_mode()
    def read_query(self, tokenizer, turns, graph, policy, *, max_len=32768, max_decisions=256):
        """One encoder prefill; every grammar choice crosses the named head.

        Position and candidate states are encoded together without a target or
        predicted AST prefix. The grammar reads paired named profiles from that
        fixed prefill. It alone supplies legal scope/identity mappings.
        """
        import hashlib
        from engine import request_timing
        from engine.readers.grammar import ASTGrammar
        from engine.readers.decisions import named_field_choice, named_propositions, InvalidDecision
        from engine.readers.ace import encode_query, decode_query
        from engine.sql_ast import query_to_tree

        if self.training:
            raise ValueError("named query inference requires evaluation mode")
        controller = ASTGrammar(turns, graph, max_decisions=max_decisions)
        state, items = controller.bank(max_decisions)
        encoded, spans = semantic_encode(tokenizer, state, items, max_len)
        ids, mask = _ids_tensor(tokenizer, [encoded])
        index, item_mask = semantic_bounds([spans])
        device = next(self.parameters()).device
        with request_timing.span("named_readout_prefill"):
            pooled = self.pooled_states(ids.to(device), mask.to(device), index.to(device), item_mask.to(device))
        fingerprint = hashlib.sha256(json.dumps({"input": state, "items": items}, sort_keys=True,
                                                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        trace = {"grammar_sha256": controller.schema_sha256, "basis": self.head.basis.record(),
                 "fields": [], "prefill_calls": 1, "hidden_choice_channels": 0,
                 "encoder_input_sha256": fingerprint, "positions": max_decisions,
                 "candidate_items": len(controller._bank_index), "input_tokens": len(encoded),
                 "readout": "parallel_positions_shared_named_pair_head"}

        def predict(query_index, request, components):
            count = len(request)
            width = max(map(len, components.values()))
            component_ids = torch.zeros((1, count, width), dtype=torch.long, device=device)
            component_mask = torch.zeros_like(component_ids, dtype=torch.bool)
            for i, key in enumerate(request):
                part = components[key]
                component_ids[0, i, :len(part)] = torch.tensor(part, device=device)
                component_mask[0, i, :len(part)] = True
            queries = torch.full((1, count), query_index, dtype=torch.long, device=device)
            with request_timing.span("named_readout_projection"):
                logits = self.pair_logits(pooled, queries, component_ids, component_mask,
                                         torch.ones_like(queries, dtype=torch.bool))[0]
            observed = torch.zeros_like(logits, dtype=torch.bool)
            for i, descriptor in enumerate(request.values()):
                observed[i, self.head.basis.coordinates.index(descriptor["coordinate"])] = True
            profiles = self.head.profiles_from_logits(logits,
                identities=[r["item"]["identity"] for r in request.values()], kinds=["frame"] * count,
                evidence_kinds=["expected_concept"] * count, evidence_mask=observed)
            return named_field_choice(self.head.basis, {profile.identity: profile for profile in profiles}, request, policy)

        action_options = {"answer": "answer", "clarify": "clarify"}
        request = named_propositions("action", action_options, {key: "dialogue." + key for key in action_options})
        action_components = {key: [controller._bank_index[json.dumps(value)]] for key, value in action_options.items()}
        trace["action"] = predict(controller._action_index, request, action_components)
        if trace["action"]["choice"] != "answer":
            trace["final_action"] = "clarify"
            return {"action": "clarify", "query": None, "named_trace": trace, "reason": trace["action"]["reason"]}

        def choose(record):
            position = record["position"]
            if position >= len(controller._position_indices):
                raise InvalidDecision("query exceeds the trained parallel position bound")
            components = {key: controller.components(record, value) for key, value in record["options"].items()}
            decision = predict(controller._position_indices[position], record["request"], components)
            trace["fields"].append({"path": record["path"], "kind": record["kind"], "position": position,
                                    "options": record["options"], "components": components, "decision": decision})
            return decision["choice"]

        try:
            query = controller.run(choose)
            text, tree = encode_query(query)
            recovered = decode_query(tree)
            if query_to_tree(recovered) != query_to_tree(query):
                raise InvalidDecision("recursive ACE differs from the constructed SQL AST")
        except (ValueError, RuntimeError, KeyError, TypeError) as exc:
            trace["final_action"] = "clarify"
            trace["readout_failure"] = {"type": type(exc).__name__, "message": str(exc)}
            return {"action": "clarify", "query": None, "named_trace": trace,
                    "reason": f"{type(exc).__name__}: {exc}"}
        trace["model_ast"] = query_to_tree(query)
        trace["final_action"] = "answer"
        trace["ace_tree_sha256"] = hashlib.sha256(json.dumps(tree, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"action": "answer", "query": query, "ace": text, "ace_tree": tree, "named_trace": trace}


def clef_semantic_model(basis, *, backbone, revision, lora_rank=16, adapter=None, seed=3407, trainable_adapter=False):
    """The reader model owner's CLEF construction for fitting and candidate reload.

    Discard the stock choice head: its lexical/residual decision channels cannot
    cross the named bottleneck. Keep the pinned pretrained backbone, optionally
    with its one LoRA adapter. No second semantic score is retained.
    """
    from unsloth import FastDecisionModel

    native, processor = FastDecisionModel.from_pretrained(
        backbone, revision=revision, load_in_4bit=True, dtype=torch.bfloat16, random_state=seed)
    # Featurization, native fitting and cold reload share the same FP32 policy.
    # Library/global defaults must not change the frozen encoder's states.
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    if adapter is not None:
        from peft import PeftModel
        native.encoder = PeftModel.from_pretrained(native.encoder, adapter, is_trainable=trainable_adapter)
    elif lora_rank:
        native = FastDecisionModel.get_peft_model(native, r=lora_rank, lora_alpha=lora_rank * 2,
                                                  lora_dropout=0, random_state=seed)
    else:
        native.encoder.requires_grad_(False)
    config = getattr(native.encoder.config, "text_config", native.encoder.config)
    model = NamedSemanticModel(native.encoder, basis, config.hidden_size, clef_prefill=True)
    # No reference to the discarded native head or LM output weight is exposed
    # to this model's forward pass or the deterministic downstream decoder.
    model.head.to(next(model.encoder.parameters()).device)
    return model, processor.tokenizer if hasattr(processor, "tokenizer") else processor


def semantic_encode(tokenizer, state, items, max_len=32768):
    """One complete prefill: schema/static profiles, then conversation/query profiles.

    On CLEF's causal backbone static item states cannot see the conversation.
    Their input contains only schema/data evidence, so a changed question cannot
    covertly turn a column's meaning into a chosen-column score. Query noun and
    operation slots follow the whole conversation and schema. Never truncate.
    Item order in the returned spans is the caller's original identity order.
    """
    if not isinstance(state, dict) or set(state) != {"schema", "conversation"}:
        raise ValueError("semantic state requires only schema and conversation")
    if not isinstance(state["schema"], list) or not isinstance(state["conversation"], list):
        raise ValueError("semantic schema and conversation must be lists")
    if not items or len({i["identity"] for i in items}) != len(items):
        raise ValueError("semantic items require unique physical identities")
    joint_bank = any(isinstance(part, dict) and "grammar" in part for part in state["schema"])
    ids, spans = [], [None] * len(items)
    source_spans, column_spans = {}, {}

    def source_description(value):
        # A schema sample is an explicit bounded observation, not the executor's
        # cell value. Preserve complete originals in state/source hashes. Never
        # feed opaque content hashes to the model as memorization features.
        if isinstance(value, str) and len(value) > 256:
            import re
            if re.fullmatch(r"0[xX][0-9a-fA-F]+", value):
                return {"source_form": "hexadecimal byte text", "characters": len(value)}
            if value[:2] in ("b'", 'b"') and value[-1:] == value[1:2]:
                return {"source_form": "byte-literal text", "characters": len(value)}
            return {"source_text_preview": value[:96], "characters": len(value)}
        return value

    def add(text):
        start = len(ids)
        ids.extend(tokenizer(text, add_special_tokens=False).input_ids)
        return list(range(start, len(ids)))

    def encode_item(i, item):
        if (set(item) != {"identity", "kind", "text"} or not isinstance(item["identity"], str)
                or not item["identity"] or not isinstance(item["text"], str) or not item["text"].strip()):
            raise ValueError("semantic items accept input descriptions, never labels or hidden targets")
        # Physical IDs are a return map, not lexical features that can memorize
        # row numbers or target identities. Meaning comes from the description.
        description = item["text"]
        if joint_bank and item["kind"] == "column":
            prefix = "Source column: "
            if not description.startswith(prefix):
                raise ValueError("joint source column must declare its physical reference")
            ref = json.loads(description[len(prefix):])
            spans[i] = column_spans[(ref["table"], ref["name"], ref["type"])]
            return
        if joint_bank and item["kind"] == "frame" and description.startswith("Candidate: "):
            value = json.loads(description[len("Candidate: "):])
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in source_spans:
                spans[i] = source_spans[key]
                return
            description = json.dumps(source_description(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            add("\nCANDIDATE ")
        elif joint_bank and item["kind"] == "control" and description.startswith("Requested AST decision number "):
            number = description[len("Requested AST decision number "):].removesuffix(".")
            if not number.isdigit(): raise ValueError("AST positions require explicit ordinal numbers")
            add("\nAST POSITION ")
            description = number
        else:
            add("\nINPUT " + item["kind"] + ": ")
        spans[i] = add(description)
        if not spans[i]:
            raise ValueError("semantic item produced an empty token span")

    static = {"column", "value", "table"}
    dynamic = {"noun", "question", "operation", "frame", "control"}
    if any(item["kind"] not in static | dynamic | {"span"} for item in items):
        raise ValueError("unknown semantic input role")
    add("<|im_start|>system\nRead the input through the named semantic interface.<|im_end|>\n")
    add("<|im_start|>user\nSCHEMA AND DATA EVIDENCE:\n")
    if joint_bank:
        add("Long source samples are explicit previews with their complete character lengths; complete_value_retained_in_source.\n")
    if joint_bank:
        columns = [part for part in state["schema"] if isinstance(part, dict)
                   and {"table", "column", "type", "samples"} <= set(part)]
        extras = [part for part in state["schema"] if not isinstance(part, dict)
                  or not {"table", "column", "type", "samples"} <= set(part)]
        add(json.dumps(extras, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        for column in columns:
            add("\nSOURCE COLUMN ")
            start = len(ids)
            description = {key: value for key, value in column.items() if key != "samples"}
            prefix = json.dumps(description, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            add(prefix[:-1] + ',"samples":[')
            for j, value in enumerate(column["samples"]):
                if j: add(",")
                key = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                positions = add(json.dumps(source_description(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                source_spans[key] = range(positions[0], positions[-1] + 1)
            add("]}")
            column_spans[(column["table"], column["column"], column["type"])] = range(start, len(ids))
    else:
        add(json.dumps(state["schema"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    for i, item in enumerate(items):
        if item["kind"] in static:
            encode_item(i, item)
    add("\nCONVERSATION:\n")
    for turn in state["conversation"]:
        if (not isinstance(turn, dict) or set(turn) != {"role", "content"}
                or turn["role"] not in ("user", "assistant") or not isinstance(turn["content"], str)):
            raise ValueError("semantic conversation accepts explicit text turns only")
    if any(item["kind"] == "span" for item in items):
        offsets = {}
        for t, turn in enumerate(state["conversation"]):
            add("\nROLE " + turn["role"] + ":\n")
            encoded = tokenizer(turn["content"], add_special_tokens=False, return_offsets_mapping=True)
            if not hasattr(encoded, "offset_mapping"):
                raise ValueError("question spans require a tokenizer with actual character offsets")
            start = len(ids)
            ids.extend(encoded.input_ids)
            offsets[t] = [(start + i, a, b) for i, (a, b) in enumerate(encoded.offset_mapping)]
            add("\nEND TURN\n")
        for i, item in enumerate(items):
            if item["kind"] != "span": continue
            if set(item) != {"identity", "kind", "text", "turn", "start", "stop"}:
                raise ValueError("question spans accept only original source offsets")
            t, a, b = item["turn"], item["start"], item["stop"]
            if (any(type(v) is not int for v in (t, a, b)) or not 0 <= t < len(state["conversation"])
                    or not 0 <= a < b <= len(state["conversation"][t]["content"])
                    or state["conversation"][t]["content"][a:b] != item["text"]):
                raise ValueError("question span differs from its original input")
            positions = [position for position, begin, stop in offsets[t] if stop > a and begin < b]
            if not positions: raise ValueError("question span has no original token evidence")
            spans[i] = range(positions[0], positions[-1] + 1)
    else:
        add(json.dumps(state["conversation"], ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    for i, item in enumerate(items):
        if item["kind"] in dynamic:
            encode_item(i, item)
    add("<|im_end|>\n")
    if type(max_len) is not int or max_len < 1 or len(ids) > max_len:
        raise ValueError("semantic input exceeds its complete-evidence token budget")
    return ids, spans


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


class _Widened(nn.Module):
    """A weight kept in bfloat16 and read in float32 (``load``)."""

    def forward(self, weight):
        return weight.float()


def load(kind, directory, *, encoder=True):
    """The model of ``kind`` saved in ``directory``, for serving: its encoder built from the saved config in bfloat16,
    the saved weights (either precision) copied in, and every computation in float32: each linear layer's bfloat16
    weight widened as it is used, the word-embedding table's rows as they are looked up, and the small weights
    (biases, norms, position embeddings, the heads) held in float32."""
    if kind == "semantic":
        import hashlib
        from pathlib import Path
        from engine.schema_decode import BindingBasis

        with open(os.path.join(directory, "semantic-model.json"), encoding="utf-8") as stream:
            record = json.load(stream)
        if record.get("input_format", SEMANTIC_INPUT_FORMAT) != SEMANTIC_INPUT_FORMAT:
            raise ValueError("semantic model uses another encoder input format")
        root = Path(directory).resolve()
        for name, expected in record["files"].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("semantic candidate artifact is missing or outside its bundle")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("semantic candidate artifact hash mismatch: " + name)
        if "named-head.pt" not in record["files"]:
            raise ValueError("semantic candidate head is not pinned by its file manifest")
        basis = record["basis"]
        qualification_path = root / "qualification.json"
        if qualification_path.exists():
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            if (qualification["candidate_metadata_sha256"] != hashlib.sha256((root / "semantic-model.json").read_bytes()).hexdigest()
                    or qualification["examples_sha256"] != record["examples_sha256"]):
                raise ValueError("qualification differs from the candidate artifact/data")
            basis = qualification["basis"]
        basis = BindingBasis.from_record(basis)
        head_state = torch.load(os.path.join(directory, "named-head.pt"), map_location="cpu", weights_only=True)
        if encoder:
            model, _ = clef_semantic_model(basis, backbone=record["backbone"], revision=record["revision"],
                                           lora_rank=0, adapter=os.path.join(directory, record["adapter"])
                                           if record.get("adapter") else None, seed=record.get("seed", 3407))
        else:
            # Offline fitting/evaluation of the same named head over manifested
            # frozen encoder features. This object cannot perform a prefill.
            model = NamedSemanticModel(None, basis, input_dim=head_state["projection.weight"].shape[1])
        model.head.load_state_dict(head_state)
        return model.eval()
    if not encoder:
        raise ValueError("head-only loading requires a semantic frozen-feature artifact")
    from torch.nn.utils import parametrize
    from transformers import AutoConfig, AutoModel
    # Built in bfloat16 rather than cast: a float32 build of XLM-R large holds 2 GB more until it is freed.
    encoder = AutoModel.from_config(AutoConfig.from_pretrained(directory), dtype=torch.bfloat16)
    model = {"schema_items": SchemaItemModel, "readings": ReadingModel, "rescorer": Rescorer}[kind](encoder)
    model.load_state_dict(torch.load(os.path.join(directory, "model.pt"), map_location="cpu", weights_only=True))
    table = encoder.get_input_embeddings()
    for module in list(encoder.modules()):
        if isinstance(module, nn.Linear):
            parametrize.register_parametrization(module, "weight", _Widened(), unsafe=True)
    for name, parameter in model.named_parameters():
        if parameter is not table.weight and ".parametrizations." not in name:
            parameter.data = parameter.data.float()
    table.register_forward_hook(lambda module, inputs, output: output.float())
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
