"""The reading model's output layout, shared by training, serving and decoding."""
AGGREGATES = ["plain", "COUNT", "SUM", "AVG", "MIN", "MAX"]
OPERATORS = ["=", "!=", ">", "<", ">=", "<=", "LIKE", "NOT LIKE", "IN", "NOT IN"]
HAVING_OPERATORS = ["=", "!=", ">", "<", ">=", "<="]
STRUCTURES = ["single", "UNION", "INTERSECT", "EXCEPT", "NESTED"]
SLOTS = 4

# per-block column outputs: name -> (offset, width)
LAYOUT = {}
_offset = 0
# "slot" is the select position of each aggregate of the column (6 x SLOTS); "where_twice" says the column is
# compared twice by one operator ("Duke or Maryland"); "share_part" says its condition makes the part of a share.
for _name, _width in (("select", 6), ("distinct_aggregate", 1), ("slot", 6 * SLOTS), ("where", 10), ("where_sub", 1),
                      ("where_twice", 1), ("group", 1), ("having", 6), ("having_op", 6), ("having_sub", 1),
                      ("order", 6), ("share_part", 1)):
    LAYOUT[_name] = (_offset, _width)
    _offset += _width
PER_BLOCK = _offset

# How many items of each kind a block holds, read as a class 0..top (more reads as top): the decoder takes that many
# of the best-scored items of the kind ("tables" counts the kept tables a block uses).
COUNTS = {"tables": 6, "select": 4, "where": 4, "group": 2, "order": 2, "having": 2}

# first-token outputs: structure, then per block distinct, OR, direction (none/ASC/DESC), limit (none/1/number), the
# counts, and whether the shown aggregate is a share of a whole
GLOBAL = {"structure": (0, 5)}
_offset = 5
for _b in range(2):
    for _name, _width in (("distinct", 1), ("conj", 1), ("dir", 3), ("limit", 3),
                          *((f"count_{kind}", top + 1) for kind, top in COUNTS.items()), ("share", 1)):
        GLOBAL[f"{_name}{_b}"] = (_offset, _width)
        _offset += _width
GLOBAL_WIDTH = _offset

# Training weighs the yes of a column's yes/no readings 3 to 1 and a table's use 2 to 1; decoding undoes the weight
# (decode.unweighted) so a probability reads as the chance the reading holds.
COLUMN_POS_WEIGHT = 3.0
TABLE_POS_WEIGHT = 2.0


