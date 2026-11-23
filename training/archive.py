"""The archive: what this project made that is too large for git, kept off this machine.

Experiment data and records (training/*/data/experiments), the retired readers' artifacts, the Enron-derived
workbooks, other checkouts' ignored training data, model checkpoints, and code that never reached a remote are kept
as zstd tar parts in one private Hugging Face dataset. Every part is pinned by sha256 in training/archive.json, so a
clone names exactly what to fetch and checks what it got. Git holds what is small; the archive holds the rest.

  python -m training.archive list
  python -m training.archive fetch GROUP [GROUP ...]
  python -m training.archive pack GROUP --source DIR [--source DIR ...] --base DIR --restore repo|archive
                                  [--weights none|only|all] [--exclude RELPATH ...] [--note TEXT]

A group restores either into the repository (``repo``: gitignored experiment folders, at their own paths) or under the
archive root (``archive``: PREREASONER_ARCHIVE, default the folder that holds this clone, at the path the group was
packed from relative to its base). Hardlinked files inside one group are stored once. Reading and packing need
HF_TOKEN with access to the dataset, and Python 3.14 or later (the standard library's zstd); ``archived`` alone, which
the training code imports, runs on any supported Python.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("archive.json")
REPO_ID = "manidoraisamy/prereasoner-archive"
REPO_TYPE = "dataset"
PART_BYTES = 2 * 2**30
LEVEL = 6
WEIGHTS = frozenset({".pt", ".pth", ".bin", ".safetensors", ".ckpt", ".gguf", ".onnx", ".npy", ".npz", ".h5",
                     ".msgpack", ".model"})
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv"})
RESTORES = ("repo", "archive")


def _zstd():
    """The standard library's zstd (Python 3.14+), which packing and fetching need."""
    try:
        from compression import zstd
    except ImportError:
        raise SystemExit("training.archive pack and fetch need Python 3.14 or later (compression.zstd)") from None
    return zstd


def archive_root() -> Path:
    """Where ``archive`` groups restore: PREREASONER_ARCHIVE, else the folder that holds this clone."""
    return Path(os.environ.get("PREREASONER_ARCHIVE") or ROOT.parent)


def archived(relpath: str) -> str:
    """A path inside an ``archive`` group as restored, for code that reads archived data; ends with / when given one."""
    return str(archive_root() / relpath).replace("\\", "/") + ("/" if relpath.endswith("/") else "")


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"repo_id": REPO_ID, "groups": {}}


def _write_manifest(data: dict) -> None:
    MANIFEST.write_bytes((json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))


def _api():
    from huggingface_hub import HfApi
    return HfApi(token=os.environ.get("HF_TOKEN") or None)


def _kept(rel: str, name: str, weights: str, exclude: tuple[str, ...]) -> bool:
    if any(rel == e or rel.startswith(e + "/") for e in exclude):
        return False
    heavy = os.path.splitext(name)[1].lower() in WEIGHTS
    return {"none": not heavy, "only": heavy, "all": True}[weights]


def files_of(sources: list[Path], weights: str, exclude: tuple[str, ...]):
    """(path, path relative to its source) for every kept file, in a stable order."""
    for source in sources:
        for folder, dirs, names in os.walk(source):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(names):
                path = Path(folder) / name
                rel = path.relative_to(source).as_posix()
                if _kept(rel, name, weights, exclude):
                    yield path


class Parts(io.RawIOBase):
    """A write-only stream cut into PART_BYTES files; each finished part is handed to ``done`` and then deleted."""

    def __init__(self, folder: Path, done):
        self.folder, self.done, self.index = folder, done, 0
        self.handle = self.digest = None
        self.size = 0

    def writable(self) -> bool:
        return True

    def _open(self) -> None:
        self.path = self.folder / f"part-{self.index:04d}.tar.zst"
        self.handle, self.digest, self.size = open(self.path, "wb"), hashlib.sha256(), 0  # noqa: SIM115 (held across write calls)

    def _finish(self) -> None:
        self.handle.close()
        self.done(self.index, self.path, self.digest.hexdigest(), self.size)
        self.path.unlink()
        self.index += 1
        self.handle = None

    def write(self, data) -> int:
        view = memoryview(data).cast("B")
        written = 0
        while written < len(view):
            if self.handle is None:
                self._open()
            take = view[written:written + PART_BYTES - self.size]
            self.handle.write(take)
            self.digest.update(take)
            self.size += len(take)
            written += len(take)
            if self.size >= PART_BYTES:
                self._finish()
        return written

    def close(self) -> None:
        if self.handle is not None:
            self._finish()
        super().close()


def _upload(api, path: Path, name: str, message: str) -> str:
    """Upload one part; returns the Hub commit that added it, which a fetch pins."""
    for attempt in range(5):
        try:
            return api.upload_file(path_or_fileobj=str(path), path_in_repo=name, repo_id=REPO_ID,
                                   repo_type=REPO_TYPE, commit_message=message).oid
        except Exception as exc:  # network or Hub errors: retried, then raised
            if attempt == 4:
                raise
            print(f"  upload {name} failed ({exc.__class__.__name__}: {str(exc)[:200]}); retrying", flush=True)
            time.sleep(30 * (attempt + 1))


