"""Source/semantic evidence audit using the existing Enron replay's saved records.

This is not an evaluator, a label generator for Enron training, or an inference
path. No model is loaded and no answer is selected here. Existing replay verdicts
are retained verbatim. Source cells, coverage gaps and reference assumptions are
recorded before a shared-interface candidate is fitted. Optional execution
replays an already selected SQL AST through the existing deterministic owner;
it does not select an answer or recompute model accuracy.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import dataclasses
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from engine.provenance import InputLineage, ProvenanceContext
from engine.sql_ast import SQLType
from engine.sql_schema import SchemaGraph
from engine.tables import TableQuery, csv_table, normalize_tables
from regress.sql_import import Unsupported, import_sql


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def reference_requirements(code):
    """Read declared reference-code dependencies without executing its Python."""
    try:
        tree = ast.parse(code or "", mode="eval")
    except SyntaxError as exc:
        return {"state": "unparsed_reference", "error": str(exc)}
    fields, functions, tables = set(), set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            fields.add(node.slice.value)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                functions.add(node.func.id)
                if node.func.id == "T":
                    try:
                        tables.append([ast.literal_eval(value) for value in node.args])
                    except (ValueError, TypeError):
                        tables.append(["dynamic_reference"])
            elif isinstance(node.func, ast.Attribute):
                functions.add(node.func.attr)
    return {"state": "reference_dependencies_only", "fields": sorted(fields),
            "functions": sorted(functions), "table_calls": tables,
            "not_training_labels": True}


def query_nodes(value):
    if dataclasses.is_dataclass(value):
        out = {type(value).__name__}
        for field in dataclasses.fields(value):
            out.update(query_nodes(getattr(value, field.name)))
        return out
    if isinstance(value, (tuple, list)):
        return set().union(*(query_nodes(v) for v in value)) if value else set()
    return set()


def selected_execution_trace(base, tables, graph, query, *, selected_result=None):
    """Audit execution of a saved selection, with no model or reference answers.

    Observed SchemaGraph types are explicit replay inputs. This does not claim
    to reproduce the original model's schema inference or named decisions.
    """
    from engine.deterministic.context import analysis_execution_context, current_execution_record
    from engine.deterministic.lower import lower_select_query
    from engine.deterministic.runtime import assert_equivalent
    from engine.numeric import wire_document
    from engine.prepared import scratch_database

    normalized = normalize_tables(tables)
    tablemap = {table["name"]: table for table in normalized}
    schema = [{"table": col.ref.table, "name": col.ref.name,
               "affinity": {SQLType.INTEGER: "INTEGER", SQLType.REAL: "REAL",
                            SQLType.BOOLEAN: "INTEGER"}.get(col.ref.type, "TEXT"),
               "is_date": col.ref.type == SQLType.DATE, "values": col.values}
              for col in graph.columns]
    plan = lower_select_query("enron_" + base["id"].replace(".", "_"), query, schema, [])
    context = ProvenanceContext(tables, uploaded_count=len(tables))
    planner = TableQuery()
    with scratch_database(), analysis_execution_context(None, "enron-source-audit", execution_mode="verify"):
        columns, rows = planner.execute(tablemap, schema, base["sql"], query=query, deterministic_plan=plan)
        record = current_execution_record()
        direct_columns, direct = planner.execute(tablemap, schema, base["sql"], query=query)
        # Positional column names make this independent of emitter alias labels.
        left = tuple({str(i): wire_document(value) for i, value in enumerate(row)} for row in rows)
        right = tuple({str(i): wire_document(value) for i, value in enumerate(row)} for row in direct)
        assert_equivalent(left, right, ordered=bool(query.order_by))
        answer = wire_document({"columns": direct_columns, "rows": direct})
        if selected_result is not None and answer != wire_document(
                {"columns": selected_result["columns"], "rows": selected_result["rows"]}):
            raise ValueError("source execution does not reproduce the selected answer")
        trace = context.execution_lineage(record["source_lineage"])
    coverage = trace["input_lineage_coverage"]
    complete = not coverage["unknown_source_keys"] and not coverage["unknown_output_row_occurrences"]
    from engine.sql_ast import query_to_tree
    return {"state": "execution_sources_resolved" if complete else "execution_sources_incomplete",
            "mode": record["mode"], "selected_ast_sql_parity": True,
            "selected_ast_sha256": hashlib.sha256(json.dumps(query_to_tree(query), sort_keys=True,
                                                             separators=(",", ":")).encode()).hexdigest(),
            "selected_answer_reproduced": selected_result is not None,
            "answer_sha256": hashlib.sha256(json.dumps(answer, sort_keys=True,
                                                       separators=(",", ":")).encode()).hexdigest(),
            "columns": columns, "rows": len(rows), "output_stage": str(plan.output),
            "baseline_row_count_reproduced": len(rows) == base.get("rows"),
            "source_lineage": trace,
            "schema_source": "SchemaGraph.from_tables observed types; not original model schema predictions",
            "learned_named_dimensions_causally_traced": False}


def audit(*, records_path, evidence_zip, tables_dir, coordinate_path, output, execute_selected=False):
    output.mkdir(parents=True, exist_ok=True)
    reference = {f"{r['workbook_n']}.{r['order']}": r for r in
                 (json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines())}
    with zipfile.ZipFile(evidence_zip) as archive:
        baseline_bytes = archive.read("evaluation/enron-results/baseline-records.jsonl")
        baseline_contract = json.loads(archive.read("evaluation/enron-results/baseline-contract.json"))
        assessment = json.loads(archive.read("evaluation/enron-results/baseline-assessment.json"))
    baseline = [json.loads(line) for line in baseline_bytes.splitlines()]
    assert len(baseline) == len({r["id"] for r in baseline}) == 186
    assert hashlib.sha256(baseline_bytes).hexdigest() == assessment["records_sha256"]
    assert all(row["mode"] == "all" for row in baseline), "oracle-tab replay is not a served baseline"
    coordinates = json.loads(coordinate_path.read_text(encoding="utf-8"))
    manifests, imported, issues = {}, {}, []
    with gzip.open(output / "input-lineage.jsonl.gz", "wt", encoding="utf-8") as sink:
        for book in sorted({int(row["id"].split(".")[0]) for row in baseline}):
            path = tables_dir / f"{book}.json"
            source = json.loads(path.read_text(encoding="utf-8"))
            tables, headers = [], set()
            sheets = source.get("sheets", []) if source.get("ok") else []
            for index, sheet in enumerate(sheets):
                table = csv_table(sheet["csv"], sheet["name"])
                if sheet.get("import") is not None:
                    table["import"] = sheet["import"]
                tables.append(table)
                headers.update(table["columns"])
                for binding in sheet.get("import", {}).get("columnBindings", []):
                    if binding.get("originalHeader"):
                        headers.add(binding["originalHeader"].strip())
                if not table["columns"]:
                    issues.append({"workbook": book, "sheet": sheet["name"], "state": "no_imported_columns"})
                    continue
                lineage = InputLineage(table, source_id=f"enron/import/{book}/sheet/{index}",
                                       import_record=sheet.get("import"))
                sink.write(json.dumps({"workbook": book, "sheet": sheet["name"],
                                       "input_lineage": lineage.record(), "import": sheet.get("import"),
                                       "source_file_sha256": sha(path),
                                       "source_csv_sha256": hashlib.sha256(sheet["csv"].encode()).hexdigest(),
                                       "semantic_profiles": None}, ensure_ascii=False) + "\n")
            imported[book] = (tables, headers)
            manifests[str(book)] = {"path": str(path), "sha256": sha(path), "tables": len(tables)}
    counts, dependency_gaps, execution_counts, execution_failures = Counter(), Counter(), Counter(), Counter()
    with gzip.open(output / "question-evidence.jsonl.gz", "wt", encoding="utf-8") as sink:
        for base in baseline:
            record = reference[base["id"]]
            book = record["workbook_n"]
            tables, headers = imported[book]
            requirements = reference_requirements(record.get("expected_code"))
            gaps = sorted(set(requirements.get("fields", [])) - headers - {"_row"})
            dependency_gaps.update(gaps)
            node_types, import_error, query = [], None, None
            if base.get("sql"):
                try:
                    graph = SchemaGraph.from_tables(normalize_tables(tables), [])
                    query = import_sql(base["sql"], graph)
                    node_types = sorted(query_nodes(query))
                except (Unsupported, ValueError, KeyError, TypeError, RuntimeError) as exc:
                    import_error = f"{type(exc).__name__}: {exc}"
            execution = None
            if execute_selected:
                if not base.get("sql"):
                    execution = {"state": "no_saved_selection"}
                elif query is None:
                    execution = {"state": "selected_sql_import_failed", "reason": import_error}
                else:
                    try:
                        execution = selected_execution_trace(base, tables, graph, query)
                    except Exception as exc:  # Every failed diagnostic is retained; never counted as a pass.
                        execution = {"state": "selected_execution_failed", "reason": f"{type(exc).__name__}: {exc}"}
                execution_counts[execution["state"]] += 1
                if execution.get("reason"):
                    execution_failures[execution["reason"]] += 1
            counts[base["verdict"]] += 1
            counts["baseline_pool_has_right"] += int(base.get("pool_right_rank") is not None)
            row = {"id": base["id"], "question": record["question"], "workbook": book,
                   "tab": record["tab"], "reference_assumptions": record.get("assumptions"),
                   "reference_requirements": requirements,
                   "reference_fields_not_exactly_named_in_import": gaps,
                   "gold_survived_import": record.get("gold_survived_import"),
                   "existing_baseline": base, "selected_ast_node_types": node_types,
                   "selected_sql_import_error": import_error,
                   "coordinate_contract_sha256": sha(coordinate_path),
                   "source_tables": manifests[str(book)],
                   "named_profile_predictions": None, "named_binding_trace": None,
                   "answer_contributor_trace": execution,
                   "interpretability_state": "baseline_bypasses_shared_interface; not_a_named_model_trace",
                   "usage": "studied_development_diagnostic; do_not_admit_to_training_or_claim_unseen_accuracy"}
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {"questions": len(baseline), "workbooks": len(imported),
               "baseline_counts_and_pool_recall": dict(counts), "baseline_contract": baseline_contract,
               "named_predictions_computed": False, "new_model_accuracy": None,
               "selected_execution_counts": dict(execution_counts),
               "selected_execution_failures": dict(execution_failures),
               "coordinate_slots": coordinates["proposed_slots"],
               "coordinate_names": coordinates["named_slots"], "unassigned_slots": coordinates["unassigned_slots"],
               "import_issues": issues, "reference_field_name_gaps": dict(dependency_gaps),
               "sources": {str(records_path): sha(records_path), str(evidence_zip): sha(evidence_zip),
                           str(coordinate_path): sha(coordinate_path)},
               "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
               "dirty_worktree": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
               "code_sha256": {str(p): sha(p) for p in [Path(__file__), Path("engine/provenance.py"),
                                                       Path("engine/schema_decode.py"), Path("engine/readers/decisions.py"),
                                                       Path("engine/deterministic/operators.py"), Path("engine/deterministic/runtime.py"),
                                                       Path("engine/deterministic/service.py"), Path("engine/deterministic/lower.py"),
                                                       Path("engine/prepared.py"), Path("engine/tables.py")]},
               "outputs": {str(output / name): sha(output / name)
                           for name in ("input-lineage.jsonl.gz", "question-evidence.jsonl.gz")},
               "limits": "Reuses saved all-tab baseline verdicts; no fresh inference. Optional execution uses observed types and already selected SQL, not model predictions. Source-row provenance is not minimal per-cell influence or learned named-dimension proof. Reference-name gaps are diagnostics, not semantic negatives. No source scopes inferred from expected answers."}
    (output / "assessment.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--evidence-zip", type=Path, required=True)
    parser.add_argument("--tables", type=Path, required=True)
    parser.add_argument("--coordinates", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute-selected", action="store_true", help="Replay saved selections through the existing dual executor")
    args = parser.parse_args()
    result = audit(records_path=args.records, evidence_zip=args.evidence_zip, tables_dir=args.tables,
                   coordinate_path=args.coordinates, output=args.out, execute_selected=args.execute_selected)
    print(json.dumps({k: result[k] for k in ("questions", "workbooks", "baseline_counts_and_pool_recall",
                                            "coordinate_slots", "coordinate_names", "unassigned_slots")}))


def source_columns(tree, scope=None):
    """Yield AST column objects and physical tables with lexical SQL alias scopes.

    A derived relation has no one physical table; its output references are
    excluded from physical metadata comparisons while its own query is visited.
    """
    scope = {} if scope is None else scope
    if isinstance(tree, dict):
        if tree.get("node") == "SelectQuery":
            scope = dict(scope)
            table = tree["from_table"]
            if isinstance(table, str):
                scope[(tree.get("from_alias") or table).casefold()] = table
            else:
                scope[table["alias"].casefold()] = None
            for join in tree.get("joins", []):
                table = join["table"]
                if isinstance(table, str):
                    scope[(join.get("alias") or table).casefold()] = table
                else:
                    scope[table["alias"].casefold()] = None
        if tree.get("node") == "ColumnRef":
            yield tree, scope.get(tree["table"].casefold(), tree["table"])
        for value in tree.values():
            yield from source_columns(value, scope)
    elif isinstance(tree, list):
        for value in tree:
            yield from source_columns(value, scope)


def explicit_constraint_issues(turns, tree):
    """Find reviewable disagreements with explicit numeric English bounds.

    This is a conservative source-label diagnostic, not an English parser or
    a passing semantic verdict. Only a single explicit bound and a single
    corresponding numeric comparison can support an operator correction.
    """
    import re
    from engine.numeric import parse_decimal
    from engine.readers.numerals import intervals, mentions
    from engine import sql_ast as sql
    if tree is None:
        return []
    text = "\n".join(turn["content"] for turn in turns if turn["role"] == "user")
    meanings = {"no more than": "<=", "at most": "<=", "less than or equal to": "<=",
                "equal or below": "<=", "equal to or below": "<=", "on and above": ">=", "equal or above": ">=",
                "no less than": ">=", "at least": ">=", "greater than or equal to": ">=",
                "more than": ">", "greater than": ">", "higher than": ">", "over": ">", "above": ">",
                "less than": "<", "fewer than": "<", "lower than": "<", "below": "<", "under": "<"}
    phrases = "|".join(re.escape(p) for p in sorted(meanings, key=lambda p: (-len(p), p)))
    claims = []
    suffixes = {"and above": ">=", "or more": ">=", "and over": ">=",
                "and below": "<=", "or less": "<="}
    for raw, start, end in mentions(text):
        prefix = re.search(r"\b(" + phrases + r")\s+$", text[:start], re.IGNORECASE)
        suffix = re.match(r"\s+(?:[a-z%]+\s+){0,3}(and above|or more|and over|and below|or less)\b",
                          text[end:], re.IGNORECASE)
        if prefix and suffix:
            continue
        if not prefix and not suffix:
            continue
        claim_start = prefix.start() if prefix else start
        claim_end = end if prefix else end + suffix.end()
        if re.search(r"\bnot(?:\s+\w+){0,3}\s*$", text[max(0, claim_start - 30):claim_start], re.IGNORECASE):
            continue
        try:
            value = parse_decimal(raw)
        except (ValueError, TypeError):
            continue
        operator = meanings[prefix[1].lower()] if prefix else suffixes[suffix[1].lower()]
        claims.append({"value": value, "operator": operator,
                       "text": text[claim_start:claim_end], "span": [claim_start, claim_end]})
    comparisons = []
    def visit(node, path, negative=False, scope=None, conjunctive=True):
        if dataclasses.is_dataclass(node):
            if isinstance(node, sql.SelectQuery):
                scope = path
                conjunctive = True
            if isinstance(node, sql.BooleanExpr) and node.operator == "OR":
                conjunctive = False
            if not negative and isinstance(node, sql.Comparison) and isinstance(node.right, sql.Literal):
                if sql.value_type(node.left).numeric:
                    try:
                        comparisons.append({"path": path, "value": parse_decimal(node.right.value), "operator": node.operator,
                                            "column": node.left if isinstance(node.left, sql.ColumnRef) else None,
                                            "scope": scope, "conjunctive": conjunctive})
                    except (ValueError, TypeError):
                        pass
            for field in dataclasses.fields(node):
                name = field.name
                complement = ((isinstance(node, sql.SetQuery) and node.operator == "EXCEPT" and name == "right")
                              or (isinstance(node, (sql.InPredicate, sql.ExistsPredicate)) and node.negated and name in {"query", "source"}))
                visit(getattr(node, name), path + "." + name, negative ^ bool(complement), scope, conjunctive)
        elif isinstance(node, (tuple, list)):
            for i, value in enumerate(node):
                visit(value, path + f"[{i}]", negative, scope, conjunctive)
    visit(sql.query_from_tree(tree), "$")
    issues = []
    for claim in claims:
        matches = [c for c in comparisons if c["value"] == claim["value"]]
        unique = len(matches) == 1 and sum(c["value"] == claim["value"] for c in claims) == 1
        if unique and matches[0]["operator"] != claim["operator"]:
            issues.append({"family": "explicit_numeric_operator_disagreement", "path": matches[0]["path"],
                           "english": claim["text"], "span": claim["span"], "literal": str(claim["value"]),
                           "source_operator": matches[0]["operator"], "expected_operator": claim["operator"],
                           "state": "independent_review_required"})
    # A unique written interval and a unique same-column SQL interval permit a
    # useful disagreement diagnostic without guessing which measure was meant.
    # Keep scopes separate: reusing a table name in a subquery is not one bound.
    written = list(intervals(text))
    numeric_mentions = list(mentions(text))
    other_ranges = sum(bool(re.fullmatch(r"\s*(?:to|through|[-–—])\s*", text[first[2]:last[1]], re.IGNORECASE))
                       for first, last in zip(numeric_mentions, numeric_mentions[1:]))
    groups = {}
    for comparison in comparisons:
        if (comparison["conjunctive"] and comparison["column"] is not None
                and comparison["operator"] in {">", ">=", "<", "<="}):
            groups.setdefault((comparison["scope"], comparison["column"]), []).append(comparison)
    bounded = []
    for candidates in groups.values():
        low = [item for item in candidates if item["operator"] in {">", ">="}]
        high = [item for item in candidates if item["operator"] in {"<", "<="}]
        if len(low) == len(high) == 1:
            bounded.append((low[0], high[0]))
    if len(written) == len(bounded) == 1 and other_ranges <= 1:
        low, high, start, end = written[0]
        negated = re.search(r"\bnot(?:\s+\w+){0,3}\s+(?:from|between)\s*$",
                            text[max(0, start - 50):start], re.IGNORECASE)
        if low <= high and not negated:
            source_low, source_high = bounded[0]
            expected = (parse_decimal(low), parse_decimal(high))
            actual = (source_low["value"], source_high["value"])
            if source_low["column"].type is sql.SQLType.INTEGER:
                # Integer-exclusive bounds can denote exactly the written
                # inclusive interval, e.g. id > 100 AND id < 106 means 101..105.
                import decimal
                actual = (actual[0].to_integral_value(rounding=decimal.ROUND_FLOOR) + 1
                          if source_low["operator"] == ">" else actual[0].to_integral_value(rounding=decimal.ROUND_CEILING),
                          actual[1].to_integral_value(rounding=decimal.ROUND_CEILING) - 1
                          if source_high["operator"] == "<" else actual[1].to_integral_value(rounding=decimal.ROUND_FLOOR))
            if actual != expected:
                issues.append({"family": "explicit_numeric_interval_disagreement",
                               "paths": [source_low["path"], source_high["path"]],
                               "english": text[start:end], "span": [start, end],
                               "source_bounds": [str(value) for value in actual],
                               "expected_bounds": [str(value) for value in expected],
                               "state": "independent_review_required"})
    return issues


def sql_result_signature(cursor, *, max_rows=2000000, spill_keys=100000):
    """Hash complete positional rows, spilling large duplicate counts to disk.

    Spill and in-memory execution produce exactly the same signature. A caller
    may lift the row bound explicitly; query deadlines still apply upstream.
    """
    import decimal
    import sqlite3
    import tempfile
    from contextlib import ExitStack, closing
    from engine.numeric import canonical_decimal
    if type(spill_keys) is not int or spill_keys < 1:
        raise ValueError("signature spill bound must be a positive integer")
    ordered = hashlib.sha256(); bag = Counter(); count = 0
    with ExitStack() as stack:
        disk = None
        for row in cursor:
            count += 1
            if max_rows is not None and count > max_rows:
                raise ValueError("SQL audit row budget exceeded")
            values = []
            for value in row:
                if isinstance(value, (int, float)):
                    number = decimal.Decimal(str(value))
                    values.append(["number", canonical_decimal(number)])
                elif isinstance(value, bytes): values.append(["blob", value.hex()])
                else: values.append(["null" if value is None else "text", value])
            key = hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).digest()
            ordered.update(key); bag[key] += 1
            if len(bag) >= spill_keys:
                if disk is None:
                    directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="ace-answer-audit-"))
                    disk = stack.enter_context(closing(sqlite3.connect(str(Path(directory) / "counts.sqlite"))))
                    disk.execute("CREATE TABLE counts (key BLOB PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID")
                disk.executemany("INSERT INTO counts VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET n=n+excluded.n", bag.items())
                bag.clear()
        if disk is not None:
            disk.executemany("INSERT INTO counts VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET n=n+excluded.n", bag.items())
            entries = disk.execute("SELECT key, n FROM counts ORDER BY key")
        else:
            entries = sorted(bag.items())
        unordered = hashlib.sha256()
        for key, occurrences in entries:
            unordered.update(key); unordered.update(occurrences.to_bytes(8, "big"))
        return {"rows": count, "ordered": ordered.hexdigest(), "multiset": unordered.hexdigest()}


def verified_program_document(identity, canonical, *, cache, validate_document, native_check):
    """Cache a codec result only after its exact native parser proof succeeds."""
    from engine.readers.ace import encode_query, decode_query
    from engine.sql_ast import query_from_tree, query_to_tree, render_query
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()
    key = digest(canonical)
    if key in cache:
        return cache[key]
    query = query_from_tree(canonical)
    text, tree = encode_query(query)
    validate_document(tree)
    if query_to_tree(decode_query(tree)) != canonical:
        raise ValueError("ACE roundtrip changed AST")
    native_check(identity, key, text, tree)
    result = {"ace_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
              "tree_sha256": digest(tree), "sql": render_query(query)}
    cache[key] = result
    return result


def corpus_main(argv):
    """Audit local training labels without loading a model or calling a service.

    An unresolved English meaning is a blocked admission, never a passing label.
    Existing native-parser witnesses are reused only for identical text/tree and
    pinned grammar/schema bytes. SQL runs read-only with time and output bounds.
    """
    import importlib.util
    import sqlite3
    import sys
    import time
    import unicodedata
    from functools import lru_cache
    from engine.sql_ast import query_from_tree, query_to_tree, render_query
    from engine.readers.grammar import canonical_query, source_schema
    from training.readers.sources import Sources

    parser = argparse.ArgumentParser(description=corpus_main.__doc__)
    parser.add_argument("--experiments", type=Path, required=True)
    parser.add_argument("--ace-study", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parents", type=Path, help="Corrected immutable parent corpus; defaults to the frozen source parents")
    parser.add_argument("--admissions", type=Path, help="Recomputed admissions for --parents")
    parser.add_argument("--source-databases", type=Path, help="Hashed manifest of explicitly recovered source versions")
    parser.add_argument("--column-dictionaries", type=Path, help="Verified source-column dictionary manifest")
    parser.add_argument("--database-overrides", type=Path, help="Private indexed-copy manifest; rows must be unchanged")
    parser.add_argument("--sql-seconds", type=float, default=5, help="Deadline for each distinct full-source verification query")
    parser.add_argument("--native-seconds", type=float, default=3, help="Declared APE verification deadline per document, at most 60 seconds")
    parser.add_argument("--max-answer-rows", type=int, default=2000000, help="Complete result bound; larger bags spill to disk")
    parser.add_argument("--questions-only", action="store_true", help="Check the supplied shared parents; do not rerun the separate historical template corpus")
    args = parser.parse_args(argv)
    if not 0 < args.native_seconds <= 60:
        parser.error("--native-seconds must be greater than zero and at most 60")
    out = args.out; out.mkdir(parents=True, exist_ok=True)
    code_paths = [Path(path) for path in (
        __file__, "engine/readers/ace.py", "engine/readers/grammar.py", "engine/readers/decisions.py",
        "engine/readers/numerals.py",
        "engine/sql_ast.py", "engine/sql_schema.py", "engine/numeric.py", "engine/sql_dates.py",
        "training/readers/examples.py", "training/readers/gold.py", "training/readers/sources.py",
        "regress/sql_import.py")]
    parser_base = args.ace_study.parent
    code_paths.extend(parser_base / path for path in ("pipeline.py", "ape_bridge.pl", "tools/swipl/bin/swipl.exe"))
    code_paths.extend(sorted((parser_base / "sources/APE/prolog").rglob("*.pl")))
    code_hashes = {str(path): sha(path) for path in code_paths}
    started = time.monotonic()
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode()).hexdigest()
    def rows(path):
        with gzip.open(path, "rt", encoding="utf8") as stream:
            yield from map(json.loads, stream)
    def load(path):
        return json.loads(path.read_text(encoding="utf8"))
    def normalize(text):
        return " ".join(unicodedata.normalize("NFKC", text).casefold().split())

    base = args.ace_study.parent
    sys.path.insert(0, str(base)); sys.path.insert(0, str(args.ace_study))
    spec = importlib.util.spec_from_file_location("ace_reference_pipeline", base / "pipeline.py")
    pipeline = importlib.util.module_from_spec(spec); spec.loader.exec_module(pipeline)
    validator = pipeline.Validator(load(base / "ace-tree.schema.json"))
    from validate_request import RequestValidator
    from audit import check as check_historical
    boundary = RequestValidator()
    grammar = args.experiments / "shared-semantic-grammar-20261106"
    scoped = args.experiments / "shared-semantic-scoped-20261106"
    parent_path = args.parents or scoped / "semantic-examples.jsonl.gz"
    admission_path = args.admissions or grammar / "grammar-admission.jsonl.gz"
    inputs = [parent_path, admission_path,
              grammar / "native-ape-records.jsonl.gz", grammar / "native-ape-assessment.json",
              args.ace_study / "ace-corpus.jsonl.gz", base / "ace-tree.schema.json",
              base / "sources/APE/prolog/parser/grammar.fit"]
    native = load(grammar / "native-ape-assessment.json")
    witness_identity = (native["admission_sha256"] == sha(grammar / "grammar-admission.jsonl.gz") and
                        native["ace_schema_sha256"] == sha(inputs[5]) and
                        native["ape_grammar_sha256"] == sha(inputs[6]))
    witnesses = {r["id"]: r for r in rows(inputs[2])}
    admissions = {r["id"]: r for r in rows(inputs[1])}
    sources = Sources()
    bird = Path("C:/work/datasets/bird/extracted/train")
    if (bird / "train.json").exists(): sources.add_bird(str(bird))
    syn = Path("C:/work/datasets/spider_syn")
    if (syn / "train_spider.json").exists(): sources.add_syn(str(syn))
    for p in Path("C:/work/datasets/spider_official/spider_data/test_database").rglob("*.sqlite"):
        sources.db_files.setdefault(p.stem, str(p))
    if args.source_databases:
        sources.add_database_versions(args.source_databases)
        inputs.append(args.source_databases)
    if args.column_dictionaries:
        sources.add_column_dictionaries(args.column_dictionaries)
        inputs.append(args.column_dictionaries)
    import copy
    schema_sources = copy.copy(sources)
    schema_sources.db_files = dict(sources.db_files)
    graph = lru_cache(maxsize=3)(schema_sources.graph)
    source_keys = lru_cache(maxsize=None)(schema_sources.primary_keys)
    if args.database_overrides:
        overrides = load(args.database_overrides)
        for db, record in overrides.items():
            if Path(sources.db_path(db)).resolve() != Path(record["source_path"]).resolve():
                raise ValueError("indexed audit copy belongs to a different source version")
            if sha(Path(record["source_path"])) != record["source_sha256"]:
                raise ValueError("indexed audit copy source identity changed")
            if sha(Path(record["indexed_path"])) != record["indexed_sha256"]:
                raise ValueError("indexed audit copy identity changed")
            sources.db_files[db] = record["indexed_path"]
        inputs.append(args.database_overrides)

    native_process = None; native_keys = set(); native_counts = Counter()
    def check_native(identity, key, text, tree, witness):
        nonlocal native_process
        if key in native_keys:
            native_counts["cached_exact_document"] += 1
            return
        if (witness_identity and witness.get("state") == "validated"
                and witness.get("ace_text_sha256") == hashlib.sha256(text.encode()).hexdigest()
                and witness.get("tree_sha256") == digest(tree)):
            native_counts["reused_pinned_exact_document"] += 1
        else:
            if native_process is None:
                native_process = subprocess.Popen([str(pipeline.SWI), "-q", "-f", "none", "-s", str(pipeline.BRIDGE),
                                                   "-g", "main", "-t", "halt", "--"], cwd=base,
                                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                  stderr=(out / "native-parser.stderr").open("w", encoding="utf8"),
                                                  text=True, encoding="utf8", bufsize=1)
            native_process.stdin.write(json.dumps({"id": identity, "text": text, "seconds": args.native_seconds}, ensure_ascii=False) + "\n")
            native_process.stdin.flush()
            response = native_process.stdout.readline()
            if not response:
                raise ValueError("native ACE parser ended without a result")
            parsed = json.loads(response)
            if parsed["status"] != "accepted" or pipeline.document(parsed) != tree:
                raise ValueError("fresh native ACE parse disagrees with generated tree")
            native_counts["fresh_native_exact_document"] += 1
        native_keys.add(key)

    connections = {}; database_inputs = {}
    def connection(db):
        if db not in connections:
            if len(connections) >= 4:
                connections.pop(next(iter(connections))).close()
            p = Path(sources.db_path(db)).resolve()
            if db not in database_inputs:
                database_inputs[db] = {"path": str(p), "sha256": sha(p), "bytes": p.stat().st_size}
            connections[db] = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
            connections[db].execute("PRAGMA query_only=ON")
        return connections[db]
    @lru_cache(maxsize=512)
    def execution(db, sql):
        conn = connection(db); deadline = time.monotonic() + args.sql_seconds
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            return sql_result_signature(conn.execute(sql), max_rows=args.max_answer_rows)
        finally: conn.set_progress_handler(None, 0)

    counts = Counter(); failures = Counter(); splits = {}; question_splits = {}; seen = set()
    sql_cache = {}; historical_counts = Counter()
    with gzip.open(out / "historical-records.jsonl.gz", "wt", encoding="utf8") as sink:
        for r in (() if args.questions_only else rows(args.ace_study / "ace-corpus.jsonl.gz")):
            result = {"id": r["id"], "partition": r["partition"], "kind": r["ir"]["kind"]}
            try:
                decoded = boundary.validate(r["json_tree"], r["ir"])
                evidence = check_historical(r, decoded, execute=True)
                result.update(state="bounded_template_checks_pass", evidence=evidence,
                              original_database_answer_verified=False)
                historical_counts["fixture_executions"] += len(evidence.get("execution_cases", []))
                historical_counts["corrected_source_targets"] += bool(r.get("target_corrected"))
            except Exception as exc:
                result.update(state="failed", reason=f"{type(exc).__name__}: {exc}")
            historical_counts[result["state"]] += 1
            historical_counts["kind/" + r["ir"]["kind"]] += 1
            sink.write(json.dumps(result, ensure_ascii=False) + "\n")
    with gzip.open(out / "question-records.jsonl.gz", "wt", encoding="utf8") as sink, \
            gzip.open(out / "admission-decisions.jsonl.gz", "wt", encoding="utf8") as decisions:
        for r in rows(parent_path):
            if not r["input"]["conversation"] and r["target_ast"] is None:
                counts["property_records_not_question_pairs"] += 1; continue
            identity = r["id"]; issues = []
            result = {"id": identity, "split": r["split"], "db_id": r["db_id"],
                      "english_meaning": "independent_review_required", "checks": {}}
            counts["question_parents"] += 1
            if identity in seen: issues.append("duplicate_identity")
            seen.add(identity)
            turns = r["input"]["conversation"]
            questions = [t["content"] for t in turns if t["role"] == "user"]
            try:
                if r["input"]["schema"] != source_schema(graph(r["db_id"])):
                    raise ValueError("parent schema evidence differs from selected source version")
                result["checks"]["input_schema_source_evidence"] = "pass"
                counts["input_schema_source_evidence_pass"] += 1
            except Exception as exc:
                issues.append(f"{type(exc).__name__}: {exc}")
            result["explicit_numeric_claim_findings"] = explicit_constraint_issues(turns, r["target_ast"])
            counts["with_explicit_numeric_claim_disagreement"] += bool(result["explicit_numeric_claim_findings"])
            if not questions or any(not x.strip() for x in questions): issues.append("empty_question")
            for q in questions: question_splits.setdefault(normalize(q), set()).add(r["split"])
            splits.setdefault(r["split"], set()).add(r["db_id"])
            admission = admissions.get(identity)
            if not admission:
                issues.append("missing_admission_record")
            elif admission["state"] != "admitted":
                issues.append("grounding_admission_rejected")
                result["grounding_reason"] = admission["reason"]
            if r["target_ast"] is None:
                result["checks"]["program"] = "clarification_without_sql"
                counts["clarification_records"] += 1
            elif admission and admission["state"] == "admitted":
                canonical = admission["canonical_ast"]
                try:
                    cached = verified_program_document(identity, canonical, cache=sql_cache,
                                validate_document=validator.validate_document,
                                native_check=lambda i, k, t, d: check_native(i, k, t, d, witnesses.get(i, {})))
                    result["checks"]["ace_schema_roundtrip_native_evidence"] = "pass"
                    counts["ace_schema_roundtrip_native_evidence_pass"] += 1
                    original = query_from_tree(r["target_ast"])
                    source_sql = render_query(original)
                    actual = execution(r["db_id"], cached["sql"])
                    expected = execution(r["db_id"], source_sql)
                    ordered = bool(r["target_ast"].get("order_by"))
                    mode = "ordered" if ordered else "multiset"
                    if actual[mode] != expected[mode] or actual["rows"] != expected["rows"]:
                        raise ValueError("canonical SQL answer differs from parent target")
                    result["checks"]["full_source_database_parent_target_parity"] = "pass"
                    result["answer_rows"] = actual["rows"]
                    result["answer_signature"] = actual
                    counts["full_source_database_parent_target_parity_pass"] += 1
                    generated = identity.startswith("conversation/") and identity.count("/") == 3
                    if identity.startswith("program/") or generated:
                        if generated:
                            source, index, variant = identity.removeprefix("conversation/").split("/")
                        else:
                            source, index = identity.removeprefix("program/").rsplit("/", 1)
                        gold = sources.questions[source][int(index)]
                        if gold["question"] != questions[0] or gold["db_id"] != r["db_id"]:
                            raise ValueError("original English source differs")
                        gold_sql = gold.get("query", gold.get("SQL"))
                        if not gold_sql: raise ValueError("original source SQL unavailable")
                        if r.get("reviewed_target_repair"):
                            result["checks"]["original_dataset_target"] = "superseded_by_reviewed_source_repair"
                            counts["reviewed_source_target_repairs"] += 1
                        else:
                            if generated and variant == "correction":
                                from training.readers.examples import generated_dialogue_query
                                from training.readers.gold import normalize_source_grouping
                                source_query = normalize_source_grouping(
                                    import_sql(gold_sql, graph(r["db_id"])), graph(r["db_id"]), source_keys(r["db_id"]))
                                query = generated_dialogue_query({"variant": variant, "state": r["input"]},
                                    source_query, gold["question"])
                                gold_sql = render_query(query)
                            expected = execution(r["db_id"], gold_sql)
                            if actual[mode] != expected[mode] or actual["rows"] != expected["rows"]:
                                raise ValueError("SQL answer differs from original dataset SQL")
                            result["checks"]["original_dataset_question_and_answer_parity"] = "pass"
                            counts["original_dataset_question_and_answer_parity_pass"] += 1
                except Exception as exc:
                    issues.append(f"{type(exc).__name__}: {exc}")
            else:
                try:
                    proposed = query_to_tree(canonical_query(query_from_tree(r["target_ast"])))
                    verified_program_document(identity, proposed, cache=sql_cache,
                                validate_document=validator.validate_document,
                                native_check=lambda i, k, t, d: check_native(i, k, t, d, witnesses.get(i, {})))
                    result["checks"]["ace_schema_roundtrip_native_evidence"] = "pass"
                    result["formal_program_role"] = "proposed_ungrounded_target"
                    counts["ace_schema_roundtrip_native_evidence_pass"] += 1
                except Exception as exc:
                    issues.append(f"{type(exc).__name__}: {exc}")
            result["issues"] = issues
            result["training_admission"] = "quarantined"
            result["admission_reasons"] = issues + ["independent_english_meaning_review_required"]
            failures.update(issues)
            counts["with_mechanical_failure" if issues else "mechanical_checks_pass"] += 1
            sink.write(json.dumps(result, ensure_ascii=False) + "\n")
            decisions.write(json.dumps({k: result[k] for k in ("id", "split", "training_admission", "admission_reasons")}) + "\n")
            if counts["question_parents"] % min(100, max(1, len(admissions) // 20)) == 0:
                sink.flush(); decisions.flush(); print(json.dumps(dict(counts)), flush=True)
    for conn in connections.values(): conn.close()
    if native_process is not None:
        native_process.stdin.close(); native_process.wait(timeout=30)
    benchmarks = {}
    for label, path in [("official_spider_dev", Path("C:/work/prereasoner-data/spider/data/dev.json")),
                        ("historical_spider_calibration", Path("C:/work/prereasoner-readers-artifacts/calibration/dev.json"))]:
        benchmark = load(path); texts = {normalize(r["question"]) for r in benchmark}
        dbs = {r["db_id"] for r in benchmark}
        benchmarks[label] = {"questions": len(benchmark), "sha256": sha(path),
                             "exact_question_matches_by_split": {split: sum(split in question_splits.get(q, set()) for q in texts)
                                                                   for split in splits},
                             "database_overlap_by_split": {split: sorted(dbs & values) for split, values in splits.items()}}
    report = {"counts": dict(counts), "historical": dict(historical_counts),
              "benchmarks": benchmarks, "source_databases": database_inputs, "native_parser": dict(native_counts),
              "failure_families": dict(failures), "training_authorized": False, "paid_calls": 0,
              "semantic_admission_gate_pass": False, "question_parents_missing_admission": sorted(set(admissions)-seen),
              "train_calibration_database_overlap": sorted(splits.get("train", set()) & splits.get("calibration", set())),
              "train_calibration_exact_question_overlap": sum(v >= {"train", "calibration"} for v in question_splits.values()),
              "inputs": {str(p): sha(p) for p in inputs},
              "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "dirty_worktree": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
              "code_sha256": code_hashes,
              "owner_files_unchanged": all(sha(path) == code_hashes[str(path)] for path in code_paths),
              "configuration": {"sql_seconds": args.sql_seconds, "native_seconds": args.native_seconds,
                                "max_answer_rows": args.max_answer_rows,
                                "historical_template_corpus_checked": not args.questions_only},
              "seconds": time.monotonic()-started,
              "limits": ["Template English checks cover a finite application profile and explicit corpus conventions.",
                         "Native APE witnesses reused only after matching current exact text/tree and pinned grammar/schema hashes.",
                         "Database execution checks answer agreement, not independent natural-language meaning.",
                         "All shared question records remain quarantined until independent English/dialogue meaning review.",
                         "Original inputs remain immutable; admission-decisions is the fail-closed audit artifact, not a replacement training corpus."]}
    (out / "assessment.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["corpus"]:
        corpus_main(sys.argv[2:])
    else:
        main()
