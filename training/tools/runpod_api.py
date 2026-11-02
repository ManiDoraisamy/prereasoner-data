"""Cost-bounded RunPod client for the repository's GPU training: the Schema.org
property head (training/schema_org) and the own-data readers (training/readers,
whose ``training.readers.lease`` builds its jobs on this lease).

The default ``lease`` command owns the complete pod lifecycle and terminates in
``finally``. A pod can outlive this process only when the operator explicitly
passes ``--keep``, or when the remote command succeeded and anything stopped its
download (exit INCOMPLETE_EXIT): the pod holds the only copy of finished work. The
remote command runs detached on the pod and the lease polls its exit status, so
a dropped ssh connection does not end it (``_run_detached``).

Environment overrides, each leaving the default below when unset:
  RUNPOD_GPUS   comma-separated GPU types, most wanted first (replaces GPU_PRIORITY)
  RUNPOD_CLOUD  the pod's cloudType (default SECURE)
  RUNPOD_IMAGE  the runner image (default the digest-pinned IMAGE)
  RUNPOD_CUDA   comma-separated allowedCudaVersions (default 12.8)
  PREREASONER_RUNPOD_STATE  the lease state file, one per concurrent lease
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REST = "https://rest.runpod.io/v1"
IMAGE = os.environ.get("RUNPOD_IMAGE") or (
    "runpod/pytorch:1.2.0-rc.162-cu1281-torch2130-ubuntu2204"
    "@sha256:ed683f5f23a4b50f2738cddaccea02254bf8303dcbcc33485c95a31b76555422"
)
GPU_PRIORITY = [
    "NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000", "NVIDIA L4",
    "NVIDIA RTX A4000", "NVIDIA GeForce RTX 3090", "NVIDIA RTX A4500",
]
if os.environ.get("RUNPOD_GPUS"):                  # a job's own list, most wanted first
    GPU_PRIORITY = [name.strip() for name in os.environ["RUNPOD_GPUS"].split(",") if name.strip()]
CLOUD = os.environ.get("RUNPOD_CLOUD") or "SECURE"
CUDA_VERSIONS = [version.strip() for version in (os.environ.get("RUNPOD_CUDA") or "12.8").split(",")
                 if version.strip()]
STATE = Path(os.environ.get("PREREASONER_RUNPOD_STATE") or
             Path.home() / ".cache" / "prereasoner" / "runpod_active.json")


def credentials() -> tuple[str, str]:
    values = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value and not value.startswith("#") and "=" in value:
                key, item = value.split("=", 1)
                values[key.strip()] = item.strip().strip('"').strip("'")
    return (
        os.environ.get("RUNPOD_API_KEY") or values.get("RUNPOD_API_KEY", ""),
        os.environ.get("HF_TOKEN") or values.get("HF_TOKEN", ""),
    )


def rest(method, path, body=None, timeout=90, *, key=None):
    key = key or credentials()[0]
    request = urllib.request.Request(
        REST + path,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # RunPod's Cloudflare edge bot-blocks Python's default urllib agent with
            # HTTP 403 error 1010; any self-identifying agent passes (measured 2026-09-19).
            "User-Agent": "prereasoner-lease/1.0",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:1000]
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        # Transport errors are data, so the lifecycle owner can still clean up the pod.
        return -1, str(exc)


def pubkey() -> str:
    return (Path.home() / ".ssh" / "runpod_prereasoner.pub").read_text(encoding="utf-8").strip()


def ssh_endpoint(pod):
    pod = pod or {}
    for port in (pod.get("runtime") or {}).get("ports") or []:
        if str(port.get("privatePort")) == "22" and port.get("isIpPublic") and port.get("type") == "tcp":
            return port.get("ip"), port.get("publicPort")
    ip, mappings = pod.get("publicIp"), pod.get("portMappings") or {}
    return (ip, mappings.get("22")) if ip and mappings.get("22") else (None, None)


def _record(pid: str, max_minutes: int, token: str = "") -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "pod_id": pid, "created_at": int(time.time()), "max_minutes": max_minutes,
        "token": token,
    }, sort_keys=True) + "\n", encoding="utf-8")


def _clear(pid: str) -> None:
    if not STATE.is_file():
        return
    try:
        active = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        active = {}
    if active.get("pod_id") == pid:
        STATE.unlink(missing_ok=True)


def create(max_minutes: int) -> str:
    # RunPod REST does not accept the CLI-documented `terminateAfter` field. Arm the
    # provider-supplied, pod-scoped CLI before /start.sh instead. Current images use
    # ``pod delete``; the older spelling remains only as a compatibility fallback.
    # The caller's finally block is a second, independent deletion path.
    # Ownership token recorded BEFORE the POST: if the create response is lost, reconcile()
    # can identify OUR pod by name without ever touching other machines' leases.
    import uuid

    token = uuid.uuid4().hex[:12]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "pending_token": token, "created_at": int(time.time()),
        "max_minutes": max_minutes,
    }, sort_keys=True) + "\n", encoding="utf-8")
    guard_seconds = max_minutes * 60
    delete_self = (
        f"sleep {guard_seconds}; "
        'runpodctl pod delete "$RUNPOD_POD_ID" || '
        'runpodctl remove pod "$RUNPOD_POD_ID" || '
        'curl -fsS -X DELETE "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID" '
        '-H "Authorization: Bearer $RUNPOD_API_KEY"'
    )
    body = {
        "name": f"prereasoner-train-{token}", "imageName": IMAGE, "cloudType": CLOUD,
        "gpuTypeIds": GPU_PRIORITY, "gpuCount": 1, "containerDiskInGb": 25,
        "volumeInGb": 0, "ports": ["22/tcp"],
        "allowedCudaVersions": CUDA_VERSIONS,
        "dockerStartCmd": [
            "bash", "-lc",
            f"({delete_self}) >/tmp/prereasoner-lease-guard.log 2>&1 & exec /start.sh",
        ],
        "env": {
            "PUBLIC_KEY": pubkey(),
            "PREREASONER_TRAINING_IMAGE": IMAGE,
        },
    }
    status, response = rest("POST", "/pods", body)
    if status not in (200, 201) or not isinstance(response, dict) or not response.get("id"):
        raise RuntimeError(f"RunPod create failed: HTTP {status} {response}")
    pid = response["id"]
    _record(pid, max_minutes, token)
    print(f"created pod {pid}; lease limit {max_minutes} minutes", flush=True)
    return pid


def terminate(pid: str, attempts: int = 3) -> None:
    """Terminate a pod with bounded retries; clear local state only on success."""
    last = None
    for attempt in range(attempts):
        status, response = rest("DELETE", f"/pods/{pid}")
        if status in (200, 202, 204, 404):
            _clear(pid)
            print(f"terminated pod {pid}", flush=True)
            return
        last = (status, response)
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    status, response = last or (-1, "no termination attempt")
    raise RuntimeError(f"RunPod termination failed for {pid}: HTTP {status} {response}")


def poll(pid: str, timeout_minutes=8):
    deadline = time.monotonic() + timeout_minutes * 60
    while time.monotonic() < deadline:
        status, pod = rest("GET", f"/pods/{pid}")
        if status == 200 and isinstance(pod, dict):
            ip, port = ssh_endpoint(pod)
            print(f"status={pod.get('desiredStatus')} ssh={f'{ip}:{port}' if ip else 'pending'}", flush=True)
            if pod.get("desiredStatus") == "RUNNING" and ip:
                return ip, port
        else:
            print(f"pod status failed: HTTP {status} {pod}", flush=True)
        time.sleep(15)
    raise TimeoutError(f"pod {pid} did not expose SSH within {timeout_minutes} minutes")


def _remote_path(value: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", value) or ".." in value.split("/"):
        raise ValueError(f"unsafe remote path: {value!r}")
    return value


TRANSFER_FLOOR_SECONDS = 15 * 60
# The slowest link a transfer is still waited for. A flat 15 minutes could never bring back a 730 MB small.pt from a
# host moving about 80 KB/s: 61 to 74 MB arrived, every attempt, and the pod went with the rest (2026-11-01).
TRANSFER_BYTES_PER_SECOND = 50 * 1024


def _transfer_seconds(size: int) -> float:
    """How long a transfer of ``size`` bytes may take."""
    return max(TRANSFER_FLOOR_SECONDS, size / TRANSFER_BYTES_PER_SECOND)


def _run_transfer(command: list[str], remaining, attempts: int = 3, size: int = 0) -> None:
    """Retry idempotent SCP uploads/downloads without ever retrying training. ``size``, the bytes moved, sets each
    attempt's time limit; the lease's own deadline still bounds it."""
    last_error = None
    for attempt in range(attempts):
        try:
            subprocess.run(command, check=True, timeout=min(remaining(), _transfer_seconds(size)))
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last_error or RuntimeError("transfer failed without an error")


INCOMPLETE_EXIT = 5        # the lease command's exit code when the command succeeded and its download did not


class IncompleteDownload(RuntimeError):
    """The remote command succeeded and its files did not all arrive: a file is still shorter or longer than the
    pod's after re-fetching it, or something stopped the download. The lease keeps the pod (its provider-side watchdog
    still deletes it at the deadline) so the files can be pulled another way, since a terminated pod takes them with
    it; the ``lease`` command exits with INCOMPLETE_EXIT."""


def _upload(ssh: list[str], scp: list[str], ip: str, source: Path, remote: str, remaining) -> None:
    """``source`` onto the pod at ``remote``, a folder file by file onto the same paths. ``scp -r`` into a folder that
    exists nests inside it, so a retried or resumed upload left its copy one level down and the old code in place."""
    target = _remote_path(remote)
    if source.is_file():
        _run_transfer([*scp, str(source), f"root@{ip}:{target}"], remaining, size=source.stat().st_size)
        return
    files = {path.relative_to(source).as_posix(): path for path in source.rglob("*") if path.is_file()}
    folders = {target, *(f"{target}/{Path(relative).parent.as_posix()}" for relative in files if "/" in relative)}
    _run_transfer([*ssh, shlex.join(["mkdir", "-p", "--", *sorted(folders)])], remaining)
    for relative, path in sorted(files.items()):
        _run_transfer([*scp, str(path), f"root@{ip}:{target}/{relative}"], remaining, size=path.stat().st_size)


def _remote_sizes(ssh: list[str], remote: str, remaining) -> dict[str, int]:
    """{path under ``remote`` ("" when ``remote`` is a file): size in bytes} of every file the pod holds there."""
    listing = subprocess.run(
        [*ssh, shlex.join(["find", _remote_path(remote), "-type", "f", "-printf", "%P\\t%s\\n"])],
        check=True, capture_output=True, text=True, timeout=min(remaining(), 120),
    ).stdout
    sizes = {}
    for line in listing.splitlines():
        relative, _, size = line.rpartition("\t")
        if size.isdigit():
            sizes[relative] = int(size)
    return sizes


# The outputs compressed on the pod before they travel: held-out outputs and checkpoints of at least this size.
COMPRESSED_SUFFIXES = (".jsonl", ".pt")
COMPRESS_FROM_BYTES = 1024 * 1024
COMPRESS_BYTES_PER_SECOND = 8 * 1024 * 1024     # what gzip -1 is given to finish, on a pod that may be busy


def _outputs(listing: dict[str, int]) -> dict[str, int]:
    """The pod's files without what ``_compress_outputs`` adds beside them, from this download or an earlier one."""
    return {relative: size for relative, size in listing.items()
            if not relative.endswith(".gz.part") and not (relative.endswith(".gz") and relative[:-3] in listing)}


