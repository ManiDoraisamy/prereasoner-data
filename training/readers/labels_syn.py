"""Readings for Spider-Syn: Spider's questions reworded with synonyms of their schema words ("heads of the
departments" -> "leaders of the divisions"), each with its original's SQL. A reworded question takes its original's
gold readings (readings_train.jsonl); the original is the Spider question on the same database with the same SQL and
wording. Questions on the calibration databases (the split manifest's calibration_dbs) are left out, so calibration
stays unseen, and so are rewordings identical to their original.

usage: python -m training.readers.labels_syn <split dir> <Spider-Syn dir> <readings_train.jsonl> <out.jsonl>
           [--spider-zip <spider_data.zip>]
"""
import argparse
import json
import os
import zipfile

from training.readers.sources import SPIDER_ZIP


def main():
    ap = argparse.ArgumentParser(description="Readings for Spider-Syn's reworded questions.")
    ap.add_argument("split_dir", help="the folder of the split's manifest.json (calibration_dbs)")
    ap.add_argument("syn_dir", help="Spider-Syn's folder (only its train_spider.json)")
    ap.add_argument("readings", help="readings_train.jsonl, the gold readings of Spider's training questions")
    ap.add_argument("out")
    ap.add_argument("--spider-zip", default=SPIDER_ZIP)
    args = ap.parse_args()
    calibration = set(json.load(open(os.path.join(args.split_dir, "manifest.json")))["calibration_dbs"])
    originals = {}
    with zipfile.ZipFile(args.spider_zip) as z:
        for index, q in enumerate(json.loads(z.read("spider_data/train_spider.json"))):
            originals[(q["db_id"], q["query"].strip(), " ".join(q["question"].split()))] = ("train_spider", index)
    readings = {(r["source"], r["index"]): r for r in map(json.loads, open(args.readings, encoding="utf-8"))
                if "readings" in r}
    written = matched = 0
    with open(args.out, "w", encoding="utf-8", newline="\n") as sink:
        for name, source in (("train_spider.json", "syn_train"),):
            for index, q in enumerate(json.load(open(os.path.join(args.syn_dir, name), encoding="utf-8"))):
                if q["db_id"] in calibration or q["SpiderSynQuestion"].strip() == q["SpiderQuestion"].strip():
                    continue
                key = originals.get((q["db_id"], q["query"].strip(), " ".join(q["SpiderQuestion"].split())))
                if key is None:
                    continue
                matched += 1
                record = readings.get(key)
                if record is None:
                    continue
                sink.write(json.dumps({"source": source, "index": index, "db_id": q["db_id"],
                                       "status": record["status"], "readings": record["readings"]}) + "\n")
                written += 1
    print(f"Spider-Syn reworded questions matched to an original {matched}; with readings {written} -> {args.out}")


if __name__ == "__main__":
    main()
