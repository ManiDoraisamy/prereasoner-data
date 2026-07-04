"""
gen20 — DATA-DRIVEN world-type discovery. Scan the downloaded CSV corpus (training/data/csv_corpus), and for each categorical
(string) column, resolve a SAMPLE of its cell VALUES to Wikidata and type the column by the DOMINANT P31 ("instance
of") of its values — NOT by the header (header-ACE is unreliable; values are ground truth). Aggregate across the
corpus to rank the entity TYPES actually present, excluding the ones already in the world model (city / country /
state / element / continent). The output (training/data/discovered_types.json) is the work-list for materialization:
each ranked type becomes a candidate world."<Type>" table fetched from Wikidata.

  $env:PYTHONUTF8=1; python -m training.corpus.discover_csv_types
"""
from __future__ import annotations
import os
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = Path(os.environ.get("CSV_CORPUS_DIR", str(ROOT / "training/data/csv_corpus")))                                                    # the CSV corpus (input)
SCAN = DATA / "corpus_scan.jsonl"
RAW = DATA / "raw_csv"
CACHE_F = ROOT / "training/data/wd_cache.json"                              # expansion outputs live in training/data
OUT = ROOT / "training/data/discovered_types.json"
UA = "PrereasonerWorldExpand/1.0 (research; mani.doraisamy@gmail.com)"

# headers that are clearly NOT world entities (metadata / measures / enums / ids / free text) — they prioritize
# which columns to spend API calls on; the TYPE decision is still made from the resolved values, not the header.
STOP = set("id ids uuid hash token index idx key value code codes type types status state_flag note notes time "
           "date datetime timestamp source dataset model unit units rank year month day week hour minute second "
           "description desc title url link uri text comment comments category categories label labels version "
           "action actions scenario context target pos position feature features algorithm obs age sex gender "
           "color colour group class level score count total sum avg min max mean std flag active enabled message "
           "error path file filename email phone fax zip postal lat lng lon latitude longitude amount price cost "
           "qty quantity number num percent pct ratio rate width height size weight length duration".split())
# P31 labels for types ALREADY in the world model (geography + element/currency) — excluded from the "new" ranking
KNOWN_RE = re.compile(r"\b(cit(y|ies)|town|municipalit|commune|borough|hamlet|village|sovereign state|countr(y|ies)|"
                      r"u\.s\. state|state of|province|region|district|count(y|ies)|prefecture|continent|element|"
                      r"currenc|capital|settlement|megacity|big city|administrative|territory|oblast|canton)\b", re.I)
# header-level geo skip — don't spend API calls re-typing columns we already cover (the world already has these)
GEO_HDR = re.compile(r"countr|\bcit(y|ies)\b|\bstate\b|province|region|\bcount(y|ies)\b|continent|capital|municipal|"
                     r"\btown\b|district|location|\bplace\b|admin|\biso\d?\b|\bfips\b|postal|zip|address|latitude|longitude", re.I)
# entity-likely headers worth probing even if they're in the frequency long tail (typing still comes from VALUES)
CURATED = set("company companies organization organisation institution university college school hospital airport "
              "airline drug medication medicine compound protein gene disease syndrome language currency team club "
              "league manufacturer brand artist album song track movie film show book novel publisher journal "
              "occupation profession job sport breed plant animal stadium venue award party religion nationality "
              "ethnicity make model vehicle aircraft ship weapon material mineral food dish festival holiday "
              "operating system framework software platform license network protocol".split())


def _cache():
    if CACHE_F.exists():
        try:
            return json.load(open(CACHE_F, encoding="utf-8"))
        except Exception:                                       # noqa: BLE001
            return {}
    return {}


