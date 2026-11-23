"""Catalog lookups. Usage:
  python -I catalogs.py count <domain> <id> [<domain> <id> ...]   # Socrata row counts + license
  python -I catalogs.py cms_xlsx                                  # data.cms.gov data.json xlsx/zip distributions
  python -I catalogs.py ckan <base> <query>                       # CKAN package_search with XLSX resources
  python -I catalogs.py medicaid                                  # data.medicaid.gov datasets (title, csv url)
"""
import sys
import requests  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"}
cmd = sys.argv[1]
if cmd == "count":
    a = sys.argv[2:]
    for dom, did in zip(a[::2], a[1::2]):
        try:
            n = requests.get(f"https://{dom}/resource/{did}.json", params={"$select": "count(*) as n"},
                             headers=UA, timeout=60).json()[0]["n"]
            v = requests.get(f"https://{dom}/api/views/{did}.json", headers=UA, timeout=60).json()
            lic = (v.get("license") or {}).get("name") or v.get("licenseId") or "-"
            print(f"{dom}\t{did}\t{n} rows\t{len(v.get('columns', []))} cols\t{lic}\t{v.get('name','')[:60]}")
        except Exception as exc:  # noqa: BLE001
            print(f"{dom}\t{did}\tERR {type(exc).__name__}")
elif cmd == "cms_xlsx":
    cat = requests.get("https://data.cms.gov/data.json", headers=UA, timeout=120).json()
    for ds in cat.get("dataset", []):
        for d in ds.get("distribution", []):
            url = d.get("downloadURL") or d.get("accessURL") or ""
            if url.lower().split("?")[0].endswith((".xlsx", ".zip")):
                print(f"{ds.get('title','')[:60]}\t{url}")
                break
elif cmd == "ckan":
    base, q = sys.argv[2], sys.argv[3]
    r = requests.get(f"{base}/api/3/action/package_search", params={"q": q, "fq": "res_format:XLSX", "rows": 40},
                     headers=UA, timeout=60).json()
    for p in r["result"]["results"]:
        xs = [x for x in p.get("resources", []) if (x.get("format") or "").upper() == "XLSX"]
        for x in xs[:2]:
            print(f"{(p.get('license_title') or '-')[:25]}\t{((p.get('organization') or {}).get('name') or '')[:12]}\t{p['title'][:55]}\t{x.get('size') or '?'}\t{x['url']}")
elif cmd == "medicaid":
    items = requests.get("https://data.medicaid.gov/api/1/metastore/schemas/dataset/items", headers=UA,
                         timeout=120).json()
    for ds in items:
        dist = ds.get("distribution", [])
        url = dist[0].get("downloadURL", "") if dist else ""
        print(f"{ds.get('identifier','')[:36]}\t{(ds.get('license') or '-')[:40]}\t{ds.get('title','')[:70]}\t{url}")
