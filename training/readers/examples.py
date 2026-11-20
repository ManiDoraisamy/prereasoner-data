"""Training examples for both reader models, from the gold readings of each question (engine.readers.readings) and
the schema as the engine receives it (engine.readers.text).

Stage 1 (schema items, stage1.jsonl): one example per (question, table); labels say whether the table is used and,
for each column, whether it is used and in which roles (select, filter, group, order, having).

Stage 2 (readings, stage2.jsonl): one example per question, the question paired with a pruned schema as serving
prunes it: the gold tables plus the nearest other tables by foreign-key hops, up to 6 in all (gold_pruning), in
schema order, at most 20 columns per table (gold columns kept), fitted to the token budget as serving fits it
(engine.readers.text.fit: the samples, then the farthest tables). With anchors (the default) the question's own values are listed beside their columns;
--no-anchors serializes without them. Labels are every reading the builder uses; a literal value is located by its
mention in the question, else in its column's listed values.

Rows carry split "train" (readings_train, BIRD, Spider-Syn, SynSQL, rewordings) or "calibration" (readings_calibration, the 20 held-out
Spider databases); the trainers fit on the first and select their checkpoint on the second.

usage: python -m training.readers.examples <readings_train.jsonl> <readings_calibration.jsonl> <out dir>
           [--bird <BIRD train dir> --bird-readings <readings_bird.jsonl>[,<readings_bird_share.jsonl>]]
           [--syn <Spider-Syn dir> --syn-readings <readings_syn.jsonl>]
           [--synsql <selection dir> --synsql-databases <dir> --synsql-readings <readings_synsql.jsonl>]
           [--reworded <rewordings.jsonl>[,...] --reworded-readings <readings_reworded.jsonl>[,...]]
           [--lookups <training.readers.lookups out dir>] [--structures <training.readers.structures out dir>[,...]]
           [--no-anchors]
           [--tokenizer <dir or hf id>] [--spider-zip <zip>] [--db-dir <dir> ...]
"""
import argparse
import collections
import json
import os
import random
import re
import sys

from engine.readers import numerals
from engine.readers.models import ROLES
from engine.readers.search import BUDGET, MAX_COLUMNS
from engine.readers.text import TOP_TABLES, Schema, fit, table_text
from training.readers.provenance import assert_no_evaluation_sources
from training.readers.sources import TOKENIZER, Sources, add_arguments


def roles_of(readings):
    """{"t.c": set of roles} and the set of used tables."""
    roles = collections.defaultdict(set)
    tables = set()
    for block in readings["blocks"]:
        tables.update(block["tables"])
        for _, _, column, _ in block["select"]:
            roles[column].add("select")
        for column, _, _ in block["where"]:
            roles[column].add("filter")
        for column in block["group"]:
            roles[column].add("group")
        for _, column, _, _ in block["having"]:
            roles[column].add("having")
        for _, column in block["order"]:
            roles[column].add("order")
    roles.pop("*", None)
    return roles, tables


