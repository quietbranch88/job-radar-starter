from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
import threading

import pytest

from job_radar.cli import main
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar import run_receipt


def fixtures(root: Path):
    jobs = root / 'jobs.json'
    jobs.write_text(json.dumps([dict(source='greenhouse', external_id='safe-1', company='Fixture',
                                     title='Go Backend Engineer', location='Remote',
                                     url='https://example.test/jobs/1', published_at='2026-07-17')]), encoding='utf-8')
    profile = root / 'profile.yaml'
    profile.write_text('profile: {skills: [Go]}\npreferences: {roles: [backend]}\nmatching: {minimum_score: 0}\n', encoding='utf-8')
    store = LocalTrackingStore((root / 'user-data').resolve())
    store.initialize()
    return jobs, profile, store


@pytest.mark.parametrize('target', ['jobs', 'profile', 'tracking'])
def test_offline_profile_build_cannot_overwrite_input_or_tracking(tmp_path: Path, target: str):
    jobs, profile, store = fixtures(tmp_path)
    output = {'jobs': jobs, 'profile': profile, 'tracking': store.path_for('lifecycle')}[target]
    before = output.read_bytes()
    code = main(['build-dashboard', '--jobs', str(jobs), '--profile', str(profile),
                 '--user-data', str(store.root), '--output', str(output)], stdout=StringIO())
    assert output.read_bytes() == before, 'offline output overwrote source/configuration/tracking bytes'
    assert code == 2


def test_run_dashboard_cannot_replace_the_live_output_guard(tmp_path: Path):
    jobs, profile, store = fixtures(tmp_path)
    catalogue = tmp_path / 'catalog.yaml'
    catalogue.write_text('sources: {}\ncompanies: {}\n', encoding='utf-8')
    invoked = []

    def scanner(*args, **kwargs):
        invoked.append(True)
        return {'contract_version': 1, 'mode': 'atomic', 'incomplete': False, 'jobs': [], 'failures': []}

    code = main(['run', '--catalog', str(catalogue), '--profile', str(profile),
                 '--jobs-output', str(jobs), '--dashboard-output', str(jobs.with_name(jobs.name + '.run.guard')),
                 '--user-data', str(store.root), '--aliases', str(tmp_path / 'absent.json')],
                scan_runner=scanner, stdout=StringIO())
    assert invoked == [], 'reserved guard alias was accepted before running the scanner'
    assert code == 2


def test_competing_attempts_cannot_own_the_same_explicit_receipt(tmp_path: Path, monkeypatch):
    path = tmp_path / 'one-attempt.json'
    first_in_write, release_write = threading.Event(), threading.Event()
    original = run_receipt.write_state
    entries, completed, failures = [], [], []
    counter_lock = threading.Lock()

    def controlled_write(target, document):
        with counter_lock:
            entries.append(document['run_id'])
            first = len(entries) == 1
        if first:
            first_in_write.set()
            assert release_write.wait(5), 'test did not release the controlled local writer'
        else:
            release_write.set()
        original(target, document)

    monkeypatch.setattr(run_receipt, 'write_state', controlled_write)

    def first_attempt():
        try:
            completed.append(run_receipt.RunReceipt(tmp_path / 'first-jobs.json', path=path))
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=first_attempt)
    thread.start()
    rejected = False
    try:
        assert first_in_write.wait(5)
        try:
            run_receipt.RunReceipt(tmp_path / 'different-jobs.json', path=path)
        except ValueError:
            rejected = True
    finally:
        release_write.set()
        thread.join(5)
    assert not thread.is_alive()
    assert failures == []
    assert rejected, 'two distinct run IDs acquired the same supposedly new receipt'
    assert len(entries) == len(completed) == 1
    assert json.loads(path.read_text(encoding='utf-8'))['run_id'] == completed[0].document['run_id']
