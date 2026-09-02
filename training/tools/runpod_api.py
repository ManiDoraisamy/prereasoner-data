"""Cost-bounded RunPod client for Schema.org training.

The default ``lease`` command owns the complete pod lifecycle and terminates in
``finally``. A pod can outlive this process only when the operator explicitly
passes ``--keep``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REST = "https://rest.runpod.io/v1"
IMAGE = (
    "runpod/pytorch:1.2.0-rc.162-cu1281-torch2130-ubuntu2204"
    "@sha256:ed683f5f23a4b50f2738cddaccea02254bf8303dcbcc33485c95a31b76555422"
)
GPU_PRIORITY = [
    "NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000", "NVIDIA L4",
    "NVIDIA RTX A4000", "NVIDIA GeForce RTX 3090", "NVIDIA RTX A4500",
]
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
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
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


def _record(pid: str, max_minutes: int) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "pod_id": pid, "created_at": int(time.time()), "max_minutes": max_minutes,
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
    # provider-supplied, pod-scoped CLI before /start.sh instead. The caller's finally
    # block remains a second, independent deletion path.
    guard_seconds = max_minutes * 60
    delete_self = (
        f"sleep {guard_seconds}; "
        'runpodctl remove pod "$RUNPOD_POD_ID" || '
        'curl -fsS -X DELETE "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID" '
        '-H "Authorization: Bearer $RUNPOD_API_KEY"'
    )
    body = {
        "name": "prereasoner-train", "imageName": IMAGE, "cloudType": "SECURE",
        "gpuTypeIds": GPU_PRIORITY, "gpuCount": 1, "containerDiskInGb": 25,
        "volumeInGb": 0, "ports": ["22/tcp"],
        "allowedCudaVersions": ["12.8"],
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
    _record(pid, max_minutes)
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


def _run_transfer(command: list[str], remaining, attempts: int = 3) -> None:
    """Retry idempotent SCP uploads/downloads without ever retrying training."""
    last_error = None
    for attempt in range(attempts):
        try:
            subprocess.run(command, check=True, timeout=min(remaining(), 15 * 60))
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last_error or RuntimeError("transfer failed without an error")


def run_lease(max_minutes: int, command: list[str], keep: bool = False,
              uploads: tuple[tuple[str, str], ...] = (),
              downloads: tuple[tuple[str, str], ...] = ()) -> str:
    if not 1 <= max_minutes <= 360:
        raise ValueError("--max-minutes must be between 1 and 360")
    pid = create(max_minutes)
    try:
        ip, port = poll(pid)
        deadline = time.monotonic() + max_minutes * 60

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError(f"pod {pid} lease expired")
            return seconds

        ssh = [
            "ssh", "-i", str(Path.home() / ".ssh" / "runpod_prereasoner"),
            "-p", str(port), "-o", "StrictHostKeyChecking=accept-new", f"root@{ip}",
        ]
        scp = [
            "scp", "-i", str(Path.home() / ".ssh" / "runpod_prereasoner"),
            "-P", str(port), "-o", "StrictHostKeyChecking=accept-new", "-r",
        ]
        for local, remote in uploads:
            source = Path(local).resolve()
            if not source.exists():
                raise FileNotFoundError(f"upload source does not exist: {source}")
            _run_transfer(
                [*scp, str(source), f"root@{ip}:{_remote_path(remote)}"],
                remaining,
            )
        if command:
            subprocess.run([*ssh, shlex.join(command)], check=True, timeout=remaining())
        for remote, local in downloads:
            destination = Path(local).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            _run_transfer(
                [*scp, f"root@{ip}:{_remote_path(remote)}", str(destination)],
                remaining,
            )
        return pid
    finally:
        if keep:
            print(
                f"WARNING: --keep left billable pod {pid} running until its provider deadline; "
                "terminate it sooner when work completes",
                file=sys.stderr,
            )
        else:
            had_error = sys.exc_info()[0] is not None
            try:
                terminate(pid)
            except Exception as cleanup_error:
                # Preserve the training failure that triggered cleanup. The provider-side
                # pod-local deletion watchdog remains the final cost bound if all deletes fail.
                print(f"CRITICAL: pod {pid} cleanup failed: {cleanup_error}", file=sys.stderr)
                if not had_error:
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
        run_lease(
            args.max_minutes, args.remote_command, args.keep,
            tuple(map(tuple, args.upload)), tuple(map(tuple, args.download)),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
