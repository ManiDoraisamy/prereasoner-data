# syntax=docker/dockerfile:1
# PreReasoner serving engine.
#
#   docker build -t prereasoner-engine .
#   docker run -p 8080:8080 -e WORLD_PG_HOST=... -e WORLD_PG_PASSWORD=... prereasoner-engine
#
# Two-stage build: the builder stage installs the (large, CPU-only) Python stack into a
# self-contained venv; the runtime stage copies just the venv + the engine package. This
# drops pip's wheel/build leftovers and keeps a single apt layer out of the final image.
#
# Model weights (engine/data/encoder.pt, encoder_meta.pt, primitives.npz,
# anchor_assignment.npz, qwen_lora/) are GITIGNORED: present in a full working copy,
# absent in a fresh clone/CI. `COPY engine/` succeeds either way, so the *build* never
# fails on missing weights — instead the entrypoint checks for them at container START
# and exits with a clear, actionable message. See engine/data/README.md.

# ---------- builder: resolve + install the full serving stack ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements.txt pins the CPU torch wheel via its own --extra-index-url line and
# installs spaCy's en_core_web_md straight from the release wheel (no post-install
# `spacy download` step needed — but keep the assertion below so a future requirements
# edit that drops the model wheel fails the build, not the first request).
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
 && python -c "import spacy; spacy.load('en_core_web_md')"

# Pre-bake the Hugging Face models the engine loads at startup (the Qwen encoder base)
# and at first embedding (bge-small). Cloud Run containers must NOT download models at
# boot — the startup probe times out and HF rate limits unauthenticated pulls.
ENV HF_HOME=/opt/hf
# Authenticate the model pull with an HF token, mounted as a BuildKit SECRET (never baked into the image
# layers or history). Unauthenticated HF pulls are rate-limited to a crawl (~40 min → build timeout);
# authenticated is a couple of minutes. Degrades to unauthenticated if the secret is absent (e.g. a local
# `docker build` without --secret), so the build still works, just slowly.
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)" python - <<'PY'
from transformers import AutoModel, AutoTokenizer
for mid in ("Qwen/Qwen2.5-0.5B", "BAAI/bge-small-en-v1.5"):
    AutoModel.from_pretrained(mid); AutoTokenizer.from_pretrained(mid)
PY

# ---------- runtime ----------
FROM python:3.11-slim

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

# engine/ includes engine/data/* when the weights exist locally; in a fresh clone only
# the small committed artifacts (alloc.json, taxonomy.csv, thresholds, word_*.json) come along.
COPY engine/ /app/engine/

# regress/ = the pre-deploy regression gate (regress.run_regression). Shipped in the image so cloudbuild
# can run the OFFLINE tier against the real weights right after build (no Postgres needed) — see cloudbuild.yaml.
COPY regress/ /app/regress/

# Startup gate: verify the gitignored model artifacts are actually in the image (or in a
# mounted PREREASONER_DATA_DIR) BEFORE handing off to the server, so a weights-less image
# fails fast with instructions instead of a torch FileNotFoundError stack trace.
COPY <<'EOF' /app/entrypoint.sh
#!/bin/sh
set -e
DATA_DIR="${PREREASONER_DATA_DIR:-/app/engine/data}"
missing=""
for f in encoder.pt encoder_meta.pt anchor_assignment.npz primitives.npz qwen_lora; do
    [ -e "$DATA_DIR/$f" ] || missing="$missing $f"
done
if [ -n "$missing" ]; then
    echo "FATAL: PreReasoner model artifacts are missing from $DATA_DIR:" >&2
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
