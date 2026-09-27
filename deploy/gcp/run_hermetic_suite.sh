#!/bin/sh
set -eu

# The pinned runtime image carries the serving stack and frozen model bundle. Overlay only
# the allowlisted source archive, retaining those ignored runtime artifacts in /app/engine/data.
cp -a /workspace/. /app/

# CI-only tools are installed in the disposable hermetic container, never in the release image.
# The live-product lane leaves the production Python environment unchanged and adds only Git.
apt-get update
apt-get install -y --no-install-recommends git
rm -rf /var/lib/apt/lists/*
if [ "${INSTALL_CI_REQUIREMENTS:-1}" = "1" ]; then
  python -m pip install --require-hashes -r /app/requirements-ci.lock.txt
fi

# git archive intentionally has no history. Build a synthetic local commit from the attested
# file inventory so provenance tests exercise exact source blobs without uploading .git history.
python - <<'PY'
import json
import subprocess

root = "/app"
inventory = json.load(open(f"{root}/tests/build_provenance.json", encoding="utf-8"))["source_files"]
subprocess.run(["git", "init", "-q", root], check=True)
subprocess.run(["git", "-C", root, "add", "-f", "--", *inventory], check=True)
subprocess.run([
    "git", "-C", root, "-c", "user.name=Hermetic Suite",
    "-c", "user.email=suite@example.invalid", "commit", "-qm",
    "attested source snapshot",
], check=True)
PY

if [ "${RUN_WORLD_REGRESSION:-0}" = "1" ]; then
  python - <<'PY'
import os
import subprocess
import sys

timeout = int(os.environ.get("LIVE_REGRESSION_TIMEOUT_SECONDS", "900"))
try:
    subprocess.run(
        [sys.executable, "-m", "regress.run_regression", "--require-world", "--skip-world-subtests"],
        cwd="/app", timeout=timeout, check=True,
    )
except subprocess.TimeoutExpired:
    raise SystemExit(f"world regression exceeded {timeout}s")
PY
fi

exec python -m tests.run_all