def _compress_outputs(ssh: list[str], root: str, sizes: dict[str, int], remaining) -> dict[str, int]:
    """Gzip each large output beside itself on the pod; returns {path under ``root``: bytes of its gzip} for those it
    made smaller. The originals stay, for pull_parts.sh, and a gzip appears under its name only whole. A gzip that fails
    or does not finish in time only means that file travels as it is."""
    large = [relative for relative, size in sizes.items()
             if relative.endswith(COMPRESSED_SUFFIXES) and size >= COMPRESS_FROM_BYTES]
    if not large:
        return {}
    steps = []
    for relative in large:
        path, gz, part = (shlex.quote(f"{root}/{relative}{suffix}") for suffix in ("", ".gz", ".gz.part"))
        steps.append(f"rm -f -- {gz}; gzip -1 -c -- {path} > {part} && mv -f -- {part} {gz} || rm -f -- {part}")
    script = "; ".join(steps)
    seconds = sum(sizes[relative] for relative in large) / COMPRESS_BYTES_PER_SECOND
    try:
        subprocess.run([*ssh, script], check=True, timeout=min(remaining(), max(120, seconds)))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"compressing on the pod stopped ({type(exc).__name__}); the rest travels as it is", flush=True)
    listing = _remote_sizes(ssh, root, remaining)
    return {relative: listing[f"{relative}.gz"] for relative in large
            if listing.get(f"{relative}.gz", sizes[relative]) < sizes[relative]}


