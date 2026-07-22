"""
gen20 — emit the world-type review artifacts in the SAME shape as deterministic16 (prereasoner-flat-lang).

The taxonomy is NOT authored here — it is READ from taxonomy.csv, which build_taxonomy_real.py produces by WALKING
the real Wikidata P279 (subclass-of) hierarchy. So every named dim is an actual Wikidata class, never hand-invented.

  taxonomy.csv     — (owned by build_taxonomy_real.py) qid, category_1..N (REAL labels root->leaf), status, world_tables.
  assignment.csv   — one row per TOKEN (a column header or a cell value, + the query-word examples):
                       Source, Token, Example, Category, category_1..N (readable), <struct>, <NODE dims>, <intent>
                     NODE dims = one 0/1 per REAL P279 node, co-firing down the token's path (a city ->
                     geographical_feature=1 ... populated_place=1 ... city=1). The walker assigns targets: a value ->
                     leaf via knowledgebase."words" / the discovery cache (Wikidata P31); a header -> leaf via a lexical alias.
  inference.csv    — the same rows + dim columns left EMPTY (to be filled by the model probe) + Accuracy, R2, PASS.

  $env:KB_PG_PASSWORD=(gcloud secrets versions access latest --secret=prereasoner-kb-pg-password --project prereasoner-inference)
  $env:PYTHONUTF8=1; python -m training.corpus.build_review
"""
from __future__ import annotations
import os
import csv
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.corpus.discover_csv_types import col_values, SCAN, name_like  # noqa: E402
# _pg (psycopg2) was unused here; normalize_surface (embedder/torch) is used ONLY by leaf_of_values (the Postgres path),
# so it is LAZY-imported there. Result: importing build_review for the taxonomy CONSTANTS (LEAF_PATH / NODE_DIMS / ...)
# needs neither Postgres nor torch — so training.lib.router and training.calibrate.validate_data load clean off a bare env.

OUT = ROOT / "training/data"
CACHE = OUT / "wd_cache.json"


