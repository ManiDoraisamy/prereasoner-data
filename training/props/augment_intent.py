#!/usr/bin/env python3
"""Intent-corpus augmentation — anchor the SERVING phrasings the base corpus never taught.

The corpus supervises each aggregate with exactly ONE cue token ("count" -> intent_agg_count,
"total" -> intent_agg_sum, "average" -> intent_agg_avg; audited 2026-07-22) and NO "in <place>"
contexts. Serving (engine/encoder_overlay.read_op_model) relies on the encoder GENERALIZING
"how many" -> count etc. — an un-anchored behavior that drifted in the first property fine-tune
(SUM 0.121 edged COUNT 0.110 on "how many customers in France"). This script makes those
phrasings TRAINED, in the exact per-token unit format of the existing intent graphs:

  count  source "count X per Y"  ->  "how(NONE) many(COUNT) X per Y"      (per-Y kept)
                                     "how many X in <place>"              (per-Y -> in <place>)
                                     "number(COUNT) of(NONE) X per Y"
  sum    source "total X per Y"  ->  "sum(SUM) of(NONE) X per Y"
                                     "how(NONE) much(SUM) X in <place>"
                                     "total X in <place>"
  avg    source "average X per Y" -> "average X in <place>"
  NONE contrast (suppression)    ->  "show(NONE) X in <place>"  /  bare "X in <place>"

CRITICAL — place ⊥ class. Every "in <place>" variant (aggregate AND None) draws its place from
ONE round-robin counter over all 12 PLACES, incremented per place-use across the interleaved
emission order. So each place appears across COUNT/SUM/AVG/None in ~equal measure; the model
CANNOT learn "in <place> => aggregate" (the confound an earlier version had, where None used only
4 of 12 places). A None variant is emitted for EVERY aggregate source, so the "in <place> is NOT
an aggregate" signal is as strong as the aggregate signal and spans all places.

Skipped on purpose: "mean"/"avg" as AVG cues — they occur 17x/8x in the corpus as NONE (data
column names in questions); labeling them AVG would contradict.

Split: deterministic sha1 of the QUESTION TEXT — 10% of phrasings calibrate the three operator
gates, the next 15% are held out in data/intent_eval.jsonl, and the rest go to
data/intent_aug_train.jsonl. Keying on the question text (not the
variant id) is load-bearing: corpus questions are template-generated, so the same text recurs
across many schemas — a per-variant split leaked 42% of eval questions verbatim into train. The
eval is deduped to one graph per question text, and per-question-text duplicates in TRAIN are
capped (DUP_CAP) so no single source schema dominates a class. Handcrafted PROBES replicate the
live serving failures AND add HELD-OUT TEMPLATES ("how many X are there", "what is the total X",
"count of X", bare "X in <place>" over all 12 places) — none of those templates are trained, so
the probes measure phrasing GENERALIZATION, not filler recombination. Every variant/probe carries
"expect": COUNT|SUM|AVG|null for the read_op_model-mirror accuracy metric (eval_intent.py).

  python -m training.props.augment_intent
"""
from __future__ import annotations
import hashlib, json, os, random
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data"); os.makedirs(DATA, exist_ok=True)

PLACES = ["France", "Germany", "Japan", "Italy", "Spain", "India",
          "Brazil", "Canada", "Texas", "California", "Europe", "Asia"]
RELATIONS = ["country", "continent", "region", "category", "department", "group"]
AGG = {"intent_agg_count": "COUNT", "intent_agg_sum": "SUM", "intent_agg_avg": "AVG"}
CALIBRATION_PCT = 10                                          # first hash bucket; never gradient-trained
EVAL_PCT = 15                                                 # next bucket; never calibrates or trains
DUP_CAP = 4                                                    # max TRAIN graphs per identical question text


def qunit(text, fired=(), tenkey=False):
    """A question-token unit in the exact shape of the corpus generator (and of serving's
    _question_readout). tenkey mirrors the join-graph shape (extra table/colname keys) so a
    variant built from a join source stays field-homogeneous."""
    u = {"text": text, "group": "q", "kind": "q", "col": -1, "link": None, "row": -2,
         "fired": list(fired), "sup": ["intent"]}
    if tenkey:
        u["table"] = None; u["colname"] = None
    return u


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def split_units(g):
    q = [u for u in g["units"] if u.get("group") == "q"]
    other = [u for u in g["units"] if u.get("group") != "q"]
    return other, q


