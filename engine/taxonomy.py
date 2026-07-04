"""taxonomy.py — the Wikidata P279 taxonomy the router serves, loaded as import-time constants.

The taxonomy is NOT authored here — it is READ from taxonomy.csv, which the training pipeline produced by
WALKING the real Wikidata P279 (subclass-of) hierarchy. So every named dim is an actual Wikidata class, never
hand-invented:

  taxonomy.csv — qid, category_1..N (REAL labels root->leaf), status, world_tables.

Importing this module needs neither Postgres nor torch — engine.router loads clean off a bare env.
"""
from __future__ import annotations
import csv
import re

from engine.config import DATA_DIR


def snake(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def name_like(v):
    """A value that looks like an entity NAME (short, has letters, not a bare number/date) — the values worth
    encoding / resolving."""
    s = str(v).strip()
    return bool(s) and len(s) <= 40 and re.search(r"[A-Za-z]", s) and not re.fullmatch(r"[\d.,:/_\- ]+", s) \
        and len(s.split()) <= 5


def _load_taxonomy():
    p = DATA_DIR / "taxonomy.csv"
    if not p.exists():
        raise SystemExit(f"{p} missing — taxonomy.csv must ship in the data directory")
    tax = []
    with open(p, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        ccols = [c for c in rd.fieldnames if c.startswith("category_")]
        for r in rd:
            if r.get("status") == "rejected":                             # rolled-up leaf: not an active type
                continue
            nodes = []
            for c in ccols:
                s = snake(r[c]) if r[c] else ""
                if s and s not in nodes:                                   # dedupe a repeated label (role > role)
                    nodes.append(s)
            if not nodes:
                continue
            tax.append({"qid": r["qid"], "path": nodes, "leaf": nodes[-1], "status": r.get("status", ""),
                        "tables": [t for t in (r.get("world_tables") or "").split(";") if t]})
    return tax


TAX = _load_taxonomy()
LEAF_PATH = {t["leaf"]: t["path"] for t in TAX}                             # leaf -> REAL P279 path (root->leaf)
LEAF_QID = {t["leaf"]: t["qid"] for t in TAX}                              # leaf -> the Wikidata QID
LEAF_TABLES = {t["leaf"]: t["tables"] for t in TAX}                        # leaf -> world tables joined ALONG the path
