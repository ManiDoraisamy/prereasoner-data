"""Pre-create legacy qid-keyed Wikidata serving tables.

For each taxonomy leaf, this adds an empty table named by the exact Wikidata label and
copies the columns from the existing ``knowledgebase.<snake_label>`` mirror. Lazy entity
synchronization can also create these tables, so running this command is optional.

This builder is deliberately additive: it never drops or replaces a table or schema. An
existing incompatible alias is an operator-visible error, not permission to destroy data.

Run after ``sync_types.py`` (and preferably ``mirror_schema.py``)::

    python -m db.sync.build_wikipedia
"""
from __future__ import annotations
import csv
from pathlib import Path

try:
    from _conn import connect
    from sync_entity import snake
except ImportError:
    from ._conn import connect
    from .sync_entity import snake

TAXONOMY = Path(__file__).resolve().parent / "data" / "taxonomy.csv"
_PG_IDENTIFIER_BYTES = 63


def _leaf_qid():
    """{snake(leaf label) -> qid} from taxonomy.csv (accepted/added rows)."""
    out = {}
    for r in csv.DictReader(open(TAXONOMY, encoding="utf-8")):
        if r.get("status") not in ("accepted", "added"):
            continue
        cats = [r[f"category_{i}"] for i in range(1, 10) if r.get(f"category_{i}")]
        if cats:
            out[snake(cats[-1])] = r["qid"]
    return out


def _quote_ident(value: str) -> str:
    """Quote a trusted catalog-derived PostgreSQL identifier."""
    return '"' + value.replace('"', '""') + '"'


def _identifier(label: str, suffix: str = "") -> str:
    """Fit a UTF-8 label in PostgreSQL's 63-byte identifier limit, preserving suffix."""
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) > _PG_IDENTIFIER_BYTES:
        raise ValueError("identifier suffix exceeds PostgreSQL's 63-byte limit")
    available = _PG_IDENTIFIER_BYTES - len(suffix_bytes)
    raw = label.encode("utf-8")[:available]
    while raw:
        try:
            stem = raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    else:
        stem = ""
    return stem + suffix


def _exists(cur, schema, table):
    cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (schema, table))
    return cur.fetchone() is not None


def _has_qid(cur, table):
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='knowledgebase' AND table_name=%s AND column_name='qid'",
        (table,),
    )
    return cur.fetchone() is not None


def main():
    conn = connect(); cur = conn.cursor()
    leaf_qid = _leaf_qid()

    # ---- 1+2: wikipedia schema + EMPTY faithful tables, qid PRIMARY KEY ----
    cur.execute("CREATE SCHEMA IF NOT EXISTS knowledgebase")
    cur.execute('SELECT qid, label FROM knowledgebase."types"')
    qlabel = {q: l for q, l in cur.fetchall()}
    mirrors = set(leaf_qid)                     # the snake mirror tables are SOURCES — never a DROP target
    created, existing, skipped, seen = 0, 0, 0, {}
    exact = 0
    for leaf in sorted(leaf_qid):
        qid = leaf_qid[leaf]
        if not _exists(cur, "knowledgebase", leaf):                         # no faithful schema mirror -> discover later
            skipped += 1; continue
        if not _has_qid(cur, leaf):
            skipped += 1; continue
        label = _identifier((qlabel.get(qid) or leaf).strip() or leaf)
        if label == leaf:                   # single-word label ("city", "country", "film"): the mirror table ALREADY
            seen[label] = leaf
            exact += 1; continue
        if label in seen or label in mirrors:
            label = _identifier(label, f" ({qid})")
        seen[label] = leaf
        if _exists(cur, "knowledgebase", label):
            if not _has_qid(cur, label):
                raise RuntimeError(
                    f'existing knowledgebase.{label!r} is incompatible: missing qid column'
                )
            existing += 1
            continue
        qlabel_ident = _quote_ident(label)
        leaf_ident = _quote_ident(leaf)
        cur.execute(
            f'CREATE TABLE knowledgebase.{qlabel_ident} '
            f'(LIKE knowledgebase.{leaf_ident} INCLUDING DEFAULTS INCLUDING GENERATED)'
        )
        cur.execute(f'ALTER TABLE knowledgebase.{qlabel_ident} ALTER COLUMN qid SET NOT NULL')
        cur.execute(f'ALTER TABLE knowledgebase.{qlabel_ident} ADD PRIMARY KEY (qid)')
        created += 1
    print(
        f"wikidata projections: {created} created, {existing} existing, "
        f"{exact} exact-name mirrors, {skipped} skipped (missing qid mirror)",
        flush=True,
    )
    conn.commit()


if __name__ == "__main__":
    main()
