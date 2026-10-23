from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from job_radar import cli
from job_radar.cli import main


def arguments(root: Path) -> list[str]:
    (root / 'catalog.yaml').write_text('''
sources:
  greenhouse: {kind: official_api, terms_url: "https://example.test/terms", enabled: true}
companies:
  Example: {countries: [CA], source: greenhouse, board: example}
''', encoding='utf-8')
    (root / 'profile.yaml').write_text('''
profile: {skills: [Go]}
preferences: {countries: [CA], roles: [backend], tracks: [backend], visa_required: false}
matching: {minimum_score: 50, must_have: [backend]}
''', encoding='utf-8')
    return ['--json', 'run', '--catalog', str(root / 'catalog.yaml'),
            '--profile', str(root / 'profile.yaml'), '--jobs-output', str(root / 'latest.json'),
            '--dashboard-output', str(root / 'index.html'), '--user-data', str(root / 'user-data'),
            '--aliases', str(root / 'absent-aliases.json'), '--observed-on', '2026-07-17',
            '--run-manifest', str(root / 'attempt.json')]


def fake_scan(*args, **kwargs):
    return {'contract_version': 1, 'observed_on': '2026-07-17', 'mode': 'best-effort',
            'incomplete': False, 'failures': [], 'jobs': [
                dict(source='greenhouse', external_id='example-1', company='Example',
                     title='Go Backend Engineer', location='Canada', country='CA',
                     category='backend', tracks=['backend'], skills=['Go'],
                     score=None, summary='', risk='', salary='', first_seen='', visa_supported=None,
                     url='https://example.test/job/1', published_at='2026-07-17')
            ]}


def test_run_persists_separate_stages_without_claiming_review_or_deployment(tmp_path: Path):
    output = StringIO()
    assert main(arguments(tmp_path), scan_runner=fake_scan, stdout=output) == 0
    report = json.loads(output.getvalue())
    receipt = json.loads((tmp_path / 'attempt.json').read_text(encoding='utf-8'))
    assert receipt['state'] == 'finished'
    assert receipt['stages'] == dict(scan='complete', workflow='complete', review='not_configured',
                                    build='complete', sync='not_configured', deployment='not_requested')
    assert report['stages'] == receipt['stages']
    assert report['review'] == {'mode': 'disabled', 'provider_calls': 0}
    assert Path(report['run_manifest']) == tmp_path / 'attempt.json'


def test_failed_build_keeps_successful_scan_and_never_claims_completed_build(tmp_path: Path, monkeypatch):
    def broken_render(*args, **kwargs):
        raise OSError('synthetic renderer failure')
    monkeypatch.setattr(cli, 'render_dashboard_view_model', broken_render)
    output = StringIO()
    assert main(arguments(tmp_path), scan_runner=fake_scan, stdout=output) == 2
    receipt = json.loads((tmp_path / 'attempt.json').read_text(encoding='utf-8'))
    assert receipt['state'] == 'failed'
    assert receipt['stages']['scan'] == 'complete'
    assert receipt['stages']['workflow'] == 'complete'
    assert receipt['stages']['build'] == 'failed'
    assert receipt['stages']['review'] == 'not_configured'
    assert receipt['error_type'] == 'OSError'
    assert 'synthetic renderer failure' not in json.dumps(receipt)
    assert not (tmp_path / 'index.html').exists()


def test_failed_scan_preserves_later_stages_as_pending(tmp_path: Path):
    def broken_scan(*args, **kwargs):
        raise RuntimeError('synthetic scanner failure')
    assert main(arguments(tmp_path), scan_runner=broken_scan, stdout=StringIO()) == 2
    receipt = json.loads((tmp_path / 'attempt.json').read_text(encoding='utf-8'))
    assert receipt['stages']['scan'] == 'failed'
    assert receipt['stages']['workflow'] == receipt['stages']['review'] == receipt['stages']['build'] == 'pending'


def test_explicit_existing_receipt_is_preserved_before_any_scan(tmp_path: Path):
    args = arguments(tmp_path)
    path = tmp_path / 'attempt.json'
    original = b'original attempt receipt'
    path.write_bytes(original)

    def forbidden_scan(*args, **kwargs):
        raise AssertionError('must not scan before validating receipt ownership')

    assert main(args, scan_runner=forbidden_scan, stdout=StringIO()) == 2
    assert path.read_bytes() == original


def test_offline_profile_filter_does_not_destroy_saved_observations(tmp_path: Path):
    arguments(tmp_path)
    raw = fake_scan()
    raw['jobs'].append(dict(raw['jobs'][0], external_id='other-2', title='Unrelated fixture role',
                            category='other', skills=[], tracks=[]))
    jobs = tmp_path / 'saved.json'
    jobs.write_text(json.dumps(raw), encoding='utf-8')
    before = jobs.read_bytes()
    assert main(['build-dashboard', '--jobs', str(jobs), '--profile', str(tmp_path / 'profile.yaml'),
                 '--output', str(tmp_path / 'offline.html')], stdout=StringIO()) == 0
    html = (tmp_path / 'offline.html').read_text(encoding='utf-8')
    assert 'Go Backend Engineer' in html
    assert 'Unrelated fixture role' not in html
    assert jobs.read_bytes() == before
