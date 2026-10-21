"""GPU leases for the readers' trainers. Each job builds the remote command the recorded runs used, stages the
pod-side code of this package, and runs it through training/tools/runpod_api.py's owned, time-bounded lease: first
on the SECURE cloud, then on COMMUNITY when that attempt fails (no instance, no CUDA, or a failed training).

Jobs:
  full      stage 2 (readings) on <data>/stage2.jsonl.gz: XLM-R large, lr 1e-5, gradient checkpointing; the model
            is shrunk on the pod (shrink_state) before out/ comes back.
  folds     cross-fold stage-2 models on <data>/stage2.jsonl.gz (training.readers.folds runs on the pod); each
            fold's weights are deleted on the pod, so only its held-out outputs (calibration_outputs.jsonl) return.
  rescorer  re-scorers one after another, each --job NAME=TRAIN.jsonl.gz=CAL.jsonl.gz[=large]: trained on TRAIN's
            candidates, its checkpoint selected on CAL's; "large" is XLM-R large with --checkpointing --questions 4
            on 24 GB cards, else XLM-R base on 16 GB or more.

Every remote command: cd /workspace, unzip the data, pip install transformers>=4.44,<5, exit 3 when CUDA is
unavailable, train (exit 4 when training fails), shrink (``> log && test -s small.pt && rm model.pt``), list out/.
The pod's /workspace/out comes back to training/readers/data/experiments/<id>/out (a failed attempt's recovered
out/ is moved aside to out.<cloud>, so the next attempt never nests into it); each attempt's log is
<id>/lease_<cloud>.log and the code that ran is <id>/code. The lease's state file is
~/.cache/prereasoner/runpod_readers_<id>.json, so parallel leases never share one.

usage: python -m training.readers.lease full --experiment <id> --data <dir with stage2.jsonl[.gz]>
           [--epochs 15] [--eval-from 8] [--backbone FacebookAI/xlm-roberta-large] [--lr 1e-5]
       python -m training.readers.lease folds --experiment <id> --data <dir> --folds 0,1 [--epochs 12] [--eval-from 9]
       python -m training.readers.lease rescorer --experiment <id> --job NAME=TRAIN.jsonl.gz=CAL.jsonl.gz[=large] ...
       options of every job: [--minutes N] [--max-hourly-cost 1.0] [--clouds SECURE,COMMUNITY] [--gpus a,b]
           [--image IMAGE] [--cuda 12.4,12.8] [--dry-run]
"""
import argparse
import gzip
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from training.readers import EXPERIMENTS_DIR

ROOT = Path(__file__).resolve().parents[2]
LARGE = "FacebookAI/xlm-roberta-large"
# XLM-R large trains with checkpointing on 24 GB or more; slower or smaller cards would outrun the lease.
LARGE_GPUS = ["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5090", "NVIDIA L40S", "NVIDIA RTX 6000 Ada Generation",
              "NVIDIA RTX A6000", "NVIDIA A40"]
BASE_GPUS = LARGE_GPUS + ["NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000", "NVIDIA RTX A4500", "NVIDIA GeForce RTX 4080",
                          "NVIDIA RTX 4000 Ada Generation", "NVIDIA RTX A4000", "NVIDIA L4"]
# The image and CUDA versions the recorded runs leased (their logs report torch 2.4.1+cu124). An empty --image or
# --cuda leaves runpod_api's digest-pinned default.
RECORDED_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
RECORDED_CUDA = "12.4,12.5,12.6,12.7,12.8,12.9"
# What the pod imports: the shared model definitions and this package's GPU-side steps, nothing that needs the engine.
POD_CODE = ("engine/__init__.py", "engine/readers/layout.py", "engine/readers/numerals.py", "engine/readers/models.py",
            "training/__init__.py", "training/readers/__init__.py", "training/readers/train.py",
            "training/readers/folds.py", "training/readers/rescorer.py", "training/readers/shrink_state.py")
PIP = "pip install -q 'transformers>=4.44,<5' 2>&1 | tail -1"
CUDA_CHECK = ("python -c 'import sys, torch; ok = torch.cuda.is_available(); "
              "print(torch.__version__, ok and torch.cuda.get_device_name(0)); sys.exit(0 if ok else 3)' || exit 3")
ALLOC = "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"
NAME = re.compile(r"[A-Za-z0-9._-]+")


# ------------------------------------------------------------------------------------------------ remote commands

def prelude(archives):
    return f"cd /workspace && mkdir -p out && gunzip -kf {archives} && {PIP}; {CUDA_CHECK}"


def shrink(model, backbone, small, log):
    return (f"python -m training.readers.shrink_state shrink {model} {backbone} {small} > {log} 2>&1 "
            f"&& test -s {small} && rm -f {model}; tail -1 {log}")