def snake(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def printable(s):
    """reject blank / control-char / mojibake tokens (e.g. '\\x00Nb Trip\\x00') so they never become training tokens."""
    s = str(s).strip()
    return bool(s) and not any(ord(ch) < 32 or ord(ch) == 0xFFFD for ch in s)


def norm(s):
    """case/space-folded KEY for dedup, the entity-disjoint split, and contradiction detection. The token KEEPS its
    original case in the row; this only decides identity — so 'France'/'FRANCE' are ONE entity (no case-variant leak)
    and 'physics'(discipline)/'PHYSICS'(journal) are ONE token (a contradiction the encoder can't separate -> dropped)."""
    return str(s).strip().lower()


# The taxonomy is DERIVED from the REAL Wikidata P279 walk — gen20/scripts/build_taxonomy_real.py writes
# taxonomy.csv (qid, category_1..N real labels root->leaf, status, world_tables on the path). build_review does NOT
# author it; it READS it, so every named dim is an actual Wikidata class, never hand-invented.
def _load_taxonomy():
    p = OUT / "taxonomy.csv"
    if not p.exists():
        raise SystemExit(f"{p} missing — restore taxonomy.csv from the release artifacts or rebuild it via the taxonomy chain (training.taxonomy.reconcile_taxonomy -> rollup_taxonomy)")
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
# struct (datatype) + the REAL taxonomy NODES (one 0/1 dim per distinct P279 node, co-firing down a token's path:
# city -> geographical_feature=1 ... populated_place=1 ... city=1) + intent. ace/nsm REMOVED; srole -> the CATEGORY
# column (field/literal/kw/op == column_name/cell_value/SQL_kw/SQL_op); clause -> intent.
STRUCT = ["is_str", "is_num", "num_frac", "is_time", "is_bool", "is_enum", "is_key", "is_ref", "currency"]
MAXD = max(len(t["path"]) for t in TAX)
CATCOLS = [f"category_{i+1}" for i in range(MAXD)]
INTENT = ["intent_agg_count", "intent_agg_sum", "intent_agg_avg", "intent_filter_eq", "intent_filter_gt",
          "intent_filter_lt", "intent_group", "intent_sort_desc", "intent_sort_asc", "intent_limit"]
# anchored node dims = REAL P279 nodes, but TRIM single-leaf glue: keep a node only if it is SHARED by >=2 leaves (the
# generalization spine) OR it IS a leaf. A node on exactly one path is redundant with that leaf (perfectly correlated),
# and the deep raw-P279 chains otherwise explode to ~255 dims of glue (mathematical_object, baryonic_matter, ...). The
# FULL path still shows in the readable category_1..N columns; only the anchored 0/1 dims are trimmed.
_ndoc = Counter(n for t in TAX for n in t["path"])
_leaves = {t["leaf"] for t in TAX}
NODE_DIMS = sorted({n for n in _ndoc if _ndoc[n] >= 2 or n in _leaves})
DIM_NAMES = STRUCT + NODE_DIMS + INTENT                                     # category_1..N stay as READABLE metadata

# A LEXICAL alias map (NOT the taxonomy — just how a column-NAME word maps to a leaf type, for the query demo + the
# synthetic-column headers). Leaf names are the real Wikidata labels (snake): u_s_state, business, single, ...
HDR_LEAF = {
    "city": "city", "town": "city", "municipality": "city", "cities": "city", "place": "city",
    "country": "country", "nation": "country", "countries": "country",
    "state": "u_s_state", "province": "u_s_state", "region": "u_s_state", "states": "u_s_state",
    "continent": "continent", "element": "chemical_element", "chemical": "chemical_element", "symbol": "chemical_element",
    "species": "taxon", "genus": "taxon", "organism": "taxon", "taxon": "taxon",
    "gene": "gene", "protein": "gene", "name": "human", "person": "human", "author": "human", "scientist": "human",
    "occupation": "profession", "profession": "profession", "job": "profession",
    "company": "business", "business": "business", "manufacturer": "business", "brand": "business",
    "university": "university", "college": "university", "institution": "university",
    "party": "political_party", "club": "association_football_club", "team": "association_football_club",
    "film": "film", "movie": "film", "album": "album", "song": "single", "single": "single", "track": "single",
    "game": "video_game", "stadium": "stadium", "venue": "stadium", "arena": "stadium",
    "disease": "disease", "syndrome": "disease", "drug": "drug", "medication": "drug", "medicine": "drug",
    "language": "language", "sport": "type_of_sport",
}
HDR_LEAF = {k: v for k, v in HDR_LEAF.items() if v in LEAF_PATH}            # keep only aliases whose leaf is real

# CATEGORY = the token's role (one vocabulary for BOTH table tokens and query words). A query decomposes into the
# SAME four: a field reference (column_name), a literal (cell_value), an aggregate/clause word (SQL_kw -> a SQL
# function), or a comparator (SQL_op -> a SQL operator). dims: column_name/cell_value -> struct + category path;
# SQL_kw/SQL_op -> intent. Source = the token's RESOLUTION (QID | SQL function | SQL operator | the literal itself).
KW_SQL = {"total": "SUM", "sum": "SUM", "count": "COUNT", "number": "COUNT", "average": "AVG", "avg": "AVG",
          "mean": "AVG", "by": "GROUP BY", "grouped": "GROUP BY", "sorted": "ORDER BY", "sort": "ORDER BY",
          "ranked": "ORDER BY", "top": "LIMIT", "where": "WHERE", "with": "WHERE"}
OP_SQL = {"over": ">", "above": ">", "greater": ">", "under": "<", "below": "<", "less": "<",
          "equals": "=", "is": "=", ">": ">", "<": "<", "=": "="}
KNOWN_ENT = {"France": "country", "Germany": "country", "Japan": "country", "Paris": "city", "Lyon": "city",
             "Berlin": "city", "Tokyo": "city"}
# (token, category, [intent dims]) per query word — the builder fills Source + the category path deterministically.
SQL_EX = [
    ("total amount in France",
     [("total", "SQL_kw", ["intent_agg_sum"]), ("amount", "column_name", []), ("France", "cell_value", [])]),
    ("count orders over 1000 by region",
     [("count", "SQL_kw", ["intent_agg_count"]), ("orders", "column_name", []), ("over", "SQL_op", ["intent_filter_gt"]),
      ("1000", "cell_value", []), ("by", "SQL_kw", ["intent_group"]), ("region", "column_name", [])]),
    ("top 3 products sorted by sales",
     [("top", "SQL_kw", ["intent_limit"]), ("3", "cell_value", []), ("products", "column_name", []),
      ("sorted", "SQL_kw", ["intent_sort_desc"]), ("sales", "column_name", [])]),
    ("average price where year = 2023",
     [("average", "SQL_kw", ["intent_agg_avg"]), ("price", "column_name", []), ("where", "SQL_kw", []),
      ("year", "column_name", []), ("=", "SQL_op", ["intent_filter_eq"]), ("2023", "cell_value", [])]),
]
# DISJOINT held-out demo queries for the TEST split (different NL -> no Example leak; no cell_value -> no entity leak),
# so the intent probe is evaluated on unseen phrasings rather than the train queries copied verbatim.
SQL_EX_TE = [
    ("sum revenue grouped by country",
     [("sum", "SQL_kw", ["intent_agg_sum"]), ("revenue", "column_name", []), ("grouped", "SQL_kw", ["intent_group"]),
      ("country", "column_name", [])]),
    ("average score sorted descending",
     [("average", "SQL_kw", ["intent_agg_avg"]), ("score", "column_name", []), ("sorted", "SQL_kw", ["intent_sort_desc"])]),
]
# knowledgebase."words" type -> real leaf label (the States table is keyed to Q35657 'U.S. state' -> leaf u_s_state).
WWORD = {"city": "city", "country": "country", "state": "u_s_state", "element": "chemical_element",
         "continent": "continent"}
# resolved-P31 QID -> leaf. Base = the taxonomy's own leaf QIDs; the rest are SUBTYPE qids cell values resolve to.
P31_LEAF = {t["qid"]: t["leaf"] for t in TAX}
P31_LEAF.update({"Q3624078": "country", "Q12308941": "human", "Q101352": "human", "Q875538": "university",
                 "Q902104": "university", "Q113145171": "drug", "Q202866": "film", "Q15416": "film",
                 "Q5398426": "film", "Q1154710": "stadium", "Q682943": "stadium", "Q33742": "language",
                 "Q1093829": "city", "Q484170": "city", "Q1549591": "city", "Q8054": "gene", "Q8261": "gene",
                 "Q105543609": "single"})
P31_LEAF = {q: lf for q, lf in P31_LEAF.items() if lf in LEAF_PATH}         # only map to leaves that exist
NUMRE = re.compile(r"^[\s$£€]*-?[\d.,]+%?$")                                # pure number (+%); a trailing LETTER (2008b,
#                                                                          a citation) is NOT numeric — was too permissive


RAW = Path(os.environ.get("CSV_CORPUS_DIR", str(ROOT / "training/data/csv_corpus"))) / "raw_csv"


def read_cols(hx, cols, want=8, maxrows=130):
    """Read raw_csv/<hx>.csv ONCE (first maxrows lines only) and return {col: [distinct name-like values]} for the
    requested cols. One file open + one pass — far cheaper than col_values per column (which re-reads the whole file)."""
    p = RAW / f"{hx}.csv"
    if not p.exists():
        return {}
    lines = []
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh):
                lines.append(ln.rstrip("\n"))
                if i >= maxrows:
                    break
    except Exception:                                                      # noqa: BLE001
        return {}
    if not lines:
        return {}
    want_set = set(cols)
    for delim in (",", ";", "\t", "|"):
        try:
            header = next(csv.reader([lines[0]], delimiter=delim))
        except Exception:                                                  # noqa: BLE001
            continue
        idx = {c: header.index(c) for c in want_set if c in header}
        if not idx:
            continue
        out = {c: [] for c in idx}
        for line in lines[1:]:
            try:
                row = next(csv.reader([line], delimiter=delim))
            except Exception:                                              # noqa: BLE001
                continue
            for c, ci in idx.items():
                if ci < len(row):
                    v = row[ci].strip()
                    if v and name_like(v) and v not in out[c] and len(out[c]) < want:
                        out[c].append(v)
        return out
    return {}