class WD:
    """Tiny polite Wikidata client: label -> top QID -> P31 (instance of) + the P31's English label. Cached on disk."""

    def __init__(self, cache):
        self.cache = cache; self.calls = 0; self.tlabel = {}

    def _api(self, params, tries=4):
        url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(params)
        for a in range(tries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                self.calls += 1
                return json.load(urllib.request.urlopen(req, timeout=25))
            except Exception as e:                              # noqa: BLE001
                if a == tries - 1:
                    print("  api fail:", e, flush=True); return {}
                time.sleep(1.5 * (a + 1))
        return {}

    def type_of(self, value):
        """-> (p31_qid, p31_label) | (None, None). Cached."""
        v = value.strip()
        if v in self.cache:
            c = self.cache[v]
            return (c[0], c[1]) if c else (None, None)
        r = self._api({"action": "wbsearchentities", "search": v, "language": "en",
                       "format": "json", "limit": 1, "type": "item"})
        hits = r.get("search") or []
        if not hits:
            self.cache[v] = None; return (None, None)
        e = self._api({"action": "wbgetentities", "ids": hits[0]["id"], "props": "claims", "format": "json"})
        cl = (e.get("entities", {}).get(hits[0]["id"], {}) or {}).get("claims", {})
        p31 = next((c["mainsnak"]["datavalue"]["value"]["id"] for c in cl.get("P31", [])
                    if c.get("mainsnak", {}).get("datavalue")), None)
        lab = self._label(p31) if p31 else None
        self.cache[v] = [p31, lab] if p31 else None
        return (p31, lab)

    def _label(self, qid):
        if qid in self.tlabel:
            return self.tlabel[qid]
        e = self._api({"action": "wbgetentities", "ids": qid, "props": "labels", "format": "json"})
        lab = (e.get("entities", {}).get(qid, {}) or {}).get("labels", {}).get("en", {}).get("value") or qid
        self.tlabel[qid] = lab; return lab


def name_like(v):
    s = str(v).strip()
    return bool(s) and len(s) <= 40 and re.search(r"[A-Za-z]", s) and not re.fullmatch(r"[\d.,:/_\- ]+", s) \
        and len(s.split()) <= 5


def col_values(hexsha, col, want=12, maxrows=120):
    """distinct name-like values of `col` read straight from raw_csv/<hexsha>.csv (all 100k available, unlike the
    typed_json subset). Tries common delimiters and picks the one whose header actually contains `col`."""
    p = RAW / f"{hexsha}.csv"
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:                                          # noqa: BLE001
        return []
    if not lines:
        return []
    for delim in (",", ";", "\t", "|"):
        try:
            header = next(csv.reader([lines[0]], delimiter=delim))
        except Exception:                                      # noqa: BLE001
            continue
        if col not in header:
            continue
        ci = header.index(col)
        seen = []
        for line in lines[1:maxrows + 1]:
            try:
                row = next(csv.reader([line], delimiter=delim))
            except Exception:                                  # noqa: BLE001
                continue
            if ci < len(row):
                v = row[ci].strip()
                if v and name_like(v) and v not in seen:
                    seen.append(v)
                    if len(seen) >= want:
                        break
        return seen
    return []


def main():
    n_headers = int(sys.argv[1]) if len(sys.argv) > 1 else 45      # candidate string headers to probe
    n_csv = int(sys.argv[2]) if len(sys.argv) > 2 else 5           # CSVs sampled per header
    wd = WD(_cache())

    # 1. headers of STRING columns across the FULL corpus, ranked, with the hexshas that have them (values read live)
    hdr_files = defaultdict(list); hdr_n = Counter()
    with open(SCAN, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:                                  # noqa: BLE001
                continue
            hx = d.get("hexsha"); ct = d.get("col_types") or {}
            for c, t in ct.items():
                if t != "string":
                    continue
                key = re.sub(r"[^a-z0-9]+", " ", str(c).lower()).strip()
                if key and key not in STOP and re.search(r"[a-z]", key):
                    hdr_n[key] += 1
                    if len(hdr_files[(key, c)]) < n_csv * 4:
                        hdr_files[(key, c)].append(hx)
    top = [h for h, _ in hdr_n.most_common() if h not in STOP and not GEO_HDR.search(h)][:n_headers]
    cur = [h for h in CURATED if h in hdr_n and h not in top and not GEO_HDR.search(h)]
    cand = top + cur
    print(f"corpus: {sum(hdr_n.values())} string-cols; probing {len(cand)} headers ({len(top)} top + {len(cur)} "
          f"curated) x {n_csv} CSVs", flush=True)

    # 2. per candidate header: sample CSVs, resolve values, dominant P31 -> the column's type
    types = defaultdict(lambda: {"qid": None, "columns": 0, "headers": Counter(), "examples": []})
    for hi, hkey in enumerate(cand):
        col_keys = [(k, orig) for (k, orig) in hdr_files if k == hkey][:1]
        done = 0
        for (k, orig) in [ck for ck in hdr_files if ck[0] == hkey]:
            for hx in hdr_files[(k, orig)]:
                if done >= n_csv:
                    break
                vals = col_values(hx, orig)
                if len(vals) < 4:
                    continue
                done += 1
                votes = Counter()
                for v in vals[:10]:
                    p31, lab = wd.type_of(v)
                    if p31:
                        votes[(p31, lab)] += 1
                if not votes:
                    continue
                (p31, lab), c = votes.most_common(1)[0]
                if c >= max(3, 0.5 * sum(votes.values())) and not KNOWN_RE.search(lab or ""):   # consistent + NEW
                    t = types[lab]; t["qid"] = p31; t["columns"] += 1; t["headers"][hkey] += 1
                    if len(t["examples"]) < 8:
                        t["examples"] += [v for v in vals[:3] if v not in t["examples"]]
        json.dump(wd.cache, open(CACHE_F, "w", encoding="utf-8"))    # checkpoint cache
        if hi % 5 == 0:
            print(f"  [{hi+1}/{len(cand)}] {hkey!r}  (api calls so far: {wd.calls})", flush=True)

    ranked = sorted(types.items(), key=lambda kv: -kv[1]["columns"])
    print(f"\n== DISCOVERED world-entity types (new, value-typed; {wd.calls} api calls) ==")
    out = []
    for lab, t in ranked:
        if t["columns"] < 2:
            continue
        hdrs = ", ".join(h for h, _ in t["headers"].most_common(4))
        print(f"  {t['columns']:3d} cols  {lab:32s} ({t['qid']:>10s})  headers[{hdrs}]  e.g. {t['examples'][:4]}")
        out.append({"type": lab, "p31": t["qid"], "columns": t["columns"],
                    "headers": dict(t["headers"]), "examples": t["examples"]})
    json.dump(out, open(OUT, "w", encoding="utf-8"), indent=1)
    print(f"\nwrote {OUT} ({len(out)} types)")


if __name__ == "__main__":
    main()
