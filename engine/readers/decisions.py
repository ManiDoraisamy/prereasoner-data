"""Joint decision inputs and checked readings, shared by training and inference.

This module contains no model, SQL parser, or executor. A decision model selects
input-derived options; the existing readings builder owns the resulting AST.
The ACE view is a bounded application vocabulary using APE's actual syntax.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field as dataclass_field
from functools import lru_cache
from fractions import Fraction
import json
import heapq
import math
import re

from engine.readers.layout import AGGREGATES, HAVING_OPERATORS, OPERATORS, STRUCTURES
from engine.readers.readings import SUBQUERY, as_built, readings_of
from engine.readers.text import Schema
from engine.sql_ast import DATE_PARTS

BOUNDS = {"select": 4, "where": 4, "group": 2, "having": 2, "order": 2}
NONE = "unused"
MAX_TURNS = 32
MAX_TURN_CHARS = 16000


@dataclass(frozen=True)
class EncodedQuestion:
    question_id: str
    question_type: int
    question_span: tuple[int, int]
    option_spans: tuple[tuple[int, int], ...]
    option_ids: tuple[str, ...]


@dataclass(frozen=True)
class EncodedRecord:
    input_ids: tuple[int, ...]
    questions: tuple[EncodedQuestion, ...]
    record_id: str
    media: dict | None = dataclass_field(default=None, compare=False, repr=False)


def encode_record(tokenizer, record, max_length=32768, processor=None):
    """One prefill with a shared option bank for CLEF's existing joint head.

    Repeated options reuse bank spans after the complete state. This changes the
    input serialization and requires its own controlled fine-tuning experiment;
    it is not claimed to be an equivalent stock-CLEF optimization. No state or
    option is ever silently truncated. Media are outside this text-only profile.
    """
    if record.get("images") or record.get("videos"):
        raise InvalidDecision("this decision profile accepts text only")
    if not isinstance(record.get("questions"), dict) or not record["questions"]:
        raise InvalidDecision("a decision request needs nonempty questions")
    ids = []
    token_cache = {}

    def add(text):
        start = len(ids)
        if text not in token_cache:
            token_cache[text] = tuple(tokenizer(text, add_special_tokens=False).input_ids)
        ids.extend(token_cache[text])
        return start, len(ids)

    add("<|im_start|>system\nResolve the conversation into the supplied typed query decisions. "
        "Read all schema evidence. Return decisions through the joint head. "
        "A shared option bank supplies each field's allowed options.<|im_end|>\n<|im_start|>user\nSTATE:\n")
    spans = {}

    def state(value):
        # Keep all evidence, but do not reuse a scalar's state span as an
        # option embedding. A causal backbone at a column name cannot read its
        # following type/samples, nor the rest of the schema. Every option must
        # be encoded after the complete state; fields still share bank spans.
        if isinstance(value, dict):
            add("{")
            for i, key in enumerate(sorted(value)):
                if i:
                    add(",")
                add(_key(key) + ":")
                state(value[key])
            add("}")
        elif isinstance(value, list):
            add("[")
            for i, item in enumerate(value):
                if i:
                    add(",")
                state(item)
            add("]")
        else:
            rendered = _key(value)
            add(rendered)

    state(record["state"])
    add("\nSHARED OPTIONS:\n")
    banks = {}
    for question in record["questions"].values():
        if question["type"] != "choice":
            raise InvalidDecision("the query profile uses explicit choices only")
        if not isinstance(question.get("instructions"), str) or not question["instructions"]:
            raise InvalidDecision("a decision field needs instructions")
        if not isinstance(question.get("criteria"), dict) or not question["criteria"]:
            raise InvalidDecision("a decision field needs allowed options")
        bank_id = id(question["criteria"])
        if bank_id in banks:
            continue
        options = tuple(sorted(question["criteria"].items()))
        banks[bank_id] = options
        for key, description in options:
            if not isinstance(key, str) or not isinstance(description, str) or not description:
                raise InvalidDecision("option identifiers and meanings must be nonempty strings")
            # IDs belong to the external readout map, not the language input.
            # The same typed meaning reuses one span even under another ID.
            semantic = _key(description)
            if semantic not in spans:
                spans[semantic] = add(description if isinstance(description, str) else semantic)
                add("\n")
    add("\nSCHEMA FIELDS:\n")
    questions = []
    layouts = {}
    for name, question in record["questions"].items():
        add(f"FIELD {name}\nINSTRUCTION: ")
        span = add(question["instructions"])
        bank_id = id(question["criteria"])
        if bank_id not in layouts:
            options = banks[bank_id]
            layouts[bank_id] = (tuple(spans[_key(d)] for _, d in options), tuple(k for k, _ in options))
        option_spans, option_ids = layouts[bank_id]
        questions.append(EncodedQuestion(str(name), 1, span,
                         option_spans, option_ids))
        add("\nEND FIELD\n")
    add("<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\nJOINT SCHEMA DECISIONS:")
    if len(ids) > max_length:
        raise InvalidDecision(f"complete request requires {len(ids)} tokens; limit is {max_length}")
    return EncodedRecord(tuple(ids), tuple(questions), str(record.get("id", "unknown")))


class InvalidDecision(ValueError):
    """An ungrounded, incomplete, inconsistent, or unsupported interpretation."""


@dataclass(frozen=True)
class NamedDecisionPolicy:
    """Exact confidence gates for finite named operation decisions."""

    min_score: tuple[int, int]
    min_margin: tuple[int, int]

    def __post_init__(self):
        for pair in (self.min_score, self.min_margin):
            if (not isinstance(pair, tuple) or len(pair) != 2 or
                    any(type(v) is not int for v in pair) or pair[1] <= 0 or
                    not 0 <= Fraction(*pair) <= 1):
                raise ValueError("named decision gates require exact fractions in [0,1]")

    def record(self):
        return {"min_score": list(self.min_score), "min_margin": list(self.min_margin)}


def named_operation_meanings(node, field, contract=None):
    """One mapping from executable enum/predicate meanings to named axes."""
    from engine.readers.layout import named_coordinate_contract

    contract = named_coordinate_contract() if contract is None else contract
    owner, attribute = ("Comparison", "operator") if (node, field) == ("Predicate", "operation") else (node, field)
    meanings = {value: row["name"] for row in contract["coordinates"] if row.get("selector") and
                row["selector"]["node"] == owner and row["selector"]["field"] == attribute
                for value in row["selector"]["values"]}
    if (node, field) == ("Predicate", "operation"):
        facets = {row.get("facet"): row["name"] for row in contract["coordinates"] if row.get("facet")}
        meanings.update({value: facets[facet] for value, facet in
                         (("IN", "in"), ("NOT IN", "not_in"), ("EXISTS", "exists"), ("NOT EXISTS", "not_exists"))})
    if not meanings:
        raise InvalidDecision("operation field has no named semantic contract")
    return meanings


def named_operator_requests():
    """Input-only operator bank for the existing bounded readings profile.

    Every supported slot exists before a query is labelled. This bank does not
    claim to supply column/value pointers or a recursive AST grammar decoder.
    """
    result = {}

    def add(identity, node, field, options, instruction, allow_zero=False, zero_value=None):
        result[identity] = {"node": node, "field": field, "options": {f"o{i}": v for i, v in enumerate(options)},
                            "allow_zero": allow_zero, "zero_value": zero_value,
                            "item": {"identity": identity, "kind": "operation", "text": instruction}}

    for block in range(2):
        add(f"b{block}.conj", "BooleanExpr", "operator", ("AND", "OR"), f"Conjunction of conditions in query block {block}.")
        add(f"b{block}.dir", "OrderTerm", "direction", (None, "ASC", "DESC"),
            f"Sort direction in query block {block}; absent if it is unordered.", True, None)
        for kind, count in BOUNDS.items():
            for slot in range(count):
                stem = f"b{block}.{kind}{slot}"
                if kind in ("select", "having", "order"):
                    add(stem + ".aggregate", "Aggregate", "function", AGGREGATES,
                        f"Aggregate of {kind} operand {slot} in query block {block}; plain if unaggregated.", True, "plain")
                if kind in ("where", "having"):
                    add(stem + ".operator", "Predicate", "operation", OPERATORS if kind == "where" else HAVING_OPERATORS,
                        f"Comparison of {kind} condition {slot} in query block {block}.")
    return result


def named_operator_choice(basis, profile, options, policy, *, node, field, allow_zero=False, zero_value=None):
    """Read one AST enum from qualified coordinates, without a choice-score head.

    The vocabulary owner defines which coordinates mean each enum value. Every
    required coordinate must be known/qualified for this item's role. A declared
    zero option means none of those operations (e.g. a plain, unaggregated field).
    Aliases with identical coordinate meanings share one candidate, not a false
    ambiguity. All score terms and the exact gates are retained for replay.
    """
    from engine.readers.layout import named_coordinate_contract
    from engine.schema_decode import BindingBasis, BindingProfile

    if not isinstance(basis, BindingBasis) or not isinstance(profile, BindingProfile) or not isinstance(policy, NamedDecisionPolicy):
        raise InvalidDecision("named operation requires the declared basis/profile/policy")
    if (profile.basis_id != basis.identity or len(profile.values) != len(basis.coordinates) or
            any(v is not None and not 0 <= v <= basis.levels for v in profile.values)):
        raise InvalidDecision("named operation profile has another basis or invalid quantization")
    if (not isinstance(options, dict) or not options or any(not isinstance(k, str) for k in options)
            or type(allow_zero) is not bool):
        raise InvalidDecision("named operation requires explicit nonempty option identities")
    contract = named_coordinate_contract()
    meanings = named_operation_meanings(node, field, contract)
    if any(name not in basis.coordinates for name in meanings.values()):
        raise InvalidDecision("AST enum is not covered by the pinned named basis")
    coordinates = sorted({meanings[value] for value in options.values()
                          if not (allow_zero and _key(value) == _key(zero_value)) and isinstance(value, str) and value in meanings})
    if not coordinates:
        raise InvalidDecision("a named operation needs at least one nonzero alternative")
    groups = {}
    for identity, value in sorted(options.items()):
        zero = allow_zero and _key(value) == _key(zero_value)
        if not zero and (not isinstance(value, str) or value not in meanings):
            raise InvalidDecision("option is not an enum value or declared zero operation")
        vector = tuple(int(not zero and meanings[value] == c) * basis.levels for c in coordinates)
        groups.setdefault(vector, []).append(identity)
    mask = basis.qualified_for(profile.kind)
    indices = [basis.coordinates.index(c) for c in coordinates]
    available = all(mask[i] and profile.values[i] is not None and basis.weights[i] > 0 for i in indices)
    denominator = sum(basis.weights[i] * basis.levels for i in indices)
    candidates = []
    for vector, identities in groups.items():
        terms = []
        for c, index, target in zip(coordinates, indices, vector):
            value = profile.values[index]
            qualified = mask[index] and basis.weights[index] > 0
            contribution = basis.weights[index] * (basis.levels - abs(value - target)) if qualified and value is not None else 0
            terms.append({"coordinate": c, "value": value, "prototype": target, "weight": basis.weights[index],
                          "qualified": qualified, "contribution": contribution})
        numerator = sum(t["contribution"] for t in terms)
        score = Fraction(numerator, denominator) if denominator else Fraction(0)
        candidates.append({"options": identities, "numerator": numerator, "normalizer": denominator,
                           "score": [score.numerator, score.denominator], "contributions": terms})
    candidates.sort(key=lambda c: (-Fraction(*c["score"]), c["options"][0]))
    reason, choice = "accepted", None
    margin = Fraction(*candidates[0]["score"]) - Fraction(*candidates[1]["score"]) if len(candidates) > 1 else Fraction(1)
    if not available:
        reason = "unqualified_or_unknown_operation_evidence"
    elif Fraction(*candidates[0]["score"]) < Fraction(*policy.min_score):
        reason = "below_score_threshold"
    elif len(candidates) > 1 and (margin == 0 or margin < Fraction(*policy.min_margin)):
        reason = "ambiguous_operation"
    else:
        choice = candidates[0]["options"][0]
    return {"choice": choice, "state": "bound" if choice is not None else "clarify", "reason": reason,
            "node": node, "field": field, "basis": basis.record(), "profile": profile.record(),
            "options": copy.deepcopy(options), "allow_zero": allow_zero, "zero_value": zero_value,
            "policy": policy.record(), "coordinate_contract_sha256": contract["contract_sha256"],
            "margin": [margin.numerator, margin.denominator], "candidates": candidates}


def replay_named_operator(record):
    """Reject altered contributions, options, qualification or enum meanings."""
    from engine.schema_decode import BindingBasis, BindingProfile

    profile = record["profile"]
    result = named_operator_choice(BindingBasis.from_record(record["basis"]),
        BindingProfile(**{**profile, "values": tuple(profile["values"])}), record["options"],
        NamedDecisionPolicy(tuple(record["policy"]["min_score"]), tuple(record["policy"]["min_margin"])),
        node=record["node"], field=record["field"], allow_zero=record["allow_zero"], zero_value=record["zero_value"])
    if result != record:
        raise InvalidDecision("named operator trace changed or contains unaccounted fields")
    return result


def named_operator_answers(basis, profiles, policy):
    """Consume the shared head's scoped exports for every input operator slot."""
    bank = named_operator_requests()
    if not isinstance(profiles, dict) or set(profiles) != set(bank):
        raise InvalidDecision("scoped named profiles must cover the complete operator request bank")
    result = {}
    for identity, request in bank.items():
        profile = profiles[identity]
        if profile.identity != identity or profile.kind != "operation":
            raise InvalidDecision("named operator profile has the wrong scope or input role")
        result[identity] = named_operator_choice(basis, profile, request["options"], policy,
            node=request["node"], field=request["field"], allow_zero=request["allow_zero"], zero_value=request["zero_value"])
    return result