# taxonomy.csv is OWNED by build_taxonomy_real.py (the real P279 walk); build_review READS it, never overwrites it.


def leaf_of_values(vals, cur, cache):
    from training.lib.embedder import normalize_surface                        # lazy (torch) — keeps build_review import light
    keep = [v for v in vals if name_like(v)][:10]
    norms = {v: normalize_surface(v) for v in keep}
    uq = sorted({n for n in norms.values() if n})
    word = {}
    if uq:
        cur.execute("SELECT norm, type FROM knowledgebase.\"words\" WHERE norm = ANY(%s) "
                    "AND type IN ('city','country','state','element','continent')", (uq,))
        for n, t in cur.fetchall():
            word.setdefault(n, WWORD.get(t))
    leaves = []
    for v in keep:
        lf = word.get(norms[v]) or P31_LEAF.get((cache.get(v.strip()) or [None])[0])
        if lf:
            leaves.append(lf)
    if not leaves:
        return None
    lf, c = Counter(leaves).most_common(1)[0]
    return lf if c / len(leaves) >= 0.5 else None


def _blank():
    row = {d: 0 for d in STRUCT + NODE_DIMS + INTENT}
    for c in CATCOLS:
        row[c] = ""
    return row


def _set_path(row, leaf):
    """fill the readable category_1..N labels AND co-fire the node's named dim down the whole path
    (album -> category_3='album' + work=1, musical_work=1, album=1)."""
    if leaf:
        for i, lab in enumerate(LEAF_PATH[leaf]):
            if i < len(CATCOLS):
                row[CATCOLS[i]] = lab                                      # readable label at this level (full path)
            if lab in row:                                                 # only the KEPT node dims co-fire
                row[lab] = 1