def _fetch(scp: list[str], ip: str, source: str, local: Path, size: int, packed: int | None, remaining) -> None:
    """One try at bringing ``source`` to ``local``, by way of its gzip when it has one (``packed``, its bytes). The
    restored file appears only whole, and a gzip that does not read back (scp has returned success with a third of a
    file) is removed, so the next try fetches it again."""
    if packed is None:
        _run_transfer([*scp, f"root@{ip}:{source}", str(local)], remaining, attempts=1, size=size)
        return
    carrier, restoring = local.with_name(f"{local.name}.gz"), local.with_name(f"{local.name}.part")
    _run_transfer([*scp, f"root@{ip}:{source}.gz", str(carrier)], remaining, attempts=1, size=packed)
    try:
        with gzip.open(carrier, "rb") as plain, open(restoring, "wb") as sink:
            shutil.copyfileobj(plain, sink, 1024 * 1024)
    except (OSError, EOFError, zlib.error):
        carrier.unlink(missing_ok=True)
        restoring.unlink(missing_ok=True)
        raise
    os.replace(restoring, local)
    carrier.unlink()


def _download(ssh: list[str], scp: list[str], ip: str, remote: str, destination: Path, remaining, fresh: bool) -> None:
    """Bring back every file the pod holds under ``remote``, smallest first, each onto its own path, so a retry lands
    where the first try did: ``scp -r`` into a folder a failed try had made nested inside it, leaving out/out beside a
    shorter copy (2026-11-01). Each file is compared with the pod's size and fetched again when it differs, up to three
    times: scp has returned success with a third of a file (2026-10-21, a stage-2 run's small.pt, lost with its pod).
    ``fresh``: this lease's command made the pod's files, so a local file of the same size from before is not one of
    them. Otherwise a file already whole is not fetched again, which is how a kept pod's download finishes."""
    root = _remote_path(remote)
    sizes = _outputs(_remote_sizes(ssh, root, remaining))
    packed = _compress_outputs(ssh, root, sizes, remaining)
    short = []
    for relative, size in sorted(sizes.items(), key=lambda item: (item[1], item[0])):
        local = destination / relative if relative else destination
        source = f"{root}/{relative}" if relative else root
        local.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            local.unlink(missing_ok=True)
        for _ in range(3):
            if local.is_file() and local.stat().st_size == size:
                break
            try:
                _fetch(scp, ip, source, local, size, packed.get(relative), remaining)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, EOFError, zlib.error) as exc:
                print(f"download of {source} stopped: {type(exc).__name__}", flush=True)
        if not (local.is_file() and local.stat().st_size == size):
            short.append(f"{source} ({local.stat().st_size if local.is_file() else 0} of {size} bytes)")
    if short:
        raise IncompleteDownload("downloads differ from the pod's files: " + ", ".join(short))


