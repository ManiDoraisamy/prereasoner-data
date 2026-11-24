"""The evaluation sets every measurement reads, from a clean clone.

Our own sets and records are tracked under regress/sets/ (sets/README.md); the public datasets are fetched by
``python -m regress.eval_sets fetch`` from their pinned sources (sets/sources.json) into PREREASONER_DATASETS
(default ~/.cache/prereasoner/datasets) and checked against their sha256 digests before any is read.

  wtq_dev_sample()            WikiTableQuestions dev (random-split-1-dev), the seeded 300 of the dev measurement
  wtq_train(n, skip)          seeded training questions (training.tsv minus dev) that SQUALL annotates, with its SQL
                              (annotated=False: the ones it leaves out, no SQL)
  wtq_test(ids)               test questions (pristine-unseen-tables) by id: the public benchmark's samples only
  wtq_test_sample(n, seed, exclude)  a seeded sample of the test ids, leaving out the ids already sampled
  databench(split)            DataBench train 001-049, dev 050-065 (graded as the dev measurement graded), test 066-080
                              (graded by databench_eval's own default_compare: regress/databench_compare.py)
  fresh_dev()                 our 176 questions on 44 public US government workbooks (sets/fresh_dev)
  messy()                     regress/messy_cases.py's dev split on its seeded workbooks

Every set is a list of training.named.evaluate.Item: an id, the question, the workbook it reads (``load`` compiles it),
and ``grade`` over a result's rows (each row's first cell is its answer: the engine shows the answer column first).

CallStore holds recorded API answers by request-body hash (sets/calls/*.jsonl.gz for the Decisions API; the public
benchmark's frontier systems in sets/public/calls/): a replay reads them, and a run that sends a body none holds keeps
its answer in a batch of its own, ``pack``ed to .jsonl.gz before it is committed.

usage: python -m regress.eval_sets fetch [wtq squall databench]
       python -m regress.eval_sets pack [FOLDER]
"""
from __future__ import annotations

import ast
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

from training.named.evaluate import Item

REPO = Path(__file__).resolve().parents[1]
SETS = REPO / "regress" / "sets"
CALLS = SETS / "calls"
SOURCES = json.loads((SETS / "sources.json").read_text(encoding="utf-8"))
WTQ_DEV_SAMPLE, WTQ_DEV_SEED = 300, 20261110
WTQ_TRAIN_SEED = 20261122


def datasets() -> Path:
    return Path(os.environ.get("PREREASONER_DATASETS") or Path.home() / ".cache" / "prereasoner" / "datasets")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _need(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing: run python -m regress.eval_sets fetch")
    return path


# ------------------------------------------------------------------------------------------------------------ fetch
def _download(url: str, target: Path, sha256: str) -> None:
    if target.exists() and _sha256(target) == sha256:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print("fetch", url, flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "prereasoner-eval"})
    with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as sink:      # noqa: S310 (pinned https sources)
        shutil.copyfileobj(response, sink)
    if _sha256(partial) != sha256:
        partial.unlink()
        raise RuntimeError(f"{url}: its sha256 is not the pinned {sha256}")
    os.replace(partial, target)


def fetch(names=("wtq", "squall", "databench")) -> None:
    """Download each public dataset from its pinned source and check every file the evaluation reads."""
    root = datasets()
    if "wtq" in names:
        source = SOURCES["wtq"]
        archive = root / source["folder"] / Path(source["url"]).name
        _download(source["url"], archive, source["sha256"])
        if not (root / source["folder"] / "WikiTableQuestions" / "data" / "training.tsv").exists():
            with zipfile.ZipFile(archive) as packed:
                packed.extractall(root / source["folder"])
    if "squall" in names:
        source = SOURCES["squall"]
        _download(source["url"], root / source["path"], source["sha256"])
    if "databench" in names:
        from huggingface_hub import hf_hub_download
        source = SOURCES["databench"]
        for relative, sha256 in source["sha256"].items():
            target = root / source["folder"] / relative
            if target.exists() and _sha256(target) == sha256:
                continue
            fetched = Path(hf_hub_download(source["repo"], relative, repo_type=source["repo_type"], revision=source["revision"],
                                           local_dir=root / source["folder"]))
            if _sha256(fetched) != sha256:
                raise RuntimeError(f"databench {relative}: its sha256 is not the pinned {sha256}")
    print("datasets ready in", root, flush=True)