def table_row(token, leaf, is_value):
    """walker targets for a TABLE token (column_name or cell_value): struct datatype (0/1) + the taxonomy PATH in
    category_1..N (labels); intent all 0 (this token isn't part of a query)."""
    row = _blank()
    if is_value:
        isnum = bool(NUMRE.match(token))
        row["is_num"], row["is_str"] = (1, 0) if isnum else (0, 1)
        row["num_frac"] = 1 if (isnum and "." in token) else 0
    else:
        row["is_str"] = 1                                                   # a column name is text
    _set_path(row, leaf)
    return row


def query_row(token, category, dims, cache):
    """walker targets for ONE query word, by its real CATEGORY. -> (row, Source). column_name/cell_value get struct
    + the category PATH if the token resolves (header synonym / cache / known entity); SQL_kw/SQL_op get the intent
    dims. Source = the resolution: a QID for an entity, the SQL function/operator for a kw/op, else the token itself."""
    row = _blank(); src = token                                            # default Source = the literal value itself
    if category == "column_name":
        row["is_str"] = 1
        hl = HDR_LEAF.get(snake(token)) or HDR_LEAF.get(snake(token).rstrip("s"))
        if hl in LEAF_PATH:                                                # leaf must exist in the CURRENT taxonomy
            _set_path(row, hl); src = LEAF_QID.get(hl, token)
    elif category == "cell_value":
        isnum = bool(NUMRE.match(token))
        row["is_num"], row["is_str"] = (1, 0) if isnum else (0, 1)
        row["num_frac"] = 1 if (isnum and "." in token) else 0
        leaf = KNOWN_ENT.get(token) or P31_LEAF.get((cache.get(token.strip()) or [None])[0])
        if leaf in LEAF_PATH:
            _set_path(row, leaf); src = LEAF_QID.get(leaf, token)
    elif category == "SQL_kw":
        for d in dims:
            row[d] = 1
        src = KW_SQL.get(snake(token), "SQL_kw")
    elif category == "SQL_op":
        for d in dims:
            row[d] = 1
        src = OP_SQL.get(token, "SQL_op")
    return row, src