def readings(backbone, lr, epochs, eval_from, examples, out):
    return (f"{ALLOC} python -m training.readers.train readings --backbone {backbone} --epochs {epochs} --lr {lr} "
            f"--checkpointing --examples {examples} --out {out} --eval-from {eval_from}")


def full_command(backbone, lr, epochs, eval_from):
    """Stage 2 on stage2.jsonl into out/stage2, shrunk."""
    train = readings(backbone, lr, epochs, eval_from, "stage2.jsonl", "out/stage2")
    return (f"{prelude('stage2.jsonl.gz')}; {train} > out/stage2.log 2>&1 || "
            "{ grep -v Warning out/stage2.log | tail -4; exit 4; }; grep -v Warning out/stage2.log | tail -4; "
            f"{shrink('out/stage2/model.pt', backbone, 'out/stage2/small.pt', 'out/shrink.log')}; ls -la out/stage2")


def folds_command(folds, backbone, lr, epochs, eval_from):
    """The fold files made on the pod, then each wanted fold trained into out/fold<i>, its weights deleted."""
    loop = "; ".join(
        f"{readings(backbone, lr, epochs, eval_from, f'folds/fold{i}.jsonl', f'out/fold{i}')} > out/fold{i}.log 2>&1"
        f" || {{ tail -3 out/fold{i}.log; exit 4; }}; rm -f out/fold{i}/model.pt" for i in folds)
    return f"{prelude('stage2.jsonl.gz')}; python -m training.readers.folds stage2.jsonl folds && {loop}; ls -la out/*"


def rescorer_command(jobs):
    """Each (name, train, calibration, size) re-scorer trained into out/<name>, shrunk."""
    steps = []
    for name, _, _, size in jobs:
        backbone = f"FacebookAI/xlm-roberta-{size}"
        extra = " --checkpointing --questions 4" if size == "large" else ""
        steps.append(
            f"{ALLOC} python -m training.readers.rescorer train --candidates {name}_train.jsonl "
            f"--calibration {name}_cal.jsonl --backbone {backbone} --out out/{name} --epochs 3{extra} "
            f"> out/{name}.log 2>&1 || {{ tail -5 out/{name}.log; exit 4; }}; grep epoch out/{name}/log.jsonl; "
            + shrink(f"out/{name}/model.pt", backbone, f"out/{name}/small.pt", f"out/{name}.shrink.log"))
    return f"{prelude('*.jsonl.gz')}; " + "; ".join(steps) + "; ls -la out/*"


# ---------------------------------------------------------------------------------------------------------- jobs

def stage2_archive(data, make=True):
    """<data>/stage2.jsonl.gz, made from stage2.jsonl when only that exists (and ``make``)."""
    data = Path(data)
    archive = data / "stage2.jsonl.gz"
    if make and not archive.exists():
        with open(data / "stage2.jsonl", "rb") as source, gzip.open(archive, "wb") as sink:
            shutil.copyfileobj(source, sink)
    return archive


def parse_job(text):
    """NAME=TRAIN=CAL[=large] -> (name, train path, calibration path, "base" or "large")."""
    parts = text.split("=")
    if len(parts) not in (3, 4) or not NAME.fullmatch(parts[0]) or (len(parts) == 4 and parts[3] != "large"):
        raise argparse.ArgumentTypeError(f"expected NAME=TRAIN.jsonl.gz=CAL.jsonl.gz[=large], got {text!r}")
    return parts[0], parts[1], parts[2], "large" if len(parts) == 4 else "base"


def stage_code(directory):
    """The pod-side modules copied into ``directory`` as their packages (uploaded as /workspace/engine and
    /workspace/training, so the pod runs them with python -m from /workspace)."""
    if directory.exists():
        shutil.rmtree(directory)
    for relative in POD_CODE:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)


