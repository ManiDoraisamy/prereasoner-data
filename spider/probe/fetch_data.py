"""Fetch the Spider dev split + schemas + the 20 dev SQLite databases into ../data (gitignored).

dev.json / tables.json come from the official taoyds/spider eval examples (they carry the pre-parsed
`sql` dict used by the official eval_hardness). The per-DB SQLite files come from the premai-io/spider
HF mirror (the canonical DBs are a Google-Drive zip; the HF mirror is scriptable). huggingface_hub is
used for the DBs because raw curl gets rate-limited on rapid multi-file pulls.
"""
from __future__ import annotations
import json
import os
import urllib.request

HERE = os.path.dirname(__file__)
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
DBS = os.path.join(DATA, "dbs")
RAW = "https://raw.githubusercontent.com/taoyds/spider/master/evaluation_examples/examples"


def get(url, dst):
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        return
    print("fetch", url)
    urllib.request.urlretrieve(url, dst)


def main():
    os.makedirs(DBS, exist_ok=True)
    get(f"{RAW}/dev.json", os.path.join(DATA, "dev.json"))
    get(f"{RAW}/tables.json", os.path.join(DATA, "tables.json"))
    dev = json.load(open(os.path.join(DATA, "dev.json"), encoding="utf-8"))
    dbids = sorted({e["db_id"] for e in dev})
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit("pip install huggingface_hub  (needed to fetch the SQLite DBs)")
    import shutil
    ok = 0
    for db in dbids:
        dst = os.path.join(DBS, db + ".sqlite")
        if os.path.exists(dst) and os.path.getsize(dst) > 2000:
            ok += 1
            continue
        for repo in ("premai-io/spider", "xlangai/spider"):
            try:
                p = hf_hub_download(repo_id=repo, repo_type="dataset",
                                    filename=f"database/{db}/{db}.sqlite")
                shutil.copy(p, dst)
                ok += 1
                break
            except Exception as e:                        # noqa: BLE001
                last = f"{repo}: {type(e).__name__}"
        else:
            print("  FAILED", db, last)
    print(f"databases: {ok}/{len(dbids)} in {DBS}")


if __name__ == "__main__":
    main()
