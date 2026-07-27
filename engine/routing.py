"""Single source of truth for execution-engine routing — imported by BOTH live serving
(engine.knowledge_compose) and the Spider evaluation (spider.probe.full_eval), so the two can never drift.

Invariant
---------
The typed-AST planner (``engine.tables.search_ast``) owns EVERY query answerable from the uploaded tables
alone. The ``ComposeEngine`` is a world-enrichment / analytical-lowering component, NOT a competing SQL
planner: it may host a query ONLY when the query's *validated plan* grounds a world dependency — a
``world_join`` / ``world_filter`` that actually resolved against the world model.

Primitive-head predictions (``DEPTH_PRIMS``) and the ``WORLD_MEASURES`` lexical cue are EVIDENCE that a
compose plan is worth *building*; they are never sufficient authority to *select the execution engine*.
That authority is the grounded world dependency, read off the built plan by ``compose_owns``.

Consequence: a query over uploaded tables with no world entity — every Spider question, and every own-data
spreadsheet question — routes to the typed-AST planner. Own-data analytical shapes (HAVING, group-by, …)
are the planner's job; year-over-year / running-total / share / ratio are supported ONLY as world-grounded
composites (the self-contained variants were retired — they were untested and weaker than the planner).
"""
from __future__ import annotations
import re

# --- EVIDENCE to attempt a compose plan (a primitive-head read + a world-measure lexical cue). NOT authority. ---
DEPTH_PRIMS = frozenset({"EXCL", "RATIO", "TOPN", "SHARE", "TIME", "HAVING", "SORT", "DIVIDE", "RUNNING", "GROUP"})
# A distinctive WORLD numeric attribute the uploaded schema cannot expose ("population", "atomic number/mass").
WORLD_MEASURES = re.compile(r"\bpopulation\b|atomic\s+(?:number|mass)", re.I)

# --- AUTHORITY: read off the BUILT view stack. ---
# Ops that prove the plan grounded a world dependency.
WORLD_DEP_OPS = frozenset({"world_join", "world_filter"})
# Analytical composition ops (the ComposeEngine's lowering library). Over a grounded world dependency these
# are genuine world composites; over own-data they belong to the typed-AST planner and must NOT stand here.
COMPOSITION_OPS = frozenset({"yoy", "running", "share", "divide", "having", "topn", "sort", "time_filter"})


def _ops(plan):
    """Normalize a plan to a list of op strings. Accepts either built view dicts (``{'op': ...}``, the serving
    shape) or a flat list of op strings (the eval's ``res['plan']``)."""
    return [v.get("op") if isinstance(v, dict) else v for v in (plan or [])]


def world_grounded(plan) -> bool:
    """True iff the built plan actually joined or filtered on the world model."""
    return any(op in WORLD_DEP_OPS for op in _ops(plan))


def compose_owns(plan, result_rows=None) -> bool:
    """Routing AUTHORITY. The ComposeEngine may host this query ONLY when its validated plan grounds a world
    dependency AND performs a genuine composition over it (an analytical op, or a real multi-column world
    group-by that the delegate cannot produce). Plans with no world dependency ALWAYS return False — the
    typed-AST planner owns them.

    ``plan`` is the built view stack (list of dicts, serving) or the flat op list (eval); ``result_rows`` is
    the engine's result rows, used only to confirm a world group-by actually produced a multi-row breakdown.
    """
    ops = _ops(plan)
    if not any(op in WORLD_DEP_OPS for op in ops):
        return False                                       # no grounded world dependency -> typed-AST planner owns it
    if any(op in COMPOSITION_OPS for op in ops):
        return True                                        # a genuine analytical composition over the world join
    # a genuine world GROUP-BY: a world join + a multi-column group_agg (dimension + aggregate, not a bare
    # scalar) that produced rows — the delegate cannot express "total sales by continent" as per-continent rows.
    if result_rows:
        for v in (plan or []):
            if isinstance(v, dict) and v.get("op") == "group_agg" and len(v.get("columns") or []) >= 2:
                return True
    return False
