"""Gold readings: each question's gold SQL -> typed AST (regress.sql_import.import_sql) -> readings
(engine.readers.readings.readings_of), written as the readings files training.readers.examples reads.

The perfect-readings oracle is the check: gold's rows are compared with the rows of the AST as imported, and with
the rows of the query the builder (engine.readers.readings.build) makes back from the readings, on the same capped
data. The printed totals say how much the readings form and the foreign-key joins lose before any model reads
anything. Every question gets one record: {"source", "index", "db_id", "status"} plus "readings" when its SQL is
readable. Status "ok" means the rebuilt query returns gold's rows. "differs" means the AST does and the rebuild does
not. "gold-ast-differs" means the AST as imported does not. "build" means the builder could not make the readings
into a query. "not-readable:<why>" and "import" mean there are no readings. "database" means the capped loader
could not read the question's database (a SynSQL value SQLite cannot hold), so the question was not read.

Sets: calibration and train split Spider's train_spider and train_others questions by the split manifest's
calibration_dbs (training.readers.splits); bird reads BIRD's training set (train.json, train_tables.json,
train_databases/), whose databases are disjoint from Spider's, so every BIRD question is training data; synsql reads
the SynSQL-2.5M questions training.readers.synsql chose, over databases disjoint from both (training data too). Their
databases hold a row or two per table, so the oracle's row check there mostly confirms that a query runs.

usage: python -m training.readers.gold <split dir> [calibration|train|bird|synsql] [--bird <BIRD train dir>]
           [--synsql <selection dir> --synsql-databases <dir>] [--shard i/n] [--resume]
           [--out readings.jsonl] [--spider-zip <zip>] [--db-dir <dir> ...]
"""
import argparse
import collections
import json
import os
import zlib

from engine.readers.readings import NotReadable, build, readings_of
from engine.sql_ast import render_query
from regress.sql_import import import_sql
from spider.probe.evalutil import exec_sql_timed
from spider.probe.spider_eval import compare
from training.readers.provenance import SPIDER_TRAIN_SETS
from training.readers.sources import Sources, add_arguments, capped_database

SECONDS = 8.0


def read_unit_program(pair, *, source_record=None, column_types=None):
    """Import a recovered Qwen token program without guessing literal types.

    Source token kinds recover identifier quoting, not missing cell datatypes.
    Without the verified original, only literal-free programs and explicit integer
    LIMITs are recoverable. An original source restores literal spellings; explicit
    source-backed column types are optional, never inferred from token spelling.
    Return the existing importer's AST/schema for a separate execution audit.
    This is offline source recovery, never a serving SQL-generation path.
    """
    import re
    from decimal import Decimal, InvalidOperation
    from sqlglot import parse
    from sqlglot.expressions import Identifier, Literal as ParsedLiteral, Neg
    from engine.sql_schema import SchemaColumn, SchemaGraph, ForeignKey
    from engine.sql_ast import ColumnRef, SQLType
    from engine.column_names import canonical_columns
    from regress.sql_import import Unsupported
    from training.readers.sources import match_unit_source

    tokens = pair["sql_units"]
    original = None if source_record is None else match_unit_source(pair, source_record)
    column_types = {} if column_types is None else column_types
    names = {u["text"] for u in tokens if u["kind"] == "table"}
    schema = pair["schema_units"]
    qualified = all(u.get("table") for u in schema)
    if not names or (not qualified and len(names) != 1):
        raise Unsupported("source-table-scope")
    default = next(iter(names)) if len(names) == 1 else None
    raw_names = collections.defaultdict(list)
    for unit in schema:
        raw_names[unit["table"] if qualified else default].append(unit.get("colname", unit["text"]))
    canonical = {table: iter(canonical_columns(values)) for table, values in raw_names.items()}
    refs, source_names, columns = {}, {}, []
    for unit in schema:
        table = unit["table"] if qualified else default
        name = unit.get("colname", unit["text"])
        if not isinstance(table, str) or not isinstance(name, str) or not name:
            raise Unsupported("source-column-identity")
        key = (table, name) if qualified else unit["col"]
        dtype = SQLType(column_types.get(key, SQLType.UNKNOWN))
        ref = ColumnRef(table, next(canonical[table]), dtype)
        if key in refs:
            raise Unsupported("source-duplicate-column")
        refs[key] = ref
        source_names[key] = name
        columns.append(SchemaColumn(ref, (), unit["col"]))
    if set(column_types) - set(refs):
        raise Unsupported("source-column-type-pointer")
    foreign_keys = []
    for unit in schema:
        if unit.get("ref") is not None:
            key = (unit["table"], unit["colname"])
            target = tuple(unit["ref"])
            if key not in refs or target not in refs:
                raise Unsupported("source-foreign-key")
            foreign_keys.append(ForeignKey(refs[key], refs[target]))
    graph = SchemaGraph(columns, foreign_keys)
    if names - set(graph.tables):
        raise Unsupported("source-unknown-table")
    rendered = []
    for index, token in enumerate(tokens):
        text, kind = token["text"], token["kind"]
        if kind == "table":
            text = Identifier(this=text, quoted=True).sql(dialect="sqlite")
        elif kind == "field":
            key = (token.get("table"), token.get("colname")) if qualified else token["col"]
            if key not in refs or source_names[key] != text:
                raise Unsupported("source-field-pointer")
            text = Identifier(this=refs[key].name, quoted=True).sql(dialect="sqlite")
        elif kind == "literal":
            if original is not None:
                text = original[index]["text"]
                expressions = parse(text, read="sqlite")
                parsed = expressions[0] if len(expressions) == 1 else None
                if not (isinstance(parsed, ParsedLiteral) or isinstance(parsed, Neg) and
                        isinstance(parsed.this, ParsedLiteral) and not parsed.this.is_string):
                    raise Unsupported("source-not-literal")
                if isinstance(parsed, ParsedLiteral) and parsed.is_string and parsed.this != str(original[index]["raw"]):
                    raise Unsupported("source-literal-value-differs")
                if not isinstance(parsed, ParsedLiteral) or not parsed.is_string:
                    try:
                        source_value = original[index].get("raw")
                        value = Decimal(text)
                        raw = Decimal(token["text"] if source_value is None else str(source_value))
                    except InvalidOperation as error:
                        raise Unsupported("source-literal-value-differs") from error
                    if not value.is_finite() or not raw.is_finite() or value != raw:
                        raise Unsupported("source-literal-value-differs")
            elif token["clause"] != "limit":
                raise Unsupported("source-filter-literal-type-unknown")
            if token["clause"] == "limit" and not re.fullmatch(r"[0-9]+", text):
                raise Unsupported("source-limit-not-integer")
        elif kind not in ("kw", "op", "punc"):
            raise Unsupported("source-token-kind")
        rendered.append(text)
    sql = " ".join(rendered)
    return sql, graph, import_sql(sql, graph)