def pack(group: str, sources: list[Path], base: Path, restore: str, weights: str, exclude: tuple[str, ...],
         note: str) -> dict:
    """Stream the group's files into tar parts on the Hub and record them in training/archive.json."""
    api = _api()
    api.create_repo(REPO_ID, repo_type=REPO_TYPE, private=True, exist_ok=True)
    parts: list[dict] = []
    counted = {"files": 0, "bytes": 0}
    started = time.time()
    zstd = _zstd()

    def done(index: int, path: Path, sha: str, size: int) -> None:
        name = f"{group}/part-{index:04d}.tar.zst"
        revision = _upload(api, path, name, f"{group}: part {index}")
        parts.append({"path": name, "sha256": sha, "bytes": size, "revision": revision})
        print(f"  {name}: {size / 2**30:.2f} GB, {counted['files']} files so far, "
              f"{(time.time() - started) / 60:.1f} min", flush=True)

    with tempfile.TemporaryDirectory(dir=os.environ.get("PREREASONER_ARCHIVE_STAGE")) as stage:
        stream = Parts(Path(stage), done)
        with (zstd.ZstdFile(stream, mode="w", level=LEVEL) as packed,
              tarfile.open(fileobj=packed, mode="w|", format=tarfile.PAX_FORMAT) as tar):
            for path in files_of(sources, weights, exclude):
                tar.add(str(path), arcname=path.relative_to(base).as_posix(), recursive=False)
                counted["files"] += 1
                counted["bytes"] += path.stat().st_size
        stream.close()
    entry = {"restore": restore, "packed_from": [s.relative_to(base).as_posix() for s in sources],
             "weights": weights, "exclude": list(exclude), "note": note, "files": counted["files"],
             "bytes": counted["bytes"], "parts": parts,
             "packed": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")}
    data = manifest()
    data["repo_id"] = REPO_ID
    data["groups"][group] = entry
    _write_manifest(data)
    return entry


class Chain(io.RawIOBase):
    """The parts read back as one stream."""

    def __init__(self, paths: list[str]):
        self.paths, self.handle = list(paths), None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while True:
            if self.handle is None:
                if not self.paths:
                    return 0
                self.handle = open(self.paths.pop(0), "rb")  # noqa: SIM115 (held across reads)
            n = self.handle.readinto(buffer)
            if n:
                return n
            self.handle.close()
            self.handle = None


def fetch(group: str) -> Path:
    """Download a group's parts, check every sha256 against the manifest, and unpack it where it restores."""
    from huggingface_hub import hf_hub_download
    zstd = _zstd()
    entry = manifest()["groups"][group]
    local = []
    for part in entry["parts"]:
        path = hf_hub_download(REPO_ID, part["path"], repo_type=REPO_TYPE, revision=part.get("revision") or "main",
                               token=os.environ.get("HF_TOKEN") or None)
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != part["sha256"]:
            raise SystemExit(f"{part['path']}: sha256 {digest.hexdigest()} is not the pinned {part['sha256']}")
        local.append(path)
    dest = ROOT if entry["restore"] == "repo" else archive_root()
    with zstd.ZstdFile(io.BufferedReader(Chain(local)), mode="r") as packed, tarfile.open(fileobj=packed, mode="r|") as tar:
        tar.extractall(dest, filter="data")
    return dest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    get = sub.add_parser("fetch")
    get.add_argument("groups", nargs="+")
    put = sub.add_parser("pack")
    put.add_argument("group")
    put.add_argument("--source", action="append", required=True, type=Path)
    put.add_argument("--base", required=True, type=Path)
    put.add_argument("--restore", required=True, choices=RESTORES)
    put.add_argument("--weights", default="all", choices=("none", "only", "all"))
    put.add_argument("--exclude", action="append", default=[])
    put.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if args.command == "list":
        for name, entry in sorted(manifest()["groups"].items()):
            size = sum(p["bytes"] for p in entry["parts"])
            print(f"{name:28s} {entry['restore']:8s} {entry['files']:7d} files {entry['bytes'] / 2**30:7.2f} GB raw "
                  f"{size / 2**30:7.2f} GB packed  {entry['note']}")
    elif args.command == "fetch":
        for group in args.groups:
            print(f"{group} -> {fetch(group)}")
    else:
        entry = pack(args.group, [s.resolve() for s in args.source], args.base.resolve(), args.restore, args.weights,
                     tuple(args.exclude), args.note)
        print(f"{args.group}: {entry['files']} files, {entry['bytes'] / 2**30:.2f} GB raw, {len(entry['parts'])} parts")


if __name__ == "__main__":
    main(sys.argv[1:])
