"""Build Spider supervision for deterministic AST sketch and schema-link proposals.

Unlike the Phase 6 ranker cache, every gold example contributes a target even
when the current grammar cannot generate a correct candidate.  Databases, not
individual questions, are split between train and validation.

Run from the repository root:

  python spider/probe/build_ast_proposal_data.py
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys
from typing import Any, Iterable, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from .ast_profile import canonical_name, profile_spider_sql
    from .hardness import WHERE_OPS, eval_hardness
except ImportError:  # direct script execution
    from ast_profile import canonical_name, profile_spider_sql
    from hardness import WHERE_OPS, eval_hardness


DATASET_VERSION = 1


def split_database_ids(
    database_ids: Iterable[str], validation_ratio: float, seed: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return deterministic, database-disjoint train and validation IDs."""
    ids = sorted(set(str(database_id) for database_id in database_ids))
    if len(ids) < 2:
        raise ValueError("proposal supervision requires at least two databases")
    if not 0.0 < validation_ratio < 1.0:
        raise ValueError("validation_ratio must be between zero and one")
    ordered = sorted(
        ids,
        key=lambda database_id: hashlib.sha256(
            f"{seed}:{database_id}".encode("utf-8")
        ).hexdigest(),
    )
    validation_count = min(len(ids) - 1, max(1, round(len(ids) * validation_ratio)))
    validation = tuple(sorted(ordered[:validation_count]))
    train = tuple(sorted(set(ids) - set(validation)))
    return train, validation


def schema_record(metadata: Mapping[str, Any], split: str) -> dict[str, Any]:
    """Serialize one schema without cell values or benchmark answers."""
    table_names = metadata["table_names_original"]
    column_names = metadata["column_names_original"]
    column_types = metadata.get("column_types", ())
    tables = [
        {
            "index": index,
            "name": canonical_name(name),
            "original_name": name,
            "columns": [],
        }
        for index, name in enumerate(table_names)
    ]
    qualified = {}
    for index, (table_index, column_name) in enumerate(column_names):
        if table_index < 0:
            continue
        item = {
            "index": index,
            "name": canonical_name(column_name),
            "original_name": column_name,
            "type": str(column_types[index]) if index < len(column_types) else "unknown",
        }
        tables[table_index]["columns"].append(item)
        qualified[index] = (
            f"{canonical_name(table_names[table_index])}.{canonical_name(column_name)}"
        )

    foreign_keys = []
    for left, right in metadata.get("foreign_keys", ()):
        if left in qualified and right in qualified:
            foreign_keys.append({"from": qualified[left], "to": qualified[right]})
    primary_keys = [
        qualified[index]
        for index in metadata.get("primary_keys", ())
        if index in qualified
    ]
    return {
        "version": DATASET_VERSION,
        "db_id": str(metadata["db_id"]),
        "split": split,
        "tables": tables,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
    }


