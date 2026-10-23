from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path

import pytest

from job_radar.catalog import Catalog, Company, Source
from job_radar.source_orchestration import AtomicSourceScanFailed, scan_catalog_resilient


def recorded_transport(url: str):
    root = Path(os.environ['RADAR_CHECKPOINT_FIXTURE'])
    board = url.rsplit('/', 2)[-2]
    with (root / f'{board}.calls').open('a', encoding='utf-8') as stream:
        stream.write('called\n')
    if board == 'retry' and not (root / 'allow-retry').exists():
        raise RuntimeError('synthetic unavailable board')
    if board == 'empty':
        return {'jobs': []}
    return {'jobs': [{'id': board, 'title': f'{board} Backend Engineer',
                      'absolute_url': f'https://example.test/jobs/{board}',
                      'location': {'name': 'Remote'}}]}


def catalog(*boards: str) -> Catalog:
    return Catalog(countries={}, sources={'greenhouse': Source('official_api', 'https://example.test/terms', True)},
                   companies={board: Company(('CA',), 'greenhouse', board) for board in boards})


def calls(root: Path, board: str) -> int:
    path = root / f'{board}.calls'
    return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0


@pytest.fixture
def fixture_root(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('RADAR_CHECKPOINT_FIXTURE', str(tmp_path))
    return tmp_path


def scan(root: Path, boards=('healthy', 'retry'), **kwargs):
    return scan_catalog_resilient(catalog(*boards), recorded_transport,
                                  mode=kwargs.pop('mode', 'best-effort'), per_source_timeout=2,
                                  output=root / 'latest.json', checkpoint=root / 'walk.json',
                                  max_concurrency=1, **kwargs)


def test_resume_reuses_proven_companies_and_preserves_walk_date(fixture_root: Path, monkeypatch):
    monkeypatch.setattr('job_radar.scan_checkpoint._now', lambda: '2026-07-17T09:00:00+00:00')
    first = scan(fixture_root, observed_on='2026-07-17')
    assert first['incomplete'] is True
    assert [job['external_id'] for job in first['jobs']] == ['healthy']
    (fixture_root / 'allow-retry').touch()
    monkeypatch.setattr('job_radar.scan_checkpoint._now', lambda: '2026-07-18T09:00:00+00:00')

    second = scan(fixture_root, resume=True, observed_on='2026-07-18')

    assert second['incomplete'] is False
    assert {job['external_id'] for job in second['jobs']} == {'healthy', 'retry'}
    assert second['observed_on'] == '2026-07-17'
    assert {job['external_id']: job['last_seen'] for job in second['jobs']} == {
        'healthy': '2026-07-17', 'retry': '2026-07-18',
    }
    assert calls(fixture_root, 'healthy') == 1
    assert calls(fixture_root, 'retry') == 2
    state = json.loads((fixture_root / 'walk.json').read_text(encoding='utf-8'))
    assert all(item['state'] == 'complete' and item['fetched_at'] for item in state['companies'])


def test_completed_empty_company_is_not_fetched_again(fixture_root: Path):
    first = scan(fixture_root, ('empty', 'healthy'))
    second = scan(fixture_root, ('empty', 'healthy'), resume=True)
    assert first == second
    assert calls(fixture_root, 'empty') == 1
    assert calls(fixture_root, 'healthy') == 1


def test_checkpoint_is_opt_in_and_existing_walk_is_not_overwritten(fixture_root: Path):
    scan(fixture_root, ('healthy',))
    before = (fixture_root / 'walk.json').read_bytes()
    with pytest.raises(ValueError, match='exists'):
        scan(fixture_root, ('healthy',))
    assert calls(fixture_root, 'healthy') == 1
    assert (fixture_root / 'walk.json').read_bytes() == before


@pytest.mark.parametrize('change', ['company', 'board', 'country', 'mode', 'limit'])
def test_changed_plan_fails_before_fetch_and_preserves_bytes(fixture_root: Path, change: str):
    scan(fixture_root, ('healthy',))
    before = (fixture_root / 'walk.json').read_bytes()
    changed = catalog('healthy')
    options = {}
    if change == 'company':
        changed = catalog('different')
    elif change == 'board':
        changed.companies['healthy'] = replace(changed.companies['healthy'], board='different')
    elif change == 'country':
        changed.companies['healthy'] = replace(changed.companies['healthy'], countries=('US',))
    elif change == 'mode':
        options['mode'] = 'atomic'
    elif change == 'limit':
        options['max_jobs_per_source'] = 5
    with pytest.raises(ValueError, match='plan'):
        scan_catalog_resilient(changed, recorded_transport, mode=options.pop('mode', 'best-effort'),
                               per_source_timeout=5, output=fixture_root / 'latest.json',
                               checkpoint=fixture_root / 'walk.json', resume=True, **options)
    assert calls(fixture_root, 'healthy') == 1
    assert calls(fixture_root, 'different') == 0
    assert (fixture_root / 'walk.json').read_bytes() == before


@pytest.mark.parametrize('corruption', ['json', 'duplicate', 'digest', 'version', 'old-version'])
def test_corrupt_receipt_is_not_discarded_or_trusted(fixture_root: Path, corruption: str):
    scan(fixture_root, ('healthy',))
    path = fixture_root / 'walk.json'
    if corruption == 'json':
        path.write_text('{broken', encoding='utf-8')
    elif corruption == 'duplicate':
        path.write_text('{"contract_version":1,"contract_version":1}', encoding='utf-8')
    else:
        state = json.loads(path.read_text(encoding='utf-8'))
        if corruption == 'digest':
            state['companies'][0]['jobs'][0]['title'] = 'unverified replacement'
        elif corruption == 'version':
            state['contract_version'] = True
        else:
            state['contract_version'] = 1
        path.write_text(json.dumps(state), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        scan(fixture_root, ('healthy',), resume=True)
    assert calls(fixture_root, 'healthy') == 1
    assert path.read_bytes() == before


def test_interruption_retains_completed_scope_before_next_fetch(fixture_root: Path, monkeypatch):
    from job_radar.scan_checkpoint import ScanCheckpoint
    complete = ScanCheckpoint.complete

    def interrupted(self, company, jobs):
        complete(self, company, jobs)
        raise SystemExit('synthetic interrupted parent')

    with monkeypatch.context() as patch:
        patch.setattr(ScanCheckpoint, 'complete', interrupted)
        with pytest.raises(SystemExit):
            scan(fixture_root, ('healthy', 'later'))
    assert calls(fixture_root, 'healthy') == 1
    assert calls(fixture_root, 'later') == 0
    resumed = scan(fixture_root, ('healthy', 'later'), resume=True)
    assert resumed['incomplete'] is False
    assert calls(fixture_root, 'healthy') == 1
    assert calls(fixture_root, 'later') == 1


def test_atomic_failure_retains_previous_output_but_can_resume(fixture_root: Path):
    output = fixture_root / 'latest.json'
    output.write_text('previous output', encoding='utf-8')
    with pytest.raises(AtomicSourceScanFailed):
        scan(fixture_root, mode='atomic')
    assert output.read_text(encoding='utf-8') == 'previous output'
    (fixture_root / 'allow-retry').touch()
    result = scan(fixture_root, mode='atomic', resume=True)
    assert len(result['jobs']) == 2
    assert calls(fixture_root, 'healthy') == 1


def _try_guard(path: str, result):
    from job_radar.local_state import writer_guard
    try:
        with writer_guard(Path(path)):
            result.put('acquired')
    except ValueError:
        result.put('blocked')


def test_os_guard_serializes_real_processes_and_retains_inode(tmp_path: Path):
    from job_radar.local_state import writer_guard
    context = multiprocessing.get_context('spawn')
    result = context.Queue()
    guard = tmp_path / 'walk.guard'
    with writer_guard(guard):
        child = context.Process(target=_try_guard, args=(str(guard), result))
        child.start()
        child.join(10)
        assert not child.is_alive()
        assert child.exitcode == 0
        assert result.get(timeout=2) == 'blocked'
        child.close()
    assert guard.exists()
    with writer_guard(guard):
        pass
    result.close()
    result.join_thread()


def _exit_holding_guard(path: str, connection):
    from job_radar.local_state import writer_guard
    with writer_guard(Path(path)):
        connection.send('held')
        connection.close()
        os._exit(0)


def test_guard_releases_after_abrupt_owner_exit(tmp_path: Path):
    from job_radar.local_state import writer_guard
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    guard = tmp_path / 'abrupt.guard'
    process = context.Process(target=_exit_holding_guard, args=(str(guard), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv() == 'held'
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
        with writer_guard(guard):
            assert guard.exists()
    finally:
        parent.close()
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()


@pytest.mark.parametrize('suffix', ['', '.guard'])
def test_output_cannot_replace_a_checkpoint_or_its_guard(fixture_root: Path, suffix: str):
    checkpoint = fixture_root / 'collision.json'
    with pytest.raises(ValueError, match='separate'):
        scan_catalog_resilient(catalog('healthy'), recorded_transport, mode='best-effort',
                               per_source_timeout=2, output=Path(str(checkpoint) + suffix),
                               checkpoint=checkpoint)
    assert calls(fixture_root, 'healthy') == 0
    assert not checkpoint.exists()
