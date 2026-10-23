from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from job_radar.source_orchestration import AtomicSourceScanFailed, scan_catalog_resilient
import job_radar.source_orchestration as orchestration
from test_scan_checkpoint import catalog, recorded_transport


def http_error_transport(url):
    raise HTTPError(url, int(os.environ.get('RADAR_HEALTH_HTTP_CODE', '429')), 'private upstream token=must-not-appear', {}, None)


def network_timeout_transport(url):
    raise URLError(TimeoutError('must-not-appear'))


def malformed_transport(url):
    return {'jobs': 'not-a-job-list'}


def run(root: Path, get_json=recorded_transport, *, mode='best-effort', resume=False):
    return scan_catalog_resilient(
        catalog('healthy', 'retry'), get_json, mode=mode, per_source_timeout=2,
        output=root / 'jobs.json', max_concurrency=1, checkpoint=root / 'checkpoint.json',
        resume=resume, health_output=root / ('health-replay.json' if resume else 'health.json'),
    )


def test_health_distinguishes_fresh_cached_failed_and_preserves_scan_v1(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RADAR_CHECKPOINT_FIXTURE', str(tmp_path))
    result = run(tmp_path)
    health = json.loads((tmp_path / 'health.json').read_text(encoding='utf8'))
    assert set(result) == {'contract_version', 'observed_on', 'mode', 'incomplete', 'jobs', 'failures'}
    assert result['incomplete'] is True and health['state'] == 'partial'
    assert health['scope'] == 'configured_company_boards'
    entries = {entry['company']: entry for entry in health['entries']}
    assert entries['healthy']['state'] == 'complete' and entries['healthy']['jobs'] == 1
    assert entries['healthy']['elapsed_seconds'] >= 0
    assert entries['healthy']['fetched_at'] and entries['healthy']['attempted_at']
    assert entries['retry']['state'] == 'failed'
    assert entries['retry']['failure_category'] == 'source_error'
    healthy_fetched = entries['healthy']['fetched_at']
    (tmp_path / 'allow-retry').touch()
    run(tmp_path, resume=True)
    replay = json.loads((tmp_path / 'health-replay.json').read_text(encoding='utf8'))
    entries = {entry['company']: entry for entry in replay['entries']}
    assert replay['state'] == 'complete'
    assert entries['healthy']['state'] == 'cached'
    assert entries['healthy']['fetched_at'] == healthy_fetched
    assert entries['healthy']['attempted_at'] is None and entries['healthy']['elapsed_seconds'] is None
    assert entries['retry']['state'] == 'complete'


def test_http_rate_limit_is_safe_and_not_a_generic_failure(tmp_path: Path):
    result = run(tmp_path, http_error_transport)
    health = json.loads((tmp_path / 'health.json').read_text(encoding='utf8'))
    assert {entry['http_status'] for entry in health['entries']} == {429}
    assert {failure['category'] for failure in result['failures']} == {'rate_limited'}
    assert health['state'] == 'failed'
    assert 'must-not-appear' not in json.dumps(health) + json.dumps(result)


def test_atomic_failure_preserves_old_output_but_finishes_health(tmp_path: Path):
    output = tmp_path / 'jobs.json'
    output.write_text('previous output', encoding='utf8')
    with pytest.raises(AtomicSourceScanFailed):
        run(tmp_path, malformed_transport, mode='atomic')
    assert output.read_text(encoding='utf8') == 'previous output'
    health = json.loads((tmp_path / 'health.json').read_text(encoding='utf8'))
    assert health['state'] == 'failed' and health['finished_at']
    assert {entry['failure_category'] for entry in health['entries']} == {'malformed'}


@pytest.mark.parametrize('code,category', [(403, 'access_denied'), (404, 'not_found'), (503, 'upstream_error')])
def test_http_diagnosis_retains_only_safe_status_and_category(tmp_path, monkeypatch, code, category):
    monkeypatch.setenv('RADAR_HEALTH_HTTP_CODE', str(code))
    result = run(tmp_path, http_error_transport)
    health = json.loads((tmp_path / 'health.json').read_text(encoding='utf8'))
    assert {entry['http_status'] for entry in health['entries']} == {code}
    assert {failure['category'] for failure in result['failures']} == {category}
    assert 'must-not-appear' not in json.dumps(health) + json.dumps(result)


def test_network_timeout_is_distinct_from_parent_kill_deadline(tmp_path):
    result = run(tmp_path, network_timeout_transport)
    assert {failure['category'] for failure in result['failures']} == {'timeout'}
    health = json.loads((tmp_path / 'health.json').read_text(encoding='utf8'))
    assert {entry['failure_category'] for entry in health['entries']} == {'network_timeout'}


@pytest.mark.parametrize('mode', ['best-effort', 'atomic'])
def test_dead_worker_without_poll_message_cannot_finish_health_as_complete(tmp_path, monkeypatch, mode):
    def dead_worker(context, company, get_json, maximum):
        return {'company_scope': company, 'company_name': company[0], 'source_name': company[1],
                'process': SimpleNamespace(is_alive=lambda: False, close=lambda: None),
                'connection': SimpleNamespace(poll=lambda: False, close=lambda: None),
                'ready': True, 'execution_deadline': time.monotonic() + 60,
                'startup_deadline': time.monotonic() + 60}
    monkeypatch.setattr(orchestration, '_start_worker', dead_worker)
    output = tmp_path / 'jobs.json'
    output.write_text('old atomic output', encoding='utf8')
    if mode == 'atomic':
        with pytest.raises(AtomicSourceScanFailed) as error:
            run(tmp_path, mode=mode)
        result = error.value.result
        assert output.read_text(encoding='utf8') == 'old atomic output'
    else:
        result = run(tmp_path, mode=mode)
    assert result['incomplete'] is True
    health = json.loads((tmp_path / 'health.json').read_text(encoding='utf8'))
    assert health['state'] == 'failed'
    assert {entry['state'] for entry in health['entries']} == {'failed'}
    assert {entry['failure_category'] for entry in health['entries']} == {'source_error'}
    assert all(entry['elapsed_seconds'] is not None for entry in health['entries'])


@pytest.mark.parametrize('name', ['jobs.json', 'checkpoint.json', 'checkpoint.json.guard'])
def test_health_output_cannot_replace_jobs_checkpoint_or_guard(tmp_path: Path, name: str):
    with pytest.raises(ValueError, match='separate|guard'):
        scan_catalog_resilient(catalog('healthy'), recorded_transport, mode='atomic',
                               per_source_timeout=2, output=tmp_path / 'jobs.json',
                               checkpoint=tmp_path / 'checkpoint.json', health_output=tmp_path / name)
    assert not (tmp_path / 'healthy.calls').exists()
