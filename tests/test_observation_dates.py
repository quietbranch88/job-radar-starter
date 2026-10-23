from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
from io import StringIO
import json
from pathlib import Path

import pytest

from job_radar.adapters import parse_greenhouse
from job_radar.cli import main
from job_radar.scan_checkpoint import ScanCheckpoint
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.public_workflow import execute_public_workflow
from job_radar.config import load_user_config
from test_run_receipts import arguments, fake_scan


class DashboardData(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.active = False
        self.parts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == 'dashboard-data':
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def value(self):
        return json.loads(''.join(self.parts))


def test_next_day_publication_can_resume_twice_without_poisoning_completed_scope(tmp_path: Path, monkeypatch):
    args = arguments(tmp_path)
    checkpoint_path = tmp_path / 'checkpoint.json'
    monkeypatch.setattr('job_radar.scan_checkpoint._now', lambda: '2026-07-17T09:00:00+00:00')
    state = ScanCheckpoint(checkpoint_path, [('Example', 'greenhouse', 'example', 'CA')],
                           mode='atomic', max_jobs=10000, observed_on='2026-07-17', resume=False)
    jobs = parse_greenhouse({'jobs': [dict(id='late-1', title='Go Backend Engineer',
                                         absolute_url='https://example.test/jobs/late-1',
                                         updated_at='2026-07-18T01:00:00Z', location={'name': 'Canada'})]}, 'Example')
    monkeypatch.setattr('job_radar.scan_checkpoint._now', lambda: '2026-07-18T09:00:00+00:00')
    state.complete(('Example', 'greenhouse', 'example', 'CA'), [replace(job, country='CA') for job in jobs])
    before = checkpoint_path.read_bytes()

    def forbidden_fetch(url):
        raise AssertionError('a completed checkpoint must not fetch on replay')

    for attempt in (1, 2):
        current = list(args)
        current[current.index('--run-manifest') + 1] = str(tmp_path / f'replay-{attempt}.json')
        current[current.index('--observed-on') + 1] = '2026-07-18'
        report = StringIO()
        assert main([*current, '--checkpoint', str(checkpoint_path), '--resume'],
                    get_json=forbidden_fetch, stdout=report) == 0, report.getvalue()
        assert json.loads(report.getvalue())['stages']['workflow'] == 'complete'
    persisted = json.loads((tmp_path / 'user-data/tracking/lifecycle.json').read_text(encoding='utf-8'))['items']
    assert persisted[0]['first_seen'] == persisted[0]['last_seen'] == '2026-07-18'
    assert checkpoint_path.read_bytes() == before
    output = tmp_path / 'replayed.html'
    assert main(['build-dashboard', '--jobs', str(tmp_path / 'latest.json'), '--output', str(output)],
                stdout=StringIO()) == 0


def test_offline_profile_only_changes_scoring_not_observation_dates(tmp_path: Path):
    arguments(tmp_path)
    raw = fake_scan()
    raw['jobs'][0]['published_at'] = '2026-07-01'
    jobs = tmp_path / 'scan.json'
    jobs.write_text(json.dumps(raw), encoding='utf-8')
    before = jobs.read_bytes()
    models = []
    for name, extra in [('plain', []), ('profiled', ['--profile', str(tmp_path / 'profile.yaml')])]:
        output = tmp_path / f'{name}.html'
        assert main(['build-dashboard', '--jobs', str(jobs), '--output', str(output), *extra], stdout=StringIO()) == 0
        models.append(DashboardData(output.read_text(encoding='utf-8')).value())
    for model in models:
        assert model['jobs'][0]['first_seen'] == model['jobs'][0]['last_seen'] == '2026-07-17'
        assert model['jobs'][0]['published_at'] == '2026-07-01'
    assert jobs.read_bytes() == before


def workflow(tmp_path, scan):
    return execute_public_workflow(
        scan_result=scan, config=load_user_config(tmp_path / 'profile.yaml'),
        repository=LocalTrackingStore(tmp_path / 'user-data'), observed_on='2026-07-17',
        aliases={'contract_version': 1, 'company_aliases': {}, 'title_aliases': {}},
    )


def test_cached_jobs_keep_own_dates_and_cannot_replace_newer_lifecycle(tmp_path: Path):
    arguments(tmp_path)
    initial = fake_scan()
    initial['jobs'][0].update(last_seen='2026-07-18', title='Newest Backend Engineer')
    workflow(tmp_path, initial)
    mixed = fake_scan()
    mixed['jobs'][0].update(last_seen='2026-07-17', title='Old cached title')
    mixed['jobs'].append({**initial['jobs'][0], 'external_id': 'new-2', 'title': 'Next Day Backend Engineer'})
    result = workflow(tmp_path, mixed)
    persisted = {item['external_id']: item for item in result['lifecycle']['jobs']}
    assert persisted['example-1']['title'] == 'Newest Backend Engineer'
    assert persisted['example-1']['first_seen'] == persisted['example-1']['last_seen'] == '2026-07-18'
    assert persisted['new-2']['first_seen'] == persisted['new-2']['last_seen'] == '2026-07-18'
    assert result['scan']['observed_on'] == '2026-07-17'


def test_mixed_walk_does_not_refresh_cached_job_to_retry_day(tmp_path: Path):
    arguments(tmp_path)
    mixed = fake_scan()
    mixed['jobs'][0]['last_seen'] = '2026-07-17'
    mixed['jobs'].append({**mixed['jobs'][0], 'external_id': 'new-2',
                          'last_seen': '2026-07-18', 'published_at': '2026-07-18'})
    result = workflow(tmp_path, mixed)
    assert {item['external_id']: (item['first_seen'], item['last_seen']) for item in result['lifecycle']['jobs']} == {
        'example-1': ('2026-07-17', '2026-07-17'), 'new-2': ('2026-07-18', '2026-07-18'),
    }


@pytest.mark.parametrize('seen,published', [('2026-07-17', '2026-07-18'), ('9999-01-01', '2026-07-17'), (False, '2026-07-17')])
def test_future_or_invalid_observation_is_not_made_valid_by_resuming(tmp_path: Path, seen, published):
    arguments(tmp_path)
    raw = fake_scan()
    raw['jobs'][0].update(last_seen=seen, published_at=published)
    with pytest.raises(ValueError):
        workflow(tmp_path, raw)
    store = LocalTrackingStore(tmp_path / 'user-data')
    assert store.read('lifecycle')['items'] == []
