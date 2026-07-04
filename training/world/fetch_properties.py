#!/usr/bin/env python3
"""
gen20/scripts/fetch_properties.py — build training/data/properties.csv: EVERY Wikidata property (P-number)
with name, description, datatype, and source.

Primary path = one WDQS SPARQL request (cleanest — the label service resolves the source database name for free).
Fallback = MediaWiki action API (enumerate the Property namespace + wbgetentities in batches) when WDQS is
rate-limiting or down. Same columns either way.

Columns:
  pid           - the P-number (e.g. P31)
  name          - English label (e.g. "instance of")
  description   - English description
  datatype      - value type: ExternalId | WikibaseItem | Quantity | Time | String | Url | ...
  source        - for external-id properties, the database the id belongs to (P1629 subject item label,
                  else the formatter-URL domain); blank for item/literal properties
  formatter_url - P1630 URL template that resolves an external id (e.g. https://musicbrainz.org/artist/$1)
  subject_qid   - P1629, the item the property is the identifier for (e.g. Q14005 = MusicBrainz)
"""
import os
import csv
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import Counter

UA = {"User-Agent": "prereasoner-properties/1.0 (mani.doraisamy@gmail.com)"}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "properties.csv")
COLS = ["pid", "name", "description", "datatype", "source", "formatter_url", "subject_qid"]

_DT = {"external-id": "ExternalId", "wikibase-item": "WikibaseItem", "quantity": "Quantity", "time": "Time",
       "string": "String", "url": "Url", "monolingualtext": "Monolingualtext", "commonsMedia": "CommonsMedia",
       "globe-coordinate": "GlobeCoordinate", "math": "Math", "wikibase-property": "WikibaseProperty",
       "wikibase-lexeme": "WikibaseLexeme", "wikibase-sense": "WikibaseSense", "wikibase-form": "WikibaseForm",
       "tabular-data": "TabularData", "musical-notation": "MusicalNotation", "geo-shape": "GeoShape",
       "entity-schema": "WikibaseEntitySchema"}


def _get(url, headers=UA, timeout=90):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def _domain(u):
    if not u:
        return ""
    try:
        p = urllib.parse.urlparse(u)
        net = p.netloc.lower()
        if net.startswith("www."):
            net = net[4:]
        if net.endswith("toolforge.org") or net.endswith("wmflabs.org"):
            # Wikidata's external-id redirect proxy — the real target site sits in a query param
            qs = urllib.parse.parse_qs(p.query)
            for k in ("url_prefix", "url", "P1"):
                if k in qs and "//" in qs[k][0]:
                    inner = urllib.parse.urlparse(qs[k][0]).netloc.lower()
                    if inner:
                        return inner[4:] if inner.startswith("www.") else inner
        return net
    except Exception:
        return ""


def via_sparql():
    q = """SELECT ?p ?pLabel ?pDescription ?type ?formatter ?source ?sourceLabel WHERE {
  ?p a wikibase:Property ; wikibase:propertyType ?type .
  OPTIONAL { ?p wdt:P1630 ?formatter. }
  OPTIONAL { ?p wdt:P1629 ?source. }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
    h = {**UA, "Accept": "application/sparql-results+json"}
    data = None
    for attempt in range(2):
        try:
            data = json.load(_get(url, headers=h, timeout=180))["results"]["bindings"]
            break
        except urllib.error.HTTPError as he:
            if he.code == 429 and attempt == 0:
                print("  WDQS 429 (1 req/min); waiting 65s then retrying once...", flush=True)
                time.sleep(65)
                continue
            raise
    rows = {}
    for b in data:
        pid = b["p"]["value"].rsplit("/", 1)[-1]
        r = rows.get(pid)
        if r is None:
            r = rows[pid] = {"pid": pid, "name": b.get("pLabel", {}).get("value", ""),
                             "description": b.get("pDescription", {}).get("value", ""),
                             "datatype": b["type"]["value"].split("#")[-1],
                             "source": "", "formatter_url": "", "subject_qid": ""}
        if not r["formatter_url"] and b.get("formatter"):
            r["formatter_url"] = b["formatter"]["value"]
        if not r["subject_qid"] and b.get("source"):
            r["subject_qid"] = b["source"]["value"].rsplit("/", 1)[-1]
    # source = the external database's website (from the formatter URL); only external-ids have a source
    for r in rows.values():
        if r["datatype"] == "ExternalId":
            r["source"] = _domain(r["formatter_url"])
    return list(rows.values())


def via_api():
    base = "https://www.wikidata.org/w/api.php"
    pids, cont = [], None
    while True:
        u = base + "?action=query&list=allpages&apnamespace=120&aplimit=500&format=json"
        if cont:
            u += "&apcontinue=" + urllib.parse.quote(cont)
        d = json.load(_get(u))
        for p in d["query"]["allpages"]:
            t = p["title"].split(":", 1)[-1]
            if t[:1] == "P" and t[1:].isdigit():
                pids.append(t)
        if "continue" in d:
            cont = d["continue"]["apcontinue"]
            time.sleep(0.1)
        else:
            break
    print("  enumerated %d properties" % len(pids), flush=True)
    rows, subj = [], {}
    for i in range(0, len(pids), 50):
        batch = pids[i:i + 50]
        u = base + "?action=wbgetentities&ids=" + "|".join(batch) + \
            "&props=labels|descriptions|datatype|claims&languages=en&format=json"
        d = None
        for attempt in range(5):
            try:
                d = json.load(_get(u, timeout=90))
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2 * (attempt + 1))
        for pid, e in d.get("entities", {}).items():
            if "missing" in e:
                continue
            claims = e.get("claims", {})

            def first(p):
                for c in claims.get(p, []):
                    dv = (c.get("mainsnak") or {}).get("datavalue")
                    if dv:
                        return dv["value"]
                return None
            fmt = first("P1630")
            sj = first("P1629")
            sj = sj["id"] if isinstance(sj, dict) and "id" in sj else ""
            rows.append({"pid": pid, "name": (e.get("labels", {}).get("en", {}) or {}).get("value", ""),
                         "description": (e.get("descriptions", {}).get("en", {}) or {}).get("value", ""),
                         "datatype": _DT.get(e.get("datatype", ""), e.get("datatype", "")),
                         "source": "", "formatter_url": fmt if isinstance(fmt, str) else "", "subject_qid": sj})
            if sj:
                subj[sj] = None
        if (i // 50) % 20 == 0:
            print("  fetched %d/%d" % (i + len(batch), len(pids)), flush=True)
        time.sleep(0.1)
    # source = the external database's website (from the formatter URL); only external-ids have a source
    for r in rows:
        if r["datatype"] == "ExternalId":
            r["source"] = _domain(r["formatter_url"])
    return rows


def main():
    try:
        print("trying WDQS SPARQL (one request)...", flush=True)
        rows = via_sparql()
        print("  SPARQL ok: %d properties" % len(rows), flush=True)
    except Exception as e:
        print("  SPARQL failed (%s: %s); falling back to action API" % (type(e).__name__, e), flush=True)
        rows = via_api()
    rows.sort(key=lambda r: int(r["pid"][1:]))
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print("wrote %s (%d rows)" % (OUT, len(rows)), flush=True)
    print("by datatype:", dict(Counter(r["datatype"] for r in rows).most_common()))
    print("with a source value:", sum(1 for r in rows if r["source"]))


if __name__ == "__main__":
    main()