EXIT_FILE, OUTPUT_FILE = "/tmp/prereasoner-lease.exit", "/tmp/prereasoner-lease.out"
PID_FILE = "/tmp/prereasoner-lease.pid"
POLL_SECONDS, POLL_FAILURES = 30, 20        # a poll every 30 s; 20 failed polls in a row (10 minutes) end the lease
# The poll's answer: the exit status once written, else "running" while the detached shell lives (its pid still runs
# a command line naming EXIT_FILE, so a pid reused after a restart does not count), else "lost".
POLL = (f"cat {EXIT_FILE} 2>/dev/null || {{ p=$(cat {PID_FILE} 2>/dev/null); "
        f"grep -qs {EXIT_FILE} /proc/$p/cmdline && echo running || echo lost; }}")


class CommandLost(RuntimeError):
    """The detached command is gone without an exit status: the pod's container restarted (a community host did,
    2026-10-22, killing a stage-2 run at its 1,300th step) or the process was killed."""


def _run_detached(ssh: list[str], command: list[str], remaining) -> None:
    """Run ``command`` on the pod detached from the ssh session (setsid, nohup), then poll for its exit status. A
    dropped connection no longer ends it: a community host dropped ssh mid-training and four stage-2 runs died with
    their sessions (2026-10-21 and 22). The command's output is printed when it ends; a nonzero exit raises
    CalledProcessError, a command gone without one raises CommandLost, and POLL_FAILURES failed polls in a row raise
    the last ssh error."""
    script = f"{shlex.join(command)}; echo $? > {EXIT_FILE}"
    launch = (f"rm -f {EXIT_FILE}; nohup setsid bash -c {shlex.quote(script)} > {OUTPUT_FILE} 2>&1 < /dev/null & "
              f"echo $! > {PID_FILE}")
    subprocess.run([*ssh, launch], check=True, timeout=min(remaining(), 120))
    failures, status = 0, "running"
    while status == "running":
        time.sleep(min(POLL_SECONDS, remaining()))
        try:
            status = subprocess.run([*ssh, POLL], check=True, capture_output=True, text=True,
                                    timeout=min(remaining(), 60)).stdout.strip()
            failures = 0
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            failures += 1
            if failures >= POLL_FAILURES:
                raise
    try:
        print(subprocess.run([*ssh, f"cat {OUTPUT_FILE}"], capture_output=True, text=True,
                             timeout=min(remaining(), 120)).stdout, end="", flush=True)
    except subprocess.TimeoutExpired:
        pass
    if not status.isdigit():
        raise CommandLost(f"the remote command is gone without an exit status ({status or 'no answer'})")
    if int(status) != 0:
        raise subprocess.CalledProcessError(int(status), command)