def is_tenkey(qtoks):
    return any("colname" in u for u in qtoks)


def agg_cue_index(qtoks):
    idxs = [i for i, u in enumerate(qtoks) if any(f in AGG for f in u.get("fired", []))]
    return idxs[0] if len(idxs) == 1 else None


def per_index(qtoks):
    for i, u in enumerate(qtoks):
        if "intent_group" in u.get("fired", []):
            return i
    return None


def place_tail(qtoks, place, tk):
    """Replace the 'per Y' tail (if any) with 'in <place>' (both NONE); else append it. Dropping
    the tail at the 'per' (intent_group) token means no labeled token is left behind mislabeled."""
    pi = per_index(qtoks)
    head = qtoks[:pi] if pi is not None else list(qtoks)
    return head + [qunit("in", tenkey=tk), qunit(place, tenkey=tk)]


def variant(src, tag, qtoks, expect, i):
    other, _ = split_units(src)
    out = {k: v for k, v in src.items() if k != "units"}
    out["units"] = [dict(u) for u in other] + qtoks
    out["aug"] = tag
    out["expect"] = expect
    out["file"] = f"aug:{tag}:{i}:{src.get('file') or src.get('fact', '?')}"
    return out


def make_variants(g, i, next_place):
    """Aggregate variants for one source. Each *_place variant draws a fresh round-robin place from
    next_place() so place ⊥ class across the whole (interleaved) emission order."""
    _, q = split_units(g)
    ci = agg_cue_index(q)
    if ci is None:
        return []
    tk = is_tenkey(q)
    dim = next(f for f in q[ci]["fired"] if f in AGG)
    op = AGG[dim]
    pre, post = q[:ci], q[ci + 1:]
    out = []
    if op == "COUNT":
        hm = pre + [qunit("how", tenkey=tk), qunit("many", [dim], tenkey=tk)] + post
        out.append(variant(g, "howmany", hm, "COUNT", i))
        out.append(variant(g, "howmany_place", place_tail(hm, next_place(), tk), "COUNT", i))
        out.append(variant(g, "numberof", pre + [qunit("number", [dim], tenkey=tk), qunit("of", tenkey=tk)] + post, "COUNT", i))
        out.append(variant(g, "countof", pre + [qunit("count", [dim], tenkey=tk), qunit("of", tenkey=tk)] + post, "COUNT", i))
        out.append(variant(g, "countthe", pre + [qunit("count", [dim], tenkey=tk), qunit("the", tenkey=tk)] + post, "COUNT", i))
    elif op == "SUM":
        out.append(variant(g, "sumof", pre + [qunit("sum", [dim], tenkey=tk), qunit("of", tenkey=tk)] + post, "SUM", i))
        out.append(variant(g, "howmuch_place",
                           place_tail(pre + [qunit("how", tenkey=tk), qunit("much", [dim], tenkey=tk)] + post, next_place(), tk), "SUM", i))
        out.append(variant(g, "total_place", place_tail(list(q), next_place(), tk), "SUM", i))
    elif op == "AVG":
        out.append(variant(g, "avg_place", place_tail(list(q), next_place(), tk), "AVG", i))
    return out


def none_variant(g, i, place, alt):
    """Suppression contrast: same schema/question shape, NO aggregate -> expect None. `alt` picks
    'show X in <place>' vs bare 'X in <place>' so both forms span all places."""
    _, q = split_units(g)
    ci = agg_cue_index(q)
    if ci is None:
        return None
    tk = is_tenkey(q)
    rest = q[:ci] + q[ci + 1:]
    if alt or not rest:
        return variant(g, "show_place", place_tail([qunit("show", tenkey=tk)] + rest, place, tk), None, i)
    return variant(g, "bare_place", place_tail(rest, place, tk), None, i)


def relation_none_variant(g, i, place):
    """Heldout-pattern support: relation questions are projections, never aggregates.

    Entity names and relation nouns rotate independently of the handcrafted Kyoto probe, and the
    exact probe text remains excluded by the ordinary question-text leakage guard.
    """
    _, q = split_units(g)
    tk = is_tenkey(q)
    noun = RELATIONS[i % len(RELATIONS)]
    tokens = [
        qunit("which", tenkey=tk),
        qunit(noun, tenkey=tk),
        qunit("is", tenkey=tk),
        qunit(place, tenkey=tk),
        qunit("in", tenkey=tk),
    ]
    return variant(g, "which_relation", tokens, None, i)


