"""The GPU lease recipe of one reader candidate, through training/readers/lease.py's one lease loop and training/tools/runpod_api.py. Nothing runs without --go: by default it prints the plan (steps, hours, dollars, what comes back and how long that may take). A paid run needs its experiment contract (design/phase4_contract.txt) and the owner's word.

Before any paid lease: runpod_api must have the size-aware transfers (a transfer limit that scales with bytes, gzip on the pod, a kept pod when a finished run's download fails). They are on main; ensure_size_aware refuses a checkout without them: a 15-minute flat limit lost readers_v17b's weights.

The lease's time limit is planned from the data, not a flat number (readers_v16a hit a flat 330 minutes at 50,000 steps and lost $3.24): steps at a conservative rate, evals, plus the download's own reserve for its size at the slowest link waited for.

usage: python -m training.named.lease --experiment <id> --examples <dir> --backbone bge-small|xlmr-base|bge-m3 [--steps N | --epochs 4] [--seed 0] [--rate $/h] [--go]
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path

from engine.named.backbone import BACKBONES, source_of
from training.named import EXPERIMENTS_DIR
from training.readers.lease import ALLOC, CUDA_CHECK, LARGE_GPUS, NAME, PIP, RECORDED_CUDA, RECORDED_IMAGE, lease
from training.tools import runpod_api

ROOT = Path(__file__).resolve().parents[2]
# L40S-class first, then what has run these encoders before. A100s stay out: 1.9 steps/s against an L40S's 4.1 on the readers (2026-10-26).
GPUS = ["NVIDIA L40S", *[name for name in LARGE_GPUS if name != "NVIDIA L40S"]]
DOLLARS_PER_HOUR = (0.60, 0.80)
QUESTIONS_PER_STEP = 8
# Whole packages the pod imports, globbed at staging, and the single modules besides them.
POD_PACKAGES = ("engine/named", "engine/sheet", "engine/program", "training/named")
POD_FILES = ("engine/__init__.py", "engine/encoder_model.py", "engine/fk_edges.py", "engine/numeric.py", "engine/data/sheet_line_model.json", "engine/data/named_coordinates.json", "training/__init__.py", "training/factory/__init__.py", "training/factory/noise.py", "training/factory/paths.py", "training/readers/__init__.py", "training/tools/__init__.py")


@dataclass(frozen=True)
class Plan:
    """What one candidate is expected to cost. Estimates: see STEP_RATE."""
    backbone: str
    tabs: int
    questions: int
    steps: int
    steps_per_second: float
    train_hours: float
    eval_hours: float
    bundle_mb: float
    transfer_minutes_limit: float
    lease_minutes: int
    dollars_low: float
    dollars_high: float


# Tabs read a second on an L40S. Estimates, unmeasured on this model on a GPU; a lease's first log lines say the truth, and the contract stops one running at under half of this. What they rest on: the CPU smoke's production-config step (bge-small, 12 layers, 3 blocks, 384 hidden, 8 rows, 4 threads) read 37 distinct texts and 8 questions in 1.6 s, 12 ms of it building; XLM-R large at 16 x 256 tokens ran 4.1 steps/s there (16,800 tokens/s), so a step of about 350 tokens is a small GPU load and the Python around it (building, a loop per link) bounds it. XLM-R base costs about 3.5x per token.
# Measured since (2026-11-12/13, 39,285 steps each, 3090- to 4090-class cards): bge-small ran 3.0-3.9 steps/s, 0.26-0.33 s a
# step of which the forward pass is about 0.21 s (much the same on a 3090 as on a 4090: small kernels, not the card's
# arithmetic, bound it). XLM-R base (12 layers of 768, about 3.5x per token) and bge-m3 (24 layers of 1024, about 17x)
# are estimates, unmeasured on a GPU.
STEP_RATE = {"bge-small": 3.8, "xlmr-base": 2.5, "bge-m3": 1.5}
EVAL_SHARE = 0.10
HEADS_MB = 24.0


def bundle_mb(backbone: str) -> float:
    """What comes back: the encoder without its frozen word table, float32, plus the heads. The word table is the named base model's."""
    spec = BACKBONES[backbone]
    return round((spec.parameters - spec.table_parameters) * 4 / 1024 ** 2 + HEADS_MB, 1)


