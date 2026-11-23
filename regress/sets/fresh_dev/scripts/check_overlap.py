"""Check this set's sources against the frozen set's manifest (its url, sha256, landing_page and file fields only).
Usage: python -I scripts/check_overlap.py   (prints counts; writes work/overlap_check.json)"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from sources import records  # noqa: E402

FROZEN_MANIFEST = os.path.join(os.path.dirname(ROOT), "fresh_frozen", "manifest.json")


def view_id(url):
    m = re.search(r"/api/views/([a-z0-9]{4}-[a-z0-9]{4})/", url)
    return m.group(1) if m else None


def base_name(url):
    return re.sub(r"\d", "#", url.split("?")[0].rsplit("/", 1)[1].lower())


frozen = [{k: s.get(k) for k in ("url", "sha256", "landing_page", "file", "zip_member")}
          for s in json.load(open(FROZEN_MANIFEST, encoding="utf-8"))["sources"]]
downloads = json.load(open(os.path.join(ROOT, "work", "downloads.json"), encoding="utf-8"))
f_url = {(s["url"], s["zip_member"]) for s in frozen}
f_sha = {s["sha256"] for s in frozen}
f_view = {view_id(s["url"]) for s in frozen} - {None}
f_page = {s["landing_page"] for s in frozen}
f_name = {base_name(s["url"]) for s in frozen if not view_id(s["url"])} | \
         {re.sub(r"\d", "#", (s["zip_member"] or "").lower()) for s in frozen if s["zip_member"]}
out = {"same_url": [], "same_sha256": [], "same_socrata_view": [], "same_landing_page": [], "same_name_pattern": []}
for rec in records():
    d = downloads[rec["id"]]
    if (rec["url"], rec["member"]) in f_url:
        out["same_url"].append(rec["id"])
    if d["sha256"] in f_sha:
        out["same_sha256"].append(rec["id"])
    if view_id(rec["url"]) in f_view:
        out["same_socrata_view"].append(rec["id"])
    if rec["landing_page"] in f_page:
        out["same_landing_page"].append(rec["id"])
    name = re.sub(r"\d", "#", rec["member"].lower()) if rec["member"] else base_name(rec["url"])
    if not view_id(rec["url"]) and name in f_name:
        out["same_name_pattern"].append(rec["id"])
with open(os.path.join(ROOT, "work", "overlap_check.json"), "w", encoding="utf-8", newline="\n") as f:
    json.dump({"frozen_sources": len(frozen), "dev_sources": len(downloads), **out}, f, indent=1)
print({k: v for k, v in out.items()})
