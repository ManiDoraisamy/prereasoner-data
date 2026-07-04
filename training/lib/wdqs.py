"""
training.lib.wdqs — minimal Wikidata Query Service (WDQS) SPARQL client, vendored from the original world-DB
importer. Only the robust HTTP client + tiny binding helpers live here; the bulk geo-table fetchers stayed with
the serving-side world importer (they provision the live world DB, not the trained model).

Set WIKIMEDIA_CONTACT to your contact address (Wikimedia API etiquette asks for one in the User-Agent).
"""
from __future__ import annotations
import json
import os
import time
import urllib.parse
import urllib.request

UA = f"prereasoner-training/1.0 ({os.environ.get('WIKIMEDIA_CONTACT', 'https://github.com/prereasoner')})"
ENDPOINT = "https://query.wikidata.org/sparql"


def wdqs(query, timeout=58, retries=4):
    """POST a SPARQL query, return list of binding dicts. Retry (with backoff) on 429/5xx/network."""
    data = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(ENDPOINT, data=data, headers={
                "User-Agent": UA, "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)["results"]["bindings"]
        except Exception as e:                       # noqa: BLE001
            last = e
            if attempt == retries - 1:
                break
            wait = min(45, 5 * (attempt + 1) ** 2)
            print(f"    WDQS retry {attempt+1}/{retries} ({getattr(e, 'code', None) or type(e).__name__}) "
                  f"wait {wait}s", flush=True)
            time.sleep(wait)
    raise last


def V(b, k):
    return b[k]["value"] if k in b else None


def qid_of(uri):
    return uri.rsplit("/", 1)[-1] if uri else None


def parse_point(wkt):
    """'Point(lng lat)' -> (lat, lng)."""
    try:
        lng, lat = wkt[wkt.index("(") + 1:wkt.index(")")].split()
        return float(lat), float(lng)
    except Exception:                                # noqa: BLE001
        return None, None
