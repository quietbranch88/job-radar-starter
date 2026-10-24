from dataclasses import asdict
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path

import pytest

from job_radar.adapters import parse_greenhouse
from job_radar.cli import main
from job_radar.config import Companies, Matching, Preferences, Profile, UserConfig
from job_radar.data.tracking_store import LocalTrackingStore
from job_radar.public_workflow import execute_public_workflow
from job_radar.review_receipts import ReviewReceiptStore


CONFIG = UserConfig(Profile(('Go',), ''), Preferences(('CA',), ('backend',), (), False),
                    Companies((), ()), Matching(0, (), ()))
ALIASES = {'contract_version': 1, 'company_aliases': {}, 'title_aliases': {}}
NOW = '2026-10-24T00:00:00Z'
RESUME = 'b' * 64


def setup(tmp_path):
    job = asdict(parse_greenhouse({'jobs': [{
        'id': 'one', 'title': 'Backend Engineer', 'location': {'name': 'Toronto'},
        'absolute_url': 'https://example.test/job/one', 'content': 'Go and SQL required',
    }]}, 'Synthetic Systems')[0])
    scan = {'contract_version': 1, 'mode': 'atomic', 'incomplete': False,
            'jobs': [job], 'failures': []}
    store = LocalTrackingStore(tmp_path / 'user-data')
    result = execute_public_workflow(scan_result=scan, config=CONFIG, repository=store,
                                    observed_on='2026-10-24', aliases=ALIASES)
    current = result['view_model']['jobs'][0]
    receipts = ReviewReceiptStore(store.path_for('review_receipts'))
    return scan, store, current, receipts


def record(receipts, job, *, resume=RESUME, decision='approve', at='2026-10-23T00:00:00Z'):
    return receipts.record(job['stable_id'], job['jd_hash'], resume, 'Independent Reviewer',
                           'private-local-evidence', decision, at, confirmed=True)


def run(scan, store, receipts, *, resume=RESUME):
    return execute_public_workflow(scan_result=scan, config=CONFIG, repository=store,
        observed_on='2026-10-24', aliases=ALIASES, receipt_checker=receipts,
        resume_sha256=resume, review_asof=NOW)


def test_daily_workflow_reads_exact_receipt_without_provider_or_private_evidence(tmp_path):
    scan, store, job, receipts = setup(tmp_path)
    receipt = record(receipts, job)
    before = receipts.path.read_bytes()
    result = run(scan, store, receipts)
    approval = result['view_model']['jobs'][0]['operator_review']
    assert approval == {'applicable': True, 'approved': True, 'decision': 'approve',
                        'reason': 'matched', 'receipt_id': receipt['receipt_id']}
    assert result['review']['provider_calls'] == 0
    assert result['operator_review'] == {'approved': 1, 'rejected': 0, 'pending': 0}
    assert 'private-local-evidence' not in json.dumps(result['view_model'])
    assert 'Independent Reviewer' not in json.dumps(result['view_model'])
    assert receipts.path.read_bytes() == before
    assert 'operator_review' not in store.read('lifecycle')['items'][0]
    assert store.read('applications')['items'] == []


@pytest.mark.parametrize('case,reason', [
    ('resume', 'version_mismatch'), ('jd', 'version_mismatch'),
    ('future', 'not_yet_reviewed'), ('missing', 'no_receipt'),
    ('no-selection', 'resume_not_selected'), ('no-jd', 'jd_unavailable'),
])
def test_daily_projection_fails_closed_for_unreviewed_versions(tmp_path, case, reason):
    scan, store, job, receipts = setup(tmp_path)
    if case != 'missing':
        record(receipts, job, at='2026-10-25T00:00:00Z' if case == 'future' else '2026-10-23T00:00:00Z')
    if case in ('jd', 'no-jd'):
        scan['jobs'][0]['jd_hash'] = 'c' * 64 if case == 'jd' else ''
    result = run(scan, store, receipts, resume='c' * 64 if case == 'resume' else None if case == 'no-selection' else RESUME)
    approval = result['view_model']['jobs'][0]['operator_review']
    assert approval['approved'] is False
    assert approval['reason'] == reason
    assert result['operator_review']['pending'] == 1


