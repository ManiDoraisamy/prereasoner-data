"""Download every source into raw/<id>/, place the workbook at workbooks/<id>.<ext>, record sha256.
Usage (from the dataset root): python -I scripts/download.py"""
import hashlib
import json
import os
import shutil
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.append("C:/Users/manid/AppData/Roaming/Python/Python314/site-packages")
sys.path.insert(0, HERE)
import requests  # noqa: E402
from sources import records  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36", "Accept": "*/*"}
MAX_BYTES = 50 * 1024 * 1024


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest):
    with requests.get(url, headers=UA, stream=True, timeout=120) as r:
        r.raise_for_status()
        n = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                n += len(chunk)
                if n > MAX_BYTES:
                    raise RuntimeError("over 50 MB")
                f.write(chunk)
    return n


out_path = os.path.join(ROOT, "work", "downloads.json")
done = json.load(open(out_path)) if os.path.exists(out_path) else {}
zip_cache = {}
for rec in records():
    sid = rec["id"]
    if sid in done and os.path.exists(os.path.join(ROOT, "workbooks", done[sid]["file"])):
        continue
    raw = os.path.join(ROOT, "raw", sid)
    os.makedirs(raw, exist_ok=True)
    try:
        if rec["kind"] == "zip":
            zpath = zip_cache.get(rec["url"])
            if zpath is None:
                zpath = os.path.join(raw, "source.zip")
                fetch(rec["url"], zpath)
                zip_cache[rec["url"]] = zpath
            with zipfile.ZipFile(zpath) as z:
                info = z.getinfo(rec["member"])
                if info.file_size > MAX_BYTES:
                    raise RuntimeError("member over 50 MB")
                src = os.path.join(raw, "member" + os.path.splitext(rec["member"])[1])
                with z.open(info) as fin, open(src, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
            ext = os.path.splitext(rec["member"])[1].lower()
        else:
            ext = ".csv" if (rec["kind"] == "socrata" or rec["url"].split("?")[0].lower().endswith(".csv")) else ".xlsx"
            src = os.path.join(raw, "download" + ext)
            fetch(rec["url"], src)
        with open(src, "rb") as f:
            head = f.read(4)
        if ext == ".xlsx" and head != b"PK\x03\x04":
            raise RuntimeError("not an xlsx file")
        fname = sid + ext
        dest = os.path.join(ROOT, "workbooks", fname)
        shutil.copyfile(src, dest)
        done[sid] = {"file": fname, "bytes": os.path.getsize(dest), "sha256": sha256(dest),
                     "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        print(f"ok\t{sid}\t{done[sid]['bytes']}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL\t{sid}\t{type(exc).__name__}: {str(exc)[:80]}")
    json.dump(done, open(out_path, "w", newline="\n"), indent=1)
print(len(done), "workbooks")
