"""Single source of truth for execution-engine routing — the ONE pure decision function that BOTH live
serving (engine.knowledge_compose) and the Spider evaluation (spider.probe.full_eval) call, so the two can
never drift.

Invariant
---------
The typed-AST planner (``engine.tables.search_ast``) owns EVERY query answerable from the uploaded tables
alone. The ``ComposeEngine`` is a world-enrichment / analytical-lowering component, NOT a competing SQL
planner: it may host a query ONLY when the query's *validated plan* grounds a **necessary** world dependency
— a world attribute/value the uploaded schema does not already provide — AND composes over it.

Necessity is explicit, NOT inferred from ``world_join``
-------------------------------------------------------
A ``world_join`` proves world data was joined, not that it was *needed*. If the uploaded table already carries
the referenced country/continent, the join is redundant and the query is own-data. ``ComposeEngine.run``
therefore emits a ``world_dependency`` record (which world attributes it supplied, which the upload lacks, and
whether a value had to be world-resolved); ``route`` decides on ``world_dependency['is_necessary']``, never on
the presence of a ``world_join`` op alone.

Primitive-head predictions (``DEPTH_PRIMS``) and the ``WORLD_MEASURES`` cue are EVIDENCE that a compose plan is
worth *building*; they are never authority to *select the engine*.
"""
from __future__ import annotations
import enum
import re

# --- EVIDENCE to attempt a compose plan (a primitive-head read + a world-measure lexical cue). NOT authority. ---
DEPTH_PRIMS = frozenset({"EXCL", "RATIO", "TOPN", "SHARE", "TIME", "HAVING", "SORT", "DIVIDE", "RUNNING", "GROUP"})
# A distinctive WORLD numeric attribute the uploaded schema cannot expose ("population", "atomic number/mass").
WORLD_MEASURES = re.compile(r"\bpopulation\b|atomic\s+(?:number|mass)", re.I)

# Ops that JOIN/FILTER on the world model (they CREATE a world dependency — necessity is judged separately).
WORLD_DEP_OPS = frozenset({"world_join", "world_filter"})
# Analytical composition ops (the ComposeEngine's lowering library). Over a *necessary* world dependency these
# are genuine world composites; over own-data they belong to the typed-AST planner and must NOT stand here.
COMPOSITION_OPS = frozenset({"yoy", "running", "share", "divide", "having", "topn", "sort", "time_filter"})


class Route(enum.Enum):
    COMPOSE = "compose"    # a NECESSARY world-grounded composite -> the ComposeEngine hosts it
    DELEGATE = "delegate"  # everything else -> hand to the delegate, which owns own-data (typed-AST planner) vs
    #                        an ordinary world lookup (KnowledgeQuery). route() is a compose-ownership decision;
    #                        the AST-vs-KnowledgeQuery split is made downstream by the delegate, not here.


def _ops(plan):
    """Normalize a plan to a list of op strings. Accepts built view dicts (``{'op': ...}``, the serving shape)
    or a flat list of op strings (the eval's ``res['plan']``)."""
    return [v.get("op") if isinstance(v, dict) else v for v in (plan or [])]


def world_grounded(plan) -> bool:
    """True iff the plan JOINED/FILTERED on the world model. A weaker signal than necessity (see module doc):
    diagnostic only — ``route`` decides on ``world_dependency['is_necessary']``, not this."""
    return any(op in WORLD_DEP_OPS for op in _ops(plan))


def _composes(plan, result_rows) -> bool:
    """The plan performs a genuine composition: an analytical op, or a multi-column group_agg that produced
    rows (a per-dimension breakdown the delegate cannot express as one scalar)."""
    ops = _ops(plan)
    if any(op in COMPOSITION_OPS for op in ops):
        return True
    if result_rows:
        for v in (plan or []):
            if isinstance(v, dict) and v.get("op") == "group_agg" and len(v.get("columns") or []) >= 2:
                return True
    return False


def route(plan, world_dependency=None, result_rows=None) -> Route:
    """THE routing decision — the single pure function BOTH serving and the Spider eval call.

    ``plan``             the built compose view stack (list of dicts) or flat op list (the eval's res['plan']).
    ``world_dependency`` ComposeEngine.run's explicit record, or None when no world join grounded.
    ``result_rows``      the engine's result rows (confirms a world group-by produced a real breakdown).

    Returns Route.COMPOSE iff the plan grounds a NECESSARY world dependency AND composes over it. Otherwise
    Route.DELEGATE — hand off to the delegate, which owns own-data (the typed-AST planner) and ordinary world
    lookups (KnowledgeQuery). route() decides ONLY compose-ownership; the AST-vs-KnowledgeQuery split is made
    downstream, so a necessary-but-non-composing world lookup is DELEGATE, not COMPOSE."""
    if not (world_dependency and world_dependency.get("is_necessary")):
        return Route.DELEGATE                                 # no NECESSARY world dependency -> delegate (own-data)
    return Route.COMPOSE if _composes(plan, result_rows) else Route.DELEGATE


def compose_owns(plan, world_dependency=None, result_rows=None) -> bool:
    """Boolean convenience over ``route``: does the ComposeEngine own this query?"""
    return route(plan, world_dependency, result_rows) is Route.COMPOSE
