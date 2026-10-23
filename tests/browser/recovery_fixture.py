"""Run the actual CLI/process/storage/render chain with invented ATS responses."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from job_radar.cli import main


def transport(url: str):
    root = Path(os.environ['RADAR_RECOVERY_BROWSER_FIXTURE'])
    board = url.rsplit('/', 2)[-2]
    with (root / f'{board}.calls').open('a', encoding='utf-8') as stream:
        stream.write('called\n')
    if board == 'retry' and not (root / 'allow-retry').exists():
        raise RuntimeError('invented temporary upstream failure')
    jobs = [{'id': board, 'title': f'{board.title()} Go Backend Engineer',
             'content': 'Go backend service development',
             'updated_at': '2026-07-18T01:00:00Z' if board == 'retry' else '2026-07-17T01:00:00Z',
             'absolute_url': f'https://example.test/jobs/{board}', 'location': {'name': 'Canada'}}]
    if board == 'healthy':
        jobs.append({'id': 'marketing', 'title': 'Marketing Engineer', 'content': 'Copywriting and marketing',
                     'absolute_url': 'https://example.test/jobs/marketing', 'location': {'name': 'Canada'}})
    return {'jobs': jobs}


def command(args):
    output, errors = io.StringIO(), io.StringIO()
    code = main(['--json', *args], get_json=transport, stdout=output, stderr=errors)
    if code != 0:
        raise RuntimeError(f'synthetic CLI failed: {output.getvalue()} {errors.getvalue()}')
    return json.loads(output.getvalue())


def build(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    os.environ['RADAR_RECOVERY_BROWSER_FIXTURE'] = str(root)
    (root / 'catalog.yaml').write_text('''
sources:
  greenhouse: {kind: official_api, terms_url: "https://example.test/terms", enabled: true}
companies:
  Fixture Alpha: {countries: [CA], source: greenhouse, board: healthy}
  Fixture Beta: {countries: [CA], source: greenhouse, board: retry}
''', encoding='utf-8')
    profile = root / 'profile.yaml'
    profile.write_text('''
profile: {skills: [Go]}
preferences: {countries: [CA], roles: [backend], tracks: [backend], visa_required: false}
matching: {minimum_score: 50, must_have: [backend]}
''', encoding='utf-8')
    common = ['run', '--catalog', str(root / 'catalog.yaml'), '--profile', str(profile),
              '--jobs-output', str(root / 'scan.json'), '--user-data', str(root / 'user-data'),
              '--aliases', str(root / 'no-aliases.json'), '--checkpoint', str(root / 'checkpoint.json'),
              '--mode', 'best-effort', '--max-source-concurrency', '1', '--source-timeout', '2']
    with patch('job_radar.scan_checkpoint._now', return_value='2026-07-17T09:00:00+00:00'):
        first = command([*common, '--observed-on', '2026-07-17', '--dashboard-output', str(root / 'partial.html'),
                         '--run-manifest', str(root / 'first-run.json')])
    (root / 'allow-retry').touch()
    with patch('job_radar.scan_checkpoint._now', return_value='2026-07-18T09:00:00+00:00'):
        second = command([*common, '--resume', '--observed-on', '2026-07-18',
                          '--dashboard-output', str(root / 'complete.html'), '--run-manifest', str(root / 'second-run.json')])
    scan_before = (root / 'scan.json').read_bytes()
    profile.write_text('''
profile: {skills: [Copywriting]}
preferences: {countries: [CA], roles: [marketing], tracks: [marketing], visa_required: false}
matching: {minimum_score: 50, must_have: [marketing]}
''', encoding='utf-8')
    command(['build-dashboard', '--jobs', str(root / 'scan.json'), '--profile', str(profile),
             '--user-data', str(root / 'user-data'), '--output', str(root / 'rescored.html')])
    scan = json.loads((root / 'scan.json').read_text(encoding='utf-8'))
    lifecycle = json.loads((root / 'user-data/tracking/lifecycle.json').read_text(encoding='utf-8'))
    return {'root': str(root), 'first': first, 'second': second,
            'first_receipt': json.loads((root / 'first-run.json').read_text(encoding='utf-8')),
            'second_receipt': json.loads((root / 'second-run.json').read_text(encoding='utf-8')),
            'scan_jobs': len(scan['jobs']), 'observed_on': scan['observed_on'],
            'last_seen': sorted({item['last_seen'] for item in lifecycle['items']}),
            'dates_by_id': {item['external_id']: [item['first_seen'], item['last_seen']] for item in lifecycle['items']},
            'persisted_jobs': len(lifecycle['items']),
            'scan_bytes_preserved_by_rebuild': scan_before == (root / 'scan.json').read_bytes(),
            'calls': {board: len((root / f'{board}.calls').read_text(encoding='utf-8').splitlines())
                      for board in ('healthy', 'retry')}}


if __name__ == '__main__':
    print(json.dumps(build(Path(sys.argv[1]).resolve())))
