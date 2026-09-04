# syntax=docker/dockerfile:1
# Prereasoner serving engine.
#
#   docker build -t prereasoner-engine .
#   docker run -p 8080:8080 -e KB_PG_HOST=... -e KB_PG_PASSWORD=... prereasoner-engine
#
# Two-stage build: the builder stage installs the (large, CPU-only) Python stack into a
# self-contained venv; the runtime stage copies just the venv + the engine package. This
# drops pip's wheel/build leftovers and keeps a single apt layer out of the final image.
#
# Model weights (engine/data/encoder.pt, encoder_meta.pt, primitives.npz,
# anchor_assignment.npz, qwen_lora/, schema_property_head.pt) are GITIGNORED: present in a full working copy,
# absent in a fresh clone/CI. `COPY engine/` succeeds either way, so the *build* never
# fails on missing weights — instead the entrypoint checks for them at container START
# and exits with a clear, actionable message. See engine/data/README.md.

# ---------- builder: resolve + install the full serving stack ----------
FROM python:3.11-slim@sha256:be1575ed968de893bd54f4c56315ff7c4736ce522c1bca08fd521731aafc0d76 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements.lock.txt pins the complete CPU stack and hashes every distribution. requirements.txt
# remains the human-maintained input used to regenerate it. The lock installs the CPU torch wheel and
# installs spaCy's en_core_web_md straight from the release wheel (no post-install
# `spacy download` step needed — but keep the assertion below so a future requirements
# edit that drops the model wheel fails the build, not the first request).
COPY requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --require-hashes -r /tmp/requirements.lock.txt \
 && python -c "import spacy; spacy.load('en_core_web_md')"

# Pre-bake the Hugging Face models the engine loads at startup (the Qwen encoder base)
# and at first embedding (bge-small). Cloud Run containers must NOT download models at
# boot — the startup probe times out and HF rate limits unauthenticated pulls.
ENV HF_HOME=/opt/hf
COPY engine/model_revisions.py /tmp/prereasoner_model_revisions.py
# Both pinned base models and the manifested Prereasoner bundle are public. Builds therefore need no
# maintainer secret and are reproducible by a third-party Cloud Build project. Hugging Face may apply
# lower anonymous rate limits, so cloudbuild.yaml keeps a generous timeout.
RUN PYTHONPATH=/tmp python - <<'PY'
from transformers import AutoModel, AutoTokenizer
from prereasoner_model_revisions import (
    BGE_MODEL_ID, BGE_REVISION, QWEN_MODEL_ID, QWEN_REVISION,
)
models = (
    (QWEN_MODEL_ID, QWEN_REVISION),
    (BGE_MODEL_ID, BGE_REVISION),
)
for mid, revision in models:
    AutoModel.from_pretrained(mid, revision=revision)
    AutoTokenizer.from_pretrained(mid, revision=revision)
PY

# ---------- runtime ----------
FROM python:3.11-slim@sha256:be1575ed968de893bd54f4c56315ff7c4736ce522c1bca08fd521731aafc0d76

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Baked-in HF cache (builder stage); never touch the network for models at runtime.
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf /opt/hf
COPY LICENSE THIRD_PARTY.md /licenses/

# engine/ includes engine/data/* when the weights exist locally; in a fresh clone only
# the small committed artifacts (alloc.json, taxonomy.csv, thresholds, word_*.json) come along.
COPY engine/ /app/engine/

# regress/ = the pre-deploy regression gate (regress.run_regression). Shipped in the image so cloudbuild
# can run the OFFLINE tier against the real weights right after build (no Postgres needed) — see cloudbuild.yaml.
COPY regress/ /app/regress/

# db/sync = the scheduled knowledgebase refresh (ecb-rates-refresh Cloud Run job runs
# `python -m db.sync.sources.ecb.sync && python -m db.sync.build_exchange_rate` on THIS image, so
# rates data and serving code always come from one artifact). 449K; psycopg2 is already a serving
# dependency and `requests` arrives with transformers.
COPY db/ /app/db/

# Startup gate: verify the gitignored model artifacts are actually in the image (or in a
# mounted PREREASONER_DATA_DIR) BEFORE handing off to the server, so a weights-less image
# fails fast with instructions instead of a torch FileNotFoundError stack trace.
COPY <<'EOF' /app/entrypoint.sh
#!/bin/sh
set -e
DATA_DIR="${PREREASONER_DATA_DIR:-/app/engine/data}"
# The retention job uses Firebase Admin only; it must not load or require the multi-GB model bundle.
if [ "$1" = "python" ] && [ "$2" = "-m" ] && [ "$3" = "engine.trace_cleanup" ]; then
    exec "$@"
fi
missing=""
for f in encoder.pt encoder_meta.pt anchor_assignment.npz primitives.npz qwen_lora schema_property_head.pt; do
    [ -e "$DATA_DIR/$f" ] || missing="$missing $f"
done
if [ -n "$missing" ]; then
    echo "FATAL: Prereasoner model artifacts are missing from $DATA_DIR:" >&2
    echo "   $missing" >&2
    echo "" >&2
    echo "These files are gitignored (large binaries) and were not present when the image" >&2
    echo "was built. To fix, either:" >&2
    echo "  1. Place the artifacts in engine/data/ and rebuild the image" >&2
    echo "     (see engine/data/README.md for the full artifact list), or" >&2
    echo "  2. Mount a directory containing them and set PREREASONER_DATA_DIR, e.g." >&2
    echo "     docker run -v /path/to/data:/data -e PREREASONER_DATA_DIR=/data ..." >&2
    exit 1
fi
exec "$@"
EOF
RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "engine.server"]