def plan_candidate(backbone: str, tabs: int, questions: int, *, epochs: float = 4.0, steps: int | None = None, rate: float | None = None) -> Plan:
    """A step reads one tab and up to QUESTIONS_PER_STEP of its questions; an epoch is every question once. The lease's limit carries the download's allowance for the bundle's size."""
    if steps is None:
        steps = max(1, math.ceil(epochs * max(tabs, questions / QUESTIONS_PER_STEP)))
    per_second = rate or STEP_RATE[backbone]
    train_hours = steps / per_second / 3600.0
    eval_hours = train_hours * EVAL_SHARE
    size = bundle_mb(backbone)
    transfer = max(15.0, size * 1024 ** 2 / runpod_transfer_bytes_per_second() / 60.0)
    lease_minutes = math.ceil((train_hours + eval_hours) * 60.0 * 1.25 + transfer + 15.0)
    return Plan(backbone, tabs, questions, steps, per_second, round(train_hours, 2), round(eval_hours, 2), size, round(transfer, 1), lease_minutes, round((train_hours + eval_hours) * DOLLARS_PER_HOUR[0] + (size / 60.0 / 60.0) * DOLLARS_PER_HOUR[0], 2), round((lease_minutes / 60.0) * DOLLARS_PER_HOUR[1], 2))


def runpod_transfer_bytes_per_second() -> int:
    """The slowest link a download is still waited for, runpod_api's own number when it has one."""
    return getattr(runpod_api, "TRANSFER_BYTES_PER_SECOND", 50 * 1024)


def ensure_size_aware() -> None:
    """A paid lease waits for the fixed transfers."""
    missing = [name for name in ("_transfer_seconds", "_compress_outputs", "_download") if not hasattr(runpod_api, name)]
    if missing:
        raise SystemExit(f"refused: training/tools/runpod_api.py lacks the size-aware transfers ({', '.join(missing)}): update from main before any paid lease")


PICKLED = ("engine/sheet/model.py", "engine/sheet/values.py")


def dataclass_fields(source: str) -> dict[str, tuple[str, ...]]:
    """Each class of a module with its annotated fields in order: what a pickle of it carries."""
    import ast
    return {node.name: tuple(item.target.id for item in node.body if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)) for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}


def ensure_same_tables(commit: str, root: Path = ROOT) -> None:
    """Examples are pickled tables of the factory's pinned commit and the pod runs this checkout: refuse if a class they pickle has other fields. A changed function or docstring beside them changes nothing a pickle holds."""
    for path in PICKLED:
        then = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{path}"], capture_output=True, text=True, encoding="utf-8", check=True).stdout
        now = (root / path).read_text(encoding="utf-8")
        if dataclass_fields(then) != dataclass_fields(now):
            raise SystemExit(f"refused: the classes the examples pickle changed in {path} since {commit[:7]}. Export again, or stage the pinned commit's engine/sheet.")


def pod_code(root: Path = ROOT) -> list[str]:
    files = list(POD_FILES)
    for package in POD_PACKAGES:
        files.extend(path.relative_to(root).as_posix() for path in sorted((root / package).rglob("*")) if path.is_file() and path.suffix in (".py", ".json") and "__pycache__" not in path.parts and "data" not in path.relative_to(root / package).parts)
    return files


def packed_examples(examples: str | Path, experiment: Path) -> Path:
    """The examples folder as one archive: one upload to resume and check."""
    archive = experiment / "examples.tar"
    experiment.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        with tarfile.open(archive, "w") as sink:
            sink.add(Path(examples), arcname="examples")
    return archive


def latest_state(experiment: Path) -> Path | None:
    """The newest training state an earlier lease brought back, if any: the next one goes on from it."""
    held = sorted(experiment.glob("out*/candidate/last_state.pt"), key=lambda path: path.stat().st_mtime)
    return held[-1] if held else None


POD_BACKBONES = "/workspace/backbones"


def backbone_upload(backbone: str) -> tuple[str, str] | None:
    """(this machine's copy, the pod's path) when the backbone is read from a copy here, else None and the pod reads the
    hub at the pinned revision. bge-m3's hub revision holds only pytorch_model.bin, which transformers will not load under
    the pod's torch 2.4: the copy here holds the same weights as safetensors, and the pod reads that copy."""
    spec = BACKBONES[backbone]
    source, local_only = source_of(spec)
    if source == spec.hub or not local_only:
        return None
    return source, f"{POD_BACKBONES}/{spec.name}"