# Candidate semantic-interface vocabulary. Existing readers keep their manifested
# layouts above until promotion; this function does not change loaded weights.
# One source owns the names used by candidate data, named heads and trace readers.
SEMANTIC_FRAMES = (
    ("identity", "Two mentions refer to the same entity/value.", ("subject", "object"), "identity constraint"),
    ("attribute", "An entity has a specified attribute value.", ("subject", "filter_attribute", "filter_value"), "Comparison"),
    ("relation", "A directed named relation links two entities.", ("relation_source", "modifier", "relation_target"), "Join + Comparison"),
    ("ownership", "An owner owns an asset; argument direction is explicit.", ("subject", "object"), "owns property binding"),
    ("membership", "An entity belongs to a set/group.", ("subject", "object"), "InPredicate or keyed relation"),
    ("part_whole", "A part belongs to a whole; not identity.", ("subject", "object"), "hasPart/isPartOf binding"),
    ("classification", "An entity is classified under a category.", ("subject", "object"), "category binding + Comparison"),
    ("status", "An entity has a lifecycle/process state.", ("subject", "filter_attribute", "filter_value"), "status binding + Comparison"),
    ("lifecycle_event", "An entity undergoes a specified event at a time.", ("subject", "modifier", "temporal_start"), "event/date bindings"),
    ("temporal_point", "An observation/event occurs at a specified time.", ("subject", "temporal_start"), "date comparison"),
    ("temporal_interval", "An observation/event lies in a stated interval.", ("subject", "temporal_start", "temporal_end"), "date bounds"),
    ("duration", "An interval has a duration in a stated unit.", ("temporal_start", "temporal_end", "unit"), "DateSpan"),
    ("temporal_limit", "A permission/contract has an explicit duration bound.", ("subject", "filter_attribute", "filter_value", "unit"), "bound comparison; domain policy required"),
    ("amount", "A monetary measure has a value and currency.", ("subject", "measure", "currency_argument"), "monetary operand + source currency"),
    ("quantity", "A physical/count measure has a value and unit.", ("subject", "measure", "unit"), "typed quantity operand"),
    ("rate", "A measure is expressed per an explicit denominator/unit.", ("measure", "object", "unit"), "ratio/measure binding"),
    ("comparison", "Two operands are compared under an explicit relation.", ("subject", "modifier", "object"), "Comparison"),
    ("containment", "Text contains/matches a stated literal/pattern.", ("subject", "filter_value"), "LIKE/NOT LIKE + pattern mode"),
    ("missingness", "An operand is missing or present under explicit null/blank rules.", ("subject", "modifier"), "IS NULL/IS NOT NULL or explicit blank comparison"),
    ("existence", "A specified matching record exists or does not exist.", ("scope_parent", "scope_child"), "ExistsPredicate"),
    ("quantification", "All/none/each has a declared restriction and scope.", ("scope_parent", "scope_child", "modifier"), "scoped existential/anti-existential compilation"),
    ("aggregation", "A measure is aggregated over an explicit record set/grain.", ("measure", "record_grain", "modifier"), "Aggregate"),
    ("grouping", "Records are partitioned by specified keys.", ("record_grain", "group_key"), "SelectQuery.group_by"),
    ("ordering", "Results are ordered by specified keys and direction.", ("record_grain", "sort_key", "modifier"), "OrderTerm + optional limit"),
    ("set_combination", "Two result sets are combined/intersected/subtracted.", ("subject", "modifier", "object"), "SetQuery"),
    ("arithmetic", "Explicit operands participate in a specified arithmetic operation.", ("subject", "modifier", "object"), "BinaryExpr"),
    ("proportion", "A stated part is divided by its stated whole.", ("subject", "object", "measure"), "existing share compilation"),
    ("weighted_aggregation", "A measure is aggregated with explicit weights.", ("measure", "modifier", "record_grain"), "SUM(measure*weight)/SUM(weight)"),
    ("difference", "A directed absolute difference compares two operands.", ("subject", "object", "unit"), "BinaryExpr subtraction"),
    ("change", "A measure changes between explicit comparison periods/bases.", ("measure", "temporal_start", "temporal_end", "modifier"), "existing registered difference/relative-change calculation"),
    ("extremum", "An entity/key has an extreme measure; signed size and magnitude differ.", ("subject", "measure", "modifier"), "MIN/MAX or ordered limited query"),
    ("conditional", "A condition selects one of two explicit result expressions.", ("filter_attribute", "subject", "object"), "unsupported by current typed scalar grammar; must refuse"),
)
SEMANTIC_ARGUMENT_ROLES = (
    "subject", "object", "modifier", "measure", "group_key", "filter_attribute", "filter_value", "sort_key",
    "projection", "join_left", "join_right", "temporal_start", "temporal_end", "unit", "currency_argument",
    "relation_source", "relation_target", "scope_parent", "scope_child", "scope_negated", "scope_universal",
    "scope_existential", "record_grain",
)
SEMANTIC_DIALOGUE_ACTS = (
    "answer", "clarify", "unsupported", "new_question", "refer_to_previous_question", "refer_to_previous_result",
    "replace_filter", "add_filter", "remove_filter", "replace_aggregate", "replace_order", "replace_limit",
    "replace_projection", "replace_grouping", "replace_table", "supply_missing_binding",
)
SQL_SEMANTIC_FACETS = (
    ("project", "SelectItem: any projection expression"),
    ("project_all", "Star: SELECT * or the row operand of COUNT(*)"),
    ("distinct_rows", "SelectQuery.distinct: distinct output rows"),
    ("aggregate_distinct", "Aggregate.distinct: distinct operand values"),
    ("filter", "SelectQuery.where: row predicate present"),
    ("having", "SelectQuery.having: aggregate/group predicate present"),
    ("join", "Join: an explicit join expression"),
    ("scalar_subquery", "ScalarSubquery: a query used as a scalar expression"),
    ("derived_table", "SubquerySource: a query used as a table source"),
    ("exists", "ExistsPredicate with negated=False"),
    ("not_exists", "ExistsPredicate with negated=True"),
    ("in", "InPredicate with negated=False"),
    ("not_in", "InPredicate with negated=True"),
    ("lower", "Lower: the lowercase text function"),
    ("date_part", "DatePart: a supported calendar projection"),
    ("date_span", "DateSpan: a duration between dates in its explicit unit"),
    ("is_null", "Comparison IS with a null literal on the right"),
    ("is_not_null", "Comparison IS NOT with a null literal on the right"),
    ("count_rows", "COUNT with a Star operand"),
    ("count_non_null", "COUNT of a non-Star operand without DISTINCT"),
    ("count_distinct", "COUNT with DISTINCT"),
    ("sum_distinct", "SUM with DISTINCT"),
    ("avg_distinct", "AVG with DISTINCT"),
    ("join_composite_key", "Join with more than one column-pair predicate"),
    ("with_ties", "SelectQuery.with_ties: include rows tied at a ranking cutoff"),
)


