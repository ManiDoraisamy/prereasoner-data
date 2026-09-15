"""Create and upload the public Community database seed from a Cloud SQL socket."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from google.cloud import storage


def main() -> None:
    output = Path("/tmp/community-seed-v4.dump")
    bucket_name = os.environ["SEED_BUCKET"]
    object_name = os.environ.get("SEED_OBJECT", "community-seed-v4.dump")
    host = os.environ["SYNC_PG_HOST"]
    database = os.environ.get("SYNC_PG_DB", "world")
    user = os.environ.get("SYNC_PG_USER", "postgres")
    password = os.environ["SYNC_PG_PASSWORD"]
    env = os.environ.copy()
    env.update(PGHOST=host, PGDATABASE=database, PGUSER=user, PGPASSWORD=password)
    command = (
        "pg_dump", "--format=custom", "--compress=1", "--no-owner", "--no-privileges",
        "--schema=public", "--schema=knowledgebase", "--schema=iana",
        "--exclude-table=knowledgebase.community_bootstrap",
        "--file", str(output),
    )
    print("seed-export: dumping public, knowledgebase, and iana schemas", flush=True)
    subprocess.run(command, check=True, env=env)
    print(f"seed-export: uploading {output.stat().st_size} bytes", flush=True)
    storage.Client().bucket(bucket_name).blob(object_name).upload_from_filename(
        str(output), content_type="application/octet-stream",
    )
    print(f"seed-export: uploaded gs://{bucket_name}/{object_name}", flush=True)


if __name__ == "__main__":
    main()