def reconcile() -> None:
    """Terminate only pods THIS machine's lease record owns and lost track of.

    Two owned failure shapes: (a) creation succeeded remotely but the POST response or
    state write was lost — identified by OUR pre-recorded ownership token in the pod
    name; (b) a recorded pod outlived its lease deadline despite the watchdog and the
    lifecycle cleanup. Pods created by other machines or concurrent leases are never
    touched: a shared display prefix is not ownership evidence."""
    try:
        active = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    deadline = active.get("created_at", 0) + active.get("max_minutes", 0) * 60 + 300
    if active.get("pod_id"):
        if time.time() > deadline:
            print(f"reconcile: terminating our expired pod {active['pod_id']}",
                  file=sys.stderr)
            terminate(active["pod_id"])
        return
    token = active.get("pending_token")
    if not token:
        return
    status, response = rest("GET", "/pods")
    if status != 200 or not isinstance(response, list):
        return
    for pod in response:
        if pod.get("name") == f"prereasoner-train-{token}":
            print(f"reconcile: terminating our lost-creation pod {pod['id']}",
                  file=sys.stderr)
            terminate(pod["id"])
    STATE.unlink(missing_ok=True)


RECOVERY_SECONDS = 30 * 60      # what a failed command's partial artifacts may take, smallest first
# What a download leaves of the lease (half of what remains, when that is less) to pull the files another way: a pod kept
# with the deadline at hand goes to its watchdog with them.
PULL_RESERVE_SECONDS = 30 * 60


