"""
Minimal RunPod REST client (stdlib only) for driving training on a GPU pod. Reads
RUNPOD_API_KEY / HF_TOKEN from repo-root .env. NEVER prints the key.

CLI:
  python -m training.tools.runpod_api check                 # auth + existing pods
  python -m training.tools.runpod_api create                # create GPU pod, poll, print ssh
  python -m training.tools.runpod_api status <id>           # pod status + ssh endpoint
  python -m training.tools.runpod_api term <id>             # terminate pod
"""
from __future__ import annotations
import json, sys, time, urllib.request, urllib.error
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
REST = "https://rest.runpod.io/v1"
IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
GPU_PRIORITY = ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A5000", "NVIDIA L4",
                "NVIDIA RTX A4000", "NVIDIA GeForce RTX 3090", "NVIDIA RTX A4500"]


def env():
    out = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


_ENV = env()
KEY = _ENV.get("RUNPOD_API_KEY", "")
HF = _ENV.get("HF_TOKEN", "")


def rest(method, path, body=None, timeout=90):
    req = urllib.request.Request(REST + path, method=method,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:1000]
    except Exception as e:
        return -1, str(e)


def pubkey():
    return (Path.home() / ".ssh" / "runpod_prereasoner.pub").read_text(encoding="utf-8").strip()


def ssh_endpoint(pod):
    """-> (ip, port) for the public 22/tcp mapping, else (None, None). Handles BOTH REST shapes:
    runtime.ports[] (older) and top-level publicIp + portMappings{"22":port} (current — the older
    parser missed this and reported ssh=pending forever even though the pod was reachable)."""
    pod = pod or {}
    rt = pod.get("runtime") or {}
    for p in rt.get("ports") or []:
        if str(p.get("privatePort")) == "22" and p.get("isIpPublic") and p.get("type") == "tcp":
            return p.get("ip"), p.get("publicPort")
    ip, pm = pod.get("publicIp"), pod.get("portMappings") or {}
    if ip and pm.get("22"):
        return ip, pm.get("22")
    return None, None


def create():
    body = {"name": "prereasoner-train", "imageName": IMAGE, "cloudType": "SECURE",
            "gpuTypeIds": GPU_PRIORITY, "gpuCount": 1,
            "containerDiskInGb": 25, "volumeInGb": 0, "ports": ["22/tcp"],
            "env": {"PUBLIC_KEY": pubkey(), "HF_TOKEN": HF}}
    st, resp = rest("POST", "/pods", body)
    print(f"POST /v1/pods -> HTTP {st}")
    if st not in (200, 201):
        print("  body:", resp); return None
    pid = resp.get("id") if isinstance(resp, dict) else None
    print(f"  created pod {pid}")
    (ROOT / "training/data").mkdir(parents=True, exist_ok=True)
    (ROOT / "training/data/pod_id.txt").write_text(pid or "")
    return pid


def poll(pid, mins=8):
    deadline = mins * 60
    waited = 0
    while waited < deadline:
        st, pod = rest("GET", f"/pods/{pid}")
        if st == 200 and isinstance(pod, dict):
            status = pod.get("desiredStatus")
            ip, port = ssh_endpoint(pod)
            print(f"  [{waited:3d}s] status={status} ssh={'%s:%s' % (ip, port) if ip else 'pending'}")
            if status == "RUNNING" and ip:
                return ip, port
        else:
            print(f"  [{waited:3d}s] GET -> {st} {pod if st!=200 else ''}")
        time.sleep(15); waited += 15
    return None, None


if __name__ == "__main__":
    if not KEY:
        print("ERROR: RUNPOD_API_KEY not in .env"); sys.exit(2)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        st, pods = rest("GET", "/pods")
        print(f"GET /v1/pods -> {st}; pods={len(pods) if isinstance(pods,list) else pods}")
    elif cmd == "create":
        pid = create()
        if pid:
            ip, port = poll(pid)
            if ip:
                print(f"\nSSH READY: ssh -i ~/.ssh/runpod_prereasoner -p {port} root@{ip}")
            else:
                print("\npod did not reach RUNNING+public-ip in time; check status")
    elif cmd == "status":
        st, pod = rest("GET", f"/pods/{sys.argv[2]}")
        ip, port = ssh_endpoint(pod) if isinstance(pod, dict) else (None, None)
        print(f"HTTP {st} status={pod.get('desiredStatus') if isinstance(pod,dict) else pod} ssh={ip}:{port}")
    elif cmd == "term":
        st, r = rest("DELETE", f"/pods/{sys.argv[2]}")
        print(f"DELETE -> {st} {r}")