# ------------------------------------------------------------------------------------------------- WikiTableQuestions
def _wtq_root() -> Path:
    return datasets() / SOURCES["wtq"]["folder"] / "WikiTableQuestions"


def _wtq_rows(split: str) -> list[dict]:
    with open(_need(_wtq_root() / "data" / split), encoding="utf-8") as source:
        reader = csv.reader(source, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader)
        return [dict(zip(header, row)) for row in reader]


def _wtq_canon(tagged: str) -> dict[str, str]:
    canon = {}
    with open(_need(_wtq_root() / "tagged" / "data" / tagged), encoding="utf-8") as source:
        reader = csv.reader(source, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader)
        for row in reader:
            record = dict(zip(header, row))
            canon[record["id"]] = record.get("targetCanon", "")
    return canon


def wtq_grader(target: str, canon: str = ""):
    """The release's own evaluator (regress/wtq_evaluator.py) over the gold strings and their CoreNLP canonical forms;
    the answer is each row's first cell."""
    from regress import wtq_evaluator as evaluator

    strings = evaluator.tsv_unescape_list(target)
    canons = evaluator.tsv_unescape_list(canon) if canon else None
    gold = evaluator.to_value_list(strings, canons if canons and len(canons) == len(strings) else None)

    def grade(rows) -> bool:
        predicted = []
        for row in rows:
            if not row or row[0] in (None, ""):
                continue
            value = row[0]
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            predicted.append(str(value))
        return bool(predicted) and bool(evaluator.check_denotation(gold, evaluator.to_value_list(predicted)))
    return grade


def _wtq_typed(cell: str):
    text = cell.strip()
    try:
        return float(text) if text and text.replace(".", "", 1).lstrip("-").isdigit() else text
    except ValueError:
        return text


def _wtq_loader(context: str):
    def load():
        from engine.program.catalog import Catalog
        from engine.sheet import compile_workbook, workbook_from_json
        # The dataset's csv escapes a quote inside a quoted cell as \" ("\"Die 4\""), so it is read that way.
        with open(_wtq_root() / context, encoding="utf-8") as source:
            rows = [[_wtq_typed(cell) for cell in row] for row in csv.reader(source, escapechar="\\", doublequote=False)]
        header = [str(h) if h != "" else f"column {i + 1}" for i, h in enumerate(rows[0])]
        return Catalog.from_model(compile_workbook(workbook_from_json({"name": context, "tabs": [{"name": "table", "rows": [header, *rows[1:]]}]})))
    return load


def _wtq_items(rows: list[dict], canon: dict[str, str]) -> list[Item]:
    return [Item(r["id"], r["utterance"], r["context"], r["context"], "", "gold", _wtq_loader(r["context"]),
                 wtq_grader(r["targetValue"], canon.get(r["id"], "")), None, []) for r in rows]


def wtq_dev_sample(n: int = WTQ_DEV_SAMPLE, seed: int = WTQ_DEV_SEED) -> list[Item]:
    """The dev measurement's seeded sample of WikiTableQuestions dev, in workbook order."""
    rows = sorted(_wtq_rows("random-split-1-dev.tsv"), key=lambda r: (r["context"], r["id"]))
    keep = set(random.Random(seed).sample(sorted(r["id"] for r in rows), n))
    return _wtq_items([r for r in rows if r["id"] in keep], _wtq_canon("training.tagged"))


