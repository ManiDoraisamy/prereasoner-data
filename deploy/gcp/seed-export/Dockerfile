FROM postgres:16-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
 && python3 -m pip install --break-system-packages --no-cache-dir google-cloud-storage \
 && rm -rf /var/lib/apt/lists/*

COPY export.py /app/export.py
ENTRYPOINT ["python3", "/app/export.py"]