def remote_command(backbone: str, steps: int, seed: int, *, hidden: int = 384, blocks: int = 3, resume: bool = False, extra: str = "", picks: bool = False, uploaded_backbone: bool = False) -> str:
    """Unpack, check CUDA by running a kernel, train into out/candidate. A failed run exits 4 with its log's tail; out/ always travels back. Evals come every tenth of the run, and the state is written with them."""
    read_from = f"NAMED_BACKBONE_DIR={POD_BACKBONES}" if uploaded_backbone else "NAMED_BACKBONE_ONLINE=1"
    train = f"{ALLOC} {read_from} python -m training.named.train --examples examples --experiment pod --out out/candidate --backbone {backbone} --steps {steps} --accumulate 4 --seed {seed} --hidden {hidden} --blocks {blocks} --amp --eval-every {max(500, steps // 10)}" + (f" {extra}" if extra else "") + (" --resume-from resume_state.pt" if resume else "")
    return f"cd /workspace && mkdir -p out && tar -xf examples.tar && {'tar -xf picks.tar && ' if picks else ''}{PIP}; pip install -q psutil 2>&1 | tail -1; {CUDA_CHECK}; {train} > out/train.log 2>&1 || {{ tail -5 out/train.log; exit 4; }}; tail -2 out/train.log; ls -la out/candidate out/candidate/best"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan, and on --go run, the lease of one reader candidate.")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--examples", required=True)
    parser.add_argument("--backbone", default="bge-small", choices=sorted(BACKBONES))
    parser.add_argument("--steps", type=int)
    parser.add_argument("--epochs", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-hourly-cost", type=float, default=0.80)
    parser.add_argument("--minutes", type=int, help="the lease's limit, over the plan's")
    parser.add_argument("--clouds", default="SECURE,COMMUNITY")
    parser.add_argument("--gpus")
    parser.add_argument("--image", default=RECORDED_IMAGE)
    parser.add_argument("--cuda", default=RECORDED_CUDA)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--corpus", help="the schema.org corpus's semantic_instances.jsonl: uploaded, and trained on as table profiles")
    parser.add_argument("--init", help="a bundle to start from (the step before): uploaded")
    parser.add_argument("--picks", help="a folder of pick examples (the teacher's recorded picks): uploaded")
    parser.add_argument("--qualified", help="a gate.json: uploaded; only the properties it qualified enter a link")
    parser.add_argument("--names", help="an experiment's name set (training.named.train --names): uploaded")
    parser.add_argument("--name-rows", help="an .npz of meaning outputs started at a feature of the hidden state (training.named.train --name-rows): uploaded")
    parser.add_argument("--train-flags", default="", help="more flags for training.named.train, as one string (--link-weight 0 --anchor-steps 300)")
    parser.add_argument("--go", action="store_true", help="lease and pay; without it, only the plan")
    args = parser.parse_args(argv)
    if not NAME.fullmatch(args.experiment):
        parser.error("--experiment takes letters, digits, '.', '_' and '-'")
    manifest = json.loads((Path(args.examples) / "manifest.json").read_text(encoding="utf-8"))
    plan = plan_candidate(args.backbone, int(manifest["tabs"]), int(manifest["gold"]), epochs=args.epochs, steps=args.steps)
    print(json.dumps(asdict(plan), indent=0, sort_keys=True))
    experiment = EXPERIMENTS_DIR / args.experiment
    state = latest_state(experiment)
    extra = " ".join(part for part in (args.train_flags, "--corpus corpus.jsonl" if args.corpus else "", "--init init" if args.init else "", "--picks picks" if args.picks else "", "--qualified gate.json" if args.qualified else "", "--names names.json" if args.names else "", "--name-rows name_rows.npz" if args.name_rows else "") if part)
    copy = backbone_upload(args.backbone)
    remote = remote_command(args.backbone, plan.steps, args.seed, resume=state is not None, extra=extra, picks=bool(args.picks), uploaded_backbone=copy is not None)
    args.minutes = args.minutes or plan.lease_minutes
    args.dry_run = not args.go
    if not args.go:
        print("remote command:", remote)
        print("uploads:", len(pod_code()), "code files; examples archive from", args.examples)
        return 0
    ensure_size_aware()
    ensure_same_tables(manifest["engine_commit"])
    free = shutil.disk_usage(ROOT).free / 1024 ** 3
    if free < plan.bundle_mb / 1024 * 2 + 2:
        raise SystemExit(f"refused: {free:.1f} GB free")
    archive = packed_examples(args.examples, experiment)
    uploads = [(str(archive), "/workspace/examples.tar")]
    if copy is not None:
        uploads.append(copy)
    if args.corpus:
        uploads.append((str(Path(args.corpus)), "/workspace/corpus.jsonl"))
    if args.picks:
        packed = experiment / "picks.tar"
        if not packed.exists():
            with tarfile.open(packed, "w") as sink:
                sink.add(Path(args.picks), arcname="picks")
        uploads.append((str(packed), "/workspace/picks.tar"))
    if args.qualified:
        uploads.append((str(Path(args.qualified)), "/workspace/gate.json"))
    if args.names:
        uploads.append((str(Path(args.names)), "/workspace/names.json"))
    if args.name_rows:
        uploads.append((str(Path(args.name_rows)), "/workspace/name_rows.npz"))
    if args.init:
        uploads.extend((str(Path(args.init) / name), f"/workspace/init/{name}") for name in ("named_bundle.json", "reader.pt"))
    if state is not None:
        kept = experiment / "resume_state.pt"
        shutil.copyfile(state, kept)                  # out/ moves aside between attempts: the upload needs a path that stays
        uploads.append((str(kept), "/workspace/resume_state.pt"))
        print("going on from", state)
    return lease(args, "named", remote, uploads, GPUS, experiments=EXPERIMENTS_DIR, code=pod_code(), state="runpod_named")


if __name__ == "__main__":
    raise SystemExit(main())