def wtq_train(n: int = 600, skip: int = 0, seed: int = WTQ_TRAIN_SEED, annotated: bool = True) -> tuple[list[Item], dict[str, str]]:
    """Seeded training questions outside random-split-1-dev that SQUALL annotates (n after skipping the first skip of
    its order), with SQUALL's human-written query for each: the find set, read for diagnosis. With annotated=False, the
    ones SQUALL leaves out instead (a fifth of training: the questions its SQL did not write), with no query."""
    squall = {entry["nt"]: entry for entry in json.loads(_need(datasets() / SOURCES["squall"]["path"]).read_text(encoding="utf-8"))}
    dev = {r["id"] for r in _wtq_rows("random-split-1-dev.tsv")}
    rows = sorted((r for r in _wtq_rows("training.tsv") if r["id"] not in dev and (r["id"] in squall) == annotated),
                  key=lambda r: (r["context"], r["id"]))
    items = _wtq_items(rows, _wtq_canon("training.tagged"))
    items.sort(key=lambda item: item.id)
    random.Random(seed).shuffle(items)
    chosen = sorted(items[skip:skip + n], key=lambda item: (item.book, item.id))
    return chosen, {item.id: " ".join(token[1] for token in squall[item.id]["sql"]) for item in chosen if item.id in squall}


def wtq_test_ids() -> list[str]:
    return sorted(r["id"] for r in _wtq_rows("pristine-unseen-tables.tsv"))


def wtq_test(ids) -> list[Item]:
    """WikiTableQuestions test questions by id (the public benchmark's samples), in workbook order."""
    keep = set(ids)
    rows = sorted((r for r in _wtq_rows("pristine-unseen-tables.tsv") if r["id"] in keep), key=lambda r: (r["context"], r["id"]))
    return _wtq_items(rows, _wtq_canon("pristine-unseen-tables.tagged"))


def wtq_test_sample(n: int, seed: int, exclude=()) -> list[str]:
    """A seeded sample of n test ids, the ids already sampled (exclude) left out."""
    left = [key for key in wtq_test_ids() if key not in set(exclude)]
    return sorted(random.Random(seed).sample(left, n))


# ---------------------------------------------------------------------------------------------------------- DataBench
DATABENCH_SPLITS = {"train": range(1, 50), "dev": range(50, 66), "test": range(66, 81)}
STRIP = "[]'\" "


def _databench_root() -> Path:
    return datasets() / SOURCES["databench"]["folder"] / "data"


def _databench_cell(value):
    import pandas as pd
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, bool):
        return str(value)
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return str(list(value))
        if isinstance(value, float) and math.isnan(value):
            return None
        return str(value) if isinstance(value, bool) else value
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else str(value)
    if isinstance(value, (list, tuple)):
        return str(list(value))
    return str(value)


def _databench_number(text) -> float:
    return round(float("".join(ch for ch in str(text) if ch.isdigit() or ch in ".-")), 2)


def _databench_items_of(text) -> list[str]:
    text = str(text).strip()
    try:
        value = ast.literal_eval(text)
        if isinstance(value, (list, tuple)):
            return [str(v).strip(STRIP) for v in value]
    except (ValueError, SyntaxError):
        pass
    return [part.strip(STRIP) for part in text.strip("[]").split(",") if part.strip(STRIP)]


def databench_same(value, truth, kind: str) -> bool:
    """The dev measurement's comparison per answer type (databench_eval's default_compare as reimplemented for
    development, stricter on a few forms; the test split is graded by the official one)."""
    import pandas as pd
    try:
        if kind == "boolean":
            return str(value).strip(STRIP).lower() == str(truth).strip(STRIP).lower()
        if kind == "category":
            a, b = str(value).strip(STRIP), str(truth).strip(STRIP)
            if a == b:
                return True
            try:
                return pd.to_datetime(a).date() == pd.to_datetime(b).date()
            except (ValueError, TypeError, OverflowError):
                return False
        if kind == "number":
            return _databench_number(value) == _databench_number(truth)
        if kind == "list[category]":
            return sorted(_databench_items_of(value)) == sorted(_databench_items_of(truth))
        if kind == "list[number]":
            return sorted(_databench_number(v) for v in _databench_items_of(value)) == sorted(_databench_number(v) for v in _databench_items_of(truth))
    except (ValueError, TypeError):
        return False
    return False