def semantic_operator_labels(query, contract=None):
    """Scoped enum supervision, retaining occurrence paths and AST defaults.

    These annotations are targets, never input-item discovery. An inference
    request must offer its complete input-derived decision bank before labels
    are attached; emitting only gold occurrence paths would leak tree shape.
    """
    from dataclasses import fields, MISSING
    from engine import sql_ast
    from engine.sql_ast import query_to_tree
    from engine.readers.layout import named_coordinate_contract

    contract = named_coordinate_contract() if contract is None else contract
    selectors = collections.defaultdict(list)
    for row in contract["coordinates"]:
        if row.get("selector"):
            selectors[row["selector"]["node"], row["selector"]["field"]].append(row)
    targets = []

    def visit(value, path):
        if isinstance(value, list):
            for i, child in enumerate(value): visit(child, f"{path}[{i}]")
        elif isinstance(value, dict):
            node = value.get("node")
            if node:
                defaults = {f.name: f.default for f in fields(getattr(sql_ast, node))}
                for (owner, field), rows in sorted(selectors.items()):
                    if owner != node: continue
                    actual = value.get(field, defaults.get(field, MISSING))
                    if actual is MISSING or not any(actual in r["selector"]["values"] for r in rows):
                        raise ValueError("typed enum has no named operation target")
                    targets.append({"path": path + "." + field, "node": node, "field": field, "value": actual,
                                    "labels": {r["name"]: int(actual in r["selector"]["values"]) for r in rows}})
            for key, child in value.items(): visit(child, path + "." + key)

    visit(query_to_tree(query), "$")
    return {"contract_sha256": contract["contract_sha256"], "operations": targets,
            "evidence": "validated_scoped_typed_program; not English span alignment"}


