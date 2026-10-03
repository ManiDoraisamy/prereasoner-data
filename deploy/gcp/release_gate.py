"""Verify attested candidate builds before promotion, and hosted evidence before launch.

This command is read-only. Deployment must use the immutable image references it
prints, never a mutable tag. Keep the manifest and evidence outside build contexts
until the tested candidate is frozen.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


REQUIRED_STEPS = {
    'engine': {'build', 'regress-offline', 'runtime-server-smoke', 'live-product-suite'},
    'chat': {'test', 'build'},
}


def verify_build(manifest: dict, target: str, build: dict) -> str:
    record = manifest[target]
    if build.get('id') != record['build_id'] or build.get('status') != 'SUCCESS':
        raise ValueError(f'{target}: candidate build has not succeeded')
    substitutions = build.get('substitutions') or {}
    if substitutions.get('_TAG') != manifest['source_commit'][:7]:
        raise ValueError(f'{target}: build tag differs from the candidate source')
    if target == 'engine' and substitutions.get('_RUN_PRODUCT_SUITES') != '1':
        raise ValueError('engine: full product suite was disabled')
    steps = {step.get('id'): step.get('status') for step in build.get('steps', [])}
    if any(steps.get(name) != 'SUCCESS' for name in REQUIRED_STEPS[target]):
        raise ValueError(f'{target}: a mandatory build step was skipped or failed')
    images = build.get('results', {}).get('images', [])
    image = next((item for item in images if item.get('name') == record['tag']), None)
    if image is None or not re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('digest', '')):
        raise ValueError(f'{target}: pushed image digest is missing')
    reference = record['tag'].rsplit(':', 1)[0] + '@' + image['digest']
    if reference != record['image']:
        raise ValueError(f'{target}: manifest image differs from the tested build')
    return reference


def validate_manifest(manifest: dict) -> None:
    if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('source_commit', '')):
        raise ValueError('manifest needs the complete source commit')
    if not re.fullmatch(r'[0-9a-f]{64}', manifest.get('weights_manifest_sha256', '')):
        raise ValueError('manifest needs the model bundle fingerprint')
    if manifest.get('chat_migration') != 11:
        raise ValueError('manifest must attest durable replay migration 11')
    for name in ('engine', 'chat', 'hosting'):
        if not manifest.get('rollback', {}).get(name):
            raise ValueError(f'manifest needs an explicit {name} rollback target')


def verify_launch(manifest: dict, evidence: dict) -> None:
    if evidence.get('source_commit') != manifest['source_commit']:
        raise ValueError('hosted evidence is for a different source commit')
    required = ('production_smoke', 'versions', 'public_examples', 'ordered_followups',
                'sheets', 'scale_30000', 'history_recovery')
    for lane in required:
        outcome = evidence.get(lane, {})
        if outcome.get('status') != 'passed' or outcome.get('skipped', 0):
            raise ValueError(f'launch gate incomplete: {lane}')
    if evidence['public_examples'].get('total') != 18:
        raise ValueError('all 18 public examples must be individually accounted for')
    if not evidence.get('apps_script_version'):
        raise ValueError('Apps Script version was not recorded')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--hosted-evidence', type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    validate_manifest(manifest)
    for target in REQUIRED_STEPS:
        raw = subprocess.check_output(['gcloud', 'builds', 'describe', manifest[target]['build_id'],
            '--project=' + manifest['project'], '--format=json'], text=True, encoding='utf-8')
        print(target + '=' + verify_build(manifest, target, json.loads(raw)))
    if args.hosted_evidence:
        verify_launch(manifest, json.loads(args.hosted_evidence.read_text(encoding='utf-8')))
        print('launch evidence verified')


if __name__ == '__main__':
    main()
