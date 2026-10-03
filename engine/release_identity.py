"""Public release diagnostics from the immutable build context, without configuration secrets."""
import json
from pathlib import Path


def release_identity(path):
    try:
        record = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'source_commit': 'development', 'build_target': 'development'}
    return {key: record[key] for key in ('source_commit', 'build_target', 'weights_manifest_sha256')
            if key in record}