def databench_answer(rows, kind: str):
    """The answer a result states: each row's first cell; a list answer every row's, as its Python list text."""
    firsts = [row[0] for row in rows if row is not None and len(row) and row[0] not in (None, "")]
    if not firsts:
        return None
    return str(firsts) if kind.startswith("list") else firsts[0]


def databench_grader(kind: str, truth, official: bool = False):
    from regress.databench_compare import default_compare

    def grade(rows) -> bool:
        value = databench_answer(rows, kind)
        if official:
            return bool(default_compare(value, truth, kind))
        return value is not None and databench_same(value, truth, kind)
    return grade


def _databench_loader(folder: Path, tab: str):
    def load():
        import pandas as pd
        from engine.program.catalog import Catalog
        from engine.sheet import compile_workbook, workbook_from_json
        frame = pd.read_parquet(folder / "all.parquet")
        rows = [[_databench_cell(v) for v in record] for record in frame.itertuples(index=False, name=None)]
        return Catalog.from_model(compile_workbook(workbook_from_json({"name": folder.name, "tabs": [{"name": tab, "rows": [[str(c) for c in frame.columns], *rows]}]})))
    return load


def databench_book_size(folder: Path) -> tuple[int, int]:
    import pyarrow.parquet as pq
    meta = pq.ParquetFile(folder / "all.parquet").metadata
    return meta.num_rows, meta.num_columns


def databench(split: str = "dev", max_rows: int | None = None) -> list[Item]:
    """DataBench's questions of a split, one workbook per dataset (every row as an upload gives it). The test split is
    graded by the official comparison; max_rows leaves out larger datasets (the dev measurement read at most 60,000)."""
    import pandas as pd
    items = []
    for n in DATABENCH_SPLITS[split]:
        folder = next(_need(_databench_root()).glob(f"{n:03d}_*"))
        if max_rows and databench_book_size(folder)[0] > max_rows:
            continue
        tab = re.sub(r"^\d+_", "", folder.name).replace("_", " ")
        load = _databench_loader(folder, tab)
        for at, row in enumerate(pd.read_parquet(folder / "qa.parquet").itertuples(index=False)):
            items.append(Item(f"db/{folder.name}/{at}", row.question, folder.name, folder.name, row.type, "gold", load,
                              databench_grader(row.type, row.answer, official=split == "test"), None, []))
    return items


# ------------------------------------------------------------------------------------------------------- fresh dev
FRESH_DEV = SETS / "fresh_dev"
RELATIVE = 1e-4
SERIAL_FIRST, SERIAL_LAST = 1, 2958465     # Excel's serial days (1900-01-01 .. 9999-12-31)
_fresh_common = None


def _common():
    """The set's own normalizers (sets/fresh_dev/scripts/common.py: norm_text, norm_yes_no, parse_list, to_number)."""
    global _fresh_common
    if _fresh_common is None:
        spec = importlib.util.spec_from_file_location("fresh_dev_common", FRESH_DEV / "scripts" / "common.py")
        _fresh_common = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_fresh_common)
    return _fresh_common


def _numbers_agree(a, b) -> bool:
    return a is not None and b is not None and (a == b or abs(a - b) <= RELATIVE * max(abs(a), abs(b)))


def _day(value):
    """A cell read as a day: an ISO date (with or without a time), a serial day, or a written date; None otherwise."""
    import datetime as dt
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if float(value).is_integer() and SERIAL_FIRST <= value <= SERIAL_LAST and not 1000 <= value <= 2999:
            return (dt.date(1899, 12, 30) + dt.timedelta(days=int(value))).isoformat()
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ][\d:.]+)?", text):
        return text[:10]
    try:
        import pandas as pd
        return pd.to_datetime(text, errors="raise").date().isoformat()
    except (ValueError, TypeError, OverflowError, NotImplementedError):    # a stamp outside Python's dates is no day
        return None


