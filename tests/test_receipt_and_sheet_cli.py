from __future__ import annotations

import io
import json
import pytest
from dataclasses import asdict

from job_radar.cli import main
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.domain.jobs import Job
from job_radar.domain.tracking import stable_job_id
from job_radar.sheet_sync import SheetOutbox


def snapshot(tmp_path):
    job = Job(source='greenhouse', external_id='synthetic-1', company='Synthetic Example',
              title='Backend Engineer', url='https://example.test/jobs/1', jd_hash='a' * 64,
              location='Synthetic', published_at=None)
    path = tmp_path / 'jobs.json'
    path.write_text(json.dumps([asdict(job)]), encoding='utf8')
    job_id = stable_job_id(source=job.source, external_id=job.external_id,
                           company=job.company, title=job.title)
    return path, job_id


def invoke(args, **kwargs):
    output = io.StringIO()
    code = main(['--json', *args], stdout=output, **kwargs)
    return code, json.loads(output.getvalue())


def receipt_args(tmp_path, command='record'):
    jobs, job_id = snapshot(tmp_path)
    args = ['review-receipt', command, '--user-data', str(tmp_path / 'data'),
            '--jobs', str(jobs), '--job-id', job_id, '--jd-sha256', 'a' * 64,
            '--resume-sha256', 'b' * 64]
    if command == 'record':
        args += ['--reviewer', 'Synthetic reviewer', '--evidence', 'review-fixture-1',
                 '--decision', 'approve', '--reviewed-at', '2026-01-01T00:00:00Z']
    return args


def test_receipt_requires_explicit_attestation_and_snapshot_match(tmp_path):
    args = receipt_args(tmp_path)
    assert invoke(args)[0] == 2
    store = LocalTrackingStore(tmp_path / 'data')
    assert not store.path_for('review_receipts').exists()
    bad = [*args, '--confirm-reviewed']
    bad[bad.index('--jd-sha256') + 1] = 'c' * 64
    assert invoke(bad)[0] == 2
    assert not store.path_for('review_receipts').exists()


def test_receipt_cli_is_exact_resume_version_bound_and_exportable(tmp_path):
    args = receipt_args(tmp_path)
    assert invoke([*args, '--confirm-reviewed'])[0] == 0
    check = receipt_args(tmp_path, 'check')
    code, result = invoke(check)
    assert code == 0 and result['result']['approved'] is True
    check[check.index('--resume-sha256') + 1] = 'c' * 64
    assert invoke(check)[1]['result']['reason'] == 'version_mismatch'
    exported = tmp_path / 'export.json'
    assert invoke(['tracking', 'export', '--user-data', str(tmp_path / 'data'), '--output', str(exported)])[0] == 0
    assert len(json.loads(exported.read_text(encoding='utf8'))['state']['review_receipts']['items']) == 1


def test_receipt_cli_future_and_unknown_job_do_not_create_receipts(tmp_path):
    args = [*receipt_args(tmp_path), '--confirm-reviewed']
    args[args.index('--reviewed-at') + 1] = '9999-01-01T00:00:00Z'
    assert invoke(args)[0] == 2
    args[args.index('--job-id') + 1] = 'unknown'
    assert invoke(args)[0] == 2
    assert not LocalTrackingStore(tmp_path / 'data').path_for('review_receipts').exists()


def test_sheet_enqueue_is_local_duplicate_safe_and_delivery_requires_confirmation(tmp_path):
    jobs, job_id = snapshot(tmp_path)
    root = tmp_path / 'data'
    args = ['sheet-sync', 'enqueue', '--user-data', str(root), '--jobs', str(jobs),
            '--job-id', job_id, '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest']
    first = invoke(args)
    second = invoke(args)
    assert first[0] == second[0] == 0
    assert first[1]['operation_id'] == second[1]['operation_id']
    outbox = SheetOutbox(LocalTrackingStore(root).path_for('sheet_outbox'))
    assert outbox.status()['total'] == 1
    calls = []
    code, report = invoke(['sheet-sync', 'deliver', '--user-data', str(root),
                          '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest'],
                         sheet_transport=lambda *a, **k: calls.append(a))
    assert code == 2 and report['ok'] is False and calls == []
    assert outbox.status()['items'][0]['attempts'] == 0


def test_sheet_cannot_infer_applied_from_browser_or_unconfirmed_state(tmp_path):
    jobs, job_id = snapshot(tmp_path)
    root = tmp_path / 'data'
    args = ['sheet-sync', 'enqueue', '--user-data', str(root), '--jobs', str(jobs),
            '--job-id', job_id, '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest',
            '--from-application']
    assert invoke(args)[0] == 2
    store = LocalTrackingStore(root)
    store.write('applications', {'contract_version': 1, 'items': [
        {'job_id': job_id, 'status': 'applied', 'applied_at': 'unknown', 'resume_version': 'go-v1'}]})
    assert invoke(args)[0] == 2
    assert not store.path_for('sheet_outbox').exists()