def extract_literal_targets(
    sql: Mapping[str, Any], metadata: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Extract recursive predicate and LIMIT values with their bound columns."""
    table_names = metadata["table_names_original"]
    column_names = metadata["column_names_original"]
    targets = []

    def is_column_unit(value: Any) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) >= 3
            and isinstance(value[0], int)
            and isinstance(value[1], int)
            and isinstance(value[2], bool)
        )

    def bound_column(value_unit: Any) -> str | None:
        if not isinstance(value_unit, (list, tuple)) or len(value_unit) < 2:
            return None
        for column_unit in value_unit[1:3]:
            if not is_column_unit(column_unit):
                continue
            column_index = column_unit[1]
            if not 0 <= column_index < len(column_names):
                continue
            table_index, column_name = column_names[column_index]
            if 0 <= table_index < len(table_names):
                return f"{canonical_name(table_names[table_index])}.{canonical_name(column_name)}"
        return None

    def add_value(
        value: Any,
        clause: str,
        operator: str,
        column: str | None,
        negated: bool,
    ) -> None:
        if value is None or is_column_unit(value):
            return
        if isinstance(value, Mapping):
            visit(value)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add_value(item, clause, operator, column, negated)
            return
        targets.append({
            "clause": clause,
            "operator": operator,
            "column": column,
            "value": value,
            "value_type": type(value).__name__,
            "negated": bool(negated),
        })

    def conditions(values: Sequence[Any], clause: str) -> None:
        for condition in values[::2]:
            if not isinstance(condition, (list, tuple)) or len(condition) < 5:
                continue
            negated, operator_id, value_unit, first, second = condition[:5]
            operator = (
                WHERE_OPS[operator_id]
                if isinstance(operator_id, int) and 0 <= operator_id < len(WHERE_OPS)
                else "unknown"
            )
            column = bound_column(value_unit)
            add_value(first, clause, operator, column, bool(negated))
            add_value(second, clause, operator, column, bool(negated))

    def visit(block: Mapping[str, Any]) -> None:
        conditions(block.get("where", ()), "where")
        conditions(block.get("having", ()), "having")
        limit = block.get("limit")
        if limit is not None:
            add_value(limit, "limit", "limit", None, False)
        for kind, value in block.get("from", {}).get("table_units", ()):
            if kind != "table_unit" and isinstance(value, Mapping):
                visit(value)
        for operator in ("intersect", "union", "except"):
            branch = block.get(operator)
            if isinstance(branch, Mapping):
                visit(branch)

    visit(sql)
    return targets


def example_record(
    index: int,
    example: Mapping[str, Any],
    metadata: Mapping[str, Any],
    split: str,
) -> dict[str, Any]:
    """Build one gold structural target without candidate-generation bias."""
    profile = profile_spider_sql(example["sql"], metadata)
    target = profile.to_dict()
    target["literals"] = extract_literal_targets(example["sql"], metadata)
    return {
        "version": DATASET_VERSION,
        "example_id": f"{example['db_id']}:{index:05d}",
        "db_id": str(example["db_id"]),
        "split": split,
        "question": str(example["question"]),
        "difficulty": eval_hardness(example["sql"]),
        "target": target,
    }


def _write_jsonl(path: str, records: Sequence[Mapping[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_summary(
    records: Sequence[Mapping[str, Any]],
    known_sketches: set[str] | None = None,
) -> dict[str, Any]:
    difficulties: collections.Counter[str] = collections.Counter()
    features: collections.Counter[str] = collections.Counter()
    role_links: collections.Counter[str] = collections.Counter()
    literal_targets: collections.Counter[str] = collections.Counter()
    sketches: collections.Counter[str] = collections.Counter()
    database_ids = set()
    for record in records:
        database_ids.add(str(record["db_id"]))
        difficulties[str(record["difficulty"])] += 1
        target = record["target"]
        sketch_key = json.dumps(target["sketch"], sort_keys=True)
        sketches[sketch_key] += 1
        features.update({name: int(value) for name, value in target["sketch"].items()})
        for role, columns in target["roles"].items():
            role_links[role] += len(columns)
        for literal in target.get("literals", ()):
            literal_targets[str(literal["clause"])] += 1
    unseen = 0
    if known_sketches is not None:
        unseen = sum(count for sketch, count in sketches.items() if sketch not in known_sketches)
    return {
        "examples": len(records),
        "databases": len(database_ids),
        "difficulty": dict(sorted(difficulties.items())),
        "unique_sketches": len(sketches),
        "examples_with_sketch_unseen_in_train": unseen,
        "sketch_feature_counts": dict(features.most_common()),
        "schema_role_link_counts": dict(role_links.most_common()),
        "literal_target_counts": dict(literal_targets.most_common()),
    }


def build_manifest(
    source: str,
    schemas: Sequence[Mapping[str, Any]],
    train: Sequence[Mapping[str, Any]],
    validation: Sequence[Mapping[str, Any]],
    seed: int,
    validation_ratio: float,
) -> dict[str, Any]:
    train_sketches = {
        json.dumps(record["target"]["sketch"], sort_keys=True)
        for record in train
    }
    column_counts = [
        sum(len(table["columns"]) for table in schema["tables"])
        for schema in schemas
    ]
    return {
        "version": DATASET_VERSION,
        "objective": "deterministic SQL AST sketch and role-aware schema-link proposals",
        "source": os.path.relpath(source, ROOT).replace("\\", "/"),
        "source_sha256": _file_sha256(source),
        "seed": seed,
        "validation_ratio": validation_ratio,
        "database_disjoint": not (
            {record["db_id"] for record in train}
            & {record["db_id"] for record in validation}
        ),
        "all_examples_supervised": len(train) + len(validation),
        "schemas": {
            "databases": len(schemas),
            "tables": sum(len(schema["tables"]) for schema in schemas),
            "columns": sum(column_counts),
            "average_columns_per_database": round(sum(column_counts) / max(len(column_counts), 1), 2),
        },
        "train": _split_summary(train),
        "validation": _split_summary(validation, train_sketches),
        "notes": [
            "Targets come from parsed gold SQL, not from the current candidate pool.",
            "Schema records contain names, types, keys, and foreign keys but no database cell dump.",
            "Predicate literals and LIMIT values are supervised with their bound column and operator.",
            "Validation databases are absent from train so schema-link metrics test transfer.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    data = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
    results = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results"))
    parser.add_argument("--source", default=os.path.join(data, "train_spider.json"))
    parser.add_argument("--tables", default=os.path.join(data, "tables.json"))
    parser.add_argument("--train-out", default=os.path.join(data, "ast_proposals_train.jsonl"))
    parser.add_argument("--validation-out", default=os.path.join(data, "ast_proposals_validation.jsonl"))
    parser.add_argument("--schemas-out", default=os.path.join(data, "ast_proposal_schemas.jsonl"))
    parser.add_argument("--manifest", default=os.path.join(results, "ast_proposal_data.json"))
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=1729)
    args = parser.parse_args()

    with open(args.source, encoding="utf-8") as handle:
        examples = json.load(handle)
    with open(args.tables, encoding="utf-8") as handle:
        metadata = {item["db_id"]: item for item in json.load(handle)}
    source_databases = {str(example["db_id"]) for example in examples}
    train_ids, validation_ids = split_database_ids(
        source_databases, args.validation_ratio, args.seed
    )
    train_set, validation_set = set(train_ids), set(validation_ids)

    schemas = [
        schema_record(
            metadata[database_id],
            "train" if database_id in train_set else "validation",
        )
        for database_id in sorted(source_databases)
    ]
    train_records = []
    validation_records = []
    for index, example in enumerate(examples):
        database_id = str(example["db_id"])
        split = "train" if database_id in train_set else "validation"
        record = example_record(index, example, metadata[database_id], split)
        (train_records if split == "train" else validation_records).append(record)

    _write_jsonl(args.schemas_out, schemas)
    _write_jsonl(args.train_out, train_records)
    _write_jsonl(args.validation_out, validation_records)
    manifest = build_manifest(
        args.source,
        schemas,
        train_records,
        validation_records,
        args.seed,
        args.validation_ratio,
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.manifest)), exist_ok=True)
    with open(args.manifest, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