def lease(args, job, remote, uploads, gpus):
    """Run the lease on each cloud in turn until one succeeds; the exit code of the last attempt."""
    experiment = EXPERIMENTS_DIR / args.experiment
    out = experiment / "out"
    code_dir = experiment / "code"
    uploads = [(str(code_dir / "engine"), "/workspace/engine"),
               (str(code_dir / "training"), "/workspace/training")] + uploads
    command = [sys.executable, "-m", "training.tools.runpod_api", "lease", "--max-minutes", str(args.minutes),
               "--max-hourly-cost", str(args.max_hourly_cost)]
    env = dict(os.environ, RUNPOD_GPUS=",".join(args.gpus.split(",") if args.gpus else gpus),
               PREREASONER_RUNPOD_STATE=str(Path.home() / ".cache" / "prereasoner"
                                            / f"runpod_readers_{args.experiment}.json"))
    for name, value in (("RUNPOD_IMAGE", args.image), ("RUNPOD_CUDA", args.cuda)):
        if value:
            env[name] = value
        else:
            env.pop(name, None)
    if args.dry_run:
        print("remote command:", remote)
        print("uploads:", *(f"{local} -> {path}" for local, path in uploads), sep="\n  ")
        print("download: /workspace/out ->", out)
        print("gpus:", env["RUNPOD_GPUS"], "| image:", env.get("RUNPOD_IMAGE", "runpod_api default"),
              "| cuda:", env.get("RUNPOD_CUDA", "runpod_api default"))
        return 0
    experiment.mkdir(parents=True, exist_ok=True)
    stage_code(code_dir)
    for local, path in uploads:
        command += ["--upload", local, path]
    command += ["--download", "/workspace/out", str(out), "--", "bash", "-lc", remote]
    code, previous = 1, None
    for cloud in args.clouds.split(","):
        if out.exists():                                   # a failed attempt's recovered artifacts
            aside = experiment / f"out.{previous}"
            n = 1
            while aside.exists():
                aside, n = experiment / f"out.{previous}.{n}", n + 1
            out.rename(aside)
        with open(experiment / f"lease_{cloud}.log", "w", encoding="utf-8", newline="\n") as log:
            code = subprocess.run(command, cwd=ROOT, env=dict(env, RUNPOD_CLOUD=cloud), stdout=log,
                                  stderr=subprocess.STDOUT).returncode
        print(f"{job} {cloud} lease exit {code}", flush=True)
        if code == 0:
            break
        previous = cloud
    print(f"{job} done ok={code == 0}")
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description="Train the readers on a bounded RunPod lease.")
    jobs = ap.add_subparsers(dest="job", required=True)
    full = jobs.add_parser("full", help="the full stage-2 model on a data directory")
    folds = jobs.add_parser("folds", help="cross-fold stage-2 models on a data directory")
    rescorer = jobs.add_parser("rescorer", help="re-scorers on candidate files")
    for parser, minutes in ((full, 200), (folds, 150), (rescorer, 180)):
        parser.add_argument("--experiment", required=True, help="the run's id: training/readers/data/experiments/<id>")
        parser.add_argument("--minutes", type=int, default=minutes, help="the lease's hard limit")
        parser.add_argument("--max-hourly-cost", type=float, default=1.0)
        parser.add_argument("--clouds", default="SECURE,COMMUNITY", help="tried in order until one succeeds")
        parser.add_argument("--gpus", help="comma-separated GPU types, most wanted first (default: by job)")
        parser.add_argument("--image", default=RECORDED_IMAGE, help="'' for runpod_api's pinned image")
        parser.add_argument("--cuda", default=RECORDED_CUDA, help="allowed CUDA versions; '' for runpod_api's")
        parser.add_argument("--dry-run", action="store_true", help="print the command and uploads; lease nothing")
    for parser, epochs, eval_from in ((full, 15, 8), (folds, 12, 9)):
        parser.add_argument("--data", required=True, help="a directory holding stage2.jsonl.gz (or stage2.jsonl)")
        parser.add_argument("--backbone", default=LARGE)
        parser.add_argument("--lr", default="1e-5")
        parser.add_argument("--epochs", type=int, default=epochs)
        parser.add_argument("--eval-from", type=int, default=eval_from)
    folds.add_argument("--folds", required=True, help="comma-separated fold numbers, e.g. 0,1")
    rescorer.add_argument("--job", dest="jobs", action="append", type=parse_job, required=True,
                          help="NAME=TRAIN.jsonl.gz=CAL.jsonl.gz[=large], repeatable")
    args = ap.parse_args(argv)
    if not NAME.fullmatch(args.experiment):
        ap.error("--experiment takes letters, digits, '.', '_' and '-'")
    if args.job == "rescorer":
        uploads = []
        for name, train, calibration, _ in args.jobs:
            uploads += [(train, f"/workspace/{name}_train.jsonl.gz"), (calibration, f"/workspace/{name}_cal.jsonl.gz")]
        gpus = LARGE_GPUS if any(size == "large" for *_, size in args.jobs) else BASE_GPUS
        return lease(args, "rescorer", rescorer_command(args.jobs), uploads, gpus)
    uploads = [(str(stage2_archive(args.data, make=not args.dry_run)), "/workspace/stage2.jsonl.gz")]
    if args.job == "full":
        remote = full_command(args.backbone, args.lr, args.epochs, args.eval_from)
    else:
        wanted = [int(i) for i in args.folds.split(",")]
        remote = folds_command(wanted, args.backbone, args.lr, args.epochs, args.eval_from)
    return lease(args, args.job, remote, uploads, LARGE_GPUS)


if __name__ == "__main__":
    raise SystemExit(main())