def _dates_agree(answer, gold) -> bool:
    """At the gold's precision: a gold year takes any day of that year or the year itself, a gold month any day of it."""
    common = _common()
    gold = common.norm_date(gold)
    plain = common.norm_date(answer)
    if plain and plain == gold:
        return True
    if re.fullmatch(r"\d{4}", gold):
        number = common.to_number(answer)
        if number is not None and number == int(gold):
            return True
        day = _day(answer)
        return day is not None and day[:4] == gold
    if re.fullmatch(r"\d{4}-\d{2}", gold):
        day = _day(answer)
        return day is not None and day[:7] == gold
    want, day = _day(gold), _day(answer)
    return want is not None and day is not None and day == want


def _lists_agree(answer, gold) -> bool:
    common = _common()
    a, b = common.parse_list(answer), common.parse_list(gold)
    if len(a) != len(b):
        return False
    numbers_a, numbers_b = [common.to_number(v) for v in a], [common.to_number(v) for v in b]
    if all(n is not None for n in numbers_a + numbers_b):
        return all(_numbers_agree(x, y) for x, y in zip(sorted(numbers_a), sorted(numbers_b)))
    return sorted(common.norm_text(v) for v in a) == sorted(common.norm_text(v) for v in b)


def fresh_answer(rows, answer_type: str):
    """The first row's first cell (a number question whose first cell is no number takes that row's last number: a rank
    row is its key, then its figure); a list answer every row's first cell."""
    to_number = _common().to_number
    rows = [row for row in rows if row is not None and len(row)]
    if not rows:
        return None
    if answer_type == "list":
        firsts = [row[0] for row in rows if row[0] not in (None, "")]
        return firsts or None
    first = rows[0][0]
    if answer_type == "number" and to_number(first) is None:
        numbers = [cell for cell in rows[0] if to_number(cell) is not None]
        return numbers[-1] if numbers else None
    return None if first in (None, "") else first


def fresh_agree(answer_type: str, answer, gold) -> bool:
    """The frozen set's rules: numbers within 1e-4 relative, lists order-free, yes/no and text through the set's own
    normalizers, a date at the gold's precision."""
    common = _common()
    if answer is None or gold is None:
        return False
    if answer_type == "number":
        return _numbers_agree(common.to_number(answer), common.to_number(gold))
    if answer_type == "list":
        return _lists_agree(answer, gold)
    if answer_type == "yes_no":
        said = common.norm_yes_no(answer)
        return said in ("yes", "no") and said == common.norm_yes_no(gold)
    if answer_type == "date":
        return _dates_agree(answer, gold)
    a, b = common.to_number(answer), common.to_number(gold)     # a name: a year or a code written as a number reads as that number
    if a is not None and b is not None:
        return _numbers_agree(a, b)
    return common.norm_text(answer) != "" and common.norm_text(answer) == common.norm_text(gold)


def fresh_workbook(path: Path) -> dict:
    """The whole file as a user uploads it: every tab of an .xlsx (engine/sheet/xlsx_reader.py), a .csv as one tab of
    text cells with a plain number read as a number."""
    if path.suffix.lower() == ".xlsx":
        from engine.sheet.xlsx_reader import read_workbook
        return read_workbook(str(path))
    from engine.tables import sheet_cell
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=encoding, newline="") as source:
                rows = [[sheet_cell(cell) for cell in row] for row in csv.reader(source)]
            break
        except UnicodeDecodeError:
            continue
    return {"name": path.name, "tabs": [{"name": path.stem, "rows": rows}]}


def _fresh_loader(name: str):
    def load():
        from engine.program.catalog import Catalog
        from engine.sheet import compile_workbook, workbook_from_json
        return Catalog.from_model(compile_workbook(workbook_from_json(fresh_workbook(FRESH_DEV / "workbooks" / Path(name).name))))
    return load