def find_value(value, question, value_spans):
    """('question'|'schema', start, end) of the literal's mention, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for number, start, end in numerals.mentions(question):
            if float(number) == float(value):
                return "question", start, end
        return None
    text = str(value).strip("%")
    if not text:
        return None
    position = question.lower().find(text.lower())
    if position >= 0:
        return "question", position, position + len(text)
    for listed, start, end in value_spans:
        if listed.lower() == text.lower():
            return "schema", start, end
    return None


def reading_labels(readings):
    """{"t.c" or "*": {"block": [facts of block 0, facts of block 1]}} and [(block, column, literal)]."""
    labels, values = {}, []

    def entry(column):
        return labels.setdefault(column, {"block": [{} for _ in range(2)]})

    for b, block in enumerate(readings["blocks"]):
        for slot, aggregate, column, distinct in block["select"]:
            item = entry(column)["block"][b]
            item.setdefault("select", []).append(aggregate)
            item.setdefault("slots", {}).setdefault(aggregate, slot)
            if distinct:
                item["distinct_aggregate"] = 1
        share = set(block.get("share") or ())
        for i, (column, operator, value) in enumerate(block["where"]):
            if i in share:
                entry(column)["block"][b]["share_part"] = 1
            entry(column)["block"][b].setdefault("where", []).append(operator)
            if isinstance(value, dict):
                entry(column)["block"][b]["where_sub"] = 1
            else:
                values.append((b, column, value))
        for (column, _), count in collections.Counter((c, o) for c, o, _ in block["where"]).items():
            if count > 1:
                entry(column)["block"][b]["where_twice"] = 1
        for column in block["group"]:
            entry(column)["block"][b]["group"] = 1
        for aggregate, column, operator, value in block["having"]:
            item = entry(column)["block"][b]
            item.setdefault("having", []).append(aggregate)
            item.setdefault("having_op", []).append(operator)
            if isinstance(value, dict):
                item["having_sub"] = 1
            else:
                values.append((b, column, value))
        for aggregate, column in block["order"]:
            entry(column)["block"][b].setdefault("order", []).append(aggregate)
    return labels, values


def reading_example(record, question, schema, tables, columns_of, droppable, tokenizer, anchored):
    tables, (text, star, table_spans, column_spans) = fit(tokenizer, BUDGET, question, schema, tables, columns_of,
                                                          droppable, anchored)
    readings = record["readings"]
    blocks = readings["blocks"]
    labels, values = reading_labels(readings)
    value_labels, missing = [], 0
    for b, column, value in values:
        found = find_value(value, question, column_spans.get(column, (None, []))[1])
        if found is None:
            missing += 1
            continue
        where, start, end = found
        value_labels.append({"block": b, "column": column, "value": value, "in": where, "start": start, "end": end})
    return {
        "kind": "gold", "split": record["split"], "db_id": record["db_id"], "source": record["source"],
        "index": record["index"], "question": question, "text": text, "star": star,
        "tables": [{"name": t, "span": table_spans[t],
                    "used": [int(t in block["tables"]) for block in blocks] + [0] * (2 - len(blocks))}
                   for t in tables],
        "columns": [{"name": f"{t}.{c}", "span": column_spans[f"{t}.{c}"][0]} for t in tables for c in columns_of[t]],
        "labels": labels, "values": value_labels, "missing_values": missing, "structure": readings["structure"],
        "globals": [{"distinct": int(block["distinct"]), "conj": block["conj"], "dir": block["dir"],
                     "limit": block["limit"], "counts": counts_of(block, tables),
                     "share": int(bool(block.get("share")))} for block in blocks],
    }


def counts_of(block, tables):
    """How many of each item the block holds, as the decoder takes them: kept tables it uses, (column, aggregate)
    items shown, (column, operator) pairs compared, grouped columns, sort terms and HAVING terms."""
    return {"tables": sum(t in block["tables"] for t in tables),
            "select": len({(column, aggregate) for _, aggregate, column, _ in block["select"]}),
            "where": len({(column, operator) for column, operator, _ in block["where"]}),
            "group": len(block["group"]), "order": len(block["order"]),
            "having": len({(aggregate, column) for aggregate, column, _, _ in block["having"]})}


def gold_pruning(record, schema, roles, used_tables, rng):
    """The gold tables and other tables as serving keeps them: up to TOP_TABLES in all, the nearest by foreign-key
    hops first (ties seeded), since the tables stage 1 also keeps are the ones that look like the question's. A
    quarter of examples keep fewer, so a short schema is seen too. The farthest go first when the budget is short."""
    others = [t for t in schema.tables if t not in used_tables]
    distance = hops(schema, used_tables)
    ranked = sorted(others, key=lambda t: (distance.get(t, 99), rng.random()))
    room = max(0, TOP_TABLES - len(used_tables))
    extra = ranked[:room if rng.random() < 0.75 else rng.randint(0, room)]
    tables = [t for t in schema.tables if t in used_tables or t in extra]
    columns_of = {}
    for t in tables:
        columns = schema.columns[t]
        if len(columns) > MAX_COLUMNS:
            keep = {c for c in columns if f"{t}.{c}" in roles}
            rest = [c for c in columns if c not in keep]
            keep |= set(rng.sample(rest, max(0, MAX_COLUMNS - len(keep))))
            columns = [c for c in columns if c in keep]
        columns_of[t] = columns
    return tables, columns_of, list(reversed(extra))


def hops(schema, start):
    """Foreign-key hops from the nearest of ``start`` to every table it reaches."""
    distance = {t: 0 for t in start}
    frontier = list(start)
    while frontier:
        following = []
        for table in frontier:
            for neighbour in sorted(schema.related[table]):
                if neighbour not in distance:
                    distance[neighbour] = distance[table] + 1
                    following.append(neighbour)
        frontier = following
    return distance


def semantic_example(state, *, identity, source, db_id, split, query=None, action="answer", contract=None,
                     dialogue_controls=None):
    """Input-only state beside directly supervised named query coordinates.

    Gold ASTs/role paths are annotations outside input and outside encoder items.
    This example currently supervises SQL presence and the annotated action, not
    noun/column/value alignment or unannotated financial predicate meanings.
    """
    from engine.readers.layout import named_coordinate_contract
    from engine.sql_ast import query_to_tree
    from training.readers.gold import semantic_labels

    assert_no_evaluation_sources([{"source": source}], "semantic reader example")
    if split not in ("train", "calibration") or action not in ("answer", "clarify", "unsupported"):
        raise ValueError("semantic examples require explicit training split and annotated action")
    if (action == "answer") != (query is not None):
        raise ValueError("only an annotated answer has an executable target")
    if (not isinstance(state, dict) or set(state) != {"schema", "conversation"}
            or not isinstance(state["schema"], list) or not isinstance(state["conversation"], list)
            or not state["conversation"]):
        raise ValueError("semantic examples require an input-only complete state")
    contract = named_coordinate_contract() if contract is None else contract
    annotation = semantic_labels(query, contract) if query is not None else {
        "labels": {}, "arguments": [], "contract_sha256": contract["contract_sha256"]}
    labels = dict(annotation["labels"])
    labels.update({"dialogue." + a: int(action == a) for a in ("answer", "clarify", "unsupported")})
    if dialogue_controls is not None:
        allowed = {r["name"] for r in contract["coordinates"] if r["block"] == "dialogue"} - set(labels)
        if (not isinstance(dialogue_controls, dict) or set(dialogue_controls) != allowed
                or any(type(v) is not int or v not in (0, 1) for v in dialogue_controls.values())):
            raise ValueError("dialogue controls require a complete explicit annotation, separate from action")
        labels.update(dialogue_controls)
    if len(state["conversation"]) == 1 and state["conversation"][0]["role"] == "user":
        labels["dialogue.new_question"] = 1
    example = {"id": identity, "source": source, "db_id": db_id, "split": split,
            "contract_sha256": contract["contract_sha256"], "input": state,
            "items": [{"identity": "latest-question", "kind": "question",
                       "text": "The complete requested query operations and response action."}],
            "targets": [labels], "target_ast": query_to_tree(query) if query is not None else None,
            "argument_annotations": annotation["arguments"],
            "annotation_scope": "Program syntax and annotated action only; no automatic property/ACE-frame negatives."}
    from engine.readers.decisions import named_operator_requests, named_operation_meanings
    from engine.readers.readings import readings_of, NotReadable
    from training.readers.gold import semantic_operator_labels

    requests = named_operator_requests()
    reading, unavailable = None, None
    if query is not None:
        try:
            reading = readings_of(query)
        except NotReadable as error:
            unavailable = str(error)
    outside = []
    if reading is not None:
        from engine.readers.decisions import BOUNDS
        for b, block in enumerate(reading["blocks"]):
            if b >= 2: outside.append(f"block {b}")
            for kind, bound in BOUNDS.items():
                outside.extend(f"b{b}.{kind}{i}" for i in range(bound, len(block[kind])))
    for identity, request in requests.items():
        example["items"].append(request["item"])
        target = {}
        block_number, suffix = identity.split(".", 1)
        b = int(block_number[1:])
        value, known = None, False
        if reading is not None and b < len(reading["blocks"]):
            block = reading["blocks"][b]
            if suffix == "conj": value, known = block["conj"], len(block["where"]) > 1
            elif suffix == "dir": value, known = block["dir"], True
            else:
                part, attribute = suffix.split(".")
                kind, slot = re.fullmatch(r"([a-z]+)([0-9]+)", part).groups()
                slot = int(slot)
                if slot < len(block[kind]):
                    term = block[kind][slot]
                    value = (term[1] if kind == "select" else term[0]) if attribute == "aggregate" else term[-2]
                    known = True
        if known:
            meanings = named_operation_meanings(request["node"], request["field"], contract)
            legal = set(request["options"].values())
            if value not in legal:
                raise ValueError(f"scoped operator target is absent from its input-derived decision bank: {identity}={value!r}")
            coordinates = {meanings[v] for v in legal if v in meanings}
            target = {name: int(meanings.get(value) == name) for name in sorted(coordinates)}
        example["targets"].append(target)
    example["scoped_operator_annotations"] = semantic_operator_labels(query, contract)["operations"] if query is not None else []
    example["operator_readout_scope"] = {"profile": "existing bounded readings", "outside_bounds": outside,
                                         "unavailable_readings": unavailable, "recursive_ast_readout": False}
    return example


def generated_dialogue_query(row, query, original_question):
    """Carry the source AST through the bounded dialogue generator unchanged.

    Reconstructing it from lossy readings can replace explicit join keys with
    a different foreign-key path. Only the generator's two declared correction
    commands may alter the source program, and both preserve its other clauses.
    """
    from dataclasses import replace
    from engine.sql_ast import SelectQuery

    turns, variant = row["state"]["conversation"], row["variant"]
    if not turns or turns[0] != {"role": "user", "content": original_question}:
        raise ValueError("generated dialogue differs from its original source question")
    if variant in ("single", "clarify"):
        if len(turns) != 1 or (variant == "clarify") != (query is None):
            raise ValueError("generated source question/action differs")
        return query
    if (variant not in ("reference", "correction") or query is None or len(turns) != 3
            or [turn["role"] for turn in turns] != ["user", "assistant", "user"]
            or turns[1]["content"] != "I will use the uploaded tables."):
        raise ValueError("unverified generated continuation")
    follow = turns[2]["content"]
    if variant == "reference":
        if follow != "Please answer that question.":
            raise ValueError("generated reference changes its original question")
        return query
    direction = {"Actually, show the lowest first instead, and only the first three.": "ASC",
                 "Actually, show the highest first instead, and only the first three.": "DESC"}.get(follow)
    if not direction or not isinstance(query, SelectQuery) or not query.order_by:
        raise ValueError("generated correction has no explicit source ordering")
    return replace(query, order_by=tuple(replace(term, direction=direction) for term in query.order_by),
                   limit=3, with_ties=False)


def generated_dialogue_controls(row, query, previous, contract):
    """Certify controls from the existing bounded conversation generator.

    Variant metadata alone is insufficient. Check the actual turns and the
    complete source-reading delta before making any negative control annotation.
    The caller builds and validates each target through the existing compiler.
    Preserve the source readings: canonical AST optimization can rewrite an
    ordered LIMIT 1 into MIN/MAX and erase the user's original ordering command.
    This offline adapter never interprets arbitrary serving conversation text.
    """
    import copy

    turns = row["state"]["conversation"]
    variant = row["variant"]
    controls = {r["name"]: 0 for r in contract["coordinates"]
                if r["block"] == "dialogue" and r["name"] not in
                ("dialogue.answer", "dialogue.clarify", "dialogue.unsupported")}
    if variant in ("single", "clarify"):
        if (len(turns) != 1 or turns[0]["role"] != "user"
                or (variant == "clarify") != (query is None)):
            raise ValueError("generated single-turn annotation does not match its input/action")
        controls["dialogue.new_question"] = 1
        return controls
    if (variant not in ("reference", "correction") or previous is None or query is None
            or len(turns) != 3 or [t["role"] for t in turns] != ["user", "assistant", "user"]
            or turns[1]["content"] != "I will use the uploaded tables."
            or previous["variant"] != "single"
            or turns[0] != previous["state"]["conversation"][0]
            or [{k: v for k, v in s.items() if k != "samples"} for s in row["state"]["schema"]]
            != [{k: v for k, v in s.items() if k != "samples"} for s in previous["state"]["schema"]]):
        raise ValueError("unverified generated conversation context")
    # request() includes input-derived value anchors in samples. A follow-up
    # can expose another existing sample without changing the schema or table.
    before, after = previous["target_readings"], row["target_readings"]
    controls["dialogue.refer_to_previous_question"] = 1
    if variant == "reference":
        if turns[2]["content"] != "Please answer that question." or before != after:
            raise ValueError("generated reference changes the previous question's program")
        return controls
    expected = copy.deepcopy(before)
    block = expected["blocks"][0]
    if not block["order"]:
        raise ValueError("generated correction needs a previous ordered block")
    direction = "ASC" if block["dir"] == "DESC" else "DESC"
    block["dir"] = direction
    block["limit"] = 3
    follow = ("Actually, show the lowest first instead, and only the first three." if direction == "ASC"
              else "Actually, show the highest first instead, and only the first three.")
    if turns[2]["content"] != follow or expected != after:
        raise ValueError("generated correction changes more than its declared order/limit commands")
    controls["dialogue.replace_order"] = 1
    # This labels the explicit command even if the previous limit was already 3.
    controls["dialogue.replace_limit"] = 1
    return controls


def semantic_support(rows, contract):
    """Actual annotation/quantization support, including named but untrained axes."""
    support = {r["name"]: {"index": r["index"], "block": r["block"], "splits": {}, "roles": {}}
               for r in contract["coordinates"]}
    count = 0
    for row in rows:
        count += 1
        if len(row["targets"]) != len(row["items"]):
            raise ValueError("semantic support audit found mismatched input/target items")
        for item, targets in zip(row["items"], row["targets"]):
            for name, value in targets.items():
                if name not in support or type(value) not in (int, float) or not 0 <= value <= 1:
                    raise ValueError("invalid target in semantic support audit")
                for category, key in (("splits", row["split"]), ("roles", item["kind"])):
                    bucket = support[name][category].setdefault(key, {"known": 0, "hard_positive": 0,
                                                                     "hard_negative": 0, "fractional": 0,
                                                                     "quantized_levels": set()})
                    bucket["known"] += 1
                    bucket["hard_positive"] += int(value == 1)
                    bucket["hard_negative"] += int(value == 0)
                    bucket["fractional"] += int(0 < value < 1)
                    bucket["quantized_levels"].add(int(value * 16 + 0.5))
    states = collections.Counter()
    for item in support.values():
        for category in ("splits", "roles"):
            for bucket in item[category].values():
                bucket["quantized_levels"] = sorted(bucket["quantized_levels"])
        train = item["splits"].get("train", {})
        state = ("unobserved" if not train.get("known") else
                 "no_positive_target" if train["hard_negative"] == train["known"] else
                 "constant_quantized_target" if len(train["quantized_levels"]) == 1 else "varying_targets")
        item["training_state"] = state
        states[state] += 1
    return {"records": count, "contract_sha256": contract["contract_sha256"], "levels": 16,
            "states": dict(states), "coordinates": support,
            "scope": "Annotation support is not calibration, semantic correctness certification, or serving qualification."}


def semantic_property_examples(instances, expected, contract):
    """Cross-role expected-concept supervision, with source facts kept separate.

    Only source-annotated Wikidata entity rows produce value examples. No owner
    table's class is assigned to every arbitrary numeric/string cell. Concept and
    column descriptions are explicitly synthetic training witnesses; they are not
    held-out real-column evidence. Test entities are emitted as test, never train.
    """
    from training.schema_org.instances import group_id
    from engine.schema_org import load_contract

    ontology = load_contract()
    profiles = expected["profiles"]
    for uri, profile in sorted(profiles.items()):
        name = ontology.classes[uri].name
        for kind, text in (("noun", name), ("column", "Column containing " + name + " entities.")):
            yield {"id": "concept/" + kind + "/" + uri, "source": "semantic_instance",
                   "db_id": "semantic/concept/" + uri, "split": "train",
                   "contract_sha256": contract["contract_sha256"],
                   "input": {"schema": [], "conversation": []},
                   "items": [{"identity": "concept", "kind": kind, "text": text}],
                   "targets": [profile["probabilities"]], "target_ast": None, "argument_annotations": [],
                   "annotation_scope": "synthetic concept witness; empirical expected source-observation profile, not individual facts",
                   "evidence_kind": "expected_concept", "annotated_class": uri,
                   "source_train_groups": profile["train_groups"]}
    for item in instances:
        if item.source != "wikidata":
            continue
        ancestors = {a for uri in item.classes for a in ontology.classes[uri].ancestors}
        leaves = set(item.classes) - ancestors
        if len(leaves) != 1:
            continue
        uri = next(iter(leaves))
        if uri not in profiles or " | name: " not in item.text:
            continue
        # The adapter's relation/table prefix includes the labelled class. It is
        # not admissible evidence for a held-out entity's type. Preserve only the
        # source name/description/other observed field text, without source IDs.
        text = item.text.split(" | ", 1)[1]
        yield {"id": "entity/" + item.instance_id, "source": "semantic_instance",
               "db_id": "semantic/entity/" + group_id(item.instance_id),
               "split": {"validation": "calibration"}.get(item.split, item.split),
               "contract_sha256": contract["contract_sha256"],
               "input": {"schema": [], "conversation": []},
               "items": [{"identity": "entity", "kind": "value", "text": text}],
               "targets": [profiles[uri]["probabilities"]], "target_ast": None, "argument_annotations": [],
               "annotation_scope": "source-class weak supervision of expected profile; observed properties remain positive/unknown evidence",
               "evidence_kind": "expected_concept", "annotated_class": uri,
               "source_instance": item.instance_id, "source_release": item.release_id,
               "observed_property_positives": list(item.properties)}


def semantic_main(argv):
    """Prepare named supervision using the same source, AST and readings owners."""
    import gzip
    import hashlib
    from pathlib import Path
    import subprocess

    from engine.readers.layout import named_coordinate_contract
    from engine.readers.grammar import source_schema
    from engine.sql_ast import query_from_tree

    ap = argparse.ArgumentParser(description="Prepare masked named-coordinate examples from admitted programs.")
    ap.add_argument("--programs", required=True)
    ap.add_argument("--conversation", action="append", default=[])
    ap.add_argument("--bird")
    ap.add_argument("--semantic-corpus", help="pinned Schema.org semantic_instances.jsonl")
    ap.add_argument("--semantic-manifest", help="its semantic_manifest.json (required with --semantic-corpus)")
    ap.add_argument("--out-dir", required=True)
    add_arguments(ap)
    args = ap.parse_args(argv)
    sources = Sources.from_args(args)
    if args.bird:
        sources.add_bird(args.bird)
    contract = named_coordinate_contract()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from functools import lru_cache
    schemas = {}
    graph = lru_cache(maxsize=3)(sources.graph)
    source_programs = {}
    counts, positive = collections.Counter(), collections.Counter()
    databases = {"train": set(), "calibration": set(), "test": set()}
    identities, inputs = set(), []

    def digest(path):
        h = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def read(path):
        inputs.append({"path": str(Path(path).resolve()), "sha256": digest(path)})
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as stream:
            yield from map(json.loads, stream)

    for manifest in (args.source_databases, args.column_dictionaries):
        if manifest:
            inputs.append({"path": str(Path(manifest).resolve()), "sha256": digest(manifest)})

    expected, property_instances = None, None
    if args.semantic_corpus:
        from training.schema_org.corpus import _verify_splits
        from training.schema_org.instances import read_jsonl
        from training.schema_org.signatures import expected_observation_profiles

        if not args.semantic_manifest:
            raise ValueError("a semantic corpus requires its frozen manifest")
        manifest = json.loads(Path(args.semantic_manifest).read_text(encoding="utf-8"))
        if digest(args.semantic_corpus) != manifest["corpus"]["sha256"]:
            raise ValueError("semantic corpus hash differs from its frozen manifest")
        inputs.extend({"path": str(Path(path).resolve()), "sha256": digest(path)}
                      for path in (args.semantic_corpus, args.semantic_manifest))
        property_instances = list(read_jsonl(args.semantic_corpus))
        _verify_splits(property_instances)
        expected = expected_observation_profiles(property_instances, [r["name"] for r in contract["coordinates"]
                                                                      if r["block"] == "property"])
        (out / "expected-property-profiles.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")

    def schema(db):
        if db not in schemas:
            schemas[db] = source_schema(graph(db))
        return schemas[db]

    target = out / "semantic-examples.jsonl.gz"
    with gzip.open(target, "wt", encoding="utf-8", compresslevel=1, newline="\n") as sink:
        def write(row, family):
            if row["id"] in identities:
                raise ValueError("duplicate semantic example identity: " + row["id"])
            identities.add(row["id"])
            databases[row["split"]].add(row["db_id"])
            counts[row["split"] + "/" + family] += 1
            for labels in row["targets"]:
                positive.update({k: v for k, v in labels.items() if v})
            sink.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            if len(identities) % 2000 == 0:
                print(json.dumps({"prepared": len(identities), "counts": dict(counts)}), flush=True)

        for program in read(args.programs):
            source = program["id"].split("/", 1)[0]
            state = {"schema": schema(program["db_id"]),
                     "conversation": [{"role": "user", "content": program["original_question"]}]}
            query = query_from_tree(program["target"])
            source_programs[program["id"]] = (program["original_question"], query)
            write(semantic_example(state, identity="program/" + program["id"], source=source,
                                   db_id=program["db_id"], split=program["partition"], query=query,
                                   contract=contract), "program")
        for path in args.conversation:
            previous_questions = {}
            for row in read(path):
                binding = row["questions"]["action"]["criteria"]
                action = json.loads(binding[row["gold"]["action"]["label"]])
                query = None
                if action == "answer":
                    source_id = row["source"] + "/" + str(row["index"])
                    if source_id not in source_programs:
                        from training.readers.gold import gold_sql, normalize_source_grouping
                        from regress.sql_import import import_sql
                        original = sources.questions[row["source"]][int(row["index"])]
                        if original["db_id"] != row["db_id"]:
                            raise ValueError("generated source program belongs to another database")
                        source_graph = graph(row["db_id"])
                        query = normalize_source_grouping(import_sql(gold_sql(original), source_graph),
                            source_graph, sources.primary_keys(row["db_id"]))
                        source_programs[source_id] = (original["question"], query)
                        counts["recovered_original_source_programs"] += 1
                    original_question, query = source_programs[source_id]
                    query = generated_dialogue_query(row, query, original_question)
                state = {"schema": schema(row["db_id"]), "conversation": row["state"]["conversation"]}
                key = (row["source"], row["index"], row["db_id"])
                controls = generated_dialogue_controls(row, query, previous_questions.get(key), contract)
                if row["variant"] == "single":
                    if key in previous_questions:
                        raise ValueError("duplicate source question in generated conversation file")
                    previous_questions[key] = {k: row[k] for k in ("variant", "state", "target_readings")}
                write(semantic_example(state, identity="conversation/" + row["id"], source=row["source"],
                                       db_id=row["db_id"], split=row["partition"], query=query,
                                       action=action, contract=contract, dialogue_controls=controls), "conversation")
        if property_instances is not None:
            for row in semantic_property_examples(property_instances, expected, contract):
                write(row, "expected_property")
    overlap = sorted((databases["train"] & databases["calibration"])
                     | (databases["train"] & databases["test"]) | (databases["calibration"] & databases["test"]))
    assessment = {"passed": not overlap, "counts": dict(counts), "database_overlap": overlap,
                  "contract_sha256": contract["contract_sha256"], "inputs": inputs,
                  "examples_sha256": digest(target), "positive_labels": dict(sorted(positive.items())),
                  "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  "dirty_worktree": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
                  "unknown_blocks": (["property"] if expected is None else []) + ["ace_frame", "structure_type_role"],
                  "expected_property_classes": len(expected["profiles"]) if expected else 0,
                  "expected_property_quantized_collisions": expected["quantized_class_collisions"] if expected else [],
                  "scope": "Named SQL/action supervision; does not establish English-to-ACE semantic correctness or end-to-end inference accuracy."}
    (out / "coordinate-contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
    (out / "preparation-assessment.json").write_text(json.dumps(assessment, indent=2), encoding="utf-8")
    with gzip.open(target, "rt", encoding="utf-8") as stream:
        support = semantic_support(map(json.loads, stream), contract)
    (out / "supervision-support.json").write_text(json.dumps(support, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in assessment.items() if k not in ("positive_labels", "inputs")}), flush=True)
    if overlap:
        raise ValueError("training/calibration database overlap")


def semantic_grammar_main(argv):
    """One target-independent prefill per question; every grammar choice labelled."""
    import gzip
    import hashlib
    from functools import lru_cache
    from pathlib import Path
    from engine.readers.grammar import ASTGrammar
    from engine.readers.layout import named_coordinate_contract
    from engine.sql_ast import query_from_tree, query_to_tree

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--semantic-examples", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--bird")
    ap.add_argument("--positions", type=int, default=256)
    ap.add_argument("--source-lookup-seconds", type=float, default=5,
                    help="Offline input-only source lookup budget; incomplete reads fail closed")
    ap.add_argument("--limit-source-questions", type=int, default=0, help="Admission preflight only; zero visits all questions")
    add_arguments(ap)
    args = ap.parse_args(argv)
    if not 1 <= args.positions <= 1024:
        ap.error("--positions must be between 1 and 1024")
    if not 0 < args.source_lookup_seconds <= 60:
        ap.error("--source-lookup-seconds must be greater than zero and at most 60")
    import subprocess
    def file_sha(path):
        with Path(path).open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    code_paths = [Path(path) for path in (
        __file__, "training/readers/sources.py", "engine/readers/grammar.py",
        "engine/readers/decisions.py", "engine/readers/layout.py", "engine/readers/numerals.py",
        "engine/sql_ast.py", "engine/sql_schema.py", "engine/sql_dates.py", "engine/numeric.py")]
    code_hashes = {str(path): file_sha(path) for path in code_paths}
    source_files = {}
    sources = Sources.from_args(args)
    if args.bird: sources.add_bird(args.bird)
    @lru_cache(maxsize=3)
    def graph(db):
        path = Path(sources.db_path(db)).resolve()
        if db not in source_files:
            stat = path.stat()
            source_files[db] = {"path": str(path), "sha256": file_sha(path),
                                "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        return sources.graph(db)
    contract = named_coordinate_contract()
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    counts, failures, kinds, labels, parents, source_reads = (collections.Counter() for _ in range(6))
    split_ids = {key: set() for key in ("train", "calibration", "test")}
    source, target = Path(args.semantic_examples), out / "semantic-examples.jsonl.gz"
    with gzip.open(source, "rt", encoding="utf-8") as stream, gzip.open(target, "wt", encoding="utf-8", compresslevel=1) as sink, \
            gzip.open(out / "grammar-admission.jsonl.gz", "wt", encoding="utf-8") as audit:
        def emit(row, family):
            assert_no_evaluation_sources([row], "joint named reader preparation")
            split_ids[row["split"]].add(row["db_id"])
            counts[row["split"] + "/" + family] += 1
            for item in row["targets"]: labels.update(item)
            sink.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

        for row in map(json.loads, stream):
            if args.limit_source_questions and sum(parents.values()) >= args.limit_source_questions: break
            if row["target_ast"] is None and not row["input"]["conversation"]:
                emit(row, "expected_property")
                continue
            assert_no_evaluation_sources([row], "joint named reader preparation")
            action = next((key.rsplit(".", 1)[1] for key, value in row["targets"][0].items()
                           if key in {"dialogue.answer", "dialogue.clarify", "dialogue.unsupported"} and value == 1), None)
            if action not in {"answer", "clarify"}: raise ValueError("source has no supported annotated response action")
            parents[row["split"]] += 1
            input_evidence = []
            try:
                evidence_graph, input_evidence = sources.question_graph(
                    row["db_id"], row["input"]["conversation"],
                    seconds=args.source_lookup_seconds, graph=graph(row["db_id"]))
                source_reads["unavailable" if any(item.get("state") == "unavailable" for item in input_evidence)
                             else "rows_added" if input_evidence else "no_rows_added"] += 1
                controller = ASTGrammar(row["input"]["conversation"], evidence_graph)
                prepared, realized, decisions = controller.training_example(
                    query_from_tree(row["target_ast"]) if action == "answer" else None, action=action, positions=args.positions)
                prepared.update({key: row[key] for key in ("id", "source", "db_id", "split")})
                prepared["contract_sha256"] = contract["contract_sha256"]
                emit(prepared, "grammar")
                kinds.update(row["split"] + "/" + decision["kind"] for decision in decisions)
                audit.write(json.dumps({"id": row["id"], "state": "admitted", "decisions": len(decisions),
                                        "input_only_source_evidence": input_evidence,
                                        "grammar_sha256": controller.schema_sha256,
                                        "source_ast": row["target_ast"],
                                        "canonical_ast": query_to_tree(realized) if realized is not None else None}) + "\n")
            except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                reason = f"{type(exc).__name__}: {exc}"; failures[reason] += 1
                audit.write(json.dumps({"id": row["id"], "state": "rejected", "reason": reason,
                                        "input_only_source_evidence": input_evidence,
                                        "missing_binding": getattr(exc, "missing_binding", None)}) + "\n")
            if sum(parents.values()) % 250 == 0:
                sink.flush(); audit.flush()
                print(json.dumps({"parents": dict(parents), "records": sum(counts.values()), "rejected": sum(failures.values()),
                                  "failure_families": failures.most_common(8)}), flush=True)
    overlap = set.intersection(split_ids["train"], split_ids["calibration"])
    assessment = {"counts": dict(counts), "parent_questions": dict(parents), "field_kinds": dict(kinds),
                  "rejections": dict(failures), "coordinate_targets": dict(labels), "database_overlap": sorted(overlap),
                  "source_sha256": file_sha(source),
                  "examples_sha256": file_sha(target),
                  "contract_sha256": contract["contract_sha256"], "parallel_positions": args.positions,
                  "source_lookup_seconds": args.source_lookup_seconds,
                  "input_only_source_lookup": dict(source_reads),
                  "source_files": source_files,
                  "source_files_unchanged": all((Path(record["path"]).stat().st_size,
                      Path(record["path"]).stat().st_mtime_ns) == (record["bytes"], record["mtime_ns"])
                      for record in source_files.values()),
                  "code_sha256": code_hashes,
                  "code_files_unchanged": all(file_sha(path) == value for path, value in code_hashes.items()),
                  "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  "dirty_worktree": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
                  "encoder_calls": 0, "training_authorized": False, "independent_english_meaning_certified": False,
                  "source_limit": args.limit_source_questions,
                  "source_database_manifest": ({"path": str(Path(args.source_databases).resolve()),
                      "sha256": hashlib.sha256(Path(args.source_databases).read_bytes()).hexdigest()}
                      if args.source_databases else None),
                  "column_dictionary_manifest": ({"path": str(Path(args.column_dictionaries).resolve()),
                      "sha256": hashlib.sha256(Path(args.column_dictionaries).read_bytes()).hexdigest()}
                      if args.column_dictionaries else None),
                  "scope": "One input-only prefill per question. No gold AST prefix or target-dependent field/position bank. Typed grammar supplies legal output masks only.",
                  "expiry": "Unpromoted candidate data; promote one accepted model or remove."}
    (out / "preparation-assessment.json").write_text(json.dumps(assessment, indent=2), encoding="utf-8")
    (out / "coordinate-contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
    print(json.dumps({key: assessment[key] for key in ("counts", "parent_questions", "rejections", "examples_sha256")}), flush=True)
    if not assessment["code_files_unchanged"] or not assessment["source_files_unchanged"]:
        raise ValueError("code or source files changed during preparation")
    if overlap: raise ValueError("joint readout preparation leaked databases across partitions")


def main():
    if sys.argv[1:2] == ["semantic"]:
        return semantic_main(sys.argv[2:])
    if sys.argv[1:2] == ["semantic-grammar"]:
        return semantic_grammar_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description="Stage-1 and stage-2 training examples from gold readings.")
    ap.add_argument("train", help="readings_train.jsonl")
    ap.add_argument("calibration", help="readings_calibration.jsonl")
    ap.add_argument("out_dir")
    ap.add_argument("--bird", help="BIRD's train directory; requires --bird-readings")
    ap.add_argument("--bird-readings", help="comma-separated BIRD readings files (readings_bird.jsonl[,shares])")
    ap.add_argument("--syn", help="Spider-Syn's directory; requires --syn-readings")
    ap.add_argument("--syn-readings")
    ap.add_argument("--synsql", help="training.readers.synsql's selection folder; requires --synsql-databases and "
                                     "--synsql-readings")
    ap.add_argument("--synsql-databases", help="SynSQL-2.5M's databases folder")
    ap.add_argument("--synsql-readings", help="the selection's readings (training.readers.gold synsql)")
    ap.add_argument("--reworded", help="comma-separated training.readers.rewordings sets' rewordings.jsonl, "
                                       "concatenated in order; requires --reworded-readings")
    ap.add_argument("--reworded-readings", help="their readings_reworded.jsonl, in the same order")
    ap.add_argument("--lookups", help="training.readers.lookups' output folder (lookup-table questions)")
    ap.add_argument("--structures", help="comma-separated training.readers.structures output folders, in order")
    ap.add_argument("--no-anchors", action="store_true", help="serialize stage 2 without the question's values")
    ap.add_argument("--tokenizer", default=TOKENIZER, help="the readers' tokenizer, for the token budget")
    add_arguments(ap)
    args = ap.parse_args()
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    data = Sources.from_args(args)
    sources = [(args.train, "train"), (args.calibration, "calibration")]
    if args.bird:
        data.add_bird(args.bird)
        for path in args.bird_readings.split(","):
            sources.insert(1, (path, "train"))
    if args.syn:
        data.add_syn(args.syn)
        sources.insert(1, (args.syn_readings, "train"))
    if args.synsql:
        data.add_synsql(args.synsql, args.synsql_databases)
        sources.insert(1, (args.synsql_readings, "train"))
    if args.reworded:
        merged = data.add_reworded(args.reworded.split(","), args.reworded_readings.split(","), args.out_dir)
        sources.insert(1, (merged, "train"))
    if args.lookups:
        data.add_lookups(args.lookups)
        sources.insert(1, (os.path.join(args.lookups, "readings_lookup.jsonl"), "train"))
    if args.structures:
        merged = data.add_structures(args.structures.split(","), args.out_dir)
        sources.insert(1, (merged, "train"))
    anchored = not args.no_anchors
    os.makedirs(args.out_dir, exist_ok=True)
    schemas = {}
    counts = collections.Counter()
    with open(os.path.join(args.out_dir, "stage1.jsonl"), "w", encoding="utf-8", newline="\n") as stage1, \
            open(os.path.join(args.out_dir, "stage2.jsonl"), "w", encoding="utf-8", newline="\n") as stage2:
        for path, split in sources:
            for line in open(path, encoding="utf-8"):
                record = json.loads(line)
                assert_no_evaluation_sources([record], "gold readings")
                if "readings" not in record:
                    continue
                record["split"] = split
                db_id = record["db_id"]
                if db_id not in schemas:
                    schemas[db_id] = Schema(data.graph(db_id))
                schema = schemas[db_id]
                question = data.questions[record["source"]][record["index"]]["question"]
                roles, used_tables = roles_of(record["readings"])
                for table in schema.tables:
                    text, spans = table_text(schema, table)
                    stage1.write(json.dumps({
                        "split": split, "db_id": db_id, "source": record["source"], "index": record["index"],
                        "question": question, "table": table, "text": text, "table_used": int(table in used_tables),
                        "columns": [{"start": s, "end": e, "name": c, "used": int(f"{table}.{c}" in roles),
                                     "roles": [int(r in roles.get(f"{table}.{c}", ())) for r in ROLES]}
                                    for c, s, e in spans]}) + "\n")
                    counts["stage1"] += 1
                rng = random.Random(f"{record['source']}:{record['index']}")
                tables, columns_of, droppable = gold_pruning(record, schema, roles, used_tables, rng)
                row = reading_example(record, question, schema, tables, columns_of, droppable, tokenizer, anchored)
                stage2.write(json.dumps(row) + "\n")
                counts["stage2"] += 1
                counts["values"] += len(row["values"]) + row["missing_values"]
                counts["values_found"] += len(row["values"])
    print(f"stage1 {counts['stage1']} / stage2 {counts['stage2']} examples -> {args.out_dir}; "
          f"literal values found {counts['values_found']}/{counts['values']}")


if __name__ == "__main__":
    main()
