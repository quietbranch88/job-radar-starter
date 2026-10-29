from __future__ import annotations

from dataclasses import asdict
from io import StringIO
import json
import os
from pathlib import Path

import pytest

from job_radar.catalog import load_catalog
from job_radar.cli import main
from job_radar.config import load_user_config
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.domain.jobs import Job
from job_radar.public_workflow import execute_public_workflow
from job_radar.source_orchestration import scan_catalog_resilient


ALIASES = {'contract_version': 1, 'company_aliases': {}, 'title_aliases': {}}


def empty_retry_transport(url):
    root = Path(os.environ['RADAR_EMPTY_RETRY_FIXTURE'])
    board = url.rsplit('/', 2)[-2]
    with (root / f'{board}.calls').open('a', encoding='utf-8') as stream:
        stream.write('called\n')
    if board == 'b':
        if not (root / 'allow-empty-retry').exists():
            raise TimeoutError('synthetic board timeout')
        return {'jobs': []}
    return {'jobs': [{'id': 'a', 'title': 'Backend Engineer',
                      'absolute_url': 'https://example.test/a',
                      'location': {'name': 'Canada'}, 'updated_at': '2026-07-01'}]}


def test_empty_next_day_retry_updates_lifecycle_and_replay_keeps_fetch_date(tmp_path, monkeypatch):
    monkeypatch.setenv('RADAR_EMPTY_RETRY_FIXTURE', str(tmp_path))
    catalog = tmp_path / 'catalog.yaml'
    catalog.write_text('''sources:
  greenhouse: {kind: official_api, terms_url: "https://example.test/terms", enabled: true}
companies:
  Company A: {countries: [CA], source: greenhouse, board: a}
  Company B: {countries: [CA], source: greenhouse, board: b}
''', encoding='utf-8')
    profile = tmp_path / 'profile.yaml'
    profile.write_text('matching: {minimum_score: 0}\n', encoding='utf-8')
    user_data = tmp_path / 'user-data'
    jobs = [Job(source='greenhouse', external_id=letter, company=f'Company {letter.upper()}',
                title='Backend Engineer', country='CA', location='Canada',
                url=f'https://example.test/{letter}', published_at='2026-07-01')
            for letter in ('a', 'b')]
    prior_scan = {'contract_version': 1, 'observed_on': '2026-07-01', 'mode': 'best-effort',
                  'incomplete': False, 'jobs': [asdict(job) for job in jobs], 'failures': []}
    execute_public_workflow(scan_result=prior_scan, config=load_user_config(profile),
                            repository=LocalTrackingStore(user_data), observed_on='2026-07-01',
                            aliases=ALIASES)
    checkpoint = tmp_path / 'walk.json'
    output = tmp_path / 'jobs.json'
    monkeypatch.setattr('job_radar.scan_checkpoint._now', lambda: '2026-07-14T09:00:00+00:00')
    first = scan_catalog_resilient(load_catalog(catalog), empty_retry_transport,
                                   mode='best-effort', per_source_timeout=3, output=output,
                                   checkpoint=checkpoint, observed_on='2026-07-14')
    assert first['incomplete'] is True
    (tmp_path / 'allow-empty-retry').touch()

    for attempt, day in enumerate(('2026-07-15', '2026-07-16')):
        monkeypatch.setattr('job_radar.scan_checkpoint._now', lambda day=day: day + 'T09:00:00+00:00')
        report = StringIO()
        assert main(['--json', 'run', '--catalog', str(catalog), '--profile', str(profile),
                     '--mode', 'best-effort', '--checkpoint', str(checkpoint), '--resume',
                     '--observed-on', day, '--jobs-output', str(output),
                     '--user-data', str(user_data), '--aliases', str(tmp_path / 'absent-aliases.json'),
                     '--dashboard-output', str(tmp_path / 'index.html'),
                     '--run-manifest', str(tmp_path / f'attempt-{attempt}.json')],
                    get_json=empty_retry_transport, scan_runner=scan_catalog_resilient,
                    stdout=report) == 0, report.getvalue()
        persisted = {job['external_id']: job for job in LocalTrackingStore(user_data).read('lifecycle')['items']}
        assert persisted['b']['freshness'] == 'stale'
        assert persisted['a']['last_seen'] == '2026-07-14'
        assert persisted['b']['last_seen'] == '2026-07-01'
        saved = json.loads(output.read_text(encoding='utf-8'))
        assert saved['observed_on'] == '2026-07-14'
        assert saved['latest_observed_on'] == '2026-07-15'
    assert (tmp_path / 'a.calls').read_text().splitlines() == ['called']
    assert (tmp_path / 'b.calls').read_text().splitlines() == ['called', 'called']

    rebuilt = tmp_path / 'rebuilt.html'
    assert main(['build-dashboard', '--jobs', str(output), '--output', str(rebuilt),
                 '--profile', str(profile)], stdout=StringIO()) == 0
    scored = tmp_path / 'rescored.json'
    assert main(['score', '--jobs', str(output), '--profile', str(profile), '--output', str(scored)],
                stdout=StringIO()) == 0
    assert json.loads(scored.read_text())['latest_observed_on'] == '2026-07-15'


@pytest.mark.parametrize('latest', [None, False, 'bad', '2026-07-13', '9999-01-01'])
def test_invalid_completed_observation_date_is_rejected(tmp_path, latest):
    raw = {'contract_version': 1, 'observed_on': '2026-07-14', 'latest_observed_on': latest,
           'mode': 'atomic', 'incomplete': False, 'jobs': [], 'failures': []}
    jobs = tmp_path / 'jobs.json'
    jobs.write_text(json.dumps(raw), encoding='utf-8')
    output = tmp_path / 'index.html'
    output.write_text('previous dashboard', encoding='utf-8')
    assert main(['--json', 'build-dashboard', '--jobs', str(jobs), '--output', str(output)],
                stdout=StringIO()) == 2
    assert output.read_text() == 'previous dashboard'
    profile = tmp_path / 'profile.yaml'
    profile.write_text('{}', encoding='utf-8')
    scored = tmp_path / 'scored.json'
    scored.write_text('previous scored scan', encoding='utf-8')
    assert main(['--json', 'score', '--jobs', str(jobs), '--profile', str(profile),
                 '--output', str(scored)], stdout=StringIO()) == 2
    assert scored.read_text() == 'previous scored scan'