def semantic_labels(query, contract=None):
    """Closed-world SQL labels and path-local syntactic roles from a checked AST.

    These are program annotations, not inferred property evidence, ACE predicate
    meanings, or English span alignments. A path preserves scope and occurrence:
    the same column can be a projection, join key and filter in different places.
    Unannotated semantic dimensions stay unknown, never automatic negatives.
    """
    from engine.readers.layout import named_coordinate_contract
    from dataclasses import fields, MISSING
    from engine import sql_ast
    from engine.sql_ast import query_to_tree

    contract = named_coordinate_contract() if contract is None else contract
    tree = query_to_tree(query)
    sql = [row for row in contract["coordinates"] if row["block"] == "sql"]
    selectors = collections.defaultdict(list)
    for row in sql:
        selector = row.get("selector")
        if selector:
            defaults = {f.name: f.default for f in fields(getattr(sql_ast, selector["node"]))}
            selectors[selector["node"]].append((row["name"], selector["field"], selector["values"],
                                                 defaults.get(selector["field"], MISSING)))
    positive, arguments = set(), []

    def mark(*names):
        positive.update(names)

    def role(path, *roles):
        arguments.append({"path": path, "roles": ["role." + r for r in roles],
                          "evidence": "typed_program_argument_not_english_alignment"})

    def visit(value, path, predicate_scope=False):
        if isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, f"{path}[{i}]", predicate_scope)
            return
        if not isinstance(value, dict):
            return
        node = value.get("node")
        # Canonical JSON omits dataclass defaults. Missing ASC/INNER fields
        # still mean ASC/INNER, rather than a negative operator annotation.
        for name, field, values, default in selectors[node]:
            if value.get(field, default) in values:
                mark(name)
        if node == "SelectQuery":
            if value.get("distinct", False):
                mark("sql.distinct_rows")
            if value.get("where") is not None:
                mark("sql.filter")
            if value.get("having") is not None:
                mark("sql.having")
            if value.get("group_by"):
                mark("intent_group")
                for i, _ in enumerate(value["group_by"]):
                    role(f"{path}.group_by[{i}]", "group_key")
            if value.get("limit") is not None:
                mark("intent_limit")
            if value.get("with_ties", False):
                mark("sql.with_ties")
            role(path + ".from_table", "record_grain")
        elif node == "SelectItem":
            mark("sql.project")
            role(path + ".expression", "projection")
        elif node == "Star":
            mark("sql.project_all")
        elif node == "Aggregate":
            distinct = value.get("distinct", False)
            function = value["function"]
            if distinct:
                mark("sql.aggregate_distinct")
                if function in ("COUNT", "SUM", "AVG"):
                    mark("sql." + function.lower() + "_distinct")
            if function == "COUNT" and not distinct:
                mark("sql.count_rows" if value["operand"]["node"] == "Star" else "sql.count_non_null")
            role(path + ".operand", "measure")
        elif node == "Comparison":
            if predicate_scope:
                intent = {"=": "eq", ">": "gt", ">=": "gt", "<": "lt", "<=": "lt"}.get(value["operator"])
                if intent:
                    mark("intent_filter_" + intent)
                role(path + ".left", "filter_attribute")
                role(path + ".right", "filter_value")
            if value["right"].get("node") == "Literal" and value["right"].get("value") is None:
                if value["operator"] in ("IS", "IS NOT"):
                    mark("sql.is_null" if value["operator"] == "IS" else "sql.is_not_null")
        elif node == "Join":
            mark("sql.join")
            role(path + ".left", "join_left")
            role(path + ".right", "join_right")
            if value.get("additional"):
                mark("sql.join_composite_key")
                for i, _ in enumerate(value["additional"]):
                    role(f"{path}.additional[{i}][0]", "join_left")
                    role(f"{path}.additional[{i}][1]", "join_right")
        elif node in ("ScalarSubquery", "SubquerySource", "Lower", "DatePart", "DateSpan"):
            mark({"ScalarSubquery": "sql.scalar_subquery", "SubquerySource": "sql.derived_table",
                  "Lower": "sql.lower", "DatePart": "sql.date_part", "DateSpan": "sql.date_span"}[node])
        elif node in ("ExistsPredicate", "InPredicate"):
            negated = value.get("negated", False)
            mark("sql." + ("not_" if negated else "") + ("exists" if node == "ExistsPredicate" else "in"))
            if node == "ExistsPredicate":
                role(path, "scope_negated" if negated else "scope_existential")
                role(path + ".query", "scope_child")
        elif node == "OrderTerm":
            role(path + ".expression", "sort_key")
        # A join predicate is not a user filter, including inside a WHERE
        # subquery. Reset at query boundaries; descendants retain their scope.
        for key, child in value.items():
            if key == "node":
                continue
            scope = predicate_scope
            if node == "SelectQuery":
                scope = key in ("where", "having")
            elif node == "Join":
                scope = False
            visit(child, path + "." + key, scope)

    visit(tree, "$")
    known = {row["name"] for row in sql}
    if positive - known or len(known) != 64:
        raise ValueError("SQL annotation and named coordinate contract disagree")
    return {"contract_sha256": contract["contract_sha256"],
            "labels": {name: int(name in positive) for name in sorted(known)},
            "arguments": arguments,
            "evidence": "validated_typed_program; all non-SQL coordinates unknown"}