def _within(remaining, seconds: float):
    """``remaining`` that also runs out ``seconds`` from now."""
    end = time.monotonic() + seconds

    def left() -> float:
        allowed = min(remaining(), end - time.monotonic())
        if allowed <= 0:
            raise TimeoutError("the time set for this transfer is over")
        return allowed
    return left


def run_lease(max_minutes: int, command: list[str], keep: bool = False,
              uploads: tuple[tuple[str, str], ...] = (),
              downloads: tuple[tuple[str, str], ...] = (),
              max_hourly_cost: float | None = None, resume: bool = False) -> str:
    if not 1 <= max_minutes <= 360:
        raise ValueError("--max-minutes must be between 1 and 360")
    if max_hourly_cost is not None and (not math.isfinite(max_hourly_cost) or max_hourly_cost <= 0):
        raise ValueError("max hourly cost must be finite and positive")
    reconcile()
    if resume:
        active = json.loads(STATE.read_text(encoding="utf-8"))
        pid, token = active.get("pod_id"), active.get("token")
        status, details = rest("GET", f"/pods/{pid}") if pid else (0, None)
        if (not token or status != 200 or not isinstance(details, dict)
                or details.get("name") != f"prereasoner-train-{token}"):
            raise RuntimeError("cannot resume: pod ownership is not verified")
        seconds_left = active["created_at"] + active["max_minutes"] * 60 - time.time()
        if seconds_left <= 0:
            terminate(pid)
            raise TimeoutError("owned pod's original lease deadline has expired")
        seconds_left = min(seconds_left, max_minutes * 60)
        print(f"resuming owned pod {pid}; {seconds_left / 60:.1f} minutes remain; deadline not extended", flush=True)
    else:
        pid = create(max_minutes)
        seconds_left = max_minutes * 60
    deadline = time.monotonic() + seconds_left
    scp, ip = None, None
    finished = False       # the remote command, if any, succeeded: the pod now holds the only copy of what it made
    failed = True
    try:
        if max_hourly_cost is not None:
            status, details = rest("GET", f"/pods/{pid}")
            price = details.get("costPerHr") if isinstance(details, dict) else None
            if (status != 200 or type(price) not in (int, float)
                    or not math.isfinite(price) or not 0 <= price <= max_hourly_cost):
                raise RuntimeError("pod hourly price unavailable or exceeds the approved cap")
            print(f"pod hourly cost=${price}; maximum lease compute=${price * max_minutes / 60:.2f}", flush=True)
        ip, port = poll(pid)
        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError(f"pod {pid} lease expired")
            return seconds

        ssh = [
            "ssh", "-i", str(Path.home() / ".ssh" / "runpod_prereasoner"),
            "-p", str(port), "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6", f"root@{ip}",
        ]
        # A dead connection ends an attempt here too, instead of waiting out a limit that can now be hours.
        scp = [
            "scp", "-i", str(Path.home() / ".ssh" / "runpod_prereasoner"),
            "-P", str(port), "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6",
        ]
        for local, remote in uploads:
            source = Path(local).resolve()
            if not source.exists():
                raise FileNotFoundError(f"upload source does not exist: {source}")
            _upload(ssh, scp, ip, source, remote, remaining)
        if command:
            _run_detached(ssh, command, remaining)
        finished = True
        if downloads:
            seconds = remaining()
            downloading = _within(remaining, seconds - min(PULL_RESERVE_SECONDS, seconds / 2))
            for remote, local in downloads:
                _download(ssh, scp, ip, remote, Path(local).resolve(), downloading, fresh=bool(command))
        failed = False
        return pid
    except BaseException as exc:
        # Whatever stopped the download, the pod keeps the finished work: a TimeoutExpired here once terminated the pod
        # holding a reader's weights and four folds' outputs, after hours of paid training (2026-11-01).
        if finished and not isinstance(exc, IncompleteDownload):
            raise IncompleteDownload(f"the download stopped: {type(exc).__name__}: {exc}") from exc
        raise
    finally:
        # A failed command's partial artifacts come back before its pod is deleted. Best effort, within a bound: a dead
        # pod or provider deadline can still prevent recovery, and a failed run does not hold a pod for hours.
        if failed and not finished and scp is not None and ip:
            recovery = _within(remaining, RECOVERY_SECONDS)
            for remote, local in downloads:
                try:
                    _download(ssh, scp, ip, remote, Path(local).resolve(), recovery, fresh=bool(command))
                except Exception as recovery_error:
                    print(f"artifact recovery failed: {remote}: {recovery_error}", file=sys.stderr)
        incomplete = failed and finished
        if keep or incomplete:
            print(
                f"WARNING: {'an incomplete download' if incomplete else '--keep'} left billable pod {pid} running "
                f"until its provider deadline (ssh root@{ip}); pull its files (training/readers/pull_parts.sh for a "
                "stage-2 run, or this lease again with --resume and no command), then terminate it",
                file=sys.stderr,
            )
        else:
            try:
                terminate(pid)
            except Exception as cleanup_error:
                # Preserve the training failure that triggered cleanup. The provider-side
                # pod-local deletion watchdog remains the final cost bound if all deletes fail.
                print(f"CRITICAL: pod {pid} cleanup failed: {cleanup_error}", file=sys.stderr)
                if not failed:
                    raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RunPod training with an owned, time-bounded lease")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    status = commands.add_parser("status"); status.add_argument("pod_id")
    term = commands.add_parser("term"); term.add_argument("pod_id")
    lease = commands.add_parser("lease")
    lease.add_argument("--max-minutes", type=int, default=120)
    lease.add_argument("--keep", action="store_true")
    lease.add_argument("--max-hourly-cost", type=float, default=None)
    lease.add_argument("--resume", action="store_true", help="resume only this state file's owned pod within its original deadline")
    lease.add_argument("--upload", action="append", nargs=2, default=[],
                       metavar=("LOCAL", "REMOTE"))
    lease.add_argument("--download", action="append", nargs=2, default=[],
                       metavar=("REMOTE", "LOCAL"))
    lease.add_argument("remote_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if not credentials()[0]:
        parser.error("RUNPOD_API_KEY must be set in the environment or repo-root .env")
    if args.command == "check":
        status_code, pods = rest("GET", "/pods")
        print(f"HTTP {status_code}; pods={len(pods) if isinstance(pods, list) else pods}")
    elif args.command == "status":
        status_code, pod = rest("GET", f"/pods/{args.pod_id}")
        ip, port = ssh_endpoint(pod) if isinstance(pod, dict) else (None, None)
        print(f"HTTP {status_code} status={pod.get('desiredStatus') if isinstance(pod, dict) else pod} ssh={ip}:{port}")
    elif args.command == "term":
        terminate(args.pod_id)
    else:
        try:
            run_lease(
                args.max_minutes,
                # argparse REMAINDER keeps an explicit "--" separator; the remote shell must
                # never see it (a pod once executed literally "--" and burned its lease).
                args.remote_command[1:] if args.remote_command[:1] == ["--"]
                else args.remote_command,
                args.keep,
                tuple(map(tuple, args.upload)), tuple(map(tuple, args.download)),
                args.max_hourly_cost,
                args.resume,
            )
        except IncompleteDownload as exc:
            print(f"incomplete download: {exc}", file=sys.stderr)
            return INCOMPLETE_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