RANKING_ENTITIES = (
    "cities", "products", "singers", "customers", "orders", "hospitals",
    "employees", "books", "flights", "restaurants", "schools", "countries",
)
RANKING_MEASURES = (
    "population", "revenue", "concert count", "order value", "bed count", "salary",
    "rating", "duration", "price", "transaction volume", "enrollment", "area",
)


def ranking_none_variant(g, i):
    """Suppression contrast: a numeric top/bottom bound is LIMIT, not COUNT intent."""
    _, q = split_units(g)
    tk = is_tenkey(q)
    direction = ("top", "highest", "lowest", "largest")[i % 4]
    number = str(2 + ((i // 4) % 9))
    entity = RANKING_ENTITIES[(i // 36) % len(RANKING_ENTITIES)]
    measure = RANKING_MEASURES[(i // 7) % len(RANKING_MEASURES)]
    tokens = [
        qunit(direction, tenkey=tk),
        qunit(number, tenkey=tk),
        qunit(entity, tenkey=tk),
        qunit("by", tenkey=tk),
        *(qunit(word, tenkey=tk) for word in measure.split()),
    ]
    return variant(g, "ranking_limit", tokens, None, i)


# ---- handcrafted probes: live serving cases + HELD-OUT templates (never trained) ----
def probe(name, columns, question_tokens, expect):
    units = [{"text": c, "group": "schema", "kind": "name", "col": ci, "row": -1, "fired": [], "sup": []}
             for ci, c in enumerate(columns)]
    units += [qunit(t, f) for t, f in question_tokens]
    return {"file": f"probe:{name}", "task": "intent", "probe": name, "expect": expect, "units": units}


CUST = ["name", "city", "remarks"]
CITY = ["city", "population", "country"]
PROBES = [
    # the live serving cases (trained templates, verbatim serving phrasing)
    probe("howmany_customers_france", CUST,
          [("how", []), ("many", ["intent_agg_count"]), ("customers", []), ("in", []), ("France", [])], "COUNT"),
    probe("howmany_orders", CUST, [("how", []), ("many", ["intent_agg_count"]), ("orders", [])], "COUNT"),
    probe("count_the_customers", CUST, [("count", ["intent_agg_count"]), ("the", []), ("customers", [])], "COUNT"),
    probe("numberof_customers_germany", CUST,
          [("number", ["intent_agg_count"]), ("of", []), ("customers", []), ("in", []), ("Germany", [])], "COUNT"),
    probe("total_amount_france", CUST,
          [("total", ["intent_agg_sum"]), ("amount", []), ("in", []), ("France", [])], "SUM"),
    probe("howmuch_did_we_sell", CUST,
          [("how", []), ("much", ["intent_agg_sum"]), ("did", []), ("we", []), ("sell", [])], "SUM"),
    probe("average_population_cities", CITY,
          [("average", ["intent_agg_avg"]), ("population", []), ("of", []), ("cities", [])], "AVG"),
    probe("which_continent_kyoto", CITY,
          [("which", []), ("continent", []), ("is", []), ("Kyoto", []), ("in", [])], None),
    probe("top3_cities_population", CITY,
          [("top", []), ("3", []), ("cities", []), ("by", []), ("population", [])], None),
    probe("largest5_hospitals_beds", CITY,
          [("largest", []), ("5", []), ("hospitals", []), ("by", []), ("bed", []), ("count", [])], None),
    probe("lowest4_products_price", CITY,
          [("lowest", []), ("4", []), ("products", []), ("by", []), ("price", [])], None),
    # HELD-OUT TEMPLATES (novel phrasings — cue words trained, template never trained)
    probe("howmany_there_products_spain", CUST,
          [("how", []), ("many", ["intent_agg_count"]), ("products", []), ("are", []), ("there", []),
           ("in", []), ("Spain", [])], "COUNT"),
    probe("whatis_total_revenue_italy", CUST,
          [("what", []), ("is", []), ("the", []), ("total", ["intent_agg_sum"]), ("revenue", []),
           ("in", []), ("Italy", [])], "SUM"),
    probe("count_of_orders_brazil", CUST,
          [("count", ["intent_agg_count"]), ("of", []), ("orders", []), ("in", []), ("Brazil", [])], "COUNT"),
    probe("whatis_average_price_canada", CUST,
          [("what", []), ("is", []), ("the", []), ("average", ["intent_agg_avg"]), ("price", []),
           ("in", []), ("Canada", [])], "AVG"),
]
# None over ALL 12 places (directly tests the place⊥class fix — bare "<noun> in <place>" -> None)
PROBES += [probe(f"bare_customers_{p.lower()}", CUST, [("customers", []), ("in", []), (p, [])], None)
           for p in PLACES]


def main():
    rng = random.Random(0)
    srcs = []
    for f in ("sql_graphs_train.jsonl", "join_graphs_train.jsonl"):
        srcs += [g for g in load(os.path.join(DATA, f)) if g.get("task") == "intent"]
    agg_srcs = [g for g in srcs if agg_cue_index(split_units(g)[1]) is not None]
    rng.shuffle(agg_srcs)

    def qtext(g):
        return " ".join(u["text"] for u in g["units"] if u.get("group") == "q")

    pc = [0]                                                   # round-robin place counter (place ⊥ class)
    def next_place():
        p = PLACES[pc[0] % len(PLACES)]; pc[0] += 1; return p

    all_variants = []
    for i, g in enumerate(agg_srcs):
        all_variants += make_variants(g, i, next_place)
        nv = none_variant(g, i, next_place(), alt=(i % 2 == 0))   # a None variant for EVERY source
        if nv:
            all_variants += [nv]
        all_variants.append(relation_none_variant(g, i, next_place()))
        all_variants.append(ranking_none_variant(g, i))

    # Split by question text; calibration and eval are separately deduped. Named probes are eval-only.
    probe_texts = {qtext(p) for p in PROBES}
    train, calibration, ev = [], [], []
    calibration_seen, ev_seen, train_qcount = set(), set(), Counter()
    for v in all_variants:
        qt = qtext(v)
        if qt in probe_texts:                                # never train a probe's exact phrasing
            continue
        bucket = int(hashlib.sha1(qt.encode(), usedforsecurity=False).hexdigest(), 16) % 100
        if bucket < CALIBRATION_PCT:
            if qt not in calibration_seen:
                calibration_seen.add(qt); calibration.append(v)
        elif bucket < CALIBRATION_PCT + EVAL_PCT:
            if qt not in ev_seen:
                ev_seen.add(qt); ev.append(v)
        else:
            if train_qcount[qt] < DUP_CAP:                    # cap identical-text dup so one schema can't dominate
                train_qcount[qt] += 1; train.append(v)
    ev += PROBES

    train_texts = {qtext(v) for v in train}
    leak = (
        (train_texts & calibration_seen)
        | (train_texts & ev_seen)
        | (train_texts & probe_texts)
        | (calibration_seen & ev_seen)
        | (calibration_seen & probe_texts)
    )
    assert not leak, f"question-text leakage across split: {sorted(leak)[:3]}"

    with open(os.path.join(DATA, "intent_aug_train.jsonl"), "w", encoding="utf-8") as f:
        for g in train:
            f.write(json.dumps(g) + "\n")
    with open(os.path.join(DATA, "intent_calibration.jsonl"), "w", encoding="utf-8") as f:
        for g in calibration:
            f.write(json.dumps(g) + "\n")
    with open(os.path.join(DATA, "intent_eval.jsonl"), "w", encoding="utf-8") as f:
        for g in ev:
            f.write(json.dumps(g) + "\n")

    def dist(rows, key):
        return dict(Counter(key(g) for g in rows).most_common())
    print(f"sources: {len(agg_srcs)} agg intent graphs (of {len(srcs)} intent)")
    print(f"train {len(train)} | calibration {len(calibration)} | "
          f"eval {len(ev)} (incl {len(PROBES)} probes)")
    print(f"train class: {dist(train, lambda g: str(g['expect']))}")
    print(f"cal   class: {dist(calibration, lambda g: str(g['expect']))}")
    print(f"eval  class: {dist([g for g in ev if not g.get('probe')], lambda g: str(g['expect']))}")
    # place ⊥ class audit: place -> class counts across "in <place>" train variants
    pc_by_class = {}
    for g in train:
        toks = [u["text"] for u in g["units"] if u.get("group") == "q"]
        if "in" in toks:
            pl = toks[toks.index("in") + 1] if toks.index("in") + 1 < len(toks) else "?"
            pc_by_class.setdefault(pl, Counter())[str(g["expect"])] += 1
    print("place-indep-class (train 'in <place>' variants):")
    for pl in PLACES:
        print(f"    {pl:11s} {dict(pc_by_class.get(pl, {}))}")


if __name__ == "__main__":
    main()
