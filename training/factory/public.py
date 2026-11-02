"""Other industries: report-shaped sheets from public sources with clear terms, each fetched into its own new folder, at most 2 GB in all. One source, the UK government's OGL files, is held out whole as a frozen other-industry test, chosen before any was read.

usage: python -I training/factory/isolated.py training.factory.public fetch eia|sec|ogl [--count N]
       python -I training/factory/isolated.py training.factory.public prepare eia|sec|ogl [--tabs N]

Fetching reads nothing but bytes and JSON listings; the sheets are read only by prepare, under python -I from outside the folder. Every file passes the factory's screen.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile

from training.factory.paths import MIN_FREE_BYTES, PUBLIC, SEED

UA = "PreReasoner data research https://prereasoner.com"
MAX_BYTES_ALL = 2 * 1024 ** 3
MAX_FILE = 25 * 1024 ** 2
SOURCES = {
    "eia": {"license": "US government publication, public domain; acknowledge EIA with the date", "terms": "https://www.eia.gov/about/copyrights_reuse.php", "url": "https://www.eia.gov/electricity/monthly/", "held_out": False},
    "sec": {"license": "public information on sec.gov, may be copied and redistributed; cite the SEC. Automated access within 10 requests a second with a declared user agent", "terms": "https://www.sec.gov/os/accessing-edgar-data", "url": "https://www.sec.gov/Archives/edgar/data/", "held_out": False},
    "ogl": {"license": "Open Government Licence v3.0, commercial use and adaptation allowed, per dataset on data.gov.uk", "terms": "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/", "url": "https://ckan.publishing.service.gov.uk/api/3/action/package_search", "held_out": True},
}


def _folder(source: str) -> str:
    path = f"{PUBLIC}{source}_raw/"
    os.makedirs(path, exist_ok=True)
    return path


def _get(url: str, *, limit: int = MAX_FILE, pause: float = 0.0, timeout: int = 90) -> bytes | None:
    time.sleep(pause)
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(limit + 1)
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError):
        return None
    return None if len(body) > limit else body


def _size_of(folder: str) -> int:
    return sum(os.path.getsize(os.path.join(folder, name)) for name in os.listdir(folder))


def _all_public_bytes() -> int:
    return sum(_size_of(f"{PUBLIC}{s}_raw/") for s in SOURCES if os.path.isdir(f"{PUBLIC}{s}_raw/"))


def _save(folder: str, name: str, body: bytes, log: list) -> bool:
    """Save only a real xlsx: a zip holding the workbook part."""
    if not body or not body.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            if "xl/workbook.xml" not in archive.namelist():
                return False
    except zipfile.BadZipFile:
        return False
    if _all_public_bytes() + len(body) > MAX_BYTES_ALL or shutil.disk_usage(folder).free < MIN_FREE_BYTES:
        raise SystemExit("over the 2 GB cap or under the disk floor")
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
    with open(folder + clean, "wb") as sink:
        sink.write(body)
    log.append(clean)
    return True


def fetch_eia(count: int) -> list:
    folder, log = _folder("eia"), []
    urls = []
    for month in ("october2026", "september2026", "august2026"):
        body = _get(f"https://www.eia.gov/electricity/monthly/current_month/{month}.zip", limit=200 * 1024 ** 2)
        if body:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                for name in sorted(archive.namelist()):
                    if name.lower().endswith(".xlsx") and len(log) < count:
                        _save(folder, "epm_" + os.path.basename(name), archive.read(name), log)
            break
    for chapter in range(1, 12):
        for table in range(1, 30):
            urls.append(f"https://www.eia.gov/electricity/annual/xls/epa_{chapter:02d}_{table:02d}.xlsx")
    for table in ("T01.01", "T01.02", "T01.03", "T01.07", "T02.01", "T02.02", "T03.01", "T03.03", "T04.01", "T04.03", "T05.01", "T06.01", "T06.02", "T07.01", "T07.02", "T09.01", "T09.10", "T10.01", "T11.01"):
        urls.append(f"https://www.eia.gov/totalenergy/data/browser/xls.php?tbl={table}")
    for name in ("generation_monthly", "emission_annual", "capability_monthly", "consumption_monthly"):
        urls.append(f"https://www.eia.gov/electricity/data/state/{name}.xlsx")
    for at, url in enumerate(urls):
        if len(log) >= count:
            break
        body = _get(url, pause=0.3)
        tail = re.sub(r"\W+", "_", url.rsplit("/", 1)[-1])
        if body:
            _save(folder, f"{tail}.xlsx" if not tail.endswith("xlsx") else tail[:-5] + ".xlsx", body, log)
    return log


def fetch_sec(count: int) -> list:
    """Financial_Report.xlsx of a seeded draw of the 10-Ks filed in 2025 Q1, from EDGAR's form index: one request a filing, under 5 a second."""
    folder, log = _folder("sec"), []
    index = PUBLIC + "form_2025q1.idx"
    if not os.path.exists(index):
        body = _get("https://www.sec.gov/Archives/edgar/full-index/2025/QTR1/form.idx", limit=80 * 1024 ** 2, timeout=300)
        if body is None:
            return log
        with open(index, "wb") as sink:
            sink.write(body)
    with open(index, encoding="latin-1") as source:
        filings = sorted(set(re.findall(r"edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt", "".join(line for line in source if line.startswith("10-K ")))))
    random.Random(SEED).shuffle(filings)
    have = {name.split("_", 1)[0] for name in os.listdir(folder)}
    for cik, accession in filings:
        if len(log) >= count:
            break
        if f"cik{cik}" in have:
            continue
        report = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/Financial_Report.xlsx", pause=0.25)
        if report:
            _save(folder, f"cik{cik}_{accession}.xlsx", report, log)
    return log


