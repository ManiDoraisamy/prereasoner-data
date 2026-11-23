"""Probe candidate URLs: status, content type, size. Usage: python -I probe.py urls.txt"""
import sys
sys.path.append("C:/Users/manid/AppData/Roaming/Python/Python314/site-packages")
import requests  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0 Safari/537.36",
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}

for line in open(sys.argv[1], encoding="utf-8"):
    url = line.strip()
    if not url or url.startswith("#"):
        continue
    try:
        r = requests.get(url, headers=UA, stream=True, timeout=30, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        cl = r.headers.get("Content-Length", "?")
        head = next(r.iter_content(8), b"")
        r.close()
        print(f"{r.status_code}\t{cl}\t{ct[:40]}\t{head[:4]!r}\t{url}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERR\t{type(exc).__name__}\t\t\t{url}")
