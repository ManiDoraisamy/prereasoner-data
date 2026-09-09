"""Analytical PRIMITIVES as deterministic view builders. Each returns the SQL for ONE view over its input
relation (a base table or a prior view). Complex analytics = a STACK of these, NOT one complex query the
single-template engine can't emit. You template the ~handful of analytical *operators*, and compose them by
depth.

These are pure SQL-string builders (no model, no I/O) — the deterministic core of the composition engine.
"""
from __future__ import annotations


def q(name):
    """quote an identifier"""
    return '"' + str(name).replace('"', '""') + '"'


def lit(v):
    """SQL literal — numbers bare, everything else a quoted string"""
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    try:
        float(s.replace(",", "")); return s
    except ValueError:
        return "'" + s.replace("'", "''") + "'"


def filter_view(src, conds):
    """row filter. conds: [(col, op, value)] -> SELECT * WHERE c1 op v1 AND ...  (e.g. exclude returns)"""
    where = " AND ".join(f'{q(c)} {op} {lit(v)}' for c, op, v in conds)
    return f'SELECT * FROM {q(src)} WHERE {where}'


def group_agg_view(src, by, aggs):
    """grouped aggregate. by: [col]; aggs: [(fn, col, out)] -> SELECT by, fn(col) AS out ... GROUP BY by.
    A COUNT agg with col None/'*' emits COUNT(*) (a ROW count — not COUNT(<col>), which skips NULLs)."""
    def one(fn, col, out):
        function = fn.upper()
        if function == "COUNT" and (col is None or col == "*"):
            return f'COUNT(*) AS {q(out)}'
        function = {
            "SUM": "decimal_sum", "AVG": "decimal_avg",
            "MIN": "decimal_min", "MAX": "decimal_max",
        }.get(function, function)
        return f'{function}({q(col)}) AS {q(out)}'
    parts = [q(c) for c in by] + [one(fn, col, out) for fn, col, out in aggs]
    sql = f'SELECT {", ".join(parts)} FROM {q(src)}'
    if by:
        sql += f' GROUP BY {", ".join(q(c) for c in by)}'
    return sql


def yoy_view(src, key, time, measure, out):
    """period-over-period growth of `measure` (optionally per `key`), via a SELF-JOIN on consecutive `time` — i.e. a
    window function expressed as a join, so it stays inside the deterministic engine. key=None -> one series over time."""
    ksel = f't.{q(key)} AS {q(key)}, ' if key else ''
    kjoin = f't.{q(key)} = p.{q(key)} AND ' if key else ''
    return (f'SELECT {ksel}t.{q(time)} AS {q(time)}, t.{q(measure)} AS {q(measure)}, '
            f'decimal_div(decimal_sub(t.{q(measure)}, p.{q(measure)}), p.{q(measure)}) AS {q(out)} '
            f'FROM {q(src)} t JOIN {q(src)} p ON {kjoin}t.{q(time)} = p.{q(time)} + 1')


def topn_view(src, order, desc, n, select=None):
    """rank / top-N: ORDER BY order [DESC], projecting `select` (or *). n=None -> SORT only (no LIMIT); n=int -> top-N."""
    sel = ", ".join(q(c) for c in select) if select else "*"
    sql = f'SELECT {sel} FROM {q(src)} ORDER BY {q(order)} COLLATE decimal {"DESC" if desc else "ASC"}'
    return sql + (f' LIMIT {int(n)}' if n is not None else '')


def share_view(src, dim, measure, out):
    """share-of-total: each row's `measure` divided by the grand total."""
    return (f'SELECT {q(dim)} AS {q(dim)}, {q(measure)} AS {q(measure)}, '
            f'decimal_div({q(measure)}, (SELECT decimal_sum({q(measure)}) FROM {q(src)})) AS {q(out)} '
            f'FROM {q(src)}')


def divide_view(src, num, den, out, keep=None):
    """ratio of TWO measures per row: num / den (e.g. margin = profit / revenue, revenue-per-order). `keep` are
    pass-through columns (the group keys). Both measures are assumed already aggregated to one row per key."""
    cols = [f'{q(c)} AS {q(c)}' for c in (keep or [])]
    cols += [f'{q(num)} AS {q(num)}', f'{q(den)} AS {q(den)}',
             f'decimal_div({q(num)}, {q(den)}) AS {q(out)}']
    return f'SELECT {", ".join(cols)} FROM {q(src)}'


def running_view(src, key, time, measure, out):
    """cumulative (running) total of `measure` over `time` (optionally within `key`), via a correlated sum of all
    earlier-or-equal periods — a window function expressed in plain SQL so it stays in the deterministic engine."""
    ksel = f't.{q(key)} AS {q(key)}, ' if key else ''
    kjoin = f'p.{q(key)} = t.{q(key)} AND ' if key else ''
    return (f'SELECT {ksel}t.{q(time)} AS {q(time)}, t.{q(measure)} AS {q(measure)}, '
            f'(SELECT decimal_sum(p.{q(measure)}) FROM {q(src)} p WHERE {kjoin}p.{q(time)} <= t.{q(time)}) AS {q(out)} '
            f'FROM {q(src)} t')


# ---- BASE relations: a multi-table FK join, or a world-meaning join, that the analytical primitives stack on ----
def join_view(fact, joins):
    """flatten a star schema into one base relation: the fact's own columns + each dimension's descriptive columns.
    joins: [(dim_table, fk_col, pk_col, [keep_cols])] -> SELECT fact.*, dim.keep... FROM fact LEFT JOIN dim ON
    fact.fk = dim.pk. The analytical stack then runs on flat column names (orders+customers -> ...by city)."""
    sel = [f'{q(fact)}.*']
    frm = q(fact)
    for dim, fk, pk, keep in joins:
        frm += f' LEFT JOIN {q(dim)} ON {q(fact)}.{q(fk)} = {q(dim)}.{q(pk)}'
        sel += [f'{q(dim)}.{q(c)} AS {q(c)}' for c in keep]
    return f'SELECT {", ".join(sel)} FROM {frm}'


def world_join_view(src, on_col, world, world_on, keep):
    """join an uploaded relation to the WORLD meaning table so an absent attribute (country/continent/currency) is
    resolved by the join: SELECT src.*, world.keep... FROM src LEFT JOIN world ON src.on = world.world_on. (Offline
    the link is an exact value match; in the live engine it is the bge-resolved bridge — same SQL shape.)"""
    sel = [f'{q(src)}.*'] + [f'{q(world)}.{q(c)} AS {q(c)}' for c in keep]
    return f'SELECT {", ".join(sel)} FROM {q(src)} LEFT JOIN {q(world)} ON {q(src)}.{q(on_col)} = {q(world)}.{q(world_on)}'
