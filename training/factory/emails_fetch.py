"""Fetch the Enron emails archive's parts (figshare 1221767, CC BY 4.0) into their own folder. Bytes only; nothing is opened.

usage: python -I training/factory/emails_fetch.py     Skips a part already whole. Keeps 50 GB of disk free.
"""
import http.client
import json
import os
import shutil
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from training.factory.paths import EMAILS

FOLDER = EMAILS + "parts/"
FLOOR = 50 * 1024 ** 3


def main():
    os.makedirs(FOLDER, exist_ok=True)
    with urllib.request.urlopen("https://api.figshare.com/v2/articles/1221767", timeout=60) as response:
        record = json.load(response)
    parts = [f for f in record["files"] if f["name"].startswith("eml.7z")]
    print("license", record["license"]["name"], "parts", len(parts), "bytes", sum(f["size"] for f in parts), flush=True)
    for part in sorted(parts, key=lambda f: f["name"]):
        path = FOLDER + part["name"]
        if os.path.exists(path) and os.path.getsize(path) == part["size"]:
            continue
        if shutil.disk_usage(FOLDER).free - part["size"] < FLOOR:
            raise SystemExit("under the disk floor")
        for attempt in range(60):
            have = os.path.getsize(path + ".tmp") if os.path.exists(path + ".tmp") else 0
            if have >= part["size"]:
                break
            request = urllib.request.Request(part["download_url"], headers={"Range": f"bytes={have}-"})
            try:
                with urllib.request.urlopen(request, timeout=120) as response, open(path + ".tmp", "ab") as sink:
                    if have and response.status != 206:
                        raise OSError("no resume")
                    shutil.copyfileobj(response, sink, 1024 * 1024)
            except (OSError, http.client.HTTPException):
                time.sleep(10)
        if os.path.getsize(path + ".tmp") != part["size"]:
            raise SystemExit(f"short read: {part['name']}")
        os.replace(path + ".tmp", path)
        print("fetched", part["name"], flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
