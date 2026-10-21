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