def test_sheet_projects_only_confirmed_minimal_application_fields(tmp_path):
    jobs, job_id = snapshot(tmp_path)
    root = tmp_path / 'data'
    store = LocalTrackingStore(root)
    store.write('applications', {'contract_version': 1, 'items': [{
        'job_id': job_id, 'status': 'applied', 'applied_at': 'unknown',
        'resume_version': 'go-v1', 'confirmation_evidence': 'synthetic-receipt-1',
        'confirmed_at': '2026-01-01T00:00:00Z', 'channel': 'synthetic', 'private_note': 'not shared'}]})
    args = ['sheet-sync', 'enqueue', '--user-data', str(root), '--jobs', str(jobs),
            '--job-id', job_id, '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest',
            '--from-application']
    assert invoke(args)[0] == 0
    item = json.loads(store.path_for('sheet_outbox').read_text(encoding='utf8'))['items'][0]
    assert set(item['fields']) == {'company', 'title', 'url', 'status', 'applied_at', 'resume_version', 'confirmation_evidence'}
    assert item['fields']['applied_at'] == 'unknown'
    assert item['fields']['status'] == 'applied'


def test_sheet_delivery_error_is_visible_and_durable(tmp_path):
    jobs, job_id = snapshot(tmp_path)
    root = tmp_path / 'data'
    assert invoke(['sheet-sync', 'enqueue', '--user-data', str(root), '--jobs', str(jobs),
                   '--job-id', job_id, '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest'])[0] == 0

    def offline(*args, **kwargs):
        raise RuntimeError('PRIVATE transport body')

    code, result = invoke(['sheet-sync', 'deliver', '--user-data', str(root),
                           '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest', '--confirm-write'],
                          sheet_transport=offline)
    assert code == 1 and result['ok'] is False
    assert result['result']['error'] == 'remote_unavailable'
    assert result['result']['pending'] == 1
    assert 'PRIVATE' not in json.dumps(result)
    assert SheetOutbox(LocalTrackingStore(root).path_for('sheet_outbox')).status()['items'][0]['attempts'] == 1


def test_actual_cli_export_and_delete_cover_real_optional_documents(tmp_path):
    assert invoke([*receipt_args(tmp_path), '--confirm-reviewed'])[0] == 0
    jobs, job_id = snapshot(tmp_path)
    root = tmp_path / 'data'
    assert invoke(['sheet-sync', 'enqueue', '--user-data', str(root), '--jobs', str(jobs),
                   '--job-id', job_id, '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest'])[0] == 0
    output = tmp_path / 'export.json'
    assert invoke(['tracking', 'export', '--user-data', str(root), '--output', str(output)])[0] == 0
    state = json.loads(output.read_text(encoding='utf8'))['state']
    assert state['sheet_outbox']['items'][0]['state'] == 'pending'
    assert state['review_receipts']['items'][0]['decision'] == 'approve'
    assert invoke(['tracking', 'delete', '--user-data', str(root)])[0] == 0
    store = LocalTrackingStore(root)
    assert not store.path_for('sheet_outbox').exists() and not store.path_for('review_receipts').exists()
    assert output.exists()


@pytest.mark.parametrize('alias', ['state-link', 'parent-link', 'hardlink', 'guard-link'])
@pytest.mark.parametrize('operation', ['record', 'export', 'delete'])
@pytest.mark.parametrize('kind', ['review_receipts', 'sheet_outbox'])
def test_optional_managed_alias_is_rejected_through_actual_cli_without_touching_target(tmp_path, alias, operation, kind):
    args = [*receipt_args(tmp_path), '--confirm-reviewed']
    if kind == 'sheet_outbox':
        jobs, job_id = snapshot(tmp_path)
        args = ['sheet-sync', 'enqueue', '--user-data', str(tmp_path / 'data'), '--jobs', str(jobs),
                '--job-id', job_id, '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest']
    root = tmp_path / 'data'
    root.mkdir()
    private = root / 'not-managed.json'
    private.write_text('{"contract_version":1,"items":[]}\n', encoding='utf8')
    saved = private.read_bytes()
    filename = 'receipts.json' if kind == 'review_receipts' else 'sheets-outbox.json'
    suffix = '.guard' if kind == 'review_receipts' else '.lock'
    directory = root / ('review' if kind == 'review_receipts' else 'tracking')
    if alias == 'parent-link':
        alternate = root / 'unmanaged-directory'
        alternate.mkdir()
        target = alternate / filename
        target.write_bytes(saved)
        private = target
        directory.symlink_to(alternate, target_is_directory=True)
    else:
        directory.mkdir()
        target = directory / (filename + suffix if alias == 'guard-link' else filename)
        if alias == 'hardlink':
            target.hardlink_to(private)
        else:
            target.symlink_to(private)
    if operation != 'record':
        args = ['tracking', operation, '--user-data', str(root)]
        if operation == 'export':
            args += ['--output', str(tmp_path / 'export.json')]
    code, _report = invoke(args)
    assert code == 2
    assert private.read_bytes() == saved
    assert not (tmp_path / 'export.json').exists()
    if alias == 'parent-link':
        assert {path.name for path in alternate.iterdir()} == {filename}


@pytest.mark.parametrize('kind', ['review_receipts', 'sheet_outbox'])
def test_cli_does_not_resolve_away_an_aliased_optional_user_data_root(tmp_path, kind):
    args = [*receipt_args(tmp_path), '--confirm-reviewed']
    root = tmp_path / 'data'
    root.mkdir()
    alias = tmp_path / 'root-alias'
    alias.symlink_to(root, target_is_directory=True)
    if kind == 'sheet_outbox':
        jobs, job_id = snapshot(tmp_path)
        args = ['sheet-sync', 'enqueue', '--user-data', str(root), '--jobs', str(jobs),
                '--job-id', job_id, '--spreadsheet-id', 'synthetic-sheet', '--tab', 'SyncTest']
    args[args.index('--user-data') + 1] = str(alias)
    assert invoke(args)[0] == 2
    assert list(root.iterdir()) == []

