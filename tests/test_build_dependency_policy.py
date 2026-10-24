"""Keep the public build and CI lock on the reviewed upstream advisory fix."""

from pathlib import Path
import json
import tomllib


def test_setuptools_build_and_ci_lock_use_the_reviewed_fix():
    project = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
    assert project['build-system']['requires'] == ['setuptools==84.1.0']
    lock = Path('requirements-ci.lock').read_text(encoding='utf-8')
    assert 'setuptools==84.1.0' in lock
    assert '--hash=sha256:5231689c6ac8156cd155cfb98c3fd6fb252c3e7e9bd3d78b64d3c55f132624c9' in lock
    assert 'setuptools==83.0.0' not in lock


def test_optional_advisory_overrides_are_hash_locked_without_toolchain_downgrade():
    lock = json.loads(Path('optional-sync/cloudflare/package-lock.json').read_text(encoding='utf-8'))['packages']
    assert lock['node_modules/undici']['version'] == '7.30.1'
    assert lock['node_modules/undici']['integrity'] == 'sha512-03KrMbjIFDWQR5RNPrSw3bd1r12JpyFQl9/yxjUbJtqkjmllqC5lFjZh6ujeuJCKr8EOQvRW872Oy/az+nLYxg=='
    assert lock['node_modules/zod']['version'] == '4.6.6'
    assert lock['node_modules/zod']['integrity'] == 'sha512-4UmzQUF5fxvfcYHlbjBmejU+w+hbDnWFx4aS8kBx7nPBHcO2SqPbpAG8snbdg+Xse4pRefR98hgjFgIMCI/UhA=='
    assert 'node_modules/@cloudflare/vitest-plugin/node_modules/zod' not in lock
    assert lock['node_modules/miniflare']['version'] == '5.20261019.2-beta'
    assert lock['node_modules/wrangler']['version'] == '4.152.0'
    assert lock['node_modules/workerd']['version'] == '1.20261019.1'
    assert lock['node_modules/@cloudflare/vitest-plugin']['version'] == '1.4.7'