def gold_sql(q):
    return q.get("query") or q["SQL"]


def normalize_source_grouping(query, graph, primary_keys):
    """Expand source GROUP BY only for columns determined by declared keys.

    Some source SQL projects a name while grouping its joined row's primary
    key. The strict AST requires that name in GROUP BY too. INNER join
    equalities and complete, non-null primary keys prove the added columns
    cannot split a group. An arbitrary SQLite bare column is left invalid.
    This is offline source normalization; the serving validator stays strict.
    """
    from dataclasses import fields, is_dataclass, replace
    from engine.sql_ast import ColumnRef, SelectQuery

    def visit(value):
        if isinstance(value, tuple):
            return tuple(visit(child) for child in value)
        if not is_dataclass(value) or isinstance(value, type):
            return value
        value = replace(value, **{field.name: visit(getattr(value, field.name)) for field in fields(value)})
        if not isinstance(value, SelectQuery) or not value.group_by:
            return value
        aliases = {value.from_alias or value.from_table: value.from_table} if isinstance(value.from_table, str) else {}
        aliases.update((join.alias or join.table, join.table) for join in value.joins)
        known = {(column.table, column.name) for column in value.group_by if isinstance(column, ColumnRef)}
        equalities = [(left, right) for join in value.joins if join.kind == "INNER" for left, right in join.predicates]
        changed = True
        while changed:
            before = len(known)
            for left, right in equalities:
                a, b = (left.table, left.name), (right.table, right.name)
                if a in known: known.add(b)
                if b in known: known.add(a)
            for alias, physical in aliases.items():
                key = primary_keys.get(physical)
                if key and all((alias, name) in known for name in key):
                    known.update((alias, column.ref.name) for column in graph.by_table.get(physical, ()))
            changed = len(known) != before
        required = [item.expression for item in value.select if isinstance(item.expression, ColumnRef)]
        required += [term.expression for term in value.order_by if isinstance(term.expression, ColumnRef)]
        additions = tuple(dict.fromkeys(column for column in required if column not in value.group_by))
        if all((column.table, column.name) in known for column in additions):
            return replace(value, group_by=value.group_by + additions)
        return value

    return visit(query)


def read_program(source, index, q, connection, graph, counts, reasons):
    """Import and execute one source program before any readings codec.

    Return its record, typed query (or None), source rows and strict agreement.
    The same oracle owns AST checks for readings and recursive-target audits.
    """
    gold_rows, source_error = exec_sql_timed(connection, gold_sql(q), timeout=SECONDS)
    counts["questions"] += 1
    record = {"source": source, "index": index, "db_id": q["db_id"]}
    if source_error is not None:
        record["source_error"] = str(source_error)
    try:
        query = import_sql(gold_sql(q), graph)
    except Exception as exc:                       # noqa: BLE001 - the grammar cannot hold it
        reasons["import:" + str(exc)[:40]] += 1
        record["status"] = "import"
        return record, None, gold_rows, False
    counts["imported"] += 1
    try:
        rows, err = exec_sql_timed(connection, render_query(query), timeout=SECONDS)
    except Exception as exc:                       # noqa: BLE001 - the AST validator refuses the gold shape
        rows, err = None, exc
    ast_ok = err is None and bool(compare(gold_rows, rows).get("strict"))
    if err is not None:
        record["ast_error"] = str(err)
    counts["ast_rows"] += ast_ok
    return record, query, gold_rows, ast_ok


def read_gold(source, index, q, connection, graph, counts, reasons, structures):
    """The record of one question, retaining the readings oracle's existing labels."""
    record, query, gold_rows, ast_ok = read_program(source, index, q, connection, graph, counts, reasons)
    if query is None:
        return record
    try:
        readings = readings_of(query)
    except NotReadable as exc:
        reasons["readings:" + str(exc)] += 1
        record["status"] = "not-readable:" + str(exc)
        return record
    counts["readable"] += 1
    structures[readings["structure"]] += 1
    record["readings"] = readings
    try:
        sql = render_query(build(readings, graph))
    except Exception as exc:                       # noqa: BLE001 - classify what the builder cannot make
        reasons["build:" + type(exc).__name__ + ":" + str(exc)[:40]] += 1
        record["status"] = "build"
        return record
    rows, err = exec_sql_timed(connection, sql, timeout=SECONDS)
    ok = err is None and bool(compare(gold_rows, rows).get("strict"))
    counts["rebuilt_rows"] += ok
    counts["rebuilt_rows_where_ast_ok"] += ok and ast_ok
    if ast_ok and not ok:
        reasons["rebuilt-differs" + (":render-error" if err else "")] += 1
    record["status"] = "ok" if ok else ("differs" if ast_ok else "gold-ast-differs")
    return record