LEAF_HDR = {}                                                              # leaf -> representative header words (aliases)
for _hdr, _leaf in HDR_LEAF.items():
    LEAF_HDR.setdefault(_leaf, []).append(_hdr)
for _t in TAX:                                                             # every leaf gets at least its own label
    LEAF_HDR.setdefault(_t["leaf"], [_t["leaf"]])


# the GEO SPINE — leaves whose P279 path passes through any of these are geo (city/country/state + their settlement /
# administrative ancestors+siblings). Their data is NOT replaced: it comes from knowledgebase."words" and already routes
# correctly, and swapping a settlement ancestor (geographical_feature/municipality) for clean Wikidata instances that
# AREN'T cities collapses the city PATH's co-firing (city read -0.03 after the first pass). Only NON-geo leaves are
# replaced — that is where the noise was (a 'referral' column mapped to `hospital`; proper-noun columns taught `street`).
_GEO_SPINE = {"geolocatable_entity", "geographic_entity", "geographical_feature", "human_geographic_territorial_entity",
              "administrative_territorial_entity", "populated_place", "human_settlement", "urban_settlement",
              "territory", "region", "municipality", "neighborhood", "borough", "federated_state"}


def _is_geo_leaf(lf):
    return bool(set(LEAF_PATH.get(lf, [])) & _GEO_SPINE)


def _prefer_clean_instances(by_leaf):
    """For each NON-GEO leaf with CLEAN Wikidata instances (fetch_type_instances.py -> type_instances.json), REPLACE the
    noisy bge-mapped cell values (a 'referral' column had mapped to `hospital`; columns of proper nouns taught `street`
    to fire on any proper noun) with real P31 instances, and ADD leaves the corpus missed. This is what makes the
    anchored leaf dims DISCRIMINATIVE — `street` stops out-firing `hospital` on "Mayo Clinic"/"Cleveland Clinic". Geo
    leaves are SKIPPED (see _GEO_SPINE) so the city/country/state routing — which already works — is left untouched."""
    ti = OUT / "type_instances.json"
    if not ti.exists():
        return
    inst = json.load(open(ti, encoding="utf-8"))
    n, skipped = 0, 0
    for lf, vals in inst.items():
        if lf not in LEAF_PATH or _is_geo_leaf(lf):                     # keep the geo spine's working knowledgebase."words" data
            skipped += 1
            continue
        clean = [v for v in vals if name_like(v) and printable(v)]
        if len(clean) >= 8:
            by_leaf[lf] = clean                                          # REPLACE the noisy pool with clean instances
            n += 1
    print(f"  build_from_mapped: clean Wikidata instances REPLACED values for {n} non-geo leaves "
          f"(skipped {skipped} geo/absent)", flush=True)


def build_from_mapped():
    """COHERENT source: the SAME bge column->leaf mapping that built the taxonomy (reconcile writes mapped_columns.json
    = [{header, values, qid}], now skipping incoherent grab-bag clusters). Each leaf's DISTINCT values (deduped) +
    real headers feed an ENTITY-DISJOINT split (see _split_by_leaf): a value lands in train XOR test, never both, and
    appears once — so no value leak (the old per-COLUMN split shared values across train/test) and no x32 duplication.
    Non-geo leaves then get their values REPLACED by clean Wikidata instances (_prefer_clean_instances). Returns None if
    the mapping file is absent (fall back to the cache)."""
    mp = OUT / "mapped_columns.json"
    if not mp.exists():
        return None
    qid2leaf = {t["qid"]: t["leaf"] for t in TAX}
    by_leaf, hdrs = {}, {}                                                # leaf -> [distinct values] / [real headers]
    for d in json.load(open(mp, encoding="utf-8")):
        leaf = qid2leaf.get(d["qid"])
        if not leaf:
            continue
        vals = [str(v) for v in d.get("values", []) if name_like(v) and printable(v)]
        if len(vals) < 4:
            continue
        by_leaf.setdefault(leaf, []).extend(vals)                        # pool ALL the leaf's values (deduped in split)
        h = str(d["header"])
        if printable(h) and name_like(h):
            hdrs.setdefault(leaf, []).append(h)
    _prefer_clean_instances(by_leaf)                                     # non-geo: clean Wikidata instances REPLACE noise
    return _split_by_leaf(by_leaf, hdrs)


