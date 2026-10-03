"""Promotion must fail for skipped suites, mutable images and mismatched evidence."""
from contextlib import contextmanager

from deploy.gcp.release_gate import verify_build, verify_launch


@contextmanager
def rejected():
    try:
        yield
    except ValueError:
        return
    raise AssertionError('An unverified release was accepted')


def fixture():
    tag = 'region-docker.pkg.dev/project/repo/engine:aaaaaaa'
    digest = 'sha256:' + 'b'*64
    manifest = {'source_commit': 'a'*40, 'engine': {'build_id': 'one', 'tag': tag,
        'image': tag.rsplit(':', 1)[0] + '@' + digest}}
    build = {'id': 'one', 'status': 'SUCCESS', 'substitutions': {'_TAG': 'aaaaaaa',
        '_RUN_PRODUCT_SUITES': '1'}, 'steps': [{'id': name, 'status': 'SUCCESS'} for name in
        ('build', 'regress-offline', 'runtime-server-smoke', 'live-product-suite')],
        'results': {'images': [{'name': tag, 'digest': digest}]}}
    return manifest, build


def test_exact_tested_digest_passes():
    manifest, build = fixture()
    assert verify_build(manifest, 'engine', build) == manifest['engine']['image']


def test_untested_promotion_is_rejected():
    for change in ('failed', 'skip', 'tag', 'digest', 'step'):
        manifest, build = fixture()
        if change == 'failed': build['status'] = 'FAILURE'
        if change == 'skip': build['substitutions']['_RUN_PRODUCT_SUITES'] = '0'
        if change == 'tag': build['substitutions']['_TAG'] = 'another'
        if change == 'digest': manifest['engine']['image'] += 'wrong'
        if change == 'step': build['steps'][-1]['status'] = 'SKIPPED'
        with rejected(): verify_build(manifest, 'engine', build)


def test_build_success_cannot_substitute_for_hosted_evidence():
    manifest, _ = fixture()
    with rejected(): verify_launch(manifest, {'source_commit': 'a'*40})


if __name__ == '__main__':
    test_exact_tested_digest_passes()
    test_untested_promotion_is_rejected()
    test_build_success_cannot_substitute_for_hosted_evidence()
    print('Promotion gate: all checks passed')