def named_coordinate_contract():
    """Definitions for the proposed 215-coordinate candidate, never calibration.

    Property meaning, source-observation masks and role targets must be qualified
    separately. A defined label is not a trained coordinate. Common ACE predicate
    frames here form an explicit application lexicon; they are not names of native
    Attempto grammar productions. Argument identities and scope edges remain typed
    graph fields, not extra opaque dimensions or a flat AST replacement.
    """
    from dataclasses import fields, is_dataclass
    import hashlib
    import json

    from engine.config import DATA_DIR
    from engine import sql_ast
    from engine.schema_org import load_contract

    ontology = load_contract()
    allocation_path = DATA_DIR / "alloc.json"
    allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
    rows = []

    def add(block, name, meaning, *, source, inherited=None, selector=None, **extra):
        rows.append({"index": len(rows), "block": block, "name": name, "meaning": meaning,
                     "source": source, "inherited_index": inherited, "selector": selector,
                     "calibrated": False, **extra})

    for d in allocation["dims"]:
        if d["family"] != "taxonomy":
            continue
        # These are new targets, not compatible reinterpretations of old weights.
        name = {"GeoCoordinates": "geo", "taxonName": "alternateName"}.get(d["name"], d["name"])
        prop = ontology.property(name)
        if prop is None:
            raise ValueError("candidate property is not in the pinned ontology: " + name)
        migration = None
        if d["name"] == "GeoCoordinates":
            migration = "Retire the class-named diagnostic channel. Train geo as a new property target; do not reuse its weight semantics."
        elif d["name"] == "taxonName":
            migration = "Retire taxonName; scientific-name observations map to the existing name property. Add alternateName as a separate supported alias target, not a rename of taxonName."
        add("property", prop.uri, prop.comment, source="selected allocation + pinned Schema.org",
            inherited=d["dim_id"] if migration is None else None, legacy_name=d["name"], migration=migration)

    intents = [d for d in allocation["dims"] if d["family"] == "intent"]
    for d in intents:
        add("sql", d["name"], "Inherited query intent; filter_eq/gt/lt apply to WHERE/HAVING comparisons.",
            source="engine/data/alloc.json", inherited=d["dim_id"])
    enums = (
        ("Aggregate", "function", sql_ast.AGGREGATES),
        ("BinaryExpr", "operator", sql_ast.ARITHMETIC_OPERATORS),
        ("BooleanExpr", "operator", ("AND", "OR")),
        ("Comparison", "operator", sql_ast.COMPARISONS),
        ("DatePart", "part", sql_ast.DATE_PARTS),
        ("DateSpan", "unit", sql_ast.DAYS_PER_UNIT),
        ("Join", "kind", ("INNER", "LEFT")),
        ("OrderTerm", "direction", ("ASC", "DESC")),
        ("SetQuery", "operator", sql_ast.SET_OPERATORS),
    )
    reuse = {("Aggregate", "function", "COUNT"): "intent_agg_count",
             ("Aggregate", "function", "SUM"): "intent_agg_sum",
             ("Aggregate", "function", "AVG"): "intent_agg_avg",
             ("OrderTerm", "direction", "ASC"): "intent_sort_asc",
             ("OrderTerm", "direction", "DESC"): "intent_sort_desc"}
    aliases = {"!=": "not_equal", "<>": "not_equal", "<": "less", "<=": "less_equal",
               "=": "equal", ">": "greater", ">=": "greater_equal", "*": "multiply",
               "+": "add", "-": "subtract", "/": "divide"}
    for node, field, values in enums:
        cls = getattr(sql_ast, node)
        if not is_dataclass(cls) or field not in {f.name for f in fields(cls)}:
            raise ValueError("semantic selector has no typed AST owner")
        for value in sorted(values):
            if (node, field, value) in reuse:
                item = next(r for r in rows if r["name"] == reuse[node, field, value])
                item["selector"] = {"node": node, "field": field, "values": [value]}
                continue
            name = "sql." + node + "." + field + "." + aliases.get(value, value.replace(" ", "_"))
            prior = next((r for r in rows if r["name"] == name), None)
            if prior:
                prior["selector"]["values"].append(value)
            else:
                add("sql", name, f"{node}.{field} is {value} in the typed AST.",
                    source="engine/sql_ast.py", selector={"node": node, "field": field, "values": [value]})
    for name, meaning in SQL_SEMANTIC_FACETS:
        add("sql", "sql." + name, meaning, source="engine/sql_ast.py", facet=name)

    for name, meaning, arguments, compilation in SEMANTIC_FRAMES:
        if not set(arguments) <= set(SEMANTIC_ARGUMENT_ROLES):
            raise ValueError("semantic frame refers to an undefined argument role")
        add("ace_frame", "ace.frame." + name, meaning,
            source="explicit bounded meaning-graph profile; not a native APE grammar production",
            arguments=arguments, compilation=compilation,
            compiler_primitives_available=name != "conditional", predicate="has-" + name.replace("_", "-"))
    for d in allocation["dims"]:
        if d["family"] == "struct":
            add("structure_type_role", d["name"], "Inherited structural/type signal.",
                source="engine/data/alloc.json", inherited=d["dim_id"])
    for name in SEMANTIC_ARGUMENT_ROLES:
        add("structure_type_role", "role." + name, "Explicit meaning-graph argument role: " + name,
            source="typed semantic graph annotation; not an AST-node presence label")
    for name in SEMANTIC_DIALOGUE_ACTS:
        add("dialogue", "dialogue." + name, "Explicit conversation action/delta: " + name,
            source="conversation state/target-delta annotation")
    counts = {block: sum(r["block"] == block for r in rows)
              for block in ("property", "sql", "ace_frame", "structure_type_role", "dialogue")}
    expected = {"property": 71, "sql": 64, "ace_frame": 32, "structure_type_role": 32, "dialogue": 16}
    if counts != expected or len({r["name"] for r in rows}) != 215:
        raise ValueError(f"named coordinate contract has drifted from the reviewed allocation: {counts}")
    payload = {"schema_version": 1, "state": "defined_candidate_not_calibrated_or_promoted",
               "coordinates": rows, "blocks": counts,
               "proposed_slots": len(rows), "named_slots": len(rows), "unassigned_slots": 0,
               "ontology_contract_sha256": ontology.contract_sha256,
               "allocation_sha256": hashlib.sha256(allocation_path.read_bytes()).hexdigest(),
               "label_contract": "Expected-concept compatibility and observed-instance evidence require separate annotation/masks on the same named property axes. No ontology-compatible property is automatically an observed fact.",
               "migration": "No runtime bundle is changed. Property targets and role meanings require fresh training/calibration; proposal AST-node presence slots are replaced by explicit argument roles.",
               "scope": "Named vocabulary does not replace AST nodes, scope edges, literal values or physical identity pointers."}
    payload["contract_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                                          separators=(",", ":")).encode()).hexdigest()
    return payload