def build_from_cache(cache):
    """Source the TABLE tokens from wd_cache.json — REAL corpus cell values already resolved to Wikidata P31. Group
    each leaf type's values into synthetic columns of ~5 (header = a taxonomy synonym), entity-disjoint train/test.
    Fully local: no Postgres, no corpus file reads. The cache IS the deterministic walker's ground truth. If a cleaned
    per-type file exists (clean_cache.py removed the encoder-outlier mislabels), use THAT instead of the raw cache."""
    clean = OUT / "clean_types.json"
    if clean.exists():
        by_leaf = {lf: [v for v in vs if name_like(v)] for lf, vs in json.load(open(clean, encoding="utf-8")).items()}
        return _split_by_leaf(by_leaf)
    by_leaf = {}
    for v, r in cache.items():
        if not r or not r[0]:
            continue
        leaf = P31_LEAF.get(r[0])
        if leaf and name_like(v):
            by_leaf.setdefault(leaf, [])
            if v not in by_leaf[leaf]:
                by_leaf[leaf].append(v)
    return _split_by_leaf(by_leaf)


def _split_by_leaf(by_leaf, hdr_map=None):
    """{leaf: [values]} -> (train, test) token rows: ENTITY-DISJOINT 80/20 on DISTINCT values, chunked into synthetic
    columns. NO `vals[:1]` fallback — a leaf too small to hold out goes train-ONLY (never copy a train value into test,
    which was the 62-Example leak). Values are deduped (dict.fromkeys), so a value can't repeat (the x32 duplication)."""
    rng = random.Random(7)
    train, test = [], []
    MAXVALS = 250                                                          # cap distinct values/leaf (enough to anchor;
    for leaf, vals in sorted(by_leaf.items()):                            # keeps the probe tractable — uncapped, taxon
        seen, uniq = set(), []                                            # DISTINCT by norm KEY (France==FRANCE), printable
        for v in vals:
            k = norm(v)
            if k and printable(v) and k not in seen:
                seen.add(k); uniq.append(v)
        vals = uniq; rng.shuffle(vals); vals = vals[:MAXVALS]
        n = len(vals)                                                      # hold out a TEST value whenever n>=2 (so tiny
        cut = int(0.8 * n) if n >= 5 else (n - 1 if n >= 2 else n)         # leaves like taxonomic_rank are evaluated)
        hdrs = list(dict.fromkeys((hdr_map or {}).get(leaf, []))) or LEAF_HDR.get(leaf, [leaf])
        qid = LEAF_QID.get(leaf, leaf)                                     # Source = the type's Wikidata QID
        for split, part in (("tr", vals[:cut]), ("te", vals[cut:])):       # DISJOINT (te empty for tiny leaves)
            dst = train if split == "tr" else test
            for ci in range(0, len(part), 5):                              # chunk distinct values into synthetic columns
                chunk = part[ci:ci + 5]
                if not chunk:
                    continue
                hdr = hdrs[(ci // 5) % len(hdrs)]                          # vary the header alias across columns
                ex = f"{hdr}: " + "; ".join(chunk[:6])
                dst.append({"Source": qid, "Token": hdr, "Example": ex, "Category": "column_name",
                            **table_row(hdr, leaf, False)})
                for v in chunk:
                    dst.append({"Source": qid, "Token": v, "Example": ex, "Category": "cell_value",
                                **table_row(v, leaf, True)})
    return train, test


def _target_sig(r):
    return tuple(d for d in NODE_DIMS + STRUCT + INTENT if r.get(d) == 1)   # the full fired-dim signature for this token


def drop_contradictions(train, test):
    """a token encodes ONE vector (case-robustly), so it carries ONE target. Drop any (norm Token, Category) that fires
    DIFFERENT targets across rows (physics->academic_discipline AND PHYSICS->academic_journal) — case-insensitive, since
    the encoder can't separate case; impossible to learn, must be removed in the data."""
    sig = {}
    for r in train + test:
        if r["Category"] in ("column_name", "cell_value"):
            sig.setdefault((norm(r["Token"]), r["Category"]), set()).add(_target_sig(r))
    bad = {k for k, v in sig.items() if len(v) > 1}
    keep = lambda rows: [r for r in rows if (norm(r["Token"]), r["Category"]) not in bad]
    return keep(train), keep(test), len(bad)


def dedup_rows(rows):
    seen, out = set(), []                                                   # drop exact-duplicate training rows (x32 overweighting)
    for r in rows:
        k = (r["Source"], r["Token"], r["Example"], r["Category"])
        if k not in seen:
            seen.add(k); out.append(r)
    return out


def build_split(cache):
    """The ONE clean (train, test) source — called by BOTH build_review.main() AND anchor_assignment, so the held-out
    set they each WRITE can never diverge (the bug: anchor used build_from_mapped() raw + SQL_EX[:2] and skipped the
    cleaning). = build_from_mapped (or cache) + SQL_EX(train)/SQL_EX_TE(test) + the case-insensitive ENTITY-DISJOINT
    guard + contradiction drop + exact-dup drop. Returns (train, test, n_contradictions)."""
    train, test = build_from_mapped() or build_from_cache(cache)
    for dst, exs in ((train, SQL_EX), (test, SQL_EX_TE)):                  # query words -> REAL category (disjoint te NL)
        for nl, toks in exs:
            for tok, cat, dims in toks:
                row, src = query_row(tok, cat, dims, cache)
                dst.append({"Source": src, "Token": tok, "Example": nl, "Category": cat, **row})
    # a value seen in TRAIN must not appear in TEST — case-insensitive (France/FRANCE same entity); column-name aliases
    # exempt (closed vocab). Then remove contradictions + exact dups.
    train_vals = {norm(r["Token"]) for r in train if r["Category"] == "cell_value"}
    test = [r for r in test if not (r["Category"] == "cell_value" and norm(r["Token"]) in train_vals)]
    train, test, ncon = drop_contradictions(train, test)
    return dedup_rows(train), dedup_rows(test), ncon


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cache = json.load(open(CACHE, encoding="utf-8")) if CACHE.exists() else {}
    train, test, ncon = build_split(cache)
    print(f"  cleaned: dropped {ncon} contradictory (Token,Category) targets; value-disjoint test enforced")
    base = ["Source", "Token", "Example", "Category"] + CATCOLS            # category_1..4 = readable metadata
    with open(OUT / "assignment.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base + DIM_NAMES); w.writeheader()
        for r in train:
            w.writerow(r)
    with open(OUT / "inference.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base + DIM_NAMES + ["Accuracy", "R2", "PASS"]); w.writeheader()
        for r in test:
            w.writerow({**{k: r[k] for k in base}, **{d: "" for d in DIM_NAMES}, "Accuracy": "", "R2": "", "PASS": ""})
    print(f"wrote assignment.csv ({len(train)} tokens) + inference.csv ({len(test)}); "
          f"{len(DIM_NAMES)} anchored dims = struct {len(STRUCT)} + nodes {len(NODE_DIMS)} + intent {len(INTENT)}")
    print("  node dims:", ", ".join(NODE_DIMS))


if __name__ == "__main__":
    main()
