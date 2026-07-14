"""Canonical Spider difficulty + SQL-shape features, ported verbatim from the official
`taoyds/spider` evaluation.py so our difficulty labels match the benchmark's own `eval_hardness`
byte-for-byte. Operates on the pre-parsed `sql` dict shipped inside dev.json (no re-parsing).

Source of truth: https://github.com/taoyds/spider/blob/master/evaluation.py
"""
from __future__ import annotations

# --- op vocabularies (verbatim from evaluation.py / process_sql.py) ---
WHERE_OPS = ('not', 'between', '=', '>', '<', '>=', '<=', '!=', 'in', 'like', 'is', 'exists')
UNIT_OPS = ('none', '-', '+', "*", '/')
AGG_OPS = ('none', 'max', 'min', 'count', 'sum', 'avg')


def has_agg(unit):
    return unit[0] != AGG_OPS.index('none')


def count_agg(units):
    return len([unit for unit in units if has_agg(unit)])


def get_nestedSQL(sql):
    nested = []
    for cond_unit in sql['from']['conds'][::2] + sql['where'][::2] + sql['having'][::2]:
        if type(cond_unit[3]) is dict:
            nested.append(cond_unit[3])
        if type(cond_unit[4]) is dict:
            nested.append(cond_unit[4])
    if sql['intersect'] is not None:
        nested.append(sql['intersect'])
    if sql['except'] is not None:
        nested.append(sql['except'])
    if sql['union'] is not None:
        nested.append(sql['union'])
    return nested


def count_component1(sql):
    count = 0
    if len(sql['where']) > 0:
        count += 1
    if len(sql['groupBy']) > 0:
        count += 1
    if len(sql['orderBy']) > 0:
        count += 1
    if sql['limit'] is not None:
        count += 1
    if len(sql['from']['table_units']) > 0:  # JOIN
        count += len(sql['from']['table_units']) - 1
    ao = sql['from']['conds'][1::2] + sql['where'][1::2] + sql['having'][1::2]
    count += len([token for token in ao if token == 'or'])
    cond_units = sql['from']['conds'][::2] + sql['where'][::2] + sql['having'][::2]
    count += len([c for c in cond_units if c[1] == WHERE_OPS.index('like')])
    return count


def count_component2(sql):
    return len(get_nestedSQL(sql))


def count_others(sql):
    count = 0
    agg_count = count_agg(sql['select'][1])
    agg_count += count_agg(sql['where'][::2])
    agg_count += count_agg(sql['groupBy'])
    if len(sql['orderBy']) > 0:
        agg_count += count_agg([unit[1] for unit in sql['orderBy'][1] if unit[1]] +
                               [unit[2] for unit in sql['orderBy'][1] if unit[2]])
    agg_count += count_agg(sql['having'])
    if agg_count > 1:
        count += 1
    if len(sql['select'][1]) > 1:
        count += 1
    if len(sql['where']) > 1:
        count += 1
    if len(sql['groupBy']) > 1:
        count += 1
    return count


def eval_hardness(sql):
    c1 = count_component1(sql)
    c2 = count_component2(sql)
    co = count_others(sql)
    if c1 <= 1 and co == 0 and c2 == 0:
        return "easy"
    elif (co <= 2 and c1 <= 1 and c2 == 0) or (c1 <= 2 and co < 2 and c2 == 0):
        return "medium"
    elif (co > 2 and c1 <= 2 and c2 == 0) or (2 < c1 <= 3 and co <= 2 and c2 == 0) or \
            (c1 <= 1 and co == 0 and c2 <= 1):
        return "hard"
    else:
        return "extra"


# --- structural shape features used by the envelope probe ---
def table_unit_indices(sql):
    """the base-table indices referenced in FROM (type 'table_unit'); 'sql' units are FROM-subqueries."""
    idx, subq = [], 0
    for tu in sql['from']['table_units']:
        if tu[0] == 'table_unit':
            idx.append(tu[1])
        else:
            subq += 1
    return idx, subq


def shape(sql):
    """A flat feature dict describing the gold query's structure."""
    tidx, from_subq = table_unit_indices(sql)
    where_conds = sql['where'][::2]
    return {
        "n_from_tables": len(tidx),
        "from_subquery": from_subq > 0,
        "self_join": len(tidx) != len(set(tidx)),
        "join": len(sql['from']['table_units']) > 1,
        "group_by": len(sql['groupBy']) > 0,
        "having": len(sql['having']) > 0,
        "order_by": len(sql['orderBy']) > 0,
        "limit": sql['limit'] is not None,
        "order_limit": len(sql['orderBy']) > 0 and sql['limit'] is not None,
        "n_where": len(where_conds),
        "n_select": len(sql['select'][1]),
        "distinct": bool(sql['select'][0]),
        "has_or": 'or' in (sql['where'][1::2] + sql['having'][1::2]),
        "has_like": any(c[1] == WHERE_OPS.index('like') for c in where_conds),
        "set_op": (sql['intersect'] is not None) or (sql['union'] is not None) or (sql['except'] is not None),
        "nested_pred": any(type(c[3]) is dict or type(c[4]) is dict
                           for c in (sql['from']['conds'][::2] + where_conds + sql['having'][::2])),
        "aggs": _aggs(sql),
    }


def _aggs(sql):
    ops = set()
    for unit in sql['select'][1]:                       # (agg_id, val_unit)
        if unit[0]:
            ops.add(AGG_OPS[unit[0]])
        vu = unit[1]                                     # (unit_op, col_unit1, col_unit2)
        if vu[1] and vu[1][0]:
            ops.add(AGG_OPS[vu[1][0]])
    for c in sql['having'][::2]:
        vu = c[2]
        if vu and vu[1] and vu[1][0]:
            ops.add(AGG_OPS[vu[1][0]])
    if sql['orderBy']:
        for vu in sql['orderBy'][1]:
            if vu[1] and vu[1][0]:
                ops.add(AGG_OPS[vu[1][0]])
    return sorted(ops)