def test_equal_time_reject_wins_without_hiding_job_or_confirming_application(tmp_path):
    scan, store, job, receipts = setup(tmp_path)
    record(receipts, job, decision='reject')
    record(receipts, job)
    result = run(scan, store, receipts)
    approval = result['view_model']['jobs'][0]['operator_review']
    assert approval['applicable'] is True and approval['approved'] is False
    assert approval['decision'] == 'reject'
    assert result['operator_review'] == {'approved': 0, 'rejected': 1, 'pending': 0}
    assert result['counts']['published'] == 1


def invoke(args, **kwargs):
    out, err = StringIO(), StringIO()
    code = main(['--json', *args], stdout=out, stderr=err, **kwargs)
    return code, json.loads(out.getvalue())


def test_real_cli_run_and_rebuild_project_receipt_into_html(tmp_path):
    scan, store, job, receipts = setup(tmp_path)
    record(receipts, job)
    profile = tmp_path / 'profile.yaml'
    profile.write_text('profile: {skills: [Go]}\nmatching: {minimum_score: 0}\n', encoding='utf-8')
    catalog = tmp_path / 'catalog.yaml'
    catalog.write_text('countries: {}\nsources: {}\ncompanies: {}\n', encoding='utf-8')
    jobs, html = tmp_path / 'jobs.json', tmp_path / 'dashboard.html'
    code, report = invoke(['run', '--catalog', str(catalog), '--profile', str(profile),
        '--jobs-output', str(jobs), '--dashboard-output', str(html), '--user-data', str(store.root),
        '--observed-on', '2026-10-24', '--resume-sha256', RESUME], scan_runner=lambda *a, **k: scan)
    assert code == 0, report
    assert report['operator_review']['approved'] == 1
    execution_receipt = json.loads(Path(report['run_manifest']).read_text(encoding='utf-8'))
    assert execution_receipt['operator_review_context']['resume_sha256'] == RESUME
    assert datetime.fromisoformat(execution_receipt['operator_review_context']['asof']).tzinfo is not None
    assert 'Operator review: approved' in html.read_text(encoding='utf-8')
    assert 'private-local-evidence' not in html.read_text(encoding='utf-8')
    record(receipts, job, decision='reject', at=datetime.now(timezone.utc).isoformat())
    code, report = invoke(['build-dashboard', '--jobs', str(jobs), '--output', str(html),
                          '--user-data', str(store.root), '--resume-sha256', RESUME])
    assert code == 0, report
    assert 'Operator review: rejected' in html.read_text(encoding='utf-8')


def test_selected_resume_and_corrupt_receipt_never_replace_last_good_html(tmp_path):
    scan, store, job, receipts = setup(tmp_path)
    record(receipts, job)
    receipts.path.write_text('{broken', encoding='utf-8')
    jobs, html = tmp_path / 'jobs.json', tmp_path / 'dashboard.html'
    jobs.write_text(json.dumps(scan), encoding='utf-8')
    html.write_text('last good', encoding='utf-8')
    code, _ = invoke(['build-dashboard', '--jobs', str(jobs), '--output', str(html),
                     '--user-data', str(store.root), '--resume-sha256', RESUME])
    assert code == 2
    assert html.read_text(encoding='utf-8') == 'last good'


def test_default_daily_projection_creates_no_optional_receipt_state(tmp_path):
    scan, store, job, receipts = setup(tmp_path)
    result = run(scan, store, receipts, resume=None)
    assert result['view_model']['jobs'][0]['operator_review']['reason'] == 'resume_not_selected'
    assert not receipts.path.exists()
    assert not receipts.path.with_name(receipts.path.name + '.guard').exists()


@pytest.mark.parametrize('value', ['', 'A' * 64, 'b' * 63, 'not-a-hash'])
def test_invalid_resume_selection_is_rejected_before_workflow_initialization(tmp_path, value):
    from job_radar.review_projection import project_review_receipts

    with pytest.raises(ValueError, match='SHA256'):
        project_review_receipts([], resume_sha256=value)


def test_resume_projection_requires_explicit_aware_current_time(tmp_path):
    from job_radar.review_projection import project_review_receipts

    _, _, job, receipts = setup(tmp_path)
    with pytest.raises(ValueError, match='explicit time'):
        project_review_receipts([job], checker=receipts, resume_sha256=RESUME)
    with pytest.raises(ValueError, match='timezone'):
        project_review_receipts([job], checker=receipts, resume_sha256=RESUME, asof='2026-10-24T00:00:00')
