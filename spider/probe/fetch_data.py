"""Fetch Spider examples, schemas, and SQLite databases into ../data (gitignored).

dev.json / tables.json come from the official taoyds/spider eval examples (they carry the pre-parsed
`sql` dict used by the official eval_hardness). The per-DB SQLite files come from the premai-io/spider
HF mirror (the canonical DBs are a Google-Drive zip; the HF mirror is scriptable). huggingface_hub is
used for the DBs because raw curl gets rate-limited on rapid multi-file pulls.

--calibration also writes the calibration split (spider/calibration/{dev,tables}.json, gitignored): every train_spider
question of the 20 databases spider/calibration/databases.json names, with their schemas, checked against the digests
recorded there. The repository keeps only those names: Spider's questions and gold SQL are not redistributed here.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import urllib.request

HERE = os.path.dirname(__file__)
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
DBS = os.path.join(DATA, "dbs")
CALIBRATION = os.path.abspath(os.path.join(HERE, "..", "calibration"))
RAW = "https://raw.githubusercontent.com/taoyds/spider/master/evaluation_examples/examples"


def get(url, dst):
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        return
    print("fetch", url)
    urllib.request.urlretrieve(url, dst)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-train", action="store_true",
        help="also fetch train_spider.json and every database referenced by it",
    )
    parser.add_argument(
        "--calibration", action="store_true",
        help="also write the calibration split into spider/calibration (implies --include-train)",
    )
    args = parser.parse_args()
    args.include_train = args.include_train or args.calibration
    os.makedirs(DBS, exist_ok=True)
    get(f"{RAW}/dev.json", os.path.join(DATA, "dev.json"))
    get(f"{RAW}/tables.json", os.path.join(DATA, "tables.json"))
    examples = json.load(open(os.path.join(DATA, "dev.json"), encoding="utf-8"))
    if args.include_train:
        train_path = os.path.join(DATA, "train_spider.json")
        get(f"{RAW}/train_spider.json", train_path)
        examples.extend(json.load(open(train_path, encoding="utf-8")))
    dbids = sorted({e["db_id"] for e in examples})
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
    if args.calibration:
        write_calibration()


def write_calibration():
    """The calibration split from the fetched train_spider.json and tables.json, checked against its recorded digests."""
    with open(os.path.join(CALIBRATION, "databases.json"), encoding="utf-8") as fh:
        spec = json.load(fh)
    chosen = set(spec["databases"])
    with open(os.path.join(DATA, "train_spider.json"), encoding="utf-8") as fh:
        train = json.load(fh)
    with open(os.path.join(DATA, "tables.json"), encoding="utf-8") as fh:
        tables = json.load(fh)
    parts = {"dev.json": [e for e in train if e["db_id"] in chosen], "tables.json": [t for t in tables if t["db_id"] in chosen]}
    for name, rows in parts.items():
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        if digest != spec["sha256_canonical"][name]:
            raise SystemExit(f"calibration {name}: sha256 {digest} is not the recorded {spec['sha256_canonical'][name]}")
        with open(os.path.join(CALIBRATION, name), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rows, fh)
    print(f"calibration: {len(parts['dev.json'])} questions, {len(parts['tables.json'])} schemas in {CALIBRATION}")


if __name__ == "__main__":
    main()