def _key(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def conversation(turns):
    """Keep roles explicit; never silently truncate a conversation."""
    if isinstance(turns, str):
        turns = [{"role": "user", "content": turns}]
    if not isinstance(turns, list) or not 1 <= len(turns) <= MAX_TURNS:
        raise InvalidDecision("conversation length outside supported bounds")
    result = []
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) != {"role", "content"}:
            raise InvalidDecision("turn must have exactly role and content")
        role, text = turn["role"], turn["content"]
        if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
            raise InvalidDecision("invalid conversation turn")
        if len(text) > MAX_TURN_CHARS:
            raise InvalidDecision("turn exceeds supported bound")
        text.encode("utf8")
        result.append({"role": role, "content": text})
    if result[-1]["role"] != "user":
        raise InvalidDecision("last turn must be the user's question")
    return result


def _number(text):
    if "," in text:
        from engine.numeric import GROUPED_DIGITS
        if not re.fullmatch(rf"[+-]?(?:{GROUPED_DIGITS})(?:\.\d*)?", text):
            return None
        text = text.replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", text):
        return None
    if "." not in text:
        return int(text)
    value = float(text)
    return value if math.isfinite(value) else None


@lru_cache(maxsize=2)
def _input_schema(graph):
    """Bounded, input-only lexical indexes; no question or training label cached."""
    schema = Schema(graph)
    words, prefixes, dates = {}, {}, {}
    for column, items in schema.text_values.items():
        for normal, original in items.items():
            terms = set(re.findall(r"[^\W_]+", normal))
            entry = (column, normal, original, len(terms))
            for term in sorted(terms):
                words.setdefault(term, []).append(entry)
                if len(term) >= 4:
                    prefixes.setdefault(term[:4], []).append(entry)
    for c in graph.columns:
        key = (c.ref.table, c.ref.name)
        dates[key] = sorted({m.group(1) + "/" for sample in c.values if isinstance(sample, str)
                             and (m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", sample.strip()))})
    return schema, words, prefixes, dates


def request(turns, graph):
    """One native CLEF state/questions record, and its input-derived option map.

    Neither this function nor its schema/value candidates receive a target.
    Large requests must fail token-budget validation at the model boundary.
    """
    turns = conversation(turns)
    schema, word_index, prefix_index, date_prefixes = _input_schema(graph)
    text = "\n".join(t["content"] for t in turns)
    anchors = schema.anchors(text)
    values, seen = [], set()

    def value(v):
        if isinstance(v, bool) or not isinstance(v, (str, int, float, type(None))):
            return
        if isinstance(v, float) and not math.isfinite(v):
            return
        if isinstance(v, str) and len(v) > 256:
            return
        key = _key(v)
        if key not in seen:
            seen.add(key)
            values.append(v)

    value(None)
    value("")
    # Numbers 0 and 1 have generic roles in COUNT/HAVING and boolean comparisons.
    value(0)
    value(1)
    from engine.readers.numerals import mentions
    for turn in turns:
        for number, _, _ in mentions(turn["content"]):
            value(number)
            # Equivalent integer count boundaries: > one and >= two, for example.
            if isinstance(number, int):
                value(number - 1)
                value(number + 1)
    for turn in turns:
        words = turn["content"].split()
        for length in range(1, min(8, len(words)) + 1):
            for start in range(len(words) - length + 1):
                span = " ".join(words[start:start + length]).strip("\"'?!;.,“”‘’�()")
                value(span)
                number = _number(span)
                if number is not None:
                    value(number)
    columns = []
    question_words = set(re.findall(r"[^\W_]+", text.casefold()))
    from engine.sql_dates import MONTHS
    for word in sorted(question_words):
        if word in MONTHS:
            value(MONTHS[word])
    matches = {}
    for word in sorted(question_words):
        entries = word_index.get(word, []) + (prefix_index.get(word[:4], []) if len(word) >= 4 else [])
        for column, normal, original, size in entries:
            item = matches.setdefault((column, normal), [original, size, set()])
            item[2].add(word)
    matching_values = {}
    for (column, normal), (original, size, matched) in matches.items():
        matching_values.setdefault(column, []).append((-len(matched) / max(1, size), normal, original))
    for c in graph.columns:
        key = (c.ref.table, c.ref.name)
        samples = anchors.get(key, []) + schema.values[key][:6]
        for sample in samples:
            value(sample)
            number = _number(str(sample))
            if number is not None:
                value(number)
        # Ground abbreviations and spelling variants against actual cells. These
        # are candidates only: a lexical match never selects a filter itself.
        for _, _, original in sorted(matching_values.get(key, []))[:3]:
            value(original)
        for prefix in date_prefixes[key]:
            value(prefix)
        columns.append({"column": f"{c.ref.table}.{c.ref.name}", "type": schema.types[key],
                        "samples": list(dict.fromkeys(str(v) for v in samples))[:9]})
    # Use the existing calendar semantics; never infer a date from a gold value.
    from engine.sql_dates import served_date_phrases
    from engine.sql_expansion import tokens
    date_columns = [c for c in graph.columns if schema.types[(c.ref.table, c.ref.name)] == "date"]
    if date_columns:
        for turn in turns:
            for phrase in served_date_phrases(turn["content"], tokens(turn["content"]), graph):
                for comparison in phrase.comparisons(date_columns[0].ref):
                    value(comparison.right.value)
    refs = [c["column"] for c in columns]
    refs += [f"{part}:{c['column']}" for c in columns if c["type"] == "date" for part in sorted(DATE_PARTS)]
    column_options = [NONE, "*", *refs]
    value_options = [SUBQUERY, *values]
    limits = [None, 1]
    for v in values:
        if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 100000 and v not in limits:
            limits.append(v)
    questions, bindings = {}, {}

    def choose(qid, instruction, options):
        mapping = {f"o{i}": v for i, v in enumerate(options)}
        bindings[qid] = mapping
        questions[qid] = {"type": "choice", "instructions": instruction,
                          "criteria": {k: _key(v) for k, v in mapping.items()}}

    choose("action", "Resolve the latest user turn using the conversation. Choose answer only when the intended "
           "query is complete and expressible. Choose clarify for missing or ambiguous intent; unsupported for "
           "a required operation this profile cannot express.", ["answer", "clarify", "unsupported"])
    choose("structure", "Query structure; NESTED means block 1 is the subquery used by block 0.", STRUCTURES)
    for b in range(2):
        prefix = f"b{b}"
        for i, table in enumerate(schema.tables):
            choose(f"{prefix}.table{i}", f"Does block {b} read table {table}? Include necessary linking tables.", [False, True])
        for field, options in (("distinct", [False, True]), ("conj", ["AND", "OR"]),
                               ("dir", [None, "ASC", "DESC"]), ("limit", limits)):
            choose(f"{prefix}.{field}", f"Block {b} {field}; use its default when inactive.", options)
        for kind, count in BOUNDS.items():
            for slot in range(count):
                stem = f"{prefix}.{kind}{slot}"
                choose(stem + ".column", f"Block {b} {kind} item {slot}, in requested order. "
                       "unused means this and later items are absent. * means all rows/columns.", column_options)
                if kind in ("select", "having", "order"):
                    choose(stem + ".aggregate", f"Aggregate for {stem}; plain means the column itself.", AGGREGATES)
                if kind == "select":
                    choose(stem + ".distinct", f"Count distinct values inside aggregate {stem}?", [False, True])
                if kind in ("where", "having"):
                    choose(stem + ".operator", f"Comparison operator for {stem}.",
                           OPERATORS if kind == "where" else HAVING_OPERATORS)
                    choose(stem + ".value", f"Comparison value for {stem}; {{block:1}} refers to the subquery.", value_options)
                    choose(stem + ".pattern", f"Text matching for {stem}; exact for non-LIKE comparisons.",
                           ["exact", "contains", "prefix", "suffix"])
                if kind == "where":
                    choose(stem + ".share", f"Is {stem} a part-only condition in a share of an aggregate?", [False, True])
    state = {"conversation": turns, "schema": columns, "tables": schema.tables,
             "foreign_keys": [{"from": f"{f.from_column.table}.{f.from_column.name}",
                               "to": f"{f.to_column.table}.{f.to_column.name}"} for f in graph.foreign_keys],
             "profile": {"blocks": 2, **BOUNDS, "rules": "No invented filters or limits. Later corrections override "
                         "earlier instructions. Preserve constraints not changed by the user. No free-form SQL."}}
    return {"state": state, "questions": questions}, bindings


def named_choice(basis, query_profile, options, profiles, policy, *, candidate_kind, required_identity=""):
    """Bind one input-derived option using only the named semantic interface.

    This is a choice supplier for the existing resolve/compiler, not a planner.
    All supplied options must have profiles; pruning must occur before this call
    and retain its own recall record. Returning no choice requires clarification.
    Other decision fields are not certified by this binding record.
    """
    from engine.schema_decode import bind_profiles

    if (not isinstance(options, dict) or not isinstance(profiles, dict)
            or set(options) != set(profiles) or any(not isinstance(key, str) for key in options)):
        raise InvalidDecision("named binding profiles must cover exactly the supplied option identities")
    if any(not isinstance(value, (str, int, float, bool, type(None), dict)) for value in options.values()):
        raise InvalidDecision("named binding option must be an explicit input-derived value")
    try:
        decision = bind_profiles(basis, query_profile, tuple(profiles[key] for key in sorted(profiles)), policy,
                                 candidate_kind=candidate_kind, required_identity=required_identity)
    except ValueError as exc:
        raise InvalidDecision("invalid named binding: " + str(exc)) from exc
    selected = next((key for key in sorted(profiles)
                     if profiles[key].identity == decision["selected"]), None)
    return {"choice": selected, "binding": decision,
            "option_bindings": [{"option": key, "identity": profiles[key].identity,
                                 "value": copy.deepcopy(options[key])} for key in sorted(options)]}


def named_propositions(identity, options, coordinates, *, absent=()):
    """Scoped candidate-role propositions, separate from static item meaning.

    Instructions and the already predicted AST prefix belong to shared context.
    Physical option IDs stay outside tokens; candidate meanings enter the shared
    named head. Absence complements an explicit presence assertion.
    """
    if not isinstance(options, dict) or not options or set(options) != set(coordinates):
        raise InvalidDecision("named propositions require one semantic coordinate per input option")
    if set(absent) - set(options):
        raise InvalidDecision("unknown absence option")
    result = {}
    for option, value in options.items():
        missing = option in absent
        text = ("The requested field has an explicit value or another element." if missing else
                "This candidate fulfills the requested grammatical/semantic role: " + _key(value))
        result[option] = {"coordinate": coordinates[option], "polarity": -1 if missing else 1,
                          "item": {"identity": identity + "/" + option, "kind": "frame", "text": text}}
    return result


def named_field_choice(basis, profiles, request, policy):
    """Choose a grounded field using only qualified named proposition values."""
    if not request or set(profiles) != {r["item"]["identity"] for r in request.values()}:
        raise InvalidDecision("named field profiles do not cover its exact input bank")
    rows, complete = [], True
    for option, descriptor in request.items():
        profile = profiles[descriptor["item"]["identity"]]
        name, polarity = descriptor["coordinate"], descriptor["polarity"]
        if name not in basis.coordinates or polarity not in (-1, 1):
            raise InvalidDecision("unknown field coordinate or polarity")
        i = basis.coordinates.index(name)
        if profile.basis_id != basis.identity or profile.kind != "frame" or profile.identity != descriptor["item"]["identity"]:
            raise InvalidDecision("field proposition profile has another basis/role/identity")
        if len(profile.values) != len(basis.coordinates) or any(v is not None and not 0 <= v <= basis.levels for v in profile.values):
            raise InvalidDecision("field proposition exceeds its pinned quantized basis")
        value = profile.values[i]
        known = basis.qualified_for("frame")[i] and value is not None and basis.weights[i] > 0
        complete &= known
        score = None if not known else Fraction(value if polarity == 1 else basis.levels - value, basis.levels)
        recorded = profile.record()
        recorded["values"] = {coordinate: number for coordinate, number in zip(basis.coordinates, profile.values)
                              if number is not None}
        rows.append({"option": option, "coordinate": name, "polarity": polarity,
                     "profile": recorded, "score": None if score is None else [score.numerator, score.denominator]})
    ranked = sorted((r for r in rows if r["score"] is not None), key=lambda r: (-Fraction(*r["score"]), r["option"]))
    choice, reason, margin = None, "unqualified_or_unknown_proposition", Fraction(0)
    if complete:
        best = Fraction(*ranked[0]["score"])
        margin = best - Fraction(*ranked[1]["score"]) if len(ranked) > 1 else best
        if best < Fraction(*policy.min_score): reason = "below_score"
        elif len(ranked) > 1 and (margin <= 0 or margin < Fraction(*policy.min_margin)): reason = "ambiguous_or_below_margin"
        else: choice, reason = ranked[0]["option"], "selected"
    return {"choice": choice, "reason": reason, "basis": basis.record(), "policy": policy.record(),
            "request": copy.deepcopy(request), "contributions": rows,
            "profile_format": "named values; omitted coordinates are unknown, never zero",
            "margin": [margin.numerator, margin.denominator], "hidden_choice_channels": 0}


def replay_named_field(record):
    from engine.schema_decode import BindingBasis, BindingProfile

    basis = BindingBasis.from_record(record["basis"])
    profiles = {}
    for row in record["contributions"]:
        r = row["profile"]
        if not isinstance(r["values"], dict) or set(r["values"]) - set(basis.coordinates):
            raise InvalidDecision("named field replay contains an unknown coordinate")
        profiles[r["identity"]] = BindingProfile(r["basis_id"], r["identity"], r["kind"],
                                                 tuple(r["values"].get(name) for name in basis.coordinates),
                                                 r["evidence"], r["relationship"], r["datatype"])
    expected = named_field_choice(basis, profiles, record["request"],
                                  NamedDecisionPolicy(tuple(record["policy"]["min_score"]), tuple(record["policy"]["min_margin"])))
    if expected != record:
        raise InvalidDecision("named field replay differs from its recorded computation")
    return expected


def choices(answers, bindings):
    """Validate option IDs; an unknown or missing decision is never repaired."""
    if set(answers) != set(bindings):
        raise InvalidDecision("missing or unexpected decision keys")
    result = {}
    for key, options in bindings.items():
        answer = answers[key]
        if isinstance(answer, dict):
            answer = answer.get("choice")
        if not isinstance(answer, str) or answer not in options:
            raise InvalidDecision("unknown option for " + key)
        result[key] = copy.deepcopy(options[answer])
    return result


def active_fields(selected, record):
    """Meaning-bearing heads, without changing the model's full input schema.

    Training can mask unused attributes without leaking the target through
    omitted input questions. Decoding uses the identical definition.
    """
    active = {"action"}
    if selected["action"] != "answer":
        return active
    active.add("structure")
    for b in range(1 if selected["structure"] == "single" else 2):
        prefix = f"b{b}"
        active.update(f"{prefix}.table{i}" for i in range(len(record["state"]["tables"])))
        active.update(f"{prefix}.{name}" for name in ("distinct", "conj", "dir", "limit"))
        for kind, count in BOUNDS.items():
            for slot in range(count):
                stem = f"{prefix}.{kind}{slot}"
                active.add(stem + ".column")
                if selected[stem + ".column"] != NONE:
                    active.update(key for key in record["questions"] if key.startswith(stem + "."))
    return active


def _block_default():
    return {"tables": [], "distinct": False, "select": [], "where": [], "conj": "AND",
            "group": [], "having": [], "order": [], "dir": None, "limit": None}


def resolve(answers, record, bindings, graph):
    """Validated meaning + typed AST, or an explicit clarification/unsupported state."""
    selected = choices(answers, bindings)
    action = selected["action"]
    if action != "answer":
        return {"action": action, "readings": None, "query": None}
    structure = selected["structure"]
    blocks = []
    for b in range(1 if structure == "single" else 2):
        block = _block_default()
        prefix = f"b{b}"
        block["tables"] = sorted(t for i, t in enumerate(record["state"]["tables"]) if selected[f"{prefix}.table{i}"])
        for field in ("distinct", "conj", "dir", "limit"):
            block[field] = selected[f"{prefix}.{field}"]
        share = []
        for kind, count in BOUNDS.items():
            ended = False
            for slot in range(count):
                stem = f"{prefix}.{kind}{slot}"
                column = selected[stem + ".column"]
                if column == NONE:
                    ended = True
                    continue
                if ended:
                    raise InvalidDecision("non-contiguous " + kind + " slots")
                aggregate = selected.get(stem + ".aggregate")
                if kind == "select":
                    block[kind].append([slot, aggregate, column, selected[stem + ".distinct"]])
                elif kind == "group":
                    block[kind].append(column)
                elif kind == "order":
                    block[kind].append([aggregate, column])
                else:
                    operator, value = selected[stem + ".operator"], selected[stem + ".value"]
                    pattern = selected[stem + ".pattern"]
                    if pattern != "exact":
                        if operator not in ("LIKE", "NOT LIKE") or not isinstance(value, str):
                            raise InvalidDecision("text pattern on a non-text match")
                        value = ("%" if pattern in ("contains", "suffix") else "") + value + ("%" if pattern in ("contains", "prefix") else "")
                    if value == SUBQUERY and (structure != "NESTED" or b != 0):
                        raise InvalidDecision("subquery outside the outer nested block")
                    block[kind].append(([aggregate] if kind == "having" else []) + [column, operator, value])
                    if kind == "where" and selected[stem + ".share"]:
                        share.append(slot)
        if share:
            block["share"] = share
        blocks.append(block)
    reading = {"structure": structure, "blocks": blocks}
    try:
        from engine.readers import ace
        ace_text, ace_tree = ace.encode(reading)
        recovered, query = ace.decode(ace_tree, graph)
        if recovered != reading:
            raise InvalidDecision("ACE view changed the selected meaning")
        # The existing compiler normalizes extrema/grouping and can omit an
        # unrelated, unused table. The exposed ACE must describe the AST it
        # actually constructed, rather than the unnormalized model flags.
        shared = any(block.get("share") for block in blocks)
        if shared:
            realized = as_built(reading, query)
        else:
            realized = readings_of(query)
        ace_text, ace_tree = ace.encode(realized)
        decoded, checked = ace.decode(ace_tree, graph)
        from engine.sql_ast import render_query
        # A lossless readings round trip does not establish AST validity.
        # Always run the owner's validator before exposing a candidate.
        checked_sql = render_query(checked)
        agrees = checked_sql == render_query(query) if shared else readings_of(checked) == realized
        if decoded != realized or not agrees:
            raise InvalidDecision("canonical ACE and SQL AST disagree")
    except Exception as exc:  # noqa: BLE001 - the existing builder owns all admissible ASTs
        # Builder failures remain refusals, never a second planner or a fallback.
        raise InvalidDecision(f"builder rejected interpretation: {type(exc).__name__}: {exc}") from exc
    return {"action": "answer", "readings": realized, "raw_readings": reading,
            "query": checked, "ace": ace_text, "ace_tree": ace_tree}


def supervision(reading, record, bindings, action="answer"):
    """Attach labels after building the input; fail if a gold value is not offered."""
    selected = {key: next(iter(options.values())) for key, options in bindings.items()}
    selected["action"] = action
    if action == "answer":
        selected["structure"] = reading["structure"]
        for b, block in enumerate(reading["blocks"]):
            prefix = f"b{b}"
            for i, table in enumerate(record["state"]["tables"]):
                selected[f"{prefix}.table{i}"] = table in block["tables"]
            for field in ("distinct", "conj", "dir", "limit"):
                selected[f"{prefix}.{field}"] = block[field]
            for kind, count in BOUNDS.items():
                if len(block[kind]) > count:
                    raise InvalidDecision("profile bound exceeded: " + kind)
                for slot, term in enumerate(block[kind]):
                    stem = f"{prefix}.{kind}{slot}"
                    if kind == "select":
                        if term[0] != slot:
                            raise InvalidDecision("selection slots are not canonical")
                        _, agg, col, distinct = term
                        selected.update({stem + ".column": col, stem + ".aggregate": agg, stem + ".distinct": distinct})
                    elif kind == "group":
                        selected[stem + ".column"] = term
                    elif kind == "order":
                        selected.update({stem + ".aggregate": term[0], stem + ".column": term[1]})
                    else:
                        col, op, val = term[-3:]
                        pattern = "exact"
                        if op in ("LIKE", "NOT LIKE") and isinstance(val, str):
                            if val.startswith("%") and val.endswith("%"):
                                pattern, val = "contains", val[1:-1]
                            elif val.startswith("%"):
                                pattern, val = "suffix", val[1:]
                            elif val.endswith("%"):
                                pattern, val = "prefix", val[:-1]
                            if "%" in val or "_" in val:
                                raise InvalidDecision("unsupported wildcard inside pattern")
                        selected.update({stem + ".column": col, stem + ".operator": op,
                                         stem + ".value": val, stem + ".pattern": pattern})
                        if kind == "having":
                            selected[stem + ".aggregate"] = term[0]
                        else:
                            selected[stem + ".share"] = slot in block.get("share", [])
    gold = {}
    for key, val in selected.items():
        matches = [k for k, v in bindings[key].items() if _key(v) == _key(val)]
        if len(matches) != 1:
            raise InvalidDecision("target absent from input-derived options: " + key + "=" + repr(val))
        gold[key] = {"label": matches[0]}
    return dict(record, gold=gold)


def checked_probabilities(answers, bindings):
    """Validate the native decision wire contract and retain its declared tie-break."""
    if set(answers) != set(bindings):
        raise InvalidDecision("missing or unexpected decision keys")
    options, probabilities, names = {}, {}, list(bindings)
    for name in names:
        probs = answers[name].get("probabilities")
        if not isinstance(probs, dict) or set(probs) != set(bindings[name]):
            raise InvalidDecision("incomplete probabilities for " + name)
        if any(type(p) not in (int, float) or not math.isfinite(p) or p < 0 or p > 1 for p in probs.values()):
            raise InvalidDecision("invalid probability")
        total = math.fsum(probs.values())
        # The pinned Unsloth prediction API rounds probabilities to four
        # decimals. Allow only that bounded wire error, then normalize it.
        if total <= 0 or abs(total - 1.0) > len(probs) * 0.00005 + 1e-6:
            raise InvalidDecision("probabilities do not sum to one")
        probabilities[name] = {key: value / total for key, value in probs.items()}
        declared = answers[name].get("choice")
        if declared is not None and (declared not in probs or probs[declared] != max(probs.values())):
            raise InvalidDecision("declared choice disagrees with probabilities")
        # Preserve the API's full-precision argmax when rounding creates a tie.
        options[name] = sorted(probs, key=lambda k: (-probs[k], k != declared, k))
    return options, probabilities


def checked_scores(answers, bindings):
    """Keep native logits until a recursive grammar applies its legal-action mask.

    Rounding probabilities before the mask can erase every legal alternative.
    Probability-only API replies retain their existing validation contract.
    A request uses one explicit score format across all fields.
    """
    if not isinstance(answers, dict) or set(answers) != set(bindings):
        raise InvalidDecision("missing or unexpected decision keys")
    if not all(isinstance(answer, dict) for answer in answers.values()):
        raise InvalidDecision("a scored decision must be an object")
    if not any("logits" in answer for answer in answers.values()):
        options, probabilities = checked_probabilities(answers, bindings)
        return options, {name: {key: math.log(p) if p > 0 else -math.inf for key, p in probs.items()}
                         for name, probs in probabilities.items()}
    options, scores = {}, {}
    for name, answer in answers.items():
        raw = answer.get("logits")
        if "probabilities" in answer or not isinstance(raw, dict) or set(raw) != set(bindings[name]):
            raise InvalidDecision("incomplete or mixed native scores for " + name)
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in raw.values()):
            raise InvalidDecision("invalid native logit")
        declared = answer.get("choice")
        if declared not in raw or raw[declared] != max(raw.values()):
            raise InvalidDecision("declared choice disagrees with logits")
        options[name] = sorted(raw, key=lambda key: (-raw[key], key != declared, key))
        scores[name] = dict(raw)
    return options, scores


def candidates(answers, record, bindings, graph, limit=16, attempts=256):
    """A bounded deterministic search over the joint head's scored choices.

    All options come from the request. No gold, database answer, extra model, or
    SQL text enters this decoder. Invalid combinations never reach execution.
    A clarify/unsupported action is respected, not flipped into an answer.
    """
    options, probabilities = checked_probabilities(answers, bindings)
    names = list(bindings)
    initial = {name: options[name][0] for name in names}
    if bindings['action'][initial['action']] != 'answer':
        return []
    # Explore the twelve least-certain decision fields, with action held fixed.
    def margin(name):
        p = probabilities[name]; choices_ = options[name]
        return math.log(max(p[choices_[0]], 1e-30)) - math.log(max(p[choices_[1]], 1e-30)) if len(choices_)>1 else math.inf
    relevant = tuple(sorted(active_fields(choices(initial, bindings), record)))
    variable = sorted((n for n in relevant if n != 'action' and len(options[n])>1), key=lambda n: (margin(n), n))[:12]

    def score(picks):
        # Identical field set across candidates, so scores have a stable order.
        return sum(math.log(max(probabilities[n][picks[n]], 1e-30)) for n in relevant) / len(relevant)

    zero = (0,) * len(variable)
    queue, seen, sql_seen, result = [(-score(initial), zero)], {zero}, set(), []
    from engine.sql_ast import render_query
    for _ in range(attempts):
        if not queue or len(result) >= limit:
            break
        negative, indices = heapq.heappop(queue)
        picks = dict(initial)
        picks.update({name: options[name][indices[i]] for i, name in enumerate(variable)})
        try:
            interpreted = resolve(picks, record, bindings, graph)
            sql = render_query(interpreted['query'])
            if sql not in sql_seen:
                sql_seen.add(sql)
                result.append((-negative, interpreted))
        except InvalidDecision:
            pass
        for i, name in enumerate(variable):
            if indices[i] + 1 >= len(options[name]):
                continue
            changed = list(indices); changed[i] += 1; changed = tuple(changed)
            if changed not in seen:
                seen.add(changed)
                next_picks = dict(initial)
                next_picks.update({n: options[n][changed[j]] for j, n in enumerate(variable)})
                heapq.heappush(queue, (-score(next_picks), changed))
    return result