def fresh_dev() -> list[Item]:
    """Our development questions on public US government workbooks (sets/fresh_dev/manifest.json), graded by the frozen
    set's rules. The workbooks from a publication the frozen set also draws on are left out
    (work/same_publication_as_frozen.json), so nothing is developed on a layout family the frozen gate meets."""
    left_out = {Path(name).name for name in json.loads((FRESH_DEV / "work" / "same_publication_as_frozen.json").read_text(encoding="utf-8"))["files"]}
    items = []
    with open(FRESH_DEV / "questions.jsonl", encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if Path(row["workbook_file"]).name in left_out:
                continue
            answer_type, gold = row["answer_type"], row["gold"]
            items.append(Item(f"fd/{row['id']}", row["question"], row["workbook_file"], row["domain"], row["kind"], "gold",
                              _fresh_loader(row["workbook_file"]),
                              lambda rows, t=answer_type, g=gold: fresh_agree(t, fresh_answer(list(rows), t), g), None, []))
    return items


def messy() -> list[Item]:
    from training.named.evaluate import load_messy
    return load_messy("dev")


# ------------------------------------------------------------------------------------------------------- call store
class CallStore:
    """Recorded answers by request-body hash: every *.jsonl.gz and *.jsonl of ``folder`` (one line each: hash and the
    recorded answer), read once on first use. ``keep`` adds a new answer to this run's own batch (``batch``.jsonl in the
    folder), so parallel runs never write one file; ``pack`` compresses a finished batch for commit."""

    def __init__(self, folder: Path = CALLS, batch: str | None = None):
        self.folder, self.batch = Path(folder), batch
        self.lock, self.held = threading.Lock(), None

    def _load(self) -> dict:
        if self.held is None:
            held = {}
            for path in sorted(self.folder.glob("*.jsonl*")):
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rt", encoding="utf-8") as source:
                    for line in source:
                        if line.strip() and line.endswith("\n"):     # a last line without its end is another run's write in progress
                            record = json.loads(line)
                            held[record.pop("hash")] = record
            self.held = held
        return self.held

    def get(self, digest: str) -> dict | None:
        with self.lock:
            return self._load().get(digest)

    def keep(self, digest: str, answer: dict) -> None:
        if self.batch is None:
            raise RuntimeError("this store records nothing: give it a batch name")
        with self.lock:
            self._load()[digest] = answer
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.folder / f"{self.batch}.jsonl", "a", encoding="utf-8", newline="\n") as sink:
                sink.write(json.dumps({"hash": digest, **answer}, ensure_ascii=False, sort_keys=True) + "\n")


def pack(folder: Path = CALLS) -> None:
    """Each plain batch of ``folder`` compressed to .jsonl.gz (sorted by hash, no timestamp, so the same answers give
    the same bytes), the plain file removed. A batch packed before keeps its lines: a run still writing it starts the
    plain file again, and the next pack adds those lines to the packed ones rather than replacing them."""
    for path in sorted(Path(folder).glob("*.jsonl")):
        target = path.with_name(path.name + ".gz")
        held = gzip.decompress(target.read_bytes()).decode("utf-8").splitlines() if target.exists() else []
        lines = sorted({line for line in [*held, *path.read_text(encoding="utf-8").splitlines()] if line.strip() and line.endswith("}")},
                       key=lambda line: json.loads(line)["hash"])
        with open(target, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as sink:
            for line in lines:
                sink.write((line + "\n").encode("utf-8"))
        path.unlink()
        print("packed", target, len(lines), flush=True)


def main(argv: list[str]) -> int:
    if argv[:1] == ["fetch"]:
        fetch(tuple(argv[1:]) or ("wtq", "squall", "databench"))
        return 0
    if argv[:1] == ["pack"]:
        pack(Path(argv[1]) if len(argv) > 1 else CALLS)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
