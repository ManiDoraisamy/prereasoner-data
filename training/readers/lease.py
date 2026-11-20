"""The one GPU lease loop: a remote command run through training/tools/runpod_api.py's owned, time-bounded lease, first on
the SECURE cloud, then on COMMUNITY when that attempt fails (no instance, no CUDA, or a failed training), with the
pod-side code a training package names staged beside it. A command that succeeded but whose download broke
(runpod_api's INCOMPLETE_EXIT) is never run again elsewhere: its pod is kept with the files, which the lease run again
with --resume pulls. A lease also refuses to start for an experiment whose out/ holds a finished run
(a best.json): that run is pulled or moved aside by hand, never trained again.

The pod's /workspace/out comes back to <experiments>/<id>/out (a failed attempt's recovered out/ is moved aside to
out.<cloud>, and one an earlier lease left to out.earlier, so the next attempt's files never mix with them); each
attempt's log is <id>/lease_<cloud>.log and the code that ran is <id>/code. The lease's state file is
~/.cache/prereasoner/<state>_<id>.json, so parallel leases never share one. training/named/lease.py builds its jobs on
it; the readers' jobs went with the readers (DECISIONS.md, 2026-11-20).
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from training.readers import EXPERIMENTS_DIR
from training.tools.runpod_api import INCOMPLETE_EXIT

ROOT = Path(__file__).resolve().parents[2]
# XLM-R large trains with checkpointing on 24 GB or more; slower or smaller cards would outrun the lease. The RTX 5090
# is left out: the recorded image's torch 2.4 has no kernels for it ("CUDA kernel errors", 2026-10-22).
LARGE_GPUS = ["NVIDIA GeForce RTX 4090", "NVIDIA L40S", "NVIDIA RTX 6000 Ada Generation", "NVIDIA RTX A6000",
              "NVIDIA A40"]
# The image and CUDA versions the recorded runs leased (their logs report torch 2.4.1+cu124). An empty --image or
# --cuda leaves runpod_api's digest-pinned default.
RECORDED_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
RECORDED_CUDA = "12.4,12.5,12.6,12.7,12.8,12.9"
PIP = "pip install -q 'transformers>=4.44,<5' 2>&1 | tail -1"
# The check runs a kernel: a card the image's torch has no kernels for reports CUDA available and fails mid-run
# (an RTX 5090, 2026-10-22), and a host with a broken driver fails here ("CUDA unknown error").
CUDA_CHECK = ("python -c 'import sys, torch; ok = torch.cuda.is_available() "
              "and float((torch.ones(8, 8, device=\"cuda\") @ torch.ones(8, 8, device=\"cuda\")).sum()) == 512.0; "
              "print(torch.__version__, ok and torch.cuda.get_device_name(0)); sys.exit(0 if ok else 3)' || exit 3")
ALLOC = "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"
NAME = re.compile(r"[A-Za-z0-9._-]+")


def stage_code(directory, code):
    """The pod-side modules copied into ``directory`` as their packages (uploaded as /workspace/engine and
    /workspace/training, so the pod runs them with python -m from /workspace)."""
    if directory.exists():
        shutil.rmtree(directory)
    for relative in code:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)


def lease(args, job, remote, uploads, gpus, *, code, experiments=None, state="runpod_readers"):
    """Run the lease on each cloud in turn until one succeeds; the exit code of the last attempt. ``code``: the
    repository files the pod imports; the keywords are what a training package (training/named/lease.py) brings to
    this one lease loop."""
    experiment = (experiments or EXPERIMENTS_DIR) / args.experiment
    out = experiment / "out"
    code_dir = experiment / "code"
    uploads = [(str(code_dir / "engine"), "/workspace/engine"),
               (str(code_dir / "training"), "/workspace/training")] + uploads
    command = [sys.executable, "-m", "training.tools.runpod_api", "lease", "--max-minutes", str(args.minutes),
               "--max-hourly-cost", str(args.max_hourly_cost)] + (["--resume"] if args.resume else [])
    env = dict(os.environ, RUNPOD_GPUS=",".join(args.gpus.split(",") if args.gpus else gpus),
               PREREASONER_RUNPOD_STATE=str(Path.home() / ".cache" / "prereasoner"
                                            / f"{state}_{args.experiment}.json"))
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
    if out.exists() and any(out.rglob("best.json")):
        print(f"{job} refused: {out} holds a finished run; pull or move it aside, never train it again")
        return 2
    experiment.mkdir(parents=True, exist_ok=True)
    stage_code(code_dir, code)
    for local, path in uploads:
        command += ["--upload", local, path]
    command += ["--download", "/workspace/out", str(out), "--", "bash", "-lc", remote]
    code, previous = 1, "earlier"                          # before this lease's first attempt, an earlier lease's
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
        if code == INCOMPLETE_EXIT:                        # trained; the pod is kept for --resume
            print(f"{job} kept its pod: see {experiment / f'lease_{cloud}.log'}", flush=True)
            break
        previous = cloud
    print(f"{job} done ok={code == 0}")
    return code