def question_set(which, split_dir, args):
    """([(source, index, question)], {db_id: (connection, graph)} loader) for a set."""
    if which == "bird":
        if not args.bird:
            raise SystemExit("bird needs --bird <BIRD train dir>")
        tables = {t["db_id"]: t for t in json.load(open(os.path.join(args.bird, "train_tables.json"),
                                                        encoding="utf-8"))}
        questions = [("bird_train", index, q)
                     for index, q in enumerate(json.load(open(os.path.join(args.bird, "train.json"), encoding="utf-8")))]

        def database(db):
            return capped_database(os.path.join(args.bird, "train_databases", db, db + ".sqlite"), tables[db])
        return questions, database
    sources = Sources.from_args(args)
    if which == "synsql":
        if not (args.synsql and args.synsql_databases):
            raise SystemExit("synsql needs --synsql <selection dir> --synsql-databases <dir>")
        sources.add_synsql(args.synsql, args.synsql_databases)
        return [("synsql", index, q) for index, q in enumerate(sources.questions["synsql"])], sources.database
    calibration = set(json.load(open(os.path.join(split_dir, "manifest.json")))["calibration_dbs"])
    questions = [(name, index, q) for name in SPIDER_TRAIN_SETS
                 for index, q in enumerate(sources.questions[name])
                 if (q["db_id"] in calibration) == (which == "calibration")]
    return questions, sources.database


def main():
    ap = argparse.ArgumentParser(description="Gold readings of a question set, checked by the perfect-readings oracle.")
    ap.add_argument("split_dir", help="the folder of the split's manifest.json (training.readers.splits)")
    ap.add_argument("which", nargs="?", default="calibration", choices=["calibration", "train", "bird", "synsql"])
    ap.add_argument("--bird", help="BIRD's train directory (for bird)")
    ap.add_argument("--synsql", help="training.readers.synsql's selection folder (for synsql)")
    ap.add_argument("--synsql-databases", help="SynSQL-2.5M's databases folder (for synsql)")
    ap.add_argument("--shard", default="0/1", help="i/n: only the questions of databases whose CRC-32 is i modulo n")
    ap.add_argument("--out", help="readings.jsonl to write; without it only the totals print")
    ap.add_argument("--resume", action="store_true", help="keep --out's records and read only the questions it lacks")
    add_arguments(ap)
    args = ap.parse_args()
    questions, database = question_set(args.which, args.split_dir, args)
    part, parts = map(int, args.shard.split("/"))
    questions = [item for item in questions if zlib.crc32(item[2]["db_id"].encode()) % parts == part]
    databases = {}
    counts, reasons, structures = collections.Counter(), collections.Counter(), collections.Counter()
    done = set()
    if args.resume and args.out and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as kept:
            done = {(r["source"], r["index"]) for r in map(json.loads, kept)}
    sink = open(args.out, "a" if done else "w", encoding="utf-8", newline="\n") if args.out else None
    for source, index, q in questions:
        if (source, index) in done:
            continue
        db = q["db_id"]
        if db not in databases:
            try:
                databases[db] = database(db)
            except (OverflowError, ValueError) as exc:    # a value the capped loader's SQLite cannot hold
                reasons["database:" + type(exc).__name__] += 1
                databases[db] = None
        if databases[db] is None:
            counts["questions"] += 1
            record = {"source": source, "index": index, "db_id": db, "status": "database"}
        else:
            connection, graph = databases[db]
            record = read_gold(source, index, q, connection, graph, counts, reasons, structures)
        if sink:
            sink.write(json.dumps(record) + "\n")
    if sink:
        sink.close()
    n = counts["questions"]
    print(f"{args.which}: {n} questions")
    for key in ("imported", "ast_rows", "readable", "rebuilt_rows", "rebuilt_rows_where_ast_ok"):
        print(f"  {key:28s} {counts[key]:5d}  {counts[key] / max(1, n):.1%}")
    print("  structures:", dict(structures))
    for reason, count in reasons.most_common(15):
        print(f"  {count:5d} {reason}")


if __name__ == "__main__":
    main()