def fetch_ogl(count: int) -> list:
    """XLSX resources of datasets marked uk-ogl on data.gov.uk, a seeded draw, one file a dataset. ONS and other departments' statistics, per dataset's own licence field."""
    folder, log = _folder("ogl"), []
    rows = []
    for start in range(0, 3000, 500):
        body = _get(f"https://ckan.publishing.service.gov.uk/api/3/action/package_search?q=res_format:XLSX&fq=license_id:uk-ogl&rows=500&start={start}", limit=120 * 1024 ** 2, timeout=120)
        if body is None:
            break
        rows.extend(json.loads(body)["result"]["results"])
    random.Random(SEED).shuffle(rows)
    index = {}
    for dataset in rows:
        if len(log) >= count:
            break
        if dataset.get("license_id") != "uk-ogl":
            continue
        for resource in dataset.get("resources", ()):
            url = resource.get("url") or ""
            if str(resource.get("format", "")).upper() == "XLSX" and url.startswith("https://"):
                body = _get(url, pause=0.3, timeout=60)
                if body and _save(folder, f"{dataset['name'][:70]}_{hashlib.blake2b(url.encode(), digest_size=4).hexdigest()}.xlsx", body, log):
                    index[log[-1]] = {"dataset": dataset["name"], "publisher": (dataset.get("organization") or {}).get("name"), "url": url, "license": dataset.get("license_id")}
                break
    with open(PUBLIC + "ogl_index.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(index, sink, indent=0)
    return log


def family_of(source: str, name: str) -> str:
    """A public workbook's family: a company, or a table stem; months of one table are one family."""
    if source == "sec":
        return "sec:" + name.split("_", 1)[0]
    return f"{source}:" + re.sub(r"\d+", "#", name.rsplit(".", 1)[0])


def prepare(source: str, tabs: int, workers: int) -> dict:
    from training.factory.prepare import run
    folder = _folder(source)
    names = sorted(os.listdir(folder))
    random.Random(SEED).shuffle(names)
    picks = [{"file": name, "path": folder + name, "family": family_of(source, name), "rank": 0, "tabs": None, "max_tabs": tabs, "screen_by_tab": source == "sec"} for name in names]
    return run(picks, f"pub_{source}", workers)


def main() -> None:
    from training.factory.paths import pin, write_manifest
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("fetch", "prepare"))
    parser.add_argument("source", choices=tuple(SOURCES))
    parser.add_argument("--count", type=int, default=150)
    parser.add_argument("--tabs", type=int, default=4)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.command == "fetch":
        log = {"eia": fetch_eia, "sec": fetch_sec, "ogl": fetch_ogl}[args.source](args.count)
        record = dict(SOURCES[args.source], files=len(log), bytes=_size_of(_folder(args.source)), fetched="2026-11-02")
        write_manifest({f"public_{args.source}": record})
        print(args.source, "files", len(log), "bytes", record["bytes"], flush=True)
    else:
        pin()
        print(json.dumps(prepare(args.source, args.tabs, args.workers)), flush=True)


if __name__ == "__main__":
    main()
