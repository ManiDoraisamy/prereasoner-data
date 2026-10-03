#!/bin/bash
set -Eeuo pipefail

# Live product checks against an isolated, disposable PostgreSQL instance. This runner must never
# connect to Cloud SQL: the only database is a local Docker container on a private build network.
readonly image="${BASE_IMAGE:?BASE_IMAGE is required}"
readonly pg_image="${PGVECTOR_IMAGE:?PGVECTOR_IMAGE is required}"
readonly seed_uri="${COMMUNITY_SEED_URI:-https://storage.googleapis.com/prereasoner-community-artifacts/community-seed-v4.dump}"
readonly seed_sha256="${COMMUNITY_SEED_SHA256:-2c39e749e2ae87654cca80881cdec4de924e131b1f8199179f6c7ceef2d8840a}"
readonly db_password="product-ci-only-not-a-secret"
readonly role_password="serving-ci-only-not-a-secret"
readonly suffix="${BUILD_ID:-local}-$$"
readonly network="prereasoner-product-${suffix}"
readonly volume="prereasoner-product-db-${suffix}"
readonly node_volume="prereasoner-product-node-${suffix}"
readonly db_name="prereasoner-product-db-${suffix}"
readonly server_name="prereasoner-product-server-${suffix}"

cleanup() {
  docker rm -f "$server_name" "$db_name" >/dev/null 2>&1 || true
  docker volume rm "$volume" "$node_volume" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$network" >/dev/null
docker volume create "$volume" >/dev/null
docker volume create "$node_volume" >/dev/null

# Excel parsing tests use the same pinned Node runtime as the hermetic suite.
docker run --rm --volume "$node_volume:/node" node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c \
  sh -ceu 'cp "$(command -v node)" /node/node; chmod 0555 /node/node'

# The focused offline planner gate is not a substitute for the complete hermetic
# source suite. Run it on the candidate's Python/model image, adding CI-only tools
# in this disposable container; the shipped runtime stays unchanged.
docker run --rm --cpus=8 --memory=16g \
  --volume "$node_volume:/opt/node:ro" --volume /workspace:/workspace:ro \
  --workdir /app \
  --env PATH=/opt/node:/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
  --env RUN_ENGINE_TESTS=0 --env RUN_ORCHESTRATOR_TESTS=0 \
  --env INSTALL_CI_REQUIREMENTS=1 --env EXTERNAL_LLM_ENABLED=false \
  --entrypoint /bin/sh "$image" /workspace/deploy/gcp/run_hermetic_suite.sh

docker run -d --name "$db_name" --network "$network" --network-alias product-db \
  --cpus=4 --memory=5g --memory-swap=5g --shm-size=3g \
  --volume "$volume:/var/lib/postgresql/data" \
  --volume /workspace/db/init.sql:/docker-entrypoint-initdb.d/10-init.sql:ro \
  --env POSTGRES_DB=world --env POSTGRES_USER=postgres --env "POSTGRES_PASSWORD=$db_password" \
  "$pg_image" >/dev/null

ready=0
for attempt in $(seq 1 180); do
  if docker exec "$db_name" pg_isready -U postgres -d world >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  docker logs "$db_name"
  echo "disposable PostgreSQL did not become ready" >&2
  exit 1
fi

# The serving role is intentionally non-superuser, matching the production connection model.
docker exec "$db_name" psql -v ON_ERROR_STOP=1 -U postgres -d world \
  -c "CREATE ROLE serving LOGIN PASSWORD '$role_password'"

# Reuse the release image's own importer, migration code, and pinned seed checksum.
docker run --rm --network "$network" --cpus=4 --memory=2g \
  --env KB_PG_HOST=product-db --env KB_PG_PORT=5432 --env KB_PG_DB=world \
  --env KB_PG_USER=postgres --env "KB_PG_PASSWORD=$db_password" --env KB_PG_SSLMODE=disable \
  --env "COMMUNITY_SEED_URI=$seed_uri" --env "COMMUNITY_SEED_SHA256=$seed_sha256" \
  --entrypoint python "$image" -m db.sync.community_seed_import --role serving

# The Community dump is a versioned world snapshot; ECB rates are a daily series and that snapshot
# can predate today's as-of date. Refresh only this public source inside the disposable database,
# then rebuild its bounded calendar projection. The ECB importer logs the release content hash.
docker run --rm --network "$network" --cpus=4 --memory=2g \
  --env SYNC_PG_HOST=product-db --env SYNC_PG_PORT=5432 --env SYNC_PG_DB=world \
  --env SYNC_PG_USER=postgres --env "SYNC_PG_PASSWORD=$db_password" --env SYNC_PG_SSLMODE=disable \
  --entrypoint python "$image" -m db.sync.sources.ecb.sync
docker run --rm --network "$network" --cpus=4 --memory=2g \
  --env KB_PG_HOST=product-db --env KB_PG_PORT=5432 --env KB_PG_DB=world \
  --env KB_PG_USER=postgres --env "KB_PG_PASSWORD=$db_password" --env KB_PG_SSLMODE=disable \
  --entrypoint python "$image" -m db.sync.build_exchange_rate
docker exec "$db_name" psql -v ON_ERROR_STOP=1 -U postgres -d world -Atc \
  "SELECT 'ecb_release=' || source_release_id || '; rows=' || count(*) || '; date_range=' || min(date)::text || '..' || max(date)::text FROM knowledgebase.exchange_rate GROUP BY source_release_id"

# Assert the actual immutable image's target runtime and installed package consistency before grading.
docker run --rm -i --cpus=2 --memory=2g --entrypoint python "$image" - <<'PY'
import importlib.metadata
import subprocess
import sys

assert sys.version_info[:2] == (3, 11), sys.version
for package in ("torch", "transformers", "spacy", "sqlglot", "psycopg2-binary"):
    print(f"runtime_{package}={importlib.metadata.version(package)}")
subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
print(f"runtime_python={sys.version.split()[0]}", flush=True)
PY

# HNSW restore needs a 2 GB build allocation; serving/query suites do not. Shrink the disposable
# database's cgroup budget after seeding so it can coexist with the multi-GB CPU model process.
docker update --memory=2g --memory-swap=2g "$db_name" >/dev/null

# Run the complete current test source against the seed using the production image's Python 3.11
# and model bundle. RUN_ENGINE_TESTS=1 is deliberate: any skipped live suite is a failed gate.
suite_status=0
if [[ "${RUN_PRODUCT_SUITES:-1}" == "1" ]]; then
  docker run --rm --network "$network" --cpus=8 --memory=16g \
    --volume "$node_volume:/opt/node:ro" --volume /workspace:/workspace:ro \
    --workdir /app \
    --env PATH=/opt/node:/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    --env RUN_ENGINE_TESTS=1 --env RUN_ORCHESTRATOR_TESTS=0 \
    --env LIVE_ENGINE_ONLY=1 \
    --env INSTALL_CI_REQUIREMENTS=0 \
    --env RUN_WORLD_REGRESSION=1 \
    --env LIVE_REGRESSION_TIMEOUT_SECONDS=600 \
    --env TEST_SUITE_TIMEOUT_SECONDS=1800 \
    --env KB_PG_HOST=product-db --env KB_PG_PORT=5432 --env KB_PG_DB=world \
    --env KB_PG_USER=serving --env "KB_PG_PASSWORD=$role_password" --env KB_PG_SSLMODE=disable \
    --entrypoint /bin/sh "$image" /workspace/deploy/gcp/run_hermetic_suite.sh || suite_status=$?
else
  echo "live_product_suites=skipped by explicit CPU-smoke-only invocation" >&2
fi

# Exercise the production HTTP entrypoint under CPU-only Cloud Run resource limits. Repeated
# read-only world joins at concurrency 1/2/4/8 match the configured per-instance concurrency cap;
# this bounded smoke is not a sustained-load SLA.
docker run -d --name "$server_name" --network "$network" --cpus=8 --memory=16g \
  --env KB_PG_HOST=product-db --env KB_PG_PORT=5432 --env KB_PG_DB=world \
  --env KB_PG_USER=serving --env "KB_PG_PASSWORD=$role_password" --env KB_PG_SSLMODE=disable \
  --env APP_ENV=development --env AUTH_TEST_SUB=localdev --env DEVICE=cpu \
  "$image" >/dev/null
docker exec -i "$server_name" python - <<'PY'
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

base = "http://127.0.0.1:8080"
deadline = time.monotonic() + 360
health = None
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(base + "/api/healthz", timeout=3) as response:
            health = json.load(response)
        if all(health.get(key) is True for key in ("ok", "reason", "world", "dimension")):
            break
    except (OSError, urllib.error.URLError):
        pass
    time.sleep(2)
else:
    raise SystemExit(f"CPU server health did not become ready: {health!r}")
print("cpu_server_health=" + json.dumps(health, sort_keys=True), flush=True)

payload = {
    "tables": [
        {"name": "customers", "data": "Name,City\nAda,Paris\nLin,Lyon\nBo,Berlin\n"},
        {"name": "orders", "data": "Customer,Amount\nAda,120\nLin,150\nBo,200\n"},
    ],
    "question": "what is the total amount in France",
}
def call_once():
    request = urllib.request.Request(
        base + "/api/reason", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer test"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    rows = (result.get("result") or {}).get("rows") or []
    if result.get("error") or result.get("clarify") or not rows or float(rows[0][0]) != 270.0:
        raise SystemExit(f"CPU serving golden failed: {result!r}")
    return (time.perf_counter() - started) * 1000

def summary(latencies):
    ordered = sorted(latencies)
    return {
        "n": len(ordered),
        "p50": round(ordered[(len(ordered) - 1) // 2], 1),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max": round(ordered[-1], 1),
    }

single = [call_once() for _ in range(3)]
print("cpu_api_reason_world_join_ms=" + json.dumps(summary(single), sort_keys=True), flush=True)

load = {}
for workers in (2, 4, 8):
    latencies = []
    # The endpoint intentionally limits each verified principal to 30 requests/minute.
    # Earlier levels send 3 + 6 + 12 = 21 calls, so one eight-request burst reaches the
    # configured concurrency cap without turning this smoke into a rate-limit test.
    rounds = 1 if workers == 8 else 3
    for _ in range(rounds):
        with ThreadPoolExecutor(max_workers=workers) as executor:
            latencies.extend(executor.map(lambda _ignored: call_once(), range(workers)))
    load[str(workers)] = summary(latencies)
print("cpu_api_reason_world_join_load=" + json.dumps(load, sort_keys=True), flush=True)
PY
docker stats --no-stream --format 'cpu_server_container_memory={{.MemUsage}} cpu={{.CPUPerc}}' "$server_name"
docker exec "$server_name" awk '/VmHWM|VmRSS/ {print "cpu_server_process_" tolower($1) "=" $2 "_kib"}' /proc/1/status

if [[ "$suite_status" != 0 ]]; then
  echo "live product suites failed with exit $suite_status (CPU HTTP smoke still completed)" >&2
  exit "$suite_status"
fi

if [[ "${RUN_PRODUCT_SUITES:-1}" == "1" ]]; then
  echo "All configured suites, including live world and dataset suites, passed against disposable PostgreSQL."
else
  echo "CPU HTTP smoke passed; product suites were intentionally skipped in this invocation."
fi
